"""
Withdrawal Admin Review & Settlement (MT-ADMIN-27)
==================================================

Admin-side review workflow for the withdrawal requests users create
through the Mini App — **discovery, inspection and confirmation only**.
Every financial mutation goes through the existing production
``WithdrawalService``; this module never touches the wallet, the ledger
or the ``withdrawal_requests`` table itself.

Architecture::

    Admin Telegram private chat
      ↓
    /withdrawals                       ← bounded pending queue (read-only)
      ↓
    SqliteWithdrawalRepository.list_pending() / .get()
      🔍 مراجعة #<short id>  (wd:view:<request id> — opaque lookup id)
      ↓
    detail card — trusted persisted facts (user_destination shown;
      pm_destination NEVER shown)
      ↓
    ✅ إتمام السحب / ❌ رفض السحب        (wd:askc:/wd:askr: — confirm card)
      ↓ confirmation press
    wd:doc:<id> / wd:dor:<id>
      ↓
    WithdrawalService.complete/reject  ← SOLE mutation path (ONE
      transaction: CAS pending→terminal, exact persisted
      wallet_debit_units, wallet + ledger settlement/release,
      completed_at/rejected_at)

Boundaries (this module must NOT):
- open the financial transaction (the handler only authenticates,
  loads/displays, confirms, delegates and formats);
- calculate or display-in-callback any debit/fee/rate — amounts come
  from the persisted row and stay out of callback payloads;
- mutate wallet/ledger/withdrawal_requests directly or bypass the
  service CAS/state protections;
- read current fee/rate/minimum settings or a current RateQuote for a
  complete/reject — settlement is the persisted ``wallet_debit_units``
  ONLY;
- expose ``pm_destination`` (the platform's own destination) or log
  any destination/secret;
- add a second authorization mechanism (``config.is_admin`` only),
  a user-side approve/reject path, or a new persistence layer.

Callback safety: ``wd:<op>:<ref>`` payloads carry ONLY an operation
word, a positive page number, or the opaque request id — everything
else (actor, row, status, every displayed fact) is re-read server-side
from SQLite.  Destinations and financial blobs never enter callback
data.

Determinism: the list has a single explicit ORDER BY (repository
``created_at DESC, request_id``) and a fixed page size, so identical
data always renders identically; untrusted page ids are clamped.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import db
import withdrawal_rules
import withdrawal_service
from config import is_admin
from withdrawal_service import MissingWalletDebitError
from withdrawal_store import SqliteWithdrawalRepository

logger = logging.getLogger(__name__)

# ── Callback payloads (untrusted lookup pointers ONLY) ────────────────
CB_PREFIX = "wd"
OP_PAGE = "page"
OP_VIEW = "view"
OP_ASK_COMPLETE = "askc"
OP_ASK_REJECT = "askr"
OP_DO_COMPLETE = "doc"
OP_DO_REJECT = "dor"
_OPS_WITH_PAGE = frozenset({OP_PAGE})
_OPS_WITH_ID = frozenset(
    {OP_VIEW, OP_ASK_COMPLETE, OP_ASK_REJECT, OP_DO_COMPLETE, OP_DO_REJECT}
)
_ALL_OPS = _OPS_WITH_PAGE | _OPS_WITH_ID

# ── Bounds (bounded, deterministic) ───────────────────────────────────

# Small fixed page size: /withdrawals never produces a giant message
# (same convention as the /reviews and payment-method queues).
PAGE_SIZE = 5

# Opaque request id bound (uuid4().hex = 32; generous head-room, still
# strictly bounded so forged payloads cannot smuggle arbitrary text).
_MAX_REQUEST_ID_LEN = 64

# ── Arabic UI strings ────────────────────────────────────────────────
MSG_ADMIN_ONLY = "⛔ هذا الأمر للمشرفين فقط."
MSG_INVALID = "⛔ طلب غير صالح."
MSG_NOT_FOUND = "⛔ طلب السحب غير موجود."
MSG_STATE_CHANGED = "⛔ تغيرت حالة الطلب — لم يتم تنفيذ أي عملية."
MSG_LEGACY = (
    "⛔ بيانات هذا الطلب غير مكتملة — لا يمكن تنفيذ العملية "
    "تلقائياً. راجع السجلات يدوياً."
)
MSG_INSUFFICIENT = (
    "⛔ الرصيد المحجوز لا يغطي العملية — لم يتم تغيير أي شيء."
)
MSG_ERROR = "⛔ حدث خطأ، حاول مرة أخرى."

# Best-effort post-commit notices to the REQUESTER (safe facts only:
# short id + status — never destinations, never credentials).
MSG_USER_COMPLETED = (
    "✅ تم إتمام طلب السحب الخاص بك.\n"
    "رقم الطلب: #{short_id}\n"
    "الحالة: مكتمل"
)
MSG_USER_REJECTED = (
    "❌ تم رفض طلب السحب الخاص بك.\n"
    "رقم الطلب: #{short_id}\n"
    "الحالة: مرفوض — المبلغ المحجوز أُعيد إلى رصيدك."
)

LIST_HEADER = "💸 طلبات السحب المعلقة"
MSG_NO_PENDING = "📭 لا توجد طلبات سحب معلقة حالياً."

DETAIL_HEADER = "🧾 مراجعة طلب سحب"
CONFIRM_COMPLETE_HEADER = "⚠️ تأكيد إتمام السحب"
CONFIRM_REJECT_HEADER = "⚠️ تأكيد رفض السحب"

BTN_VIEW = "🔍 مراجعة #{short_id}"
BTN_COMPLETE = "✅ إتمام السحب"
BTN_REJECT = "❌ رفض السحب"
BTN_CONFIRM_COMPLETE = "✅ تأكيد الإتمام"
BTN_CONFIRM_REJECT = "✅ تأكيد الرفض"
BTN_BACK = "⬅️ رجوع"
BTN_LIST = "⬅️ القائمة"
PAGE_NEXT = "التالي ▶️"
PAGE_PREV = "◀️ السابق"

_STATUS_LABEL = {
    "pending": "⏳ قيد الانتظار",
    "completed": "✅ مكتمل",
    "rejected": "❌ مرفوض",
}


def status_label(status: object) -> str:
    key = getattr(status, "value", status)
    return _STATUS_LABEL.get(str(key), str(key))


# ── Callback payload helpers ──────────────────────────────────────────


def page_callback_data(page: int) -> str:
    return f"{CB_PREFIX}:{OP_PAGE}:{page}"


def view_callback_data(request_id: str) -> str:
    return f"{CB_PREFIX}:{OP_VIEW}:{request_id}"


def ask_complete_callback_data(request_id: str) -> str:
    return f"{CB_PREFIX}:{OP_ASK_COMPLETE}:{request_id}"


def ask_reject_callback_data(request_id: str) -> str:
    return f"{CB_PREFIX}:{OP_ASK_REJECT}:{request_id}"


def do_complete_callback_data(request_id: str) -> str:
    return f"{CB_PREFIX}:{OP_DO_COMPLETE}:{request_id}"


def do_reject_callback_data(request_id: str) -> str:
    return f"{CB_PREFIX}:{OP_DO_REJECT}:{request_id}"


def parse_callback(data: object) -> tuple[str, object] | None:
    """``wd:<op>:<ref>`` → ``(op, ref)``.  None for anything else.

    ``ref`` is a positive int for ``page`` and a bounded opaque
    request id (ASCII alnum / ``-`` / ``_``) for every other op — a
    lookup pointer ONLY; no destination, amount or other fact.
    """
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CB_PREFIX:
        return None
    op, ref = parts[1], parts[2]
    if op not in _ALL_OPS:
        return None
    if op in _OPS_WITH_PAGE:
        if not (ref.isascii() and ref.isdigit()):
            return None
        page = int(ref)
        return (op, page) if page > 0 else None
    if not 1 <= len(ref) <= _MAX_REQUEST_ID_LEN:
        return None
    if not all(
        char.isascii() and (char.isalnum() or char in "-_")
        for char in ref
    ):
        return None
    return op, ref


def _short_id(request_id: str) -> str:
    return request_id[:8]


def _fmt_when(value: object) -> str:
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return str(value)


def _method_label(request) -> str:
    return request.pm_display_name or request.method


def _user_who(user_id: int) -> str:
    """Trusted user data (username / first name) — never a secret."""
    try:
        user = db.get_user(user_id)
    except Exception:  # pragma: no cover - display must never break
        logger.debug("User lookup failed: user=%s", user_id, exc_info=True)
        user = None
    if not isinstance(user, dict):
        return "—"
    username = user.get("username")
    if isinstance(username, str) and username:
        return f"@{username}"
    first_name = user.get("first_name")
    if isinstance(first_name, str) and first_name:
        return first_name
    return "—"


# ── Rendering (Arabic, bounded) ───────────────────────────────────────


def build_list_page(
    requests, page: int = 1
) -> tuple[str, InlineKeyboardMarkup | None]:
    """One bounded page of the pending queue (deterministic).

    Empty queue → the concise Arabic empty state with no keyboard.
    ``page`` is clamped into the valid range, so untrusted page ids
    can never address out-of-range data.
    """
    total = len(requests)
    if total == 0:
        return MSG_NO_PENDING, None

    max_page = max(1, -(-total // PAGE_SIZE))  # ceil division
    page = min(max(1, int(page)), max_page)
    start = (page - 1) * PAGE_SIZE
    chunk = requests[start : start + PAGE_SIZE]

    header = LIST_HEADER
    if total > PAGE_SIZE:
        header += f" ({start + 1}–{start + len(chunk)} من {total})"

    lines = [header]
    rows: list[list[InlineKeyboardButton]] = []
    for index, request in enumerate(chunk, start=1):
        short_id = _short_id(request.request_id)
        lines.append("")
        lines.append(f"{index}. #{short_id} — المستخدم {request.user_id}")
        lines.append(
            f"   {_method_label(request)} — {request.amount_egp} EGP"
        )
        lines.append(
            f"   {_fmt_when(request.created_at)} — "
            f"{status_label(request.status)}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    BTN_VIEW.format(short_id=short_id),
                    callback_data=view_callback_data(request.request_id),
                )
            ]
        )

    if max_page > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 1:
            nav.append(
                InlineKeyboardButton(
                    PAGE_PREV, callback_data=page_callback_data(page - 1)
                )
            )
        if page < max_page:
            nav.append(
                InlineKeyboardButton(
                    PAGE_NEXT, callback_data=page_callback_data(page + 1)
                )
            )
        if nav:
            rows.append(nav)

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_detail_text(request) -> str:
    """The review card — ONLY trusted persisted facts.

    Shows the USER's payout destination (what the admin must manually
    pay).  ``pm_destination`` — the platform's own destination — is
    never rendered.
    """
    if request.wallet_debit_units is None:
        debit = "غير محدد (سجل قديم)"
    else:
        debit = str(request.wallet_debit_units)

    lines = [
        DETAIL_HEADER,
        "━━━━━━━━━━━━━━━━",
        f"رقم الطلب: {request.request_id}",
        f"المستخدم: {request.user_id} — {_user_who(request.user_id)}",
        f"الطريقة: {_method_label(request)}",
        f"الأصل: {request.pm_asset or '—'}",
        f"الشبكة: {request.pm_network or '—'}",
        f"المزود: {request.pm_provider or '—'}",
        f"المبلغ المطلوب: {request.amount_egp} EGP",
        f"الرسوم: {request.fee_egp} EGP",
        f"خصم المحفظة: {debit}",
        f"وجهة دفع المستخدم: {request.user_destination or '—'}",
        f"الحالة: {status_label(request.status)}",
        f"تاريخ الإنشاء: {request.created_at}",
        f"السعر المحفوظ (EGP/USDT): {request.wallet_rate_usdt_egp} — "
        f"{request.rate_provider}",
        f"تاريخ التقاط السعر: {request.rate_captured_at}",
    ]
    if request.rate_usdt_egp is not None:
        lines.append(f"السعر المرجعي وقت الطلب: {request.rate_usdt_egp}")
    return "\n".join(lines)


def build_detail_keyboard(request) -> InlineKeyboardMarkup:
    """✅/❌ action buttons only while the request is pending; the
    ⬅️ رجوع button always returns to the bounded list."""
    rows: list[list[InlineKeyboardButton]] = []
    if str(getattr(request.status, "value", request.status)) == "pending":
        rows.append(
            [
                InlineKeyboardButton(
                    BTN_COMPLETE,
                    callback_data=ask_complete_callback_data(
                        request.request_id
                    ),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    BTN_REJECT,
                    callback_data=ask_reject_callback_data(
                        request.request_id
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                BTN_BACK, callback_data=page_callback_data(1)
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_confirm_text(request, *, complete: bool) -> str:
    header = CONFIRM_COMPLETE_HEADER if complete else CONFIRM_REJECT_HEADER
    action = (
        "إتمام السحب يعتبر تأكيداً منك على أن المبلغ تم دفعه للمستخدم "
        "يدوياً؛ ستُثبت الحالة «مكتمل» ويُخصم المبلغ المحجوز المحفوظ "
        "نهائياً."
        if complete
        else "رفض السحب سيُثبت الحالة «مرفوض» وسيُعاد المبلغ المحجوز "
        "المحفوظ إلى رصيد المستخدم."
    )
    return (
        f"{header}\n"
        "━━━━━━━━━━━━━━━━\n"
        f"رقم الطلب: {request.request_id}\n"
        f"المبلغ: {request.amount_egp} EGP\n\n"
        f"{action}"
    )


def build_confirm_keyboard(request, *, complete: bool) -> InlineKeyboardMarkup:
    if complete:
        confirm = InlineKeyboardButton(
            BTN_CONFIRM_COMPLETE,
            callback_data=do_complete_callback_data(request.request_id),
        )
    else:
        confirm = InlineKeyboardButton(
            BTN_CONFIRM_REJECT,
            callback_data=do_reject_callback_data(request.request_id),
        )
    back = InlineKeyboardButton(
        BTN_BACK, callback_data=view_callback_data(request.request_id)
    )
    return InlineKeyboardMarkup([[confirm], [back]])


def _success_text(action: str, request_id: str) -> str:
    if action == "complete":
        return (
            f"✅ تمت إتمام السحب بنجاح.\n"
            f"رقم الطلب: {request_id}\n"
            f"الحالة: {_STATUS_LABEL['completed']}"
        )
    return (
        f"✅ تم رفض السحب بنجاح.\n"
        f"رقم الطلب: {request_id}\n"
        f"الحالة: {_STATUS_LABEL['rejected']}"
    )


def _list_again_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_LIST, callback_data=page_callback_data(1)
                )
            ]
        ]
    )


# ── The ONE production mutation boundary ──────────────────────────────


async def _notify_user(context, request, action: str) -> None:
    """Best-effort post-commit notice to the requester.

    Runs strictly AFTER the service transaction committed — a delivery
    failure is logged and swallowed, so it can never roll back or
    repeat the financial operation.  The text carries safe facts only
    (short id + status).
    """
    try:
        template = (
            MSG_USER_COMPLETED if action == "complete"
            else MSG_USER_REJECTED
        )
        text = template.format(short_id=str(request.request_id)[:8])
        await context.bot.send_message(
            chat_id=request.user_id, text=text
        )
    except Exception:
        logger.exception(
            "Withdrawal user notification failed: request=%s action=%s",
            getattr(request, "request_id", "?"),
            action,
        )


def _service() -> withdrawal_service.WithdrawalService:
    """The existing production service — the sole financial path.

    Handlers never construct wallet/ledger/repository adapters and
    never open ``db.transaction()`` themselves.
    """
    return withdrawal_service.WithdrawalService()


# ── Local async safety wrappers ───────────────────────────────────────


async def _safe_answer(query, text: str | None) -> None:
    try:
        if text:
            await query.answer(text=text)
        else:
            await query.answer()
    except Exception:
        logger.debug("Could not answer withdrawal callback", exc_info=True)


async def _safe_edit(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        logger.debug("Could not edit withdrawal message", exc_info=True)


def _non_private_chat(update) -> bool:
    """True only when *update* positively targets a group/channel.

    MT-ADMIN-02 isolation semantics, inlined (no import cycle with
    bot.py): unknown chat types are NOT treated as groups.
    """
    chat = getattr(update, "effective_chat", None)
    chat_type = getattr(chat, "type", None)
    return isinstance(chat_type, str) and chat_type != "private"


def _actor_id(value: object) -> int | None:
    """A trusted positive int id, or None (untrusted identities die)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


