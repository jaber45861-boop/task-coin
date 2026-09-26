"""
Manual / Social Proof Task (MT-TASK-15)
=======================================

The manual/social-proof task family: the worker submits a bounded
text/URL proof reference, and ONLY the task's server-defined approver
may approve or reject it.  Built on the existing referral approval
architecture (MT-TASK-06) as the model — same submission-layer approval
state, same CAS decision, same CompletionGate → TaskRewardService
settlement.  No photo/file storage: the proof is a bounded text/URL
reference only.

Task type:
    manual  (distinct from channel_subscription, telegram_channel
             and referral_task)

Task data contract (server-set, trusted, validated on every read):

    {
        "provider": "telegram",
        "action": "proof",
        "approver": {
            "telegram_user_id": <reviewer telegram user id>
        }
    }

`approver.telegram_user_id` is the ONLY reviewer authorization in this
family: decisions compare the verified caller identity against the
definition — never arbitrary authenticated users, never an admin
fallback, never a body-supplied id.  Reward is NOT part of task_data —
it stays in tasks.reward.

proof_ref (worker-supplied, untrusted):
    - a bounded text/URL reference only (string, non-empty after
      strip, at most MAX_PROOF_REF_LENGTH chars, no control chars)
    - persisted ONLY through TaskSubmissionStore.proof_ref
    - NEVER trusted for identity, authorization, reward, task or user
      identity — those all come from the server-side definition and
      the verified caller

Pending approval lives at the SUBMISSION layer (never on user_tasks):

    worker ─► TaskStartGate ─► started
    worker ─► ManualProofService.submit
              ─► task_submissions row born with
                 status='submitted' + approval_status='pending'
                 (+ proof_ref)  ─► PENDING outcome
                 (NO VerificationResult, NO completion, NO reward)
                 + fresh claim → admin inbox notification
                   (MT-ADMIN-03: ADMINS private chat, fail-soft)
    reviewer ─► ManualReviewService.decide (authorized approver only)
              approve ─► CAS approval ─► record 'passed'
                        ─► CompletionGate ─► TaskRewardService
              reject  ─► CAS approval ─► record 'failed'
                        ─► NO CompletionGate, NO reward
                        ─► user_task stays 'started' (retry allowed)

Only ONE pending approval exists per (user, task) — enforced by
TaskSubmissionStore.create_approval_claim inside BEGIN IMMEDIATE.
"""

from __future__ import annotations

import json
import logging
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
from task_taxonomy import (  # MT-ADMIN-05 shared generic contract vocabulary
    GENERIC_PROVIDER_SET,
    MANUAL_TASK_ACTIONS,
    MAX_TARGET_LABEL_LENGTH,
    MAX_TARGET_REF_LENGTH,
    has_unsafe_control_chars,
)

logger = logging.getLogger(__name__)

# The single documented task type this family handles.  No aliases.
MANUAL_TASK_TYPE = "manual"

# ── Server-side task_data contract (MT-TASK-15) ─────────────────────
#
# MT-ADMIN-05 (smallest additive extension): the contract gained an
# OPTIONAL generic ``target`` (``ref`` + optional ``label``) so a
# wizard-created manual task can carry its platform target, and the
# provider/action checks widened from equality with the legacy
# ``telegram``/``proof`` pair to the shared stable whitelists in
# ``task_taxonomy``.  Legacy MT-TASK-15 payloads keep validating
# unchanged; the referral action stays rejected (it belongs to the
# MT-TASK-06 referral family); ``reward`` stays forbidden (it lives in
# tasks.reward only).  Authorization semantics are untouched:
# ``approver.telegram_user_id`` remains the ONLY decision authority.

MANUAL_PROVIDER = "telegram"
MANUAL_ACTION = "proof"
APPROVER_KEY = "approver"
APPROVER_USER_ID_KEY = "telegram_user_id"

# Strict whitelists: anything else in the definition is ambiguous and
# rejected (including "reward", which lives in tasks.reward only).
ALLOWED_TASK_DATA_KEYS = frozenset(
    {"provider", "action", "approver", "target"}
)
ALLOWED_APPROVER_KEYS = frozenset({APPROVER_USER_ID_KEY})
ALLOWED_TARGET_KEYS = frozenset({"ref", "label"})

# ── Proof bounds (MT-TASK-15) ────────────────────────────────────────

# A proof is a bounded text/URL reference — not a file, not a photo,
# not a payload.
MAX_PROOF_REF_LENGTH = 500


