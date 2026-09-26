"""
Task Reward Settlement (Micro-task MT-REWARD-01)
==================================================

Credits a successfully completed Task's reward to the user's USDT
wallet and records the matching ledger entry — inside the SAME
``db.transaction()`` (``BEGIN IMMEDIATE``) that performs the
CompletionGate's ``started → completed`` transition, so completion,
wallet credit and ledger entry commit or roll back as one unit.

Flow (invoked by ``CompletionGate.complete`` right after its
compare-and-set, on the transaction's own connection):

    CAS user_tasks → completed
        ↓
    TaskRewardService.settle_completion(conn, user_id, task_id, task)
        ↓
    reward validation → completion-cycle identity → idempotency guard
        ↓
    wallet.credit_units(connection=conn)     (available += units, held untouched)
        ↓
    LedgerService(connection=conn).record_credit(reference_type="task")
        ↓
    outer transaction commits (this module never commits/rolls back)

Money representation (existing model only, never float):

    ``tasks.reward_units`` (MT-ADMIN-13) is the **authoritative**
    accounting value: exact USDT atomic units (1 USDT = 100,000,000,
    sub-cent included), read as an ``int`` straight from the stored
    row.  ``tasks.reward`` keeps its whole-USDT display meaning and is
    still validated on every settlement — a corrupt display row never
    settles — but it supplies the credited amount ONLY when the atomic
    field is absent or NULL (legacy in-memory fixtures / pre-migration
    rows).  A reward that is negative, a float smuggled past the
    application layer, or otherwise not a valid USDT amount raises
    :class:`TaskRewardError` and the whole completion rolls back —
    values are never guessed, rounded, or reinterpreted.

Eligibility / idempotency:

    - Reward units == 0 → the task still completes, but nothing is
      credited and no ledger row is written (the ledger schema only
      accepts positive amounts).
    - ``reference_type`` is always ``"task"``.
    - ``reference_id`` and ``idempotency_key`` are the same stable
      string ``task_reward:<user_id>:<task_id>:<cycle>`` where
      ``<cycle>`` is:

        * ``submission:<submission_id>`` — the persisted submission
          whose verification produced this completion (production path:
          TaskLifecycle → CompletionBridge → CompletionGate), or
        * ``cycle:<n>`` — the 1-based count of this user/task's existing
          task-reward credits, used for direct CompletionGate calls
          that have no submission record (legacy callers/tests).

      Each legitimate completion cycle therefore gets a distinct
      identity (repeatable tasks never collide with earlier cycles),
      while a retry of the *same* completion resolves to the same key —
      never a second credit.  The database enforces both ends: the
      partial-unique ``idempotency_key`` index and
      ``UNIQUE(reference_type, reference_id, entry_type)``.  If an entry
      for this cycle already exists, it is returned as-is and the
      wallet is NOT credited again.

Snapshot metadata (deterministic JSON via ``LedgerService``):
    task_id, user_id, reward (the task definition's value at completion
    time), reward_units, cycle token, submission_id/attempt_number when
    known — enough to audit the credited amount against the task
    definition as it stood at completion.  No secrets, no client
    payloads, no floating point.

Boundaries preserved:

    - ``task_verifier`` / ``VerificationResult`` stay side-effect free.
    - HTTP routes and Mini App JavaScript stay financially unaware —
      they only reach this code through TaskLifecycle → CompletionBridge.
    - No withdrawals, deposits, fees, exchange rates, referral or
      advertiser funding, EGP rewards, or floating-point money.
    - This module never opens a transaction, never commits, never rolls
      back: the outer ``db.transaction()`` owns the connection (no
      nested transactions), and all money SQL goes through the existing
      wallet service and ``LedgerService`` — no second implementation.

Run:
    python3 -m pytest test_task_reward.py -v
"""

from __future__ import annotations

import logging

import db
import wallet
from ledger import LedgerEntryNotFoundError, LedgerService
from task_submission_store import SubmissionRecord, TaskSubmissionStore

logger = logging.getLogger(__name__)

# Ledger reference_type for every task-reward credit (schema CHECK
# allows 'withdrawal', 'task', 'referral', 'deposit', 'admin_credit',
# 'adjustment' — task rewards always use 'task').
TASK_REFERENCE_TYPE = "task"

