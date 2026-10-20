"""
Rate Store (MT-ADMIN-26 → automatic-first)
==========================================

The authoritative source of the current EGP-per-USDT rate — the
missing link MT-ADMIN-25's WithdrawalService will consume later.
Consumers read through ``get_current_quote`` only.

Modes (policy: automatic is NORMAL, manual is EMERGENCY-only)
-------------------------------------------------------------
- NORMAL: ``rate_worker`` fetches a market quote (``rate_api``,
  provider ``coingecko``) and persists it through
  ``set_automatic_rate`` — no human in the loop.
- EMERGENCY: while a fresh AUTOMATIC quote exists, ``set_rate``
  (``/setrate``) is BLOCKED (``ManualRateBlockedError``) — the manual
  path opens only once the automatic quote is missing or older than
  the TTL (i.e. the provider failed to keep it fresh), and only for a
  configured admin.  A successful automatic refresh overwrites a
  manual emergency quote and re-closes the gate (return to normal).
- Every write records who (``updated_by``: admin id, or ``0`` = the
  system worker) and when (``captured_at`` / ``updated_at``); the
  worker logs the automatic refresh, and ``set_rate`` logs every
  manual override (value + admin id + the evaluated emergency
  context — why the gate was open).
- Staleness applies to BOTH providers: one TTL, one parser, no
  provider-specific grace period.

There is no Mini App endpoint and this module performs no HTTP
fetching itself — ``rate_api`` owns that boundary.

Rate meaning
------------
``rate_usdt_egp`` is **EGP per 1 USDT** (``1 USDT = rate_usdt_egp
EGP``) — exactly the ``rate_quote.RateQuote`` contract.  It is a
market reference price, not a guaranteed execution price.

Storage — one dedicated singleton row, never platform_settings
---------------------------------------------------------------
``db.init_db`` creates::

    current_rate (
        id            INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton
        rate_usdt_egp TEXT NOT NULL,   -- canonical plain-decimal TEXT
        provider      TEXT NOT NULL,   -- "coingecko" (automatic)
                                      -- | "manual" (emergency)
        captured_at   TIMESTAMP NOT NULL,  -- aware UTC ISO-8601
        updated_by    INTEGER NOT NULL,    -- admin Telegram id | 0 = system
        updated_at    TIMESTAMP NOT NULL   -- aware UTC ISO-8601
    )

Deliberately NOT ``platform_settings``: that table is integer-only by
contract (``value INTEGER ... CHECK typeof='integer'``) and fractional
rates must never acquire a second encoding there.  No REAL/float type
exists anywhere in this schema — the rate is TEXT, validated and
canonicalized exclusively through the existing ``rate_quote.parse_rate``
/ ``rate_quote.canonical_rate_text`` contract (no second parser is
introduced).

Authorization
-------------
Mutation reuses the ONE existing model — ``config.is_admin`` (imported,
never re-implemented; no admin id is hardcoded here).

Connection ownership (mirrors platform_settings / MT-ADMIN-23)
--------------------------------------------------------------
- reads: caller-provided ``connection=`` is borrowed exactly as-is —
  never committed, rolled back or closed, no hidden second connection
  (this is what MT-ADMIN-25's atomic withdrawal transaction needs);
  otherwise the standard ``db.get_connection()`` read scope opens;
- writes: one ``db.transaction()`` (``BEGIN IMMEDIATE``) scope unless
  the caller passes its own connection, in which case the CALLER owns
  the commit/rollback boundary.  Nothing is written before validation
  succeeds, so a failed update always leaves the prior rate intact.

Staleness policy (ONE explicit, deterministic policy)
-----------------------------------------------------
- missing row            -> ``RateUnavailableError`` (no quote exists)
- age >= TTL             -> ``RateStaleError`` (a STALE quote is never
                            silently returned; it is a subclass of
                            ``RateUnavailableError``)
- age <  TTL             -> fresh ``RateQuote``
- captured_at in the
  future                -> ``InvalidPersistedRateError`` (invalid
                            persisted data, never "fresh forever")
- naive / unparsable /
    unapproved provider
    persisted row        -> ``InvalidPersistedRateError`` (wraps the
                            exact rate_quote failure as ``__cause__``)

The boundary is PINNED: freshness is **strictly** ``age < ttl`` — a
rate captured exactly ``ttl_seconds`` old is stale.  Default TTL is
``RATE_TTL_SECONDS = 900`` (15 minutes), configurable per call via
``ttl_seconds=`` — the repository's established TTL pattern
(``youtube_oauth.STATE_TTL_SECONDS`` module constant + optional
parameter).

``get_current_quote`` returns a fresh frozen ``RateQuote`` built from
the persisted row (exact Decimal, ``"manual"``, the PERSISTED
``captured_at`` — never replaced with "now") and never a mutable
database record.

Run:
    python3 -m pytest test_rate_store.py -v
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import db
from config import is_admin
from rate_quote import (
    APPROVED_RATE_PROVIDERS,
    PROVIDER_MANUAL,
    RateQuote,
    RateQuoteError,
    RateValidationError,
    canonical_rate_text,
    canonical_timestamp_text,
    parse_rate,
    validate_rate_provider,
)

logger = logging.getLogger(__name__)

# ── Staleness configuration (youtube_oauth.STATE_TTL_SECONDS pattern) ──

RATE_TTL_SECONDS = 900  # 15 minutes — safe production default

# Automatic providers are every APPROVED provider MINUS `manual`: the
# manual quote exists only for the admin emergency path and is never
# written by the automatic worker.
_AUTOMATIC_PROVIDERS = APPROVED_RATE_PROVIDERS - {PROVIDER_MANUAL}

TABLE = "current_rate"
SINGLETON_ID = 1
_COLUMNS = "rate_usdt_egp, provider, captured_at, updated_by, updated_at"


# ── Errors ────────────────────────────────────────────────────────────


class RateStoreError(Exception):
    """Base class for rate-store failures (authorization, arguments)."""


class RatePermissionError(RateStoreError):
    """Mutation attempted by a non-admin (config.is_admin reuse)."""


class RateUnavailableError(RateQuoteError):
    """No usable rate quote exists (missing or stale)."""


class RateStaleError(RateUnavailableError):
    """The persisted rate is older than the TTL — never used silently."""


class InvalidPersistedRateError(RateQuoteError):
    """The stored row is invalid data (future/naive timestamp, bad
    text or an unapproved provider) — rejected, never treated as fresh."""


class ManualRateBlockedError(RateStoreError):
    """``/setrate`` attempted while the AUTOMATIC quote is fresh.

    Manual updates are the EMERGENCY path only: they open once the
    automatic provider fails to keep a fresh quote (missing or aged
    past the TTL).  The store error carries English diagnostics; the
    command layer renders the operator-facing Arabic message.
    """


# ── Connection handling (caller-owned or repository-owned) ────────────


@contextmanager
def _connection(
    conn: sqlite3.Connection | None,
    *,
    db_path: str | None,
    write: bool = False,
):
    """Yield the caller's connection, or open one for this operation.

    ``conn`` given → the caller owns the transaction boundary (the
    caller's ``db.transaction()`` commits/rolls back; this helper
    never commits, rolls back or closes the borrowed connection).
    Otherwise reads use ``db.get_connection()`` and writes use
    ``db.transaction()`` (``BEGIN IMMEDIATE``) — the repository's
    existing conventions (same shape as ``platform_settings``).
    """
    if conn is not None:
        yield conn
        return
    if write:
        with db.transaction(db_path) as owned:
            yield owned
    else:
        with db.get_connection(db_path) as owned:
            yield owned


# ── Input/argument validation ─────────────────────────────────────────


def _require_admin(user_id: object) -> int:
    """Authorization reuse: ``config.is_admin`` is the ONE model.

    The store deliberately implements no authorization logic of its
    own — only the existing admin check plus the type guard every
    caller in this repository applies to actor ids.
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise RatePermissionError("⛔ هذا الإجراء للمشرفين فقط.")
    if not is_admin(user_id):
        raise RatePermissionError("⛔ هذا الإجراء للمشرفين فقط.")
    return user_id


