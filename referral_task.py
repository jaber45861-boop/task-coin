"""
Paid Referral Task (MT-TASK-06)
===============================

The buyer-mediated Paid Referral Task family, adapted from the old
repo's referral_tasks / referral_task_claims behavior into the new
Task architecture.  Old tables and monolithic handlers are NOT copied.

Task type:
    referral_task  (distinct from channel_subscription and
                    telegram_channel — a third, separate family)

Task data contract (server-set, trusted, validated on every read):

    {
        "provider": "telegram",
        "action": "referral",
        "target": {
            "bot_username": "<target bot username>"
        },
        "approver": {
            "telegram_user_id": <buyer/client telegram user id>
        }
    }

`approver` is the minimum representation of the old repo's
referral_tasks.buyer_id: the buyer/client identity lives inside the
existing server-controlled task definition (no generic owner column on
tasks, nothing client-writable).  Reward is NOT part of task_data — it
stays in tasks.reward.

Referral identity (PART 3 / PART 11):
    ONLY the existing users.referred_by relationship, read server-side.
    A claimable worker must have at least one genuine referral — a user
    row whose referred_by equals the worker — excluding self-attributed
    rows (registration already blocks self-referral; this validates it
    again).  First-referrer-wins registration behavior is untouched.
    The client NEVER supplies referred_by / referrer_id /
    referral_user_id / buyer_id / approval / reward / user_id.

Pending approval lives at the SUBMISSION layer (never on user_tasks):

    worker ─► TaskStartGate ─► started
    worker ─► ReferralClaimService.submit
              ─► task_submissions row born with
                 status='submitted' + approval_status='pending'
              ─► PENDING outcome (NO VerificationResult, NO completion,
                 NO reward)
    buyer  ─► ReferralApprovalService.decide (authorized approver only)
              approve ─► CAS approval ─► record 'passed'
                        ─► CompletionGate ─► TaskRewardService
              reject  ─► CAS approval ─► record 'failed'
                        ─► NO CompletionGate, NO reward
    user_tasks stays available/started/completed — approval state and
    rejection live only on the submission record.

Old-behavior mapping decisions (reported in MT-TASK-06):
- one claim per worker per task (old UNIQUE(task_id, worker_id)) →
  at most ONE open pending claim per (user, task), enforced in the
  store inside BEGIN IMMEDIATE.
- old behavior had NO repeatable referral campaigns (approval was
  terminal per worker per task), so referral claims are one_time only:
  a repeatable referral cycle identity is not representable by the
  current data model and is rejected server-side instead of guessed.
- task quantity / slot reservation is NOT implemented: "Paid referral
  quantity requires the pending task quantity domain decision."
- complaints/arbitration are DEFERRED: the product has no complaint
  authority workflow; approved/rejected is recorded durably instead.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import db
from task_attempt import TaskAttemptPolicy
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)
from task_submission import SubmissionError, _normalize_idempotency_key
from task_submission_store import SubmissionRecord, TaskSubmissionStore

logger = logging.getLogger(__name__)

# The single documented task type this family handles.  No aliases.
REFERRAL_TASK_TYPE = "referral_task"

# ── Server-side task_data contract (MT-TASK-06) ─────────────────────

REFERRAL_PROVIDER = "telegram"
REFERRAL_ACTION = "referral"
TARGET_KEY = "target"
BOT_USERNAME_KEY = "bot_username"
APPROVER_KEY = "approver"
APPROVER_USER_ID_KEY = "telegram_user_id"

# Strict whitelists: anything else in the definition is ambiguous and
# rejected (including "reward", which lives in tasks.reward only).
ALLOWED_TASK_DATA_KEYS = frozenset(
    {"provider", "action", "target", "approver"}
)
ALLOWED_TARGET_KEYS = frozenset({BOT_USERNAME_KEY})
ALLOWED_APPROVER_KEYS = frozenset({APPROVER_USER_ID_KEY})

# A bot username is a configured Telegram identifier: letters, digits
# and underscore only — never a URL, never an @handle, never a phone
# number or numeric chat id.
MAX_BOT_USERNAME_LENGTH = 64
_BOT_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class ReferralTaskDataError(ValueError):
    """task_data violates the referral_task server-side contract."""


def validate_referral_task_data(task_data: object) -> dict:
    """Validate a parsed referral_task task_data object.

    Enforces provider, action, target and approver — nothing else is
    accepted.

    Raises:
        ReferralTaskDataError: missing/wrong provider, missing/wrong
        action, missing/malformed target, unsafe bot username,
        missing/malformed approver, unexpected keys (including reward),
        or a non-object payload.
    """
    if not isinstance(task_data, dict):
        raise ReferralTaskDataError("task_data must be a JSON object")

    unexpected = set(task_data) - ALLOWED_TASK_DATA_KEYS
    if unexpected:
        if "reward" in unexpected:
            raise ReferralTaskDataError(
                "reward must not appear in task_data (reward lives in "
                "tasks.reward)"
            )
        raise ReferralTaskDataError(
            "unexpected task_data keys: "
            + ", ".join(sorted(str(k) for k in unexpected))
        )

    # ── provider ────────────────────────────────────────────────
    if "provider" not in task_data:
        raise ReferralTaskDataError("missing 'provider' in task_data")
    if task_data["provider"] != REFERRAL_PROVIDER:
        raise ReferralTaskDataError(
            f"'provider' must be '{REFERRAL_PROVIDER}', "
            f"got {task_data['provider']!r}"
        )

    # ── action ──────────────────────────────────────────────────
    if "action" not in task_data:
        raise ReferralTaskDataError("missing 'action' in task_data")
    if task_data["action"] != REFERRAL_ACTION:
        raise ReferralTaskDataError(
            f"'action' must be '{REFERRAL_ACTION}', "
            f"got {task_data['action']!r}"
        )

    # ── target ──────────────────────────────────────────────────
    if "target" not in task_data:
        raise ReferralTaskDataError("missing 'target' in task_data")
    target = task_data["target"]
    if not isinstance(target, dict):
        raise ReferralTaskDataError("'target' must be a JSON object")
    extra_target = set(target) - ALLOWED_TARGET_KEYS
    if extra_target:
        raise ReferralTaskDataError(
            "unexpected target keys: "
            + ", ".join(sorted(str(k) for k in extra_target))
            + " — a target is a bot_username only "
            "(no URLs, links or chat ids)"
        )
    if BOT_USERNAME_KEY not in target:
        raise ReferralTaskDataError(
            "missing bot identifier 'target.bot_username'"
        )
    bot_username = target[BOT_USERNAME_KEY]
    if not isinstance(bot_username, str):
        raise ReferralTaskDataError("'target.bot_username' must be a string")
    if not _BOT_USERNAME_PATTERN.fullmatch(bot_username):
        raise ReferralTaskDataError(
            "'target.bot_username' is not a safe bot username "
            "(letters, digits, underscore only — URLs, @handles and "
            "numeric ids are rejected)"
        )

    # ── approver (buyer/client identity) ────────────────────────
    if APPROVER_KEY not in task_data:
        raise ReferralTaskDataError("missing 'approver' in task_data")
    approver = task_data[APPROVER_KEY]
    if not isinstance(approver, dict):
        raise ReferralTaskDataError("'approver' must be a JSON object")
    extra_approver = set(approver) - ALLOWED_APPROVER_KEYS
    if extra_approver:
        raise ReferralTaskDataError(
            "unexpected approver keys: "
            + ", ".join(sorted(str(k) for k in extra_approver))
        )
    if APPROVER_USER_ID_KEY not in approver:
        raise ReferralTaskDataError(
            "missing buyer identity 'approver.telegram_user_id'"
        )
    approver_id = approver[APPROVER_USER_ID_KEY]
    if isinstance(approver_id, bool) or not isinstance(approver_id, int):
        raise ReferralTaskDataError(
            "'approver.telegram_user_id' must be an integer telegram "
            "user id"
        )
    if approver_id <= 0:
        raise ReferralTaskDataError(
            "'approver.telegram_user_id' must be a positive telegram "
            "user id"
        )

    return task_data


def parse_referral_task_data(raw: object) -> dict:
    """JSON-parse a task row's ``task_data`` column and validate it.

    Raises:
        ReferralTaskDataError: missing/blank/non-string/non-JSON
        payload, or any contract violation.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ReferralTaskDataError("task_data is missing")
    if not isinstance(raw, str):
        raise ReferralTaskDataError("task_data must be a JSON string")
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ReferralTaskDataError(
            f"task_data is not valid JSON: {exc}"
        ) from exc
    return validate_referral_task_data(parsed)


