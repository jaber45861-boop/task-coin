"""
Admin Manual-Proof Inbox (MT-ADMIN-03)
======================================

Telegram private-chat review surface for manual proof claims: when a
worker's proof submission opens a fresh pending claim, the configured
admins receive one Arabic notification with persistent Approve /
Reject buttons, and the decision flow reuses the EXISTING review
pipeline unchanged.

Architecture (server-side only — no wallet/ledger/completion code here)::

    Worker
      ↓
    ManualProofService.submit()
      ↓
    persistent task_submissions claim (pending)
      ↓  fresh claim only (created=True, state=pending)
    schedule_pending_notification()          ← this module
      ↓  (bot event loop, cross-thread)
    AdminNotifier.notify_system()            ← ADMINS private chats only
      ↓
    admin_notifications linkage (persistent) ← admin_notification_store
      ↓
    Admin private Telegram message
      ├── ✅ Approve   (mproof:approve:<claim id>)
      └── ❌ Reject    (mproof:reject:<claim id>)
           ↓
    handle_callback()                        ← this module
      ↓  linkage resolved server-side + claim/task re-read + actor
      ↓  authorized ONLY by task_data.approver.telegram_user_id
    ManualReviewService.decide()             ← SOLE decision path
      ↓
    existing CompletionGate / TaskRewardService

Callback security (every press is untrusted):
- callback data carries ONLY the action and the claim id — the
  operation must already exist in the persistent linkage table
  (server-created notification), otherwise it is stale/invalid
- the claim, task, task type and approval gate are re-read from the
  database — never parsed from the callback payload
- the actor is the verified Telegram user pressing the button; there
  is no admin bypass and no second authorization mechanism
- ManualReviewService.decide() applies CAS/idempotency; this module
  never mutates wallet, ledger, reward or completion state itself

Boundaries (this module must NOT):
- decide anything except through ManualReviewService.decide()
- authorize by is_admin (ADMINS only RECEIVE notifications)
- send outside config.ADMINS (AdminNotifier enforces)
- hold operation state in memory (SQLite linkage only)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import db
from admin_notification_store import AdminNotificationStore
from manual_task import (
    MANUAL_TASK_TYPE,
    ManualDecisionError,
    ManualReviewService,
    manual_task_approver_user_id,
)
from task_submission_store import SubmissionRecord, TaskSubmissionStore
import task_proof_storage

logger = logging.getLogger(__name__)

# ── Operation + callback vocabulary ──────────────────────────────────

OPERATION_MANUAL_PROOF = "manual_proof"
CALLBACK_PREFIX = "mproof"
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"

# Transport shapes injected by the bot (production) or tests.
AnswerFunc = Callable[[str], Awaitable[None]]
EditFunc = Callable[[int, int, str], Awaitable[None]]
ScheduleFunc = Callable[..., object]

# ── Arabic messages (concise, operational; no parse_mode markup) ─────

MSG_PENDING_HEADER = "📥 طلب إثبات جديد — مهمة يدوية"
MSG_PENDING_STATE = "⏳ بانتظار قرار معتمد هذه المهمة فقط."
MSG_APPROVED = "✅ تمت الموافقة على الإثبات"
MSG_REJECTED = "❌ تم رفض الإثبات"
MSG_STATUS_APPROVED = "تمت الموافقة"
MSG_STATUS_REJECTED = "تم الرفض"

MSG_APPROVED_TOAST = "✅ تمت الموافقة على الطلب"
MSG_REJECTED_TOAST = "❌ تم رفض الطلب"
MSG_ALREADY_DECIDED = "ℹ️ تم اتخاذ قرار على هذا الطلب مسبقاً"
MSG_UNAUTHORIZED = "⛔ ليس لديك صلاحية اتخاذ هذا القرار"
MSG_STALE = "⚠️ الطلب غير صالح أو منتهي الصلاحية"
MSG_INVALID = "⚠️ أمر غير صالح"
MSG_ERROR = "⚠️ تعذر تنفيذ العملية، حاول مرة أخرى"
MSG_EDIT_CLOSED = "⚠️ لم يعد هذا الطلب قابلاً للمعالجة."
MSG_WORKER_APPROVED = (
    "✅ تمت الموافقة على إثبات مهمتك، وتمت إضافة المكافأة إلى رصيدك."
)
MSG_WORKER_REJECTED = (
    "❌ لم يتم قبول إثبات مهمتك، يمكنك إرسال إثبات آخر من صفحة المهام."
)

BUTTON_APPROVE = "✅ موافقة"
BUTTON_REJECT = "❌ رفض"


# ── Message / keyboard builders (presentation only) ──────────────────


def _task_label(task: dict) -> str:
    """Human task label from the trusted task row (title + id only)."""
    title = task.get("title") or "—"
    return f"{title} (#{task.get('task_id', task.get('id', '?'))})"


def _proof_display(proof_ref: object) -> str:
    """Human proof line — never an internal storage key.

    A stored task-proof screenshot (MT-ADMIN-34) is shown as an
    attached image: the key is an internal identifier and printing it
    would tell the reviewer nothing.  A plain text/URL proof is
    rendered verbatim, exactly as before.  Nothing is parsed and no
    markdown is ever applied on this surface.
    """
    if task_proof_storage.is_image_reference(proof_ref):
        return "📷 صورة مرفقة (انظر الصورة أعلاه)"
    return str(proof_ref) if proof_ref else "—"


def build_pending_text(task: dict, record: SubmissionRecord) -> str:
    """Arabic pending notification: task, claim and proof only.

    Never includes task_data, approver identity, worker identity or
    reward internals — the proof reference is rendered as plain text
    (no parse mode is ever used on this surface).
    """
    return (
        f"{MSG_PENDING_HEADER}\n\n"
        f"المهمة: {_task_label(task)}\n"
        # MT-ADMIN-35: this id is the CLAIM (task_submissions) id —
        # calling it "رقم الطلب" collided with the user_task_requests
        # numbering and misdirected reviews.
        f"رقم المطالبة: {record.submission_id}\n"
        f"الإثبات: {_proof_display(record.proof_ref)}\n\n"
        f"{MSG_PENDING_STATE}"
    )


def build_decided_text(
    task: dict, record: SubmissionRecord, approved: bool
) -> str:
    """Arabic inert replacement once the claim is decided."""
    header = MSG_APPROVED if approved else MSG_REJECTED
    status = MSG_STATUS_APPROVED if approved else MSG_STATUS_REJECTED
    return (
        f"{header}\n\n"
        f"المهمة: {_task_label(task)}\n"
        # MT-ADMIN-35: claim id — never "رقم الطلب" (see build_pending_text).
        f"رقم المطالبة: {record.submission_id}\n"
        f"الحالة: {status}"
    )


def callback_data(approve: bool, submission_id: int) -> str:
    """Serialize an Approve/Reject button payload (claim id only)."""
    action = ACTION_APPROVE if approve else ACTION_REJECT
    return f"{CALLBACK_PREFIX}:{action}:{submission_id}"


def build_decision_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    """Persistent ✅ Approve / ❌ Reject inline keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BUTTON_APPROVE, callback_data=callback_data(True, submission_id)
                ),
                InlineKeyboardButton(
                    BUTTON_REJECT, callback_data=callback_data(False, submission_id)
                ),
            ]
        ]
    )


