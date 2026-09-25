"""
Mini App Task API (MT-TASK-03)
==============================

Production HTTP wiring between the Tasks page of the Mini App and the
existing Task pipeline.  Registered on both existing Mini App servers
(``serve_miniapp.py`` and the WispByte single-entry ``bot.py`` app) —
no second web server is created.

Endpoints (all under ``/api/tasks``):

- ``GET  /api/tasks``                    active catalog + the caller's status
- ``POST /api/tasks/<task_id>/start``    TaskLifecycle → TaskStartGate
- ``POST /api/tasks/<task_id>/submit``   TaskLifecycle → CompletionBridge
                                         (AttemptPolicy → SubmissionService
                                          → ChannelTaskVerifier →
                                          CompletionGate on PASSED only)
                                         referral tasks dispatch to the
                                         approval-gated claim path instead
                                         (MT-TASK-06 — see below)

Paid referral extension (MT-TASK-06, minimal):

- ``GET  /api/tasks/<task_id>/claims``         pending claims, only for
                                               the task's server-defined
                                               buyer/approver
- ``POST /api/tasks/<task_id>/claims/<sid>/decision``  approve|reject by
                                               the verified buyer identity

Referral claims never complete on submission: they open a pending
claim at the submission layer, and only the authorized buyer's
approve decision reaches CompletionGate → TaskRewardService.  The
worker-facing API carries no approval capability of any kind.

Manual/social-proof extension (MT-TASK-15, minimal):

- ``POST /api/tasks/<task_id>/submit`` dispatches ``manual`` tasks to
  ``ManualProofService.submit`` — a pending claim carrying a bounded
  text/URL ``proof_ref`` (no photo/file storage, never trusted for
  identity, authorization, reward or task selection)
- the claims + decision endpoints above also serve ``manual`` tasks,
  authorized ONLY by task_data.approver.telegram_user_id (no admin
  fallback, no arbitrary authenticated user)

Idempotency (MT-TASK-04): the submit endpoint accepts the standard
``Idempotency-Key`` header.  The key is validated here and enforced by
the database — a repeated key returns the original submission result
without re-verifying.  When the header is absent the server generates
a key, so every submission is still uniquely identified.

Security rules enforced here:

- every endpoint authenticates the Telegram Mini App user via the
  existing ``miniapp_auth`` initData verification — a browser-supplied
  ``user_id`` (body, query or header) is never trusted as identity
- the HTTP layer NEVER mutates ``user_tasks`` itself: the start route
  only calls ``TaskLifecycle.start_task`` and the submit route only
  calls ``TaskLifecycle.submit_task``; ``CompletionGate`` is never
  imported here
- responses expose only safe presentation fields (id, title,
  description, type, reward, status) plus, for ``channel_subscription``
  and ``telegram_channel`` tasks, the public ``https://t.me/<username>``
  join destination resolved server-side from the trusted task
  definition — never raw ``task_data``, never numeric channel ids,
  never verifier internals
- errors are distinguished with stable machine codes and concise
  Arabic user-facing messages; internal exception details and stack
  traces never reach the client
- no reward, wallet, ledger or balance behaviour exists in this module:
  ``tasks.reward`` remains display metadata only
"""

import json
import logging
import os

from flask import Blueprint, jsonify, request

import db
import miniapp_auth
from config import get_channel
from channel_task_verifier import CHANNEL_TASK_TYPE
from telegram_channel_task_verifier import (
    TELEGRAM_CHANNEL_TASK_TYPE,
    task_channel_slug,
)
from referral_task import (
    REFERRAL_TASK_TYPE,
    ReferralApprovalService,
    ReferralClaimError,
    ReferralClaimService,
    ReferralDecisionError,
    task_approver_user_id,
    worker_claim_state,
)
from manual_task import (
    MANUAL_TASK_TYPE,
    ManualDecisionError,
    ManualProofError,
    ManualProofService,
    ManualReviewService,
    manual_task_approver_user_id,
    worker_awaiting_decision,
)
from task_catalog import TaskCatalog
from task_lifecycle import TaskLifecycle
from task_start import StartGateError
from task_submission import FORBIDDEN_FIELDS
from task_submission_store import TaskSubmissionStore

