import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .db import Base, engine, get_db, SessionLocal
from .models import User, Wallet, Purchase, GameRound, Redemption, DailyGrant, ExposureState, AuditEvent
from .ledger import get_balances, post_balanced, InsufficientFunds, assert_journal_balanced
from .games import GAMES, settle

APP_NAME = "Aurora Social Sandbox"
ADMIN_KEY = os.getenv("ADMIN_KEY", "dev-admin-key")
financial_lock = threading.RLock()

PACKAGES = {
    "starter": {"usd_cents": 499, "gc": 50_000, "sc": 2},
    "popular": {"usd_cents": 1999, "gc": 250_000, "sc": 10},
    "vault": {"usd_cents": 4999, "gc": 700_000, "sc": 25},
}
DAILY_GRANT = {"gc": 2_500, "sc": 1}

app = FastAPI(title=APP_NAME, version="0.4.0")
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

class PlayIn(BaseModel):
    request_id: str = Field(min_length=8, max_length=180)
    user_id: int = 1
    game_key: str
    currency: str
    stake: int = Field(gt=0, le=10_000)

class PurchaseIn(BaseModel):
    request_id: str = Field(min_length=8, max_length=180)
    user_id: int = 1
    package_key: str

class RedemptionIn(BaseModel):
    request_id: str = Field(min_length=8, max_length=180)
    user_id: int = 1
    amount: int = Field(gt=0, le=1_000_000)

class CreditIn(BaseModel):
    request_id: str = Field(min_length=8, max_length=180)
    user_id: int = 1
    currency: str
    amount: int = Field(gt=0, le=10_000_000)

class UserFlagsIn(BaseModel):
    sandbox_access: bool | None = None
    is_self_excluded: bool | None = None


def require_admin(x_admin_key: str | None = Header(default=None)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="admin authentication required")
    return True


def init_db():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        user = db.get(User, 1)
        if not user:
            user = User(id=1, email="demo@aurora.local", display_name="Demo Player", state_code="OH", sandbox_access=True)
            db.add(user)
            db.flush()
            db.add_all([Wallet(user_id=1, currency="GC", balance=25_000), Wallet(user_id=1, currency="SC", balance=10)])
        if not db.get(ExposureState, 1):
            db.add(ExposureState(id=1, reserved_sc=0, max_sc=100_000))
        db.commit()

init_db()

@app.get("/")
def home():
    return FileResponse(static_dir / "index.html")

@app.get("/api/health")
def health():
    return {"ok": True, "app": APP_NAME, "mode": "sandbox", "live_payments": False, "cash_payouts": False}

@app.get("/api/games")
def games():
    return [{"key": g.key, "name": g.name, "version": g.version, "rtp": g.rtp, "max_multiplier": g.max_multiplier, "description": g.description} for g in GAMES.values()]

@app.get("/api/packages")
def packages():
    return PACKAGES

@app.get("/api/player/{user_id}")
def player(user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "user not found")
    return {
        "user": {"id": user.id, "display_name": user.display_name, "email": user.email, "state_code": user.state_code, "sandbox_access": user.sandbox_access, "is_self_excluded": user.is_self_excluded},
        "wallets": get_balances(db, user_id),
    }


def check_play_access(user: User, currency: str):
    if not user.sandbox_access:
        raise HTTPException(403, "sandbox access disabled")
    if user.is_self_excluded:
        raise HTTPException(403, "play disabled by self-exclusion")
    if currency not in {"GC", "SC"}:
        raise HTTPException(400, "currency must be GC or SC")

@app.post("/api/daily-grant/{user_id}")
def daily_grant(user_id: int, db: Session = Depends(get_db)):
    today = datetime.now(timezone.utc).date().isoformat()
    with financial_lock:
        existing = db.scalar(select(DailyGrant).where(DailyGrant.user_id == user_id, DailyGrant.grant_date == today))
        if existing:
            return {"claimed": False, "date": today, "wallets": get_balances(db, user_id)}
        user = db.get(User, user_id)
        if not user or not user.sandbox_access:
            raise HTTPException(403, "grant unavailable")
        db.add(DailyGrant(user_id=user_id, grant_date=today, gc_amount=DAILY_GRANT["gc"], sc_amount=DAILY_GRANT["sc"]))
        post_balanced(db, idempotency_key=f"daily:{user_id}:{today}:gc", kind="daily_grant", entries=[
            {"user_id": user_id, "account": "wallet", "currency": "GC", "amount": DAILY_GRANT["gc"]},
            {"account": "promo_issuance", "currency": "GC", "amount": -DAILY_GRANT["gc"]},
        ])
        post_balanced(db, idempotency_key=f"daily:{user_id}:{today}:sc", kind="daily_grant", entries=[
            {"user_id": user_id, "account": "wallet", "currency": "SC", "amount": DAILY_GRANT["sc"]},
            {"account": "promo_issuance", "currency": "SC", "amount": -DAILY_GRANT["sc"]},
        ])
        db.commit()
        return {"claimed": True, "date": today, "wallets": get_balances(db, user_id)}

