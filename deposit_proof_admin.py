"""
Deposit Proof Admin Review (MT-ADMIN-31)
========================================

Admin-side review of the MANUAL deposit-proof screenshots users
upload through the Mini App — **discovery, inspection and decision
only**.  The screenshot is EVIDENCE for the admin's own manual
verification of the payment; it never proves anything by itself.

Architecture::

    Admin Telegram private chat (config.is_admin)
      ↓
    /deposits                        ← bounded pending queue (read-only)
      ↓
    deposit_proof_store.list_pending / get_proof
      🔍 مراجعة #<short id>          (dp:view:<proof id> — lookup only)
      ↓
    photo message — the stored screenshot + detail card built from
    trusted persisted facts (pm_destination NEVER shown)
      ↓
    ✅ اعتماد الدفع / ❌ رفض الإثبات   (dp:aska: / dp:askr: — confirm)
      ↓ confirmation press
    dp:doa:<proof id> / dp:dor:<proof id>
      ↓
    deposit_manual_review.approve    ← SOLE credit path: delegates to
      the EXISTING atomic credit boundary with the PERSISTED amount
    deposit_manual_review.reject     ← evidence decision only: no
      wallet, no ledger, no deposit status change

Boundaries (this module must NOT):
- credit a wallet or write the ledger itself (the callback only
  authenticates, loads/displays, confirms and delegates);
- take an amount from anywhere — the credit amount comes from the
  persisted deposit row inside the trusted service;
- infer payment receipt from the screenshot (the admin is the
  verifying actor; confirmation copy says so explicitly);
- expose ``pm_destination`` (the platform's own destination), the
  storage key or any credential — and never log image contents;
- add a second authorization mechanism (``config.is_admin`` only)
  or a user-side decision path.

Callback safety: ``dp:<op>:<ref>`` payloads carry ONLY an operation
word, a positive page number, or the opaque proof id — everything
else (actor authorization, proof, deposit, amounts, states) is
re-read server-side from SQLite.  MT-ADMIN-02 isolation: private
chats only — groups/channels get ZERO financial detail.

Determinism: the queue has a single explicit ORDER BY
(``created_at DESC, proof_id``) and a fixed page size; untrusted
page ids are clamped.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile

import db
import deposit_manual_review
import deposit_proof_storage
import deposit_proof_store
import deposit_store
import deposit_verification
import wallet
from config import is_admin
from deposit_manual_review import ManualApprovalOutcome
from deposit_proof_store import DepositProof
from deposit_proof_store import (
    STATUS_APPROVED,
    STATUS_PENDING_REVIEW,
    STATUS_REJECTED,
)

logger = logging.getLogger(__name__)

# ── Callback payloads (untrusted lookup pointers ONLY) ────────────────

CB_PREFIX = "dp"
OP_PAGE = "page"
OP_VIEW = "view"
OP_ASK_APPROVE = "aska"
OP_ASK_REJECT = "askr"
OP_DO_APPROVE = "doa"
OP_DO_REJECT = "dor"
_OPS_WITH_PAGE = frozenset({OP_PAGE})
_OPS_WITH_ID = frozenset(
    {OP_VIEW, OP_ASK_APPROVE, OP_ASK_REJECT, OP_DO_APPROVE, OP_DO_REJECT}
)
_ALL_OPS = _OPS_WITH_PAGE | _OPS_WITH_ID

# ── Bounds (bounded, deterministic) ───────────────────────────────────

# Small fixed page size — same convention as the other admin queues.
PAGE_SIZE = 5

# Opaque proof id bound (uuid4().hex = 32; generous head-room, still
# strictly bounded so forged payloads cannot smuggle arbitrary text).
_MAX_PROOF_ID_LEN = 64

# ── Arabic UI strings ─────────────────────────────────────────────────

MSG_ADMIN_ONLY = "⛔ هذا الأمر للمشرفين فقط."
MSG_INVALID = "⛔ طلب غير صالح."
MSG_NOT_FOUND = "⛔ إثبات الدفع غير موجود."
MSG_STATE_CHANGED = "⛔ تغيرت حالة الإثبات — لم يتم تنفيذ أي عملية."
MSG_ERROR = "⛔ حدث خطأ، حاول مرة أخرى."

LIST_HEADER = "🧾 إثباتات الإيداع المعلقة"
MSG_NO_PENDING = "📭 لا توجد إثباتات إيداع بانتظار المراجعة."

DETAIL_HEADER = "🔎 مراجعة إثبات دفع"
CONFIRM_APPROVE_HEADER = "⚠️ تأكيد اعتماد الدفع"
CONFIRM_REJECT_HEADER = "⚠️ تأكيد رفض الإثبات"
MSG_IMAGE_UNAVAILABLE = "⚠️ تعذر تحميل صورة الإثبات."

BTN_VIEW = "🔍 مراجعة #{short_id}"
BTN_APPROVE = "✅ اعتماد الدفع"
BTN_REJECT = "❌ رفض الإثبات"
BTN_CONFIRM_APPROVE = "✅ تأكيد الاعتماد"
BTN_CONFIRM_REJECT = "✅ تأكيد الرفض"
BTN_BACK = "⬅️ رجوع"
BTN_LIST = "⬅️ القائمة"
PAGE_NEXT = "التالي ▶️"
PAGE_PREV = "◀️ السابق"

_PROOF_STATUS_LABEL = {
    STATUS_PENDING_REVIEW: "⏳ بانتظار المراجعة",
    STATUS_APPROVED: "✅ معتمد",
    STATUS_REJECTED: "❌ مرفوض",
}

_DEPOSIT_STATUS_LABEL = {
    "pending": "⏳ قيد الانتظار",
    "credited": "✅ تم الترحيل إلى الرصيد",
    "rejected": "❌ مرفوض",
}


def proof_status_label(status: object) -> str:
    key = getattr(status, "value", status)
    return _PROOF_STATUS_LABEL.get(str(key), str(key))


def deposit_status_label(status: object) -> str:
    key = getattr(status, "value", status)
    return _DEPOSIT_STATUS_LABEL.get(str(key), str(key))


# ── Callback payload helpers ──────────────────────────────────────────


def page_callback_data(page: int) -> str:
    return f"{CB_PREFIX}:{OP_PAGE}:{page}"


def view_callback_data(proof_id: str) -> str:
    return f"{CB_PREFIX}:{OP_VIEW}:{proof_id}"


def ask_approve_callback_data(proof_id: str) -> str:
    return f"{CB_PREFIX}:{OP_ASK_APPROVE}:{proof_id}"


def ask_reject_callback_data(proof_id: str) -> str:
    return f"{CB_PREFIX}:{OP_ASK_REJECT}:{proof_id}"


def do_approve_callback_data(proof_id: str) -> str:
    return f"{CB_PREFIX}:{OP_DO_APPROVE}:{proof_id}"


def do_reject_callback_data(proof_id: str) -> str:
    return f"{CB_PREFIX}:{OP_DO_REJECT}:{proof_id}"


def parse_callback(data: object) -> tuple[str, object] | None:
    """``dp:<op>:<ref>`` → ``(op, ref)``.  None for anything else.

    ``ref`` is a positive int for ``page`` and a bounded opaque proof
    id (ASCII alnum / ``-`` / ``_``) for every other op — a lookup
    pointer ONLY; no amount, destination or other fact.
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
    if not 1 <= len(ref) <= _MAX_PROOF_ID_LEN:
        return None
    if not all(
        char.isascii() and (char.isalnum() or char in "-_")
        for char in ref
    ):
        return None
    return op, ref


