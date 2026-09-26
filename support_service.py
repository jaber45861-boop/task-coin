"""
Support Service (MT-ADMIN-06)
=============================

Persistent Telegram user support: a user opens a support conversation
from their private chat, admins are notified through the existing
AdminNotifier (MT-ADMIN-02) and answer from their private Telegram
admin chat.  Telegram stays the only control plane — there is no Mini
App support UI in this task.

Architecture::

    User (private chat)
      ↓ /support → category → message
    persistent SupportInquiry + SupportMessage  (support_store)
      ↓ AdminNotifier.notify_system  (ADMINS private chats ONLY)
    Admin private Telegram chat
      ↓ [ فتح #id ] → [ 💬 رد ] → persisted reply context
    support_store.append_admin_message  (persist FIRST)
      ↓ context.bot.send_message → recipient from the inquiry row
    User Telegram chat

Command routing (one deterministic approach):
    /support in a private chat →
        config admin  → the admin support queue
        anyone else   → the user support flow (category selection)
    Group/channel invocations of any support handler are silent
    (MT-ADMIN-02 isolation).

State: ALL conversation state lives in SQLite (support_store) —
the user's pending category, the admin's reply context, inquiries and
messages.  A bot restart loses nothing.  Callback payloads carry ONLY
an opaque positive inquiry/page id as a lookup pointer; every fact
(actor authorization, inquiry status, recipient) is re-read
server-side.  Recipients come from the persistent inquiry row, never
from callback data.

Notifications reuse AdminNotifier + AdminNotificationStore with
``operation_type='support'`` — no second notification system.  The
notification carries only safe operational data: inquiry id, category
label, user identifier, message text and status.

Message rendering is plain text everywhere (no parse_mode), so user
text can never inject Markdown/HTML.
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import admin_task_wizard
import db
import support_store
import task_draft_store
from admin_notification_store import AdminNotificationStore
from config import is_admin
from support_store import (
    CATEGORIES,
    CATEGORY_LABELS,
    MAX_MESSAGE_LENGTH,
    STATUS_CLOSED,
    STATUS_OPEN,
    SupportInquiry,
    SupportMessage,
    SupportValidationError,
)

logger = logging.getLogger(__name__)

# AdminNotificationStore operation_type for support notifications —
# resolved as operation type + inquiry id + admin chat/message
# linkage, exactly like manual_proof (MT-ADMIN-03).
OPERATION_SUPPORT = "support"

# ── Callback payloads (untrusted lookup pointers ONLY) ────────────────
CB_CATEGORY = "supcat"  # supcat:<category id>
CB_ADMIN = "sup"        # sup:<op>:<positive int>
OP_OPEN = "open"
OP_REPLY = "reply"
OP_CLOSE = "close"
OP_CANCEL = "cancel"
OP_PAGE = "page"
_ADMIN_OPS = (OP_OPEN, OP_REPLY, OP_CLOSE, OP_CANCEL, OP_PAGE)

# ── Bounds (deterministic, bounded rendering) ─────────────────────────
PAGE_SIZE = 5
DETAIL_MESSAGE_WINDOW = 5  # messages shown in the admin detail view
NOTIFICATION_EXCERPT = 500  # message excerpt inside a notification
RENDER_EXCERPT = 200  # message excerpt inside admin views
LABEL_LIMIT = 64

# ── Arabic UX strings ─────────────────────────────────────────────────
MSG_ADMIN_ONLY = "⛔ هذا الأمر للمشرفين فقط."
MSG_NOT_USER_FLOW = "⛔ هذا الاختيار مخصص لطلبات المستخدمين."
MSG_INVALID = "⛔ طلب غير صالح."
MSG_INVALID_TEXT = (
    "⛔ نص الرسالة غير صالح: لا يمكن أن يكون فارغاً، "
    f"ويجب ألا يتجاوز {MAX_MESSAGE_LENGTH} حرف أو يحتوي رموز تحكم ممنوعة."
)
MSG_ERROR = "⛔ حدث خطأ، حاول مرة أخرى."

MSG_QUEUE_HEADER = "💬 طلبات الدعم"
MSG_NO_INQUIRIES = "📭 لا توجد طلبات دعم مفتوحة حالياً."

MSG_CATEGORY_HEADER = "💬 الدعم\n\nاختر نوع المشكلة:"
MSG_PROMPT = "✍️ اكتب رسالتك الآن — سيصلها فريق الدعم مباشرة."
MSG_HAS_OPEN = "ℹ️ لديك طلب مفتوح (#{inquiry_id}) — ستُضاف رسالتك إليه."
MSG_USER_CREATED = "✅ تم إنشاء طلب الدعم #{inquiry_id} وتم إشعار فريق الدعم."
MSG_USER_APPENDED = "✅ تمت إضافة رسالتك إلى طلبك المفتوح #{inquiry_id}."

MSG_INQUIRY_GONE = "⛔ الطلب غير موجود."
MSG_INQUIRY_CLOSED = "⛔ هذا الطلب مغلق — لا يمكن الرد عليه."
MSG_CLOSED_CARD = "✅ تم إغلاق الطلب #{inquiry_id}."
MSG_ALREADY_CLOSED = "✅ الطلب #{inquiry_id} مغلق مسبقاً."

MSG_REPLY_PROMPT = (
    "📝 الرد على الطلب #{inquiry_id}\n"
    "اكتب رسالتك الآن — ستُرسل إلى المستخدم مباشرة."
)
MSG_REPLY_SENT = "✅ تم إرسال الرد إلى المستخدم."
MSG_REPLY_DUPLICATE = "✅ هذا الرد محفوظ ومُسلَّم مسبقاً."
MSG_DELIVERY_FAILED = (
    "⚠️ تعذر تسليم الرد للمستخدم. الرد محفوظ في المحادثة "
    "ويمكنك إعادة المحاولة بإرساله مرة أخرى."
)
MSG_REPLY_CANCELLED = "✅ تم إلغاء وضع الرد."

BTN_OPEN = "فتح #{inquiry_id}"
BTN_REPLY = "💬 رد"
BTN_CLOSE = "✅ إغلاق"
BTN_CANCEL_REPLY = "❌ إلغاء الرد"
PAGE_NEXT = "التالي ▶️"
PAGE_PREV = "◀️ السابق"

STATUS_LABELS = {STATUS_OPEN: "مفتوح", STATUS_CLOSED: "مغلق"}
SENDER_LABELS = {"user": "المستخدم", "admin": "مشرف"}


def category_label(category: object) -> str:
    """Arabic label for a stored category id (raw id as fallback)."""
    if isinstance(category, str) and category in CATEGORY_LABELS:
        return CATEGORY_LABELS[category]
    return str(category)


def _excerpt(text: object, limit: int) -> str:
    """Bounded, control-free single-line-safe excerpt of user text."""
    if not isinstance(text, str):
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _safe_label(value: object, limit: int = LABEL_LIMIT) -> str:
    """Strip non-printable characters and bound *value* (display only)."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned.strip()[:limit]