class ManualTaskDataError(ValueError):
    """task_data violates the manual task server-side contract."""


def validate_manual_task_data(task_data: object) -> dict:
    """Validate a parsed manual task_data object.

    Enforces provider, action and approver — nothing else is accepted
    (plus the MT-ADMIN-05 optional generic ``target``).  The provider
    must be one of the platform's stable provider identifiers and the
    action one of the manual-family actions — this is how a generic
    wizard task (e.g. ``instagram`` / ``follow``) stays representable
    without weakening the strict whitelist (``web``, ``nope``,
    ``referral`` and friends remain rejected).

    Raises:
        ManualTaskDataError: missing/wrong provider, missing/wrong
        action, missing/malformed approver, malformed optional target,
        unexpected keys (including reward), or a non-object payload.
    """
    if not isinstance(task_data, dict):
        raise ManualTaskDataError("task_data must be a JSON object")

    unexpected = set(task_data) - ALLOWED_TASK_DATA_KEYS
    if unexpected:
        if "reward" in unexpected:
            raise ManualTaskDataError(
                "reward must not appear in task_data (reward lives in "
                "tasks.reward)"
            )
        raise ManualTaskDataError(
            "unexpected task_data keys: "
            + ", ".join(sorted(str(k) for k in unexpected))
        )

    # ── provider ────────────────────────────────────────────────
    if "provider" not in task_data:
        raise ManualTaskDataError("missing 'provider' in task_data")
    provider = task_data["provider"]
    if not isinstance(provider, str) or provider not in GENERIC_PROVIDER_SET:
        raise ManualTaskDataError(
            f"'provider' must be one of the supported provider "
            f"identifiers, got {provider!r}"
        )

    # ── action ──────────────────────────────────────────────────
    if "action" not in task_data:
        raise ManualTaskDataError("missing 'action' in task_data")
    action = task_data["action"]
    if not isinstance(action, str) or action not in MANUAL_TASK_ACTIONS:
        raise ManualTaskDataError(
            f"'action' must be a supported manual action identifier, "
            f"got {action!r}"
        )

    # ── approver (reviewer identity) ────────────────────────────
    if APPROVER_KEY not in task_data:
        raise ManualTaskDataError("missing 'approver' in task_data")
    approver = task_data[APPROVER_KEY]
    if not isinstance(approver, dict):
        raise ManualTaskDataError("'approver' must be a JSON object")
    extra_approver = set(approver) - ALLOWED_APPROVER_KEYS
    if extra_approver:
        raise ManualTaskDataError(
            "unexpected approver keys: "
            + ", ".join(sorted(str(k) for k in extra_approver))
        )
    if APPROVER_USER_ID_KEY not in approver:
        raise ManualTaskDataError(
            "missing reviewer identity 'approver.telegram_user_id'"
        )
    approver_id = approver[APPROVER_USER_ID_KEY]
    if isinstance(approver_id, bool) or not isinstance(approver_id, int):
        raise ManualTaskDataError(
            "'approver.telegram_user_id' must be an integer telegram "
            "user id"
        )
    if approver_id <= 0:
        raise ManualTaskDataError(
            "'approver.telegram_user_id' must be a positive telegram "
            "user id"
        )

    # ── target (MT-ADMIN-05: optional generic platform target) ───
    # Legacy MT-TASK-15 payloads have no target and keep validating;
    # when present it is bounded display/data only — never executable,
    # never a credential, never a reward.
    if "target" in task_data:
        target = task_data["target"]
        if not isinstance(target, dict):
            raise ManualTaskDataError("'target' must be a JSON object")
        extra_target = set(target) - ALLOWED_TARGET_KEYS
        if extra_target:
            raise ManualTaskDataError(
                "unexpected target keys: "
                + ", ".join(sorted(str(k) for k in extra_target))
            )
        if "ref" not in target:
            raise ManualTaskDataError("missing 'target.ref'")
        ref = target["ref"]
        if not isinstance(ref, str) or not ref.strip():
            raise ManualTaskDataError("'target.ref' must be a non-empty string")
        if len(ref) > MAX_TARGET_REF_LENGTH:
            raise ManualTaskDataError(
                f"'target.ref' exceeds {MAX_TARGET_REF_LENGTH} characters"
            )
        if has_unsafe_control_chars(ref, allow_newlines=False):
            raise ManualTaskDataError(
                "'target.ref' contains unsafe control characters"
            )
        if "label" in target:
            label = target["label"]
            if not isinstance(label, str) or not label.strip():
                raise ManualTaskDataError(
                    "'target.label' must be a non-empty string"
                )
            if len(label) > MAX_TARGET_LABEL_LENGTH:
                raise ManualTaskDataError(
                    f"'target.label' exceeds "
                    f"{MAX_TARGET_LABEL_LENGTH} characters"
                )
            if has_unsafe_control_chars(label, allow_newlines=False):
                raise ManualTaskDataError(
                    "'target.label' contains unsafe control characters"
                )

    return task_data


