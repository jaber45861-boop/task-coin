"""
USDT Wallet Service (Micro-task MT-2)
=====================================

Persistent wallet primitives on top of the ``wallets`` table created in
MT-1.  Storage goes through the existing ``db.get_connection()``
architecture (WAL, ``PRAGMA foreign_keys = ON``, commit/rollback per
operation) — no second connection system.

Currency model:

    PRIMARY wallet currency = USDT   (EGP is display-only, never stored)
    1 USDT = 100,000,000 wallet units (``USDT_SCALE``), SQLite INTEGER.

    12.50000000 USDT = 1,250,000,000 wallet units
    Decimal("1.5")    -> 150,000,000 units

Scope (MT-2 only, plus MT-REWARD-01's injected-connection support):

    - exact Decimal <-> integer-unit conversion (no rounding, no float)
    - lazy wallet creation (``ensure_wallet``)
    - available-balance read (``balance_of`` -> Decimal, available only)
    - ``reserve`` / ``release_units`` / ``settle_units`` primitives
    - connection-injected availability credit (``credit_units``,
      MT-REWARD-01) so a caller's outer transaction can move units
      atomically with its own writes — no nested transactions, and
      this scope never commits/rolls back a caller-owned connection

Deliberately NOT implemented here (future micro-tasks):

    - Ledger entries (MT-3): every wallet mutation touches ONLY the
      ``wallets`` table — no ledger rows are ever written here (the
      task-reward ledger rows belong to ``task_reward.py``)
    - Withdrawal persistence / integration (MT-4, MT-5)
    - deposits, referral rewards, admin credits
    - EGP conversion, exchange-rate fetching, fee/rate pinning
    - Telegram handlers, Mini App, balance UI

Atomicity & concurrency:

    Every mutation is ONE conditional UPDATE verified by its affected-row
    count (``available_units >= ?`` / ``held_units >= ?``, or the
    rowcount of the availability credit), executed via
    the existing commit/rollback context manager.  The forbidden
    read-calculate-write pattern is never used, so two concurrent
    reserves cannot spend the same balance and a failed operation leaves
    no balance change.

Exceptions:

    Wallet-specific hierarchy rooted at ``WalletError``; this module does
    not import ``withdrawal_rules`` and shares no exception types with it.

Conventions:

    Money math is Decimal/integer only — floats are forbidden.
    ``test_wallet.py`` AST-scans this source for float usage, mirroring
    the withdrawal rules' source protection.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

import db

# ── USDT unit definition ─────────────────────────────────────────────
# 1 USDT = 100,000,000 wallet units (USDT's native 8 decimal places),
# stored as SQLite INTEGER.  No REAL, no floats, ever.
USDT_SCALE = 100_000_000
USDT_DECIMALS = 8


# ── Wallet-specific exceptions ───────────────────────────────────────

class WalletError(Exception):
    """Base class for every wallet failure."""


class InvalidWalletAmountError(WalletError):
    """Amount is not a valid, positive, <= 8 dp USDT quantity
    (or an invalid integer-unit count for unit-denominated operations)."""


class InsufficientBalanceError(WalletError):
    """``available`` cannot cover a reserve — no balance was changed."""


class InsufficientHeldBalanceError(WalletError):
    """``held`` cannot cover a release/settlement — no balance changed."""


class UserNotFoundError(WalletError):
    """``user_id`` does not identify an existing row in ``users``."""


# ── Unit-denominated view of a wallet row ────────────────────────────

class WalletUnits(NamedTuple):
    """Raw integer-unit balances (internal/tests; not a spendable quote)."""

    available_units: int
    held_units: int


# ── Exact Decimal <-> integer-unit conversion ────────────────────────

def decimal_to_units(value: object, *, field: str = "amount") -> int:
    """Convert a USDT amount to integer wallet units. Exact, never rounded.

    Accepts ``Decimal``, ``int`` (whole USDT) and ``str`` (normalized
    through ``Decimal`` — exact decimal text, not binary floating point).

    Rejects: ``float``, ``bool``, ``NaN``, ``Infinity``, negative values
    (all wallet amounts are non-negative magnitudes) and precision
    greater than 8 decimal places.

    Examples:
        Decimal("1")          -> 100000000
        Decimal("1.5")        -> 150000000
        Decimal("0.00000001") -> 1
        Decimal("0.000000001") -> rejected (9 decimal places)
    """
    if isinstance(value, bool):
        raise InvalidWalletAmountError(
            f"{field}: bool is not a monetary value; pass Decimal or str"
        )
    if isinstance(value, float):
        raise InvalidWalletAmountError(
            f"{field}: float is forbidden in financial math; "
            f"pass Decimal or str"
        )
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str):
        try:
            dec = Decimal(value)
        except InvalidOperation as exc:
            raise InvalidWalletAmountError(
                f"{field}: not a valid decimal: {value!r}"
            ) from exc
    else:
        raise InvalidWalletAmountError(
            f"{field}: unsupported type {type(value).__name__}; "
            f"pass Decimal or str"
        )

    if not dec.is_finite():
        raise InvalidWalletAmountError(
            f"{field}: must be a finite decimal, got {value!r}"
        )
    if dec < 0:
        raise InvalidWalletAmountError(
            f"{field}: must not be negative, got {dec}"
        )

    sign, digits, exponent = dec.as_tuple()
    if exponent < -USDT_DECIMALS:
        raise InvalidWalletAmountError(
            f"{field}: at most {USDT_DECIMALS} decimal place(s) allowed, "
            f"got {dec}"
        )

    # Exact integer math from the decimal tuple — no context rounding,
    # no float: units = digits * 10^(exponent + 8), exponent >= -8.
    digits_int = int("".join(str(d) for d in digits))
    return digits_int * 10 ** (exponent + USDT_DECIMALS)


def units_to_decimal(units: object) -> Decimal:
    """Convert integer wallet units back to a USDT ``Decimal``. Exact.

    Examples:
        100000000 -> Decimal("1.00000000")   (== Decimal("1"))
        150000000 -> Decimal("1.50000000")   (== Decimal("1.5"))
        1         -> Decimal("0.00000001")

    Integer input only: ``bool`` and ``float`` are rejected.
    Negative integers are converted exactly as well (useful for signed
    deltas in future services); balances themselves are never negative.
    """
    if isinstance(units, bool):
        raise InvalidWalletAmountError(
            "units: bool is not an integer unit count"
        )
    if isinstance(units, float):
        raise InvalidWalletAmountError(
            "units: float is forbidden in financial math; pass an int"
        )
    if not isinstance(units, int):
        raise InvalidWalletAmountError(
            f"units: must be an int, got {type(units).__name__}"
        )
    sign = 1 if units < 0 else 0
    digits = tuple(int(ch) for ch in str(abs(units)))
    # Constructed directly with exponent -8: exact, no division context.
    return Decimal((sign, digits, -USDT_DECIMALS))


# ── SQL: single conditional UPDATEs (atomic, rowcount-verified) ──────

_RESERVE_SQL = """
    UPDATE wallets
    SET available_units = available_units - ?,
        held_units = held_units + ?,
        updated_at = CURRENT_TIMESTAMP
    WHERE user_id = ?
      AND available_units >= ?
