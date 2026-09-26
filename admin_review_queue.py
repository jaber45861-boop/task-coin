"""
Admin Manual Review Queue (MT-ADMIN-04)
=======================================

Cross-task discovery/navigation for pending manual proof claims in the
Telegram Admin Control Plane — **discovery only**.  The queue never
decides anything: pressing Review re-opens the EXISTING MT-ADMIN-03
manual-proof inbox representation (same linkage, same Approve/Reject
buttons, same ManualReviewService.decide authorization).

Architecture::

    Admin Telegram private chat
      ↓
    /reviews                          ← read-only queue snapshot
      ↓
    persistent pending manual claims  ← task_submissions JOIN tasks
      ↓
    🔍 مراجعة #<claim_id>  (mrview:<claim_id> — opaque lookup id only)
      ↓
    server-side re-read + pending-manual verification
      + admin gate + task_data.approver gate (NO new authority)
      ↓
    existing MT-ADMIN-03 inbox card   ← linkage + Approve/Reject
      ↓
    ManualReviewService.decide()      ← SOLE decision path (unchanged)

Boundaries (this module must NOT):
- approve/reject anything — decisions live ONLY in
  ManualReviewService.decide (called by manual_proof_inbox)
- grant decision authority (is_admin only gates QUEUE ACCESS; the
  task-specific approver remains the only decider — MT-TASK-15)
- mutate claims by listing/paging/opening them (no locks, no
  reservations, no "claimed" markers)
- expose task_data, reward-as-decision-value, approver credentials,
  worker secrets, tokens or connection data
- add schema/tables (existing task_submissions + tasks +
  admin_notifications are sufficient) or hold queue state in memory

Determinism: the queue query has a single explicit ORDER BY
(``submission_id ASC`` — oldest pending submission first) and a fixed
page size, so identical data always renders identically.

Callback safety: ``mrview:``/``mrvp:`` payloads carry ONLY a positive
integer id (claim id / page number) and are treated as untrusted
lookup pointers — every claim, task and authorization fact is re-read
server-side from SQLite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import db
from admin_notification_store import AdminNotificationStore
from config import is_admin
from manual_proof_inbox import (
    MSG_ALREADY_DECIDED,
    MSG_EDIT_CLOSED,
    MSG_ERROR,
    MSG_INVALID,
    MSG_STALE,
    OPERATION_MANUAL_PROOF,
    build_decided_text,
    build_decision_keyboard,
    build_pending_text,
)
from manual_task import (
    MANUAL_TASK_TYPE,
    manual_task_approver_user_id,
)
from task_submission_store import TaskSubmissionStore

logger = logging.getLogger(__name__)

# ── Queue shape (bounded, deterministic) ─────────────────────────────

# Small fixed page size: /reviews never produces a giant message.
PAGE_SIZE = 5

# Callback prefixes — payload is exactly one positive integer.
CALLBACK_REVIEW_PREFIX = "mrview"
CALLBACK_PAGE_PREFIX = "mrvp"

# Queue-level Arabic messages (decision messages stay in manual_proof_inbox).
MSG_QUEUE_HEADER = "📋 المراجعات المعلقة"
MSG_NO_REVIEWS = "📭 لا توجد مراجعات معلقة حالياً."
MSG_ADMIN_ONLY = "⛔ هذا الأمر للمشرفين فقط."
MSG_REVIEW_UNAUTHORIZED = "⛔ لا تملك صلاحية مراجعة هذه المهمة"

REVIEW_BUTTON = "🔍 مراجعة #{claim_id}"
PAGE_NEXT = "التالي ▶️"
PAGE_PREV = "◀️ السابق"

# Transport shapes injected by the PTB adapters (or tests).
AnswerFunc = Callable[[str], Awaitable[None]]
# Queue edits may carry a keyboard (the review card) or None (inert).
EditFunc = Callable[[int, int, str, Optional[object]], Awaitable[None]]


# ── 1. Queue source (read-only repository operation) ─────────────────


@dataclass(frozen=True)
class PendingManualClaim:
    """One genuinely pending manual approval claim — SAFE fields only.

    Exactly the data an admin needs to identify/review a claim:
    claim id, task id, task title, submission time and the bounded
    proof reference.  No task_data, no reward, no approver identity,
    no worker identity, no transport/secret material.
    """

    claim_id: int
    task_id: int
    task_title: str
    submitted_at: str
    proof_ref: str | None


def list_pending_manual_claims(
    db_path: str | None = None,
) -> list[PendingManualClaim]:
    """Read-only, cross-task snapshot of pending manual claims.

    Excludes, by construction:
    - decided claims (approved/rejected) and terminal submissions
    - referral approval claims and every non-manual submission
      (``tasks.type = 'manual'`` join + approval-gated ``pending``)
    - inactive/dead tasks and malformed task_data definitions
      (unresolvable approver → filtered out)

    Ordering: ``submission_id ASC`` — oldest pending submission first,
    a single deterministic key.  Listing never mutates any row.
    """
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT s.submission_id, s.task_id, s.submitted_at, "
            "       s.proof_ref, t.title, t.task_data "
            "FROM task_submissions AS s "
            "INNER JOIN tasks AS t ON t.id = s.task_id "
            "WHERE s.approval_status = ? "
            "  AND s.status = ? "
            "  AND t.type = ? "
            "  AND t.active = 1 "
            "ORDER BY s.submission_id ASC",
            (
                db.SUBMISSION_APPROVAL_PENDING,
                db.SUBMISSION_STATUS_SUBMITTED,
                MANUAL_TASK_TYPE,
            ),
        ).fetchall()

    claims: list[PendingManualClaim] = []
    for row in rows:
        # Definition validity (MT-TASK-15 contract): a claim whose task
        # no longer resolves a trusted approver is a dead record and
        # must not enter the queue.
        if manual_task_approver_user_id({"task_data": row["task_data"]}) is None:
            continue
        claims.append(
            PendingManualClaim(
                claim_id=row["submission_id"],
                task_id=row["task_id"],
                task_title=row["title"],
                submitted_at=row["submitted_at"],
                proof_ref=row["proof_ref"],
            )
        )
    return claims


# ── Callback payload helpers (untrusted lookup ids only) ─────────────


def review_callback_data(claim_id: int) -> str:
    return f"{CALLBACK_REVIEW_PREFIX}:{claim_id}"


def page_callback_data(page: int) -> str:
    return f"{CALLBACK_PAGE_PREFIX}:{page}"


def _parse_positive_id(prefix: str, data: object) -> int | None:
    """Parse ``<prefix>:<positive int>``; None for anything else."""
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != prefix:
        return None
    raw = parts[1]
    if not (raw.isascii() and raw.isdigit()):
        return None
    value = int(raw)
    return value if value > 0 else None


def parse_review_callback(data: object) -> int | None:
    """``mrview:<claim id>`` → claim id.  Lookup pointer ONLY."""
    return _parse_positive_id(CALLBACK_REVIEW_PREFIX, data)


def parse_page_callback(data: object) -> int | None:
    """``mrvp:<page>`` → page number.  UI navigation ONLY."""
    return _parse_positive_id(CALLBACK_PAGE_PREFIX, data)


# ── 3/4. Queue rendering (Arabic, bounded) ───────────────────────────


def build_queue_page(
    claims: list[PendingManualClaim], page: int = 1
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Render one bounded page of the queue (deterministic).

    Empty queue → the concise Arabic empty state with no keyboard.
    ``page`` is clamped into the valid range, so untrusted page ids
    can never address out-of-range data.
    """
    total = len(claims)
    if total == 0:
        return MSG_NO_REVIEWS, None

    max_page = max(1, -(-total // PAGE_SIZE))  # ceil division
    page = min(max(1, int(page)), max_page)
    start = (page - 1) * PAGE_SIZE
    chunk = claims[start : start + PAGE_SIZE]

    header = MSG_QUEUE_HEADER
    if total > PAGE_SIZE:
        header += f" ({start + 1}–{start + len(chunk)} من {total})"

    lines = [header]
    for index, claim in enumerate(chunk, start=1):
        lines.append("")
        lines.append(f"{index}. مهمة: {claim.task_title}")
        lines.append(f"   الطلب: #{claim.claim_id}")
        lines.append(f"   المهمة: #{claim.task_id}")
        lines.append(f"   تاريخ الإرسال: {claim.submitted_at}")

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                REVIEW_BUTTON.format(claim_id=c.claim_id),
                callback_data=review_callback_data(c.claim_id),
            )
        ]
        for c in chunk
    ]
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


