# Aurora Social Sandbox

A local-only continuation build for a dual-currency social/sweepstakes prototype. **Live payments and cash payouts are intentionally disabled.**

## What is implemented

- Separate GC and SC wallets.
- Immutable double-entry journal; every transaction must balance by currency.
- Server-priced sandbox GC packages with promotional SC.
- Daily free grant with one-claim-per-day persistence.
- Three original server-authoritative games with published probability/RTP metadata.
- Cryptographically secure server draws using Python `secrets`; there is no admin control to choose winners or change a player's result.
- Idempotent purchases, rounds, grants, credits and redemption requests.
- SC risk-budget admission control for maximum open-round exposure.
- Pending-redemption liability state; no actual payout integration.
- Self-exclusion/access gates.
- Operator treasury view and immutable admin audit events.
- Responsive mobile player UI.
- 20 automated financial/control tests.

## Run

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

Default sandbox admin header: `X-Admin-Key: dev-admin-key`. Change it with `ADMIN_KEY`.

## Tests

```bash
pytest -q
```

## Database

The default database is SQLite (`./aurora.db`) to keep the sandbox zero-cost and one-command. Set `DATABASE_URL` to a supported SQLAlchemy relational database for integration work.

The sandbox wraps financial transitions in a process-level lock plus database uniqueness constraints. Before any multi-instance deployment, replace the process lock with database-native transaction/row locking and verify behavior under the selected database.

## Deliberate live-launch gates

This build does **not** include a real acquiring processor, cash payout provider, production KYC/geolocation, jurisdiction allowlisting, licensed game supplier, or a live prize reserve account. Those should remain disabled until the exact product/markets and providers are approved and the reserve model is funded.

Outcome probabilities are never changed in response to revenue, player history, purchase behavior, treasury position or operator preference. Treasury controls may deny *new* SC risk when there is insufficient pre-funded exposure headroom; they do not alter already-admitted game results.