# Stable identity prefix: ``<prefix>:<user_id>:<task_id>:<cycle>``.
REWARD_REFERENCE_PREFIX = "task_reward"


class TaskRewardError(Exception):
    """A completion's reward cannot be settled.

    Raised (before or during the financial writes) when the task's
    reward is not a valid USDT amount.  The caller's transaction must
    roll back — the completion, the wallet credit and the ledger entry
    all disappear together.
    """


class TaskRewardService:
    """Atomic task-reward settlement: wallet credit + ledger entry.

    Everything here runs inside the CompletionGate's open transaction
    on that transaction's connection.  The service:

    1. validates the task's stored ``reward_units`` (authoritative
       atomic units) and the legacy ``reward`` display value, rejecting
       anything that is not an exact non-negative integer amount,
    2. derives the stable completion-cycle identity,
    3. replays an already-recorded reward instead of crediting twice,
    4. credits ``wallets.available_units`` (held never changes),
    5. records the ``reference_type="task"`` ledger credit.

    It must NOT:
    - open/commit/roll back its own transaction,
    - write SQL directly (wallet and ledger services own their SQL),
    - read reward or identity from client input,
    - implement fees, conversion, EGP, withdrawals, or referral logic.
    """

    # ── Reward calculation ──────────────────────────────────────

    @staticmethod
    def reward_units(task: dict | None) -> int:
        """Exact USDT atomic units this task's completion must credit.

        ``tasks.reward_units`` is authoritative: a populated stored
        value IS the accounting amount itself — whole USDT and
        sub-cent alike — and must be an exact non-negative ``int``
        (bool / non-int / negative → :class:`TaskRewardError`, never
        coerced, never rounded, never converted through float).

        The legacy whole-USDT ``reward`` field is validated on EVERY
        call (a corrupt display row still rolls the completion back,
        exactly as before MT-ADMIN-13) and provides the amount only
        when ``reward_units`` is absent or NULL — legacy in-memory
        fixtures and rows predating the migration.  ``reward`` never
        overrides a populated ``reward_units``.

        Raises:
            TaskRewardError: missing task, invalid legacy reward, or
                invalid stored ``reward_units`` — the caller's
                transaction rolls the completion back with the money.
        """
        if not isinstance(task, dict):
            raise TaskRewardError(
                "a server-side task definition is required to settle a reward"
            )
        try:
            legacy_units = wallet.decimal_to_units(
                task.get("reward"), field="reward"
            )
        except wallet.InvalidWalletAmountError as exc:
            raise TaskRewardError(
                f"task {task.get('id')} has an invalid reward: {exc}"
            ) from exc

        stored = task.get("reward_units")
        if stored is None:
            # No atomic value stored (legacy fixture / pre-migration
            # row): the validated whole-USDT conversion remains the
            # amount, exactly as before MT-ADMIN-13.
            return legacy_units
        if isinstance(stored, bool) or not isinstance(stored, int):
            raise TaskRewardError(
                f"task {task.get('id')} has invalid reward_units: "
                "must be an exact int of atomic units"
            )
        if stored < 0:
            raise TaskRewardError(
                f"task {task.get('id')} has invalid reward_units: "
                "must not be negative"
            )
        return stored

    # ── Settlement (inside the caller's transaction) ────────────

    @staticmethod
    def settle_completion(
        connection,
        *,
        user_id: int,
        task_id: int,
        task: dict,
    ) -> int | None:
        """Credit the reward of a just-completed task on ``connection``.

        MUST be called inside the caller's open ``db.transaction()``
        AFTER the ``started → completed`` compare-and-set succeeded.
        The connection is borrowed: never committed, rolled back or
        closed here — the outer transaction owns its fate, so any
        failure below rolls the completion back with the money.

        Args:
            connection: the transaction's sqlite3 connection.
            user_id: the completing user (server-side identity).
            task_id: the completed task's id.
            task: the task definition as read inside this transaction.

        Returns:
            The credited units, or ``None`` when the reward is zero
            (nothing to credit — the task still completes).

        Raises:
            TaskRewardError: invalid reward (rolls everything back).
            wallet.WalletError hierarchy / ledger errors: financial
            write failures (roll everything back).
        """
        units = TaskRewardService.reward_units(task)
        if units == 0:
            logger.info(
                "Task reward is zero: completion without credit "
                "user=%d task=%d",
                user_id, task_id,
            )
            return None

        token, submission = TaskRewardService._cycle_identity(
            connection, user_id=user_id, task_id=task_id
        )
        reference_id = (
            f"{REWARD_REFERENCE_PREFIX}:{user_id}:{task_id}:{token}"
        )
        idempotency_key = reference_id
        ledger = LedgerService(connection=connection)

        # Idempotency guard (PART 5): if this completion cycle already
        # produced its ledger entry, never credit the wallet again —
        # return the existing reward result instead.
        try:
            existing = ledger.get_by_idempotency_key(idempotency_key)
        except LedgerEntryNotFoundError:
            existing = None
        if existing is not None:
            logger.info(
                "Task reward already settled for key=%s; no second credit",
                idempotency_key,
            )
            return existing.amount_units

        metadata = {
            "attempt_number": (
                submission.attempt_number if submission is not None else None
            ),
            "cycle": token,
            "reward": task.get("reward"),
            "reward_units": units,
            "submission_id": (
                submission.submission_id if submission is not None else None
            ),
            "task_id": task_id,
            "user_id": user_id,
        }

        # 1) wallet: available += units (held untouched) on THIS
        #    transaction's connection — one UPDATE, rowcount-verified.
        wallet.ensure_wallet(user_id, connection=connection)
        wallet.credit_units(user_id, units, connection=connection)

        # 2) ledger: the matching credit entry on the SAME connection.
        #    The service never commits — the outer transaction does.
        entry = ledger.record_credit(
            user_id,
            amount_units=units,
            reference_type=TASK_REFERENCE_TYPE,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )
        logger.info(
            "Task reward credited: user=%d task=%d units=%d "
            "ledger_id=%s key=%s",
            user_id, task_id, units, entry.id, idempotency_key,
        )
        return units

    # ── Completion-cycle identity ───────────────────────────────

    @staticmethod
    def _cycle_identity(
        connection, *, user_id: int, task_id: int
    ) -> tuple[str, SubmissionRecord | None]:
        """Stable cycle token for this completion.

        Returns ``(token, submission)``; the caller assembles the full
        identity ``task_reward:<user>:<task>:<token>`` which doubles as
        the ledger idempotency key:

        - Production path (TaskLifecycle → CompletionBridge): the
          verification that produced this completion is persisted as a
          ``passed`` submission whose ``completed_at`` is stamped only
          AFTER the gate succeeds — so the newest passed-and-unstamped
          record is exactly this cycle's submission, and its globally
          unique ``submission_id`` distinguishes repeatable cycles.
        - Direct CompletionGate calls with no submission record
          (legacy callers/tests): fall back to ``cycle:<n>`` where
          ``n`` is the count of this user/task's existing task-reward
          credits + 1, computed on the transaction's own connection
          under ``BEGIN IMMEDIATE`` (serialized — never reused).

        Never returns a bare ``task:<user>:<task>`` identity: that
        would collide across repeatable cycles.
        """
        records = TaskSubmissionStore.list_user_task_submissions(
            user_id, task_id
        )
        submission: SubmissionRecord | None = None
        for record in reversed(records):  # newest first
            if (
                record.status == db.SUBMISSION_STATUS_PASSED
                and record.completed_at is None
            ):
                submission = record
                break

        if submission is not None:
            token = f"submission:{submission.submission_id}"
        else:
            already_credited = connection.execute(
                "SELECT COUNT(*) AS c FROM ledger "
                "WHERE user_id = ? AND entry_type = 'credit' "
                "AND reference_type = ? AND reference_id GLOB ?",
                (
                    user_id,
                    TASK_REFERENCE_TYPE,
                    f"{REWARD_REFERENCE_PREFIX}:{user_id}:{task_id}:*",
                ),
            ).fetchone()["c"]
            token = f"cycle:{already_credited + 1}"

        return token, submission
