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
  tasks only, the public ``https://t.me/<username>`` join destination
  resolved server-side from the trusted task definition — never raw
  ``task_data``, never numeric channel ids, never verifier internals
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
from task_catalog import TaskCatalog
from task_lifecycle import TaskLifecycle
from task_start import StartGateError

logger = logging.getLogger(__name__)

tasks_bp = Blueprint("tasks", __name__)

# Telegram initData carrier for XHR calls (kept out of URLs/logs).
INIT_DATA_HEADER = "X-Telegram-Init-Data"
# Also accepted as a query parameter, mirroring the social routes.
INIT_DATA_QUERY = "init_data"

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
_MSG_VERIFICATION_FAILED = (
    "لم يتم تأكيد إنجاز المهمة، تأكد من اشتراكك ثم أعد المحاولة"
)
_MSG_VERIFICATION_ERROR = "تعذر التحقق حالياً، حاول مرة أخرى لاحقاً"

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

    Narrow scope: only ``channel_subscription`` tasks, only the
    configured channel's public ``@username`` rendered as a
    ``https://t.me/<username>`` link.  Numeric channel ids, slugs,
    task_data and admin fields are never returned.
    """
    if task_type != CHANNEL_TASK_TYPE:
        return None
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
    if not isinstance(slug, str) or not slug.strip():
        return None
    channel = get_channel(slug)
    if channel is None:
        return None
    username = (channel.username or "").strip().lstrip("@")
    if not username:
        return None
    return "https://t.me/" + username


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

    try:
        result = TaskLifecycle().submit_task(user_id, task_id, actual_data)
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
