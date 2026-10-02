"""
Admin Task-Request Review Surface (Mini App «إضافة مهمة»)
=========================================================

Telegram private-chat review queue for user-proposed tasks: a user
submits a proposal from the Mini App, it waits as ``pending`` in
``user_task_requests``, and THIS module is where admins discover,
inspect, edit, approve, return or reject it.

Architecture (decisions flow through the existing creation service)::

    Mini App user
      ↓  POST /api/task-requests        (task_routes — validated)
    user_task_requests (pending)        ← task_request_store
      ↓  /taskrequests                  ← this module (queue, read-only)
    📝 #<id>  (treq:view:<id>)          ← detail, server-side re-read
      ├─ ✅ موافقة   → claim CAS → task_creation.create_task_from_spec
      │               (approver = the approving admin, funded from the
      │                requesting user's wallet — the authenticated
      │                creator, wizard semantics) → mark approved,
      │                ALL inside ONE db.transaction()
      ├─ ✏️ تعديل    → prompt → store.admin_edit_field (validated,
      │               previous payload kept in history_json audit)
      ├─ ↩️ إرجاع    → prompt note → pending → changes_requested
      └─ ❌ رفض      → prompt reason → pending → rejected (terminal)

Security (every press/text is untrusted):
- callbacks carry ONLY the operation + a positive request id — a
  lookup pointer; the request, its status and its owner are re-read
  from SQLite every time
- authorization is ``config.is_admin`` on the VERIFIED presser
  identity (never from the payload), re-checked on every action;
  non-private chats are silent (MT-ADMIN-02 isolation)
- the user-facing Mini App API has NO approve/reject/return/edit-
  for-others capability: decisions exist only here
- approve never trusts a client reward/identity: the spec is built
  from the STORED (validated) payload, and the state flip is a CAS
  (``claim_for_approval``) so a replayed/concurrent approve creates
  exactly ONE task

Boundaries (this module must NOT):
- create tasks by any other path than ``task_creation
  .create_task_from_spec`` (the ONE creation service)
- decide worker proofs (ManualReviewService owns that — approving a
  request only PUBLISHES the task)
- touch wallet/ledger directly (funding happens inside the creation
  transaction via the existing ``task_funding`` path)
- render anything to non-admins or outside private chats
"""

from __future__ import annotations

import logging
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import db
import task_request_store
from config import is_admin
from task_creation import (
    TaskCreationError,
    create_task_from_spec,
    reward_units_to_text,
)
from task_taxonomy import ACTION_LABELS, PROVIDER_LABELS
from task_request_store import (
    STATUS_APPROVED,
    STATUS_CHANGES_REQUESTED,
    STATUS_PENDING,
    STATUS_REJECTED,
    TaskRequest,
    TaskRequestError,
)

logger = logging.getLogger(__name__)

# ── Callback vocabulary (closed grammar, parse-verified) ─────────────

CALLBACK_PREFIX = "treq"
OP_LIST = "list"
OP_PAGE = "page"
OP_VIEW = "view"
OP_EDIT = "edit"
OP_APPROVE = "approve"
OP_FIELD = "field"
OP_RETURN = "return"
OP_REJECT = "reject"

EDITABLE_FIELDS = ("title", "description", "target_ref", "reward")

# Small fixed page — bounded reads, mobile-friendly rendering.
PAGE_SIZE = 5

# Transient one-shot input state (reject reason / return note / admin
# field edit) held in the PTB ``context.user_data`` — never a source
# of truth: every action re-reads the request from SQLite, and a lost
# state (bot restart) just means the admin presses the button again.
INPUT_STATE_KEY = "treq_input"

# ── Arabic messages (concise, operational; no parse_mode markup) ─────

MSG_ADMIN_ONLY = "⛔ هذا الأمر للمشرفين فقط."
MSG_NO_REQUESTS = "📭 لا توجد طلبات مهام معلقة حالياً."
MSG_INVALID = "⚠️ أمر غير صالح"
MSG_STALE = "⚠️ الطلب غير صالح أو لم يعد قابلاً للمعالجة"
MSG_ERROR = "⚠️ تعذر تنفيذ العملية، حاول مرة أخرى"