logger = logging.getLogger(__name__)

tasks_bp = Blueprint("tasks", __name__)

# Telegram initData carrier for XHR calls (kept out of URLs/logs).
INIT_DATA_HEADER = "X-Telegram-Init-Data"
# Also accepted as a query parameter, mirroring the social routes.
INIT_DATA_QUERY = "init_data"
# Standard idempotency header for submissions (MT-TASK-04).
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
MAX_IDEMPOTENCY_KEY_LENGTH = 128
_IDEMPOTENCY_KEY_CHARS = set("-_.,:")

# Only these task statuses exist (terminal "completed" included).
_STATUS_AVAILABLE = db.USER_TASK_STATUS_AVAILABLE
_STATUS_STARTED = db.USER_TASK_STATUS_STARTED
_STATUS_COMPLETED = db.USER_TASK_STATUS_COMPLETED

# ── Arabic user-facing messages (concise, no internal details) ────────

_MSG_UNAUTHENTICATED = "افتح التطبيق من تيليجرام أولاً"
_MSG_SERVER = "حدث خطأ غير متوقع، حاول مرة أخرى"
_MSG_INVALID_REQUEST = "الطلب غير صالح"
_MSG_TASK_NOT_FOUND = "المهمة غير موجودة"
_MSG_TASK_INACTIVE = "المهمة غير متاحة حالياً"
_MSG_NOT_AVAILABLE = "هذه المهمة غير متاحة لك"
_MSG_ALREADY_STARTED = "بدأت هذه المهمة مسبقاً"
_MSG_ALREADY_COMPLETED = "لقد أكملت هذه المهمة مسبقاً"
_MSG_NOT_STARTED = "يجب بدء المهمة أولاً"
_MSG_INVALID_STATE = "لا يمكن تنفيذ هذا الإجراء في الوقت الحالي"
_MSG_INVALID_SUBMISSION = "بيانات الإرسال غير صالحة"
_MSG_INVALID_IDEMPOTENCY_KEY = "مفتاح الطلب غير صالح"
_MSG_SUBMISSION_IN_PROGRESS = "جارٍ التحقق من محاولة سابقة، حاول بعد قليل"
_MSG_VERIFICATION_FAILED = (
    "لم يتم تأكيد إنجاز المهمة، تأكد من اشتراكك ثم أعد المحاولة"
)
_MSG_VERIFICATION_ERROR = "تعذر التحقق حالياً، حاول مرة أخرى لاحقاً"

# Paid referral claims (MT-TASK-06)
_MSG_CLAIM_RECEIVED = "تم استلام طلبك، بانتظار موافقة العميل"
_MSG_CLAIM_REJECTED = "تم رفض الطلب، يمكنك إعادة المحاولة"
_MSG_NO_REFERRAL = "لا توجد إحالة صالحة مرتبطة بحسابك"
_MSG_OWN_TASK = "لا يمكنك تنفيذ مهمة تعود لك"
_MSG_CLAIM_NOT_REPEATABLE = "هذه المهمة غير متاحة للتقديم الحالي"
_MSG_NOT_APPROVER = "ليست لديك صلاحية اتخاذ هذا القرار"
_MSG_CLAIM_NOT_FOUND = "الطلب غير موجود"
_MSG_ALREADY_CLAIM_APPROVED = "تمت الموافقة على هذا الطلب مسبقاً"
_MSG_ALREADY_CLAIM_REJECTED = "تم رفض هذا الطلب مسبقاً"
_MSG_INVALID_DECISION = "القرار غير صالح"
_MSG_APPROVED_OK = "تمت الموافقة على الطلب"
_MSG_REJECTED_OK = "تم رفض الطلب"

# Manual/social-proof claims (MT-TASK-15)
_MSG_PROOF_RECEIVED = "تم استلام إثباتك، بانتظار مراجعة المشرف"
_MSG_INVALID_PROOF = "الإثبات غير صالح، أرسل رابطاً أو نصاً واضحاً"

_MSG_STARTED_OK = "تم بدء المهمة"
_MSG_COMPLETED_OK = "تم إنجاز المهمة بنجاح"


# ── Authentication (existing miniapp_auth initData validation) ────────


