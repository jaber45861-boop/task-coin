"""
Deposit Store (MT-ADMIN-28)
===========================

The persisted USER DEPOSIT INTENT model — the smallest foundation for
the future verification/crediting pipeline.

What a deposit request IS: a user's instruction/intent record saying
"I am sending funds to this configured platform deposit destination
via this method, for this amount".  What it is NOT: proof of payment.
Creating a row:

- NEVER touches ``wallets`` or ``ledger`` — no credit, no hold, no
  ledger entry of any kind.  The deposit endpoint has no authoritative
  verification source in this repository, so every request stays
  ``pending`` (unverified) by construction.  Only a FUTURE deposit
  verifier — the authority for received amount, transaction identity,
  confirmations and the final internal USDT credit — may move a row to
  ``credited`` or ``rejected``.
- NEVER fabricates an external transaction id: ``external_tx_id`` is
  NULL at creation (there is no blockchain interaction here), with a
  partial UNIQUE index so one verified external transaction can never
  be credited twice (idempotency-ready).
- NEVER invents a rate or asset conversion: the amount is stored as
  exact integer USDT atomic units exactly as requested; crediting a
  non-USDT asset later requires an explicit configured mapping — none
  exists yet and none is invented here.

Architecture notes:

- ``payment_methods.deposits_enabled`` (MT-ADMIN-28 additive column)
  is the EXPLICIT deposit-availability flag.  ``category`` (crypto /
  cash) is deliberately NOT overloaded, and an active method alone
  never implies deposits — the admin must opt a method in.
- ``payment_methods.destination`` is the PLATFORM deposit destination
  the user sends funds TO; it is snapshotted into ``pm_destination``
  so later admin edits/deactivation never rewrite historical
  instructions.  The withdrawal-side ``user_destination`` concept does
  not exist for deposits and is never asked for or stored.
- Amount validation is exact integer math only: ``float``/``bool`` are
  rejected, precision is bounded (8 dp), zero/negative are rejected,
  and values beyond the signed SQLite INTEGER bound are rejected.  The
  authoritative ``minimum_deposit_units`` platform setting is enforced
  with exact integer units; a missing setting follows the existing
  platform-settings contract (``SettingNotFoundError`` — no invented
  fallback).

Conventions: the single INSERT runs inside one ``db.transaction()``
(BEGIN IMMEDIATE); amount parsing happens before it (pure); the method
resolution + minimum read borrow that SAME connection, so there is no
hidden second transaction.  Logs carry ids/operation/actor only —
never the destination.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import db
import payment_method_store
import platform_settings
import wallet
from payment_method_store import PaymentMethodValidationError

logger = logging.getLogger(__name__)

# ── Status lifecycle (smallest explicit set for this phase) ──────────

STATUS_PENDING = "pending"        # unverified — the ONLY user-writable state
STATUS_CREDITED = "credited"      # future authoritative verification only
STATUS_REJECTED = "rejected"      # future authoritative verification only
STATUSES = (STATUS_PENDING, STATUS_CREDITED, STATUS_REJECTED)

# Signed SQLite INTEGER bound (amount_units must fit; never REAL).
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807


# ── Errors (Arabic-facing messages, mapped at the HTTP edge) ─────────


class DepositError(Exception):
    """Base class for deposit-request failures."""


class DepositValidationError(DepositError):
    """Untrusted input failed exact validation (amount shapes)."""


class DepositMethodUnavailableError(DepositError):
    """The method exists and is active but was never opted in as a
    user deposit method (``deposits_enabled = 0``)."""


class DepositBelowMinimumError(DepositError):
    """Requested units are below the configured ``minimum_deposit_units``.

    Carries the exact integer facts — never a float, never a fallback.
    """

    def __init__(self, amount_units: int, minimum_units: int) -> None:
        super().__init__(
            f"amount {amount_units} is below the configured minimum "
            f"{minimum_units} atomic USDT units"
        )
        self.amount_units = amount_units
        self.minimum_units = minimum_units


# ── Row snapshot ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class DepositRequest:
    """Immutable snapshot of one persisted deposit intent.

    ``pm_destination`` (the platform's deposit destination) is excluded
    from ``repr`` — same discipline as ``PaymentMethod``.
    """

    request_id: str
    user_id: int
    payment_method_id: int
    amount_units: int
    status: str
    pm_display_name: str
    pm_asset: str
    pm_network: str | None
    pm_provider: str
    pm_destination: str
    external_tx_id: str | None
    created_at: str
    updated_at: str


_COLUMNS = (
    "request_id, user_id, payment_method_id, amount_units, status, "
    "pm_display_name, pm_asset, pm_network, pm_provider, pm_destination, "
    "external_tx_id, created_at, updated_at"
)


def _row_to_request(row) -> DepositRequest:
    return DepositRequest(
        request_id=row["request_id"],
        user_id=row["user_id"],
        payment_method_id=row["payment_method_id"],
        amount_units=int(row["amount_units"]),
        status=row["status"],
        pm_display_name=row["pm_display_name"],
        pm_asset=row["pm_asset"],
        pm_network=row["pm_network"],
        pm_provider=row["pm_provider"],
        pm_destination=row["pm_destination"],
        external_tx_id=row["external_tx_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Exact amount parsing (integer-only; no float anywhere) ───────────


def parse_amount_units(value: object, *, field: str = "amount") -> int:
    """Raw client amount (str/int/Decimal) → exact positive USDT units.

    Rejects (deterministically, ``DepositValidationError``):
    ``float``/``bool`` (float math is forbidden), malformed or
    non-finite text, more than 8 decimal places, zero, negative, and
    anything beyond the signed SQLite INTEGER bound.  Conversion itself
    goes through the existing exact ``wallet.decimal_to_units`` — this
    module performs no division, no rounding and no float arithmetic.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise DepositValidationError(
            f"{field}: float/bool are forbidden in financial math; "
            "pass Decimal, int or str"
        )
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise DepositValidationError(f"{field}: not a valid decimal")
        try:
            dec = Decimal(text)
        except InvalidOperation as exc:
            raise DepositValidationError(
                f"{field}: not a valid decimal: {value!r}"
            ) from exc
    else:
        raise DepositValidationError(
            f"{field}: unsupported type {type(value).__name__}; "
            "pass Decimal, int or str"
        )
    if not dec.is_finite():
        raise DepositValidationError(
            f"{field}: must be a finite decimal, got {value!r}"
        )

    try:
        units = wallet.decimal_to_units(dec, field=field)
    except wallet.InvalidWalletAmountError as exc:
        # Negative / over-precision — same rejection family.
        raise DepositValidationError(str(exc)) from exc

    if units <= 0:
        raise DepositValidationError(f"{field}: must be positive")
    if units > _SQLITE_INT64_MAX:
        raise DepositValidationError(
            f"{field}: exceeds the supported maximum"
        )
    return units


def _timestamp_text(value: datetime) -> str:
    """UTC wall-clock TEXT (CURRENT_TIMESTAMP shape) — exact, no tz."""
    if value.tzinfo is not None and value.utcoffset() is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Create (ONE transaction; money is never moved here) ──────────────


def create_deposit_request(
    *,
    user_id: object,
    payment_method_id: object,
    amount: object,
    now: datetime | None = None,
    db_path: str | None = None,
) -> DepositRequest:
    """Persist one PENDING deposit intent.

    Inside ONE ``db.transaction()``: resolve the method as ACTIVE on
    that exact connection → require the explicit deposit opt-in →
    enforce the configured minimum in exact integer units → INSERT the
    snapshot row.  No wallet call, no ledger call, no rate read, no
    external transaction id — ever.

    Raises:
        PaymentMethodNotFoundError / PaymentMethodInactiveError /
        PaymentMethodValidationError: the id is not an ACTIVE method.
        DepositMethodUnavailableError: active, but not a deposit method.
        DepositValidationError: malformed/zero/negative/overflow amount.
        DepositBelowMinimumError: below ``minimum_deposit_units``.
        platform_settings.SettingNotFoundError: the minimum setting was
            never configured (existing contract — no fallback invented).
        db-level errors propagate unchanged (nothing is partially
            written: the transaction rolls back).
    """
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
    ):
        raise DepositValidationError("user_id: معرف موجب مطلوب")

    # Pure exact parsing — BEFORE any transaction.
    amount_units = parse_amount_units(amount)

    at = _utc_now() if now is None else now
    at_text = _timestamp_text(at)
    request_id = uuid.uuid4().hex

    with db.transaction(db_path) as conn:
        # Trusted stored row decides everything about the method —
        # the client can never choose asset/network/provider/destination.
        pm = payment_method_store.get_active_payment_method(
            payment_method_id, connection=conn
        )
        if not pm.deposits_enabled:
            raise DepositMethodUnavailableError(
                f"وسيلة الدفع #{pm.id} غير متاحة للإيداع"
            )

        # Authoritative minimum — exact integer units, existing
        # platform-settings contract (missing setting raises; it is
        # never defaulted or guessed).
        minimum_units = platform_settings.get_required_setting(
            platform_settings.MINIMUM_DEPOSIT_UNITS, conn=conn
        )
        if amount_units < minimum_units:
            raise DepositBelowMinimumError(amount_units, minimum_units)

        conn.execute(
            "INSERT INTO deposit_requests "
            "(request_id, user_id, payment_method_id, amount_units, "
            " status, pm_display_name, pm_asset, pm_network, pm_provider, "
            " pm_destination, external_tx_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                request_id,
                user_id,
                pm.id,
                amount_units,
                STATUS_PENDING,
                pm.display_name,
                pm.asset,
                pm.network,
                pm.provider,
                pm.destination,
                at_text,
                at_text,
            ),
        )
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM deposit_requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()

    created = _row_to_request(row)
    # Audit line: ids/operation/actor only — NEVER the destination.
    logger.info(
        "Deposit request created: request=%s user=%s method=%d "
        "status=%s",
        created.request_id, created.user_id, created.payment_method_id,
        created.status,
    )
    return created


