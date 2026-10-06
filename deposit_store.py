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
  exact integer ATOMIC UNITS OF ``pm_asset`` at that asset's
  registered scale (the ``asset_units`` registry — USDT 8 dp, EGP
  2 dp); no exchange rate is ever read and crediting a non-USDT
  asset later requires an explicit configured mapping — none exists
  yet and none is invented here.

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
  rejected, precision is bounded by the ASSET's registered scale (8 dp
  for USDT, 2 dp for EGP), zero/negative are rejected, and values
  beyond the signed SQLite INTEGER bound are rejected.  The
  authoritative minimum is the PER-METHOD
  ``payment_methods.min_deposit_units`` — exact integer atomic units
  of the same asset's scale; ``NULL`` (not configured) or an asset
  with no registered scale raises ``DepositConfigurationError``
  (fail-closed — no invented default, no fallback scale).

Conventions: the single INSERT runs inside one ``db.transaction()``
(BEGIN IMMEDIATE); the method resolution, scale/minimum read and the
amount parse (pure) all borrow that SAME connection, so there is no
hidden second transaction.  Logs carry ids/operation/actor only —
never the destination.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import asset_units
import db
import payment_method_store
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


class DepositConfigurationError(DepositError):
    """Required per-method deposit configuration is missing.

    Raised when ``min_deposit_units`` was never configured (NULL) or
    the method's asset has no registered decimal scale — both are
    fail-closed ``settings incomplete`` states: no default minimum
    and no fallback scale are ever invented.  Mapped by the HTTP
    layer to the stable ``deposit_settings_missing`` response.
    """


class DepositBelowMinimumError(DepositError):
    """Requested units are below the method's configured minimum.

    Carries the exact integer facts — never a float, never a
    fallback.  Both values are atomic units of the SAME method
    asset, so a cross-currency comparison is impossible by
    construction.
    """

    def __init__(self, amount_units: int, minimum_units: int) -> None:
        super().__init__(
            f"amount {amount_units} is below the configured minimum "
            f"{minimum_units} atomic units"
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


def parse_amount_units(
    value: object, asset: object, *, field: str = "amount"
) -> int:
    """Raw client amount (str/int/Decimal) → exact positive atomic
    units of *asset* at that asset's registered scale.

    The scale comes ONLY from the ``asset_units`` registry (USDT 8 dp,
    EGP 2 dp) — this function never assumes a global scale and
    performs no division, no rounding and no float arithmetic.

    Rejects (deterministically, ``DepositValidationError``):
    ``float``/``bool`` (float math is forbidden), malformed text, more
    fractional digits than the asset's scale (rejected — never
    rounded), zero, negative, and anything beyond the signed SQLite
    INTEGER bound.

    Raises:
        DepositValidationError: the value is not an exact in-range
            amount for this asset.
        UnknownAssetScaleError: the asset has no registered scale —
            fail-closed; the caller maps it to incomplete deposit
            settings (no fallback scale is invented).
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise DepositValidationError(
            f"{field}: float/bool are forbidden in financial math; "
            "pass Decimal, int or str"
        )
    try:
        return asset_units.parse_units(value, asset, field=field)
    except asset_units.UnknownAssetScaleError:
        raise
    except asset_units.AssetAmountError as exc:
        raise DepositValidationError(str(exc)) from exc


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
    resolve the asset's registered scale (fail-closed) → require the
    configured PER-METHOD minimum (fail-closed) → parse the amount at
    that SAME asset's scale → compare in the same units → INSERT the
    snapshot row.  No wallet call, no ledger call, no rate read, no
    external transaction id — ever.

    Raises:
        PaymentMethodNotFoundError / PaymentMethodInactiveError /
        PaymentMethodValidationError: the id is not an ACTIVE method.
        DepositMethodUnavailableError: active, but not a deposit method.
        DepositConfigurationError: ``min_deposit_units`` was never
            configured (NULL) for the method — fail-closed, no
            invented default (mapped by the HTTP layer to
            ``deposit_settings_missing``).
        asset_units.UnknownAssetScaleError: the method's asset has no
            registered decimal scale — fail-closed, no fallback
            scale (mapped the same way).
        DepositValidationError: malformed/zero/negative/over-precision
            amount for the asset's scale.
        DepositBelowMinimumError: below the method's configured
            minimum (both sides in the SAME asset's atomic units).
        db-level errors propagate unchanged (nothing is partially
            written: the transaction rolls back).
    """
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
    ):
        raise DepositValidationError("user_id: معرف موجب مطلوب")

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

        # 1) Asset scale — the ONE registry; unknown asset fails
        #    closed (no default scale is ever invented).
        try:
            asset_units.decimals_for(pm.asset)
        except asset_units.UnknownAssetScaleError as exc:
            raise DepositConfigurationError(
                f"no registered decimal scale for method #{pm.id} "
                "deposit asset"
            ) from exc

        # 2) Per-method minimum — NULL = not configured = fail
        #    closed (no global fallback, no invented default).
        if pm.min_deposit_units is None:
            raise DepositConfigurationError(
                f"min_deposit_units not configured for method #{pm.id}"
            )

        # 3) Exact parse at the method's OWN asset scale (EGP cents vs
        #    USDT atomic units — never one global scale).
        amount_units = parse_amount_units(amount, pm.asset)

        # 4) Compare in the SAME asset's atomic units — a
        #    cross-currency comparison is impossible by construction.
        if amount_units < pm.min_deposit_units:
            raise DepositBelowMinimumError(
                amount_units, pm.min_deposit_units
            )

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
