"""
Ledger Persistence Service (Micro-task MT-3)
=============================================

Append-only accounting-event store on top of the ``ledger`` SQLite table
created in MT-1 (schema is authoritative: integer USDT units,
1 USDT = 100,000,000 units, ``currency = 'USDT'``, no REAL/float money).

Responsibilities (MT-3 only):

    - record immutable credit / debit / hold / release / settlement rows
    - compute ``available_delta`` / ``held_delta`` from ``entry_type``
      (never trust the caller; the schema CHECK verifies them again)
    - idempotent replay by ``idempotency_key`` (safe retries return the
      original entry; materially different data raises a conflict)
    - reference uniqueness via the existing
      ``UNIQUE(reference_type, reference_id, entry_type)`` constraint,
      converted into a clear domain exception
    - read APIs: by ledger id, by idempotency key, and a user's entries
      in deterministic newest-first order

Deliberately NOT implemented here (future micro-tasks):

    - wallet balance mutation: this module never imports ``wallet`` and
      never calls reserve / release / settle — it records accounting
      events ONLY.  The later atomic integration task will combine a
      wallet mutation with a ledger insert inside one transaction.
    - withdrawal integration (MT-4/MT-5), deposits, rewards, admin
      credits, Mini App, Telegram handlers, business workflows
    - balance computation from ledger rows
    - a new transaction framework / money-transaction helper

Connection design (transaction ownership is explicit):

    ``LedgerService()`` with no connection — every operation runs in its
    own ``db.get_connection()`` scope, which commits (or rolls back) and
    closes per call.

    ``LedgerService(connection=conn)`` — the caller owns the transaction:
    statements are executed on the supplied connection but the service
    NEVER commits, rolls back or closes it, so a future transaction
    layer can group wallet + ledger writes safely.

Conventions:

    - integer unit arithmetic only; no Decimal maths for stored units
      and no floating-point conversion anywhere (``test_ledger.py``
      AST-scans this source)
    - timestamps come from the schema's ``CURRENT_TIMESTAMP`` default,
      exactly like every other table in this repository
    - ``metadata``: a ``dict``/JSON value is serialized deterministically
      (``sort_keys``), a ``str`` must already be valid JSON and is stored
      byte-for-byte verbatim — caller data is never silently mutated
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

import db

# ── Domain constants (mirror the MT-1 schema CHECK constraints) ──────

ENTRY_TYPES = frozenset(
    {"credit", "debit", "hold", "release", "settlement"}
)
REFERENCE_TYPES = frozenset(
    {"withdrawal", "task", "referral", "deposit", "admin_credit", "adjustment"}
)
CURRENCY = "USDT"

# entry_type -> exact (available_delta, held_delta) for a given amount
_DELTAS = {
    "credit": lambda a: (a, 0),
    "debit": lambda a: (-a, 0),
    "hold": lambda a: (-a, a),
    "release": lambda a: (a, -a),
    "settlement": lambda a: (0, -a),
}

_ENTRY_COLUMNS = (
    "id, user_id, entry_type, amount_units, available_delta, held_delta, "
    "currency, reference_type, reference_id, idempotency_key, "
    "actor_user_id, rate_usdt_egp, metadata, created_at"
)

_INSERT_SQL = f"""
    INSERT INTO ledger (
        user_id, entry_type, amount_units, available_delta, held_delta,
        currency, reference_type, reference_id, idempotency_key,
        actor_user_id, rate_usdt_egp, metadata
    ) VALUES (?, ?, ?, ?, ?, '{CURRENCY}', ?, ?, ?, ?, ?, ?)
