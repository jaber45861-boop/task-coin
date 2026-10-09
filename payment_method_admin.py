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
    /addpm                      → INTERACTIVE WIZARD (buttons + text steps)
    /addpm <form>               → legacy pipe form, byte-for-byte unchanged
    /editpm <id>                → interactive field-edit menu
    /editpm <id> | <form>       → legacy pipe form, unchanged
    list buttons per method:
      [ ✏️ تعديل ]              → full current values + /editpm template
      [ 🟢 تفعيل | 🔴 تعطيل ]  → idempotent active toggle
      [ 🗑️ حذف ]               → confirmation card → pm:delyes:<id>

The wizard stages its form in ONE short-lived in-memory slot per
admin (keyed by the trusted Telegram actor id, TTL-expired, popped
on confirm/save/cancel — the admin_control pending-input
precedent).  Nothing touches SQLite before the review page's
explicit "تأكيد وإضافة"; the legacy pipe form stays available
byte-for-byte for existing scripts.

Security:

- every command and callback re-checks private chat + ``is_admin``;
- callback payloads carry only ``pm:<op>[:<positive id>]`` — a lookup
  pointer; the row, its fields and the actor are re-read server-side;
- wizard identity/ownership comes ONLY from the Telegram actor — no
  user id ever travels inside callback data or message text, so a
  second admin can never drive or complete another admin's wizard;
- destinations are shown masked in lists (full only inside the admin's
  own edit template); they are NEVER logged;
- no private keys are ever requested or stored (the store rejects the
  one generic, recognizable private-key marker);
- normal users cannot reach any control: non-admins get the standard
  admin-only refusal and groups/channels get zero replies.
