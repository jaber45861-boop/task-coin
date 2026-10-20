# Automatic EGP → USDT rate (feat/auto-rate)

Development branch documentation — NOT deployed, NOT merged. Production
access, production data and deploys are out of scope for this change.

## Policy in one paragraph

The automatic provider (**Coinbase**) is the NORMAL rate source: a
background worker refreshes the singleton `current_rate` row every
840 s, strictly below the existing 900 s freshness TTL.

> **Coinbase was chosen over CoinGecko because CoinGecko's public tier
> does not serve the `egp` field.** Verified against the live API (not
> assumed): `simple/price?ids=tether&vs_currencies=egp` returns
> `{"tether":{}}` while `usd`/`eur` are returned normally, so the
> CoinGecko fetcher can only succeed on a plan/endpoint that has the
> field. Coinbase serves a DIRECT USDT/EGP spot quote with **no API
> key and no subscription**. The CoinGecko fetcher is retained and
> fully tested, and both providers are approved interchangeably.

> **The price is a market REFERENCE price from one venue (Coinbase),
> not a guaranteed execution price.** There is no guarantee that a
> user's transfer matches it; it is the rate used to freeze and credit
> a deposit request, and nothing more. The manual
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

- `rate_api.py` — two fetchers, both fail-closed (bounded body read
  64 KiB, 8 s default timeout, aware-UTC capture instant; every
  transport/payload problem raises `RateProviderError`):
  - `fetch_usdt_egp_quote_coinbase()` — **the provider the worker
    uses.** `GET {base}/v2/prices/USDT-EGP/spot`; validates HTTP 200,
    a bounded (non-truncated) body, and `data.amount` /
    `data.base == "USDT"` / `data.currency == "EGP"` BEFORE parsing.
    A mismatched or mislabelled pair is refused, not half-trusted.
  - `fetch_usdt_egp_quote()` — CoinGecko `simple/price`
    (`tether`/`egp`); retained for a plan/endpoint that serves `egp`.
  Provider constants come from `rate_quote.PROVIDER_COINGECKO` /
  `PROVIDER_COINBASE` (single source of truth).
- `rate_worker.py` — single daemon thread (idempotent `start()`,
  testable `_refresh_once()`); first cycle runs immediately at
  startup; a failed cycle writes NOTHING so the previous quote ages
  out on schedule; started from `bot.main()` and from
  `serve_miniapp.py`'s `__main__` block.
- `rate_store.set_automatic_rate()` — the system writer
  (`updated_by = 0`; the quote's OWN provider — `coingecko` or
  `coinbase` — is persisted verbatim with its capture instant and is
  never rewritten to a different value); accepts every approved
  AUTOMATIC provider (`APPROVED_RATE_PROVIDERS` minus `manual`),
  rejects `manual` and anything unapproved; overwrites a manual quote
  (recovery).
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

- `COINGECKO_API_KEY` — sent as `x-cg-demo-api-key` when set
  (CoinGecko fetcher ONLY; **the Coinbase fetcher needs no key at
  all**, so no secret has to exist for the normal path).
- `TASKCOIN_RATE_API_URL` — overrides the endpoint base. NOTE: it is
  read by BOTH fetchers, so a mirror override applies to whichever
  fetcher is invoked; point it at a base that serves the corresponding
  response shape.
- `TASKCOIN_RATE_API_URL` — endpoint override (tests/mirrors).

## Validation status (this branch, this environment)

- `python3 -m pytest -q` → **3810 passed, 11 skipped**, exit 0
  (full repository suite; the 11 skips predate this change).
- Targeted suites: rate (quote/store/api/worker) **102 passed**,
  exit 0; deposit (store/verification/proof/admin-control/
  manual-review) all passed in the full run.
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
3. Coinbase is a market REFERENCE price from a single venue, not a
   guaranteed execution price — documented in the fetcher/`rate_quote`
   docstrings and in this file; changing providers means adding one
   constant to `APPROVED_RATE_PROVIDERS` plus a fetcher.