def task_approver_user_id(task: dict | None) -> int | None:
    """Trusted buyer/approver id of a referral_task row, or None.

    Safe non-raising accessor used by the task API to authorize claim
    reads and decisions.  Server-side definition only — client input is
    never involved.
    """
    if not isinstance(task, dict):
        return None
    try:
        data = parse_referral_task_data(task.get("task_data"))
    except ReferralTaskDataError:
        return None
    return data[APPROVER_KEY][APPROVER_USER_ID_KEY]


# ── Referral identity (existing users.referred_by only) ─────────────


def referral_identity(worker_id: int) -> tuple[int, int]:
    """Server-side referral attribution check for *worker_id*.

    Reads ONLY the existing users.referred_by relationship produced by
    the deep-link registration mechanism (first-referrer-wins,
    registration-time self-referral block — both untouched).

    Returns ``(valid_count, self_count)``:
    - ``valid_count``: user rows referred_by the worker that are NOT
      self-attributed — a claimable referral exists iff this > 0.
    - ``self_count``: rows where the user is their own referrer
      (impossible via register_user; detected defensively so a crafted
      row is never counted as a referral).

    Read-only: never writes users, never repairs client claims.
    """
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN user_id = referred_by "
            "THEN 1 ELSE 0 END), 0) AS self_rows "
            "FROM users WHERE referred_by = ?",
            (worker_id,),
        ).fetchone()
    total = row["total"] or 0
    self_rows = row["self_rows"] or 0
    return total - self_rows, self_rows