LIST_HEADER = "📝 طلبات المهام المعلقة"
DETAIL_HEADER = "📝 طلب مهمة"
PAGE_OF = "الصفحة {page}/{pages}"

MSG_APPROVED = "✅ تمت الموافقة على الطلب ونشر المهمة"
MSG_APPROVED_ALREADY = "ℹ️ تمت الموافقة على هذا الطلب مسبقاً"
MSG_REJECTED_TOAST = "❌ تم رفض الطلب"
MSG_RETURNED_TOAST = "↩️ أُعيد الطلب للمستخدم للتعديل"
MSG_EDITED_TOAST = "✏️ تم تعديل بيانات الطلب"

PROMPT_REJECT = (
    "أرسل سبب الرفض (يظهر للمستخدم في طلبه)."
)
PROMPT_RETURN = (
    "أرسل ملاحظة الإرجاع للمستخدم (ما الذي يحتاج تعديلاً)."
)

BUTTON_APPROVE = "✅ موافقة"
BUTTON_EDIT = "✏️ تعديل البيانات"
BUTTON_RETURN = "↩️ إرجاع للتعديل"
BUTTON_REJECT = "❌ رفض"
BUTTON_LIST = "◀️ طلبات المعلّق"
BUTTON_REFRESH = "🔄 تحديث"
PAGE_NEXT = "التالي ▶️"
PAGE_PREV = "◀️ السابق"

STATUS_LABELS = {
    STATUS_PENDING: "⏳ قيد المراجعة",
    STATUS_APPROVED: "✅ منشورة",
    STATUS_REJECTED: "❌ مرفوضة",
    STATUS_CHANGES_REQUESTED: "✏️ تحتاج إلى تعديل",
}

FIELD_LABELS = {
    "title": "عنوان المهمة",
    "description": "وصف المهمة",
    "target_ref": "رابط/هدف المهمة",
    "reward": "المكافأة (USDT)",
}


# ── Callback parsing (UNTRUSTED input → closed op tuple) ──────────────


def _bounded_int(raw: str) -> int | None:
    if not (raw.isascii() and raw.isdigit()):
        return None
    if len(raw) > 9:
        return None
    value = int(raw)
    return value if value > 0 else None


def parse_callback(data: object) -> tuple | None:
    """``treq:<op>[:<arg>]`` → ``(op, a, b)`` or None.

    Accepts exactly the closed grammar; everything else (wrong
    prefix, extra segments, non-ASCII digits, zero/negative ids,
    unknown fields) returns None.  The returned id/field are LOOKUP
    POINTERS ONLY — never trusted without fresh server-side reads
    and the admin gate.
    """
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if not parts or parts[0] != CALLBACK_PREFIX:
        return None
    rest = parts[1:]
    if len(rest) == 1 and rest[0] == OP_LIST:
        return (OP_LIST, None, None)
    if len(rest) == 2 and rest[0] == OP_PAGE:
        page = _bounded_int(rest[1])
        if page is None:
            return None
        return (OP_PAGE, page, None)
    if len(rest) == 2 and rest[0] in (OP_VIEW, OP_EDIT, OP_APPROVE,
                                      OP_RETURN, OP_REJECT):
        request_id = _bounded_int(rest[1])
        if request_id is None:
            return None
        return (rest[0], request_id, None)
    if len(rest) == 3 and rest[0] == OP_FIELD:
        if rest[1] not in EDITABLE_FIELDS:
            return None
        request_id = _bounded_int(rest[2])
        if request_id is None:
            return None
        return (OP_FIELD, request_id, rest[1])
    return None


def _callback(op: str, arg=None) -> str:
    if arg is None:
        return f"{CALLBACK_PREFIX}:{op}"
    return f"{CALLBACK_PREFIX}:{op}:{arg}"


# ── Presentation builders (pure — no I/O, no authorization) ───────────