def parse_callback(data: object) -> tuple[bool, int] | None:
    """Parse UNTRUSTED callback data → ``(approve, submission_id)``.

    Accepts exactly ``mproof:<approve|reject>:<positive claim id>``;
    everything else (extra segments, non-ASCII digits, zero/negative
    ids, wrong prefixes) returns None.  The returned claim id is a
    LOOKUP POINTER ONLY — it is never trusted without the persistent
    linkage plus fresh server-side reads.
    """
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    prefix, action, raw_id = parts
    if prefix != CALLBACK_PREFIX:
        return None
    if action == ACTION_APPROVE:
        approve = True
    elif action == ACTION_REJECT:
        approve = False
    else:
        return None
    if not (raw_id.isascii() and raw_id.isdigit()):
        return None
    submission_id = int(raw_id)
    if submission_id <= 0:
        return None
    return approve, submission_id


# ── Delivery binding (server thread → bot event loop) ───────────────

# Bound once by bot.py when the Telegram loop starts; None before that
# (and after shutdown) so submissions fail soft instead of failing the
# worker's claim.  No operation state lives here — only transports.
_notifier = None
_scheduler: Optional[ScheduleFunc] = None
# Delivery transport for the WORKER's outcome notice (MT-ADMIN-35).
# Kept strictly separate from the admin-bound transports above so the
# admin notification path cannot be affected by it, and so nothing
# here can ever reach a chat other than the submitting worker.
_user_notifier = None