def _live_user_label(user: object) -> str:
    """Safe user identifier for notifications (id + @username + name)."""
    uid = getattr(user, "id", None)
    label = str(uid) if isinstance(uid, int) and not isinstance(uid, bool) else "?"
    username = _safe_label(getattr(user, "username", None))
    if username:
        label += f" @{username.lstrip('@')}"
    first = _safe_label(getattr(user, "first_name", None))
    if first:
        label += f" ({first})"
    return label


def _stored_user_label(user_id: int) -> str:
    """Safe user identifier re-read from server state (admin views)."""
    label = str(user_id)
    try:
        row = db.get_user(user_id)
    except Exception:  # pragma: no cover - defensive
        row = None
    if isinstance(row, dict):
        username = _safe_label(row.get("username"))
        if username:
            label += f" @{username.lstrip('@')}"
        first = _safe_label(row.get("first_name"))
        if first:
            label += f" ({first})"
    return label


# ── Callback payload builders / parsers (ids only) ────────────────────


def category_callback_data(category: str) -> str:
    return f"{CB_CATEGORY}:{category}"


def admin_callback_data(op: str, ref: int) -> str:
    return f"{CB_ADMIN}:{op}:{ref}"


def parse_category_callback(data: object) -> str | None:
    """``supcat:<category>`` → validated category id (lookup pointer)."""
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != CB_CATEGORY:
        return None
    try:
        return support_store.validate_category(parts[1])
    except SupportValidationError:
        return None