def parse_manual_task_data(raw: object) -> dict:
    """JSON-parse a task row's ``task_data`` column and validate it.

    Raises:
        ManualTaskDataError: missing/blank/non-string/non-JSON
        payload, or any contract violation.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ManualTaskDataError("task_data is missing")
    if not isinstance(raw, str):
        raise ManualTaskDataError("task_data must be a JSON string")
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ManualTaskDataError(
            f"task_data is not valid JSON: {exc}"
        ) from exc
    return validate_manual_task_data(parsed)


def manual_task_approver_user_id(task: dict | None) -> int | None:
    """Trusted reviewer id of a manual task row, or None.

    Safe non-raising accessor used by the task API to authorize claim
    reads and decisions.  Server-side definition only — client input is
    never involved.
    """
    if not isinstance(task, dict):
        return None
    try:
        data = parse_manual_task_data(task.get("task_data"))
    except ManualTaskDataError:
        return None
    return data[APPROVER_KEY][APPROVER_USER_ID_KEY]


def worker_awaiting_decision(user_id: int, task_id: int) -> bool:
    """True while the worker's own manual claim awaits a decision.

    The narrow worker-facing state for the task list API (mirrors the
    referral `awaiting_decision` boolean).  Own state only — store-level
    read, no task_data, no approver data, no other user's data.
    """
    record = TaskSubmissionStore.get_latest_claim(user_id, task_id)
    return (
        record is not None
        and record.approval_status == db.SUBMISSION_APPROVAL_PENDING
    )


# ── Proof validation (bounded text/URL reference only) ──────────────


class ManualProofError(Exception):
    """The proof submission was rejected before opening a claim."""


def normalize_proof_ref(proof_ref: object) -> str:
    """Validate and normalize an untrusted proof reference.

    A proof is a bounded text/URL reference ONLY — no files, no photos,
    no multi-line payloads.  Returns the stripped reference.

    Raises:
        ManualProofError: non-string, empty/whitespace-only, longer
        than MAX_PROOF_REF_LENGTH, or containing control characters.
    """
    if not isinstance(proof_ref, str):
        raise ManualProofError("proof_ref must be a string")
    proof = proof_ref.strip()
    if not proof:
        raise ManualProofError("proof_ref must not be empty")
    if len(proof) > MAX_PROOF_REF_LENGTH:
        raise ManualProofError(
            f"proof_ref exceeds {MAX_PROOF_REF_LENGTH} characters"
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in proof):
        raise ManualProofError(
            "proof_ref must not contain control characters"
        )
    return proof


# ── Outcomes & errors ───────────────────────────────────────────────


class ManualDecisionError(Exception):
    """A reviewer decision was rejected (authorization or state)."""


@dataclass(frozen=True)
class ManualProofOutcome:
    """Immutable result of a proof submission or a reviewer decision.

    Attributes:
        state:  'pending' | 'approved' | 'rejected'.
        submission_id: The claim's task_submissions id (0 if none).
        reason: Internal server-side explanation (never surfaced
                verbatim to unauthorized clients).
    """

    state: str
    submission_id: int = 0
    reason: str = ""


# Claim/outcome states — pending approval is never forced into
# PASSED/FAILED/ERROR (same vocabulary as the referral family).
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
    # Unreachable for manual claims (born pending) — defensive.
    raise ManualProofError(
        "claim record exists without an approval state"
    )


# ── Worker proof submission ─────────────────────────────────────────


class ManualProofService:
    """Opens a reviewer-pending manual proof claim for a started task.

    This service must NOT:
    - complete a task or invoke CompletionGate
    - credit wallets / write ledger entries
    - produce VerificationResult(PASSED) or any completion-authorization
    - read identity, authorization or reward from the proof reference
    - touch user_tasks (it stays 'started' throughout)
    - decide the claim (MT-ADMIN-03 only schedules an admin-inbox
      NOTIFICATION for a freshly opened claim — fail-soft, never on
      idempotent replays — and all decision authority stays with
      ManualReviewService.decide)
    """

    @staticmethod
    def submit(
        user_id: int,
        task_id: int,
        proof_ref: object,
        idempotency_key: str | None = None,
    ) -> ManualProofOutcome:
        """Open (or resolve) the worker's manual proof claim.

        Returns the claim outcome: PENDING for a freshly opened claim;
        APPROVED/REJECTED when an idempotency key (or an already-open
        claim) resolves to a decided/pending existing claim.

        Raises:
            ManualProofError: any pre-condition failure — invalid
            idempotency key, invalid proof reference, unknown /
            inactive / non-manual task, state rejections from the
            existing attempt policy, invalid task definition, or the
            approver submitting their own task.
        """
        # Shared key normalization with the generic submission path so
        # both enforce identical idempotency-key rules.
        try:
            key = _normalize_idempotency_key(idempotency_key)
        except SubmissionError as exc:
            raise ManualProofError(str(exc)) from exc

        # ── Proof: bounded text/URL reference, untrusted ─────────
        proof = normalize_proof_ref(proof_ref)

        # ── 1. Task definition (server-side, trusted) ────────────
        task = db.get_task(task_id)
        if task is None:
            raise ManualProofError(f"Task {task_id} not found")
        if task["type"] != MANUAL_TASK_TYPE:
            raise ManualProofError(
                f"Task {task_id} is not a manual task"
            )
        if not task["active"]:
            raise ManualProofError(f"Task {task_id} is not active")

        # ── 2. Existing attempt policy (read-only, reused) ───────
        policy = TaskAttemptPolicy.can_submit(user_id, task_id)
        if not policy.allowed:
            raise ManualProofError(policy.reason)

        # ── 3. Validated contract → reviewer identity ────────────
        try:
            contract = parse_manual_task_data(task.get("task_data"))
        except ManualTaskDataError as exc:
            raise ManualProofError(
                f"invalid manual task definition: {exc}"
            ) from exc
        approver_id = contract[APPROVER_KEY][APPROVER_USER_ID_KEY]

        # ── 4. The reviewer cannot review their own proof ────────
        if user_id == approver_id:
            raise ManualProofError(
                "cannot submit your own manual task"
            )

        # ── 5. Persist the claim (store decides races) ───────────
        # At most ONE open pending claim per (user, task); same key →
        # same record; database UNIQUE + BEGIN IMMEDIATE decide wins.
        record, created = TaskSubmissionStore.create_approval_claim(
            user_id, task_id, key, proof_ref=proof
        )
        state = _state_from_approval(record.approval_status)
        logger.info(
            "Manual proof claim %s: user=%d task=%d submission=%d key=%s",
            "opened" if created else "resolved",
            user_id, task_id, record.submission_id, key,
        )

        # ── MT-ADMIN-03: notify the admin private-chat inbox ──────
        # ONLY a freshly opened pending claim notifies; an idempotent
        # replay (created=False) resolves the existing claim and never
        # sends a duplicate notification.  Fail-soft by design: the
        # claim is already durable, so a notification/transport
        # failure must never fail the worker's submission.  The late
        # import avoids a module cycle (the inbox imports this
        # module's review services).
        if created and state == STATE_PENDING:
            try:
                from manual_proof_inbox import (
                    schedule_pending_notification,
                )
                schedule_pending_notification(task, record)
            except Exception:
                logger.exception(
                    "Admin inbox notification failed for claim %d "
                    "(submission preserved)",
                    record.submission_id,
                )

        return ManualProofOutcome(
            state=state,
            submission_id=record.submission_id,
            reason=(
                "proof submitted, awaiting reviewer decision"
                if created
                else "existing claim resolved"
            ),
        )


# ── Reviewer decision ───────────────────────────────────────────────


class ManualReviewService:
    """The ONLY path from a pending claim to a reviewer decision.

    Authorization comes from the server-side task definition
    (task_data.approver.telegram_user_id) compared against the
    caller's verified identity — never from request bodies, never an
    admin fallback, never "any authenticated user".

    approve → CAS approval → record 'passed' → CompletionGate
              (→ TaskRewardService inside the gate) — exactly once.
    reject  → CAS approval → record 'failed' — no gate, no reward,
              user_task stays 'started' (the worker may retry with a
              new submission).
    """

    @staticmethod
    def decide(
        actor_user_id: int,
        task_id: int,
        submission_id: int,
        approve: bool,
    ) -> ManualProofOutcome:
        """Apply the authorized reviewer's decision to one pending claim.

        Args:
            actor_user_id: the VERIFIED identity of the decider
                (initData-authenticated — never a body field).
            task_id: the manual task the claim belongs to.
            submission_id: the claim (task_submissions id).
            approve: True to approve, False to reject.

        Returns:
            ManualProofOutcome with state 'approved' or 'rejected'.
            Repeating an already-applied decision is idempotent.

        Raises:
            ManualDecisionError: unknown/non-manual task, invalid
            definition, unauthorized decider, self-decision, unknown
            or foreign claim, non-approval-gated claim, conflicting
            decision, or a failed completion on the approve path
            (retrying the same decision re-runs the idempotent
            completion block, healing any crash window).
        """
        # ── 1. Task + contract → who may decide ───────────────────
        task = db.get_task(task_id)
        if task is None:
            raise ManualDecisionError(f"Task {task_id} not found")
        if task["type"] != MANUAL_TASK_TYPE:
            raise ManualDecisionError(
                f"Task {task_id} is not a manual task"
            )
        try:
            contract = parse_manual_task_data(task.get("task_data"))
        except ManualTaskDataError as exc:
            raise ManualDecisionError(
                f"invalid manual task definition: {exc}"
            ) from exc
        approver_id = contract[APPROVER_KEY][APPROVER_USER_ID_KEY]

        # ── 2. Authorization: verified actor == definition approver
        #      (never "any authenticated user", never an admin
        #      fallback, never a body-supplied approver id.)
        if actor_user_id != approver_id:
            raise ManualDecisionError(
                "only the task's authorized approver can decide "
                "this claim"
            )

        # ── 3. The claim must exist and belong to this task ───────
        record = TaskSubmissionStore.get_submission(submission_id)
        if record is None or record.task_id != task_id:
            raise ManualDecisionError("claim not found")
        if record.approval_status is None:
            raise ManualDecisionError("claim is not approval-gated")
        # Defense in depth: a reviewer can never adjudicate their own
        # work (claims from the approver are already impossible).
        if record.user_id == actor_user_id:
            raise ManualDecisionError("cannot decide your own claim")

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
                    raise ManualDecisionError("claim not found")
                record = refreshed

        # ── 5. Already-decided semantics ──────────────────────────
        if record.approval_status == db.SUBMISSION_APPROVAL_APPROVED:
            if not approve:
                raise ManualDecisionError("claim already approved")
            return ManualReviewService._finalize_approved(
                record, task_id
            )
        if record.approval_status == db.SUBMISSION_APPROVAL_REJECTED:
            if approve:
                raise ManualDecisionError("claim already rejected")
            return ManualReviewService._finalize_rejected(record)

        # Still pending after the CAS attempt: unreachable while
        # BEGIN IMMEDIATE serializes writers — fail closed.
        raise ManualDecisionError(
            "claim decision could not be recorded"
        )

    # ── Side-effect blocks (idempotent; safe to re-run) ────────────

    @staticmethod
    def _finalize_approved(
        record: SubmissionRecord, task_id: int
    ) -> ManualProofOutcome:
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
            "manual proof approved by the task approver",
        )

        # Completion + reward: only while the cycle is 'started';
        # 'completed' means a previous attempt already finished it.
        user_task = db.get_user_task(worker_id, task_id)
        if user_task is None:
            raise ManualDecisionError("claim has no task cycle")
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
            raise ManualDecisionError(
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
                "Failed to stamp manual claim completion: "
                "user=%d task=%d",
                worker_id, task_id,
            )

        logger.info(
            "Manual proof approved: submission=%d worker=%d task=%d",
            record.submission_id, worker_id, task_id,
        )
        return ManualProofOutcome(
            state=STATE_APPROVED,
            submission_id=record.submission_id,
            reason="approved by the task approver",
        )

    @staticmethod
    def _finalize_rejected(record: SubmissionRecord) -> ManualProofOutcome:
        """Rejected claim → failed submission.  No completion, no reward,
        user_task untouched (stays 'started' so the worker may retry
        with a fresh claim and new proof).  The rejection is durable."""
        TaskSubmissionStore.record_verification_result(
            record.submission_id,
            db.SUBMISSION_STATUS_FAILED,
            "manual proof rejected by the task approver",
        )
        logger.info(
            "Manual proof rejected: submission=%d worker=%d task=%d",
            record.submission_id, record.user_id, record.task_id,
        )
        return ManualProofOutcome(
            state=STATE_REJECTED,
            submission_id=record.submission_id,
            reason="rejected by the task approver",
        )