def _server_now(now: object) -> datetime:
    """Server-generated aware UTC instant (injectable for tests)."""
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise RateStoreError("now: must be a timezone-aware datetime")
    if now.tzinfo is None or now.utcoffset() is None:
        raise RateStoreError("now: must be a timezone-aware datetime")
    return now


def _require_ttl(ttl_seconds: object) -> int:
    """Configurable TTL: a non-negative int of seconds (bool rejected)."""
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise RateStoreError("ttl_seconds: must be a non-negative int")
    if ttl_seconds < 0:
        raise RateStoreError("ttl_seconds: must be a non-negative int")
    return ttl_seconds


# ── Mutation (admin-only, atomic) ─────────────────────────────────────


def set_rate(
    rate_value: object,
    *,
    admin_user_id: object,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    now: datetime | None = None,
) -> RateQuote:
    """Validate and atomically replace the current rate — ADMIN-ONLY.

    Args:
        rate_value: exact input for ``rate_quote.parse_rate`` — text
            (``"48.5"``) or ``Decimal``.  ``float``/``bool`` are
            rejected by the existing contract; there is no code path
            that could accept a float as the authoritative value.
        admin_user_id: acting admin, authorized through
            ``config.is_admin``; stored as ``updated_by``.
        connection: optional caller-owned connection — the caller's
            transaction owns commit/rollback (no nested transaction is
            ever opened).
        db_path: database path when no connection is supplied.
        now: server-side capture instant (defaults to aware UTC now;
            injectable only so tests stay deterministic).

    Returns:
        The freshly built immutable ``RateQuote`` that was persisted.

    Raises:
        RatePermissionError: non-admin actor (before anything is read
            or written).
        RateValidationError: malformed/zero/negative/exponent/float
            input (nothing is written — the prior rate intact).
        ManualRateBlockedError: the AUTOMATIC quote is still fresh —
            manual updates are emergency-only (nothing is written).

    The provider is NOT a parameter: it is always ``"manual"``, so no
    caller (Telegram included) can ever select another source.  Both
    ``captured_at`` and ``updated_at`` are generated server-side as
    aware UTC ISO-8601 text; they are never accepted from a caller.
    """
    admin = _require_admin(admin_user_id)      # authorize BEFORE parsing
    rate = parse_rate(rate_value)              # RateValidationError → nothing written
    rate_text = canonical_rate_text(rate)      # canonical plain-decimal TEXT
    captured_at = _server_now(now)
    captured_text = canonical_timestamp_text(captured_at)

    with _connection(connection, db_path=db_path, write=True) as active:
        # ── Emergency gate ────────────────────────────────────────
        # The manual path is CLOSED while the AUTOMATIC quote is
        # positively fresh; it opens only when the provider failed to
        # keep one (missing / stale / unparsable row).  Admin
        # authorization already happened above.  The raise rolls
        # back with zero writes (the transaction owns the outcome).
        # The evaluated context doubles as the audit REASON logged
        # for every accepted manual override.
        row = active.execute(
            "SELECT provider, captured_at FROM current_rate WHERE id = ?",
            (SINGLETON_ID,),
        ).fetchone()
        manual_context = "no automatic quote on record (bootstrap)"
        if row is not None and row["provider"] != PROVIDER_MANUAL:
            try:
                previous_at = datetime.fromisoformat(row["captured_at"])
            except (TypeError, ValueError):
                previous_at = None
            if previous_at is None or (
                previous_at.tzinfo is None or previous_at.utcoffset() is None
            ):
                manual_context = (
                    "emergency: automatic quote capture instant is "
                    "unparsable"
                )
            elif captured_at < previous_at:
                manual_context = (
                    "emergency: automatic quote capture instant is in "
                    "the future (invalid row)"
                )
            elif captured_at - previous_at >= timedelta(
                seconds=RATE_TTL_SECONDS
            ):
                age = int((captured_at - previous_at).total_seconds())
                manual_context = (
                    f"emergency: automatic quote stale "
                    f"(age {age}s >= TTL {RATE_TTL_SECONDS}s)"
                )
            else:
                age = int((captured_at - previous_at).total_seconds())
                raise ManualRateBlockedError(
                    "automatic provider quote is fresh "
                    f"(age {age}s < TTL {RATE_TTL_SECONDS}s) — "
                    "/setrate is emergency-only while the automatic "
                    "source is healthy"
                )
        elif row is not None:
            manual_context = "already in manual (emergency) mode"
        cursor = active.execute(
            "UPDATE current_rate "
            "SET rate_usdt_egp = ?, provider = ?, captured_at = ?, "
            "    updated_by = ?, updated_at = ? "
            "WHERE id = ?",
            (
                rate_text,
                PROVIDER_MANUAL,
                captured_text,
                admin,
                captured_text,
                SINGLETON_ID,
            ),
        )
        if cursor.rowcount == 0:
            active.execute(
                "INSERT INTO current_rate "
                "(id, rate_usdt_egp, provider, captured_at, "
                " updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    SINGLETON_ID,
                    rate_text,
                    PROVIDER_MANUAL,
                    captured_text,
                    admin,
                    captured_text,
                ),
            )

    # Audit line: canonical value + actor id + WHY the emergency path
    # was open — the rate and the admin id are no secret, and no
    # other financial data is ever logged.
    logger.info(
        "Manual rate set: rate=%s admin=%d reason=%s",
        rate_text, admin_user_id, manual_context,
    )
    return RateQuote(rate, PROVIDER_MANUAL, captured_at)