def _get_init_data() -> str | None:
    value = request.headers.get(INIT_DATA_HEADER)
    if value:
        return value
    value = request.args.get(INIT_DATA_QUERY)
    if value:
        return value
    return None


def _authenticate() -> dict | None:
    """Verify the Telegram Mini App user; ``None`` when untrusted.

    Uses the existing ``miniapp_auth`` HMAC validation — the caller's
    identity comes only from cryptographically verified initData.
    """
    init_data = _get_init_data()
    if not init_data:
        return None
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not configured")
        return None
    is_valid, user, _error = miniapp_auth.validate_init_data(
        init_data, bot_token
    )
    if not is_valid or not user:
        logger.info("Task endpoint rejected unauthenticated request")
        return None
    return user


def _ensure_user(user: dict) -> int:
    """Make sure the verified Telegram user exists as a users row."""
    user_id = int(user["user_id"])
    if db.get_user(user_id) is None:
        db.register_user(
            user_id=user_id,
            username=user.get("username"),
            first_name=user.get("first_name"),
            referred_by=None,
        )
    return user_id


# ── Response helpers ──────────────────────────────────────────────────


def _error(code: str, message: str, http_status: int, **extra):
    payload = {"ok": False, "error": code, "message": message}
    payload.update(extra)
    return jsonify(payload), http_status


def _unauthenticated():
    return _error("unauthenticated", _MSG_UNAUTHENTICATED, 401)


def _server_error():
    return _error("server_error", _MSG_SERVER, 500)


def _current_status(user_id: int, task_id: int) -> str:
    row = db.get_user_task(user_id, task_id)
    if row is None:
        return _STATUS_AVAILABLE
    return row["status"]


# ── Domain-error mapping (stable codes → Arabic messages) ─────────────


def _start_error(message: str):
    """Translate a StartGateError message into a safe API error."""
    lowered = message.lower()
    if "not found" in lowered:
        return _error("task_not_found", _MSG_TASK_NOT_FOUND, 404)
    if "not active" in lowered:
        return _error("task_inactive", _MSG_TASK_INACTIVE, 404)
    if "already completed" in lowered:
        return _error(
            "task_already_completed", _MSG_ALREADY_COMPLETED, 409
        )
    if "already started" in lowered:
        return _error("task_already_started", _MSG_ALREADY_STARTED, 409)
    return _error("invalid_task_state", _MSG_INVALID_STATE, 409)


def _pipeline_state_error(reason: str):
    """Map submission-pipeline rejections to safe API errors.

    Returns ``None`` when *reason* is not a state/validation rejection,
    so the caller can treat it as a verification outcome instead.
    """
    lowered = reason.lower()
    if "forbidden fields" in lowered or "actual_data must be a dict" in lowered:
        return _error("invalid_submission", _MSG_INVALID_SUBMISSION, 400)
    if "invalid idempotency key" in lowered:
        return _error(
            "invalid_idempotency_key", _MSG_INVALID_IDEMPOTENCY_KEY, 400
        )
    if "in progress" in lowered:
        return _error(
            "submission_in_progress", _MSG_SUBMISSION_IN_PROGRESS, 409
        )
    if "no user_task record" in lowered:
        return _error("task_not_available", _MSG_NOT_AVAILABLE, 409)
    if "not found" in lowered:
        return _error("task_not_found", _MSG_TASK_NOT_FOUND, 404)
    if "not active" in lowered:
        return _error("task_inactive", _MSG_TASK_INACTIVE, 404)
    if "already completed" in lowered or "current: completed" in lowered:
        return _error(
            "task_already_completed",
            _MSG_ALREADY_COMPLETED,
            409,
            status=_STATUS_COMPLETED,
        )
    if "not in started state" in lowered:
        if "current: available" in lowered:
            return _error(
                "task_not_started",
                _MSG_NOT_STARTED,
                409,
                status=_STATUS_AVAILABLE,
            )
        return _error("invalid_task_state", _MSG_INVALID_STATE, 409)
    return None


