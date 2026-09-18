import json
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .models import Wallet, JournalTxn, JournalEntry

VALID_CURRENCIES = {"GC", "SC"}

class LedgerError(Exception):
    pass

class InsufficientFunds(LedgerError):
    pass

def get_wallet(db: Session, user_id: int, currency: str) -> Wallet:
    if currency not in VALID_CURRENCIES:
        raise LedgerError("unsupported currency")
    wallet = db.scalar(select(Wallet).where(Wallet.user_id == user_id, Wallet.currency == currency))
    if not wallet:
        wallet = Wallet(user_id=user_id, currency=currency, balance=0)
        db.add(wallet)
        db.flush()
    return wallet

def get_balances(db: Session, user_id: int):
    return {c: get_wallet(db, user_id, c).balance for c in sorted(VALID_CURRENCIES)}

def post_balanced(db: Session, *, idempotency_key: str, kind: str, entries: list[dict], metadata: dict | None = None):
    existing = db.scalar(select(JournalTxn).where(JournalTxn.idempotency_key == idempotency_key))
    if existing:
        return existing, False
    by_currency: dict[str, int] = {}
    for e in entries:
        currency = e["currency"]
        by_currency[currency] = by_currency.get(currency, 0) + int(e["amount"])
    if any(total != 0 for total in by_currency.values()):
        raise LedgerError(f"unbalanced journal: {by_currency}")
    txn = JournalTxn(idempotency_key=idempotency_key, kind=kind, metadata_json=json.dumps(metadata or {}, sort_keys=True))
    db.add(txn)
    db.flush()
    for e in entries:
        db.add(JournalEntry(txn_id=txn.id, user_id=e.get("user_id"), account=e["account"], currency=e["currency"], amount=int(e["amount"])))
        if e["account"] == "wallet":
            wallet = get_wallet(db, int(e["user_id"]), e["currency"])
            new_balance = wallet.balance + int(e["amount"])
            if new_balance < 0:
                raise InsufficientFunds(f"insufficient {e['currency']}")
            wallet.balance = new_balance
    db.flush()
    return txn, True

def assert_journal_balanced(db: Session):
    rows = db.execute(
        select(JournalEntry.txn_id, JournalEntry.currency, func.sum(JournalEntry.amount))
        .group_by(JournalEntry.txn_id, JournalEntry.currency)
    ).all()
    return all(total == 0 for _, _, total in rows), rows
