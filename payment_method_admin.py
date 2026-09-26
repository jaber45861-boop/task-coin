"""
Payment Method Admin (MT-ADMIN-08)
==================================

Private-Telegram Admin Control Plane for the dynamic payment-method /
wallet management foundation.  Follows the established MT-ADMIN
conventions (admin-only authorization via ``config.is_admin``,
MT-ADMIN-02 private-chat isolation, thin handlers over a store layer,
opaque callback lookup ids, plain-text rendering — no parse modes).

Workflow (all in the admin's private chat)::

    /paymethods                 → panel
      [ ➕ إضافة وسيلة ]        → format help (pm:help)
      [ 📋 عرض الوسائل ]        → bounded oldest-first list (pm:list)
    /addpm <form>               → validate + persist a new method
    list buttons per method:
      [ ✏️ تعديل ]              → full current values + /editpm template
      [ 🟢 تفعيل | 🔴 تعطيل ]  → idempotent active toggle
      [ 🗑️ حذف ]               → confirmation card → pm:delyes:<id>

Input is a single pipe-delimited command — the same stateless
convention as ``/addchannel slug|@user|title`` — so NO conversation
state (memory or persisted) exists anywhere in this flow.

Security:

- every command and callback re-checks private chat + ``is_admin``;
- callback payloads carry only ``pm:<op>[:<positive id>]`` — a lookup
  pointer; the row, its fields and the actor are re-read server-side;
- destinations are shown masked in lists (full only inside the admin's
  own edit template); they are NEVER logged;
- no private keys are ever requested or stored (the store rejects the
  one generic, recognizable private-key marker);
- normal users cannot reach any control: non-admins get the standard
  admin-only refusal and groups/channels get zero replies.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import payment_method_store as store
from config import is_admin
from payment_method_store import (
    CATEGORY_LABELS,
    EMPTY_FIELD,
    PaymentMethod,
    PaymentMethodValidationError,
)

logger = logging.getLogger(__name__)

# ── Callback payloads (untrusted lookup pointers ONLY) ────────────────
CB_PREFIX = "pm"
OP_HELP = "help"
OP_LIST = "list"
OP_PAGE = "page"
OP_EDIT = "edit"
OP_ON = "on"
OP_OFF = "off"
OP_DEL = "del"
OP_DEL_YES = "delyes"
_OPS_WITH_ID = frozenset({OP_PAGE, OP_EDIT, OP_ON, OP_OFF, OP_DEL, OP_DEL_YES})
_OPS_NO_ID = frozenset({OP_HELP, OP_LIST})
_ALL_OPS = _OPS_WITH_ID | _OPS_NO_ID

# ── Bounds ────────────────────────────────────────────────────────────
PAGE_SIZE = 5

# ── Arabic UI strings ─────────────────────────────────────────────────
MSG_ADMIN_ONLY = "⛔ هذا الأمر للمشرفين فقط."
MSG_INVALID = "⛔ طلب غير صالح."
MSG_NOT_FOUND = "⛔ الوسيلة غير موجودة."
MSG_ERROR = "⛔ حدث خطأ، حاول مرة أخرى."

PANEL_HEADER = "💰 إدارة وسائل الدفع"
LIST_HEADER = "💰 وسائل الدفع"
MSG_LIST_EMPTY = "📭 لا توجد وسائل دفع بعد."

BTN_ADD = "➕ إضافة وسيلة"
BTN_LIST = "📋 عرض الوسائل"
BTN_EDIT = "✏️ تعديل"
BTN_ACTIVATE = "🟢 تفعيل"
BTN_DEACTIVATE = "🔴 تعطيل"
BTN_DELETE = "🗑️ حذف"
BTN_DELETE_CONFIRM = "🗑️ تأكيد الحذف"
BTN_CANCEL = "↩️ إلغاء"
PAGE_NEXT = "التالي ▶️"
PAGE_PREV = "◀️ السابق"

STATUS_ACTIVE = "🟢 نشطة"
STATUS_INACTIVE = "🔴 متوقفة"

DEST_LABEL = {"crypto": "العنوان", "cash": "الحساب"}

HELP_TEXT = (
    "➕ إضافة وسيلة دفع\n"
    "━━━━━━━━━━━━━━━━━━━\n\n"
    "أرسل الأمر بهذه الصيغة:\n\n"
    "/addpm <الفئة> | <الاسم> | <العملة> | <الشبكة> | "
    "<المزود> | <العنوان> | <الملاحظات>\n\n"
    "• الفئة: crypto (عملات رقمية) أو cash (محفظة إلكترونية)\n"
    "• الشبكة أو الملاحظات: اكتب - إذا لم توجد\n"
    "• الاسم والعملة والشبكة والمزود بيانات حرة تحددها بنفسك\n"
    "  (أي مزود أو أي شبكة تُضاف بدون أي تعديل في الكود)\n"
    "• العنوان يُسجَّل كما هو دون تحقق من أي شبكة — تحقق منه بنفسك\n"
    "• للتعديل استخدم: /editpm <رقم> | نفس الصيغة\n\n"
    "⚠️ لا ترسل أي مفاتيح خاصة (Private Keys) — لا نحتاجها أبداً."
)

MSG_USAGE_ADD = (
    "❌ الصيغة غير صحيحة.\n"
    "استخدم:\n"
    "/addpm <الفئة> | <الاسم> | <العملة> | <الشبكة> | "
    "<المزود> | <العنوان> | <الملاحظات>\n\n"
    "اكتب - للشبكة أو الملاحظات عند عدم وجودها."
)
MSG_USAGE_EDIT = (
    "❌ الصيغة غير صحيحة.\n"
    "استخدم:\n"
    "/editpm <رقم> | <الفئة> | <الاسم> | <العملة> | <الشبكة> | "
    "<المزود> | <العنوان> | <الملاحظات>"
)


# ── Parsing helpers (pure, unit-tested) ───────────────────────────────


def parse_callback(data: object) -> tuple[str, int | None] | None:
    """``pm:<op>`` / ``pm:<op>:<positive int>`` → (op, id | None).

    Anything else (unknown op, non-numeric/negative/zero id, wrong
    arity) → None.  The id is a lookup pointer only.
    """
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if parts[0] != CB_PREFIX or len(parts) not in (2, 3):
        return None
    op = parts[1]
    if op not in _ALL_OPS:
        return None
    if len(parts) == 2:
        return (op, None) if op in _OPS_NO_ID else None
    raw = parts[2]
    if not (raw.isascii() and raw.isdigit()):
        return None
    value = int(raw)
    if value <= 0 or op not in _OPS_WITH_ID:
        return None
    return op, value


def parse_add_form(body: object) -> tuple | None:
    """``cat | name | asset | network | provider | dest [| notes]``.

    Returns the seven raw field strings (notes may be None) or None
    when the structure is wrong.  Field validation happens later in
    the store so its Arabic errors reach the admin verbatim.
    """
    if not isinstance(body, str):
        return None
    parts = body.split("|", 6)
    if len(parts) < 6:
        return None
    fields = tuple(p.strip() for p in parts[:6])
    instructions: str | None = None
    if len(parts) == 7:
        # Raw tail keeps the admin's original spacing inside notes.
        instructions = parts[6].strip() or None
    return fields + (instructions,)


def parse_edit_form(body: object) -> tuple[int, tuple] | None:
    """``<id> | <same six/seven form fields>`` → (id, form fields)."""
    if not isinstance(body, str):
        return None
    head, sep, rest = body.partition("|")
    if not sep:
        return None
    id_text = head.strip()
    if not (id_text.isascii() and id_text.isdigit()):
        return None
    method_id = int(id_text)
    if method_id <= 0:
        return None
    form = parse_add_form(rest)
    if form is None:
        return None
    return method_id, form


def mask_destination(value: str) -> str:
    """Masked display for lists: first 6 + … + last 4 when long."""
    if not isinstance(value, str):
        return ""
    if len(value) <= 10:
        return value
    return f"{value[:6]}…{value[-4:]}"


def _field_or_dash(value: str | None) -> str:
    return value if value else EMPTY_FIELD


# ── Rendering (pure, deterministic, bounded) ──────────────────────────


def build_panel_text(methods: list[PaymentMethod]) -> str:
    active = sum(1 for m in methods if m.is_active)
    return (
        f"{PANEL_HEADER}\n"
        f"النشطة: {active} — الإجمالي: {len(methods)}\n\n"
        "استخدم ➕ لإظهار صيغة الإضافة، أو 📋 لعرض الوسائل."
    )


def build_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(BTN_ADD, callback_data=f"{CB_PREFIX}:{OP_HELP}"),
                InlineKeyboardButton(BTN_LIST, callback_data=f"{CB_PREFIX}:{OP_LIST}"),
            ]
        ]
    )


def _method_lines(
    methods: list[PaymentMethod], start_index: int
) -> list[str]:
    lines: list[str] = []
    for offset, method in enumerate(methods):
        lines.append(f"{start_index + offset}. {method.display_name}")
        composition = method.asset
        if method.network:
            composition = f"{method.asset} / {method.network}"
        lines.append(
            f"   {composition} — {CATEGORY_LABELS.get(method.category, method.category)}"
        )
        lines.append(f"   المزود: {method.provider}")
        dest_label = DEST_LABEL.get(method.category, "العنوان")
        lines.append(
            f"   {dest_label}: {mask_destination(method.destination)}"
        )
        lines.append(
            f"   {STATUS_ACTIVE if method.is_active else STATUS_INACTIVE}"
        )
        lines.append("")
    return lines


def _method_buttons(method: PaymentMethod) -> list[InlineKeyboardButton]:
    toggle = (
        InlineKeyboardButton(
            BTN_DEACTIVATE,
            callback_data=f"{CB_PREFIX}:{OP_OFF}:{method.id}",
        )
        if method.is_active
        else InlineKeyboardButton(
            BTN_ACTIVATE,
            callback_data=f"{CB_PREFIX}:{OP_ON}:{method.id}",
        )
    )
    return [
        InlineKeyboardButton(
            BTN_EDIT, callback_data=f"{CB_PREFIX}:{OP_EDIT}:{method.id}"
        ),
        toggle,
        InlineKeyboardButton(
            BTN_DELETE, callback_data=f"{CB_PREFIX}:{OP_DEL}:{method.id}"
        ),
    ]


def build_list_page(
    methods: list[PaymentMethod], page: int = 1
) -> tuple[str, InlineKeyboardMarkup | None]:
    """One bounded page (oldest first).  Untrusted page ids clamped."""
    total = len(methods)
    if total == 0:
        return MSG_LIST_EMPTY, None

    max_page = max(1, -(-total // PAGE_SIZE))  # ceil division
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = min(max(1, page), max_page)
    start = (page - 1) * PAGE_SIZE
    chunk = methods[start : start + PAGE_SIZE]

    header = LIST_HEADER
    if total > PAGE_SIZE:
        header += f" ({start + 1}–{start + len(chunk)} من {total})"

    lines = [header, ""] + _method_lines(chunk, start + 1)
    rows: list[list[InlineKeyboardButton]] = [
        _method_buttons(m) for m in chunk
    ]
    if max_page > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 1:
            nav.append(
                InlineKeyboardButton(
                    PAGE_PREV,
                    callback_data=f"{CB_PREFIX}:{OP_PAGE}:{page - 1}",
                )
            )
        if page < max_page:
            nav.append(
                InlineKeyboardButton(
                    PAGE_NEXT,
                    callback_data=f"{CB_PREFIX}:{OP_PAGE}:{page + 1}",
                )
            )
        if nav:
            rows.append(nav)
    return "\n".join(lines).rstrip("\n"), InlineKeyboardMarkup(rows)


def build_edit_template(method: PaymentMethod) -> str:
    """Full current values + the exact replacement command."""
    return (
        f"✏️ تعديل الوسيلة #{method.id}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"النوع: {method.category}\n"
        f"الاسم: {method.display_name}\n"
        f"العملة: {method.asset}\n"
        f"الشبكة: {_field_or_dash(method.network)}\n"
        f"المزود: {method.provider}\n"
        f"{DEST_LABEL.get(method.category, 'العنوان')}: {method.destination}\n"
        f"الملاحظات: {_field_or_dash(method.instructions)}\n\n"
        "أرسل الأمر الجديد (استبدل القيم):\n"
        f"/editpm {method.id} | {method.category} | {method.display_name} "
        f"| {method.asset} | {_field_or_dash(method.network)} "
        f"| {method.provider} | {method.destination} "
        f"| {_field_or_dash(method.instructions)}"
    )


def build_delete_confirm(method: PaymentMethod) -> tuple[str, InlineKeyboardMarkup]:
    return (
        f"🗑️ حذف الوسيلة #{method.id} — {method.display_name}؟\n"
        "لن يمكن التراجع عن الحذف.",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        BTN_DELETE_CONFIRM,
                        callback_data=f"{CB_PREFIX}:{OP_DEL_YES}:{method.id}",
                    ),
                    InlineKeyboardButton(
                        BTN_CANCEL, callback_data=f"{CB_PREFIX}:{OP_LIST}"
                    ),
                ]
            ]
        ),
    )


def build_created_text(method: PaymentMethod) -> str:
    composition = method.asset
    if method.network:
        composition = f"{method.asset} / {method.network}"
    return (
        f"✅ تمت إضافة الوسيلة #{method.id}\n"
        f"{method.display_name}\n"
        f"{composition} — المزود: {method.provider}"
    )


def _list_again_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_LIST, callback_data=f"{CB_PREFIX}:{OP_LIST}"
                )
            ]
        ]
    )


# ── Local async safety wrappers ───────────────────────────────────────


async def _safe_answer(query, text: str | None) -> None:
    try:
        if text:
            await query.answer(text=text)
        else:
            await query.answer()
    except Exception:
        logger.debug("Could not answer payment-method callback", exc_info=True)


async def _safe_edit(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        logger.debug("Could not edit payment-method message", exc_info=True)


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


def _command_body(message) -> str:
    """Everything after the command word (``/addpm ...`` → payload)."""
    text = getattr(message, "text", None)
    if not isinstance(text, str):
        return ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


# ── PTB handlers ──────────────────────────────────────────────────────


async def paymethods_command(update, context) -> None:
    """``/paymethods`` — the payment-method management panel.

    Private admin chat ONLY; groups/channels stay silent and
    non-admins get the standard admin-only refusal.
    """
    if _non_private_chat(update):
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    actor = _actor_id(getattr(getattr(update, "effective_user", None), "id", None))
    if actor is None:
        return
    if not is_admin(actor):
        await message.reply_text(MSG_ADMIN_ONLY)
        return
    try:
        methods = store.list_payment_methods()
    except Exception:
        logger.exception("Payment method list failed: admin=%d", actor)
        await message.reply_text(MSG_ERROR)
        return
    await message.reply_text(
        build_panel_text(methods), reply_markup=build_panel_keyboard()
    )


async def add_pm_command(update, context) -> None:
    """``/addpm <form>`` — validate + persist a new payment method."""
    if _non_private_chat(update):
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    actor = _actor_id(getattr(getattr(update, "effective_user", None), "id", None))
    if actor is None:
        return
    if not is_admin(actor):
        await message.reply_text(MSG_ADMIN_ONLY)
        return

    body = _command_body(message)
    if not body:
        await message.reply_text(HELP_TEXT)
        return
    form = parse_add_form(body)
    if form is None:
        await message.reply_text(MSG_USAGE_ADD)
        return
    category, name, asset, network, provider, destination, instructions = form
    try:
        created = store.create_payment_method(
            category=category,
            display_name=name,
            asset=asset,
            network=network,
            provider=provider,
            destination=destination,
            instructions=instructions,
            created_by=actor,
        )
    except PaymentMethodValidationError as exc:
        await message.reply_text(f"❌ {exc}")
        return
    except Exception:
        logger.exception("Payment method create failed: admin=%d", actor)
        await message.reply_text(MSG_ERROR)
        return
    await message.reply_text(
        build_created_text(created), reply_markup=_list_again_button()
    )


async def edit_pm_command(update, context) -> None:
    """``/editpm <id> | <form>`` — full replace; the id stays stable."""
    if _non_private_chat(update):
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    actor = _actor_id(getattr(getattr(update, "effective_user", None), "id", None))
    if actor is None:
        return
    if not is_admin(actor):
        await message.reply_text(MSG_ADMIN_ONLY)
        return

    body = _command_body(message)
    parsed = parse_edit_form(body)
    if parsed is None:
        await message.reply_text(MSG_USAGE_EDIT)
        return
    method_id, form = parsed
    category, name, asset, network, provider, destination, instructions = form
    try:
        updated = store.update_payment_method(
            method_id,
            category=category,
            display_name=name,
            asset=asset,
            network=network,
            provider=provider,
            destination=destination,
            instructions=instructions,
            updated_by=actor,
        )
    except PaymentMethodValidationError as exc:
        await message.reply_text(f"❌ {exc}")
        return
    except Exception:
        logger.exception(
            "Payment method update failed: id=%s admin=%d",
            method_id, actor,
        )
        await message.reply_text(MSG_ERROR)
        return
    if updated is None:
        await message.reply_text(MSG_NOT_FOUND)
        return
    await message.reply_text(
        f"✅ تم تعديل الوسيلة #{updated.id}",
        reply_markup=_list_again_button(),
    )


async def payment_method_callback(update, context) -> None:
    """``pm:`` callbacks — private admin chat ONLY, server re-reads all.

    The payload is an opaque lookup pointer; actor authorization, the
    row and every displayed/validated field come from SQLite.
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
    actor = _actor_id(getattr(getattr(query, "from_user", None), "id", None))
    if actor is None or not is_admin(actor):
        await _safe_answer(query, MSG_ADMIN_ONLY)
        return
    op, ref = parsed

    try:
        if op == OP_HELP:
            await _safe_answer(query, None)
            await _safe_edit(query, HELP_TEXT, None)
            return

        if op in (OP_LIST, OP_PAGE):
            methods = store.list_payment_methods()
            text, markup = build_list_page(
                methods, 1 if ref is None else ref
            )
            await _safe_answer(query, None)
            await _safe_edit(query, text, markup)
            return

        # All remaining ops need a concrete method id.
        method = store.get_payment_method(ref)
        if method is None:
            await _safe_answer(query, MSG_NOT_FOUND)
            return

        if op == OP_EDIT:
            await _safe_answer(query, None)
            await _safe_edit(query, build_edit_template(method), None)
            return

        if op in (OP_ON, OP_OFF):
            updated = store.set_payment_method_active(
                method.id, op == OP_ON, updated_by=actor
            )
            if updated is None:
                await _safe_answer(query, MSG_NOT_FOUND)
                return
            await _safe_answer(query, None)
            state = "تم تفعيل" if updated.is_active else "تم تعطيل"
            await _safe_edit(
                query,
                f"✅ {state} الوسيلة #{updated.id}",
                _list_again_button(),
            )
            return

        if op == OP_DEL:
            text, markup = build_delete_confirm(method)
            await _safe_answer(query, None)
            await _safe_edit(query, text, markup)
            return

        # OP_DEL_YES — deletion re-checked server-side (stale confirms
        # for an already-deleted id report NOT_FOUND and mutate nothing).
        if store.delete_payment_method(method.id, deleted_by=actor):
            await _safe_answer(query, None)
            await _safe_edit(
                query,
                f"🗑️ تم حذف الوسيلة #{method.id}.",
                _list_again_button(),
            )
        else:
            await _safe_answer(query, MSG_NOT_FOUND)
        return
    except Exception:
        logger.exception(
            "Payment method callback failed: op=%s ref=%s admin=%s",
            op, ref, actor,
        )
        await _safe_answer(query, MSG_ERROR)