"""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import asset_units
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
OP_DEPON = "depon"
OP_DEPOFF = "depoff"
OP_DEL = "del"
OP_DEL_YES = "delyes"

# ── Interactive wizard ops (same pm: family, same strict grammar) ────
# Wizard callbacks stay bounded lookup pointers: the staged state is
# keyed by the Telegram actor and NEVER by anything in the payload.
OP_WCAT = "wcat"          # pm:wcat:<1 crypto | 2 cash>
OP_WASSET = "wasset"      # pm:wasset:<1-based suggestion index>
OP_WNET = "wnet"          # pm:wnet:<1-based suggestion index>
OP_WNONE = "wnone"        # network = not applicable
OP_WMANUAL = "wmanual"    # switch the current step to free text
OP_WSKIP = "wskip"        # optional notes: skip
OP_WFIELD = "wfield"      # pm:wfield:<1-based menu field index>
OP_WMENU = "wmenu"        # review → field menu
OP_WREVIEW = "wreview"    # field menu → review
OP_WCONFIRM = "wconfirm"  # review → create (single-use)
OP_WSAVE = "wsave"        # edit menu → persist (single-use)
OP_WCANCEL = "wcancel"    # abandon + drop the staged state

_WIZARD_OPS_NO_ID = frozenset(
    {
        OP_WNONE,
        OP_WMANUAL,
        OP_WSKIP,
        OP_WMENU,
        OP_WREVIEW,
        OP_WCONFIRM,
        OP_WSAVE,
        OP_WCANCEL,
    }
)
_WIZARD_OPS_WITH_ID = frozenset({OP_WCAT, OP_WASSET, OP_WNET, OP_WFIELD})
_WIZARD_OPS = _WIZARD_OPS_NO_ID | _WIZARD_OPS_WITH_ID

_OPS_WITH_ID = frozenset(
    {OP_PAGE, OP_EDIT, OP_ON, OP_OFF, OP_DEPON, OP_DEPOFF, OP_DEL, OP_DEL_YES}
) | _WIZARD_OPS_WITH_ID
_OPS_NO_ID = frozenset({OP_HELP, OP_LIST}) | _WIZARD_OPS_NO_ID
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
BTN_DEPOSIT_ON = "🟢 تفعيل الإيداع"
BTN_DEPOSIT_OFF = "🔴 تعطيل الإيداع"
BTN_DELETE_CONFIRM = "🗑️ تأكيد الحذف"
BTN_CANCEL = "↩️ إلغاء"
PAGE_NEXT = "التالي ▶️"
PAGE_PREV = "◀️ السابق"

STATUS_ACTIVE = "🟢 نشطة"
STATUS_INACTIVE = "🔴 متوقفة"
DEPOSIT_ON = "الإيداع: 🟢 مفعّل"
DEPOSIT_OFF = "الإيداع: 🔴 غير مفعّل"
DEPOSIT_UNAVAILABLE_HEADER = "⚠️ وسائل إيداع مفعّلة لا يمكنها استقبال الطلبات:"
DEPOSIT_UNAVAILABLE_LABEL = "⚠️ لا يمكن استقبال الإيداعات:"
DEPOSIT_UNAVAILABLE_REASONS = {
    "inactive": "الوسيلة غير نشطة",
    "asset": "مقياس الأصل غير مسجل",
    "minimum_missing": "الحد الأدنى غير مضبوط",
    "minimum_invalid": "الحد الأدنى غير صالح",
}

DEST_LABEL = {"crypto": "العنوان", "cash": "الحساب"}

HELP_TEXT = (
    "➕ إضافة وسيلة دفع\n"
    "━━━━━━━━━━━━━━━━━━━\n\n"
    "💡 الأسهل: أرسل /addpm بدون وسيط لبدء الـ Wizard التفاعلي بالأزرار.\n\n"
    "أو أرسل الأمر بهذه الصيغة:\n\n"
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

# ── Interactive wizard strings (Arabic, button-driven) ────────────────

WZ_RULE = "━━━━━━━━━━━━━━━━━━━"
WZ_ADD_TITLE = "➕ إضافة وسيلة دفع"
WZ_SELECT_ASSET = "💰 اختر العملة:"
WZ_SELECT_NETWORK = "🌐 اختر الشبكة:"
WZ_PROMPT_ASSET = "💰 اكتب العملة:\n\nأرسل رمز العملة كرسالة."
WZ_PROMPT_ASSET_MANUAL = "💰 أرسل رمز العملة كرسالة:"
WZ_PROMPT_NETWORK = "🌐 اكتب الشبكة:\n\nأرسل اسم الشبكة كرسالة."
WZ_PROMPT_NETWORK_MANUAL = "🌐 أرسل اسم الشبكة كرسالة:"
WZ_PROMPT_NAME = "✏️ اكتب اسم وسيلة الدفع:"
WZ_PROMPT_PROVIDER = "🏦 اكتب اسم المزود:"
WZ_PROMPT_DESTINATION = (
    "📍 أرسل عنوان الاستقبال العام:\n\n"
    "⚠️ أرسل العنوان العام فقط.\n"
    "❌ لا ترسل Private Key.\n"
    "❌ لا ترسل Seed Phrase."
)
WZ_PROMPT_INSTRUCTIONS = "📝 أرسل ملاحظات أو اضغط \"تخطي\":"
WZ_PROMPT_MIN_DEPOSIT = (
    "💵 أرسل الحد الأدنى للإيداع بوحدة العملة الحالية (مثال: 50):\n\n"
    "اكتب 0 لمسح الحد الأدنى (يُرفض الإيداع حتى تضبطه من جديد)."
)
WZ_PROMPT_MIN_DEPOSIT_NO_ASSET = (
    "💵 حدّد العملة أولًا — لا يمكن ضبط الحد الأدنى قبل معرفة "
    "وحدة العملة."
)
WZ_PROMPT_CATEGORY = (
    f"{WZ_ADD_TITLE}\n{WZ_RULE}\n\n"
    "اختر الفئة:\n\n"
    "💡 يمكنك أيضًا استخدام الصيغة القديمة:\n"
    "/addpm <الفئة> | <الاسم> | <العملة> | ..."
)

BTN_WZ_CRYPTO = "💰 Crypto"
BTN_WZ_CASH = "💵 Cash"
BTN_WZ_CANCEL = "❌ إلغاء"
BTN_WZ_MANUAL = "✏️ إدخال يدوي"
BTN_WZ_NO_NETWORK = "🚫 بدون شبكة"
BTN_WZ_SKIP = "⏭️ تخطي"
BTN_WZ_CONFIRM = "✅ تأكيد وإضافة"
BTN_WZ_EDIT_FIELDS = "✏️ تعديل"
BTN_WZ_REVIEW = "🔎 مراجعة"
BTN_WZ_SAVE = "💾 حفظ"

MSG_WIZARD_STALE = (
    "⌛ انتهت جلسة المساعدة أو لم تكن هناك جلسة نشطة.\n"
    "أرسل /addpm للإضافة أو /editpm <رقم> للتعديل."
)
MSG_WIZARD_CANCELED = "❌ تم الإلغاء. لم يُحفظ أي شيء."
MSG_WIZARD_INVALID_OPTION = (
    "❌ البيانات غير صحيحة.\n"
    "اختر من الأزرار الموجودة أسفل الرسالة."
)
MSG_WIZARD_MISSING_PREFIX = "❌ بيانات ناقصة: "
MSG_WIZARD_RETRY = "\n\nاكتب القيمة مرة أخرى أو اضغط ❌ إلغاء."


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


def _deposit_unavailable_reasons(method: PaymentMethod) -> tuple[str, ...]:
    """Explain why an opted-in deposit method currently rejects requests.

    This is a read-only diagnostic mirroring the fail-closed checks in
    ``deposit_store.create_deposit_request``. Methods not opted in for
    deposits are intentionally omitted from this warning.
    """
    if not method.deposits_enabled:
        return ()

    reasons: list[str] = []
    if not method.is_active:
        reasons.append(DEPOSIT_UNAVAILABLE_REASONS["inactive"])
    if not asset_units.is_supported(method.asset):
        reasons.append(DEPOSIT_UNAVAILABLE_REASONS["asset"])

    minimum = method.min_deposit_units
    if minimum is None:
        reasons.append(DEPOSIT_UNAVAILABLE_REASONS["minimum_missing"])
    elif (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or minimum <= 0
    ):
        reasons.append(DEPOSIT_UNAVAILABLE_REASONS["minimum_invalid"])
    return tuple(reasons)


def build_panel_text(methods: list[PaymentMethod]) -> str:
    active = sum(1 for m in methods if m.is_active)
    text = (
        f"{PANEL_HEADER}\n"
        f"النشطة: {active} — الإجمالي: {len(methods)}\n\n"
        "استخدم ➕ لإظهار صيغة الإضافة، أو 📋 لعرض الوسائل."
    )
    unavailable = [
        (method, reasons)
        for method in methods
        if (reasons := _deposit_unavailable_reasons(method))
    ]
    if not unavailable:
        return text

    lines = ["", "", DEPOSIT_UNAVAILABLE_HEADER]
    for method, reasons in unavailable[:PAGE_SIZE]:
        lines.append(
            f"• #{method.id} {method.display_name}: {'، '.join(reasons)}"
        )
    remaining = len(unavailable) - PAGE_SIZE
    if remaining > 0:
        lines.append(f"• و{remaining} وسيلة أخرى؛ راجع 📋 عرض الوسائل.")
    return text + "\n".join(lines)


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
        lines.append(
            f"   {DEPOSIT_ON if method.deposits_enabled else DEPOSIT_OFF}"
        )
        unavailable_reasons = _deposit_unavailable_reasons(method)
        if unavailable_reasons:
            lines.append(
                f"   {DEPOSIT_UNAVAILABLE_LABEL} "
                f"{'، '.join(unavailable_reasons)}"
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
        InlineKeyboardButton(
            BTN_DEPOSIT_ON if not method.deposits_enabled else BTN_DEPOSIT_OFF,
            callback_data=(
                f"{CB_PREFIX}:"
                f"{OP_DEPON if not method.deposits_enabled else OP_DEPOFF}:"
                f"{method.id}"
            ),
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


# ── Interactive wizard state (in-memory, per-admin, TTL) ──────────────
#
# ``/addpm`` without arguments opens a step-by-step button wizard and
# ``/editpm <id>`` (without the pipe form) opens the field-edit menu.
# Following the established admin_control pending-input precedent the
# bridge is a SHORT-LIVED in-memory dict: ONE slot per admin, keyed by
# the trusted Telegram actor id — never by anything carried in
# callback data or message text.  The slot holds only the staged form
# fields and the current step: no secrets, no tokens, and it is never
# logged (``destination`` lives here transiently and nowhere else).
# A slot is replaced wholesale when a wizard restarts (never merged,
# so two wizards can never mix), popped on confirm/save/cancel and
# expired after WIZARD_TTL_SECONDS of inactivity.  Selection buttons
# (asset/network) are DERIVED from the ``payment_methods`` table —
# what the system actually holds — because MT-ADMIN-08 forbids
# hard-coding any provider/network/asset literal in this module.

WIZARD_TTL_SECONDS = 600
_WIZARD_STATES: dict[int, dict] = {}

# ``pm:wfield:<index>`` → staged dict key (fixed server-side order;
# the callback carries only the bounded positive index).
_MENU_FIELDS: tuple[str, ...] = (
    "category",
    "display_name",
    "asset",
    "network",
    "provider",
    "destination",
    "instructions",
    # Appended LAST so the existing field indices (1-7) stay stable
    # for live menus and tests; needs the staged asset's scale.
    "min_deposit_units",
)

_FIELD_LABELS = {
    "category": "الفئة",
    "display_name": "الاسم",
    "asset": "العملة",
    "network": "الشبكة",
    "provider": "المزود",
    "destination": "العنوان",
    "instructions": "الملاحظات",
    "min_deposit_units": "💵 الحد الأدنى للإيداع",
}

_MENU_BUTTON_LABELS = {
    "category": "🏷️ الفئة",
    "display_name": "✏️ الاسم",
    "asset": "💰 العملة",
    "network": "🌐 الشبكة",
    "provider": "🏦 المزود",
    "destination": "📍 العنوان",
    "instructions": "📝 الملاحظات",
    "min_deposit_units": "💵 الحد الأدنى",
}

# Text steps consumed by ``wizard_text_input`` (category/review/menu
# wait for buttons instead).
_TEXT_STEPS = frozenset(
    {"asset", "network", "display_name", "provider", "destination",
     "instructions", "min_deposit_units"}
)

# Existing store validators — the wizard invents NO rules of its own.
_FIELD_VALIDATORS = {
    "asset": store.validate_asset,
    "network": store.validate_network,
    "display_name": store.validate_display_name,
    "provider": store.validate_provider,
    "destination": store.validate_destination,
    "instructions": store.validate_instructions,
}

_REQUIRED_FIELDS = ("category", "display_name", "asset", "provider", "destination")

# Suggestion buttons are bounded (Telegram keyboards stay readable).
_MAX_OPTIONS = 6


def _new_wizard_state(mode: str) -> dict:
    """A fresh staged form; ``mode`` is ``"add"`` or ``"edit"``."""
    return {
        "mode": mode,
        "step": "category" if mode == "add" else "menu",
        "method_id": None,
        "category": None,
        "display_name": None,
        "asset": None,
        "network": None,
        "provider": None,
        "destination": None,
        "instructions": None,
        "min_deposit_units": None,
        "manual": False,
        "editing": False,
        "asset_options": (),
        "updated_at": time.monotonic(),
    }


def _wizard_state(actor: int) -> tuple[dict | None, bool]:
    """Live state for *actor* + whether an EXPIRED one was collected.

    Every successful read refreshes the inactivity TTL.  An expired
    slot is popped here, so a stale button press can never complete
    an old operation.
    """
    state = _WIZARD_STATES.get(actor)
    if state is None:
        return None, False
    try:
        updated_at = float(state.get("updated_at", 0.0))
    except (TypeError, ValueError):
        updated_at = 0.0
    if time.monotonic() - updated_at > WIZARD_TTL_SECONDS:
        _WIZARD_STATES.pop(actor, None)
        return None, True
    state["updated_at"] = time.monotonic()
    return state, False


# ── Wizard rendering (pure; option lists come from the store) ────────


def build_category_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_WZ_CRYPTO,
                    callback_data=f"{CB_PREFIX}:{OP_WCAT}:1",
                ),
                InlineKeyboardButton(
                    BTN_WZ_CASH,
                    callback_data=f"{CB_PREFIX}:{OP_WCAT}:2",
                ),
            ],
            [
                InlineKeyboardButton(
                    BTN_WZ_CANCEL, callback_data=f"{CB_PREFIX}:{OP_WCANCEL}"
                )
            ],
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_WZ_CANCEL, callback_data=f"{CB_PREFIX}:{OP_WCANCEL}"
                )
            ]
        ]
    )


def _options_keyboard(step: str, options: tuple[str, ...]) -> InlineKeyboardMarkup:
    op = OP_WASSET if step == "asset" else OP_WNET
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                value, callback_data=f"{CB_PREFIX}:{op}:{index}"
            )
        ]
        for index, value in enumerate(options, start=1)
    ]
    extra: list[InlineKeyboardButton] = []
    if step == "network":
        extra.append(
            InlineKeyboardButton(
                BTN_WZ_NO_NETWORK, callback_data=f"{CB_PREFIX}:{OP_WNONE}"
            )
        )
    extra.append(
        InlineKeyboardButton(
            BTN_WZ_MANUAL, callback_data=f"{CB_PREFIX}:{OP_WMANUAL}"
        )
    )
    rows.append(extra)
    rows.append(
        [
            InlineKeyboardButton(
                BTN_WZ_CANCEL, callback_data=f"{CB_PREFIX}:{OP_WCANCEL}"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _network_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_WZ_NO_NETWORK, callback_data=f"{CB_PREFIX}:{OP_WNONE}"
                )
            ],
            [
                InlineKeyboardButton(
                    BTN_WZ_CANCEL, callback_data=f"{CB_PREFIX}:{OP_WCANCEL}"
                )
            ],
        ]
    )


def _instructions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_WZ_SKIP, callback_data=f"{CB_PREFIX}:{OP_WSKIP}"
                )
            ],
            [
                InlineKeyboardButton(
                    BTN_WZ_CANCEL, callback_data=f"{CB_PREFIX}:{OP_WCANCEL}"
                )
            ],
        ]
    )


def build_review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_WZ_CONFIRM,
                    callback_data=f"{CB_PREFIX}:{OP_WCONFIRM}",
                ),
                InlineKeyboardButton(
                    BTN_WZ_EDIT_FIELDS,
                    callback_data=f"{CB_PREFIX}:{OP_WMENU}",
                ),
            ],
            [
                InlineKeyboardButton(
                    BTN_WZ_CANCEL, callback_data=f"{CB_PREFIX}:{OP_WCANCEL}"
                )
            ],
        ]
    )


def _menu_keyboard(mode: str) -> InlineKeyboardMarkup:
    pairs = list(enumerate(_MENU_FIELDS, start=1))
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                _MENU_BUTTON_LABELS[field],
                callback_data=f"{CB_PREFIX}:{OP_WFIELD}:{index}",
            )
            for index, field in pairs[offset : offset + 2]
        ]
        for offset in range(0, len(pairs), 2)
    ]
    if mode == "add":
        rows.append(
            [
                InlineKeyboardButton(
                    BTN_WZ_REVIEW, callback_data=f"{CB_PREFIX}:{OP_WREVIEW}"
                ),
                InlineKeyboardButton(
                    BTN_WZ_CANCEL, callback_data=f"{CB_PREFIX}:{OP_WCANCEL}"
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    BTN_WZ_SAVE, callback_data=f"{CB_PREFIX}:{OP_WSAVE}"
                ),
                InlineKeyboardButton(
                    BTN_WZ_CANCEL, callback_data=f"{CB_PREFIX}:{OP_WCANCEL}"
                ),
            ]
        )
    return InlineKeyboardMarkup(rows)


def build_wizard_summary(state: dict) -> str:
    """The staged values — full destination, admin's own chat
    only (the same exposure as the existing edit template)."""
    category = state.get("category")
    dest_label = DEST_LABEL.get(category or "", "العنوان")
    return (
        f"الفئة: {category or EMPTY_FIELD}\n"
        f"الاسم: {state.get('display_name') or EMPTY_FIELD}\n"
        f"العملة: {state.get('asset') or EMPTY_FIELD}\n"
        f"الشبكة: {state.get('network') or EMPTY_FIELD}\n"
        f"المزود: {state.get('provider') or EMPTY_FIELD}\n"
        f"{dest_label}: {state.get('destination') or EMPTY_FIELD}\n"
        f"الملاحظات: {state.get('instructions') or EMPTY_FIELD}\n"
        f"الحد الأدنى للإيداع: {_min_deposit_display(state)}"
    )


def _min_deposit_display(state: dict) -> str:
    """Staged minimum in the staged asset's unit — never invented.

    ``None`` renders as not-configured; an unregistered asset falls
    back to the raw integer units so the review text never breaks.
    """
    units = state.get("min_deposit_units")
    if units is None:
        return "غير مضبوط"
    try:
        return str(
            asset_units.units_to_asset_decimal(
                units, state.get("asset")
            )
        )
    except asset_units.AssetUnitsError:
        return str(units)


def build_review_text(state: dict) -> str:
    return f"🔎 مراجعة وسيلة الدفع\n{WZ_RULE}\n\n{build_wizard_summary(state)}"


def build_menu_text(state: dict) -> str:
    if state.get("mode") == "edit":
        header = f"✏️ تعديل وسيلة الدفع #{state.get('method_id')}"
    else:
        header = "✏️ تعديل البيانات قبل الحفظ"
    return (
        f"{header}\n{WZ_RULE}\n\n{build_wizard_summary(state)}\n\n"
        "اختر الحقل الذي تريد تعديله:"
    )


def _missing_required(state: dict) -> list[str]:
    return [
        _FIELD_LABELS[field]
        for field in _REQUIRED_FIELDS
        if not state.get(field)
    ]


def _distinct_options(state: dict, step: str) -> tuple[str, ...]:
    """Values already held by the system (bounded, deduplicated).

    Empty on any read failure — the step then falls back to free
    text, so a transient database error never strands the wizard.
    """
    try:
        methods = store.list_payment_methods()
    except Exception:
        logger.debug("Payment-method wizard options read failed", exc_info=True)
        return ()
    category = state.get("category")
    seen: list[str] = []
    for method in methods:
        if category and method.category != category:
            continue
        value = method.network if step == "network" else method.asset
        if value and value not in seen:
            seen.append(value)
    return tuple(seen[:_MAX_OPTIONS])


def _render_step(state: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """Prompt + keyboard for the state's CURRENT step (refreshes the
    option snapshot the selection callbacks validate against)."""
    step = state.get("step")
    if step == "category":
        return WZ_PROMPT_CATEGORY, build_category_keyboard()
    if step == "asset":
        options = _distinct_options(state, "asset")
        state["asset_options"] = options
        if options:
            state["manual"] = False
            return WZ_SELECT_ASSET, _options_keyboard("asset", options)
        state["manual"] = True
        return WZ_PROMPT_ASSET, _cancel_keyboard()
    if step == "network":
        options = _distinct_options(state, "network")
        state["asset_options"] = options
        if options:
            state["manual"] = False
            return WZ_SELECT_NETWORK, _options_keyboard("network", options)
        state["manual"] = True
        return WZ_PROMPT_NETWORK, _network_prompt_keyboard()
    if step == "display_name":
        return WZ_PROMPT_NAME, _cancel_keyboard()
    if step == "provider":
        return WZ_PROMPT_PROVIDER, _cancel_keyboard()
    if step == "destination":
        return WZ_PROMPT_DESTINATION, _cancel_keyboard()
    if step == "min_deposit_units":
        if not state.get("asset"):
            return WZ_PROMPT_MIN_DEPOSIT_NO_ASSET, _cancel_keyboard()
        return (
            f"{WZ_PROMPT_MIN_DEPOSIT}\n\n"
            f"العملة الحالية: {state.get('asset')}",
            _cancel_keyboard(),
        )
    if step == "instructions":
        return WZ_PROMPT_INSTRUCTIONS, _instructions_keyboard()
    if step == "review":
        return build_review_text(state), build_review_keyboard()
    if step == "menu":
        return build_menu_text(state), _menu_keyboard(state.get("mode", "add"))
    raise ValueError(f"unknown wizard step: {step!r}")


def _next_step_after(state: dict, field: str) -> str:
    if state.get("mode") == "edit":
        return "menu"
    if state.get("editing"):
        return "review"
    if field == "category":
        return "asset"
    if field == "asset":
        # Network applies to crypto; cash methods stage network=None
        # (the store maps that to NULL exactly like the "-" sentinel).
        return (
            "network"
            if state.get("category") == store.CATEGORY_CRYPTO
            else "display_name"
        )
    if field == "network":
        return "display_name"
    if field == "display_name":
        return "provider"
    if field == "provider":
        return "destination"
    if field == "destination":
        return "min_deposit_units"
    if field == "min_deposit_units":
        return "instructions"
    return "review"  # instructions — optional, reached last


def _commit_field(state: dict, field: str, value) -> None:
    state[field] = value
    state["manual"] = False
    state["asset_options"] = ()
    if field == "asset":
        # The staged minimum belongs to the OLD asset's scale:
        # clearing it forces a fresh entry against the new asset
        # (the store re-enforces the same rule at save time).
        state["min_deposit_units"] = None
    next_step = _next_step_after(state, field)
    if next_step in ("review", "menu"):
        state["editing"] = False
    state["step"] = next_step


# ── Wizard starters ───────────────────────────────────────────────────


async def _start_add_wizard(actor: int, message) -> None:
    _WIZARD_STATES[actor] = _new_wizard_state("add")
    logger.info("Payment method wizard started: admin=%d mode=add", actor)
    text, markup = _render_step(_WIZARD_STATES[actor])
    await message.reply_text(text, reply_markup=markup)


async def _start_edit_wizard(actor: int, method_id: int, message) -> None:
    try:
        method = store.get_payment_method(method_id)
    except Exception:
        logger.exception(
            "Payment method wizard edit load failed: id=%s admin=%d",
            method_id,
            actor,
        )
        await message.reply_text(MSG_ERROR)
        return
    if method is None:
        await message.reply_text(MSG_NOT_FOUND)
        return
    state = _new_wizard_state("edit")
    state["method_id"] = method.id
    state["category"] = method.category
    state["display_name"] = method.display_name
    state["asset"] = method.asset
    state["network"] = method.network
    state["provider"] = method.provider
    state["destination"] = method.destination
    state["instructions"] = method.instructions
    state["min_deposit_units"] = method.min_deposit_units
    _WIZARD_STATES[actor] = state
    logger.info(
        "Payment method wizard started: admin=%d mode=edit id=%d",
        actor,
        method.id,
    )
    text, markup = _render_step(state)
    await message.reply_text(text, reply_markup=markup)


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
    """``/addpm`` → interactive wizard; ``/addpm <form>`` → legacy pipe.

    Authorization runs BEFORE either path, so a non-admin never
    reaches the wizard state machine.
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

    body = _command_body(message)
    if not body:
        # The interactive wizard (requested UX).  The pipe form below
        # stays byte-for-byte compatible for existing scripts.
        await _start_add_wizard(actor, message)
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
    """``/editpm <id>`` → interactive field menu; ``<id> | <form>`` →
    legacy full replace (unchanged); the id stays stable either way.
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

    body = _command_body(message)
    if body.isascii() and body.isdigit() and int(body) > 0:
        # /editpm <id> → interactive edit menu (the pipe form below
        # stays unchanged for existing scripts).
        await _start_edit_wizard(actor, int(body), message)
        return
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


async def wizard_text_input(update, context) -> None:
    """Free-text answers for the payment-method wizard.

    Registered in its OWN handler group (group 10 in bot.py) and
    completely SELF-GATED: silent unless THIS private chat's admin
    holds a live wizard state on a text-input step, so ordinary chat
    and every other text flow are untouched.  Identity comes from
    ``effective_user`` only (never from message text), authorization
    is re-checked before validation, and validation delegates to the
    EXISTING store validators — their Arabic errors reach the admin
    verbatim.  A validation failure keeps the state so the admin can
    simply retry.
    """
    message = getattr(update, "message", None)
    if message is None:
        return
    if _non_private_chat(update):
        return
    actor = _actor_id(getattr(getattr(update, "effective_user", None), "id", None))
    if actor is None:
        return
    state, expired = _wizard_state(actor)
    if state is None:
        if expired:
            await message.reply_text(MSG_WIZARD_STALE)
        return  # not our state — stay silent like the other catch-alls
    if not is_admin(actor):
        # Authorization can be revoked mid-flow: drop the staged form
        # and never validate or arm anything without auth.
        _WIZARD_STATES.pop(actor, None)
        await message.reply_text(MSG_ADMIN_ONLY)
        return
    step = state.get("step")
    if step not in _TEXT_STEPS or (
        step in ("asset", "network") and not state.get("manual")
    ):
        # Category/review/menu and suggestion steps expect buttons —
        # answer with the guidance line instead of swallowing the text.
        await message.reply_text(MSG_WIZARD_INVALID_OPTION)
        return
    validator = _FIELD_VALIDATORS.get(step)
    try:
        if step == "min_deposit_units":
            # Needs the STAGED asset for its scale — validated by the
            # store; the asset_units registry is the only scale
            # source (no literal, no default scale here).
            value = store.validate_min_deposit_units(
                getattr(message, "text", None), state.get("asset")
            )
        elif validator is not None:
            value = validator(getattr(message, "text", None))
        else:
            return
    except PaymentMethodValidationError as exc:
        await message.reply_text(f"❌ {exc}{MSG_WIZARD_RETRY}")
        return
    _commit_field(state, step, value)
    try:
        text, markup = _render_step(state)
    except Exception:
        logger.exception("Payment method wizard render failed: admin=%d", actor)
        _WIZARD_STATES.pop(actor, None)
        await message.reply_text(MSG_ERROR)
        return
    await message.reply_text(text, reply_markup=markup)


async def _wizard_step_edit(query, state: dict) -> None:
    text, markup = _render_step(state)
    await _safe_edit(query, text, markup)


async def _wizard_callback(query, actor: int, op: str, ref: int | None) -> None:
    """``pm:<wizard op>`` — the staged state is located by the
    TELEGRAM ACTOR (``query.from_user``), never by the payload.
    Another admin therefore has no handle on this wizard at all, and
    a stale/expired/unknown button answers with a clear Arabic
    message while mutating nothing.
    """
    state, _expired = _wizard_state(actor)
    if state is None:
        await _safe_answer(query, MSG_WIZARD_STALE)
        return
    if not is_admin(actor):
        _WIZARD_STATES.pop(actor, None)
        await _safe_answer(query, MSG_ADMIN_ONLY)
        return
    try:
        if op == OP_WCANCEL:
            _WIZARD_STATES.pop(actor, None)
            await _safe_answer(query, None)
            await _safe_edit(query, MSG_WIZARD_CANCELED, None)
            return

        if op == OP_WCAT:
            if state.get("step") != "category" or ref not in (1, 2):
                await _safe_answer(query, MSG_WIZARD_INVALID_OPTION)
                return
            _commit_field(
                state,
                "category",
                store.CATEGORY_CRYPTO if ref == 1 else store.CATEGORY_CASH,
            )
            await _wizard_step_edit(query, state)
            return

        if op in (OP_WASSET, OP_WNET):
            step = "asset" if op == OP_WASSET else "network"
            if state.get("step") != step:
                await _safe_answer(query, MSG_WIZARD_STALE)
                return
            options = state.get("asset_options") or ()
            if ref is None or ref < 1 or ref > len(options):
                # Crafted/out-of-range index: state kept, nothing set.
                await _safe_answer(query, MSG_WIZARD_INVALID_OPTION)
                return
            _commit_field(state, step, options[ref - 1])
            await _wizard_step_edit(query, state)
            return

        if op == OP_WNONE:
            if state.get("step") != "network":
                await _safe_answer(query, MSG_WIZARD_STALE)
                return
            _commit_field(state, "network", None)
            await _wizard_step_edit(query, state)
            return

        if op == OP_WMANUAL:
            if state.get("step") not in ("asset", "network"):
                await _safe_answer(query, MSG_WIZARD_STALE)
                return
            state["manual"] = True
            if state.get("step") == "asset":
                prompt = WZ_PROMPT_ASSET_MANUAL
            else:
                prompt = WZ_PROMPT_NETWORK_MANUAL
            await _safe_edit(query, prompt, _cancel_keyboard())
            return

        if op == OP_WSKIP:
            if state.get("step") != "instructions":
                await _safe_answer(query, MSG_WIZARD_STALE)
                return
            _commit_field(state, "instructions", None)
            await _wizard_step_edit(query, state)
            return

        if op == OP_WFIELD:
            if state.get("step") != "menu":
                await _safe_answer(query, MSG_WIZARD_STALE)
                return
            if ref is None or ref < 1 or ref > len(_MENU_FIELDS):
                await _safe_answer(query, MSG_WIZARD_INVALID_OPTION)
                return
            field = _MENU_FIELDS[ref - 1]
            state["manual"] = False
            state["asset_options"] = ()
            if state.get("mode") == "add":
                state["editing"] = True
            state["step"] = field
            await _wizard_step_edit(query, state)
            return

        if op == OP_WMENU:
            if state.get("mode") != "add" or state.get("step") != "review":
                await _safe_answer(query, MSG_WIZARD_STALE)
                return
            state["step"] = "menu"
            state["editing"] = False
            await _wizard_step_edit(query, state)
            return

        if op == OP_WREVIEW:
            if state.get("mode") != "add" or state.get("step") != "menu":
                await _safe_answer(query, MSG_WIZARD_STALE)
                return
            missing = _missing_required(state)
            if missing:
                await _safe_answer(
                    query, MSG_WIZARD_MISSING_PREFIX + "، ".join(missing)
                )
                return
            state["step"] = "review"
            await _wizard_step_edit(query, state)
            return

        if op == OP_WCONFIRM:
            if state.get("mode") != "add" or state.get("step") != "review":
                await _safe_answer(query, MSG_WIZARD_STALE)
                return
            missing = _missing_required(state)
            if missing:
                await _safe_answer(
                    query, MSG_WIZARD_MISSING_PREFIX + "، ".join(missing)
                )
                return
            try:
                store.validate_form(
                    state.get("category"),
                    state.get("display_name"),
                    state.get("asset"),
                    state.get("network"),
                    state.get("provider"),
                    state.get("destination"),
                    state.get("instructions"),
                )
            except PaymentMethodValidationError as exc:
                # State kept — the admin can fix it from the menu.
                await _safe_answer(query, f"❌ {exc}")
                return
            # SINGLE-USE: pop BEFORE the write so a double press can
            # never create a second row.  The local ``state`` dict
            # still holds the validated values for the insert below.
            if _WIZARD_STATES.pop(actor, None) is None:
                await _safe_answer(query, MSG_WIZARD_STALE)
                return
            try:
                created = store.create_payment_method(
                    category=state.get("category"),
                    display_name=state.get("display_name"),
                    asset=state.get("asset"),
                    network=state.get("network"),
                    provider=state.get("provider"),
                    destination=state.get("destination"),
                    instructions=state.get("instructions"),
                    min_deposit_units=state.get("min_deposit_units"),
                    created_by=actor,
                )
            except PaymentMethodValidationError as exc:
                await _safe_edit(query, f"❌ {exc}", None)
                return
            except Exception:
                # The slot is already popped and the store writes
                # transactionally → no half-written row, no stale
                # state: everything stays consistent.
                logger.exception(
                    "Payment method wizard create failed: admin=%d", actor
                )
                await _safe_edit(query, MSG_ERROR, None)
                return
            await _safe_answer(query, None)
            await _safe_edit(query, build_created_text(created), _list_again_button())
            return

        if op == OP_WSAVE:
            if state.get("mode") != "edit" or state.get("step") != "menu":
                await _safe_answer(query, MSG_WIZARD_STALE)
                return
            missing = _missing_required(state)
            if missing:
                await _safe_answer(
                    query, MSG_WIZARD_MISSING_PREFIX + "، ".join(missing)
                )
                return
            method_id = state.get("method_id")
            try:
                updated = store.update_payment_method(
                    method_id,
                    category=state.get("category"),
                    display_name=state.get("display_name"),
                    asset=state.get("asset"),
                    network=state.get("network"),
                    provider=state.get("provider"),
                    destination=state.get("destination"),
                    instructions=state.get("instructions"),
                    min_deposit_units=state.get("min_deposit_units"),
                    updated_by=actor,
                )
            except PaymentMethodValidationError as exc:
                # State kept — fix the field and save again.
                await _safe_answer(query, f"❌ {exc}")
                return
            except Exception:
                # Transactional store → the row is unchanged and the
                # staged state is still valid, so retry is safe.
                logger.exception(
                    "Payment method wizard save failed: id=%s admin=%d",
                    method_id,
                    actor,
                )
                await _safe_answer(query, MSG_ERROR)
                return
            _WIZARD_STATES.pop(actor, None)
            if updated is None:
                await _safe_edit(query, MSG_NOT_FOUND, _list_again_button())
                return
            await _safe_answer(query, None)
            save_text = f"✅ تم تعديل الوسيلة #{updated.id}"
            if updated.min_deposit_units is None:
                # Fail-closed reminder: deposits stay refused until
                # the minimum is configured again.
                save_text += (
                    "\n⚠️ الحد الأدنى للإيداع غير مضبوط — الإيداع "
                    "مرفوض حتى تضبطه."
                )
            await _safe_edit(
                query,
                save_text,
                _list_again_button(),
            )
            return

        # Defensive: parse_callback only admits the grammar above.
        await _safe_answer(query, MSG_INVALID)
    except Exception:
        logger.exception(
            "Payment method wizard callback failed: op=%s admin=%d", op, actor
        )
        await _safe_answer(query, MSG_ERROR)


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
        # Wizard buttons: same pm: family, dispatched before the
        # method-id ops (their "ref" is an option/field index, never
        # a payment-method id).
        if op in _WIZARD_OPS:
            await _wizard_callback(query, actor, op, ref)
            return

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

        # MT-ADMIN-28: explicit deposit availability — the ONLY way a
        # method becomes a user deposit destination.
        if op in (OP_DEPON, OP_DEPOFF):
            try:
                updated = store.set_payment_method_deposits_enabled(
                    method.id, op == OP_DEPON, updated_by=actor
                )
            except PaymentMethodValidationError as exc:
                # Gate refusal (no configured minimum / unregistered
                # asset) — a clear business error, not a crash.
                await _safe_answer(query, f"❌ {exc}")
                return
            if updated is None:
                await _safe_answer(query, MSG_NOT_FOUND)
                return
            await _safe_answer(query, None)
            state = (
                "تم تفعيل الإيداع لـ" if op == OP_DEPON
                else "تم تعطيل الإيداع لـ"
            )
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
