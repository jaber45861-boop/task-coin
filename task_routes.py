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

Account summary (Mini App «حسابي», minimal):

- ``GET  /api/me``                       the caller's own read-only
                                         summary: the available balance
                                         (``wallets.available_units``),
                                         the completed / in-progress task
                                         counts (``user_tasks``) and the
                                         lifetime task earnings (the
                                         ledger's ``credit``/``task``
                                         entries).  No XP/level exists
                                         anywhere in the backend, so
                                         none is returned.

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
- no reward, wallet, ledger or balance MUTATION exists in this module:
  ``tasks.reward`` remains display metadata only, and the ONE balance
  interaction here is the MT-TRANS-01 creation-time guard — a read-only
  comparison of the requested reward against ``wallets.available_units``
  that rejects (HTTP 400) before any request/pending/notification state
  is created
- the account summary is read-only as well: ``GET /api/me``
  composes its figures through the read-only
  ``account_summary`` service (``wallet_units``,
  ``db.list_user_tasks`` and the ledger's public
  ``list_user_entries`` read API) and writes nothing
"""

import json
import logging
import uuid
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
import task_request_notifications
import wallet
from urllib.parse import urlsplit

import task_proof_storage
import task_request_rate_limit
import task_request_store
from account_summary import load_summary
from task_catalog import TaskCatalog
from task_creation import parse_reward_units
from task_lifecycle import TaskLifecycle
from task_start import StartGateError
from task_submission import FORBIDDEN_FIELDS
from task_submission_store import TaskSubmissionStore
from wallet import units_to_decimal

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
# Manual task proof screenshot evidence (MT-ADMIN-34)
_MSG_PROOF_NOT_STARTED = "ابدأ المهمة قبل إرسال الإثبات"
_MSG_INVALID_IMAGE = "الصورة غير صالحة أو غير مدعومة"
_MSG_INVALID_UPLOAD = "ملف غير صالح"
_MSG_FILE_TOO_LARGE = "حجم الصورة كبير جداً"
_MSG_PROOF_IMAGE_READY = "تم تجهيز الصورة، أرسل الإثبات الآن"

_MSG_STARTED_OK = "تم بدء المهمة"
_MSG_COMPLETED_OK = "تم إنجاز المهمة بنجاح"

# User task requests (Mini App «إضافة مهمة») — proposed tasks that
# enter admin review BEFORE ever becoming catalog tasks.
_MSG_TRQ_SUBMITTED = "تم إرسال المهمة للمراجعة من الإدارة."
_MSG_TRQ_INVALID = "بيانات طلب المهمة غير صالحة"
_MSG_TRQ_NOT_FOUND = "الطلب غير موجود"
_MSG_TRQ_INVALID_STATUS = "لا يمكن تعديل هذا الطلب في وضعه الحالي"
_MSG_TRQ_RATE_LIMITED = "تجاوزت الحد المسموح لطلبات المهام، حاول بعد قليل"


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


def _safe_task_url(task_id: int) -> str | None:
    """The manual task's external destination, or ``None``.

    Narrow scope: ``manual`` tasks only, and only the ONE field
    ``task_data["target"]["ref"]``.  The raw ``task_data`` blob is
    never returned — in particular the ``approver`` block and every
    other key stay server-side.

    The value is validated, never followed: this function performs no
    network request.  It is rejected unless it is a non-empty string
    with scheme exactly ``https``, a non-empty hostname, and no
    embedded credentials (``user:password@host``).  Anything else —
    ``javascript:``, ``data:``, ``http:``, an empty or whitespace
    string, a non-string, a bare word, a host-less URL, or a
    credential-bearing URL — yields ``None`` so the client simply
    shows no open-link control.
    """
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
    target = task_data.get("target")
    if not isinstance(target, dict):
        return None
    ref = target.get("ref")
    if not isinstance(ref, str):
        return None
    candidate = ref.strip()
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() != "https":
        return None
    if not parts.netloc or not parts.hostname:
        return None
    # Reject embedded credentials: https://user:pass@host/…
    if parts.username or parts.password or "@" in parts.netloc:
        return None
    return candidate


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

    Safe fields only: id, title, description, type, reward,
    reward_units, status (plus ``join_url`` for channel tasks when a
    public username is configured, and ``task_url`` for manual tasks
    when a valid external destination exists).

    ``reward_units`` (MT-ADMIN-14) is the exact atomic accounting
    value: the page derives sub-cent USDT display from it with
    integer math only.  ``reward`` stays the whole-USDT
    compatibility/display field.

    MT-TASK-29: a task whose caller-cycle is fully approved
    (``user_tasks.status == completed``) is EXCLUDED from this active
    list — it disappears on every reload/reopen.  Nothing is mutated:
    the row, its submissions and its approval audit stay intact in
    the database, the account stats keep counting them, and only the
    ACTIVE catalog read skips the terminal cycle.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    try:
        summaries = TaskCatalog().list_available_tasks()
        # Batch-load advertisers so the frontend can show owner actions
        # only for tasks the caller funds (one query, not N+1).
        task_ids = [s.id for s in summaries]
        advertisers: dict[int, int] = {}
        if task_ids:
            with db.get_connection() as conn:
                placeholders = ",".join("?" * len(task_ids))
                rows = conn.execute(
                    f"SELECT task_id, advertiser_id FROM task_funding "
                    f"WHERE task_id IN ({placeholders})",
                    task_ids,
                ).fetchall()
                for r in rows:
                    advertisers[r["task_id"]] = r["advertiser_id"]

        tasks = []
        for summary in summaries:
            row = db.get_user_task(user_id, summary.id)
            # MT-TASK-29: approved (completed) cycles leave the active
            # list.  A plain read-skip — no state, history, reward or
            # repeat-policy semantics change with it.
            if row is not None and row["status"] == _STATUS_COMPLETED:
                continue
            status = row["status"] if row is not None else _STATUS_AVAILABLE
            task_row = db.get_task(summary.id)
            entry = {
                "id": summary.id,
                "title": summary.title,
                "description": summary.description,
                "type": summary.type,
                "reward": summary.reward,
                "reward_units": summary.reward_units,
                "status": status,
                "paused": bool(task_row["paused"]) if task_row else False,
                "archived_at": task_row["archived_at"] if task_row else None,
                "is_owner": advertisers.get(summary.id) == user_id,
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
                # The task's own external destination (MT-TASK-17), so
                # the user can open it BEFORE sending a proof.  One
                # validated field only — the raw task_data, and every
                # approver identity, stays server-side.  Absent unless
                # a valid https link exists.
                task_url = _safe_task_url(summary.id)
                if task_url:
                    entry["task_url"] = task_url
            tasks.append(entry)
    except Exception:
        logger.exception("Failed to list tasks for user %s", user_id)
        return _server_error()

    return jsonify({"ok": True, "tasks": tasks}), 200


# ── GET /api/me — the caller's own account summary ────────


@tasks_bp.get("/api/me")
def account_summary():
    """Read-only account summary for the verified caller.

    Every figure comes from an existing source of truth:

    - ``wallet`` — ``null`` when the caller has no wallet row
      (never had a wallet created), else
      ``{"availableUnits": <int>, "egpDisplayMinor": <int|null>}``
      with the exact ``wallets.available_units`` — a wallet that
      exists with a zero balance is a real zero, never "unknown".
      ``egpDisplayMinor`` is the wallet's EGP ESTIMATE at the
      current market rate, in integer piastres, and is ``null``
      when no fresh valid rate exists.  It is a display figure,
      not a withdrawable EGP balance.
    - ``stats.completedTasks`` / ``stats.inProgressTasks`` —
      the caller's own ``user_tasks`` rows
    - ``stats.earnedUnits`` — the ledger's ``credit``/``task``
      entries, the system's only earnings source (deposits,
      withdrawals, referrals and admin credits are excluded)

    No XP/level exists anywhere in the backend, so none is
    returned.  Identity comes only from verified initData — a
    client-supplied ``user_id`` is never consulted — and the
    handler mutates nothing: every figure is composed by the
    read-only ``account_summary`` service.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    try:
        summary = load_summary(user_id)
    except Exception:
        logger.exception(
            "Account summary failed: user=%s", user_id
        )
        return _server_error()

    return jsonify(
        {
            "ok": True,
            "user": {
                "id": user_id,
                "username": user.get("username"),
                "firstName": user.get("first_name"),
            },
            "wallet": (
                {
                    "availableUnits":
                        summary["wallet"]["available_units"],
                    "egpDisplayMinor":
                        summary["wallet"]["egp_display_minor"],
                }
                if summary["wallet"] is not None
                else None
            ),
            "stats": {
                "completedTasks":
                    summary["stats"]["completed_tasks"],
                "inProgressTasks":
                    summary["stats"]["in_progress_tasks"],
                "earnedUnits":
                    summary["stats"]["earned_units"],
            },
        }
    ), 200


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