def set_automatic_rate(
    quote: RateQuote,
    *,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> RateQuote:
    """Persist a validated AUTOMATIC provider quote (the normal mode).

    Called by ``rate_worker`` only — no admin credential is required
    because the source is the trusted provider adapter; ``updated_by
    = 0`` marks the system as the actor in the existing schema and
    every write is logged (provider + capture instant).

    The provider must be an approved AUTOMATIC provider (currently
    ``coingecko`` or ``coinbase`` — see
    ``rate_quote.APPROVED_RATE_PROVIDERS`` minus ``manual``);
    ``captured_at`` comes from the quote itself and is persisted
    verbatim.  A successful call OVERWRITES any manual emergency quote
    — that is the return-to-normal path after the provider recovers.
    Freshness is NOT re-evaluated here: the ONE staleness policy stays
    in ``get_current_quote`` (a quote written here ages out exactly
    like any other).

    Raises:
        RateStoreError: not a ``RateQuote``, or a provider that is not
            an approved automatic one (nothing is written).
    """
    if not isinstance(quote, RateQuote):
        raise RateStoreError("quote must be a RateQuote")
    if quote.provider not in _AUTOMATIC_PROVIDERS:
        raise RateStoreError(
            "automatic rate requires an approved automatic provider, "
            f"got {quote.provider!r}"
        )
    rate_text = canonical_rate_text(quote.rate_usdt_egp)
    captured_text = canonical_timestamp_text(quote.captured_at)
    with _connection(connection, db_path=db_path, write=True) as active:
        cursor = active.execute(
            "UPDATE current_rate SET rate_usdt_egp = ?, provider = ?, "
            "captured_at = ?, updated_by = ?, updated_at = ? "
            "WHERE id = ?",
            (
                rate_text,
                quote.provider,
                captured_text,
                0,
                captured_text,
                SINGLETON_ID,
            ),
        )
        if cursor.rowcount == 0:
            active.execute(
                "INSERT INTO current_rate "
                "(id, rate_usdt_egp, provider, captured_at, "
                " updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    SINGLETON_ID,
                    rate_text,
                    quote.provider,
                    captured_text,
                    0,
                    captured_text,
                ),
            )
    logger.info(
        "Automatic rate stored: provider=%s captured_at=%s",
        quote.provider, captured_text,
    )
    return quote