# ── Local async safety wrappers ──────────────────────────────────────


async def _safe_answer(answer: AnswerFunc, text: str) -> None:
    try:
        await answer(text)
    except Exception:
        logger.debug("Could not answer review queue callback", exc_info=True)


async def _safe_edit(
    edit: EditFunc,
    chat_id: object,
    message_id: object,
    text: str,
    reply_markup: Optional[object],
) -> None:
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return  # no target — nothing to render onto
    try:
        await edit(chat_id, message_id, text, reply_markup)
    except Exception:
        logger.debug(
            "Could not edit queue message %s/%s",
            chat_id, message_id,
            exc_info=True,
        )


def _live_manual_task(task: object) -> bool:
    """True only for an active, type-manual task row."""
    return (
        isinstance(task, dict)
        and task.get("type") == MANUAL_TASK_TYPE
        and bool(task.get("active"))
    )


# ── 5. Review button (opens the EXISTING review representation) ──────


async def handle_review_callback(
    data: object,
    actor_user_id: object,
    *,
    chat_id: object,
    message_id: object,
    answer: AnswerFunc,
    edit: EditFunc,
) -> str:
    """Open the existing MT-ADMIN-03 review card for one claim.

    The payload is an opaque lookup id: every claim/task/authorization
    fact is re-read from SQLite.  This action NEVER approves or
    rejects — it only renders the existing Approve/Reject controls,
    which later flow through manual_proof_inbox.handle_callback →
    ManualReviewService.decide (unchanged).

    Returns one of ``invalid``, ``forbidden``, ``stale``,
    ``already_decided``, ``unauthorized``, ``error``, ``opened``.
    """
    claim_id = parse_review_callback(data)
    if claim_id is None:
        await _safe_answer(answer, MSG_INVALID)
        return "invalid"

    # Queue ACCESS = existing admin policy (this is not decision
    # authority — the approver gate below still governs the card).
    if not isinstance(actor_user_id, int) or not is_admin(actor_user_id):
        await _safe_answer(answer, MSG_ADMIN_ONLY)
        return "forbidden"

    # ── Server-side resolution (payload supplies ONLY the claim id) ─
    try:
        record = TaskSubmissionStore.get_submission(claim_id)
        task = db.get_task(record.task_id) if record is not None else None
    except Exception:
        logger.exception(
            "Review queue resolution failed: claim=%s actor=%r",
            claim_id, actor_user_id,
        )
        await _safe_answer(answer, MSG_ERROR)
        return "error"

    if record is None:
        await _safe_answer(answer, MSG_STALE)
        await _safe_edit(edit, chat_id, message_id, MSG_EDIT_CLOSED, None)
        return "stale"

    # ── Already decided → show already decided, no mutation ────────
    if record.approval_status in (
        db.SUBMISSION_APPROVAL_APPROVED,
        db.SUBMISSION_APPROVAL_REJECTED,
    ):
        approved = record.approval_status == db.SUBMISSION_APPROVAL_APPROVED
        await _safe_answer(answer, MSG_ALREADY_DECIDED)
        text = (
            build_decided_text(task, record, approved)
            if _live_manual_task(task)
            else MSG_EDIT_CLOSED
        )
        await _safe_edit(edit, chat_id, message_id, text, None)
        return "already_decided"

    # ── Must STILL be a genuinely pending manual claim ──────────────
    if (
        record.approval_status != db.SUBMISSION_APPROVAL_PENDING
        or record.status != db.SUBMISSION_STATUS_SUBMITTED
        or not _live_manual_task(task)
    ):
        await _safe_answer(answer, MSG_STALE)
        await _safe_edit(edit, chat_id, message_id, MSG_EDIT_CLOSED, None)
        return "stale"

    approver_id = manual_task_approver_user_id(task)
    if approver_id is None:
        await _safe_answer(answer, MSG_STALE)
        await _safe_edit(edit, chat_id, message_id, MSG_EDIT_CLOSED, None)
        return "stale"

    # ── Authority: task-specific approver only (MT-TASK-15, no      ─
    #    is_admin bypass).  Non-approvers get an Arabic authorization
    #    response and NOTHING is mutated or shown.
    if actor_user_id != approver_id:
        await _safe_answer(answer, MSG_REVIEW_UNAUTHORIZED)
        return "unauthorized"

    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        await _safe_answer(answer, MSG_ERROR)
        return "error"

    # ── Persist the linkage (idempotent), then render the EXISTING
    #    inbox representation with the existing Approve/Reject buttons.
    try:
        AdminNotificationStore.create_linkage(
            OPERATION_MANUAL_PROOF,
            record.submission_id,
            chat_id,
            message_id,
        )
    except Exception:
        logger.exception(
            "Could not persist review linkage: claim=%s chat=%s",
            record.submission_id, chat_id,
        )
        await _safe_answer(answer, MSG_ERROR)
        return "error"

    await _safe_edit(
        edit,
        chat_id,
        message_id,
        build_pending_text(task, record),
        build_decision_keyboard(record.submission_id),
    )
    await _safe_answer(answer, "")  # dismiss the press, no toast
    logger.info(
        "Manual review opened from queue: claim=%d task=%d actor=%d",
        record.submission_id, record.task_id, actor_user_id,
    )
    return "opened"


