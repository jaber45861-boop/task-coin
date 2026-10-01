"""
Withdrawal Submission Notifications (side effect, never in the tx)
==================================================================

Best-effort admin alerts for NEW pending withdrawals, following the
established ``manual_proof_inbox`` binder pattern: ``bot.py`` binds the
existing ``AdminNotifier`` + loop scheduler once the bot loop runs, and
both transports (Mini App route + Telegram command) call
:func:`notify_submission` AFTER ``WithdrawalService.create`` has
already COMMITTED its atomic transaction.

Hard rules:

- the notification is never inside the financial transaction and can
  never roll back, repeat or delay a committed create;
- every failure (unbound inbox, scheduler error, delivery error) is
  logged and swallowed — :func:`notify_submission` never raises;
- delivery reuses the bound ``AdminNotifier`` (config.ADMINS private
  chats only — never channels/groups); no second Telegram delivery
  path is created here;
- the text carries safe facts only (short id, amount, fee, method
  display name): no destinations, no credentials, no DB internals.

Idempotency: the helper is invoked only by the exact code path that
PERFORMED the create.  Replayed submissions never reach it — the
service's pending/cooldown/duplicate guards refuse them first — so no
persistent operation-linkage row is required (the
``admin_notifications.operation_id`` column is an INTEGER while
withdrawal request ids are opaque strings; the natural once-only call
sites make that linkage unnecessary).
"""

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

Notifier = object  # AdminNotifier-like: async notify_system(text, ...)
ScheduleFunc = Callable[..., object]

_notifier: Optional[Notifier] = None
_scheduler: Optional[ScheduleFunc] = None


def bind(notifier, scheduler: ScheduleFunc) -> None:
    """Attach the bot-loop notifier + scheduler (called from bot.py)."""
    global _notifier, _scheduler
    _notifier = notifier
    _scheduler = scheduler
    logger.info("Withdrawal submission notifications bound")


def unbind() -> None:
    """Detach — a stopped bot must never send (called from bot.py)."""
    global _notifier, _scheduler
    _notifier = None
    _scheduler = None
    logger.info("Withdrawal submission notifications unbound")


def is_bound() -> bool:
    return _notifier is not None and _scheduler is not None


def build_submission_text(request) -> str:
    """Arabic admin notice for one PENDING withdrawal — safe facts."""
    short_id = str(request.request_id)[:8]
    unit = request.native_unit
    return (
        "💸 طلب سحب جديد بانتظار المراجعة\n"
        f"رقم الطلب: #{short_id}\n"
        f"المبلغ: {request.amount_native} {unit}\n"
        f"الرسوم: {request.fee_native} {unit}\n"
        f"الوسيلة: {request.pm_display_name}\n"
        "راجع /withdrawals للمراجعة والإتمام"
    )


async def _deliver(notifier, text: str) -> None:
    """Send on the bot event loop.  Failures are logged, never raised
    — the create is already committed before this runs."""
    try:
        await notifier.notify_system(text)
    except Exception:
        logger.exception("Withdrawal submission notification delivery failed")


def _log_future_result(future) -> None:
    """Surface async delivery failures in the logs (never raise)."""
    try:
        exc = future.exception()
    except Exception:
        logger.exception("Withdrawal notification future failed")
        return
    if exc is not None:
        logger.error(
            "Withdrawal submission notification errored: %r", exc
        )


def notify_submission(request) -> None:
    """Schedule the admin notice for a just-created request.

    Best-effort: NEVER raises.  Must be called only by the caller that
    actually performed ``WithdrawalService.create`` (post-commit).
    """
    try:
        notifier = _notifier
        scheduler = _scheduler
        if notifier is None or scheduler is None:
            logger.info(
                "Withdrawal submission notification skipped "
                "(inbox not bound): request=%s",
                getattr(request, "request_id", "?"),
            )
            return
        text = build_submission_text(request)
        coroutine = _deliver(notifier, text)
        try:
            future = scheduler(coroutine)
        except Exception:
            coroutine.close()
            raise
        if future is not None and hasattr(future, "add_done_callback"):
            future.add_done_callback(_log_future_result)
    except Exception:
        logger.exception(
            "Failed to schedule withdrawal submission notification: "
            "request=%s",
            getattr(request, "request_id", "?"),
        )
