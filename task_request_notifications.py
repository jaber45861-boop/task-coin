"""
Task Request Admin Notifications (Push for «إضافة مهمة ➕»)
===========================================================

Push a notification to ``config.ADMINS`` whenever a user task request
enters ``pending`` — the missing link between:

    Mini App  POST /api/task-requests
        → user_task_requests (pending)             [task_routes / store]
        → AdminNotifier.notify_system              [THIS module]
        → Telegram admin review (existing treq:…)  [task_request_admin]

Design (reuses the EXISTING notification stack — no parallel system):

- ``AdminNotifier`` (MT-ADMIN-02/03) is the ONLY delivery path; it may
  target ``config.ADMINS`` exclusively, so there is no new ADMIN_ID
  anywhere in this module.
- The send runs on the BOT event loop through the scheduler bridge
  bound by ``bot.py`` (``run_coroutine_threadsafe``) — never directly
  on the Flask/Waitress request thread.
- Idempotency reuses ``admin_notification_store`` with a dedicated
  operation type ``task_request``.  The operation id encodes the
  *pending cycle* (``request_id * 1000 + cycle``, cycle = number of
  ``submitted`` history events), so a retry/replay can never double
  send for the same cycle, while a resubmit (a NEW pending cycle)
  notifies exactly once more.
- Fail-soft by contract: NOTHING here may raise into the creation
  path.  A Telegram failure, an unbound bridge or an empty
  ``config.ADMINS`` is logged (with the request id + reason) and the
  already-persisted request stays successful.
- No new decision surface: the inline buttons only reference the
  EXISTING closed ``treq:`` grammar (``treq:view:<id>`` / list), and
  every press is re-authorized and re-read server-side by
  ``task_request_admin`` — approve/reject semantics are untouched.

Run:
    python3 -m pytest test_task_request_notifications.py -v
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from admin_notification_store import AdminNotificationStore
import task_request_admin
from task_request_store import STATUS_PENDING, TaskRequest

logger = logging.getLogger(__name__)

# ── Linkage identity (dedup) ──────────────────────────────────────────

# Dedicated operation type — current operations (manual_proof, …) are
# untouched; this only ADDS a namespace for task-request pushes.
OPERATION_TASK_REQUEST = "task_request"

# operation_id = request_id * _CYCLES_PER_REQUEST + cycle  (cycle < 1000)
_CYCLES_PER_REQUEST = 1000

# ── Presentation (pure — reuses the existing review builders) ─────────

HEADER_NEW = "🆕 طلب مهمة جديد للمراجعة"
HEADER_RESUBMIT = "🔁 طلب مهمة أُعيد إرساله للمراجعة"
BUTTON_REVIEW = "🔍 مراجعة الطلب"
HINT = "افتح المراجعة لعرض التفاصيل ثم اتخذ القرار (موافقة/تعديل/إرجاع/رفض)."


def pending_cycle(request: TaskRequest) -> int:
    """1 for the first pending cycle, +1 for every resubmission.

    Derived from the stored ``submitted`` history events, so the value
    is deterministic for retries of the SAME cycle.
    """
    submitted = 0
    for event in request.history or []:
        if isinstance(event, dict) and event.get("event") == "submitted":
            submitted += 1
    return min(max(submitted, 1), _CYCLES_PER_REQUEST - 1)


def operation_id_for(request: TaskRequest) -> int:
    """Dedup key for ONE pending cycle of a request (positive int)."""
    return int(request.request_id) * _CYCLES_PER_REQUEST + pending_cycle(
        request
    )


def build_text(request: TaskRequest) -> str:
    """Notification body: header + request id + the existing detail
    card (status ⏳ قيد المراجعة, owner, title, description, provider ·
    action, target, reward)."""
    header = (
        HEADER_NEW if pending_cycle(request) <= 1 else HEADER_RESUBMIT
    )
    return "\n".join(
        [
            header,
            f"رقم الطلب: #{request.request_id}",
            "",
            task_request_admin.build_detail_text(request),
            "",
            HINT,
        ]
    )


def build_keyboard(request: TaskRequest) -> InlineKeyboardMarkup:
    """Inline buttons into the EXISTING review path (closed treq: grammar)."""
    view_cb = (
        f"{task_request_admin.CALLBACK_PREFIX}:"
        f"{task_request_admin.OP_VIEW}:{request.request_id}"
    )
    list_cb = (
        f"{task_request_admin.CALLBACK_PREFIX}:"
        f"{task_request_admin.OP_LIST}"
    )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{BUTTON_REVIEW} #{request.request_id}",
                    callback_data=view_cb,
                )
            ],
            [
                InlineKeyboardButton(
                    task_request_admin.BUTTON_LIST, callback_data=list_cb
                )
            ],
        ]
    )


# ── Delivery binding (server thread → bot event loop) ────────────────
#
# Bound once by bot.py when the Telegram loop starts; None before that
# (and after shutdown) so a request creation fails soft instead of
# failing.  No operation state lives here — only transports.

ScheduleFunc = Callable[[object], object]

_notifier = None
_scheduler: Optional[ScheduleFunc] = None


def bind(notifier, scheduler: ScheduleFunc) -> None:
    """Attach the AdminNotifier + cross-thread scheduler (bot.py)."""
    global _notifier, _scheduler
    if not callable(scheduler):
        raise ValueError("scheduler must be callable")
    _notifier = notifier
    _scheduler = scheduler
    logger.info(
        "Task request notifications bound to the Telegram bot loop"
    )


def unbind() -> None:
    """Detach transports (bot shutdown) — notifications fail soft."""
    global _notifier, _scheduler
    _notifier = None
    _scheduler = None
    logger.info("Task request notifications unbound")


def is_bound() -> bool:
    return _notifier is not None and _scheduler is not None


# ── Delivery (runs on the bot event loop) ─────────────────────────────


async def _deliver(
    notifier,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    operation_id: int,
    request_id: int,
) -> None:
    """Send the notification and persist its linkages.

    Runs on the bot event loop.  Failures are logged, never raised —
    the request row is already durable before this runs.
    """
    try:
        # Replay guard: one notification per pending cycle (belt above:
        # the UNIQUE (operation_type, operation_id, admin_chat) index).
        if AdminNotificationStore.has_linkage(
            OPERATION_TASK_REQUEST, operation_id
        ):
            logger.info(
                "Task request notification already exists: request=%s "
                "operation_id=%s",
                request_id,
                operation_id,
            )
            return
        delivered = await notifier.notify_system(
            text, reply_markup=reply_markup
        )
        if not delivered:
            logger.warning(
                "Task request created but no admin recipient: request=%s "
                "operation_id=%s (config.ADMINS empty?)",
                request_id,
                operation_id,
            )
            return
        for chat_id, message_id in delivered:
            if not isinstance(message_id, int) or message_id <= 0:
                logger.warning(
                    "No message id for chat %s — linkage skipped: "
                    "request=%s",
                    chat_id,
                    request_id,
                )
                continue
            AdminNotificationStore.create_linkage(
                OPERATION_TASK_REQUEST, operation_id, chat_id, message_id
            )
        logger.info(
            "Task request notification delivered: request=%s "
            "operation_id=%s admins=%s",
            request_id,
            operation_id,
            len(delivered),
        )
    except Exception:
        logger.exception(
            "Task request notification delivery failed: request=%s "
            "operation_id=%s",
            request_id,
            operation_id,
        )


def _log_future_result(future) -> None:
    """Surface async delivery failures in the logs (never raise)."""
    try:
        exc = future.exception()
    except Exception:  # pragma: no cover - defensive
        return
    if exc is not None:
        logger.error(
            "Task request notification task failed: %s", exc, exc_info=exc
        )


# ── Entry point (Flask request thread — fail-soft by contract) ────────


def notify_pending(request: Optional[TaskRequest]) -> None:
    """Notify ADMINS that *request* is pending review (fail-soft).

    Called by ``task_routes`` right after a successful create/resubmit.
    Only a request whose CURRENT status is ``pending`` is notified, so
    GET/reload/decided requests never notify; the linkage makes a
    replay of the same pending cycle send exactly one message.
    """
    try:
        if request is None:
            return
        if request.status != STATUS_PENDING:
            logger.info(
                "Task request notification skipped (not pending): "
                "request=%s status=%s",
                getattr(request, "request_id", "?"),
                request.status,
            )
            return
        request_id = int(request.request_id)
        operation_id = operation_id_for(request)
        if AdminNotificationStore.has_linkage(
            OPERATION_TASK_REQUEST, operation_id
        ):
            logger.info(
                "Task request notification replay suppressed: request=%s "
                "operation_id=%s",
                request_id,
                operation_id,
            )
            return
        notifier = _notifier
        scheduler = _scheduler
        if notifier is None or scheduler is None:
            logger.warning(
                "Task request created but notification bridge is not "
                "bound: request=%s operation_id=%s",
                request_id,
                operation_id,
            )
            return
        admin_ids = getattr(notifier, "admin_ids", None)
        if isinstance(admin_ids, (list, tuple)) and not admin_ids:
            logger.warning(
                "Task request created but config.ADMINS is empty — no "
                "recipient for the admin notification: request=%s",
                request_id,
            )
            return
        coroutine = _deliver(
            notifier,
            build_text(request),
            build_keyboard(request),
            operation_id,
            request_id,
        )
        try:
            future = scheduler(coroutine)
        except Exception:
            coroutine.close()
            raise
        if future is not None and hasattr(future, "add_done_callback"):
            future.add_done_callback(_log_future_result)
    except Exception:
        # Never propagate: the request row is already committed.
        logger.exception(
            "Failed to schedule task request notification: request=%s",
            getattr(request, "request_id", "?"),
        )