def parse_admin_callback(data: object) -> tuple[str, int] | None:
    """``sup:<op>:<positive int>`` → (op, ref).  Untrusted ids only."""
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CB_ADMIN:
        return None
    op, raw = parts[1], parts[2]
    if op not in _ADMIN_OPS:
        return None
    if not (raw.isascii() and raw.isdigit()):
        return None
    ref = int(raw)
    if ref <= 0:
        return None
    return op, ref


# ── Rendering (pure, deterministic, bounded) ──────────────────────────


def build_category_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                CATEGORY_LABELS[cat],
                callback_data=category_callback_data(cat),
            )
        ]
        for cat in CATEGORIES
    ]
    return InlineKeyboardMarkup(rows)


def build_queue_page(
    inquiries: list[SupportInquiry], page: int = 1
) -> tuple[str, InlineKeyboardMarkup | None]:
    """One bounded queue page (oldest first, ``PAGE_SIZE`` per page).

    Untrusted page ids are clamped into the valid range.
    """
    total = len(inquiries)
    if total == 0:
        return MSG_NO_INQUIRIES, None

    max_page = max(1, -(-total // PAGE_SIZE))  # ceil division
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = min(max(1, page), max_page)
    start = (page - 1) * PAGE_SIZE
    chunk = inquiries[start : start + PAGE_SIZE]

    header = MSG_QUEUE_HEADER
    if total > PAGE_SIZE:
        header += f" ({start + 1}–{start + len(chunk)} من {total})"

    lines = [header, ""]
    for inq in chunk:
        lines.append(f"#{inq.id} — {category_label(inq.category)}")

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                BTN_OPEN.format(inquiry_id=inq.id),
                callback_data=admin_callback_data(OP_OPEN, inq.id),
            )
        ]
        for inq in chunk
    ]
    if max_page > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 1:
            nav.append(
                InlineKeyboardButton(
                    PAGE_PREV,
                    callback_data=admin_callback_data(OP_PAGE, page - 1),
                )
            )
        if page < max_page:
            nav.append(
                InlineKeyboardButton(
                    PAGE_NEXT,
                    callback_data=admin_callback_data(OP_PAGE, page + 1),
                )
            )
        if nav:
            rows.append(nav)

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_detail_text(
    inquiry: SupportInquiry,
    messages: list[SupportMessage],
    truncated: bool = False,
) -> str:
    """Admin inquiry view: id, category, user, status, conversation."""
    lines = [
        f"📩 طلب دعم #{inquiry.id}",
        f"النوع: {category_label(inquiry.category)}",
        f"المستخدم: {_stored_user_label(inquiry.user_id)}",
        f"الحالة: {STATUS_LABELS.get(inquiry.status, inquiry.status)}",
        "",
    ]
    if not messages:
        lines.append("📭 لا توجد رسائل بعد.")
        return "\n".join(lines)
    note = (
        f"— المحادثة (أحدث {DETAIL_MESSAGE_WINDOW} رسائل) —"
        if truncated
        else "— المحادثة —"
    )
    lines.append(note)
    for msg in messages:
        who = SENDER_LABELS.get(msg.sender_type, msg.sender_type)
        lines.append(f"[{who}]: {_excerpt(msg.message, RENDER_EXCERPT)}")
    return "\n".join(lines)


def build_inquiry_keyboard(
    inquiry: SupportInquiry,
) -> InlineKeyboardMarkup | None:
    """[ 💬 رد ] [ ✅ إغلاق ] — only while the inquiry is open."""
    if not inquiry.is_open:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_REPLY,
                    callback_data=admin_callback_data(OP_REPLY, inquiry.id),
                ),
                InlineKeyboardButton(
                    BTN_CLOSE,
                    callback_data=admin_callback_data(OP_CLOSE, inquiry.id),
                ),
            ]
        ]
    )


def build_reply_keyboard(inquiry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_CANCEL_REPLY,
                    callback_data=admin_callback_data(OP_CANCEL, inquiry_id),
                )
            ]
        ]
    )


