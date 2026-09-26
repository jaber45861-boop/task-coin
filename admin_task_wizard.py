"""
Generic Telegram Admin Task Creation Wizard (MT-ADMIN-05)
==========================================================

Replaces the rigid /addtask pipe with a persistent, step-by-step
wizard for configured admins in **private chats only**:

    /addtask  →  title → family → provider → target → action
              → instructions → verification → (approver) → reward
              → repeat → preview → [✅ نشر] [✏️ تعديل] [❌ إلغاء]

Key properties:

- **Persisted state**: every step lands in the ``admin_task_drafts``
  table via ``task_draft_store`` — the draft survives bot restarts,
  handler recreation and process failures.  No in-memory draft state
  exists here (module-level constants only).
- **Discovery/creation UI only**: the canonical contract building and
  validation live in ``task_creation`` (shared with the legacy pipe);
  decisions/approvals stay in ManualReviewService; rewards settle
  through the existing task/reward pipeline.  Nothing here touches
  wallets or the ledger.
- **Untrusted callbacks**: callback data carries ONLY a draft id
  lookup pointer plus an operation.  Any choice token it carries is
  re-validated against server-side whitelists (task_taxonomy) and the
  draft's own persisted step; the actor, ownership, current step and
  every collected field are re-read from the DB on each press.  Reward,
  target, instructions and title never travel in callbacks at all.
- **Authorization**: ``config.is_admin`` for access (no second ADMINS
  mechanism); drafts are per-admin, so one admin can never read or
  modify another admin's draft.  Publish re-validates everything and
  CAS-claims the draft, so a replayed/concurrent confirm creates
  exactly one task.
- **Chat isolation**: private admin chats only — group/channel
  invocations and presses produce ZERO messages.

Registered by ``bot.py``:
    CommandHandler("addtask", add_task)          → /addtask (no args)
    CallbackQueryHandler(wizard_callback, "^atw:")
    MessageHandler(TEXT & ~COMMAND & PRIVATE, wizard_text_input)
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import db
import task_draft_store
from config import ADMINS, CHANNELS, is_admin
from task_creation import TaskCreationError, TaskSpec, create_task_from_spec
from task_draft_store import DRAFT_STATUS_PUBLISHED, TaskDraft
from task_taxonomy import (
    ACTION_JOIN_CHANNEL,
    ACTION_LABELS,
    ACTIONS_BY_PROVIDER,
    FAMILIES,
    FAMILY_LABELS,
    FAMILY_PROVIDERS,
    GENERIC_PROVIDER_SET,
    PROVIDER_LABELS,
    PROVIDER_TELEGRAM,
    VERIFICATION_APPROVAL,
    VERIFICATION_AUTO,
    VERIFICATION_LABELS,
    VERIFICATION_MANUAL,
    VERIFICATION_MODE_SET,
    parse_non_negative_int,
    parse_positive_int,
    validate_instructions,
    validate_target_ref,
    validate_title,
)

logger = logging.getLogger(__name__)

# ── Callback protocol ─────────────────────────────────────────────────
# ``atw:<draft_id>:<op>[:<value>]`` — draft id is the ONLY identifier;
# every value is untrusted input re-validated against server whitelists.

CALLBACK_PREFIX = "atw"

OP_FAMILY = "family"
OP_PROVIDER = "provider"
OP_ACTION = "action"
OP_VERIF = "verif"
OP_APPROVER = "approver"
OP_REPEAT = "repeat"
OP_EDIT = "edit"
OP_CONFIRM = "confirm"
OP_CANCEL = "cancel"

_OPS_WITH_VALUE = (
    OP_FAMILY, OP_PROVIDER, OP_ACTION, OP_VERIF, OP_APPROVER,
    OP_REPEAT, OP_EDIT,
)
_OPS_WITHOUT_VALUE = (OP_CONFIRM, OP_CANCEL)
_ALL_OPS = frozenset(_OPS_WITH_VALUE) | frozenset(_OPS_WITHOUT_VALUE)

# op → the wizard step that produced the button (for _next_step).
_STEP_BY_OP = {
    OP_FAMILY: "family",
    OP_PROVIDER: "provider",
    OP_ACTION: "action",
    OP_VERIF: "verification",
    OP_APPROVER: "approver",
    OP_REPEAT: "repeat",
}

# ── Steps ─────────────────────────────────────────────────────────────

STEP_TITLE = "title"
STEP_FAMILY = "family"
STEP_PROVIDER = "provider"
STEP_TARGET = "target"
STEP_ACTION = "action"
STEP_INSTRUCTIONS = "instructions"
STEP_VERIFICATION = "verification"
STEP_APPROVER = "approver"
STEP_REWARD = "reward"
STEP_REPEAT = "repeat"
STEP_REPEAT_HOURS = "repeat_hours"
STEP_PREVIEW = "preview"

STEP_ORDER = (
    STEP_TITLE,
    STEP_FAMILY,
    STEP_PROVIDER,
    STEP_TARGET,
    STEP_ACTION,
    STEP_INSTRUCTIONS,
    STEP_VERIFICATION,
    STEP_APPROVER,
    STEP_REWARD,
    STEP_REPEAT,
    STEP_REPEAT_HOURS,
    STEP_PREVIEW,
)

# Steps that collect free text from the admin's next message.
TEXT_INPUT_STEPS = frozenset(
    {STEP_TITLE, STEP_TARGET, STEP_INSTRUCTIONS, STEP_REWARD,
     STEP_REPEAT_HOURS}
)

# payload field written by each step.
_FIELD_BY_STEP = {
    STEP_TITLE: "title",
    STEP_FAMILY: "family",
    STEP_PROVIDER: "provider",
    STEP_TARGET: "target_ref",
    STEP_ACTION: "action",
    STEP_INSTRUCTIONS: "instructions",
    STEP_VERIFICATION: "verification",
    STEP_APPROVER: "approver_id",
    STEP_REWARD: "reward",
    STEP_REPEAT: "repeat_policy",
    STEP_REPEAT_HOURS: "repeat_hours",
}

# Preview edit menu: (payload field / step key, button label).
EDIT_MENU_FIELDS = (
    (STEP_TITLE, "📌 العنوان"),
    (STEP_FAMILY, "🌐 المنصة"),
    (STEP_TARGET, "🎯 الهدف"),
    (STEP_ACTION, "⚡ الإجراء"),
    (STEP_INSTRUCTIONS, "📝 التعليمات"),
    (STEP_VERIFICATION, "🔍 طريقة التحقق"),
    (STEP_REWARD, "💰 المكافأة"),
    (STEP_REPEAT, "🔁 سياسة التكرار"),
)

# ── Messages (Arabic UI) ──────────────────────────────────────────────

MSG_ADMIN_ONLY = "⛔ هذا الأمر للمشرفين فقط."
MSG_INVALID = "❌ طلب غير صالح."
MSG_DRAFT_GONE = (
    "❌ هذه المسودة لم تعد متاحة. أرسل ‎/addtask‎ لبدء مسودة جديدة."
)
MSG_DENIED = "⛔ لا تملك صلاحية تعديل هذه المسودة."
MSG_ERROR = "❌ حدث خطأ مؤقتًا، حاول مرة أخرى."
MSG_CANCELLED = "❌ تم إلغاء إنشاء المهمة."
MSG_PUBLISHED_TOAST = "✅ تم نشر المهمة"

MSG_TITLE_PROMPT = (
    "➕ إضافة مهمة\n\n"
    "1️⃣ ما عنوان المهمة؟\n"
    "أرسل العنوان في رسالة واحدة (حتى 200 حرف)."
)
MSG_FAMILY_PROMPT = "2️⃣ اختر نوع المهمة:"
MSG_PROVIDER_PROMPT = "3️⃣ اختر المنصة (provider):"
MSG_TARGET_PROMPT_GENERIC = (
    "4️⃣ أرسل الهدف (رابط أو معرّف المهمة):"
)
MSG_TARGET_PROMPT_TELEGRAM = (
    "4️⃣ أرسل معرّف القناة المسجّل (channel_slug).\n"
    "مثال: main — استخدم ‎/listchannels‎ لعرض السجل."
)
MSG_ACTION_PROMPT = "5️⃣ اختر الإجراء المطلوب:"
MSG_INSTRUCTIONS_PROMPT = (
    "6️⃣ أرسل تعليمات المهمة للمستخدم:\n"
    "تعليمات واضحة لما عليه فعله (حتى 1000 حرف)."
)
MSG_VERIFICATION_PROMPT = "7️⃣ اختر طريقة التحقق:"
MSG_APPROVER_PROMPT = (
    "8️⃣ اختر المراجع المختص بالموافقة على هذه المهمة:"
)
MSG_REWARD_PROMPT = (
    "9️⃣ أرسل المكافأة (نقاط): رقم صحيح من 0 فأكثر."
)
MSG_REPEAT_PROMPT = "🔟 اختر سياسة التكرار:"
MSG_REPEAT_HOURS_PROMPT = (
    "🔁 كم ساعة بين كل دورة؟ أرسل رقمًا صحيحًا 1 أو أكثر."
)
MSG_EDIT_PROMPT = "✏️ اختر ما تريد تعديله:"

CANCEL_LABEL = "❌ إلغاء"


class DraftGoneError(Exception):
    """Draft missing/stale/closed — safe failure, no mutation."""


class DraftAccessError(Exception):
    """Actor does not own the draft (forged/cross-admin pointer)."""


# ── Callback parsing (strict, untrusted input) ────────────────────────


def parse_callback(data: object) -> tuple[int, str, str | None] | None:
    """Parse ``atw:<draft_id>:<op>[:<value>]`` — or None.

    Rejects everything malformed: non-strings, wrong arity, non-ASCII
    or non-positive draft ids, unknown ops, missing/extra values.
    The returned draft id is a LOOKUP POINTER only.
    """
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if parts[0] != CALLBACK_PREFIX:
        return None
    if len(parts) not in (3, 4):
        return None
    raw_id, op = parts[1], parts[2]
    if not raw_id.isascii() or not raw_id.isdigit():
        return None
    draft_id = int(raw_id)
    if draft_id <= 0:
        return None
    if op in _OPS_WITH_VALUE:
        if len(parts) != 4 or not parts[3]:
            return None
        return draft_id, op, parts[3]
    if op in _OPS_WITHOUT_VALUE:
        if len(parts) != 3:
            return None
        return draft_id, op, None
    return None


def _cb(draft_id: int, op: str, value: str | None = None) -> str:
    data = f"{CALLBACK_PREFIX}:{draft_id}:{op}"
    if value is not None:
        data += f":{value}"
    return data


# ── Draft state helpers ───────────────────────────────────────────────


def _auto_available(payload: dict) -> bool:
    """The ONLY automatic verifier: Telegram membership join."""
    return (
        payload.get("provider") == PROVIDER_TELEGRAM
        and payload.get("action") == ACTION_JOIN_CHANNEL
    )


def _step_satisfied(step: str, payload: dict) -> bool:
    """True when *step* needs no more input for this payload."""
    if step == STEP_PREVIEW:
        return True
    if step == STEP_APPROVER:
        # Only meaningful in approval mode; otherwise implicitly done.
        return (
            payload.get("verification") != VERIFICATION_APPROVAL
            or payload.get("approver_id") is not None
        )
    if step == STEP_REPEAT_HOURS:
        return (
            payload.get("repeat_policy") != db.REPEAT_POLICY_REPEATABLE
            or payload.get("repeat_hours") is not None
        )
    if step == STEP_FAMILY:
        return bool(payload.get("family")) or bool(
            payload.get("provider")
        )
    field = _FIELD_BY_STEP[step]
    value = payload.get(field)
    if field == "reward":
        return value is not None
    return bool(value)


def _next_step(current: str, payload: dict) -> str:
    """First unsatisfied step after *current*, else the preview."""
    try:
        index = STEP_ORDER.index(current)
    except ValueError:
        return STEP_PREVIEW
    for step in STEP_ORDER[index + 1:]:
        if step == STEP_PREVIEW:
            return STEP_PREVIEW
        if not _step_satisfied(step, payload):
            return step
    return STEP_PREVIEW


def _drop_provider_dependents(payload: dict) -> None:
    """Provider changed → its action/verification no longer apply."""
    payload.pop("action", None)
    payload.pop("verification", None)


# ── Selection handlers (button ops) ───────────────────────────────────
# Each returns a NEW payload; every token is validated against the
# server-side whitelist before it is stored.


def _select_family(payload: dict, value: str) -> dict:
    if value not in FAMILIES:
        raise ValueError("❌ نوع مهمة غير صالح.")
    payload = dict(payload)
    payload["family"] = value
    provider = payload.get("provider")
    if provider and provider not in FAMILY_PROVIDERS[value]:
        payload.pop("provider", None)
        _drop_provider_dependents(payload)
    return payload


def _select_provider(payload: dict, value: str) -> dict:
    if value not in GENERIC_PROVIDER_SET:
        raise ValueError("❌ منصة غير صالحة.")
    family = payload.get("family")
    if family and value not in FAMILY_PROVIDERS[family]:
        raise ValueError("❌ هذه المنصة غير متاحة داخل هذا النوع.")
    payload = dict(payload)
    if payload.get("provider") != value:
        payload["provider"] = value
        _drop_provider_dependents(payload)
    return payload


def _select_action(payload: dict, value: str) -> dict:
    provider = payload.get("provider")
    allowed = ACTIONS_BY_PROVIDER.get(provider) if provider else None
    if allowed is None or value not in allowed:
        raise ValueError("❌ هذا الإجراء غير متاح للمنصة المختارة.")
    payload = dict(payload)
    if payload.get("action") != value:
        payload["action"] = value
        # auto capability is provider+action specific — drop it when
        # the new action can no longer be auto-verified.
        if (
            payload.get("verification") == VERIFICATION_AUTO
            and not _auto_available(payload)
        ):
            payload.pop("verification", None)
    return payload


def _select_verification(payload: dict, value: str) -> dict:
    if value not in VERIFICATION_MODE_SET:
        raise ValueError("❌ طريقة تحقق غير صالحة.")
    if value == VERIFICATION_AUTO:
        if not _auto_available(payload):
            raise ValueError(
                "❌ التحقق التلقائي متاح فقط لمهام انضمام قنوات Telegram."
            )
        slug = (payload.get("target_ref") or "").strip().lstrip("@")
        if not slug:
            raise ValueError("❌ أدخل هدف المهمة أولًا.")
        if slug not in CHANNELS:
            raise ValueError(
                f"❌ الـ channel_slug '{slug}' غير موجود في سجل القنوات."
            )
    payload = dict(payload)
    payload["verification"] = value
    return payload


def _select_approver(payload: dict, value: str) -> dict:
    if not value.isascii() or not value.isdigit():
        raise ValueError("❌ مراجع غير صالح.")
    approver_id = int(value)
    if approver_id <= 0 or approver_id not in ADMINS:
        raise ValueError("❌ هذا المستخدم ليس مشرفًا مصرحًا.")
    payload = dict(payload)
    payload["approver_id"] = approver_id
    return payload


def _select_repeat(payload: dict, value: str) -> dict:
    if value not in db.REPEAT_POLICIES:
        raise ValueError("❌ سياسة تكرار غير صالحة.")
    payload = dict(payload)
    payload["repeat_policy"] = value
    if value != db.REPEAT_POLICY_REPEATABLE:
        payload.pop("repeat_hours", None)
    return payload


_SELECTORS = {
    OP_FAMILY: _select_family,
    OP_PROVIDER: _select_provider,
    OP_ACTION: _select_action,
    OP_VERIF: _select_verification,
    OP_APPROVER: _select_approver,
    OP_REPEAT: _select_repeat,
}


# ── Text-input step validation ────────────────────────────────────────


def _validate_text_step(step: str, text: str, payload: dict) -> dict:
    """Validate one free-text answer and return the updated payload.

    Raises ValueError (Arabic message) on any invalid input — the
    draft keeps its current step so the admin can retry.
    """
    payload = dict(payload)
    if step == STEP_TITLE:
        payload["title"] = validate_title(text)
    elif step == STEP_TARGET:
        payload["target_ref"] = validate_target_ref(text)
    elif step == STEP_INSTRUCTIONS:
        payload["instructions"] = validate_instructions(text)
    elif step == STEP_REWARD:
        payload["reward"] = parse_non_negative_int(
            text, field="المكافأة"
        )
    elif step == STEP_REPEAT_HOURS:
        payload["repeat_hours"] = parse_positive_int(
            text, field="عدد الساعات"
        )
    else:  # pragma: no cover — guarded by TEXT_INPUT_STEPS
        raise ValueError(MSG_INVALID)
    return payload


# ── Rendering ─────────────────────────────────────────────────────────


def _cancel_row(draft_id: int) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(CANCEL_LABEL, callback_data=_cb(draft_id, OP_CANCEL))]


def _rows(pairs: list[tuple[str, str]], draft_id: int, op: str,
          per_row: int = 1) -> list[list[InlineKeyboardButton]]:
    """[[label, value]] → keyboard rows of callback buttons."""
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(pairs), per_row):
        chunk = pairs[i:i + per_row]
        rows.append([
            InlineKeyboardButton(
                label, callback_data=_cb(draft_id, op, value)
            )
            for label, value in chunk
        ])
    return rows


def build_preview_text(draft: TaskDraft) -> str:
    """Server-generated preview from the PERSISTED draft only."""
    p = draft.payload
    provider = p.get("provider") or "—"
    action = p.get("action") or "—"
    verification = p.get("verification") or "—"
    lines = [
        "👁️ معاينة المهمة",
        "",
        f"📌 العنوان: {p.get('title') or '—'}",
        f"📝 التعليمات: {p.get('instructions') or '—'}",
        f"🌐 المنصة: {provider}"
        + (f" ({PROVIDER_LABELS.get(provider, '')})"
           if provider != "—" else ""),
        f"⚡ الإجراء: {action}"
        + (f" ({ACTION_LABELS.get(action, '')})"
           if action != "—" else ""),
        f"🎯 الهدف: {p.get('target_ref') or '—'}",
        f"🔍 التحقق: {VERIFICATION_LABELS.get(verification, verification)}",
    ]
    if verification != VERIFICATION_AUTO:
        approver = p.get("approver_id") or draft.admin_user_id
        lines.append(f"👤 المراجع المختص: {approver}")
    lines.append(f"💰 المكافأة: {p.get('reward') if p.get('reward') is not None else '—'} نقطة")
    if p.get("repeat_policy") == db.REPEAT_POLICY_REPEATABLE:
        hours = p.get("repeat_hours")
        lines.append(f"🔁 التكرار: كل {hours} ساعة" if hours
                     else "🔁 التكرار: قابلة للتكرار")
    else:
        lines.append("🔁 التكرار: مرة واحدة")
    return "\n".join(lines)


def _preview_keyboard(draft: TaskDraft) -> InlineKeyboardMarkup:
    draft_id = draft.draft_id
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نشر المهمة",
                              callback_data=_cb(draft_id, OP_CONFIRM))],
        [InlineKeyboardButton("✏️ تعديل",
                              callback_data=_cb(draft_id, OP_EDIT, "menu"))],
        _cancel_row(draft_id),
    ])


def _edit_menu_keyboard(draft: TaskDraft) -> InlineKeyboardMarkup:
    draft_id = draft.draft_id
    # EDIT_MENU_FIELDS stores (step_key, label); _rows wants
    # (button_label, op_value) — swap explicitly.
    pairs = list(EDIT_MENU_FIELDS)
    if draft.payload.get("verification") == VERIFICATION_APPROVAL:
        pairs.insert(
            next(i for i, (key, _) in enumerate(pairs)
                 if key == STEP_VERIFICATION) + 1,
            (STEP_APPROVER, "👤 المراجع المختص"),
        )
    rows = _rows(
        [(label, key) for key, label in pairs],
        draft_id, OP_EDIT, per_row=2,
    )
    rows.append([
        InlineKeyboardButton("🔙 معاينة",
                             callback_data=_cb(draft_id, OP_EDIT, "back")),
    ])
    rows.append(_cancel_row(draft_id))
    return InlineKeyboardMarkup(rows)


def render_step(draft: TaskDraft) -> tuple[str, InlineKeyboardMarkup]:
    """(text, keyboard) for the draft's CURRENT persisted step."""
    draft_id = draft.draft_id
    p = draft.payload
    step = draft.step

    if step == STEP_TITLE:
        return MSG_TITLE_PROMPT, InlineKeyboardMarkup([_cancel_row(draft_id)])

    if step == STEP_FAMILY:
        rows = _rows(
            [(FAMILY_LABELS[f], f) for f in FAMILIES],
            draft_id, OP_FAMILY, per_row=2,
        )
        rows.append(_cancel_row(draft_id))
        return MSG_FAMILY_PROMPT, InlineKeyboardMarkup(rows)

    if step == STEP_PROVIDER:
        family = p.get("family")
        providers = FAMILY_PROVIDERS.get(family) if family else None
        if not providers:
            # Edit path without a chosen family — go back one step.
            rows = _rows(
                [(FAMILY_LABELS[f], f) for f in FAMILIES],
                draft_id, OP_FAMILY, per_row=2,
            )
            rows.append(_cancel_row(draft_id))
            return MSG_FAMILY_PROMPT, InlineKeyboardMarkup(rows)
        rows = _rows(
            [(PROVIDER_LABELS[prov], prov) for prov in providers],
            draft_id, OP_PROVIDER, per_row=2,
        )
        rows.append(_cancel_row(draft_id))
        return MSG_PROVIDER_PROMPT, InlineKeyboardMarkup(rows)

    if step == STEP_TARGET:
        prompt = (
            MSG_TARGET_PROMPT_TELEGRAM
            if p.get("provider") == PROVIDER_TELEGRAM
            else MSG_TARGET_PROMPT_GENERIC
        )
        return prompt, InlineKeyboardMarkup([_cancel_row(draft_id)])

    if step == STEP_ACTION:
        provider = p.get("provider")
        allowed = ACTIONS_BY_PROVIDER.get(provider) or ()
        rows = _rows(
            [(ACTION_LABELS[a], a) for a in allowed],
            draft_id, OP_ACTION, per_row=2,
        )
        rows.append(_cancel_row(draft_id))
        return MSG_ACTION_PROMPT, InlineKeyboardMarkup(rows)

    if step == STEP_INSTRUCTIONS:
        return (MSG_INSTRUCTIONS_PROMPT,
                InlineKeyboardMarkup([_cancel_row(draft_id)]))

    if step == STEP_VERIFICATION:
        modes = [VERIFICATION_MANUAL, VERIFICATION_APPROVAL]
        if _auto_available(p):
            modes.insert(0, VERIFICATION_AUTO)
        rows = _rows(
            [(VERIFICATION_LABELS[m], m) for m in modes],
            draft_id, OP_VERIF, per_row=1,
        )
        rows.append(_cancel_row(draft_id))
        return MSG_VERIFICATION_PROMPT, InlineKeyboardMarkup(rows)

    if step == STEP_APPROVER:
        rows = [
            [InlineKeyboardButton(
                f"👤 {admin_id}",
                callback_data=_cb(draft_id, OP_APPROVER, str(admin_id)),
            )]
            for admin_id in ADMINS
        ] or []
        rows.append(_cancel_row(draft_id))
        return MSG_APPROVER_PROMPT, InlineKeyboardMarkup(rows)

    if step == STEP_REWARD:
        return MSG_REWARD_PROMPT, InlineKeyboardMarkup([_cancel_row(draft_id)])

    if step == STEP_REPEAT:
        rows = _rows(
            [
                ("🕐 مرة واحدة", db.REPEAT_POLICY_ONE_TIME),
                ("🔁 قابلة للتكرار", db.REPEAT_POLICY_REPEATABLE),
            ],
            draft_id, OP_REPEAT, per_row=1,
        )
        rows.append(_cancel_row(draft_id))
        return MSG_REPEAT_PROMPT, InlineKeyboardMarkup(rows)

    if step == STEP_REPEAT_HOURS:
        return (MSG_REPEAT_HOURS_PROMPT,
                InlineKeyboardMarkup([_cancel_row(draft_id)]))

    # STEP_PREVIEW (and any unknown legacy step — fail toward review)
    return build_preview_text(draft), _preview_keyboard(draft)