# ── POST /api/tasks/<task_id>/proof-image — evidence upload (MT-ADMIN-34) ──


@tasks_bp.post("/api/tasks/<int:task_id>/proof-image")
def upload_task_proof_image(task_id: int):
    """Store one task-proof screenshot as review evidence.

    This handler ONLY stores the evidence bytes and returns a
    server-generated reference.  It opens NO submission, never
    changes the task's state, and can never reward or complete
    anything: the client then submits that reference through the
    existing ``POST /api/tasks/<id>/submit`` path, so the whole
    approval lifecycle, idempotency and reward rules stay exactly
    where they were.

    Authorization mirrors the deposit-proof upload (MT-ADMIN-31):
    identity comes only from cryptographically verified initData — a
    ``user_id`` in the body/form/query is ignored; the caller must
    own a STARTED claim on a manual task.

    Content is validated by MAGIC BYTES with bounded size and pixel
    dimensions; the client filename and claimed MIME type are never
    trusted or stored.  The on-disk name is server-generated.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    task = db.get_task(task_id)
    if task is None or task["type"] != MANUAL_TASK_TYPE:
        return _error(
            "payment_method_not_found", _MSG_TASK_NOT_FOUND, 404
        )

    # The claim must exist and be in progress — the same precondition
    # the submit path enforces, so an image can never be attached to
    # an available, completed or unknown task.
    row = db.get_user_task(user_id, task_id)
    if row is None:
        return _error(
            "invalid_request", _MSG_PROOF_NOT_STARTED, 409
        )
    if row["status"] != "started":
        return _error(
            "invalid_request", _MSG_PROOF_NOT_STARTED, 409
        )

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return _error("invalid_upload", _MSG_INVALID_UPLOAD, 400)

    # ── bounded read + content sniffing (client MIME ignored) ────
    try:
        data = upload.read(task_proof_storage.MAX_IMAGE_BYTES + 1)
        if not data:
            return _error("invalid_upload", _MSG_INVALID_UPLOAD, 400)
        if len(data) > task_proof_storage.MAX_IMAGE_BYTES:
            return _error(
                "file_too_large", _MSG_FILE_TOO_LARGE, 400
            )
        _mime, ext, _width, _height = task_proof_storage.probe(data)
    except task_proof_storage.ProofImageError as exc:
        return _error(getattr(exc, "code", "invalid_upload"),
                      _MSG_INVALID_IMAGE, 400)
    except Exception:
        logger.exception(
            "Task proof read failed: task=%s user=%s", task_id, user_id
        )
        return _server_error()

    # ── evidence file under a server-generated key.  A store failure
    #    removes the just-written file — no orphan evidence.
    proof_id = uuid.uuid4().hex
    try:
        storage_key = task_proof_storage.save_image(
            data, proof_id=proof_id, ext=ext
        )
    except Exception:
        logger.exception(
            "Task proof storage failed: task=%s user=%s",
            task_id, user_id,
        )
        return _server_error()

    logger.info(
        "Task proof image stored: task=%s user=%s size=%d",
        task_id, user_id, len(data),
    )
    return jsonify(
        {
            "ok": True,
            "task_id": task_id,
            "proof_ref": storage_key,
            "bytes": len(data),
            "message": _MSG_PROOF_IMAGE_READY,
        }
    ), 201


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


# ════════════════════════════════════════════════════════════════════
# User task requests (Mini App «إضافة مهمة ➕»)
# ════════════════════════════════════════════════════════════════════
#
# A user proposes a task; it lands in the ``user_task_requests``
# table as ``pending`` and NEVER becomes a catalog task until an
# admin approves it from Telegram (``task_request_admin`` — this
# HTTP surface carries no approve/reject/return capability of any
# kind):
#
# - ``POST /api/task-requests``               create a pending request
# - ``GET  /api/task-requests``               the caller's own requests
# - ``GET  /api/task-requests/<request_id>``  one OWN request (404 else)
# - ``PATCH /api/task-requests/<request_id>`` edit + resubmit
#                                             (only when the admin
#                                             returned it for changes)
#
# Security rules (same as every route above):
# - identity comes ONLY from verified initData (``_authenticate``);
#   a body/query/header ``user_id`` is never trusted, and every
#   read/write is filtered by the verified owner column
# - every field is validated server-side with the SAME validators
#   the creation path uses (``task_request_store.validate_payload``);
#   client validation is cosmetic only
# - the response exposes safe presentation fields only: no history,
#   no admin identity (``decided_by``), no internals
# - POST alone is rate-limited per VERIFIED user_id
#   (``task_request_rate_limit`` — atomic check+count, HTTP 429);
#   GET/PATCH, the admin workflow and the state machine are never
#   consulted by it, and resubmit-after-changes (PATCH) can never
#   be blocked by it


def _trq_safe(req) -> dict:
    """Safe presentation fields for one user task request."""
    return {
        "request_id": req.request_id,
        "status": req.status,
        "title": req.payload.get("title"),
        "description": req.payload.get("description"),
        "provider": req.payload.get("provider"),
        "action": req.payload.get("action"),
        "target_ref": req.payload.get("target_ref"),
        "reward": req.reward,
        "reward_units": req.reward_units,
        # MT-TASK-28: the advertiser's execution policy (all optional;
        # null = not configured = legacy behaviour on the task row).
        "repeat_policy": req.repeat_policy,
        "repeat_hours": req.repeat_hours,
        "max_executions": req.max_executions,
        "reservation_minutes": req.reservation_minutes,
        "reason": req.decision_reason,
        "task_id": req.published_task_id,
        "created_at": req.created_at,
        "updated_at": req.updated_at,
    }


@tasks_bp.post("/api/task-requests")
def create_task_request():
    """Create a pending task request for the verified caller.

    The proposal is validated server-side and stored as ``pending``;
    it does NOT appear in the public catalog until an admin approves
    it (the catalog reads ``tasks`` exclusively).
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    # Rate limit — POST only, keyed by the verified user_id, counted
    # atomically BEFORE the payload is parsed so a flood of invalid
    # bodies is bounded too.  GET/PATCH/admin handlers never reach
    # this line.
    retry_after = task_request_rate_limit.check_attempt(user_id)
    if retry_after is not None:
        body, status = _error(
            "rate_limited", _MSG_TRQ_RATE_LIMITED, 429,
            retry_after=retry_after,
        )
        body.headers["Retry-After"] = str(retry_after)
        return body, status

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)

    # ── Server-side balance guard (MT-TRANS-01) ──
    # Before anything is persisted, prove the verified caller can
    # actually cover the requested reward.  The reward is parsed from
    # the raw payload with the ONE canonical exact parser (the very
    # ``parse_reward_units`` call the store's ``validate_payload``
    # makes), then compared against the source of truth —
    # ``wallets.available_units`` (SQLite INTEGER, 1 USDT =
    # 100_000_000 atomic units) — as an exact Decimal, BEFORE the
    # INSERT, before any pending state and before any admin
    # notification.  A malformed reward is left to
    # ``validate_payload`` below (it rejects it with the usual
    # ``invalid_payload``).  No new column, no new wallet/ledger
    # operation, no schema change: a pure read + comparison — the
    # balance itself never changes on this path.
    try:
        required_units = parse_reward_units(
            body.get("reward"), field="المكافعة"
        )
    except (TypeError, ValueError):
        required_units = None
    available_units = 0
    if required_units is not None:
        with db.get_connection() as conn:
            wallet_row = conn.execute(
                "SELECT available_units FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if wallet_row is not None:
            available_units = int(wallet_row["available_units"])
    if required_units is not None and available_units < required_units:
        available = units_to_decimal(available_units)
        required = units_to_decimal(required_units)
        return _error(
            "balance_insufficient",
            f"رصيدك غير كافٍ لطلب هذه المهمة. الرصيد المتاح: "
            f"{available:.8f} USDT، والمبلغ المطلوب: {required:.8f} USDT.",
            400,
            available_balance=float(available),
            required_reward=float(required),
        )

    try:
        created = task_request_store.create_request(user_id, body)
    except task_request_store.TaskRequestError as exc:
        return _error("invalid_payload", str(exc), 400)
    except Exception:
        logger.exception(
            "Task request creation failed: user=%s", user_id
        )
        return _server_error()

    # Admin push (fail-soft): schedule the ADMINS notification for the
    # fresh pending request via AdminNotifier + the bot-loop scheduler.
    # It NEVER raises and never changes this response — a failed or
    # unbound notification only logs (with the request id).
    task_request_notifications.notify_pending(created)

    return jsonify(
        {
            "ok": True,
            "request": _trq_safe(created),
            "message": _MSG_TRQ_SUBMITTED,
        }
    ), 200


@tasks_bp.get("/api/task-requests")
def list_task_requests():
    """The verified caller's own requests (newest first)."""
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    try:
        requests = task_request_store.list_for_user(user_id)
    except Exception:
        logger.exception(
            "Task request list failed: user=%s", user_id
        )
        return _server_error()
    return jsonify(
        {
            "ok": True,
            "requests": [_trq_safe(r) for r in requests],
        }
    ), 200


@tasks_bp.get("/api/task-requests/<int:request_id>")
def get_task_request(request_id: int):
    """One of the caller's OWN requests — 404 for anyone else's."""
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    try:
        req = task_request_store.get_request(request_id)
    except Exception:
        logger.exception(
            "Task request read failed: user=%s request=%s",
            user_id, request_id,
        )
        return _server_error()
    # Ownership is part of the lookup: a foreign request is
    # indistinguishable from a missing one for this caller.
    if req is None or req.user_id != user_id:
        return _error("request_not_found", _MSG_TRQ_NOT_FOUND, 404)
    return jsonify({"ok": True, "request": _trq_safe(req)}), 200


@tasks_bp.patch("/api/task-requests/<int:request_id>")
def resubmit_task_request(request_id: int):
    """Edit + resubmit the caller's OWN request after the admin
    returned it for changes (``changes_requested`` → ``pending``).

    The body is the full proposal; every field is re-validated
    server-side.  No status/user id can be client-set — the state
    transition is decided by the store, never by the payload.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)

    try:
        updated = task_request_store.resubmit_request(
            request_id, user_id, body
        )
    except task_request_store.TaskRequestError as exc:
        message = str(exc)
        if message == "الطلب غير موجود.":
            return _error("request_not_found", _MSG_TRQ_NOT_FOUND, 404)
        if message == "لا يمكن تعديل هذا الطلب في وضعه الحالي.":
            return _error(
                "invalid_status", _MSG_TRQ_INVALID_STATUS, 409
            )
        return _error("invalid_payload", message, 400)
    except Exception:
        logger.exception(
            "Task request resubmit failed: user=%s request=%s",
            user_id, request_id,
        )
        return _server_error()

    # Admin push (fail-soft) — only reached when the request actually
    # went back to ``pending`` (the store enforces the transition), so
    # a fresh review cycle is notified exactly once (dedup by cycle).
    task_request_notifications.notify_pending(updated)

    return jsonify(
        {
            "ok": True,
            "request": _trq_safe(updated),
            "message": _MSG_TRQ_SUBMITTED,
        }
    ), 200


# ── Arabic messages for owner task management (MT-MINIAPP-USER-TASK-MANAGEMENT) ──
_MSG_OWNER_NOT_FOUND = "أنت لست صاحب هذه المهمة"
_MSG_TASK_ALREADY_PAUSED = "المهمة متوقفة مؤقتاً مسبقاً"
_MSG_TASK_ALREADY_ACTIVE = "المهمة نشطة بالفعل"
_MSG_TASK_ALREADY_ARCHIVED = "المهمة مؤرشفة مسبقاً"
_MSG_TASK_NOT_ARCHIVED = "المهمة ليست مؤرشفة"
_MSG_INSUFFICIENT_BALANCE_FOR_RESUME = (
    "رصيدك غير كافٍ لاستئناف المهمة. يرجى شحن المحفظة ثم إعادة المحاولة."
)
_MSG_UPDATE_OK = "تم تحديث المهمة بنجاح"
_MSG_PAUSE_OK = "تم إيقاف المهمة مؤقتاً"
_MSG_RESUME_OK = "تم استئناف المهمة"
_MSG_ARCHIVE_OK = "تم أرشفة المهمة"
_MSG_UNARCHIVE_OK = "تم إعادة المهمة إلى القائمة النشطة"


# ── Task owner management (MT-MINIAPP-USER-TASK-MANAGEMENT) ─────────
# Every endpoint verifies ownership server-side via task_funding — the
# browser never chooses the owner.  Financial rules:
#   * increasing reward → extra commitment checked against available
#     balance BEFORE any write
#   * decreasing reward → no automatic refund (approved refund path only)
#   * submissions, claims, audit rows and financial constraints are
#     never deleted or retroactively changed


def _task_owner_only(task_id: int, user_id: int):
    """Return (task_row, None) on success, or (None, error_response).

    Ownership is decided by task_funding.advertiser_id — the
    authenticated advertiser who funded the task.  A client-supplied
    user_id is never trusted.
    """
    task = db.get_task(task_id)
    if task is None:
        return None, _error("task_not_found", _MSG_TASK_NOT_FOUND, 404)
    advertiser_id = db.get_task_advertiser(task_id)
    if advertiser_id is None or advertiser_id != user_id:
        return None, _error("not_owner", _MSG_OWNER_NOT_FOUND, 403)
    return task, None


def _funding_cost_units(task_id: int):
    """Total funded cost (reward_units + commission_units) or None."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT reward_units, commission_units FROM task_funding "
            "WHERE task_id = ?", (task_id,)
        ).fetchone()
    if row is None:
        return None
    ru = row["reward_units"] or 0
    cu = row["commission_units"] or 0
    return ru + cu