def _safe_join_url(task_id: int, task_type: str) -> str | None:
    """Public join destination for a channel task, or ``None``.

    Narrow scope: only ``channel_subscription`` and
    ``telegram_channel`` tasks, only the configured channel's public
    ``@username`` rendered as a ``https://t.me/<username>`` link.  The
    slug always comes from the trusted server-side task definition —
    the browser never chooses the target.  Numeric channel ids, slugs,
    task_data and admin fields are never returned.
    """
    if task_type == CHANNEL_TASK_TYPE:
        task = db.get_task(task_id)
        if task is None:
            return None
        raw = task.get("task_data") or ""
        try:
            task_data = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            return None
        if not isinstance(task_data, dict):
            return None
        slug = task_data.get("channel_slug")
    elif task_type == TELEGRAM_CHANNEL_TASK_TYPE:
        task = db.get_task(task_id)
        if task is None:
            return None
        slug = task_channel_slug(task)
    else:
        return None
    if not isinstance(slug, str) or not slug.strip():
        return None
    channel = get_channel(slug)
    if channel is None:
        return None
    username = (channel.username or "").strip().lstrip("@")
    if not username:
        return None
    return "https://t.me/" + username


# ── Paid referral claim helpers (MT-TASK-06) ─────────────────────────


def _claim_error(reason: str, user_id: int, task_id: int):
    """Map a ReferralClaimError message to a safe API error.

    Existing pipeline/state rejections reuse their exact stable codes
    first; claim-specific reasons get narrow dedicated codes; anything
    else degrades to the generic state error without leaking internals.
    """
    state_error = _pipeline_state_error(reason)
    if state_error is not None:
        return state_error
    lowered = reason.lower()
    if "no referral attribution" in lowered or "self-referral" in lowered:
        return _error("no_referral", _MSG_NO_REFERRAL, 409)
    if "own referral task" in lowered:
        return _error("own_task", _MSG_OWN_TASK, 409)
    if "not supported" in lowered or "repeatable" in lowered:
        return _error(
            "claim_not_repeatable", _MSG_CLAIM_NOT_REPEATABLE, 409
        )
    logger.info(
        "Referral claim rejected: user=%s task=%s — %s",
        user_id, task_id, reason,
    )
    return _error("invalid_task_state", _MSG_INVALID_STATE, 409)


def _claim_outcome_response(
    outcome,
    user_id: int,
    task_id: int,
    pending_message: str = _MSG_CLAIM_RECEIVED,
):
    """Safe response for a claim outcome (pending/approved/rejected).

    Shared by the referral and manual proof families — identical
    state vocabulary; ``pending_message`` only tailors the Arabic
    wording.  Referral callers use the default (unchanged).
    """
    status = _current_status(user_id, task_id)
    if outcome.state == "pending":
        return jsonify(
            {
                "ok": True,
                "status": status,
                "approval": "pending",
                # Page-facing boolean: keep the pending/approved/
                # rejected vocabulary server-side only.
                "awaiting_decision": True,
                "message": pending_message,
            }
        ), 200
    if outcome.state == "rejected":
        # Same-key replay of a rejected claim: durable outcome, no
        # completion, no reward.
        return _error(
            "claim_rejected",
            _MSG_CLAIM_REJECTED,
            409,
            status=status,
            approval="rejected",
        )
    # approved: idempotent same-key replay of an already-completed
    # claim (the attempt policy blocks this path otherwise).
    return jsonify(
        {
            "ok": True,
            "status": status,
            "approval": "approved",
            "message": _MSG_COMPLETED_OK,
        }
    ), 200


def _manual_error(reason: str, user_id: int, task_id: int):
    """Map a ManualProofError message to a safe API error.

    Existing pipeline/state rejections reuse their exact stable codes
    first; proof-specific rejections get a narrow dedicated code;
    anything else degrades to the generic state error without leaking
    internals.
    """
    state_error = _pipeline_state_error(reason)
    if state_error is not None:
        return state_error
    lowered = reason.lower()
    if "proof_ref" in lowered:
        return _error("invalid_proof", _MSG_INVALID_PROOF, 400)
    if "own manual task" in lowered:
        return _error("own_task", _MSG_OWN_TASK, 409)
    logger.info(
        "Manual proof rejected: user=%s task=%s — %s",
        user_id, task_id, reason,
    )
    return _error("invalid_task_state", _MSG_INVALID_STATE, 409)