def _published_text(task_id: int) -> str:
    task = db.get_task(task_id) or {}
    return (
        "✅ تم نشر المهمة بنجاح!\n\n"
        f"🆔 المهمة: #{task_id}\n"
        f"📌 العنوان: {task.get('title', '—')}\n"
        f"💰 النقاط: {task.get('reward', '—')}\n"
        f"📡 النوع: {task.get('type', '—')}\n\n"
        "ظهرت المهمة في قائمة المهام المتاحة."
    )


# ── Spec assembly + publish (CAS, idempotent) ─────────────────────────


def spec_from_draft(draft: TaskDraft) -> TaskSpec:
    """Rebuild the TaskSpec from the PERSISTED draft (never callbacks)."""
    p = draft.payload
    verification = p.get("verification")
    if verification not in VERIFICATION_MODE_SET:
        raise TaskCreationError("اختر طريقة التحقق أولًا.")
    reward = p.get("reward")
    if isinstance(reward, bool) or not isinstance(reward, int):
        raise TaskCreationError("أدخل مكافأة صالحة أولًا.")
    target_ref = p.get("target_ref")
    if not isinstance(target_ref, str) or not target_ref.strip():
        raise TaskCreationError("أدخل هدف المهمة أولًا.")
    instructions = p.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise TaskCreationError("أدخل تعليمات المهمة أولًا.")
    approver_id = p.get("approver_id") or draft.admin_user_id
    if verification == VERIFICATION_AUTO:
        target = {"channel_slug": target_ref.strip().lstrip("@")}
    else:
        target = {"ref": validate_target_ref(target_ref)}
    return TaskSpec(
        title=p.get("title") or "",
        description=instructions,
        provider=p.get("provider") or "",
        action=p.get("action") or "",
        target=target,
        verification=verification,
        reward=reward,
        approver_id=approver_id,
        repeat_policy=p.get("repeat_policy") or db.REPEAT_POLICY_ONE_TIME,
        repeat_hours=p.get("repeat_hours"),
    )