def bind(notifier, scheduler: ScheduleFunc) -> None:
    """Attach the AdminNotifier + cross-thread scheduler (bot.py)."""
    global _notifier, _scheduler
    if not callable(scheduler):
        raise ValueError("scheduler must be callable")
    _notifier = notifier
    _scheduler = scheduler
    logger.info("Manual proof inbox bound to the Telegram bot loop")


def bind_user_notifier(user_notifier) -> None:
    """Attach the WORKER outcome transport (bot.py, MT-ADMIN-35).

    A separate, optional transport: it delivers the decision to the
    worker whose proof was reviewed.  It is bound only while the bot
    loop runs, exactly like the admin transport, and a failure is
    always fail-soft — the decision itself is already durable.
    """
    global _user_notifier
    _user_notifier = user_notifier
    logger.info("Manual proof worker notifier bound")


def unbind_user_notifier() -> None:
    """Detach the worker transport (bot shutdown)."""
    global _user_notifier
    _user_notifier = None
    logger.info("Manual proof worker notifier unbound")


def is_user_notifier_bound() -> bool:
    return _user_notifier is not None


def unbind() -> None:
    """Detach transports (bot shutdown) — notifications fail soft."""
    global _notifier, _scheduler
    _notifier = None
    _scheduler = None
    logger.info("Manual proof inbox unbound"); unbind_user_notifier()


def is_bound() -> bool:
    return _notifier is not None and _scheduler is not None


async def _deliver(
    notifier,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    submission_id: int,
    evidence_image: bytes | None = None,
) -> None:
    """Send the pending notification and persist its linkages.

    Runs on the bot event loop.  Failures are logged, never raised —
    the worker's claim is already durable before this runs.

    When *evidence_image* is present it is delivered through the
    notifier's ADDITIONAL photo path so the reviewer sees the actual
    screenshot instead of a storage key.  That path is entirely
    optional: when no notifier photo transport is configured, or the
    photo send fails, it falls back to the plain text notification, so
    a missing transport can never lose the event.  Without an image
    the behaviour is byte-identical to before.
    """
    try:
        # Replay guard: never issue a second pending notification for
        # a claim that already has one (belt above: the UNIQUE index).
        if AdminNotificationStore.has_linkage(
            OPERATION_MANUAL_PROOF, submission_id
        ):
            logger.info(
                "Manual proof notification already exists: submission=%d",
                submission_id,
            )
            return
        if evidence_image is not None and hasattr(
            notifier, "notify_system_photo"
        ):
            # Additional photo path — never a replacement.  It reuses
            # the notifier's own admin-only recipient resolution and
            # falls back to text on any failure.
            delivered = await notifier.notify_system_photo(
                text, evidence_image, reply_markup=reply_markup
            )
        else:
            delivered = await notifier.notify_system(
                text, reply_markup=reply_markup
            )
        for chat_id, message_id in delivered:
            if not isinstance(message_id, int) or message_id <= 0:
                logger.warning(
                    "No message id for chat %s — linkage skipped: "
                    "submission=%d",
                    chat_id, submission_id,
                )
                continue
            AdminNotificationStore.create_linkage(
                OPERATION_MANUAL_PROOF, submission_id, chat_id, message_id
            )
    except Exception:
        logger.exception(
            "Manual proof notification delivery failed: submission=%d",
            submission_id,
        )