def _decision_error(reason: str):
    """Map a referral/manual decision error message to a safe API error."""
    lowered = reason.lower()
    if (
        "authorized buyer" in lowered
        or "authorized approver" in lowered
        or "own claim" in lowered
    ):
        return _error("not_approver", _MSG_NOT_APPROVER, 403)
    if "not found" in lowered:
        return _error("claim_not_found", _MSG_CLAIM_NOT_FOUND, 404)
    if "already approved" in lowered:
        return _error(
            "claim_already_approved", _MSG_ALREADY_CLAIM_APPROVED, 409
        )
    if "already rejected" in lowered:
        return _error(
            "claim_already_rejected", _MSG_ALREADY_CLAIM_REJECTED, 409
        )
    logger.warning("Referral decision rejected: %s", reason)
    return _error("invalid_decision", _MSG_INVALID_DECISION, 409)


# ── GET /api/tasks — catalog + the caller's status ────────────────────


@tasks_bp.get("/api/tasks")
def list_tasks():
    """Return active tasks with the authenticated user's status.

    Safe fields only: id, title, description, type, reward, status
    (plus ``join_url`` for channel tasks when a public username is
    configured).
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    try:
        summaries = TaskCatalog().list_available_tasks()
        tasks = []
        for summary in summaries:
            row = db.get_user_task(user_id, summary.id)
            status = row["status"] if row is not None else _STATUS_AVAILABLE
            entry = {
                "id": summary.id,
                "title": summary.title,
                "description": summary.description,
                "type": summary.type,
                "reward": summary.reward,
                "status": status,
            }
            join_url = _safe_join_url(summary.id, summary.type)
            if join_url:
                entry["join_url"] = join_url
            if summary.type == REFERRAL_TASK_TYPE:
                # The worker's OWN claim state as a narrow boolean for
                # the page (pending → true).  Own state only — never
                # anyone else's identity or the buyer's data, never
                # approval authority (this page has no decision control).
                entry["awaiting_decision"] = (
                    worker_claim_state(user_id, summary.id)
                    == db.SUBMISSION_APPROVAL_PENDING
                )
            elif summary.type == MANUAL_TASK_TYPE:
                # Same narrow own-state boolean for manual proofs
                # (MT-TASK-15): true while the worker's own claim
                # awaits the reviewer's decision.  Own state only.
                entry["awaiting_decision"] = worker_awaiting_decision(
                    user_id, summary.id
                )
            tasks.append(entry)
    except Exception:
        logger.exception("Failed to list tasks for user %s", user_id)
        return _server_error()

    return jsonify({"ok": True, "tasks": tasks}), 200


# ── POST /api/tasks/<task_id>/start — TaskLifecycle → TaskStartGate ───


@tasks_bp.post("/api/tasks/<int:task_id>/start")
def start_task(task_id: int):
    """Start an available task through the existing lifecycle.

    Flow: HTTP → TaskLifecycle.start_task → TaskStartGate → started.
    The HTTP handler never mutates user_tasks itself, and any body
    supplied by the browser (including a ``user_id`` field) is ignored:
    identity comes only from verified initData.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    body = request.get_json(silent=True)
    if body is not None and not isinstance(body, dict):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)

    try:
        result = TaskLifecycle().start_task(user_id, task_id)
    except StartGateError as exc:
        logger.info(
            "Start rejected: user=%s task=%s — %s", user_id, task_id, exc
        )
        return _start_error(str(exc))
    except Exception:
        logger.exception(
            "Start failed unexpectedly: user=%s task=%s", user_id, task_id
        )
        return _server_error()

    if not result.success:
        return _start_error(result.message)

    return jsonify(
        {
            "ok": True,
            "status": result.status,
            "message": _MSG_STARTED_OK,
        }
    ), 200


# ── POST /api/tasks/<task_id>/submit — TaskLifecycle → CompletionBridge