def _short_id(value: str) -> str:
    return value[:8]


def _fmt_when(value: object) -> str:
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return str(value)


def _amount_text(deposit) -> str:
    """Exact persisted amount — read-only display, never computed."""
    if deposit is None:
        return "—"
    amount = wallet.units_to_decimal(deposit.amount_units)
    asset = deposit.pm_asset or "USDT"
    return f"{amount} {asset}"


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


def _deposit_of(proof: DepositProof):
    """The persisted financial source of truth for one proof."""
    return deposit_store.get_deposit_request(proof.request_id)


# ── Rendering (Arabic, bounded) ───────────────────────────────────────


def build_list_page(
    proofs, page: int = 1
) -> tuple[str, InlineKeyboardMarkup | None]:
    """One bounded page of the pending queue (deterministic).

    Empty queue → the concise Arabic empty state with no keyboard.
    ``page`` is clamped into the valid range.
    """
    proofs = list(proofs)
    total = len(proofs)
    if total == 0:
        return MSG_NO_PENDING, None

    max_page = max(1, -(-total // PAGE_SIZE))  # ceil division
    page = min(max(1, int(page)), max_page)
    start = (page - 1) * PAGE_SIZE
    chunk = proofs[start : start + PAGE_SIZE]

    header = LIST_HEADER
    if total > PAGE_SIZE:
        header += f" ({start + 1}–{start + len(chunk)} من {total})"

    lines = [header]
    rows: list[list[InlineKeyboardButton]] = []
    for index, proof in enumerate(chunk, start=1):
        deposit = _deposit_of(proof)
        short_id = _short_id(proof.proof_id)
        lines.append("")
        lines.append(f"{index}. #{short_id} — المستخدم {proof.user_id}")
        if deposit is not None:
            method = deposit.pm_display_name or "—"
            lines.append(f"   {method} — {_amount_text(deposit)}")
            lines.append(f"   تاريخ الطلب: {_fmt_when(deposit.created_at)}")
        lines.append(f"   رُفع الإثبات: {_fmt_when(proof.created_at)}")
        rows.append(
            [
                InlineKeyboardButton(
                    BTN_VIEW.format(short_id=short_id),
                    callback_data=view_callback_data(proof.proof_id),
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


def build_detail_text(proof: DepositProof, deposit) -> str:
    """The review card — ONLY trusted persisted facts.

    ``pm_destination`` (the platform's own destination) and the
    evidence ``storage_key`` are never rendered.
    """
    if deposit is None:
        return (
            f"{DETAIL_HEADER}\n"
            "━━━━━━━━━━━━━━━━\n"
            f"إثبات: #{_short_id(proof.proof_id)}\n"
            "⛔ بيانات الطلب غير متاحة."
        )
    lines = [
        DETAIL_HEADER,
        "━━━━━━━━━━━━━━━━",
        f"إثبات: #{_short_id(proof.proof_id)}",
        f"رقم الطلب: {deposit.request_id}",
        f"المستخدم: {proof.user_id} — {_user_who(proof.user_id)}",
        f"المبلغ المطلوب: {_amount_text(deposit)}",
        f"الأصل: {deposit.pm_asset or '—'}",
        f"الشبكة: {deposit.pm_network or '—'}",
        f"وسيلة الدفع: {deposit.pm_display_name or '—'}",
        f"تاريخ الطلب: {_fmt_when(deposit.created_at)}",
        f"رفع الإثبات: {_fmt_when(proof.created_at)}",
        f"حالة الإثبات: {proof_status_label(proof.status)}",
        f"الحالة المالية للطلب: "
        f"{deposit_status_label(deposit.status)}",
    ]
    if proof.reviewed_by is not None:
        lines.append(
            f"رُوجِّع بواسطة: {proof.reviewed_by} — "
            f"{_fmt_when(proof.reviewed_at)}"
        )
    return "\n".join(lines)


def build_detail_keyboard(proof: DepositProof) -> InlineKeyboardMarkup:
    """✅/❌ decision buttons only while the proof is pending review;
    the ⬅️ رجوع button always returns to the bounded list."""
    rows: list[list[InlineKeyboardButton]] = []
    if proof.status == STATUS_PENDING_REVIEW:
        rows.append(
            [
                InlineKeyboardButton(
                    BTN_APPROVE,
                    callback_data=ask_approve_callback_data(
                        proof.proof_id
                    ),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    BTN_REJECT,
                    callback_data=ask_reject_callback_data(
                        proof.proof_id
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


def build_confirm_text(
    proof: DepositProof, deposit, *, approve: bool
) -> str:
    header = (
        CONFIRM_APPROVE_HEADER if approve else CONFIRM_REJECT_HEADER
    )
    action = (
        "اعتماد الدفع يعني تأكيدك — بعد مراجعة الصورة يدوياً — أن "
        "الدفعة وصلت فعلياً؛ سيُرحَّل المبلغ المحدد في الطلب نفسه "
        "(من السجل المحفوظ فقط) إلى رصيد المستخدم عبر العملية "
        "الذرية الوحيدة. الصورة وحدها لا تُثبت استلام أي مبلغ."
        if approve
        else "رفض الإثبات يُثبت أن الصورة مرفوضة فقط: لا يُضاف أي "
        "رصيد، ويبقى طلب الإيداع قيد الانتظار ويمكن للمستخدم رفع "
        "إثبات جديد. لا تُحذف الصورة المرفوضة."
    )
    return (
        f"{header}\n"
        "━━━━━━━━━━━━━━━━\n"
        f"رقم الطلب: {proof.request_id}\n"
        f"المبلغ: {_amount_text(deposit)}\n\n"
        f"{action}"
    )


def build_confirm_keyboard(
    proof: DepositProof, *, approve: bool
) -> InlineKeyboardMarkup:
    if approve:
        confirm = InlineKeyboardButton(
            BTN_CONFIRM_APPROVE,
            callback_data=do_approve_callback_data(proof.proof_id),
        )
    else:
        confirm = InlineKeyboardButton(
            BTN_CONFIRM_REJECT,
            callback_data=do_reject_callback_data(proof.proof_id),
        )
    back = InlineKeyboardButton(
        BTN_BACK, callback_data=view_callback_data(proof.proof_id)
    )
    return InlineKeyboardMarkup([[confirm], [back]])


def build_outcome_text(proof: DepositProof, outcome) -> str:
    """Post-decision caption — states exactly what happened."""
    if isinstance(outcome, ManualApprovalOutcome):
        if outcome.credited:
            return (
                "✅ تم اعتماد الدفع — أُضيف مبلغ الطلب كاملاً إلى "
                "رصيد المستخدم.\n"
                f"رقم الطلب: {outcome.request_id}\n"
                f"الحالة المالية: "
                f"{deposit_status_label(outcome.deposit_status)}"
            )
        return (
            "ℹ️ تمت معالجة هذا الطلب مسبقاً — لم تُجرَ أي عملية "
            "مالية جديدة.\n"
            f"رقم الطلب: {outcome.request_id}\n"
            f"الحالة المالية: "
            f"{deposit_status_label(outcome.deposit_status)}"
        )
    # reject outcome
    return (
        "❌ تم رفض إثبات الدفع — لم يُضف أي رصيد.\n"
        f"رقم الطلب: {proof.request_id}\n"
        "الطلب ما زال قيد الانتظار ويمكن للمستخدم إرسال إثبات جديد."
    )


def build_settled_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_LIST, callback_data=page_callback_data(1)
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
        logger.debug("Could not answer deposit proof callback",
                     exc_info=True)


async def _safe_edit(query, text: str, reply_markup=None) -> None:
    """Edit the card in place — caption for photo messages, text
    otherwise (the detail card is a screenshot message)."""
    try:
        photo = getattr(getattr(query, "message", None), "photo", None)
        if isinstance(photo, list) and photo:
            await query.edit_message_caption(
                text, reply_markup=reply_markup
            )
        else:
            await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        logger.debug("Could not edit deposit proof message",
                     exc_info=True)


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


async def _send_detail(update, context, proof: DepositProof, deposit) -> None:
    """Send the screenshot + detail card (a photo message can never
    be an edit of the text queue)."""
    text = build_detail_text(proof, deposit)
    markup = build_detail_keyboard(proof)
    image = None
    try:
        image = deposit_proof_storage.read_image(proof.storage_key)
    except Exception:
        logger.warning(
            "Deposit proof image unavailable: proof=%s request=%s",
            proof.proof_id, proof.request_id,
        )
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    try:
        if image is not None:
            ext = proof.storage_key.rsplit(".", 1)[-1]
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(image, filename=f"proof.{ext}"),
                caption=text,
                reply_markup=markup,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{text}\n\n{MSG_IMAGE_UNAVAILABLE}",
                reply_markup=markup,
            )
    except Exception:
        logger.exception(
            "Deposit proof detail send failed: proof=%s admin=%s",
            proof.proof_id, _actor_id(
                getattr(getattr(update, "effective_user", None), "id", None)
            ),
        )


# ── PTB handlers ──────────────────────────────────────────────────────


async def deposits_command(update, context) -> None:
    """``/deposits`` — admin-only pending deposit-proof queue.

    Registered by bot.py as ``CommandHandler("deposits", ...)``.
    Private chats only: a group/channel invocation produces ZERO
    replies.  Non-admins get the standard admin-only refusal and
    never see proof data.
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
        proofs = deposit_proof_store.list_pending_proofs()
    except Exception:
        logger.exception(
            "Deposit proof queue load failed: admin=%d", actor
        )
        await message.reply_text(MSG_ERROR)
        return

    text, markup = build_list_page(proofs, 1)
    await message.reply_text(text, reply_markup=markup)
    logger.info(
        "Deposit proof queue shown: admin=%d pending=%d",
        actor, len(proofs),
    )


async def proof_callback(update, context) -> None:
    """``dp:`` callbacks — private admin chat ONLY, server re-reads all.

    The payload is an opaque lookup pointer (op + proof id / page);
    actor authorization, the proof, the deposit and every displayed
    fact come from SQLite.  ``doa``/``dor`` are the ONLY decision
    presses: approval delegates to the trusted manual-review service
    (the EXISTING atomic credit boundary is the sole financial path)
    and rejection delegates to the evidence CAS — this handler never
    opens a financial transaction and never credits anything itself.
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

    # ── UI navigation: fresh read-only snapshot, clamped page ────
    if op == OP_PAGE:
        proofs = deposit_proof_store.list_pending_proofs()
        text, markup = build_list_page(proofs, ref)
        await _safe_answer(query, None)
        await _safe_edit(query, text, markup)
        return

    proof_id = ref
    proof = deposit_proof_store.get_proof(proof_id)
    if proof is None:
        await _safe_answer(query, MSG_NOT_FOUND)
        return

    # ── Detail: screenshot message (read-only) ──────────────────
    if op == OP_VIEW:
        deposit = _deposit_of(proof)
        if deposit is None:
            await _safe_answer(query, MSG_NOT_FOUND)
            return
        await _safe_answer(query, None)
        await _send_detail(update, context, proof, deposit)
        logger.info(
            "Deposit proof review opened: proof=%s request=%s "
            "admin=%d status=%s",
            proof_id, proof.request_id, actor, proof.status,
        )
        return

    deposit = _deposit_of(proof)
    if deposit is None:
        await _safe_answer(query, MSG_NOT_FOUND)
        return

    # ── Confirmation step (NO mutation happens here) ────────────
    if op in (OP_ASK_APPROVE, OP_ASK_REJECT):
        if proof.status != STATUS_PENDING_REVIEW:
            await _safe_answer(query, MSG_STATE_CHANGED)
            return
        approve = op == OP_ASK_APPROVE
        await _safe_answer(query, None)
        await _safe_edit(
            query,
            build_confirm_text(proof, deposit, approve=approve),
            build_confirm_keyboard(proof, approve=approve),
        )
        return

    # ── Confirmed decisions: the ONE delegation each ────────────
    if op == OP_DO_APPROVE:
        try:
            outcome = deposit_manual_review.approve(
                proof_id, admin_id=actor
            )
        except deposit_manual_review.ProofReviewNotFoundError:
            logger.info(
                "Deposit proof approval: proof=%s admin=%d "
                "result=not_found",
                proof_id, actor,
            )
            await _safe_answer(query, MSG_NOT_FOUND)
            return
        except deposit_manual_review.ProofReviewConflictError:
            logger.info(
                "Deposit proof approval: proof=%s admin=%d "
                "result=conflict",
                proof_id, actor,
            )
            await _safe_answer(query, MSG_STATE_CHANGED)
            return
        except deposit_verification.DepositConflictError:
            logger.info(
                "Deposit proof approval: proof=%s admin=%d "
                "result=deposit_conflict",
                proof_id, actor,
            )
            await _safe_answer(query, MSG_STATE_CHANGED)
            return
        except deposit_verification.DepositVerificationError:
            logger.exception(
                "Deposit proof approval rejected by the credit "
                "boundary: proof=%s admin=%d",
                proof_id, actor,
            )
            await _safe_answer(query, MSG_ERROR)
            return
        except Exception:
            logger.exception(
                "Deposit proof approval failed: proof=%s admin=%d",
                proof_id, actor,
            )
            await _safe_answer(query, MSG_ERROR)
            return

        await _safe_answer(query, None)
        await _safe_edit(
            query,
            build_outcome_text(proof, outcome),
            build_settled_keyboard(),
        )
        return

    # op is dor — parse_callback guarantees this.
    try:
        deposit_manual_review.reject(proof_id, admin_id=actor)
    except deposit_manual_review.ProofReviewNotFoundError:
        await _safe_answer(query, MSG_NOT_FOUND)
        return
    except deposit_manual_review.ProofReviewConflictError:
        await _safe_answer(query, MSG_STATE_CHANGED)
        return
    except Exception:
        logger.exception(
            "Deposit proof rejection failed: proof=%s admin=%d",
            proof_id, actor,
        )
        await _safe_answer(query, MSG_ERROR)
        return

    await _safe_answer(query, None)
    await _safe_edit(
        query,
        build_outcome_text(proof, None),
        build_settled_keyboard(),
    )