def worker_claim_state(user_id: int, task_id: int) -> str | None:
    """The worker's own latest claim approval state for this task.

    Safe accessor for the task list API: 'pending' | 'approved' |
    'rejected', or None when no claim exists.  Own state only — never
    another user's data.
    """
    record = TaskSubmissionStore.get_latest_claim(user_id, task_id)
    return record.approval_status if record is not None else None


# ── Outcomes & errors ───────────────────────────────────────────────


class ReferralClaimError(Exception):
    """The worker's claim was rejected before opening a pending claim."""


class ReferralDecisionError(Exception):
    """A buyer decision was rejected (authorization or state)."""


@dataclass(frozen=True)
class ReferralClaimOutcome:
    """Immutable result of a claim submission or a buyer decision.

    Attributes:
        state:  'pending' | 'approved' | 'rejected'.
        submission_id: The claim's task_submissions id (0 if none).
        reason: Internal server-side explanation (never surfaced
                verbatim to unauthorized clients).
    """

    state: str
    submission_id: int = 0
    reason: str = ""


# Claim/outcome states (kept distinct from the four submission
# statuses and from VerificationResult on purpose — pending approval is
# never forced into PASSED/FAILED/ERROR).
STATE_PENDING = "pending"
STATE_APPROVED = "approved"
STATE_REJECTED = "rejected"


def _state_from_approval(approval_status: str | None) -> str:
    if approval_status == db.SUBMISSION_APPROVAL_PENDING:
        return STATE_PENDING
    if approval_status == db.SUBMISSION_APPROVAL_APPROVED:
        return STATE_APPROVED
    if approval_status == db.SUBMISSION_APPROVAL_REJECTED:
        return STATE_REJECTED
    # Unreachable for referral claims (born pending) — defensive.
    raise ReferralClaimError(
        "claim record exists without an approval state"
    )