def publish_draft(draft_id: int, actor_id: int) -> int:
    """Create EXACTLY one task from the draft. Returns the task id.

    1. Re-read the draft (ownership + status) from the DB.
    2. Validate the complete spec again (reward, repeat, provider /
       action / verification compatibility — via task_creation).
    3. CAS-claim the draft inside ONE ``db.transaction()`` and create
       the task on that same connection: a replayed or concurrent
       confirm loses the claim, reads ``published_task_id`` and
       creates nothing.

    Raises:
        DraftGoneError / DraftAccessError: stale or foreign draft.
        ValueError (TaskCreationError, contract errors): invalid
            draft content — zero tasks created, draft stays open.
    """
    draft = task_draft_store.get_draft(draft_id)
    if draft is None:
        raise DraftGoneError(MSG_DRAFT_GONE)
    if draft.admin_user_id != actor_id:
        raise DraftAccessError(MSG_DENIED)
    if draft.status == DRAFT_STATUS_PUBLISHED:
        if draft.published_task_id is None:
            raise DraftGoneError(MSG_DRAFT_GONE)
        return draft.published_task_id  # duplicate confirm → same task
    if not draft.is_open:
        raise DraftGoneError(MSG_DRAFT_GONE)

    # Full re-validation BEFORE any write: a rejected draft creates
    # zero tasks and stays open for correction.
    spec = spec_from_draft(draft)

    with db.transaction() as conn:
        if not task_draft_store.claim_for_publish(
            conn, draft_id, actor_id
        ):
            # Lost the race — the winner's task id is authoritative.
            published = task_draft_store.read_published_task_id(
                conn, draft_id, actor_id
            )
            if published is not None:
                return published
            raise DraftGoneError(MSG_DRAFT_GONE)
        task_id = create_task_from_spec(spec, conn=conn)
        task_draft_store.mark_published(conn, draft_id, task_id)

    logger.info(
        "Wizard-published task: id=%d draft=%d admin=%d",
        task_id, draft_id, actor_id,
    )
    return task_id