async def handle_page_callback(
    data: object,
    actor_user_id: object,
    *,
    chat_id: object,
    message_id: object,
    answer: AnswerFunc,
    edit: EditFunc,
) -> str:
    """Re-render one queue page from a FRESH read-only snapshot.

    Paging is pure UI navigation: it takes a fresh list (so a decided
    claim simply disappears), clamps the untrusted page id into range,
    and mutates no claim state.
    """
    page = parse_page_callback(data)
    if page is None:
        await _safe_answer(answer, MSG_INVALID)
        return "invalid"
    if not isinstance(actor_user_id, int) or not is_admin(actor_user_id):
        await _safe_answer(answer, MSG_ADMIN_ONLY)
        return "forbidden"

    try:
        claims = list_pending_manual_claims()
    except Exception:
        logger.exception("Review queue page load failed")
        await _safe_answer(answer, MSG_ERROR)
        return "error"

    text, markup = build_queue_page(claims, page)
    await _safe_edit(edit, chat_id, message_id, text, markup)
    await _safe_answer(answer, "")  # dismiss the press, no toast
    return "page"


# ── python-telegram-bot adapters (registered once from bot.py) ───────


def _non_private_chat(update) -> bool:
    """True only when *update* positively targets a group/channel.

    MT-ADMIN-02 isolation semantics, inlined to keep this module free
    of any import cycle with bot.py: unknown chat types are NOT
    treated as groups.
    """
    chat = getattr(update, "effective_chat", None)
    chat_type = getattr(chat, "type", None)
    return isinstance(chat_type, str) and chat_type != "private"