def build_worker_notice_text(approved: bool) -> str:
    """The worker's outcome message.  No internal identifiers, no
    approver identity, no reward figure — only the decision and what
    the worker should do next."""
    return MSG_WORKER_APPROVED if approved else MSG_WORKER_REJECTED


def schedule_worker_notice(user_id: int, approved: bool) -> None:
    """Schedule the WORKER's outcome notice (MT-ADMIN-35).

    Called from the decision path ONLY after ManualReviewService has
    durably approved/rejected the claim — never for a transient
    failure, an unauthorized press or a repeat of an already-decided
    claim, so a retry cannot notify twice.

    Delivery uses the worker's OWN trusted telegram user id (the
    submission row's user_id); no client-supplied id is ever used.
    The send is fail-soft: a Telegram failure is logged and never
    changes the already-saved decision.
    """
    notifier = _user_notifier
    if notifier is None:
        logger.info(
            "Worker notice skipped (transport not bound): user=%d "
            "approved=%s",
            user_id, approved,
        )
        return
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
    ):
        logger.warning("Worker notice skipped: invalid user id")
        return
    text = build_worker_notice_text(approved)

    if _scheduler is not None:
        # Hand the send across when this runs off the bot loop.
        coroutine = _deliver_worker_notice(notifier, user_id, text)
        try:
            future = _scheduler(coroutine)
        except Exception:
            coroutine.close()
            raise
        if future is not None and hasattr(future, "add_done_callback"):
            future.add_done_callback(_log_future_result)
        return

    # Inside the bot's own coroutine there is already a running loop;
    # awaiting is correct and cannot lose the send.
    asyncio.run(_deliver_worker_notice(notifier, user_id, text))


async def _deliver_worker_notice(notifier, user_id: int, text: str) -> None:
    """Send one worker notice.  Runs on the bot event loop.

    Fail-soft by design: the decision it reports is already durable,
    so a Telegram failure must never be able to look like a failed
    approval.  Failures are logged with the id only — never the
    message body, which is already fixed text.
    """
    # Guard here rather than only inside a specific transport, so the
    # rule holds for whatever is bound.  Both arguments are already
    # produced by this module, so this is a bug guard, not input
    # validation of untrusted data.
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
    ):
        raise ValueError("worker id must be a positive integer")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("worker notice text must be non-empty")
    try:
        await notifier.notify(user_id, text)
    except Exception:
        logger.exception(
            "Worker notice delivery failed: user=%s", user_id
        )


def schedule_pending_notification(
    task: dict, record: SubmissionRecord
) -> None:
    """Notify ADMINS about one FRESH pending manual claim (fail-soft).

    Called by ManualProofService.submit only when the claim was just
    created (idempotent replays resolve an existing claim and never
    reach this point).  The actual send is scheduled onto the bot
    event loop; without a bound inbox the call is a logged no-op.
    """
    try:
        if not isinstance(task, dict) or task.get("type") != MANUAL_TASK_TYPE:
            return
        if record.approval_status != db.SUBMISSION_APPROVAL_PENDING:
            return
        if AdminNotificationStore.has_linkage(
            OPERATION_MANUAL_PROOF, record.submission_id
        ):
            logger.info(
                "Manual proof notification replay suppressed: "
                "submission=%d",
                record.submission_id,
            )
            return
        notifier = _notifier
        scheduler = _scheduler
        if notifier is None or scheduler is None:
            logger.info(
                "Manual proof notification skipped (inbox not bound): "
                "submission=%d",
                record.submission_id,
            )
            return
        # When the evidence is a stored screenshot, read it back (the
        # storage layer re-validates the key) so the reviewer sees the
        # actual image.  A missing/damaged file or a plain text/URL
        # proof yields None and the notification stays text-only —
        # never a broken state.
        evidence_image = None
        try:
            if task_proof_storage.is_image_reference(record.proof_ref):
                evidence_image = task_proof_storage.read_image(
                    record.proof_ref
                )
        except Exception:
            logger.exception(
                "Task proof evidence unreadable: submission=%d "
                "(delivering text-only)",
                record.submission_id,
            )
            evidence_image = None
        coroutine = _deliver(
            notifier,
            build_pending_text(task, record),
            build_decision_keyboard(record.submission_id),
            record.submission_id,
            evidence_image,
        )
        try:
            future = scheduler(coroutine)
        except Exception:
            coroutine.close()
            raise
        if future is not None and hasattr(future, "add_done_callback"):
            future.add_done_callback(_log_future_result)
    except Exception:
        logger.exception(
            "Failed to schedule manual proof notification: submission=%s",
            getattr(record, "submission_id", "?"),
        )