@tasks_bp.patch("/api/tasks/<int:task_id>/owner")
def update_owned_task(task_id: int):
    """Edit the caller's OWN task (title, description, reward).

    Server-side ownership verification: only the task's advertiser
    (task_funding.advertiser_id) may edit.  Increasing the reward
    requires sufficient available balance; the extra commitment is
    computed and checked BEFORE any write.  Decreasing the reward never
    triggers an automatic refund.

    Allowed body fields: title, description, reward.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    task, err = _task_owner_only(task_id, user_id)
    if err:
        return err

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)

    new_title = body.get("title")
    new_description = body.get("description")
    new_reward = body.get("reward")

    if new_title is not None and (not new_title or not new_title.strip()):
        return _error("invalid_payload", "العنوان لا يمكن أن يكون فارغاً", 400)
    if new_description is not None and (
        not new_description or not new_description.strip()
    ):
        return _error("invalid_payload", "الوصف لا يمكن أن يكون فارغاً", 400)
    if new_reward is not None:
        try:
            new_reward = int(new_reward)
        except (TypeError, ValueError):
            return _error("invalid_payload", "المكافأة يجب أن تكون رقمًا صحيحًا", 400)
        if new_reward < 0:
            return _error("invalid_payload", "المكافأة لا يمكن أن تكون سالبة", 400)

    # ── Financial guard: reward increase requires balance check ─────
    reward_increase_units = 0
    if new_reward is not None and new_reward > task["reward"]:
        delta_reward = new_reward - task["reward"]
        delta_units = delta_reward * wallet.USDT_SCALE
        total_cost_units = _funding_cost_units(task_id)
        if total_cost_units is not None and task["reward_units"]:
            original_reward_units = task["reward_units"]
            commission_ratio = (
                task["commission_units"] / original_reward_units
                if original_reward_units else 0
            )
            delta_commission_units = int(delta_units * commission_ratio)
        else:
            delta_commission_units = 0
        reward_increase_units = delta_units + delta_commission_units

        with db.get_connection() as conn:
            wallet_row = conn.execute(
                "SELECT available_units FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        available_units = int(wallet_row["available_units"]) if wallet_row else 0
        if available_units < reward_increase_units:
            available = units_to_decimal(available_units)
            needed = units_to_decimal(reward_increase_units)
            return _error(
                "balance_insufficient",
                "رصيدك غير كافٍ لتغطية الزيادة في مكافأة المهمة. "
                f"الرصيد المتاح: {available:.8f} USDT، "
                f"المطلوب إضافياً: {needed:.8f} USDT.",
                400,
                available_balance=float(available),
                required_reward=float(needed),
            )

    # ── Apply the update inside a transaction ───────────────────────
    try:
        with db.transaction() as conn:
            fresh = db.get_task(task_id)
            if fresh is None:
                return _error("task_not_found", _MSG_TASK_NOT_FOUND, 404)
            advertiser_id = db.get_task_advertiser(task_id)
            if advertiser_id is None or advertiser_id != user_id:
                return _error("not_owner", _MSG_OWNER_NOT_FOUND, 403)

            if reward_increase_units > 0:
                wallet_row = conn.execute(
                    "SELECT available_units FROM wallets WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                available_units = (
                    int(wallet_row["available_units"]) if wallet_row else 0
                )
                if available_units < reward_increase_units:
                    return _error(
                        "balance_insufficient",
                        "رصيدك غير كافٍ لتغطية الزيادة في مكافأة المهمة.",
                        400,
                    )

            ok = db.update_task(
                task_id,
                title=new_title.strip() if new_title is not None else None,
                description=(
                    new_description.strip()
                    if new_description is not None else None
                ),
                reward=new_reward,
                conn=conn,
            )
            if not ok:
                return _error("task_not_found", _MSG_TASK_NOT_FOUND, 404)
    except Exception:
        logger.exception(
            "Owner task update failed: user=%s task=%s", user_id, task_id
        )
        return _server_error()

    updated = db.get_task(task_id)
    return jsonify(
        {
            "ok": True,
            "task": updated,
            "message": _MSG_UPDATE_OK,
        }
    ), 200


@tasks_bp.post("/api/tasks/<int:task_id>/pause")
def pause_owned_task(task_id: int):
    """Pause the caller's OWN task — stops new starts/assignments.

    Existing user_tasks, submissions and claims are preserved.  Only
    the advertiser (task_funding.advertiser_id) may pause.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    task, err = _task_owner_only(task_id, user_id)
    if err:
        return err
    if task.get("paused"):
        return _error("already_paused", _MSG_TASK_ALREADY_PAUSED, 409)
    if task.get("archived_at") is not None:
        return _error("task_archived", "لا يمكن إيقاف مهمة مؤرشفة", 409)

    try:
        with db.transaction() as conn:
            fresh = db.get_task(task_id)
            if fresh is None or fresh.get("archived_at") is not None:
                return _error("task_not_found", _MSG_TASK_NOT_FOUND, 404)
            advertiser_id = db.get_task_advertiser(task_id)
            if advertiser_id is None or advertiser_id != user_id:
                return _error("not_owner", _MSG_OWNER_NOT_FOUND, 403)
            db.update_task(task_id, paused=True, conn=conn)
    except Exception:
        logger.exception("Pause failed: user=%s task=%s", user_id, task_id)
        return _server_error()

    return jsonify(
        {"ok": True, "message": _MSG_PAUSE_OK, "paused": True}
    ), 200