def _request_label(request: TaskRequest) -> str:
    title = str(request.payload.get("title") or "—")
    if len(title) > 32:
        title = title[:31] + "…"
    return f"📝 #{request.request_id} — {title}"


def build_list_text(requests: list[TaskRequest], page: int, pages: int) -> str:
    if not requests:
        return MSG_NO_REQUESTS
    lines = [LIST_HEADER, ""]
    for r in requests:
        title = str(r.payload.get("title") or "—")
        lines.append(
            f"#{r.request_id} · {STATUS_LABELS.get(r.status, r.status)}"
        )
        lines.append(f"  {title}")
    lines.append("")
    lines.append(PAGE_OF.format(page=page + 1, pages=pages))
    return "\n".join(lines)


def build_list_keyboard(
    requests: list[TaskRequest], page: int, pages: int
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            _request_label(r), callback_data=_callback(OP_VIEW, r.request_id)
        )]
        for r in requests
    ]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                PAGE_PREV, callback_data=_callback(OP_PAGE, page)
            ))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(
                PAGE_NEXT, callback_data=_callback(OP_PAGE, page + 2)
            ))
        if nav:
            rows.append(nav)
    rows.append([InlineKeyboardButton(
        BUTTON_REFRESH, callback_data=_callback(OP_LIST)
    )])
    return InlineKeyboardMarkup(rows)


def _owner_label(user_id: int) -> str:
    user = db.get_user(user_id)
    if not user:
        return str(user_id)
    username = user.get("username")
    if username:
        return f"{user_id} · @{username}"
    first_name = user.get("first_name")
    if first_name:
        return f"{user_id} · {first_name}"
    return str(user_id)


def _provider_label(provider: object) -> str:
    if isinstance(provider, str):
        return PROVIDER_LABELS.get(provider, provider)
    return "—"


def _action_label(action: object) -> str:
    if isinstance(action, str):
        return ACTION_LABELS.get(action, action)
    return "—"


def build_detail_text(request: TaskRequest) -> str:
    p = request.payload
    reward_text = reward_units_to_text(request.reward_units)
    lines = [
        f"{DETAIL_HEADER} #{request.request_id}",
        f"الحالة: {STATUS_LABELS.get(request.status, request.status)}",
        f"صاحب الطلب: {_owner_label(request.user_id)}",
        f"تاريخ الإرسال: {request.created_at}",
        f"آخر تحديث: {request.updated_at}",
        "",
        f"العنوان: {p.get('title') or '—'}",
        f"الوصف: {p.get('description') or '—'}",
        f"النوع: {_provider_label(p.get('provider'))} · "
        f"{_action_label(p.get('action'))}",
        f"الهدف: {p.get('target_ref') or '—'}",
        f"المكافأة: {reward_text} USDT",
    ]
    if request.status == STATUS_APPROVED and request.published_task_id:
        lines.append(f"المهمة المنشورة: #{request.published_task_id}")
    if request.status in (STATUS_REJECTED, STATUS_CHANGES_REQUESTED):
        reason = request.decision_reason or "—"
        kind = "سبب الرفض" if request.status == STATUS_REJECTED else "ملاحظة"
        lines.append(f"{kind}: {reason}")
    return "\n".join(lines)


def build_detail_keyboard(request: TaskRequest) -> InlineKeyboardMarkup:
    rows = []
    if request.status == STATUS_PENDING:
        rows.append([InlineKeyboardButton(
            BUTTON_APPROVE, callback_data=_callback(OP_APPROVE, request.request_id)
        )])
        rows.append([InlineKeyboardButton(
            BUTTON_EDIT, callback_data=_callback(OP_EDIT, request.request_id)
        )])
        rows.append([
            InlineKeyboardButton(
                BUTTON_RETURN,
                callback_data=_callback(OP_RETURN, request.request_id),
            ),
            InlineKeyboardButton(
                BUTTON_REJECT,
                callback_data=_callback(OP_REJECT, request.request_id),
            ),
        ])
    rows.append([InlineKeyboardButton(
        BUTTON_LIST, callback_data=_callback(OP_LIST)
    )])
    return InlineKeyboardMarkup(rows)