# ── PTB handlers ──────────────────────────────────────────────────────


async def withdrawals_command(update, context) -> None:
    """``/withdrawals`` — admin-only pending withdrawal queue.

    Registered by bot.py as ``CommandHandler("withdrawals", ...)``.
    Private chats only: a group/channel invocation produces ZERO
    replies.  Non-admins get the standard admin-only refusal and
    never see queue data.
    """
    if _non_private_chat(update):
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    actor = _actor_id(
        getattr(getattr(update, "effective_user", None), "id", None)
    )
    if actor is None:
        return
    if not is_admin(actor):
        await message.reply_text(MSG_ADMIN_ONLY)
        return

    try:
        requests = SqliteWithdrawalRepository().list_pending()
    except Exception:
        logger.exception("Withdrawal queue load failed: admin=%d", actor)
        await message.reply_text(MSG_ERROR)
        return

    text, markup = build_list_page(requests, 1)
    await message.reply_text(text, reply_markup=markup)
    logger.info(
        "Withdrawal queue shown: admin=%d pending=%d",
        actor, len(requests),
    )


async def withdrawal_callback(update, context) -> None:
    """``wd:`` callbacks — private admin chat ONLY, server re-reads all.

    The payload is an opaque lookup pointer (op + request id / page);
    actor authorization, the row and every displayed/validated fact
    come from SQLite.  ``doc``/``dor`` are the ONLY mutation presses
    and delegate to ``WithdrawalService.complete``/``.reject`` — this
    handler never opens the financial transaction itself.
    """
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    if _non_private_chat(update):
        await _safe_answer(query, None)
        return
    parsed = parse_callback(getattr(query, "data", None))
    if parsed is None:
        await _safe_answer(query, MSG_INVALID)
        return
    actor = _actor_id(
        getattr(getattr(query, "from_user", None), "id", None)
    )
    if actor is None or not is_admin(actor):
        await _safe_answer(query, MSG_ADMIN_ONLY)
        return
    op, ref = parsed

    try:
        # ── UI navigation: fresh read-only snapshot, clamped page ──
        if op == OP_PAGE:
            requests = SqliteWithdrawalRepository().list_pending()
            text, markup = build_list_page(requests, ref)
            await _safe_answer(query, None)
            await _safe_edit(query, text, markup)
            return

        request_id = ref
        repository = SqliteWithdrawalRepository()
        try:
            request = repository.get(request_id)
        except withdrawal_rules.RequestNotFoundError:
            await _safe_answer(query, MSG_NOT_FOUND)
            return
        except Exception:
            logger.exception(
                "Withdrawal load failed: request=%s admin=%d",
                request_id, actor,
            )
            await _safe_answer(query, MSG_ERROR)
            return

        # ── Detail card (read-only; stale rows render as-is) ───────
        if op == OP_VIEW:
            await _safe_answer(query, None)
            await _safe_edit(
                query,
                build_detail_text(request),
                build_detail_keyboard(request),
            )
            logger.info(
                "Withdrawal review opened: request=%s admin=%d status=%s",
                request_id, actor, request.status.value,
            )
            return

        pending = request.status is withdrawal_rules.RequestStatus.PENDING

        # ── Confirmation step (NO mutation happens here) ───────────
        if op in (OP_ASK_COMPLETE, OP_ASK_REJECT):
            if not pending:
                await _safe_answer(query, MSG_STATE_CHANGED)
                return
            complete = op == OP_ASK_COMPLETE
            await _safe_answer(query, None)
            await _safe_edit(
                query,
                build_confirm_text(request, complete=complete),
                build_confirm_keyboard(request, complete=complete),
            )
            return

        # ── Confirmed action: the ONE delegation to the service ────
        # (op is doc or dor — parse_callback guarantees this.)
        action = "complete" if op == OP_DO_COMPLETE else "reject"
        if not pending:
            # Fast-path for a stale card; the service CAS below stays
            # authoritative if the state changes in between.
            logger.info(
                "Withdrawal %s: request=%s admin=%d result=invalid_state",
                action, request_id, actor,
            )
            await _safe_answer(query, MSG_STATE_CHANGED)
            return

        try:
            service = _service()
            if action == "complete":
                updated = service.complete(request_id)
            else:
                updated = service.reject(request_id)
        except withdrawal_rules.RequestNotFoundError:
            logger.info(
                "Withdrawal %s: request=%s admin=%d result=not_found",
                action, request_id, actor,
            )
            await _safe_answer(query, MSG_NOT_FOUND)
            return
        except withdrawal_rules.InvalidStateError:
            # Already completed/rejected by the time we got here —
            # the CAS refused; nothing settled or released twice.
            logger.info(
                "Withdrawal %s: request=%s admin=%d result=invalid_state",
                action, request_id, actor,
            )
            await _safe_answer(query, MSG_STATE_CHANGED)
            return
        except MissingWalletDebitError:
            # Legacy row: never guessed, never mutated.
            logger.info(
                "Withdrawal %s: request=%s admin=%d "
                "result=missing_wallet_debit",
                action, request_id, actor,
            )
            await _safe_answer(query, MSG_LEGACY)
            return
        except withdrawal_rules.InsufficientHeldBalanceError:
            logger.info(
                "Withdrawal %s: request=%s admin=%d "
                "result=insufficient_held",
                action, request_id, actor,
            )
            await _safe_answer(query, MSG_INSUFFICIENT)
            return
        except Exception:
            logger.exception(
                "Withdrawal %s failed: request=%s admin=%d result=error",
                action, request_id, actor,
            )
            await _safe_answer(query, MSG_ERROR)
            return

        # Post-commit side effect: tell the requester.  Strictly
        # best-effort — the transaction above is already committed and
        # a notification failure must never re-run or roll it back.
        await _notify_user(context, updated, action)

        logger.info(
            "Withdrawal %s: request=%s admin=%d result=%s",
            action, request_id, actor, updated.status.value,
        )
        await _safe_answer(query, None)
        await _safe_edit(
            query, _success_text(action, request_id), _list_again_button()
        )
    except Exception:
        # Catch-all: a transport/rendering failure must never leak a
        # traceback or internals to the admin chat.
        logger.exception(
            "Withdrawal callback failed: admin=%s data=%r",
            actor, getattr(query, "data", None),
        )
        await _safe_answer(query, MSG_ERROR)