# ── Worker claim submission ─────────────────────────────────────────


class ReferralClaimService:
    """Opens a buyer-pending referral claim for a started task.

    This service must NOT:
    - complete a task or invoke CompletionGate
    - credit wallets / write ledger entries
    - produce VerificationResult(PASSED) or any completion-authorization
    - read identity or approval from client input
    - touch user_tasks (it stays 'started' throughout)
    """

    @staticmethod
    def submit(
        user_id: int,
        task_id: int,
        idempotency_key: str | None = None,
    ) -> ReferralClaimOutcome:
        """Open (or resolve) the worker's referral claim.

        Returns the claim outcome: PENDING for a freshly opened claim;
        APPROVED/REJECTED when an idempotency key (or an already-open
        claim) resolves to a decided/pending existing claim.

        Raises:
            ReferralClaimError: any pre-condition failure — unknown /
            inactive / non-referral task, state rejections from the
            existing attempt policy, invalid task definition, worker is
            the buyer, repeatable definition, no valid referral
            attribution, or an invalid idempotency key.
        """
        # Shared key normalization with the generic submission path so
        # both enforce identical idempotency-key rules.
        try:
            key = _normalize_idempotency_key(idempotency_key)
        except SubmissionError as exc:
            raise ReferralClaimError(str(exc)) from exc

        # ──1. Task definition (server-side, trusted) ──────────────
        task = db.get_task(task_id)
        if task is None:
            raise ReferralClaimError(f"Task {task_id} not found")
        if task["type"] != REFERRAL_TASK_TYPE:
            raise ReferralClaimError(
                f"Task {task_id} is not a referral task"
            )
        if not task["active"]:
            raise ReferralClaimError(f"Task {task_id} is not active")

        # ── 2. Existing attempt policy (read-only, reused) ────────
        policy = TaskAttemptPolicy.can_submit(user_id, task_id)
        if not policy.allowed:
            raise ReferralClaimError(policy.reason)

        # ── 3. Validated contract → buyer identity ────────────────
        try:
            contract = parse_referral_task_data(task.get("task_data"))
        except ReferralTaskDataError as exc:
            raise ReferralClaimError(
                f"invalid referral task definition: {exc}"
            ) from exc
        approver_id = contract[APPROVER_KEY][APPROVER_USER_ID_KEY]

        # ── 4. Repeat policy: one_time only (MT-TASK-06 report) ───
        # Old behavior had no repeatable referral campaigns and a new
        # legitimate referral-cycle identity is not representable by
        # the current data model — rejected instead of guessed.
        if task.get("repeat_policy") != db.REPEAT_POLICY_ONE_TIME:
            raise ReferralClaimError(
                "repeatable referral tasks are not supported: a new "
                "referral cycle identity is not representable by the "
                "current data model"
            )

        # ── 5. The buyer cannot work their own task (old own_task) ─
        if user_id == approver_id:
            raise ReferralClaimError(
                "cannot claim your own referral task"
            )

        # ── 6. Referral identity from users.referred_by only ──────
        valid, self_rows = referral_identity(user_id)
        if valid <= 0:
            if self_rows > 0:
                raise ReferralClaimError(
                    "self-referral cannot be claimed as a referral"
                )
            raise ReferralClaimError(
                "no referral attribution found for this user"
            )

        # ── 7. Persist the claim (store decides races) ────────────
        # At most ONE open pending claim per (user, task); same key →
        # same record; database UNIQUE + BEGIN IMMEDIATE decide wins.
        record, created = TaskSubmissionStore.create_approval_claim(
            user_id, task_id, key
        )
        state = _state_from_approval(record.approval_status)
        logger.info(
            "Referral claim %s: user=%d task=%d submission=%d key=%s",
            "opened" if created else "resolved",
            user_id, task_id, record.submission_id, key,
        )
        return ReferralClaimOutcome(
            state=state,
            submission_id=record.submission_id,
            reason=(
                "claim opened, awaiting buyer decision"
                if created
                else "existing claim resolved"
            ),
        )