"""

_RELEASE_SQL = """
    UPDATE wallets
    SET available_units = available_units + ?,
        held_units = held_units - ?,
        updated_at = CURRENT_TIMESTAMP
    WHERE user_id = ?
      AND held_units >= ?
"""

_SETTLE_SQL = """
    UPDATE wallets
    SET held_units = held_units - ?,
        updated_at = CURRENT_TIMESTAMP
    WHERE user_id = ?
      AND held_units >= ?
"""

# Availability credit (MT-REWARD-01): available increases by the exact
# integer-unit amount, held is never touched.  The affected-row count is
# verified by the caller (the row must exist — never create money).
_CREDIT_SQL = """
    UPDATE wallets
    SET available_units = available_units + ?,
        updated_at = CURRENT_TIMESTAMP
    WHERE user_id = ?
"""


# ── Internal helpers ─────────────────────────────────────────────────

def _require_user_id(user_id: object) -> int:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise UserNotFoundError(
            f"user_id must be a positive int, got {user_id!r}"
        )
    return user_id


def _require_positive_units(units: object, *, field: str) -> int:
    """Validate an integer-unit amount for release/settle operations."""
    if isinstance(units, bool) or not isinstance(units, int):
        raise InvalidWalletAmountError(
            f"{field}: must be an int number of USDT units, "
            f"got {type(units).__name__}"
        )
    if units <= 0:
        raise InvalidWalletAmountError(
            f"{field}: must be greater than 0, got {units}"
        )
    return units


def _amount_to_units(amount: object, *, field: str) -> int:
    """Validate a Decimal-denominated amount: exact, positive, <= 8 dp."""
    units = decimal_to_units(amount, field=field)
    if units <= 0:
        raise InvalidWalletAmountError(
            f"{field}: must be greater than 0, got {amount!r}"
        )
    return units


# ── Wallet service ───────────────────────────────────────────────────

def ensure_wallet(
    user_id: int, *, connection: sqlite3.Connection | None = None
) -> bool:
    """Create the wallet row for an existing user if it is missing.

    Lazily initializes ``available_units = 0`` and ``held_units = 0``.
    Idempotent and race-safe (``INSERT OR IGNORE``): repeated or
    concurrent calls never duplicate the row.

    Args:
        user_id: existing Telegram user id.
        connection: optional caller-owned connection (MT-REWARD-01).
            When supplied, the row is created on THAT connection so the
            creation participates in the caller's transaction — this
            scope then never commits, rolls back or closes it.  When
            omitted, the standard ``db.get_connection()`` scope owns
            commit/rollback/close exactly as before.

    Returns:
        True when the row was created, False when it already existed.

    Raises:
        UserNotFoundError: invalid user_id or no such user in ``users``.
    """
    _require_user_id(user_id)
    if connection is not None:
        return _ensure_wallet_on(connection, user_id)
    with db.get_connection() as conn:
        return _ensure_wallet_on(conn, user_id)


def _ensure_wallet_on(conn: sqlite3.Connection, user_id: int) -> bool:
    """``ensure_wallet`` body executed on an explicit connection.

    The connection is borrowed: statements are executed on it but it
    is never committed, rolled back or closed here.
    """
    if conn.execute(
        "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
    ).fetchone() is None:
        raise UserNotFoundError(f"user {user_id} does not exist")
    if conn.execute(
        "SELECT 1 FROM wallets WHERE user_id = ?", (user_id,)
    ).fetchone() is not None:
        return False  # already there — no write, no contention
    cursor = conn.execute(
        "INSERT OR IGNORE INTO wallets "
        "(user_id, available_units, held_units) VALUES (?, 0, 0)",
        (user_id,),
    )
    return cursor.rowcount == 1


def credit_units(
    user_id: int,
    amount_units: object,
    *,
    connection: sqlite3.Connection | None = None,
) -> int:
    """Atomically add ``amount_units`` USDT units to available balance.

    ONE UPDATE (``available_units = available_units + ?``) with the
    affected-row count verified — the amount is added exactly once and
    ``held_units`` is never touched.  A missing wallet row or user
    raises instead of creating money.

    Connection ownership (MT-REWARD-01):

        With ``connection`` the statement runs inside the caller's
        open transaction (e.g. the CompletionGate's ``BEGIN
        IMMEDIATE`` block): this scope never commits/rolls back/closes
        it, there is no nested transaction, and the credit commits or
        rolls back together with the caller's other writes.  Without
        it, the standard ``db.get_connection()`` scope commits per
        call, exactly like the other mutations.

    Only the ``wallets`` table is touched: no ledger entry is ever
    written here — composing wallet + ledger is the settlement
    service's job (``task_reward.py``).

    Args:
        user_id: existing Telegram user id.
        amount_units: positive int of USDT units (bool/float rejected).
        connection: optional caller-owned connection to join.

    Returns:
        The credited amount in integer wallet units.

    Raises:
        UserNotFoundError, InvalidWalletAmountError, WalletError.
    """
    units = _require_positive_units(amount_units, field="amount_units")
    if connection is None:
        with db.get_connection() as conn:
            return _credit_units_on(conn, user_id, units)
    return _credit_units_on(connection, user_id, units)


def _credit_units_on(
    conn: sqlite3.Connection, user_id: int, units: int
) -> int:
    """Ensure the wallet exists on ``conn`` and credit ``units`` to it
    with one UPDATE on that same (borrowed) connection."""
    ensure_wallet(user_id, connection=conn)
    cursor = conn.execute(_CREDIT_SQL, (units, user_id))
    if cursor.rowcount != 1:
        raise WalletError(
            f"wallet row missing for user {user_id}; credit not applied"
        )
    return units


def wallet_units(user_id: int) -> WalletUnits:
    """Raw ``(available_units, held_units)`` for tests/internal services.

    Read-only: an existing user without a wallet row reports ``(0, 0)``
    without creating one.  Never raises for missing wallets.

    Raises:
        UserNotFoundError: invalid user_id or no such user.
    """
    _require_user_id(user_id)
    with db.get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ).fetchone() is None:
            raise UserNotFoundError(f"user {user_id} does not exist")
        row = conn.execute(
            "SELECT available_units, held_units FROM wallets "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return WalletUnits(0, 0)
    return WalletUnits(row["available_units"], row["held_units"])


def balance_of(user_id: int) -> Decimal:
    """Available USDT balance as an exact ``Decimal`` (e.g. 12.50000000).

    This matches the withdrawal rules' ``Ledger.balance_of`` contract:
    **available only** — held funds are never spendable balance and are
    not included here (use ``wallet_units()`` to inspect them).

    Raises:
        UserNotFoundError: invalid user_id or no such user.
    """
    return units_to_decimal(wallet_units(user_id).available_units)


def reserve(user_id: int, amount: object) -> int:
    """Atomically move ``amount`` USDT from available to held.

    ``available -= amount`` and ``held += amount`` happen as ONE
    conditional UPDATE guarded by ``available_units >= ?`` — concurrent
    reserves cannot overspend, and an insufficient balance raises
    without changing anything.

    Only the ``wallets`` table is touched: no ledger entry, no
    withdrawal row (MT-3 / MT-4 own those).

    Args:
        user_id: existing Telegram user id.
        amount: USDT amount as ``Decimal``/``str``/``int``; positive,
            at most 8 decimal places.

    Returns:
        The reserved amount in integer wallet units.

    Raises:
        UserNotFoundError, InvalidWalletAmountError,
        InsufficientBalanceError.
    """
    units = _amount_to_units(amount, field="amount")
    ensure_wallet(user_id)
    with db.get_connection() as conn:
        cursor = conn.execute(
            _RESERVE_SQL, (units, units, user_id, units)
        )
        if cursor.rowcount != 1:
            row = conn.execute(
                "SELECT available_units FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            available = row["available_units"] if row else 0
            raise InsufficientBalanceError(
                f"reserve of {units} units exceeds available balance "
                f"{available} units for user {user_id}"
            )
    return units


def release_units(user_id: int, amount_units: object) -> int:
    """Atomically move ``amount_units`` from held back to available.

    Exact inverse of ``reserve``: a matching release restores the wallet
    to precisely its pre-reserve state.

    Only the ``wallets`` table is touched: no ledger entry, no
    withdrawal status change.

    Raises:
        UserNotFoundError, InvalidWalletAmountError,
        InsufficientHeldBalanceError.
    """
    units = _require_positive_units(amount_units, field="amount_units")
    ensure_wallet(user_id)
    with db.get_connection() as conn:
        cursor = conn.execute(
            _RELEASE_SQL, (units, units, user_id, units)
        )
        if cursor.rowcount != 1:
            row = conn.execute(
                "SELECT held_units FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            held = row["held_units"] if row else 0
            raise InsufficientHeldBalanceError(
                f"release of {units} units exceeds held balance "
                f"{held} units for user {user_id}"
            )
    return units


def settle_units(user_id: int, amount_units: object) -> int:
    """Permanently remove ``amount_units`` from held (funds leave).

    ``held -= amount_units`` only — ``available`` is deliberately NOT
    reduced again (a settled amount was already removed from available
    when it was reserved).

    Only the ``wallets`` table is touched: no ledger entry, no
    withdrawal state change.

    Raises:
        UserNotFoundError, InvalidWalletAmountError,
        InsufficientHeldBalanceError.
    """
    units = _require_positive_units(amount_units, field="amount_units")
    ensure_wallet(user_id)
    with db.get_connection() as conn:
        cursor = conn.execute(_SETTLE_SQL, (units, user_id, units))
        if cursor.rowcount != 1:
            row = conn.execute(
                "SELECT held_units FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            held = row["held_units"] if row else 0
            raise InsufficientHeldBalanceError(
                f"settlement of {units} units exceeds held balance "
                f"{held} units for user {user_id}"
            )
    return units