@tasks_bp.post("/api/tasks/<int:task_id>/resume")
def resume_owned_task(task_id: int):
    """Resume the caller's OWN task after a pause.

    Before resuming, verify the advertiser still has sufficient
    available balance to cover the task's total funded cost
    (reward + commission).  Only the advertiser may resume.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    task, err = _task_owner_only(task_id, user_id)
    if err:
        return err
    if not task.get("paused"):
        return _error("not_paused", _MSG_TASK_ALREADY_ACTIVE, 409)
    if task.get("archived_at") is not None:
        return _error("task_archived", "لا يمكن استئناف مهمة مؤرشفة", 409)

    # Funding/balance guard: the advertiser must still cover the cost.
    total_units = _funding_cost_units(task_id)
    if total_units is not None and total_units > 0:
        with db.get_connection() as conn:
            wallet_row = conn.execute(
                "SELECT available_units FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        available_units = int(wallet_row["available_units"]) if wallet_row else 0
        if available_units < total_units:
            available = units_to_decimal(available_units)
            required = units_to_decimal(total_units)
            return _error(
                "balance_insufficient",
                _MSG_INSUFFICIENT_BALANCE_FOR_RESUME,
                400,
                available_balance=float(available),
                required_reward=float(required),
            )

    try:
        with db.transaction() as conn:
            fresh = db.get_task(task_id)
            if fresh is None or fresh.get("archived_at") is not None:
                return _error("task_not_found", _MSG_TASK_NOT_FOUND, 404)
            advertiser_id = db.get_task_advertiser(task_id)
            if advertiser_id is None or advertiser_id != user_id:
                return _error("not_owner", _MSG_OWNER_NOT_FOUND, 403)
            if not fresh.get("paused"):
                return _error("not_paused", _MSG_TASK_ALREADY_ACTIVE, 409)
            db.update_task(task_id, paused=False, conn=conn)
    except Exception:
        logger.exception("Resume failed: user=%s task=%s", user_id, task_id)
        return _server_error()

    return jsonify(
        {"ok": True, "message": _MSG_RESUME_OK, "paused": False}
    ), 200


@tasks_bp.post("/api/tasks/<int:task_id>/archive")
def archive_owned_task(task_id: int):
    """Archive the caller's OWN task (soft delete).

    Archives the task — sets archived_at — without deleting any rows:
    user_tasks, task_submissions, task_funding, ledger entries and audit
    records all stay intact.  Only the advertiser may archive, and a
    clear confirmation is required (the client must send
    {"confirm": true}).
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    task, err = _task_owner_only(task_id, user_id)
    if err:
        return err
    if task.get("archived_at") is not None:
        return _error("already_archived", _MSG_TASK_ALREADY_ARCHIVED, 409)

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return _error(
            "confirmation_required",
            "تأكيد مطلوب قبل الأرشفة — أرسل {\"confirm\": true}",
            400,
        )

    try:
        with db.transaction() as conn:
            fresh = db.get_task(task_id)
            if fresh is None:
                return _error("task_not_found", _MSG_TASK_NOT_FOUND, 404)
            advertiser_id = db.get_task_advertiser(task_id)
            if advertiser_id is None or advertiser_id != user_id:
                return _error("not_owner", _MSG_OWNER_NOT_FOUND, 403)
            if fresh.get("archived_at") is not None:
                return _error("already_archived", _MSG_TASK_ALREADY_ARCHIVED, 409)
            conn.execute(
                "UPDATE tasks SET archived_at = CURRENT_TIMESTAMP WHERE id = ?",
                (task_id,),
            )
    except Exception:
        logger.exception("Archive failed: user=%s task=%s", user_id, task_id)
        return _server_error()

    return jsonify(
        {"ok": True, "message": _MSG_ARCHIVE_OK, "archived": True}
    ), 200