def build_field_keyboard(request: TaskRequest) -> InlineKeyboardMarkup:
    """Field chooser while the request is still pending."""
    rows = [
        [InlineKeyboardButton(
            f"✏️ {label}",
            callback_data=_callback(OP_FIELD, f"{field}:{request.request_id}"),
        )]
        for field, label in FIELD_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(
        BUTTON_LIST, callback_data=_callback(OP_LIST)
    )])
    return InlineKeyboardMarkup(rows)


def build_prompt_keyboard(request: TaskRequest) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "◀️ رجوع للطلب",
            callback_data=_callback(OP_VIEW, request.request_id),
        )
    ]])


def build_decided_text(request: TaskRequest, header: str) -> str:
    return f"{header}\n\n{build_detail_text(request)}"


# ── Queue pagination (read-only, deterministic) ───────────────────────


def _page_slice(page: int, total: int) -> tuple[int, int, int, int]:
    """Return (offset, limit, page, pages) with the page clamped."""
    pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
    page = max(0, min(page, pages - 1))
    return page * PAGE_SIZE, PAGE_SIZE, page, pages


# ── PTB adapters ──────────────────────────────────────────────────────


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


async def _safe_answer(query, text: str | None = None) -> None:
    try:
        if text is None:
            await query.answer()
        else:
            await query.answer(text)
    except Exception:  # pragma: no cover — transport fail-soft
        logger.debug("task request callback answer failed", exc_info=True)