# ── Read ─────────────────────────────────────────────────────────────


def get_deposit_request(
    request_id: object,
    db_path: str | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> DepositRequest | None:
    """Fetch one deposit intent by id; None for invalid/missing ids.

    Connection ownership (MT-ADMIN-29): with ``connection`` the lookup
    runs on that exact caller-owned connection — borrowed, never
    committed, rolled back or closed here — so an atomic workflow can
    re-read its row inside its own transaction without opening a hidden
    second connection.
    """
    if not isinstance(request_id, str) or not request_id:
        return None
    if connection is not None:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM deposit_requests "
            "WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    else:
        with db.get_connection(db_path) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM deposit_requests "
                "WHERE request_id = ?",
                (request_id,),
            ).fetchone()
    return _row_to_request(row) if row else None


def find_request_by_external_tx_id(
    external_tx_id: str, *, connection: sqlite3.Connection
) -> str | None:
    """Request id that already owns ``external_tx_id``, or None.

    Read-only, on the caller's transaction connection (MT-ADMIN-29):
    one external transaction may credit AT MOST one deposit request —
    backed by the partial ``ux_deposit_requests_tx`` UNIQUE index.
    """
    row = connection.execute(
        "SELECT request_id FROM deposit_requests "
        "WHERE external_tx_id = ?",
        (external_tx_id,),
    ).fetchone()
    return row[0] if row else None


def mark_credited(
    request_id: str,
    external_tx_id: str,
    updated_at: str,
    *,
    connection: sqlite3.Connection,
) -> bool:
    """CAS ``pending → credited`` + persist the verified tx id.

    The ONLY write that ever sets ``external_tx_id`` / ``credited``:
    guarded by ``status = 'pending'`` AND ``external_tx_id IS NULL``
    so a stale or repeated transition affects zero rows and the caller
    must treat that as a state conflict (no partial write happens —
    the surrounding transaction owns the outcome).

    Returns True only when exactly one row transitioned.
    """
    cursor = connection.execute(
        "UPDATE deposit_requests "
        "SET status = ?, external_tx_id = ?, updated_at = ? "
        "WHERE request_id = ? AND status = ? "
        "AND external_tx_id IS NULL",
        (
            STATUS_CREDITED,
            external_tx_id,
            updated_at,
            request_id,
            STATUS_PENDING,
        ),
    )
    return cursor.rowcount == 1