def build_notification_text(
    inquiry: SupportInquiry,
    message: SupportMessage,
    user_label: str = "",
) -> str:
    """Admin notification — SAFE operational fields only.

    inquiry id, category label, user identifier, status and a bounded
    excerpt of the message text.  Never a token, initData, password,
    task_data, wallet/ledger data or unrelated user data.
    """
    label = _safe_label(user_label) or str(inquiry.user_id)
    return "\n".join(
        [
            f"🔔 تنبيه دعم #{inquiry.id}",
            f"النوع: {category_label(inquiry.category)}",
            f"المستخدم: {label}",
            f"الحالة: {STATUS_LABELS.get(inquiry.status, inquiry.status)}",
            "الرسالة:",
            _excerpt(message.message, NOTIFICATION_EXCERPT),
        ]
    )


def build_notification_keyboard(inquiry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_OPEN.format(inquiry_id=inquiry_id),
                    callback_data=admin_callback_data(OP_OPEN, inquiry_id),
                )
            ]
        ]
    )


def _delivery_text(inquiry_id: int, text: str) -> str:
    """Plain-text envelope for the admin reply sent to the user."""
    return f"📩 رد فريق الدعم على الطلب #{inquiry_id}:\n\n{text}"


def _detail_messages(inquiry_id: int) -> tuple[list[SupportMessage], bool]:
    rows = support_store.list_messages(
        inquiry_id, DETAIL_MESSAGE_WINDOW + 1
    )
    if len(rows) > DETAIL_MESSAGE_WINDOW:
        return rows[-DETAIL_MESSAGE_WINDOW:], True
    return rows, False


# ── Delivery binding (bot.py wires the AdminNotifier once) ────────────

# Bound once by bot.py when the Telegram loop starts; None before that
# (and after shutdown) so notifications fail soft instead of failing
# the user's submission.  No conversation state lives here.
_notifier = None


def bind(notifier) -> None:
    """Attach the shared AdminNotifier (bot.py)."""
    global _notifier
    if notifier is None:
        raise ValueError("notifier must not be None")
    _notifier = notifier
    logger.info("Support service bound to the AdminNotifier")


def unbind() -> None:
    """Detach the transport (bot shutdown) — notifications fail soft."""
    global _notifier
    _notifier = None
    logger.info("Support service unbound")


def is_bound() -> bool:
    return _notifier is not None


async def notify_admins(
    inquiry: SupportInquiry,
    message: SupportMessage,
    user_label: str = "",
) -> list[tuple[int, Optional[int]]]:
    """Notify configured ADMINS about one support message (fail-soft).

    Uses the existing AdminNotifier (ADMINS-only targeting is enforced
    inside the notifier) and persists the operation → admin message
    linkage, so a callback can always resolve the inquiry server-side.
    """
    notifier = _notifier
    if notifier is None:
        logger.info(
            "Support notification skipped (service not bound): inquiry=%d",
            inquiry.id,
        )
        return []
    try:
        delivered = await notifier.notify_system(
            build_notification_text(inquiry, message, user_label),
            reply_markup=build_notification_keyboard(inquiry.id),
        )
    except Exception:
        logger.exception(
            "Support notification delivery failed: inquiry=%d", inquiry.id
        )
        return []
    for chat_id, message_id in delivered:
        if not isinstance(message_id, int) or message_id <= 0:
            logger.warning(
                "No message id for chat %s — linkage skipped: inquiry=%d",
                chat_id,
                inquiry.id,
            )
            continue
        try:
            AdminNotificationStore.create_linkage(
                OPERATION_SUPPORT, inquiry.id, chat_id, message_id
            )
        except Exception:
            logger.exception(
                "Support notification linkage failed: inquiry=%d chat=%s",
                inquiry.id,
                chat_id,
            )
    return delivered


# ── Local async safety wrappers (transport failures never crash) ──────


async def _safe_answer(query, text: str | None) -> None:
    try:
        if text:
            await query.answer(text=text)
        else:
            await query.answer()
    except Exception:
        logger.debug("Could not answer support callback", exc_info=True)