# ── Read: the authoritative quote ─────────────────────────────────────


def get_current_quote(
    *,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    ttl_seconds: int = RATE_TTL_SECONDS,
    now: datetime | None = None,
) -> RateQuote:
    """Load, validate and return the current fresh ``RateQuote``.

    Args:
        connection: optional caller-owned connection — borrowed
            exactly as-is for the read (never committed/rolled back/
            closed, no hidden second connection), so MT-ADMIN-25 can
            resolve the quote inside its own withdrawal transaction.
        db_path: database path when no connection is supplied.
        ttl_seconds: staleness budget (default ``RATE_TTL_SECONDS`` =
            900 s / 15 min).  Fresh means ``age < ttl_seconds``
            strictly — at exactly the boundary the quote is STALE.
        now: reference instant (defaults to aware UTC now).

    Returns:
        A fresh frozen ``RateQuote``: exact ``Decimal`` rate,
        ``"manual"``, and the PERSISTED ``captured_at`` (never
        replaced with "now").

    Raises:
        RateUnavailableError: no rate has ever been set.
        RateStaleError: the row is older than the TTL — a stale quote
            is never returned silently.
        InvalidPersistedRateError: the stored row itself is invalid
            data — future, naive or unparsable ``captured_at``,
            malformed rate text, or an unapproved provider.
        RateStoreError: bad ``ttl_seconds``/``now`` arguments.
    """
    ttl = _require_ttl(ttl_seconds)
    reference = _server_now(now)

    with _connection(connection, db_path=db_path) as active:
        row = active.execute(
            "SELECT rate_usdt_egp, provider, captured_at "
            "FROM current_rate WHERE id = ?",
            (SINGLETON_ID,),
        ).fetchone()

    if row is None:
        raise RateUnavailableError(
            "لا يوجد سعر صرف محفوظ — يجب ضبط السعر أولًا "
            "(أوامر المشرف: /setrate)."
        )

    # Everything below validates PERSISTED data: a bad row is corrupt
    # state, never a usable (or "fresh") quote.
    try:
        rate = parse_rate(row["rate_usdt_egp"])
        provider = validate_rate_provider(row["provider"])
        captured_raw = row["captured_at"]
        if not isinstance(captured_raw, str):
            raise RateValidationError(
                f"captured_at must be ISO text, got {type(captured_raw).__name__}"
            )
        captured_at = datetime.fromisoformat(captured_raw)
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise RateValidationError(
                "captured_at must be timezone-aware"
            )
    except RateQuoteError as exc:
        raise InvalidPersistedRateError(
            f"persisted rate row is invalid data: {exc}"
        ) from exc
    except ValueError as exc:
        raise InvalidPersistedRateError(
            f"persisted captured_at is unparsable: {captured_raw!r}"
        ) from exc

    if captured_at > reference:
        raise InvalidPersistedRateError(
            "persisted captured_at is in the future — invalid data, "
            "never treated as fresh"
        )

    age = reference - captured_at
    if age >= timedelta(seconds=ttl):
        raise RateStaleError(
            f"rate captured at {captured_at.isoformat()} is "
            f"{int(age.total_seconds())}s old (TTL {ttl}s) — stale, "
            "no quote available"
        )

    # Fresh: a brand-new immutable quote built from persisted facts.
    return RateQuote(rate, provider, captured_at)
