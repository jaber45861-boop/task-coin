"""
SQLite Withdrawal Repository + Financial Adapters (MT-ADMIN-22)
===============================================================

Production persistence boundary for the future atomic withdrawal
service.  ADAPTER/PERSISTENCE ONLY — this module implements NO
business workflow: no create/reject/complete orchestration, no rate
fetching, no minimum/fee settings, no routing, no UI, no notifications,
no currency conversion, no wallet/ledger money movement from the
repository itself.

Target architecture (owned by the FUTURE service, one
``db.transaction()`` per flow)::

    create: validate -> resolve rate -> resolve payment method
            -> wallet reserve -> ledger hold -> repository insert
    reject: repository CAS (pending -> rejected)
            -> wallet release -> ledger release
    complete: repository CAS (pending -> completed)
            -> wallet settle -> ledger settlement

This module supplies the capable pieces; the orchestration deliberately
does not exist yet.

SqliteWithdrawalRepository (Parts A-D)
--------------------------------------
- ``insert``     — persists a fully formed ``WithdrawalRequest`` with
                   EXACT values (no rates/fees/debits/minimums are
                   computed here; unit-notation conversion between the
                   model's EGP major Decimals and the schema's integer
                   minor units is exact and never rounds).
- ``get``        — row -> ``WithdrawalRequest`` (missing -> domain
                   ``RequestNotFoundError``).
- ``transition`` — guarded CAS: only ``pending -> rejected`` and
                   ``pending -> completed``, stamping the matching
                   transition timestamp; rowcount 0 distinguishes
                   not-found (``RequestNotFoundError``) from invalid
                   state (``InvalidStateError``).  NEVER touches
                   wallet or ledger.
- ``list_pending`` / ``latest_for`` — read queries for the future
                   service (cooldown semantics: ``latest_for`` counts
                   every status, exactly like the rules module).
- Every method takes a keyword-only ``connection=``: supplied → that
                   exact connection is used, borrowed — never
                   committed, rolled back, closed, or joined with a
                   second connection; omitted → the standalone
                   ``db.get_connection()`` compatibility scope (the
                   repository convention).  No second transaction
                   abstraction exists here.

Legacy rows (Part I)
--------------------
``wallet_debit_units`` / ``user_destination`` may be NULL: they read
back as ``None`` — never zero, never synthesized, never "repaired".
Rate facts that the schema declares NOT NULL are validated on insert
(bad/missing values raise a domain error instead of persisting);
nothing is invented.

Money units
-----------
- ``wallet_debit_units``: exact INTEGER USDT atomic units — stored
  verbatim, never calculated here.
- ``amount_egp_minor`` / ``fee_egp_minor``: INTEGER EGP minor (1 EGP
  = 100).
- ``amount_native_minor`` / ``fee_native_minor``: INTEGER in the
  row's ``native_unit`` scale (EGP minor, or USDT atomic units —
  exact reuse of ``wallet.decimal_to_units`` / ``units_to_decimal``).
- No EGP <-> USDT conversion happens anywhere in this module
  (Part E/F adapters included); float never touches money (AST-
  scanned by the tests).

Adapters (Parts E-G)
--------------------
- ``SqliteWalletAdapter`` — the smallest wallet port: forwards USDT
  atomic units to ``wallet.reserve`` / ``release_units`` /
  ``settle_units`` on the caller's connection.  ``reserve`` performs
  ONE lossless same-currency notation conversion (units -> Decimal)
  because ``wallet.reserve`` takes a USDT major amount while
  release/settle take units; wallet exception types pass through
  untranslated; nothing commits independently.
- ``SqliteLedgerAdapter`` — records hold/release/settlement through
  the EXISTING ``LedgerService`` (no INSERT logic is duplicated) with
  ``reference_type='withdrawal'``, ``reference_id=request_id`` and the
  canonical idempotency keys ``withdrawal:<id>:<entry_type>`` on the
  caller's connection.
- ``resolve_active_payment_method`` — reuses the existing
  ``payment_method_store.get_active_payment_method`` behind the
  MT-ADMIN-21 translation boundary (inactive/missing -> domain
  ``PaymentMethodUnavailableError``); no selection, no logging of
  destinations.

Errors (Part H)
---------------
All failures flow through ``withdrawal_contract.translated_errors``:
known lower-level errors become withdrawal-domain errors, domain
errors pass through, unexpected exceptions propagate unchanged.  The
repository adds ONE deterministic rule on top: an INSERT primary-key
violation maps to the new domain ``DuplicateRequestError`` (the
MT-ADMIN-21 boundary deliberately pins other UNIQUE messages as
pass-through, so this check lives at the repository that owns the
table).

Run:
    python -m pytest test_withdrawal_store.py -v
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Protocol

import db
import payment_method_store
import rate_quote
import wallet
import withdrawal_rules
from ledger import LedgerService, LedgerEntry
from withdrawal_contract import translated_errors
from withdrawal_rules import RequestStatus, WithdrawalRequest

TABLE = "withdrawal_requests"
_DUP_REQUEST_MSG = f"UNIQUE constraint failed: {TABLE}.request_id"
_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"   # SQLite CURRENT_TIMESTAMP shape
_MINOR_PRECISION = 60
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807

_INSERT_COLUMNS = (
    "request_id", "user_id", "method",
    "amount_egp_minor", "fee_egp_minor",
    "amount_native_minor", "fee_native_minor", "native_unit",
    "rate_usdt_egp", "wallet_rate_usdt_egp", "rate_captured_at",
    "rate_provider", "status", "created_at",
    "payment_method_id", "pm_display_name", "pm_category", "pm_asset",
    "pm_network", "pm_provider", "pm_destination",
    "wallet_debit_units", "user_destination", "rejected_at",
    "completed_at",
)
_INSERT_SQL = (
    f"INSERT INTO withdrawal_requests ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _INSERT_COLUMNS)})"
)

_NATIVE_UNITS = ("EGP", "USDT")
_ALLOWED_TRANSITIONS = ("rejected", "completed")


# ── Exact notation helpers (never a currency conversion) ──────────────


def _timestamp_text(value: object, *, field: str) -> str:
    """datetime/str -> SQLite TIMESTAMP text; explicit values are kept
    byte-for-byte (never regenerated), aware datetimes are rendered as
    UTC wall-clock (the CURRENT_TIMESTAMP convention)."""
    if isinstance(value, str):
        if value == "":
            raise withdrawal_rules.ValidationError(f"{field}: must not be empty")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime(_TIMESTAMP_FMT)
    raise withdrawal_rules.ValidationError(
        f"{field}: must be datetime or str, got {type(value).__name__}"
    )


def _parse_timestamp(value: object, *, field: str) -> datetime | None:
    """SQLite TIMESTAMP text -> naive UTC datetime (repository-written
    rows always use the standard shape; anything else is rejected
    rather than guessed at)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(value, _TIMESTAMP_FMT)
    except (TypeError, ValueError) as exc:
        raise withdrawal_rules.ValidationError(
            f"{field}: not a repository timestamp: {value!r}"
        ) from exc