async def _safe_edit(query, text: str, markup=None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:  # fail-soft: stale/identical message edits
        logger.debug("task request message edit failed", exc_info=True)


async def taskrequests_command(update, context) -> None:
    """/taskrequests — admin pending-queue snapshot (private only)."""
    if _non_private_chat(update):
        return
    actor = _actor_id(update)
    if actor is None or not is_admin(actor):
        message = getattr(update, "message", None)
        if message is not None:
            await message.reply_text(MSG_ADMIN_ONLY)
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    try:
        total = task_request_store.count_pending()
        offset, limit, page, pages = _page_slice(0, total)
        items = task_request_store.list_pending(limit=limit, offset=offset)
    except Exception:
        logger.exception("Task request queue failed: admin=%s", actor)
        await message.reply_text(MSG_ERROR)
        return
    if not items:
        await message.reply_text(MSG_NO_REQUESTS)
        return
    await message.reply_text(
        build_list_text(items, page, pages),
        reply_markup=build_list_keyboard(items, page, pages),
    )


async def _render_list(query, page: int) -> None:
    try:
        total = task_request_store.count_pending()
        offset, limit, page, pages = _page_slice(page, total)
        items = task_request_store.list_pending(limit=limit, offset=offset)
    except Exception:
        logger.exception("Task request list render failed")
        await _safe_answer(query, MSG_ERROR)
        return
    if not items:
        await _safe_edit(query, MSG_NO_REQUESTS)
        return
    await _safe_edit(
        query,
        build_list_text(items, page, pages),
        build_list_keyboard(items, page, pages),
    )


async def _render_detail(query, request_id: int) -> None:
    request = task_request_store.get_request(request_id)
    if request is None:
        await _safe_answer(query, MSG_STALE)
        return
    await _safe_edit(
        query,
        build_detail_text(request),
        build_detail_keyboard(request),
    )


async def _approve(query, actor: int, request_id: int) -> None:
    """Claim CAS → create the task → mark approved, ONE transaction.

    The spec comes from the STORED validated payload; the task's
    approver is the approving admin; funding charges the REQUESTING
    user's wallet through the existing creation transaction (the
    authenticated creator — the same semantics the wizard applies to
    its actor).  Any failure (invalid content surfaced late, funding)
    rolls the claim back: the request stays pending, zero tasks.

    Concurrency: NO Telegram round-trip may ever run while the
    ``BEGIN IMMEDIATE`` write transaction is open — the loser-path
    answers are recorded inside the transaction and sent only after
    it exits, so a slow Bot API call can never stall other writers
    (up to ``db.BUSY_TIMEOUT_MS`` → SQLITE_BUSY).
    """
    current = task_request_store.get_request(request_id)
    if current is None:
        await _safe_answer(query, MSG_STALE)
        return
    if current.status == STATUS_APPROVED:
        # Idempotent replay: never a second task, never a second charge.
        await _safe_answer(query, MSG_APPROVED_ALREADY)
        await _safe_edit(
            query,
            build_decided_text(current, MSG_APPROVED_ALREADY),
            build_detail_keyboard(current),
        )
        return
    if current.status != STATUS_PENDING:
        await _safe_answer(query, MSG_STALE)
        return

    # Chosen INSIDE the transaction, awaited only AFTER it exits.
    loser_answer: str | None = None

    try:
        with db.transaction() as conn:
            claimed = task_request_store.claim_for_approval(
                conn, request_id, actor
            )
            if claimed is None:
                # Lost the race (or state changed under us): the
                # winner's published task id is authoritative.  The
                # callback answer itself is deferred until the
                # transaction has exited.
                published = task_request_store.read_published_task_id(
                    conn, request_id
                )
                if published is not None:
                    logger.info(
                        "Task request approve replay: request_id=%s "
                        "task=%s",
                        request_id, published,
                    )
                    loser_answer = MSG_APPROVED_ALREADY
                else:
                    loser_answer = MSG_STALE
            else:
                spec = task_request_store.spec_from_request(
                    claimed, approver_id=actor
                )
                task_id = create_task_from_spec(
                    spec,
                    conn=conn,
                    funding_advertiser_id=claimed.user_id,
                )
                task_request_store.mark_approved(
                    conn, request_id, actor, task_id
                )
    except TaskCreationError as exc:
        # Arabic, admin-displayable (validation / funding refusal) —
        # the claim rolled back, the request stays pending.
        logger.info(
            "Task request approve refused: request_id=%s — %s",
            request_id, exc,
        )
        await _safe_answer(query, str(exc))
        return
    except Exception:
        logger.exception(
            "Task request approve failed: request_id=%s admin=%s",
            request_id, actor,
        )
        await _safe_answer(query, MSG_ERROR)
        return

    if loser_answer is not None:
        # Transaction already committed/rolled back — the write lock
        # is free before this (possibly slow) network await runs.
        await _safe_answer(query, loser_answer)
        return

    approved = task_request_store.get_request(request_id)
    await _safe_answer(query, MSG_APPROVED)
    if approved is not None:
        await _safe_edit(
            query,
            build_decided_text(approved, MSG_APPROVED),
            build_detail_keyboard(approved),
        )
    logger.info(
        "Task request approved: request_id=%s admin=%s task=%s",
        request_id, actor, task_id,
    )


async def _start_input(
    query, context, request: TaskRequest, op: str, field: str | None,
    prompt: str,
) -> None:
    """Prompt the admin for free text and remember the one-shot state."""
    context.user_data[INPUT_STATE_KEY] = {
        "op": op,
        "request_id": request.request_id,
        "field": field,
    }
    await _safe_answer(query, "أرسل الرسالة الآن.")
    await _safe_edit(
        query, prompt, build_prompt_keyboard(request)
    )


async def task_request_callback(update, context) -> None:
    """PTB handler for ``treq:`` callback queries (admin-only)."""
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    if _non_private_chat(update):
        await _safe_answer(query)
        return
    actor = _actor_id(update)
    if actor is None or not is_admin(actor):
        await _safe_answer(query, MSG_ADMIN_ONLY)
        return

    parsed = parse_callback(query.data)
    if parsed is None:
        await _safe_answer(query, MSG_INVALID)
        return
    op, arg, field = parsed

    # Any button press cancels a pending text-input prompt.
    context.user_data.pop(INPUT_STATE_KEY, None)

    try:
        if op == OP_LIST:
            await _render_list(query, 0)
            return
        if op == OP_PAGE:
            await _render_list(query, int(arg) - 1)
            return
        if op == OP_VIEW:
            await _render_detail(query, int(arg))
            return
        if op == OP_APPROVE:
            await _approve(query, actor, int(arg))
            return

        request = task_request_store.get_request(int(arg))
        if request is None:
            await _safe_answer(query, MSG_STALE)
            return
        if request.status != STATUS_PENDING:
            await _safe_answer(query, MSG_STALE)
            return

        if op == OP_EDIT:
            # Field chooser for this request.
            await _safe_edit(
                query,
                build_detail_text(request),
                build_field_keyboard(request),
            )
            return
        if op == OP_FIELD:
            # Direct field edit → ask for the new value.
            await _start_input(
                query, context, request, OP_FIELD, field,
                f"أرسل القيمة الجديدة لحقل «{FIELD_LABELS[field]}»:",
            )
            return
        if op == OP_RETURN:
            await _start_input(
                query, context, request, OP_RETURN, None, PROMPT_RETURN
            )
            return
        if op == OP_REJECT:
            await _start_input(
                query, context, request, OP_REJECT, None, PROMPT_REJECT
            )
            return
    except Exception:
        logger.exception(
            "Task request callback failed: admin=%s data=%r",
            actor, getattr(query, "data", None),
        )
        await _safe_answer(query, MSG_ERROR)
        return

    await _safe_answer(query, MSG_INVALID)


async def task_request_text_input(update, context) -> None:
    """Admin free text for reject reason / return note / field edit.

    Registered STATICALLY in bot.py (own handler group) — self-gated:
    silent unless the sender is an admin in a private chat holding a
    ``treq:`` input state, so ordinary chat, the wizard, support and
    the other admin text catch-alls are unaffected.
    """
    if _non_private_chat(update):
        return
    actor = _actor_id(update)
    if actor is None or not is_admin(actor):
        return
    state = context.user_data.get(INPUT_STATE_KEY)
    if not isinstance(state, dict):
        return
    message = getattr(update, "message", None)
    text = getattr(message, "text", None)
    if not isinstance(text, str) or not text.strip():
        return

    op = state.get("op")
    request_id = state.get("request_id")
    if op not in (OP_FIELD, OP_RETURN, OP_REJECT):
        context.user_data.pop(INPUT_STATE_KEY, None)
        return

    try:
        if op == OP_REJECT:
            request = task_request_store.admin_reject_request(
                request_id, actor, text
            )
        elif op == OP_RETURN:
            request = task_request_store.admin_return_request(
                request_id, actor, text
            )
        else:
            request = task_request_store.admin_edit_field(
                request_id, actor, state.get("field"), text
            )
    except TaskRequestError as exc:
        reason = str(exc)
        if reason in ("الطلب غير موجود.",
                      "تم اتخاذ قرار على هذا الطلب مسبقاً."):
            context.user_data.pop(INPUT_STATE_KEY, None)
            await message.reply_text(MSG_STALE)
            return
        # Retryable (e.g. bad reward): keep the input state.
        await message.reply_text(reason)
        return
    except Exception:
        context.user_data.pop(INPUT_STATE_KEY, None)
        logger.exception(
            "Task request text input failed: admin=%s op=%s",
            actor, op,
        )
        await message.reply_text(MSG_ERROR)
        return

    context.user_data.pop(INPUT_STATE_KEY, None)
    toast = {
        OP_REJECT: MSG_REJECTED_TOAST,
        OP_RETURN: MSG_RETURNED_TOAST,
        OP_FIELD: MSG_EDITED_TOAST,
    }[op]
    await message.reply_text(
        f"{toast}\n\n{build_detail_text(request)}",
        reply_markup=build_detail_keyboard(request),
    )
    logger.info(
        "Task request input applied: request_id=%s admin=%s op=%s",
        request.request_id, actor, op,
    )