async def _safe_edit(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        logger.debug("Could not edit support message", exc_info=True)


def _non_private_chat(update) -> bool:
    """True only when *update* positively targets a group/channel.

    MT-ADMIN-02 isolation semantics, inlined to keep this module free
    of any import cycle with bot.py: unknown chat types are NOT
    treated as groups.
    """
    chat = getattr(update, "effective_chat", None)
    chat_type = getattr(chat, "type", None)
    return isinstance(chat_type, str) and chat_type != "private"


def _actor_id(value: object) -> int | None:
    """A trusted positive int id, or None (untrusted identities die here)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _private_chat_id(update, actor: int) -> int:
    """Private chat id; falls back to the actor (private chat == user)."""
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if isinstance(chat_id, int) and not isinstance(chat_id, bool):
        return chat_id
    return actor


def _source_message_id(message) -> int | None:
    """The sender's Telegram message id (replay guard), when resolvable."""
    return _actor_id(getattr(message, "message_id", None))


def _wizard_owns_text(actor: int) -> bool:
    """True when the /addtask wizard is waiting for this admin's text.

    The wizard's catch-all text handler runs BEFORE this module's
    handler (bot.py group 0 vs group 2); when a draft sits on a
    text-input step, the wizard owns the message and the support reply
    path stays silent — one deterministic owner per message.
    """
    try:
        draft = task_draft_store.get_open_draft(actor)
    except Exception:  # pragma: no cover - defensive
        return False
    return draft is not None and draft.step in admin_task_wizard.TEXT_INPUT_STEPS


# ── PTB handler: /support ─────────────────────────────────────────────


async def support_command(update, context) -> None:
    """``/support`` — private admin → queue; private non-admin → flow.

    Group/channel invocations produce ZERO replies (MT-ADMIN-02).
    """
    if _non_private_chat(update):
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    actor = _actor_id(getattr(getattr(update, "effective_user", None), "id", None))
    if actor is None:
        return

    # Admin → the support queue (bounded, oldest first).
    if is_admin(actor):
        try:
            inquiries = support_store.list_open_inquiries()
        except Exception:
            logger.exception("Support queue load failed: actor=%d", actor)
            await message.reply_text(MSG_ERROR)
            return
        text, markup = build_queue_page(inquiries, 1)
        await message.reply_text(text, reply_markup=markup)
        logger.info(
            "Support queue shown: actor=%d inquiries=%d",
            actor, len(inquiries),
        )
        return

    # User → category selection (state persisted, nothing in memory).
    try:
        support_store.clear_pending_category(actor)
        active = support_store.get_active_inquiry(actor)
    except Exception:
        logger.exception("Support entry failed: user=%d", actor)
        await message.reply_text(MSG_ERROR)
        return
    text = MSG_CATEGORY_HEADER
    if active is not None:
        text += "\n" + MSG_HAS_OPEN.format(inquiry_id=active.id)
    await message.reply_text(text, reply_markup=build_category_keyboard())


# ── PTB handler: category buttons ─────────────────────────────────────


async def support_category_callback(update, context) -> None:
    """``supcat:<category>`` — persist the user's category selection."""
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    if _non_private_chat(update):
        await _safe_answer(query, None)
        return
    category = parse_category_callback(getattr(query, "data", None))
    if category is None:
        await _safe_answer(query, MSG_INVALID)
        return
    actor = _actor_id(getattr(getattr(query, "from_user", None), "id", None))
    if actor is None:
        await _safe_answer(query, MSG_INVALID)
        return
    if is_admin(actor):
        await _safe_answer(query, MSG_NOT_USER_FLOW)
        return
    try:
        support_store.set_pending_category(actor, category)
        active = support_store.get_active_inquiry(actor)
    except Exception:
        logger.exception(
            "Support category selection failed: user=%d cat=%s",
            actor, category,
        )
        await _safe_answer(query, MSG_ERROR)
        return
    prompt = MSG_PROMPT
    if active is not None:
        prompt += "\n" + MSG_HAS_OPEN.format(inquiry_id=active.id)
    await _safe_answer(query, None)
    await _safe_edit(query, prompt, None)
    logger.info(
        "Support category selected: user=%d category=%s", actor, category
    )


# ── PTB handler: private text (user message OR admin reply) ───────────


async def support_text_input(update, context) -> None:
    """Route one private text message into the right persisted flow.

    Registered by bot.py in its OWN handler group with
    ``TEXT & ~COMMAND & PRIVATE`` so it is never shadowed by the
    group-0 wizard catch-all and never shadows it either.  The body is
    silent unless the sender's OWN persisted state matches:
    - admins → only with an active reply context (and only when the
      /addtask wizard is not waiting for their text);
    - users  → only with a pending category selection.
    """
    if _non_private_chat(update):
        return
    message = getattr(update, "message", None)
    text = getattr(message, "text", None)
    if not isinstance(text, str):
        return
    if text.startswith("/"):
        return
    actor = _actor_id(getattr(getattr(update, "effective_user", None), "id", None))
    if actor is None:
        return

    if is_admin(actor):
        if _wizard_owns_text(actor):
            return
        await _handle_admin_reply_text(update, context, actor, message, text)
        return
    await _handle_user_text(update, context, actor, message, text)


async def _handle_user_text(update, context, actor, message, text) -> None:
    """Append one validated user message to their support flow."""
    try:
        pending = support_store.get_pending_category(actor)
    except Exception:
        logger.exception("Support pending state read failed: user=%d", actor)
        await message.reply_text(MSG_ERROR)
        return
    if pending is None:
        return  # not in the support flow — ordinary chat stays untouched

    try:
        body = support_store.validate_message_text(text)
    except SupportValidationError:
        await message.reply_text(MSG_INVALID_TEXT)
        return  # stay in the flow; nothing is persisted

    try:
        submission = support_store.submit_user_message(
            actor, pending, body, _source_message_id(message)
        )
    except Exception:
        logger.exception("Support submission failed: user=%d", actor)
        await message.reply_text(MSG_ERROR)
        return

    # A replayed Telegram update appends nothing and must not
    # re-notify admins — the original submission already did.
    if not submission.duplicate:
        user = getattr(update, "effective_user", None)
        await notify_admins(
            submission.inquiry, submission.message, _live_user_label(user)
        )

    if submission.created:
        reply = MSG_USER_CREATED.format(inquiry_id=submission.inquiry.id)
    else:
        reply = MSG_USER_APPENDED.format(inquiry_id=submission.inquiry.id)
    await message.reply_text(reply)
    logger.info(
        "Support message accepted: user=%d inquiry=%d created=%s "
        "duplicate=%s",
        actor, submission.inquiry.id, submission.created,
        submission.duplicate,
    )


async def _handle_admin_reply_text(update, context, actor, message, text) -> None:
    """Send one persisted admin reply to the inquiry's user.

    The recipient comes ONLY from the server-side inquiry row; the
    reply context is resolved from the DB by admin id + private chat
    id.  The message is persisted BEFORE delivery, so a Telegram
    failure never loses it — the admin gets an operational error and a
    safe retry (re-sending) works.
    """
    try:
        ctx = support_store.get_reply_context(actor, _private_chat_id(update, actor))
    except Exception:
        logger.exception("Support reply context read failed: admin=%d", actor)
        await message.reply_text(MSG_ERROR)
        return
    if ctx is None:
        return  # not answering anything — ordinary admin chat is silent

    try:
        inquiry = support_store.get_inquiry(ctx.inquiry_id)
    except Exception:
        logger.exception(
            "Support inquiry read failed: inquiry=%d", ctx.inquiry_id
        )
        await message.reply_text(MSG_ERROR)
        return
    if inquiry is None:
        support_store.clear_reply_context(actor)
        await message.reply_text(MSG_INQUIRY_GONE)
        return
    if not inquiry.is_open:
        support_store.clear_reply_context(actor, inquiry.id)
        await message.reply_text(MSG_INQUIRY_CLOSED)
        return

    try:
        body = support_store.validate_message_text(text)
    except SupportValidationError:
        await message.reply_text(MSG_INVALID_TEXT)
        return

    try:
        submission = support_store.append_admin_message(
            inquiry.id, actor, body, _source_message_id(message)
        )
    except Exception:
        logger.exception(
            "Support reply persist failed: inquiry=%d admin=%d",
            inquiry.id, actor,
        )
        await message.reply_text(MSG_ERROR)
        return
    if submission is None:
        # Raced with a close — stale reply mutates nothing.
        support_store.clear_reply_context(actor, inquiry.id)
        await message.reply_text(MSG_INQUIRY_CLOSED)
        return

    if submission.duplicate and submission.message.delivered:
        await message.reply_text(MSG_REPLY_DUPLICATE)
        return

    # Recipient = the persistent inquiry's user (never client data).
    try:
        await context.bot.send_message(
            chat_id=inquiry.user_id,
            text=_delivery_text(inquiry.id, body),
        )
    except Exception:
        logger.warning(
            "Support reply delivery failed: inquiry=%d message=%d "
            "recipient=%d (message preserved, undelivered)",
            inquiry.id, submission.message.id, inquiry.user_id,
            exc_info=True,
        )
        await message.reply_text(MSG_DELIVERY_FAILED)
        return  # delivered_at stays NULL → a resend is a safe retry

    try:
        support_store.mark_message_delivered(submission.message.id)
    except Exception:  # pragma: no cover - delivery already happened
        logger.exception(
            "Could not stamp delivery: message=%d", submission.message.id
        )
    await message.reply_text(MSG_REPLY_SENT)
    logger.info(
        "Support reply delivered: inquiry=%d admin=%d recipient=%d",
        inquiry.id, actor, inquiry.user_id,
    )


# ── PTB handler: admin callbacks (sup:<op>:<id>) ──────────────────────


async def support_admin_callback(update, context) -> None:
    """Open / reply / close / cancel / page — all server-side re-reads.

    Callback data is untrusted: it carries only a positive integer id.
    The actor must be a config admin; the inquiry, its status and the
    recipient are always re-read from SQLite.
    """
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    if _non_private_chat(update):
        await _safe_answer(query, None)
        return
    parsed = parse_admin_callback(getattr(query, "data", None))
    if parsed is None:
        await _safe_answer(query, MSG_INVALID)
        return
    actor = _actor_id(getattr(getattr(query, "from_user", None), "id", None))
    if actor is None or not is_admin(actor):
        await _safe_answer(query, MSG_ADMIN_ONLY)
        return
    op, ref = parsed
    chat_id = _private_chat_id(update, actor)

    try:
        if op == OP_PAGE:
            inquiries = support_store.list_open_inquiries()
            text, markup = build_queue_page(inquiries, ref)
            await _safe_answer(query, None)
            await _safe_edit(query, text, markup)
            return

        inquiry = support_store.get_inquiry(ref)

        if op == OP_OPEN:
            if inquiry is None:
                await _safe_answer(query, MSG_INQUIRY_GONE)
                return
            messages, truncated = _detail_messages(inquiry.id)
            await _safe_answer(query, None)
            await _safe_edit(
                query,
                build_detail_text(inquiry, messages, truncated),
                build_inquiry_keyboard(inquiry),
            )
            return

        if op == OP_REPLY:
            if inquiry is None:
                await _safe_answer(query, MSG_INQUIRY_GONE)
                return
            if not inquiry.is_open:
                await _safe_answer(query, MSG_INQUIRY_CLOSED)
                return
            ctx = support_store.set_reply_context(actor, chat_id, inquiry.id)
            if ctx is None:
                # Raced with a close — nothing was stored.
                await _safe_answer(query, MSG_INQUIRY_CLOSED)
                return
            await _safe_answer(query, None)
            await _safe_edit(
                query,
                MSG_REPLY_PROMPT.format(inquiry_id=inquiry.id),
                build_reply_keyboard(inquiry.id),
            )
            logger.info(
                "Support reply context set: admin=%d inquiry=%d",
                actor, inquiry.id,
            )
            return

        if op == OP_CLOSE:
            if inquiry is None:
                await _safe_answer(query, MSG_INQUIRY_GONE)
                return
            result = support_store.close_inquiry(inquiry.id)
            if result is None:
                await _safe_answer(query, MSG_INQUIRY_GONE)
                return
            await _safe_answer(
                query,
                None if result else MSG_ALREADY_CLOSED.format(
                    inquiry_id=inquiry.id
                ),
            )
            await _safe_edit(
                query,
                MSG_CLOSED_CARD.format(inquiry_id=inquiry.id),
                None,
            )
            return

        # OP_CANCEL — drop the persisted reply context (idempotent).
        cleared = support_store.clear_reply_context(actor, inquiry_id=ref)
        await _safe_answer(
            query, MSG_REPLY_CANCELLED if cleared else MSG_INQUIRY_GONE
        )
        if inquiry is not None:
            messages, truncated = _detail_messages(inquiry.id)
            await _safe_edit(
                query,
                build_detail_text(inquiry, messages, truncated),
                build_inquiry_keyboard(inquiry),
            )
        return
    except Exception:
        logger.exception(
            "Support callback failed: op=%s ref=%s actor=%s", op, ref, actor
        )
        await _safe_answer(query, MSG_ERROR)