@tasks_bp.post("/api/tasks/<int:task_id>/submit")
def submit_task(task_id: int):
    """Submit a started task for verification through the lifecycle.

    Flow: HTTP → TaskLifecycle.submit_task → CompletionBridge →
    TaskAttemptPolicy → TaskSubmissionService → ChannelTaskVerifier →
    VerificationResult → CompletionGate (PASSED only).

    The request may carry an optional JSON object of submission data;
    it is handed to the existing TaskSubmissionService contract, which
    rejects forbidden fields (reward, status, task/user identifiers).
    The server-side task definition stays authoritative — a client
    channel slug/id can never choose which channel is verified.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    body = request.get_json(silent=True)
    if body is None:
        actual_data: dict = {}
    elif isinstance(body, dict):
        actual_data = body
    else:
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)

    # Optional standard idempotency header (MT-TASK-04): validated
    # here, enforced by the database; absent → server-generated key.
    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key:
            idempotency_key = None
        elif (
            len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH
            or not all(
                c.isalnum() or c in _IDEMPOTENCY_KEY_CHARS
                for c in idempotency_key
            )
        ):
            return _error(
                "invalid_idempotency_key",
                _MSG_INVALID_IDEMPOTENCY_KEY,
                400,
            )

    # ── Paid referral tasks (MT-TASK-06): approval-gated dispatch ──
    # Referral claims never complete on submission: they open a
    # pending claim at the submission layer.  Completion happens only
    # through the buyer's decision endpoint below.
    task_row = db.get_task(task_id)
    if task_row is not None and task_row["type"] == REFERRAL_TASK_TYPE:
        forbidden_found = FORBIDDEN_FIELDS.intersection(
            actual_data.keys()
        )
        if forbidden_found:
            return _error(
                "invalid_submission", _MSG_INVALID_SUBMISSION, 400
            )
        try:
            outcome = ReferralClaimService.submit(
                user_id, task_id, idempotency_key
            )
        except ReferralClaimError as exc:
            return _claim_error(str(exc), user_id, task_id)
        except Exception:
            logger.exception(
                "Referral claim failed unexpectedly: user=%s task=%s",
                user_id, task_id,
            )
            return _server_error()
        return _claim_outcome_response(outcome, user_id, task_id)

    # ── Manual/social-proof tasks (MT-TASK-15): approval-gated dispatch
    # Manual claims never complete on submission: they open a pending
    # claim at the submission layer carrying the bounded proof_ref.
    # Completion happens only through the reviewer's decision endpoint
    # below; proof_ref itself is never trusted for identity,
    # authorization, reward or task selection.
    if task_row is not None and task_row["type"] == MANUAL_TASK_TYPE:
        forbidden_found = FORBIDDEN_FIELDS.intersection(
            actual_data.keys()
        )
        if forbidden_found:
            return _error(
                "invalid_submission", _MSG_INVALID_SUBMISSION, 400
            )
        proof_ref = actual_data.get("proof_ref")
        try:
            outcome = ManualProofService.submit(
                user_id, task_id, proof_ref, idempotency_key
            )
        except ManualProofError as exc:
            return _manual_error(str(exc), user_id, task_id)
        except Exception:
            logger.exception(
                "Manual proof claim failed unexpectedly: "
                "user=%s task=%s",
                user_id, task_id,
            )
            return _server_error()
        return _claim_outcome_response(
            outcome, user_id, task_id,
            pending_message=_MSG_PROOF_RECEIVED,
        )

    try:
        result = TaskLifecycle().submit_task(
            user_id, task_id, actual_data,
            idempotency_key=idempotency_key,
        )
    except Exception:
        logger.exception(
            "Submit failed unexpectedly: user=%s task=%s", user_id, task_id
        )
        return _server_error()

    if result.passed:
        return jsonify(
            {
                "ok": True,
                "status": _STATUS_COMPLETED,
                "message": _MSG_COMPLETED_OK,
            }
        ), 200

    # Distinguish state/validation rejections from real verification
    # outcomes (FAILED / ERROR) without leaking internal details.
    state_error = _pipeline_state_error(result.reason)
    if state_error is not None:
        logger.info(
            "Submit rejected: user=%s task=%s — %s",
            user_id, task_id, result.reason,
        )
        return state_error

    status = _current_status(user_id, task_id)
    if result.status.value == "failed":
        return _error(
            "verification_failed",
            _MSG_VERIFICATION_FAILED,
            409,
            status=status,
        )

    logger.warning(
        "Verification error: user=%s task=%s", user_id, task_id
    )
    return _error(
        "verification_error",
        _MSG_VERIFICATION_ERROR,
        502,
        status=status,
    )


# ── GET /api/tasks/<task_id>/claims — buyer/approver view ────────────


@tasks_bp.get("/api/tasks/<int:task_id>/claims")
def list_claims(task_id: int):
    """Pending referral claims of one task — the buyer's view.

    Authorization comes ONLY from the trusted server-side task
    definition (task_data.approver.telegram_user_id) compared against
    the verified initData identity.  A worker probing this endpoint
    gets a narrow denial and zero data; worker identities are never
    exposed to anyone here.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    task = db.get_task(task_id)
    if task is None:
        return _error("task_not_found", _MSG_TASK_NOT_FOUND, 404)
    if task["type"] not in (REFERRAL_TASK_TYPE, MANUAL_TASK_TYPE):
        return _error("invalid_task_state", _MSG_INVALID_STATE, 409)

    if task["type"] == MANUAL_TASK_TYPE:
        approver_id = manual_task_approver_user_id(task)
    else:
        approver_id = task_approver_user_id(task)
    if approver_id is None:
        # Broken/malformed definition: fail closed, expose nothing.
        return _error("invalid_task_state", _MSG_INVALID_STATE, 409)
    if user_id != approver_id:
        return _error("not_approver", _MSG_NOT_APPROVER, 403)

    try:
        claims = TaskSubmissionStore.list_pending_claims_for_task(task_id)
    except Exception:
        logger.exception("Failed to list claims: task=%s", task_id)
        return _server_error()

    # Safe fields only: claim id + when it was opened, plus (manual
    # tasks only) the task id and the bounded proof reference the
    # authorized reviewer must see.  No worker identity, no referral
    # ids, no task_data, no reward internals.
    items = []
    for c in claims:
        item = {
            "claim_id": c.submission_id,
            "submitted_at": c.submitted_at,
        }
        if task["type"] == MANUAL_TASK_TYPE:
            item["task_id"] = task_id
            item["proof_ref"] = c.proof_ref
        items.append(item)
    return jsonify({"ok": True, "claims": items}), 200