def _log_future_result(future) -> None:
    """Surface async delivery failures in the logs (never raise)."""
    try:
        exc = future.exception()
    except Exception:  # pragma: no cover - defensive
        return
    if exc is not None:
        logger.error(
            "Manual proof notification task failed: %s", exc, exc_info=exc
        )


# ── Callback handling (the ONLY Telegram decision entry point) ────────


async def _safe_answer(answer: AnswerFunc, text: str) -> None:
    try:
        await answer(text)
    except Exception:
        logger.debug("Could not answer manual proof callback", exc_info=True)


def _edit_targets(
    links, pressed_chat_id, pressed_message_id
) -> list[tuple[int, int]]:
    """Message edit targets: every linked admin copy PLUS the message
    the button was actually pressed on — de-duplicated, stable order.

    MT-ADMIN-04: a review card opened from the /reviews queue may not
    have its own linkage row (UNIQUE is per operation+chat), so the
    pressed message must be inerted directly as well.
    """
    targets = [(l.admin_chat_id, l.message_id) for l in links]
    if isinstance(pressed_chat_id, int) and isinstance(
        pressed_message_id, int
    ):
        pressed = (pressed_chat_id, pressed_message_id)
        if pressed not in targets:
            targets.append(pressed)
    return targets


async def _edit_all(edit: EditFunc, targets, text: str) -> None:
    """Make every target message inert (no buttons)."""
    for chat_id, message_id in targets:
        try:
            await edit(chat_id, message_id, text)
        except Exception:
            logger.debug(
                "Could not edit admin notification %s/%s",
                chat_id, message_id,
                exc_info=True,
            )