# ── Buyer decision ──────────────────────────────────────────────────


class ReferralApprovalService:
    """The ONLY path from a pending claim to a buyer decision.

    Authorization comes from the server-side task definition
    (task_data.approver.telegram_user_id) compared against the
    caller's verified identity — never from request bodies.

    approve → CAS approval → record 'passed' → CompletionGate
              (→ TaskRewardService inside the gate) — exactly once.
    reject  → CAS approval → record 'failed' — no gate, no reward.
    """

    @staticmethod
    def decide(
        actor_user_id: int,
        task_id: int,
        submission_id: int,
        approve: bool,
    ) -> ReferralClaimOutcome:
        """Apply the authorized buyer's decision to one pending claim.

        Args:
            actor_user_id: the VERIFIED identity of the decider
                (initData-authenticated — never a body field).
            task_id: the referral task the claim belongs to.
            submission_id: the claim (task_submissions id).
            approve: True to approve, False to reject.

        Returns:
            ReferralClaimOutcome with state 'approved' or 'rejected'.
            Repeating an already-applied decision is idempotent.

        Raises:
            ReferralDecisionError: unknown/non-referral task, invalid
            definition, unauthorized decider, self-decision, unknown
            or foreign claim, non-approval-gated claim, conflicting
            decision, or a failed completion on the approve path
            (retrying the same decision re-runs the idempotent
            completion block, healing any crash window).
        """
        # ── 1. Task + contract → who may decide ───────────────────
        task = db.get_task(task_id)
        if task is None:
            raise ReferralDecisionError(f"Task {task_id} not found")
        if task["type"] != REFERRAL_TASK_TYPE:
            raise ReferralDecisionError(
                f"Task {task_id} is not a referral task"
            )
        try:
            contract = parse_referral_task_data(task.get("task_data"))
        except ReferralTaskDataError as exc:
            raise ReferralDecisionError(
                f"invalid referral task definition: {exc}"
            ) from exc
        approver_id = contract[APPROVER_KEY][APPROVER_USER_ID_KEY]

        # ── 2. Authorization: verified actor == definition approver
        #      (never "any authenticated user", never an admin
        #      fallback, never a body-supplied buyer_id.)
        if actor_user_id != approver_id:
            raise ReferralDecisionError(
                "only the task's authorized buyer can decide this claim"
            )

        # ── 3. The claim must exist and belong to this task ───────
        record = TaskSubmissionStore.get_submission(submission_id)
        if record is None or record.task_id != task_id:
            raise ReferralDecisionError("claim not found")
        if record.approval_status is None:
            raise ReferralDecisionError("claim is not approval-gated")
        # Defense in depth: a buyer can never adjudicate their own
        # work (claims from the buyer are already impossible).
        if record.user_id == actor_user_id:
            raise ReferralDecisionError("cannot decide your own claim")

        # ── 4. Decide (CAS on 'pending' decides the single winner) ─
        desired = (
            db.SUBMISSION_APPROVAL_APPROVED
            if approve
            else db.SUBMISSION_APPROVAL_REJECTED
        )
        if record.approval_status == db.SUBMISSION_APPROVAL_PENDING:
            updated = TaskSubmissionStore.mark_approval_decision(
                record.submission_id, desired, actor_user_id
            )
            if updated is not None:
                record = updated
            else:
                # Lost a race to a concurrent decision — refresh and
                # apply the already-decided rules below.
                refreshed = TaskSubmissionStore.get_submission(
                    record.submission_id
                )
                if refreshed is None:
                    raise ReferralDecisionError("claim not found")
                record = refreshed

        # ── 5. Already-decided semantics ──────────────────────────
        if record.approval_status == db.SUBMISSION_APPROVAL_APPROVED:
            if not approve:
                raise ReferralDecisionError("claim already approved")
            return ReferralApprovalService._finalize_approved(
                record, task_id
            )
        if record.approval_status == db.SUBMISSION_APPROVAL_REJECTED:
            if approve:
                raise ReferralDecisionError("claim already rejected")
            return ReferralApprovalService._finalize_rejected(record)

        # Still pending after the CAS attempt: unreachable while
        # BEGIN IMMEDIATE serializes writers — fail closed.
        raise ReferralDecisionError(
            "claim decision could not be recorded"
        )

    # ── Side-effect blocks (idempotent; safe to re-run) ────────────

    @staticmethod
    def _finalize_approved(
        record: SubmissionRecord, task_id: int
    ) -> ReferralClaimOutcome:
        """Approved claim → passed submission → CompletionGate → reward.

        Every step is idempotent: the submission CAS, the gate's
        started→completed compare-and-set, and TaskRewardService's
        ledger idempotency together guarantee exactly one completion
        and exactly one credit — a repeated decision re-runs this
        block without side effects (and heals an interrupted first
        attempt).
        """
        worker_id = record.user_id

        # Submission becomes terminal 'passed' (CAS from 'submitted').
        TaskSubmissionStore.record_verification_result(
            record.submission_id,
            db.SUBMISSION_STATUS_PASSED,
            "referral claim approved by the task buyer",
        )

        # Completion + reward: only while the cycle is 'started';
        # 'completed' means a previous attempt already finished it.
        user_task = db.get_user_task(worker_id, task_id)
        if user_task is None:
            raise ReferralDecisionError("claim has no task cycle")
        if user_task["status"] == db.USER_TASK_STATUS_STARTED:
            try:
                CompletionGate().complete(
                    worker_id,
                    task_id,
                    VerificationResult(status=VerificationStatus.PASSED),
                )
            except CompletionGateError:
                latest = db.get_user_task(worker_id, task_id)
                if (
                    latest is None
                    or latest["status"] != db.USER_TASK_STATUS_COMPLETED
                ):
                    raise
                # Already completed by a concurrent/retried decision —
                # exactly-once semantics hold; fall through to stamping.
        elif user_task["status"] != db.USER_TASK_STATUS_COMPLETED:
            raise ReferralDecisionError(
                f"claim task cycle is not completable "
                f"(current: {user_task['status']})"
            )

        # Tie the claim to the completion transition it produced.
        try:
            TaskSubmissionStore.stamp_completion(
                worker_id, task_id, record.idempotency_key
            )
        except Exception:  # pragma: no cover - stamping is log-only
            logger.exception(
                "Failed to stamp referral claim completion: "
                "user=%d task=%d",
                worker_id, task_id,
            )

        logger.info(
            "Referral claim approved: submission=%d worker=%d task=%d",
            record.submission_id, worker_id, task_id,
        )
        return ReferralClaimOutcome(
            state=STATE_APPROVED,
            submission_id=record.submission_id,
            reason="approved by the task buyer",
        )

    @staticmethod
    def _finalize_rejected(record: SubmissionRecord) -> ReferralClaimOutcome:
        """Rejected claim → failed submission.  No completion, no reward,
        user_task untouched (stays 'started' so the worker may retry
        with a fresh claim — mirroring the old repo's post-rejection
        re-claim behavior).  The rejection is durable."""
        TaskSubmissionStore.record_verification_result(
            record.submission_id,
            db.SUBMISSION_STATUS_FAILED,
            "referral claim rejected by the task buyer",
        )
        logger.info(
            "Referral claim rejected: submission=%d worker=%d task=%d",
            record.submission_id, record.user_id, record.task_id,
        )
        return ReferralClaimOutcome(
            state=STATE_REJECTED,
            submission_id=record.submission_id,
            reason="rejected by the task buyer",
        )