# ── POST /api/tasks/<task_id>/claims/<sid>/decision ──────────────────


@tasks_bp.post(
    "/api/tasks/<int:task_id>/claims/<int:submission_id>/decision"
)
def decide_claim(task_id: int, submission_id: int):
    """The authorized buyer's/reviewer's approve/reject decision.

    Flow: HTTP → ReferralApprovalService.decide (referral tasks) or
    ManualReviewService.decide (manual proof tasks) — server-side
    authorization against the trusted task definition → CAS approval
    → on approve: record passed → CompletionGate → TaskRewardService;
    on reject: record failed, nothing else.  The decision comes from
    a literal body value — identity, authority, reward and completion
    all stay server-side.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    body = request.get_json(silent=True)
    if body is not None and not isinstance(body, dict):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)
    decision = body.get("decision") if isinstance(body, dict) else None
    if decision not in ("approve", "reject"):
        return _error("invalid_decision", _MSG_INVALID_DECISION, 400)

    try:
        # Dispatch by the server-side task type: manual proof claims
        # go to the manual review service (MT-TASK-15); everything
        # else keeps the referral decision path exactly as before.
        task = db.get_task(task_id)
        if task is not None and task["type"] == MANUAL_TASK_TYPE:
            outcome = ManualReviewService.decide(
                user_id,
                task_id,
                submission_id,
                approve=(decision == "approve"),
            )
        else:
            outcome = ReferralApprovalService.decide(
                user_id,
                task_id,
                submission_id,
                approve=(decision == "approve"),
            )
    except (ReferralDecisionError, ManualDecisionError) as exc:
        return _decision_error(str(exc))
    except Exception:
        logger.exception(
            "Decision failed unexpectedly: user=%s task=%s claim=%s",
            user_id, task_id, submission_id,
        )
        return _server_error()

    return jsonify(
        {
            "ok": True,
            "claim_id": submission_id,
            "approval": outcome.state,
            "message": (
                _MSG_APPROVED_OK
                if outcome.state == "approved"
                else _MSG_REJECTED_OK
            ),
        }
    ), 200