async def reviews_command(update, context) -> None:
    """/reviews — admin-only pending manual-review queue.

    Registered by bot.py as ``CommandHandler("reviews", ...)``.
    Private chats only: a group/channel invocation produces ZERO
    replies (no unsolicited operational behavior).  Non-admins get the
    existing safe admin-only refusal and never see queue data.
    """
    if _non_private_chat(update):
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    user = getattr(update, "effective_user", None)
    actor_id = getattr(user, "id", 0)
    if not isinstance(actor_id, int) or not is_admin(actor_id):
        await message.reply_text(MSG_ADMIN_ONLY)
        return

    try:
        claims = list_pending_manual_claims()
    except Exception:
        logger.exception("Review queue load failed")
        await message.reply_text(MSG_ERROR)
        return

    text, markup = build_queue_page(claims, 1)
    await message.reply_text(text, reply_markup=markup)
    logger.info(
        "Review queue shown: actor=%d claims=%d", actor_id, len(claims)
    )


async def review_queue_callback(update, context) -> None:
    """PTB handler for ``mrview:`` / ``mrvp:`` callbacks (MT-ADMIN-04).

    Thin transport adapter: private-chat isolation, answer/edit
    plumbing, then delegation to :func:`handle_review_callback` /
    :func:`handle_page_callback`.  Registered by bot.py as::

        app.add_handler(CallbackQueryHandler(
            admin_review_queue.review_queue_callback,
            pattern=r"^mr(view|vp):",
        ), group=5)
    """
    query = getattr(update, "callback_query", None)
    if query is None:
        return

    if _non_private_chat(update):
        try:
            await query.answer()
        except TelegramError:
            pass
        return

    async def _answer(text: str) -> None:
        try:
            if text:
                await query.answer(text=text)
            else:
                await query.answer()
        except TelegramError:
            logger.debug(
                "Could not answer review queue callback", exc_info=True
            )

    async def _edit(
        chat_id: int, message_id: int, text: str, reply_markup=None
    ) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except TelegramError:
            logger.debug(
                "Could not edit queue message %s/%s",
                chat_id, message_id,
                exc_info=True,
            )

    pressed = getattr(query, "message", None)
    pressed_chat = getattr(pressed, "chat", None)
    chat_id = getattr(pressed_chat, "id", None)
    message_id = getattr(pressed, "message_id", None)

    actor = getattr(query, "from_user", None)
    actor_id = getattr(actor, "id", 0)
    data = query.data

    try:
        if parse_page_callback(data) is not None or (
            isinstance(data, str) and data.startswith(f"{CALLBACK_PAGE_PREFIX}:")
        ):
            status = await handle_page_callback(
                data,
                actor_id,
                chat_id=chat_id,
                message_id=message_id,
                answer=_answer,
                edit=_edit,
            )
        else:
            status = await handle_review_callback(
                data,
                actor_id,
                chat_id=chat_id,
                message_id=message_id,
                answer=_answer,
                edit=_edit,
            )
    except Exception:
        logger.exception(
            "Review queue callback failed: actor=%r data=%r",
            actor_id, data,
        )
        await _answer(MSG_ERROR)
        return
    logger.info(
        "Review queue callback: actor=%r data=%r → %s",
        actor_id, data, status,
    )