def _egp_major_to_minor(
    value: object, *, field: str, allow_zero: bool
) -> int:
    """EGP major Decimal -> exact INTEGER EGP minor units.

    Exact-only: a value not representable in minor units raises — it is
    NEVER rounded (no silent money loss).
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise withdrawal_rules.ValidationError(
            f"{field}: float/bool are forbidden in financial math"
        )
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    else:
        raise withdrawal_rules.ValidationError(
            f"{field}: must be a Decimal, got {type(value).__name__}"
        )
    with localcontext() as ctx:
        ctx.prec = _MINOR_PRECISION
        scaled = dec * 100
    if scaled != scaled.to_integral_value():
        raise withdrawal_rules.InvalidAmountError(
            f"{field}: {value!r} is not representable in EGP minor "
            "units — never rounded"
        )
    minor = int(scaled)
    if allow_zero:
        if minor < 0:
            raise withdrawal_rules.InvalidAmountError(
                f"{field}: must not be negative, got {value!r}"
            )
    elif minor <= 0:
        raise withdrawal_rules.InvalidAmountError(
            f"{field}: must be positive, got {value!r}"
        )
    if minor > _SQLITE_INT64_MAX:
        raise withdrawal_rules.InvalidAmountError(
            f"{field}: exceeds the SQLite INTEGER range"
        )
    return minor


def _usdt_major_to_units(value: object, *, field: str, allow_zero: bool) -> int:
    """USDT major Decimal -> exact INTEGER atomic units via the existing
    ``wallet.decimal_to_units`` (float/bool/negative/over-precision are
    rejected there and translated by the boundary)."""
    units = wallet.decimal_to_units(value, field=field)
    if allow_zero:
        if units < 0:  # decimal_to_units already rejects; belt-and-braces
            raise withdrawal_rules.InvalidAmountError(
                f"{field}: must not be negative"
            )
    elif units <= 0:
        raise withdrawal_rules.InvalidAmountError(
            f"{field}: must be positive, got {value!r}"
        )
    return units


def _minor_to_egp_major(minor: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _MINOR_PRECISION
        return Decimal(minor) / 100


def _native_minor_to_major(minor: int, native_unit: str) -> Decimal:
    if native_unit == "EGP":
        return _minor_to_egp_major(minor)
    return wallet.units_to_decimal(minor)  # USDT atomic units, exact


# ── Repository ────────────────────────────────────────────────────────


class SqliteWithdrawalRepository:
    """Production SQLite persistence for ``withdrawal_requests``.

    Persistence only: exact mapping between the ``WithdrawalRequest``
    model and the table's 25 columns, guarded CAS status transitions,
    and connection-injected participation in a caller's transaction.
    No wallet/ledger movement, no rate/fee/debit/minimum calculation,
    no automatic anything.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path

    @contextmanager
    def _connection(self, connection: sqlite3.Connection | None):
        """Borrow the caller's connection, or open the standalone scope.

        A supplied connection is NEVER committed, rolled back, closed
        or replaced by a second connection — transaction ownership
        stays entirely with the caller (``db.transaction()``).
        """
        if connection is not None:
            yield connection
        else:
            with db.get_connection(self._db_path) as conn:
                yield conn

    # ── mapping: model -> row (exact persistence) ────────────────

    def _to_row(self, request: WithdrawalRequest) -> tuple:
        if not isinstance(request, WithdrawalRequest):
            raise withdrawal_rules.ValidationError(
                f"request must be a WithdrawalRequest, "
                f"got {type(request).__name__}"
            )
        # closed-set validations (deterministic domain errors instead
        # of raw CHECK-constraint IntegrityErrors)
        if request.method not in withdrawal_rules.SUPPORTED_METHODS:
            raise withdrawal_rules.InvalidMethodError(
                f"unsupported method {request.method!r}"
            )
        try:
            status = RequestStatus(request.status).value
        except ValueError as exc:
            raise withdrawal_rules.ValidationError(
                f"invalid status {request.status!r}"
            ) from exc
        native_unit = request.native_unit
        if native_unit is None:
            # deterministic derivation for models that omit it: the
            # method set is closed (vodafone -> EGP, usdt -> USDT).
            native_unit = (
                "USDT" if request.method == withdrawal_rules.METHOD_USDT_BEP20
                else "EGP"
            )
        if native_unit not in _NATIVE_UNITS:
            raise withdrawal_rules.ValidationError(
                f"invalid native_unit {native_unit!r}"
            )

        # rate facts: exact values, validated, never invented
        rate_text = (
            None if request.rate_usdt_egp is None
            else rate_quote.canonical_rate_text(request.rate_usdt_egp)
        )
        if request.wallet_rate_usdt_egp is None:
            raise withdrawal_rules.InvalidRateError(
                "wallet_rate_usdt_egp is required (never invented here)"
            )
        if not isinstance(request.wallet_rate_usdt_egp, str):
            raise withdrawal_rules.ValidationError(
                "wallet_rate_usdt_egp must be the stored TEXT rate"
            )
        rate_quote.parse_rate(request.wallet_rate_usdt_egp)  # validate only
        if request.rate_provider is None:
            raise withdrawal_rules.ValidationError(
                "rate_provider is required (never invented here)"
            )
        rate_quote.validate_rate_provider(request.rate_provider)

        # payment-method linkage (nullable; FK enforced by schema)
        payment_method_id = request.payment_method_id
        if payment_method_id is not None and (
            isinstance(payment_method_id, bool)
            or not isinstance(payment_method_id, int)
        ):
            raise withdrawal_rules.ValidationError(
                "payment_method_id must be an int or None"
            )

        # wallet debit: stored VERBATIM (exact int, or None for legacy)
        debit = request.wallet_debit_units
        if debit is not None:
            if isinstance(debit, bool) or not isinstance(debit, int):
                raise withdrawal_rules.ValidationError(
                    "wallet_debit_units must be an int of USDT atomic "
                    f"units or None, got {type(debit).__name__}"
                )
            if debit < 0 or debit > _SQLITE_INT64_MAX:
                raise withdrawal_rules.InvalidAmountError(
                    "wallet_debit_units out of range"
                )

        destination = request.user_destination
        if destination is not None and not isinstance(destination, str):
            raise withdrawal_rules.ValidationError(
                "user_destination must be a str or None"
            )

        if native_unit == "EGP":
            amount_native = _egp_major_to_minor(
                request.amount_native, field="amount_native", allow_zero=False
            )
            fee_native = _egp_major_to_minor(
                request.fee_native, field="fee_native", allow_zero=True
            )
        else:
            amount_native = _usdt_major_to_units(
                request.amount_native, field="amount_native", allow_zero=False
            )
            fee_native = _usdt_major_to_units(
                request.fee_native, field="fee_native", allow_zero=True
            )

        return (
            request.request_id,
            request.user_id,
            request.method,
            _egp_major_to_minor(
                request.amount_egp, field="amount_egp", allow_zero=False
            ),
            _egp_major_to_minor(
                request.fee_egp, field="fee_egp", allow_zero=True
            ),
            amount_native,
            fee_native,
            native_unit,
            rate_text,
            request.wallet_rate_usdt_egp,
            _rate_captured_text(request),
            request.rate_provider,
            status,
            _timestamp_text(request.created_at, field="created_at"),
            payment_method_id,
            request.pm_display_name,
            request.pm_category,
            request.pm_asset,
            request.pm_network,
            request.pm_provider,
            request.pm_destination,
            debit,
            destination,
            None if request.rejected_at is None
            else _timestamp_text(request.rejected_at, field="rejected_at"),
            None if request.completed_at is None
            else _timestamp_text(request.completed_at, field="completed_at"),
        )

    # ── mapping: row -> model (every persisted fact) ──────────────

    @staticmethod
    def _from_row(row: sqlite3.Row) -> WithdrawalRequest:
        native_unit = row["native_unit"]
        try:
            rate = (
                None if row["rate_usdt_egp"] is None
                else Decimal(row["rate_usdt_egp"])
            )
        except InvalidOperation as exc:
            raise withdrawal_rules.InvalidRateError(
                f"stored rate_usdt_egp is not a decimal: "
                f"{row['rate_usdt_egp']!r}"
            ) from exc
        return WithdrawalRequest(
            request_id=row["request_id"],
            user_id=row["user_id"],
            method=row["method"],
            amount_egp=_minor_to_egp_major(row["amount_egp_minor"]),
            fee_egp=_minor_to_egp_major(row["fee_egp_minor"]),
            rate_usdt_egp=rate,
            amount_native=_native_minor_to_major(
                row["amount_native_minor"], native_unit
            ),
            fee_native=_native_minor_to_major(
                row["fee_native_minor"], native_unit
            ),
            status=RequestStatus(row["status"]),
            created_at=_parse_timestamp(row["created_at"], field="created_at"),
            wallet_debit_units=row["wallet_debit_units"],   # None on legacy
            user_destination=row["user_destination"],       # None on legacy
            rejected_at=_parse_timestamp(row["rejected_at"],
                                         field="rejected_at"),
            completed_at=_parse_timestamp(row["completed_at"],
                                          field="completed_at"),
            native_unit=native_unit,
            payment_method_id=row["payment_method_id"],
            pm_display_name=row["pm_display_name"],
            pm_category=row["pm_category"],
            pm_asset=row["pm_asset"],
            pm_network=row["pm_network"],
            pm_provider=row["pm_provider"],
            pm_destination=row["pm_destination"],
            wallet_rate_usdt_egp=row["wallet_rate_usdt_egp"],
            rate_captured_at=row["rate_captured_at"],
            rate_provider=row["rate_provider"],
        )

    @staticmethod
    def _select(conn: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
        return conn.execute(
            f"SELECT * FROM {TABLE} WHERE request_id = ?", (request_id,)
        ).fetchone()

    # ── public API ────────────────────────────────────────────────

    def insert(
        self,
        request: WithdrawalRequest,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Persist a fully formed request — exact values, no math.

        Participation: with ``connection`` the INSERT runs inside the
        caller's open transaction (borrowed — never committed/rolled
        back/closed here); without it the standalone ``get_connection``
        scope commits per call.

        Raises:
            DuplicateRequestError: this request_id already exists.
            domain validation/rate errors: bad values rejected BEFORE
                anything is written (nothing is invented).
            RequestNotFoundError-level FK failure: unknown user (via
                boundary pass-through of the FK IntegrityError).
        """
        with self._connection(connection) as conn:
            try:
                with translated_errors():
                    values = self._to_row(request)
                    conn.execute(_INSERT_SQL, values)
            except sqlite3.IntegrityError as exc:
                if _DUP_REQUEST_MSG in str(exc):
                    raise withdrawal_rules.DuplicateRequestError(
                        f"withdrawal request {request.request_id!r} "
                        "already exists"
                    ) from exc
                raise

    def get(
        self,
        request_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> WithdrawalRequest:
        """Fetch one request; missing id -> domain ``RequestNotFoundError``."""
        with self._connection(connection) as conn:
            with translated_errors():
                row = self._select(conn, request_id)
                if row is None:
                    raise withdrawal_rules.RequestNotFoundError(
                        f"no withdrawal request {request_id!r}"
                    )
                return self._from_row(row)

    def transition(
        self,
        request_id: str,
        *,
        to_status: str,
        at: object,
        connection: sqlite3.Connection | None = None,
    ) -> WithdrawalRequest:
        """Guarded CAS status transition with its timestamp stamp.

        Only ``pending -> rejected`` and ``pending -> completed`` can
        succeed (the UPDATE itself carries ``AND status='pending'``):
        any second or invalid transition fails without touching the
        row.  The repository moves ONLY the status + timestamp columns —
        wallet and ledger movement belong to the future service in the
        SAME transaction.

        Args:
            request_id: target request.
            to_status: ``'rejected'`` or ``'completed'``.
            at: transition timestamp (datetime or exact TEXT) — stored
                exactly as supplied.
            connection: caller-owned connection (borrowed).

        Returns:
            The updated request as persisted.

        Raises:
            ValidationError: unknown target status.
            RequestNotFoundError: no such request.
            InvalidStateError: current status is not ``pending``.
        """
        if to_status not in _ALLOWED_TRANSITIONS:
            raise withdrawal_rules.ValidationError(
                f"invalid target status {to_status!r}; "
                f"allowed: {sorted(_ALLOWED_TRANSITIONS)}"
            )
        stamp = _timestamp_text(at, field="at")
        stamp_column = "rejected_at" if to_status == "rejected" else "completed_at"
        with self._connection(connection) as conn:
            with translated_errors():
                cursor = conn.execute(
                    f"UPDATE {TABLE} "
                    f"SET status = ?, {stamp_column} = ? "
                    f"WHERE request_id = ? AND status = 'pending'",
                    (to_status, stamp, request_id),
                )
                if cursor.rowcount == 0:
                    row = self._select(conn, request_id)
                    if row is None:
                        raise withdrawal_rules.RequestNotFoundError(
                            f"no withdrawal request {request_id!r}"
                        )
                    raise withdrawal_rules.InvalidStateError(
                        f"cannot move {row['status']!r} -> {to_status!r} "
                        f"for {request_id!r} (only 'pending' transitions)"
                    )
                row = self._select(conn, request_id)
                return self._from_row(row)

    def list_pending(
        self,
        *,
        user_id: int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[WithdrawalRequest]:
        """All pending requests (optionally one user's), newest first."""
        sql = f"SELECT * FROM {TABLE} WHERE status = 'pending'"
        params: tuple = ()
        if user_id is not None:
            sql += " AND user_id = ?"
            params = (user_id,)
        sql += " ORDER BY created_at DESC, request_id"
        with self._connection(connection) as conn:
            with translated_errors():
                rows = conn.execute(sql, params).fetchall()
            return [self._from_row(row) for row in rows]

    def latest_for(
        self,
        user_id: int,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> WithdrawalRequest | None:
        """Most recent request for a user (ANY status — the cooldown
        rule counts rejected requests too, exactly like
        ``withdrawal_rules``).  None when the user has none."""
        with self._connection(connection) as conn:
            with translated_errors():
                row = conn.execute(
                    f"SELECT * FROM {TABLE} WHERE user_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
        return None if row is None else self._from_row(row)

    def list_for_user(
        self,
        user_id: int,
        *,
        limit: int = 20,
        connection: sqlite3.Connection | None = None,
    ) -> list[WithdrawalRequest]:
        """One user's OWN requests, newest first, bounded.

        Read-only (no business decision, no mutation) — the Mini App
        status/list endpoint uses this to show the authenticated
        user's withdrawal history.  ``limit`` is a positive int; the
        transport validates the client-supplied value before calling.
        """
        with self._connection(connection) as conn:
            with translated_errors():
                rows = conn.execute(
                    f"SELECT * FROM {TABLE} WHERE user_id = ? "
                    "ORDER BY created_at DESC, request_id LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            return [self._from_row(row) for row in rows]


def _rate_captured_text(request: WithdrawalRequest) -> str:
    """Exact stored text for rate_captured_at; missing -> domain error
    (never invented)."""
    if request.rate_captured_at is None:
        raise withdrawal_rules.ValidationError(
            "rate_captured_at is required (never invented here)"
        )
    return _timestamp_text(request.rate_captured_at,
                           field="rate_captured_at")


# ── Wallet adapter (Part E) ──────────────────────────────────────────


class WalletPort(Protocol):
    """What the future withdrawal service needs from the wallet —
    USDT atomic units in, USDT atomic units out, on a caller-owned
    connection."""

    def reserve(
        self, user_id: int, amount_units: int, *,
        connection: sqlite3.Connection,
    ) -> int: ...

    def release_units(
        self, user_id: int, amount_units: int, *,
        connection: sqlite3.Connection,
    ) -> int: ...

    def settle_units(
        self, user_id: int, amount_units: int, *,
        connection: sqlite3.Connection,
    ) -> int: ...


class SqliteWalletAdapter:
    """Smallest wallet boundary for the withdrawal service.

    Forwards to the MT-ADMIN-18 connection-injected wallet functions;
    preserves wallet exception types (the FUTURE service translates
    them through the MT-ADMIN-21 boundary at its edge); never converts
    currency; never commits independently.

    NOTE on units: ``wallet.reserve`` takes a USDT MAJOR amount while
    ``release_units``/``settle_units`` take atomic units.  This adapter
    presents ONE uniform units-in/units-out surface and performs the
    single lossless same-currency notation conversion for reserve via
    ``wallet.units_to_decimal`` (exact, integer <-> Decimal, never a
    float, never a currency conversion).
    """

    def reserve(
        self, user_id: int, amount_units: int, *,
        connection: sqlite3.Connection,
    ) -> int:
        return wallet.reserve(
            user_id,
            wallet.units_to_decimal(amount_units),
            connection=connection,
        )

    def release_units(
        self, user_id: int, amount_units: int, *,
        connection: sqlite3.Connection,
    ) -> int:
        return wallet.release_units(
            user_id, amount_units, connection=connection
        )

    def settle_units(
        self, user_id: int, amount_units: int, *,
        connection: sqlite3.Connection,
    ) -> int:
        return wallet.settle_units(
            user_id, amount_units, connection=connection
        )


# ── Ledger adapter (Part F) ──────────────────────────────────────────


class LedgerPort(Protocol):
    """Withdrawal-scoped ledger operations on a caller-owned
    connection, with the reference/idempotency contract baked in."""

    def hold(
        self, user_id: int, amount_units: int, *, request_id: str,
        connection: sqlite3.Connection, rate_usdt_egp: str | None = None,
    ) -> LedgerEntry: ...

    def release(
        self, user_id: int, amount_units: int, *, request_id: str,
        connection: sqlite3.Connection, rate_usdt_egp: str | None = None,
    ) -> LedgerEntry: ...

    def settle(
        self, user_id: int, amount_units: int, *, request_id: str,
        connection: sqlite3.Connection, rate_usdt_egp: str | None = None,
    ) -> LedgerEntry: ...


class SqliteLedgerAdapter:
    """Withdrawal ledger port on top of the EXISTING ``LedgerService``.

    No INSERT logic, no delta logic, no entry-type semantics are
    duplicated here — ``hold`` = available -> held, ``release`` =
    held -> available, ``settlement`` = held -> gone come entirely
    from the ledger module.  Contract per operation:

    - ``reference_type = 'withdrawal'``, ``reference_id = request_id``
      (the schema's ``UNIQUE(reference_type, reference_id, entry_type)``
      allows at most one of each per request);
    - idempotency key ``withdrawal:<request_id>:<entry_type>`` — a
      replay returns the original entry instead of double-recording;
    - the caller's connection is used as-is (borrowed);
    - ledger errors pass through the MT-ADMIN-21 translation boundary.
    """

    REFERENCE_TYPE = "withdrawal"
    _ENTRY_API = {
        "hold": "record_hold",
        "release": "record_release",
        "settlement": "record_settlement",
    }

    def hold(
        self, user_id: int, amount_units: int, *, request_id: str,
        connection: sqlite3.Connection, rate_usdt_egp: str | None = None,
    ) -> LedgerEntry:
        return self._record(
            "hold", user_id, amount_units,
            request_id=request_id, connection=connection,
            rate_usdt_egp=rate_usdt_egp,
        )

    def release(
        self, user_id: int, amount_units: int, *, request_id: str,
        connection: sqlite3.Connection, rate_usdt_egp: str | None = None,
    ) -> LedgerEntry:
        return self._record(
            "release", user_id, amount_units,
            request_id=request_id, connection=connection,
            rate_usdt_egp=rate_usdt_egp,
        )

    def settle(
        self, user_id: int, amount_units: int, *, request_id: str,
        connection: sqlite3.Connection, rate_usdt_egp: str | None = None,
    ) -> LedgerEntry:
        return self._record(
            "settlement", user_id, amount_units,
            request_id=request_id, connection=connection,
            rate_usdt_egp=rate_usdt_egp,
        )

    def _record(
        self, entry_type: str, user_id: int, amount_units: int, *,
        request_id: str, connection: sqlite3.Connection,
        rate_usdt_egp: str | None,
    ) -> LedgerEntry:
        service = LedgerService(connection=connection)  # borrowed conn
        record = getattr(service, self._ENTRY_API[entry_type])
        with translated_errors():
            return record(
                user_id,
                amount_units=amount_units,
                reference_type=self.REFERENCE_TYPE,
                reference_id=request_id,
                idempotency_key=f"withdrawal:{request_id}:{entry_type}",
                rate_usdt_egp=rate_usdt_egp,
            )


# ── Payment-method read boundary (Part G) ────────────────────────────


def resolve_active_payment_method(
    method_id: object, db_path: str | None = None
) -> payment_method_store.PaymentMethod:
    """Resolve ONE active payment method behind the domain boundary.

    Reuses the existing strict lookup (no selection/routing logic): an
    inactive or missing method raises the domain
    ``PaymentMethodUnavailableError`` — it can never back a new
    withdrawal.  Destinations are never logged here.
    """
    with translated_errors():
        return payment_method_store.get_active_payment_method(
            method_id, db_path=db_path
        )