async def handle_callback(
    data: object,
    actor_user_id: object,
    *,
    answer: AnswerFunc,
    edit: EditFunc,
    pressed_chat_id: int | None = None,
    pressed_message_id: int | None = None,
) -> str:
    """Resolve an Approve/Reject press through server-side state.

    Args:
        data: Raw (untrusted) Telegram callback payload.
        actor_user_id: Verified Telegram user id of the presser —
            never taken from the payload.
        answer: Toast the pressed chat (``async (text) -> None``).
        edit: Replace one target admin message with inert text
            (``async (chat_id, message_id, text) -> None``); the edit
            surface carries no keyboard, so buttons cannot survive.
        pressed_chat_id / pressed_message_id: The message the button
            was actually pressed on (MT-ADMIN-04) — added to the edit
            targets so a queue-opened review card also goes inert
            after a decision.  Optional; never trusted as identity.

    Returns:
        One of ``invalid``, ``stale``, ``unauthorized``, ``approved``,
        ``rejected``, ``already_decided``, ``error`` — for logging and
        tests.  State changes happen exclusively inside
        ManualReviewService.decide().
    """
    parsed = parse_callback(data)
    if parsed is None:
        await _safe_answer(answer, MSG_INVALID)
        return "invalid"
    approve, submission_id = parsed

    # ── 1. Server-side linkage (callback is only a lookup pointer) ──
    try:
        links = AdminNotificationStore.list_for_operation(
            OPERATION_MANUAL_PROOF, submission_id
        )
        record = (
            TaskSubmissionStore.get_submission(submission_id)
            if links
            else None
        )
        task = db.get_task(record.task_id) if record is not None else None
    except Exception:
        logger.exception(
            "Manual proof callback resolution failed: submission=%d "
            "actor=%r",
            submission_id, actor_user_id,
        )
        await _safe_answer(answer, MSG_ERROR)
        return "error"

    targets = _edit_targets(links, pressed_chat_id, pressed_message_id)

    if not links or record is None:
        await _safe_answer(answer, MSG_STALE)
        await _edit_all(edit, targets, MSG_EDIT_CLOSED)
        return "stale"

    # ── 2. The linked claim must still be a manual approval-gated claim
    if (
        task is None
        or task.get("type") != MANUAL_TASK_TYPE
        or record.approval_status is None
    ):
        await _safe_answer(answer, MSG_STALE)
        await _edit_all(edit, targets, MSG_EDIT_CLOSED)
        return "stale"

    approver_id = manual_task_approver_user_id(task)
    if approver_id is None:
        # Broken/malformed definition: fail closed, buttons go inert.
        await _safe_answer(answer, MSG_STALE)
        await _edit_all(edit, targets, MSG_EDIT_CLOSED)
        return "stale"

    # ── 3. Authorization: verified actor == task approver, NO bypass ─
    #      ADMINS receive the notification; only this identity decides.
    if not isinstance(actor_user_id, int) or actor_user_id != approver_id:
        await _safe_answer(answer, MSG_UNAUTHORIZED)
        return "unauthorized"  # no edit, no state change

    # ── 4. The SOLE decision path (CAS + idempotency authoritative) ──
    # Snapshot the claim state BEFORE deciding.  ManualReviewService is
    # idempotent: re-pressing Approve on an already-approved claim
    # re-returns the same outcome WITHOUT raising, so without this
    # snapshot a repeated press would look like a fresh approval and
    # notify the worker twice.
    was_pending = (
        record.approval_status == db.SUBMISSION_APPROVAL_PENDING
    )
    try:
        outcome = ManualReviewService.decide(
            actor_user_id,
            record.task_id,
            record.submission_id,
            approve=approve,
        )
    except ManualDecisionError as exc:
        reason = str(exc)
        if reason in ("claim already approved", "claim already rejected"):
            current = TaskSubmissionStore.get_submission(record.submission_id)
            approved_now = bool(
                current is not None
                and current.approval_status == db.SUBMISSION_APPROVAL_APPROVED
            )
            await _safe_answer(answer, MSG_ALREADY_DECIDED)
            if current is not None:
                await _edit_all(
                    edit,
                    targets,
                    build_decided_text(task, record, approved_now),
                )
            else:
                await _edit_all(edit, targets, MSG_EDIT_CLOSED)
            return "already_decided"
        if "authorized approver" in reason:
            # task_data changed under us — still no admin fallback.
            await _safe_answer(answer, MSG_UNAUTHORIZED)
            return "unauthorized"
        if (
            "not found" in reason
            or "not a manual task" in reason
            or "invalid manual task definition" in reason
            or "not approval-gated" in reason
        ):
            logger.warning(
                "Manual proof decision target closed: submission=%d — %s",
                record.submission_id, reason,
            )
            await _safe_answer(answer, MSG_STALE)
            await _edit_all(edit, targets, MSG_EDIT_CLOSED)
            return "stale"
        # Transient/ambiguous failure: fail closed — keep the buttons,
        # change nothing, let the approver retry.
        logger.warning(
            "Manual proof decision rejected: submission=%d — %s",
            record.submission_id, reason,
        )
        await _safe_answer(answer, MSG_ERROR)
        return "error"
    except Exception:
        logger.exception(
            "Manual proof decision failed unexpectedly: submission=%d "
            "actor=%r",
            record.submission_id, actor_user_id,
        )
        await _safe_answer(answer, MSG_ERROR)
        return "error"

    # ── 5. Success: toast + make EVERY admin copy inert ──────────────
    approved = outcome.state == "approved"
    await _safe_answer(
        answer, MSG_APPROVED_TOAST if approved else MSG_REJECTED_TOAST
    )
    await _edit_all(edit, targets, build_decided_text(task, record, approved))

    # ── 6. Tell the WORKER the outcome (MT-ADMIN-35) ──────────────
    # Only when this press actually APPLIED the decision.  A replay of
    # an already-decided claim re-runs decide() idempotently and lands
    # here too, but was_pending is False then — the worker was already
    # told, so no second notice.  Fail-soft by design: the decision is
    # already saved, so a delivery problem must not look like a failed
    # approval.
    if was_pending:
        # The decision path already runs on the bot's event loop, so the
        # notice is awaited directly — no cross-thread hop, and nothing
        # can silently drop it.
        notifier = _user_notifier
        try:
            if notifier is not None and isinstance(record.user_id, int) \
                    and not isinstance(record.user_id, bool) \
                    and record.user_id > 0:
                await _deliver_worker_notice(
                    notifier,
                    record.user_id,
                    build_worker_notice_text(approved),
                )
        except Exception:
            logger.exception(
                "Worker notice scheduling failed: submission=%d user=%s",
                record.submission_id, record.user_id,
            )

    logger.info(
        "Manual proof decided via Telegram: submission=%d actor=%d "
        "decision=%s status=%s",
        record.submission_id, actor_user_id,
        "approve" if approve else "reject", outcome.state,
    )
    return "approved" if approved else "rejected"


