"""
Task Funding Service (roadmap item 4: advertiser funding)
==========================================================

The ONE place a task's funding is secured: the advertiser's wallet is
charged the task's total cost and the matching accounting entries are
written, all inside the CALLER's ``db.transaction()`` — typically the
same transaction that creates the task
(``task_creation.create_task_from_spec`` → wizard publish / legacy
``/addtask``), so either the task exists funded or nothing happened.

Financial meaning (derived from the existing model, not invented):

    advertiser total cost = worker reward + platform commission
                          = ``tasks.reward_units`` + ``tasks.commission_units``

Both amounts are the task's IMMUTABLE creation-time snapshot: the
commission is read from the persisted ``commission_units`` column and
never recomputed from the live ``advertiser_commission`` setting
(current rate × historical task is forbidden — the snapshot is the
accounting source of that task).

Wallet movement (existing wallet vocabulary only — this module never
writes SQL against ``wallets`` or ``ledger``):

    wallet.reserve(total, connection=conn)        available → held
    ledger.record_hold(...)                      the matching hold
    wallet.settle_units(total, connection=conn)   held leaves for good
    ledger.record_settlement(...)                the matching settlement

Net effect of the pair — available −total, held unchanged — is exactly
an immediate permanent debit of the total cost, expressed through the
SAME reserve→settle primitives and the SAME hold→settlement ledger
events the withdrawal flow established (withdrawal_service does
reserve+hold at request time and settle+settlement at completion).

Why not ``LedgerService.record_debit`` (Phase 6 decision):
``record_debit`` is schema/API-only today because ``wallet.py`` — the
ONLY module permitted to mutate ``wallets`` — ships no debit primitive:
its money-out path IS ``reserve`` then ``settle_units``.  Writing a
single ``debit`` row while the wallet moved through held would either
desync the two ledgers or force a new wallet primitive into a protected
module.  ``hold`` + ``settlement`` are the correct existing events for
the existing wallet movement, so no new ledger event type and no new
wallet API was invented.

Atomicity contract:

    ``fund_task(connection, ...)`` REQUIRES an already-open
    ``db.transaction()`` (verified — it never begins, commits or rolls
    back anything itself).  Every write runs on that borrowed
    connection, so a failure anywhere (wallet, ledger, record insert)
    rolls the wallet mutation, both ledger rows, the funding record
    and the freshly created task back together.

Exactly-once charging:

    * ``task_funding.task_id`` is the PRIMARY KEY — a second funding
      of the same task raises before any mutation (and the INSERT is
      still a hard database backstop against races).
    * the ledger rows carry idempotency keys
      ``task_funding:<task_id>:hold`` / ``...:settlement`` and the
      existing ``UNIQUE(reference_type, reference_id, entry_type)``
      constraint, mirroring ``task_reward.py``.

Boundaries (this module must NOT):

    - open/commit/rollback a transaction (the caller owns it),
    - read any rate/setting or recompute commission (snapshot only),
    - touch task state (``active``), user_tasks, drafts, or rewards,
    - be called from Telegram/HTTP transport code — transports call
      the creation service, never this one directly for accounting,
    - use floats, ``round()``, or non-integer money math anywhere.

Run:
    python3 -m pytest test_task_funding.py -v
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

import db
import wallet
from ledger import LedgerService

logger = logging.getLogger(__name__)

# Ledger reference vocabulary (schema CHECK allows reference_type
# 'task'; task-reward credits use reference_id ``task_reward:...`` —
# funding uses ``task_funding:<task_id>`` so the two never collide).
TASK_REFERENCE_TYPE = "task"
FUNDING_REFERENCE_PREFIX = "task_funding"
HOLD_TOKEN = "hold"
SETTLEMENT_TOKEN = "settlement"

_FUNDING_COLUMNS = (
    "task_id, advertiser_id, reward_units, commission_units, "
    "total_units, created_at"
)

# Mirrors db._SQLITE_INT64_MAX — the signed SQLite INTEGER bound the
# stored atomic values must fit (integer money, never REAL).
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807


class TaskFundingError(Exception):
    """The task cannot be funded as requested.

    Raised before (or in place of) any committed mutation: unknown
    task/advertiser, a task without an immutable commission snapshot,
    an already-funded task, or a caller that did not open
    ``db.transaction()``.  ``task_creation`` translates it into its
    admin-displayable ``TaskCreationError``; the whole transaction
    rolls back with it.
    """


@dataclass(frozen=True)
class TaskFundingResult:
    """Immutable outcome of one successful funding."""

    task_id: int
    advertiser_id: int
    reward_units: int
    commission_units: int
    total_units: int
    hold_entry_id: int | None
    settlement_entry_id: int | None


# ── Validation helpers (fail fast, never coerce) ──────────────────────


def _require_id(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TaskFundingError(
            f"{field} must be a positive int, got {value!r}"
        )
    return value


def _require_units(value: object, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _SQLITE_INT64_MAX
    ):
        raise TaskFundingError(
            f"{field} must be a non-negative int of atomic units"
        )
    return value


def _cost_snapshot(
    connection: sqlite3.Connection, task_id: int
) -> tuple[int, int]:
    """(reward_units, commission_units) — the task's immutable snapshot.

    The commission comes VERBATIM from ``tasks.commission_units``; a
    NULL snapshot (legacy/direct-created row whose commission was never
    resolved) is refused rather than guessed from the live setting —
    charging a historical task at the current rate is forbidden.

    ``tasks.reward_units`` is authoritative when populated; a NULL
    there falls back to the same whole-USDT derivation
    ``db.create_task``/``task_reward`` accept for legacy rows.
    """
    row = connection.execute(
        "SELECT reward, reward_units, commission_units "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise TaskFundingError(f"task {task_id} does not exist")

    commission_units = row["commission_units"]
    if commission_units is None:
        raise TaskFundingError(
            f"task {task_id} has no commission snapshot; "
            "refusing to fund at a guessed rate"
        )
    _require_units(commission_units, field="commission_units")

    reward_units = row["reward_units"]
    if reward_units is None:
        legacy_reward = row["reward"]
        if (
            isinstance(legacy_reward, bool)
            or not isinstance(legacy_reward, int)
            or legacy_reward < 0
        ):
            raise TaskFundingError(
                f"task {task_id} has no valid reward_units to fund"
            )
        reward_units = legacy_reward * wallet.USDT_SCALE
    _require_units(reward_units, field="reward_units")

    if reward_units + commission_units > _SQLITE_INT64_MAX:
        raise TaskFundingError(
            f"task {task_id} total cost exceeds the INTEGER range"
        )
    return reward_units, commission_units


# ── Read API ──────────────────────────────────────────────────────────


def get_funding(
    task_id: object, *, connection: sqlite3.Connection | None = None
) -> dict | None:
    """The persisted funding record for ``task_id``, or None (unfunded).

    Read-only; with ``connection`` the read joins that (borrowed)
    connection, without it the standard ``db.get_connection()`` scope
    is used.  Never exposes other users' data — the row is keyed by
    the task alone and carries no credentials.
    """
    if (
        isinstance(task_id, bool)
        or not isinstance(task_id, int)
        or task_id <= 0
    ):
        return None
    if connection is not None:
        row = connection.execute(
            f"SELECT {_FUNDING_COLUMNS} FROM task_funding "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    with db.get_connection() as conn:
        row = conn.execute(
            f"SELECT {_FUNDING_COLUMNS} FROM task_funding "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _insert_record(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    advertiser_id: int,
    reward_units: int,
    commission_units: int,
    total_units: int,
) -> None:
    """Insert the one funding row on the caller's connection.

    Kept as the module's single INSERT seam so failure-injection tests
    can prove that a record-persistence failure rolls the already
    executed wallet mutation, ledger rows and task INSERT back.
    """
    connection.execute(
        "INSERT INTO task_funding "
        "(task_id, advertiser_id, reward_units, commission_units, "
        " total_units) VALUES (?, ?, ?, ?, ?)",
        (task_id, advertiser_id, reward_units, commission_units,
         total_units),
    )


# ── Funding (inside the caller's transaction) ─────────────────────────


def fund_task(
    connection: sqlite3.Connection,
    *,
    task_id: object,
    advertiser_id: object,
) -> TaskFundingResult:
    """Charge the advertiser the task's total cost — atomically.

    MUST be called inside the caller's open ``db.transaction()``:
    the connection is borrowed, never begun/committed/rolled back/
    closed here, so the charge commits or vanishes together with the
    caller's task INSERT (and every failure leaves zero partial
    state).

    Steps, in order:

        validate ids + load immutable cost snapshot from the task row
        refuse an already-funded task (PRIMARY KEY backstop too)
        ensure the advertiser's wallet row exists (validates identity)
        if total > 0:
            wallet.reserve(total, connection=conn)          available → held
            ledger.record_hold(...)                          hold entry
            wallet.settle_units(total, connection=conn)      held leaves
            ledger.record_settlement(...)                    settlement entry
        INSERT INTO task_funding (… the exact charged snapshot …)

    Args:
        connection: the transaction's sqlite3 connection.
        task_id: the freshly created task to fund.
        advertiser_id: the authenticated funding advertiser (the
            creation surface's authenticated actor — never client
            payload data).

    Returns:
        TaskFundingResult with the exact charged amounts.

    Raises:
        TaskFundingError: invalid ids, unknown task, missing snapshot,
            already funded, or no open transaction — nothing mutated.
        wallet.InsufficientBalanceError: available balance cannot
            cover the total (raised by the wallet service; the caller's
            transaction rolls everything back).
        wallet.WalletError / ledger errors / sqlite3 errors: financial
            write failures — the caller's transaction rolls back.
    """
    task_id = _require_id(task_id, field="task_id")
    advertiser_id = _require_id(advertiser_id, field="advertiser_id")

    if not getattr(connection, "in_transaction", False):
        raise TaskFundingError(
            "fund_task must run inside the caller's db.transaction()"
        )

    reward_units, commission_units = _cost_snapshot(connection, task_id)
    total_units = reward_units + commission_units

    # Exactly-once (application level; the PRIMARY KEY below is the
    # database-level backstop): a second funding attempt mutates
    # nothing — the refusal happens BEFORE any wallet or ledger write.
    existing = connection.execute(
        "SELECT 1 FROM task_funding WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if existing is not None:
        raise TaskFundingError(f"task {task_id} is already funded")

    # Validates the advertiser is a registered account and gives them
    # a wallet row (idempotent INSERT OR IGNORE, same transaction).
    wallet.ensure_wallet(advertiser_id, connection=connection)

    hold_entry_id: int | None = None
    settlement_entry_id: int | None = None

    if total_units > 0:
        reference_id = f"{FUNDING_REFERENCE_PREFIX}:{task_id}"
        metadata = {
            "advertiser_id": advertiser_id,
            "commission_units": commission_units,
            "reward_units": reward_units,
            "task_id": task_id,
            "total_units": total_units,
        }
        ledger = LedgerService(connection=connection)

        # 1. available → held (one conditional UPDATE — insufficient
        #    balance raises here with zero rows written beyond the
        #    wallet-row ensure above, and the caller's ROLLBACK clears
        #    even that).  ``wallet.reserve`` takes a USDT amount, not
        #    units — the exact integer→Decimal conversion mirrors
        #    withdrawal_store.SqliteWalletAdapter (never a float).
        wallet.reserve(
            advertiser_id,
            wallet.units_to_decimal(total_units),
            connection=connection,
        )

        # 2. the matching hold entry on the SAME connection.
        hold_entry = ledger.record_hold(
            advertiser_id,
            amount_units=total_units,
            reference_type=TASK_REFERENCE_TYPE,
            reference_id=reference_id,
            idempotency_key=f"{reference_id}:{HOLD_TOKEN}",
            metadata=metadata,
        )
        hold_entry_id = hold_entry.id

        # 3. held leaves the wallet for good — the commission and
        #    reward pool are actually collected at funding time.
        wallet.settle_units(advertiser_id, total_units, connection=connection)

        # 4. the matching settlement entry on the SAME connection.
        settlement_entry = ledger.record_settlement(
            advertiser_id,
            amount_units=total_units,
            reference_type=TASK_REFERENCE_TYPE,
            reference_id=reference_id,
            idempotency_key=f"{reference_id}:{SETTLEMENT_TOKEN}",
            metadata=metadata,
        )
        settlement_entry_id = settlement_entry.id

    # 5. the persisted funding record — PRIMARY KEY task_id makes a
    #    second charge for this task a hard database error, and its
    #    CHECK already pins total == reward + commission.
    _insert_record(
        connection,
        task_id=task_id,
        advertiser_id=advertiser_id,
        reward_units=reward_units,
        commission_units=commission_units,
        total_units=total_units,
    )

    logger.info(
        "Task funded: task=%d advertiser=%d reward_units=%d "
        "commission_units=%d total_units=%d hold_ledger=%s "
        "settlement_ledger=%s",
        task_id, advertiser_id, reward_units, commission_units,
        total_units, hold_entry_id, settlement_entry_id,
    )
    return TaskFundingResult(
        task_id=task_id,
        advertiser_id=advertiser_id,
        reward_units=reward_units,
        commission_units=commission_units,
        total_units=total_units,
        hold_entry_id=hold_entry_id,
        settlement_entry_id=settlement_entry_id,
    )