"""

# Fields compared on idempotent replay (every stored column except the
# identity/timestamp pair).
_REPLAY_FIELDS = (
    "user_id", "entry_type", "amount_units", "available_delta",
    "held_delta", "currency", "reference_type", "reference_id",
    "idempotency_key", "actor_user_id", "rate_usdt_egp", "metadata",
)


# ── Exceptions ───────────────────────────────────────────────────────

class LedgerError(Exception):
    """Base class for every ledger failure."""


class InvalidLedgerEntryError(LedgerError):
    """Entry data failed validation (bad ids, types, amounts, refs)."""


class LedgerIdempotencyConflictError(LedgerError):
    """An idempotency key was reused with different accounting data."""


class LedgerReferenceConflictError(LedgerError):
    """(reference_type, reference_id, entry_type) already recorded."""


class LedgerEntryNotFoundError(LedgerError):
    """No ledger entry matches the requested identifier."""


# ── Immutable entry model ────────────────────────────────────────────

@dataclass(frozen=True)
class LedgerEntry:
    """Frozen, read-only representation of one ledger row."""

    id: int
    user_id: int
    entry_type: str
    amount_units: int
    available_delta: int
    held_delta: int
    currency: str
    reference_type: str
    reference_id: str
    idempotency_key: Optional[str]
    actor_user_id: Optional[int]
    rate_usdt_egp: Optional[str]
    metadata: Optional[str]
    created_at: str


# ── Validation helpers (fail fast, domain exceptions only) ──────────

def _validate_units(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidLedgerEntryError(
            f"{field}: bool is not a money value; pass an int of units"
        )
    if isinstance(value, float):
        raise InvalidLedgerEntryError(
            f"{field}: float is forbidden in financial math; "
            f"pass an int of USDT units"
        )
    if not isinstance(value, int):
        raise InvalidLedgerEntryError(
            f"{field}: must be an int of USDT units, "
            f"got {type(value).__name__}"
        )
    if value <= 0:
        raise InvalidLedgerEntryError(
            f"{field}: must be greater than 0, got {value}"
        )
    return value


def _validate_user_id(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidLedgerEntryError(
            f"{field}: must be a positive int, got {value!r}"
        )
    return value


def _validate_choice(value: object, allowed: frozenset, *, field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise InvalidLedgerEntryError(
            f"{field}: {value!r} is not one of {sorted(allowed)}"
        )
    return value


def _validate_reference_id(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidLedgerEntryError(
            f"reference_id: must be a str, got {type(value).__name__}"
        )
    if value == "" or value.strip() != value:
        raise InvalidLedgerEntryError(
            f"reference_id: must be non-empty and free of surrounding "
            f"whitespace, got {value!r}"
        )
    return value


def _validate_idempotency_key(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidLedgerEntryError(
            f"idempotency_key: must be a str or None, "
            f"got {type(value).__name__}"
        )
    if value == "" or value.strip() != value:
        raise InvalidLedgerEntryError(
            f"idempotency_key: must be non-empty and free of surrounding "
            f"whitespace, got {value!r}"
        )
    return value


def _validate_actor(value: object) -> Optional[int]:
    if value is None:
        return None
    return _validate_user_id(value, field="actor_user_id")


def _validate_rate(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidLedgerEntryError(
            f"rate_usdt_egp: must be a canonical decimal str or None, "
            f"got {type(value).__name__}"
        )
    if value == "" or value.strip() != value:
        raise InvalidLedgerEntryError(
            f"rate_usdt_egp: must be non-empty and free of surrounding "
            f"whitespace, got {value!r}"
        )
    return value


def _canonicalize_metadata(value: object) -> Optional[str]:
    """Return the stored JSON text.

    - ``None`` -> ``None``
    - ``str``  -> must already be valid JSON; stored byte-for-byte
      verbatim (caller data is never rewritten)
    - anything else -> deterministically serialized with ``sort_keys``
      and compact separators; non-JSON-serializable values are rejected
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            json.loads(value)
        except ValueError as exc:
            raise InvalidLedgerEntryError(
                f"metadata: str values must be valid JSON: {exc}"
            ) from exc
        return value
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    except (TypeError, ValueError) as exc:
        raise InvalidLedgerEntryError(
            f"metadata: value is not JSON-serializable: {exc}"
        ) from exc