@app.post("/api/sandbox/purchase")
def sandbox_purchase(body: PurchaseIn, db: Session = Depends(get_db)):
    if body.package_key not in PACKAGES:
        raise HTTPException(400, "unknown package")
    pkg = PACKAGES[body.package_key]
    with financial_lock:
        existing = db.scalar(select(Purchase).where(Purchase.request_id == body.request_id))
        if existing:
            if existing.user_id != body.user_id or existing.package_key != body.package_key:
                raise HTTPException(409, "idempotency key already used for different purchase")
            return {"purchase_id": existing.id, "replayed": True, "wallets": get_balances(db, body.user_id)}
        user = db.get(User, body.user_id)
        if not user or not user.sandbox_access:
            raise HTTPException(403, "purchase unavailable")
        purchase = Purchase(request_id=body.request_id, user_id=body.user_id, package_key=body.package_key, usd_cents=pkg["usd_cents"], gc_amount=pkg["gc"], sc_promo=pkg["sc"], status="settled")
        db.add(purchase)
        db.flush()
        post_balanced(db, idempotency_key=f"purchase:{body.request_id}:gc", kind="sandbox_purchase", entries=[
            {"user_id": body.user_id, "account": "wallet", "currency": "GC", "amount": pkg["gc"]},
            {"account": "gc_issuance", "currency": "GC", "amount": -pkg["gc"]},
        ], metadata={"package": body.package_key, "usd_cents": pkg["usd_cents"]})
        if pkg["sc"]:
            post_balanced(db, idempotency_key=f"purchase:{body.request_id}:sc", kind="promo_grant", entries=[
                {"user_id": body.user_id, "account": "wallet", "currency": "SC", "amount": pkg["sc"]},
                {"account": "promo_issuance", "currency": "SC", "amount": -pkg["sc"]},
            ], metadata={"package": body.package_key})
        db.commit()
        return {"purchase_id": purchase.id, "replayed": False, "server_price": pkg, "wallets": get_balances(db, body.user_id)}

@app.post("/api/play")
def play(body: PlayIn, db: Session = Depends(get_db)):
    if body.game_key not in GAMES:
        raise HTTPException(400, "unknown game")
    game = GAMES[body.game_key]
    with financial_lock:
        existing = db.scalar(select(GameRound).where(GameRound.request_id == body.request_id))
        if existing:
            if any([existing.user_id != body.user_id, existing.game_key != body.game_key, existing.currency != body.currency, existing.stake != body.stake]):
                raise HTTPException(409, "idempotency key already used for different round")
            return {"round_id": existing.id, "replayed": True, "payout": existing.payout, "result": json.loads(existing.result_json), "wallets": get_balances(db, body.user_id)}
        user = db.get(User, body.user_id)
        if not user:
            raise HTTPException(404, "user not found")
        check_play_access(user, body.currency)
        exposure = db.get(ExposureState, 1)
        reserve = game.max_multiplier * body.stake if body.currency == "SC" else 0
        if reserve and exposure.reserved_sc + reserve > exposure.max_sc:
            raise HTTPException(503, "SC risk budget temporarily unavailable")
        if reserve:
            exposure.reserved_sc += reserve
            db.flush()
        try:
            post_balanced(db, idempotency_key=f"round:{body.request_id}:stake", kind="game_stake", entries=[
                {"user_id": body.user_id, "account": "wallet", "currency": body.currency, "amount": -body.stake},
                {"account": "game_house", "currency": body.currency, "amount": body.stake},
            ], metadata={"game": body.game_key, "version": game.version})
            result = settle(body.game_key, body.stake)
            payout = int(result["payout"])
            if payout:
                post_balanced(db, idempotency_key=f"round:{body.request_id}:payout", kind="game_payout", entries=[
                    {"user_id": body.user_id, "account": "wallet", "currency": body.currency, "amount": payout},
                    {"account": "game_house", "currency": body.currency, "amount": -payout},
                ], metadata={"game": body.game_key, "version": game.version, "draw": result["draw"]})
            row = GameRound(request_id=body.request_id, user_id=body.user_id, game_key=body.game_key, game_version=game.version, currency=body.currency, stake=body.stake, payout=payout, result_json=json.dumps(result, sort_keys=True), status="settled")
            db.add(row)
            if reserve:
                exposure.reserved_sc -= reserve
            db.commit()
            db.refresh(row)
            return {"round_id": row.id, "replayed": False, "payout": payout, "result": result, "wallets": get_balances(db, body.user_id)}
        except InsufficientFunds as exc:
            db.rollback()
            raise HTTPException(409, str(exc))
        except Exception:
            db.rollback()
            raise