# ── Telegram handlers ─────────────────────────────────────────────────


def _non_private_chat(update) -> bool:
    """True for groups/channels/unknown chats — must stay SILENT."""
    chat = getattr(getattr(update, "effective_chat", None), "type", None)
    return not (isinstance(chat, str) and chat == "private")


def _actor_id(update_or_query) -> int | None:
    user = getattr(update_or_query, "effective_user", None)
    if user is None:
        return None
    uid = getattr(user, "id", None)
    return uid if isinstance(uid, int) and not isinstance(uid, bool) else None


async def _safe_answer(query, text: str | None) -> None:
    try:
        if text is None:
            await query.answer()
        else:
            await query.answer(text)
    except Exception:  # pragma: no cover — transport fail-soft
        logger.debug("wizard callback answer failed", exc_info=True)


async def _safe_edit(query, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:  # fail-soft: stale/identical message edits
        logger.debug("wizard message edit failed", exc_info=True)


async def start_wizard(update, context) -> None:
    """/addtask entry (no arguments): resume or open the admin's draft.

    Private admin chat ONLY: groups/channels get zero replies; a
    non-admin in private gets the existing admin-only refusal.
    """
    if _non_private_chat(update):
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    actor = _actor_id(update)
    if actor is None or not is_admin(actor):
        await message.reply_text(MSG_ADMIN_ONLY)
        return
    try:
        draft = task_draft_store.get_or_create_open_draft(
            actor, STEP_TITLE, {}
        )
    except Exception:
        logger.exception("Task draft open failed for admin %s", actor)
        await message.reply_text(MSG_ERROR)
        return
    text, markup = render_step(draft)
    await message.reply_text(text, reply_markup=markup)
    logger.info(
        "Task wizard opened: admin=%d draft=%d step=%s",
        actor, draft.draft_id, draft.step,
    )


async def wizard_text_input(update, context) -> None:
    """Route the admin's next private text message into the draft.

    Registered with ``TEXT & ~COMMAND & PRIVATE``: silent unless the
    sender is an admin with an OPEN draft sitting on a text-input
    step, so ordinary chat is never swallowed by accident.
    """
    if _non_private_chat(update):
        return
    message = getattr(update, "message", None)
    text = getattr(message, "text", None)
    if not isinstance(text, str):
        return
    text = text.strip()
    if text.startswith("/"):
        return
    actor = _actor_id(update)
    if actor is None or not is_admin(actor):
        return
    draft = task_draft_store.get_open_draft(actor)
    if draft is None or draft.step not in TEXT_INPUT_STEPS:
        return

    try:
        payload = _validate_text_step(draft.step, text, draft.payload)
    except ValueError as exc:
        await message.reply_text(str(exc))  # stay on the step
        return

    next_step = _next_step(draft.step, payload)
    saved = task_draft_store.save_step(
        draft.draft_id, actor, next_step, payload
    )
    if saved is None:
        await message.reply_text(MSG_DRAFT_GONE)
        return
    next_text, next_markup = render_step(saved)
    await message.reply_text(next_text, reply_markup=next_markup)


async def wizard_callback(update, context) -> None:
    """Handle ``atw:`` presses: lookup draft → re-check everything.

    Callback data is untrusted: the draft id is a lookup pointer; the
    actor must be a config admin AND the draft's owner; the pressed
    value is validated against the server whitelist and the draft's
    persisted step; publish re-reads and re-validates from the DB.
    """
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    if _non_private_chat(update):
        # Group/channel press → silent dismissal, nothing else.
        await _safe_answer(query, None)
        return

    parsed = parse_callback(getattr(query, "data", None))
    if parsed is None:
        await _safe_answer(query, MSG_INVALID)
        return

    actor = getattr(getattr(query, "from_user", None), "id", None)
    if (
        not isinstance(actor, int)
        or isinstance(actor, bool)
        or not is_admin(actor)
    ):
        await _safe_answer(query, MSG_ADMIN_ONLY)
        return

    draft_id, op, value = parsed
    draft = task_draft_store.get_draft(draft_id)
    if draft is None:
        await _safe_answer(query, MSG_DRAFT_GONE)
        return
    if draft.admin_user_id != actor:
        await _safe_answer(query, MSG_DENIED)
        return

    # ── cancel: delete the draft (ownership-filtered) ─────────────────
    if op == OP_CANCEL:
        if task_draft_store.delete_draft(draft_id, actor):
            await _safe_answer(query, MSG_CANCELLED)
            await _safe_edit(query, MSG_CANCELLED, None)
        else:
            await _safe_answer(query, MSG_DRAFT_GONE)
        return

    # ── confirm: publish (idempotent CAS, full re-validation) ─────────
    if op == OP_CONFIRM:
        try:
            task_id = publish_draft(draft_id, actor)
        except DraftAccessError:
            await _safe_answer(query, MSG_DENIED)
        except DraftGoneError:
            await _safe_answer(query, MSG_DRAFT_GONE)
        except ValueError as exc:  # TaskCreationError / contract / bounds
            logger.warning(
                "Wizard publish rejected: draft=%d err=%s",
                draft_id, exc,
            )
            await _safe_answer(query, f"❌ تعذر النشر: {exc}")
        else:
            await _safe_answer(query, MSG_PUBLISHED_TOAST)
            await _safe_edit(query, _published_text(task_id), None)
        return

    # ── edit: menu / back / jump to a field ───────────────────────────
    if op == OP_EDIT:
        if value == "menu":
            await _safe_answer(query, None)
            await _safe_edit(query, MSG_EDIT_PROMPT,
                             _edit_menu_keyboard(draft))
            return
        if value == "back":
            text, markup = render_step(draft)
            await _safe_answer(query, None)
            await _safe_edit(query, text, markup)
            return
        if value == STEP_APPROVER and (
            draft.payload.get("verification") != VERIFICATION_APPROVAL
        ):
            await _safe_answer(query, MSG_INVALID)
            return
        if value not in _FIELD_BY_STEP:
            await _safe_answer(query, MSG_INVALID)
            return
        saved = task_draft_store.save_step(
            draft_id, actor, value, draft.payload
        )
        if saved is None:
            await _safe_answer(query, MSG_DRAFT_GONE)
            return
        text, markup = render_step(saved)
        await _safe_answer(query, None)
        await _safe_edit(query, text, markup)
        return

    # ── choice ops: validate token, persist, advance ──────────────────
    selector = _SELECTORS.get(op)
    if selector is None or value is None:
        await _safe_answer(query, MSG_INVALID)
        return
    try:
        payload = selector(draft.payload, value)
    except ValueError as exc:
        await _safe_answer(query, str(exc))
        return

    next_step = _next_step(_STEP_BY_OP[op], payload)
    saved = task_draft_store.save_step(draft_id, actor, next_step, payload)
    if saved is None:
        await _safe_answer(query, MSG_DRAFT_GONE)
        return
    text, markup = render_step(saved)
    await _safe_answer(query, None)
    await _safe_edit(query, text, markup)
    logger.debug(
        "Wizard step saved: draft=%d op=%s → step=%s",
        draft_id, op, saved.step,
    )
