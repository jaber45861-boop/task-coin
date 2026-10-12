# Automatic EGP → USDT rate (feat/auto-rate)

Development branch documentation — NOT deployed, NOT merged. Production
access, production data and deploys are out of scope for this change.

## Policy in one paragraph

The automatic provider (CoinGecko) is the NORMAL rate source: a
background worker refreshes the singleton `current_rate` row every
840 s, strictly below the existing 900 s freshness TTL. The manual
`/setrate` path is EMERGENCY-ONLY: while a fresh automatic quote
exists the command is refused; it opens only once the provider fails
to keep one fresh (missing / stale / corrupt row), and only for a
configured admin. A successful automatic refresh overwrites any
manual emergency quote and re-closes the gate. Deposits that need a
conversion (EGP) fail closed — clear `rate_unavailable` error, no
row — whenever no fresh, valid quote exists, and each created request
FREEZES its rate, provider, capture instant and exact converted USDT
units, which are the only units ever credited (later rate changes
never rewrite an old request).

## Behavior added

- `rate_api.py` — fetches the `tether`/`egp` quote from CoinGecko's
  simple-price endpoint; bounded body read (64 KiB), 8 s default
  timeout, aware-UTC capture instant; every transport/payload problem
  raises `RateProviderError` (fail-closed). Provider constant comes
  from `rate_quote.PROVIDER_COINGECKO` (single source of truth).
- `rate_worker.py` — single daemon thread (idempotent `start()`,
  testable `_refresh_once()`); first cycle runs immediately at
  startup; a failed cycle writes NOTHING so the previous quote ages
  out on schedule; started from `bot.main()` and from
  `serve_miniapp.py`'s `__main__` block.
- `rate_store.set_automatic_rate()` — the system writer
  (`updated_by = 0`, provider `coingecko`, capture instant verbatim);
  rejects any other provider; overwrites a manual quote (recovery).
- `rate_store.set_rate()` — unchanged authorization-first contract,
  plus the emergency gate (`ManualRateBlockedError`) and an audit log
  line that records value + admin id + WHY the gate was open.
- `rate_admin` — operator-facing Arabic refusal
  (`MSG_EMERGENCY_ONLY`) for the gated case; all other behavior
  untouched.
- `deposit_requests` — additive nullable snapshot columns
  `wallet_rate_usdt_egp`, `rate_provider`, `rate_captured_at`,
  `credited_usdt_units` (CHECK `IS NULL OR > 0`), created for fresh
  databases in `CREATE TABLE` and added by a per-column guarded
  `ALTER TABLE` for existing databases (idempotent; legacy rows keep
  NULL and are refused at the credit boundary — never backfilled).
- `deposit_store.create_deposit_request()` — for EGP methods, reads
  the fresh quote on the SAME transaction and freezes rate +
  `egp_to_wallet_units()` (Decimal, ROUND_CEILING) into the row;
  missing/stale/corrupt quote → nothing inserted.
- `deposit_verification` — USDT path byte-for-byte unchanged; EGP
  credits ONLY the frozen units, with the frozen rate recorded on the
  ledger entry and full conversion metadata; a snapshot that is
  missing/corrupt (or an unapproved provider) is rejected BEFORE any
  wallet/ledger write; idempotent replay reports the same credited
  units; the amount check stays in the request's own EGP units.
- `deposit_routes` — stable `rate_unavailable` (503) with a clear
  Arabic message for rate failures; responses still never carry rate
  facts.
- `conftest.py` — tests never spawn the background worker (real
  network I/O + process-global `db.DB_PATH` writes); tests drive
  `_refresh_once()` explicitly; `bot.main()` registration tests pin
  that the worker start IS wired.

## Configuration (optional, no secrets in this tree)

- `COINGECKO_API_KEY` — sent as `x-cg-demo-api-key` when set.
- `TASKCOIN_RATE_API_URL` — endpoint override (tests/mirrors).

## Validation status (this branch, this environment)

- `python3 -m pytest -q` → **3810 passed, 11 skipped**, exit 0
  (full repository suite; the 11 skips predate this change).
- Targeted suites: rate (quote/store/api/worker) 97 passed; deposit
  (store/verification/proof/admin-control/manual-review) all passed.
- Requirement mapping lives in the test names: auto write, gate in
  normal mode, emergency path for admins only, rejected
  missing/stale/corrupt quotes, first-fetch failure, failure never
  extends freshness, return-to-normal after recovery, conversion
  accuracy/rounding/units, frozen old requests, no double credit,
  network/timeout/invalid-response handling, and migration of a
  pre-existing database (init_db on an old-schema file).
- Production server and production database were not accessed or
  changed; nothing was deployed.

## Decisions that stay open for a human

1. Emergency manual quotes expire under the SAME 900 s TTL — an admin
   must re-set the price after 15 minutes of provider outage, or the
   provider must recover. A separate (longer) manual validity window
   would be a new financial policy and was deliberately NOT invented.
2. The audit "reason" is logged (value + admin id + evaluated
   emergency context) but not persisted as a column; a
   `manual_reason` column on `current_rate` would be an additive
   schema change needing approval.
3. CoinGecko is a market REFERENCE price, not a guaranteed execution
   price — the mapping is documented in `rate_quote`/`rate_api`
   docstrings; changing providers means adding one constant.