# ── python-telegram-bot adapter (registered once from bot.py) ───────


async def proof_callback_handler(update, context) -> None:
    """PTB handler for ``mproof:`` callback queries (MT-ADMIN-03).

    Thin transport adapter only: it answers/edits the pressed message
    and delegates every decision to :func:`handle_callback`.  Registered
    exactly once by bot.py's ``main()``:

        app.add_handler(CallbackQueryHandler(
            manual_proof_inbox.proof_callback_handler, pattern=r"^mproof:",
        ), group=5)

    Isolation mirrors the MT-ADMIN-02 guard: reviews are private-chat
    only — outside private chats the press is dismissed silently and
    nothing is edited or decided.
    """
    query = getattr(update, "callback_query", None)
    if query is None:
        return

    # Private-chat isolation (same semantics as bot._non_private_chat,
    # inlined to keep this module import-cycle free).
    chat = getattr(update, "effective_chat", None)
    chat_type = getattr(chat, "type", None)
    if isinstance(chat_type, str) and chat_type != "private":
        try:
            await query.answer()
        except TelegramError:
            pass
        return

    async def _answer(text: str) -> None:
        try:
            await query.answer(text=text)
        except TelegramError:
            logger.debug(
                "Could not answer manual proof callback", exc_info=True
            )

    async def _edit(chat_id: int, message_id: int, text: str) -> None:
        # Text-only edit: the inline keyboard goes with it, so a
        # decided notification's buttons can never be reused.
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text
            )
        except TelegramError:
            logger.debug(
                "Could not edit admin notification %s/%s",
                chat_id, message_id, exc_info=True,
            )

    actor = getattr(query, "from_user", None)
    actor_id = getattr(actor, "id", 0)

    # The pressed message joins the inert-edit set (MT-ADMIN-04), so a
    # review card opened from the /reviews queue also loses its buttons.
    pressed = getattr(query, "message", None)
    pressed_chat = getattr(pressed, "chat", None)
    pressed_chat_id = getattr(pressed_chat, "id", None)
    pressed_message_id = getattr(pressed, "message_id", None)

    try:
        status = await handle_callback(
            query.data,
            actor_id,
            answer=_answer,
            edit=_edit,
            pressed_chat_id=pressed_chat_id,
            pressed_message_id=pressed_message_id,
        )
    except Exception:
        logger.exception(
            "Manual proof callback failed: actor=%r data=%r",
            actor_id, query.data,
        )
        await _answer(MSG_ERROR)
        return
    logger.info(
        "Manual proof callback: actor=%r data=%r → %s",
        actor_id, query.data, status,
    )