@app.post("/api/redemptions")
def create_redemption(body: RedemptionIn, db: Session = Depends(get_db)):
    with financial_lock:
        existing = db.scalar(select(Redemption).where(Redemption.request_id == body.request_id))
        if existing:
            if existing.user_id != body.user_id or existing.amount != body.amount:
                raise HTTPException(409, "idempotency key already used for different redemption")
            return {"redemption_id": existing.id, "status": existing.status, "replayed": True, "wallets": get_balances(db, body.user_id)}
        user = db.get(User, body.user_id)
        if not user or not user.sandbox_access:
            raise HTTPException(403, "redemption unavailable")
        if user.is_self_excluded:
            # Existing balances remain redeemable in the sandbox; self-exclusion blocks play, not legitimate withdrawal.
            pass
        row = Redemption(request_id=body.request_id, user_id=body.user_id, amount=body.amount, status="pending")
        db.add(row)
        db.flush()
        try:
            post_balanced(db, idempotency_key=f"redemption:{body.request_id}", kind="redemption_reserve", entries=[
                {"user_id": body.user_id, "account": "wallet", "currency": "SC", "amount": -body.amount},
                {"user_id": body.user_id, "account": "pending_redemption", "currency": "SC", "amount": body.amount},
            ], metadata={"redemption_id": row.id})
        except InsufficientFunds as exc:
            db.rollback()
            raise HTTPException(409, str(exc))
        db.commit()
        return {"redemption_id": row.id, "status": row.status, "replayed": False, "wallets": get_balances(db, body.user_id)}

@app.post("/api/admin/test-credit", dependencies=[Depends(require_admin)])
def admin_test_credit(body: CreditIn, db: Session = Depends(get_db)):
    if body.currency not in {"GC", "SC"}:
        raise HTTPException(400, "currency must be GC or SC")
    with financial_lock:
        try:
            _, created = post_balanced(db, idempotency_key=f"admin-credit:{body.request_id}", kind="admin_test_credit", entries=[
                {"user_id": body.user_id, "account": "wallet", "currency": body.currency, "amount": body.amount},
                {"account": "sandbox_admin_issuance", "currency": body.currency, "amount": -body.amount},
            ], metadata={"sandbox_only": True})
            if created:
                db.add(AuditEvent(actor="sandbox-admin", action="test_credit", detail_json=json.dumps(body.model_dump(), sort_keys=True)))
            db.commit()
            return {"created": created, "wallets": get_balances(db, body.user_id)}
        except Exception:
            db.rollback()
            raise

@app.patch("/api/admin/users/{user_id}/flags", dependencies=[Depends(require_admin)])
def admin_user_flags(user_id: int, body: UserFlagsIn, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "user not found")
    if body.sandbox_access is not None:
        user.sandbox_access = body.sandbox_access
    if body.is_self_excluded is not None:
        user.is_self_excluded = body.is_self_excluded
    db.add(AuditEvent(actor="sandbox-admin", action="user_flags", detail_json=json.dumps({"user_id": user_id, **body.model_dump(exclude_none=True)}, sort_keys=True)))
    db.commit()
    return {"ok": True, "sandbox_access": user.sandbox_access, "is_self_excluded": user.is_self_excluded}

@app.get("/api/admin/treasury", dependencies=[Depends(require_admin)])
def treasury(db: Session = Depends(get_db)):
    sc_wallet = db.scalar(select(func.coalesce(func.sum(Wallet.balance), 0)).where(Wallet.currency == "SC")) or 0
    gc_wallet = db.scalar(select(func.coalesce(func.sum(Wallet.balance), 0)).where(Wallet.currency == "GC")) or 0
    pending = db.scalar(select(func.coalesce(func.sum(Redemption.amount), 0)).where(Redemption.status == "pending")) or 0
    exposure = db.get(ExposureState, 1)
    ok, rows = assert_journal_balanced(db)
    return {
        "player_liabilities": {"GC_wallet_units": int(gc_wallet), "SC_wallet_units": int(sc_wallet), "SC_pending_redemptions": int(pending)},
        "risk": {"SC_reserved_open_rounds": exposure.reserved_sc, "SC_max_open_round_exposure": exposure.max_sc, "SC_headroom": exposure.max_sc - exposure.reserved_sc},
        "ledger_balanced": ok,
        "journal_groups_checked": len(rows),
        "live_payments_enabled": False,
        "cash_payouts_enabled": False,
    }

@app.get("/api/admin/audit", dependencies=[Depends(require_admin)])
def audit(db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(100)).all()
    return [{"id": r.id, "actor": r.actor, "action": r.action, "detail": json.loads(r.detail_json), "created_at": r.created_at} for r in rows]