def _entry_from_row(cursor, row) -> Optional[LedgerEntry]:
    """Map a fetched row (sqlite3.Row or tuple) to a frozen entry."""
    if row is None:
        return None
    columns = [description[0] for description in cursor.description]
    values = {columns[i]: row[i] for i in range(len(columns))}
    return LedgerEntry(
        id=values["id"],
        user_id=values["user_id"],
        entry_type=values["entry_type"],
        amount_units=values["amount_units"],
        available_delta=values["available_delta"],
        held_delta=values["held_delta"],
        currency=values["currency"],
        reference_type=values["reference_type"],
        reference_id=values["reference_id"],
        idempotency_key=values["idempotency_key"],
        actor_user_id=values["actor_user_id"],
        rate_usdt_egp=values["rate_usdt_egp"],
        metadata=values["metadata"],
        created_at=values["created_at"],
    )


# ── Service ──────────────────────────────────────────────────────────

class LedgerService:
    """Append-only ledger persistence.

    Public API (and nothing else — the module ships no mutation API):

        record_credit / record_debit / record_hold / record_release /
        record_settlement
        get_entry / get_by_idempotency_key / list_user_entries

    Args:
        connection: optional caller-owned ``sqlite3.Connection``.  When
            supplied, the service executes statements on it but never
            commits, rolls back or closes it — transaction ownership
            stays with the caller (future integration layer).  When
            omitted, each operation uses its own ``db.get_connection()``
            scope which commits and closes per call.
    """

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self._injected_connection = connection

    # ── connection scoping (explicit ownership) ────────────────

    @contextmanager
    def _connection(self):
        if self._injected_connection is not None:
            # Borrowed connection: never commit/rollback/close it here.
            yield self._injected_connection
        else:
            # Standalone: get_connection owns commit/rollback/close.
            with db.get_connection() as conn:
                yield conn

    # ── record APIs ────────────────────────────────────────────

    def record_credit(
        self,
        user_id: int,
        *,
        amount_units: int,
        reference_type: str,
        reference_id: str,
        idempotency_key: str | None = None,
        actor_user_id: int | None = None,
        rate_usdt_egp: str | None = None,
        metadata: object = None,
    ) -> LedgerEntry:
        """Record an incoming movement: available += amount_units."""
        return self._record(
            "credit",
            user_id=user_id,
            amount_units=amount_units,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            actor_user_id=actor_user_id,
            rate_usdt_egp=rate_usdt_egp,
            metadata=metadata,
        )

    def record_debit(
        self,
        user_id: int,
        *,
        amount_units: int,
        reference_type: str,
        reference_id: str,
        idempotency_key: str | None = None,
        actor_user_id: int | None = None,
        rate_usdt_egp: str | None = None,
        metadata: object = None,
    ) -> LedgerEntry:
        """Record an outgoing movement: available -= amount_units."""
        return self._record(
            "debit",
            user_id=user_id,
            amount_units=amount_units,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            actor_user_id=actor_user_id,
            rate_usdt_egp=rate_usdt_egp,
            metadata=metadata,
        )

    def record_hold(
        self,
        user_id: int,
        *,
        amount_units: int,
        reference_type: str,
        reference_id: str,
        idempotency_key: str | None = None,
        actor_user_id: int | None = None,
        rate_usdt_egp: str | None = None,
        metadata: object = None,
    ) -> LedgerEntry:
        """Record available -> held (funds reserved)."""
        return self._record(
            "hold",
            user_id=user_id,
            amount_units=amount_units,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            actor_user_id=actor_user_id,
            rate_usdt_egp=rate_usdt_egp,
            metadata=metadata,
        )

    def record_release(
        self,
        user_id: int,
        *,
        amount_units: int,
        reference_type: str,
        reference_id: str,
        idempotency_key: str | None = None,
        actor_user_id: int | None = None,
        rate_usdt_egp: str | None = None,
        metadata: object = None,
    ) -> LedgerEntry:
        """Record held -> available (reservation returned)."""
        return self._record(
            "release",
            user_id=user_id,
            amount_units=amount_units,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            actor_user_id=actor_user_id,
            rate_usdt_egp=rate_usdt_egp,
            metadata=metadata,
        )

    def record_settlement(
        self,
        user_id: int,
        *,
        amount_units: int,
        reference_type: str,
        reference_id: str,
        idempotency_key: str | None = None,
        actor_user_id: int | None = None,
        rate_usdt_egp: str | None = None,
        metadata: object = None,
    ) -> LedgerEntry:
        """Record held leaving the wallet permanently (consumed)."""
        return self._record(
            "settlement",
            user_id=user_id,
            amount_units=amount_units,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            actor_user_id=actor_user_id,
            rate_usdt_egp=rate_usdt_egp,
            metadata=metadata,
        )

    # ── read APIs ──────────────────────────────────────────────

    def get_entry(self, entry_id: int) -> LedgerEntry:
        """Fetch one entry by its ledger id.

        Raises:
            InvalidLedgerEntryError: malformed id.
            LedgerEntryNotFoundError: no such entry.
        """
        entry_id = _validate_user_id(entry_id, field="entry_id")
        with self._connection() as conn:
            cursor = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM ledger WHERE id = ?",
                (entry_id,),
            )
            entry = _entry_from_row(cursor, cursor.fetchone())
        if entry is None:
            raise LedgerEntryNotFoundError(f"no ledger entry {entry_id}")
        return entry

    def get_by_idempotency_key(self, idempotency_key: str) -> LedgerEntry:
        """Fetch one entry by its idempotency key.

        Raises:
            InvalidLedgerEntryError: malformed key.
            LedgerEntryNotFoundError: key never recorded.
        """
        key = _validate_idempotency_key(idempotency_key)
        if key is None:
            raise InvalidLedgerEntryError(
                "idempotency_key: must be a non-empty str"
            )
        with self._connection() as conn:
            entry = self._select_by_key(conn, key)
        if entry is None:
            raise LedgerEntryNotFoundError(
                f"no ledger entry for idempotency key {key!r}"
            )
        return entry

    def list_user_entries(self, user_id: int) -> list[LedgerEntry]:
        """One user's entries in deterministic newest-first order.

        Ordering is ``id DESC`` — the AUTOINCREMENT primary key is
        monotonic, so the order is stable even when several rows share
        the same ``created_at`` second.  Never returns other users'
        rows.  (Balances are deliberately not computed here.)

        Raises:
            InvalidLedgerEntryError: malformed user_id.
        """
        user_id = _validate_user_id(user_id, field="user_id")
        with self._connection() as conn:
            cursor = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM ledger "
                "WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            )
            rows = cursor.fetchall()
            return [
                _entry_from_row(cursor, row)
                for row in rows
            ]

    # ── internals ──────────────────────────────────────────────

    def _record(
        self,
        entry_type: str,
        *,
        user_id: int,
        amount_units: int,
        reference_type: str,
        reference_id: str,
        idempotency_key: str | None,
        actor_user_id: int | None,
        rate_usdt_egp: str | None,
        metadata: object,
    ) -> LedgerEntry:
        # 1) validate everything before touching the database
        entry_type = _validate_choice(
            entry_type, ENTRY_TYPES, field="entry_type"
        )
        amount = _validate_units(amount_units, field="amount_units")
        user_id = _validate_user_id(user_id, field="user_id")
        reference_type = _validate_choice(
            reference_type, REFERENCE_TYPES, field="reference_type"
        )
        reference_id = _validate_reference_id(reference_id)
        key = _validate_idempotency_key(idempotency_key)
        actor = _validate_actor(actor_user_id)
        rate = _validate_rate(rate_usdt_egp)
        meta = _canonicalize_metadata(metadata)
        available_delta, held_delta = _DELTAS[entry_type](amount)

        expected = {
            "user_id": user_id,
            "entry_type": entry_type,
            "amount_units": amount,
            "available_delta": available_delta,
            "held_delta": held_delta,
            "currency": CURRENCY,
            "reference_type": reference_type,
            "reference_id": reference_id,
            "idempotency_key": key,
            "actor_user_id": actor,
            "rate_usdt_egp": rate,
            "metadata": meta,
        }

        with self._connection() as conn:
            # 2) idempotent replay: same key -> original entry back
            if key is not None:
                existing = self._select_by_key(conn, key)
                if existing is not None:
                    return self._replay(existing, expected, key)

            # 3) foreign-key existence (clear domain error up front)
            self._require_user(conn, user_id, "user_id")
            if actor is not None:
                self._require_user(conn, actor, "actor_user_id")

            # 4) insert (append-only: the module has no other statement)
            try:
                cursor = conn.execute(
                    _INSERT_SQL,
                    (
                        user_id, entry_type, amount,
                        available_delta, held_delta,
                        reference_type, reference_id, key,
                        actor, rate, meta,
                    ),
                )
                entry_id = cursor.lastrowid
            except sqlite3.IntegrityError as exc:
                # a) lost idempotency race -> replay the winner
                if key is not None:
                    existing = self._select_by_key(conn, key)
                    if existing is not None:
                        return self._replay(existing, expected, key)
                # b) reference uniqueness -> dedicated domain conflict
                conflicting = self._select_reference(
                    conn, reference_type, reference_id, entry_type
                )
                if conflicting is not None:
                    raise LedgerReferenceConflictError(
                        f"{reference_type}:{reference_id} already has a "
                        f"{entry_type} entry (ledger id {conflicting.id})"
                    ) from exc
                # c) FK slipped past the pre-check -> validation error
                if "FOREIGN KEY" in str(exc).upper():
                    raise InvalidLedgerEntryError(
                        f"ledger insert rejected by foreign key: {exc}"
                    ) from exc
                raise  # unknown integrity problem: do not mask it

            cursor = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM ledger WHERE id = ?",
                (entry_id,),
            )
            entry = _entry_from_row(cursor, cursor.fetchone())
        return entry

    @staticmethod
    def _select_by_key(conn, key: str) -> LedgerEntry | None:
        cursor = conn.execute(
            f"SELECT {_ENTRY_COLUMNS} FROM ledger WHERE idempotency_key = ?",
            (key,),
        )
        return _entry_from_row(cursor, cursor.fetchone())

    @staticmethod
    def _select_reference(
        conn, reference_type: str, reference_id: str, entry_type: str
    ) -> LedgerEntry | None:
        cursor = conn.execute(
            f"SELECT {_ENTRY_COLUMNS} FROM ledger "
            "WHERE reference_type = ? AND reference_id = ? "
            "AND entry_type = ?",
            (reference_type, reference_id, entry_type),
        )
        return _entry_from_row(cursor, cursor.fetchone())

    @staticmethod
    def _require_user(conn, user_id: int, field: str) -> None:
        if conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ).fetchone() is None:
            raise InvalidLedgerEntryError(
                f"{field}: user {user_id} does not exist"
            )

    @staticmethod
    def _replay(
        existing: LedgerEntry, expected: dict, key: str
    ) -> LedgerEntry:
        """Same key + same accounting data -> return the original row;
        same key + materially different data -> dedicated conflict."""
        mismatched = [
            field
            for field in _REPLAY_FIELDS
            if getattr(existing, field) != expected[field]
        ]
        if mismatched:
            raise LedgerIdempotencyConflictError(
                f"idempotency key {key!r} was already recorded with "
                f"differing data (fields: {', '.join(mismatched)})"
            )
        return existing
