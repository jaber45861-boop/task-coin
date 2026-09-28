"""
Admin Control Center (MT-ADMIN-32 foundation → MT-ADMIN-33 ops)
==============================================================

A deliberately thin, READ-ONLY dashboard over the EXISTING admin
surfaces and stores, extended operationally (MT-ADMIN-33): attention-
first queue navigation, richer rate status (value + provider +
freshness) and an explicit task-review route — still with zero
financial mutation of its own.

Entry point
-----------
``/control`` — exactly one command, registered exactly once in
``bot.main()`` (group 0, with the other admin commands).  Private admin
chat ONLY; ``config.is_admin`` is the SOLE authorization model (the
same model every other admin handler uses) — there is no second admin
list, no username checks, no hardcoded ids and no client-supplied
identity is ever trusted.  Group/channel invocations stay silent
(MT-ADMIN-02 isolation) and non-admins get the standard refusal before
any data is read.

Buttons (MT-ADMIN-33: attention-first)
--------------------------------------
``ctl:<surface>`` — the payload is a FIXED surface identifier from a
closed set (never user input, ids, secrets, amounts, destinations or
proof references).  The callback re-checks ``config.is_admin`` on
EVERY press, fails safely on stale/malformed presses, and DELEGATES
to the existing command handler so navigation lands on the very same
surface the command opens — the queues that need admin attention
(withdrawals, deposit proofs, manual task reviews) are first-class
buttons.  This module never reimplements those surfaces and never
duplicates the ``wd:`` / ``dp:`` / ``mr(view|vp):`` / ``pm:`` /
``atw:`` families.

Data rules
----------
Every metric comes from an existing authoritative read:

* active tasks        → ``db.list_tasks(active_only=True)``
* pending review      → ``admin_review_queue.list_pending_manual_claims()``
* pending withdrawals → ``withdrawal_store.SqliteWithdrawalRepository().list_pending()``
* pending proofs      → ``deposit_proof_store.list_pending_proofs()``
* payment methods     → ``payment_method_store.list_payment_methods()``
  (withdrawal-capable = active — exactly the read behind the
  user-facing payout list; deposit-enabled = active + opt-in)
* rate                → ``rate_store.get_current_quote()``
  (value + provider + freshness — the quote is fresh BY CONTRACT,
  stale rows raise and render as ``غير متاح``)

No aggregate without an authoritative interface is invented (deposit
store exposes no list-query for raw pending deposits, so none is
shown); no wallet or ledger balance is ever computed here; a rate is
NEVER constructed by hand and NEVER read from ``platform_settings``.
Opening the dashboard opens no financial transaction and mutates
nothing — wallet/ledger/withdrawal/deposit/payment-method/rate/task
state are untouched by construction.  Failures degrade to
``غير متاح`` and are logged with the actor id only — tracebacks are
never shown to administrators or users, and no secret, RPC credential,
environment value, proof storage key or private payment destination
can reach the dashboard output.
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import admin_review_queue
import db
import deposit_proof_admin
import deposit_proof_store
import payment_method_admin
import payment_method_store
import rate_admin
import rate_store
import withdrawal_admin
import withdrawal_store
from config import is_admin
from rate_quote import RateQuoteError
from rate_store import RateStoreError

logger = logging.getLogger(__name__)

# ── Arabic UI strings (project convention: plain text, no parse mode) ──
MSG_ADMIN_ONLY = "⛔ هذا الأمر للمشرفين فقط."
MSG_INVALID = "⛔ طلب غير صالح."
MSG_ERROR = "⛔ حدث خطأ، حاول مرة أخرى."

NA = "غير متاح"

HEADER = "🎛️ لوحة التحكم"

TASKS_HEADER = "📋 المهام"
WITHDRAWALS_HEADER = "💸 السحوبات"
DEPOSITS_HEADER = "💵 الإيداعات"
PAYMETHODS_HEADER = "💳 طرق الدفع"
RATE_HEADER = "💱 سعر USDT"
USERS_HEADER = "👤 المستخدمون"

# Button labels (MT-ADMIN-33) — attention-first: the queues that need
# admin action lead; the remaining management surfaces follow.
BTN_REVIEWS = "📋 مراجعات المهام"
BTN_WITHDRAWALS = "💸 السحوبات المعلقة"
BTN_DEPOSITS = "💵 إثباتات الدفع"
BTN_PAYMETHODS = "💳 طرق الدفع"
BTN_TASKS = "📋 المهام"
BTN_RATE = "💱 إدارة السعر"

# Closed callback set — ``ctl:<surface>`` with a fixed surface id only.
CALLBACK_PREFIX = "ctl:"
_SURFACE_RE = re.compile(
    r"ctl:(tasks|reviews|withdrawals|deposits|paymethods|rate)"
)

# Navigation: surface → the EXISTING command text (for the shim) and
# the existing handler that renders the real surface (looked up at
# call time so tests can spy and imports never cycle).
_NAV_COMMANDS = {
    "tasks": "/listtasks",
    "reviews": "/reviews",
    "withdrawals": "/withdrawals",
    "deposits": "/deposits",
    "paymethods": "/paymethods",
    "rate": "/setrate",
}


# ── Shared MT-ADMIN-02 isolation helpers (inlined, no import cycle) ───


def _non_private_chat(update) -> bool:
    """True only when *update* positively targets a group/channel.

    MT-ADMIN-02 isolation semantics: unknown chat types are NOT
    treated as groups.
    """
    chat = getattr(update, "effective_chat", None)
    chat_type = getattr(chat, "type", None)
    return isinstance(chat_type, str) and chat_type != "private"


def _actor_id(value: object) -> int | None:
    """A trusted positive int id, or None (untrusted identities die)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


async def _safe_answer(query, text: str | None) -> None:
    try:
        if text:
            await query.answer(text=text)
        else:
            await query.answer()
    except Exception:
        logger.debug("Could not answer control callback", exc_info=True)


# ── Read-only aggregates (existing authoritative interfaces ONLY) ────


def _metric(name: str, factory):
    """Run one authoritative read; log and degrade to ``None``.

    Unexpected failures are logged (never swallowed silently) and the
    dashboard still renders — a broken metric shows ``غير متاح``.
    """
    try:
        return factory()
    except Exception:
        logger.exception("Control center metric failed: %s", name)
        return None


def _count_active_tasks() -> int:
    return len(db.list_tasks(active_only=True))


def _count_pending_reviews() -> int:
    return len(admin_review_queue.list_pending_manual_claims())


def _count_pending_withdrawals() -> int:
    return len(withdrawal_store.SqliteWithdrawalRepository().list_pending())


def _count_pending_proofs() -> int:
    return len(deposit_proof_store.list_pending_proofs())


def _payment_method_counts() -> tuple[int, int]:
    """(active withdrawal-capable, active deposit-enabled).

    One existing list read: ``is_active`` IS the withdrawal-capable
    set (the user-facing payout list reads exactly
    ``list_payment_methods(active_only=True)``) and the
    ``deposits_enabled`` opt-in gates the user deposit list.
    """
    methods = payment_method_store.list_payment_methods()
    active = sum(1 for m in methods if m.is_active)
    deposit_enabled = sum(
        1 for m in methods if m.is_active and m.deposits_enabled
    )
    return active, deposit_enabled


def _current_rate_quote() -> tuple[str, str] | None:
    """Fresh ``(rate text, provider)``, or None (missing/stale row).

    Uses ``rate_store.get_current_quote()`` — the ONE authoritative
    read (MT-ADMIN-26).  A returned quote is fresh BY CONTRACT, so the
    freshness state is ``صالح``; missing/stale/corrupt rows raise and
    degrade to a safe unavailable state.  No RateQuote is ever
    constructed here and no rate is ever read from
    ``platform_settings``.
    """
    try:
        quote = rate_store.get_current_quote()
    except (RateQuoteError, RateStoreError):
        # Missing / stale / invalid persisted row → safe status.
        return None
    return quote.rate_text, quote.provider


def collect_snapshot() -> dict:
    """Read-only aggregate snapshot — every source authoritative."""
    pm_counts = _metric("payment_methods", _payment_method_counts)
    pm_active, pm_deposit = (
        pm_counts if pm_counts is not None else (None, None)
    )
    return {
        "tasks_active": _metric("tasks_active", _count_active_tasks),
        "tasks_pending_review": _metric(
            "tasks_pending_review", _count_pending_reviews
        ),
        "withdrawals_pending": _metric(
            "withdrawals_pending", _count_pending_withdrawals
        ),
        "proofs_pending": _metric(
            "deposits_pending_proofs", _count_pending_proofs
        ),
        "pm_active": pm_active,
        "pm_deposit_enabled": pm_deposit,
        "rate": _metric("rate", _current_rate_quote),
    }


def _num(value: object) -> str:
    return NA if value is None else str(value)


def build_dashboard_text(snapshot: dict) -> str:
    """Plain-text dashboard (Arabic, compact, mobile-friendly)."""
    rate = snapshot.get("rate")
    if isinstance(rate, tuple) and len(rate) == 2:
        # Fresh quote → value, provider, freshness state (all three
        # come straight from the authoritative quote — never built).
        rate_lines = [
            f"{rate[0]} USDT/EGP",
            f"المصدر: {rate[1]}",
            "الحالة: صالح",
        ]
    else:
        rate_lines = [f"USDT/EGP: {NA}"]
    lines = [
        HEADER,
        "",
        TASKS_HEADER,
        f"المهام النشطة: {_num(snapshot.get('tasks_active'))}",
        f"بانتظار المراجعة: {_num(snapshot.get('tasks_pending_review'))}",
        "",
        WITHDRAWALS_HEADER,
        f"السحوبات المعلقة: {_num(snapshot.get('withdrawals_pending'))}",
        "",
        DEPOSITS_HEADER,
        f"إثباتات الدفع: {_num(snapshot.get('proofs_pending'))}",
        "",
        PAYMETHODS_HEADER,
        f"طرق الدفع النشطة: {_num(snapshot.get('pm_active'))}",
        f"طرق الإيداع المتاحة: {_num(snapshot.get('pm_deposit_enabled'))}",
        "",
        RATE_HEADER,
        *rate_lines,
        "",
        USERS_HEADER,
        f"الإحصائيات: {NA}",
    ]
    return "\n".join(lines)


def build_dashboard_keyboard() -> InlineKeyboardMarkup:
    """Attention-first navigation — fixed ``ctl:<surface>`` payloads.

    The queues requiring an admin decision lead the keyboard; every
    button routes to the EXISTING operational surface (no review,
    approval or rejection logic lives here).
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_WITHDRAWALS, callback_data="ctl:withdrawals"
                ),
                InlineKeyboardButton(
                    BTN_DEPOSITS, callback_data="ctl:deposits"
                ),
            ],
            [
                InlineKeyboardButton(
                    BTN_REVIEWS, callback_data="ctl:reviews"
                ),
                InlineKeyboardButton(
                    BTN_PAYMETHODS, callback_data="ctl:paymethods"
                ),
            ],
            [
                InlineKeyboardButton(BTN_TASKS, callback_data="ctl:tasks"),
                InlineKeyboardButton(BTN_RATE, callback_data="ctl:rate"),
            ],
        ]
    )


def parse_callback(data: object) -> str | None:
    """Fixed surface id from ``ctl:<surface>``, or None (stale/other)."""
    if not isinstance(data, str):
        return None
    match = _SURFACE_RE.fullmatch(data)
    return match.group(1) if match else None


# ── Navigation delegation to the EXISTING command handlers ───────────


def _nav_update(update, command_text: str):
    """Present the callback update to an existing command handler as
    if the admin had typed *command_text* in this private chat.

    The handler sees the REAL ``effective_user``/``effective_chat`` —
    so its own admin + isolation checks re-run unchanged — and a
    minimal message shim whose ``reply_text`` is the real bound
    ``Message.reply_text``.  The handler renders its OWN surface;
    nothing is duplicated here.  ``None`` when the pressed message is
    gone (stale press → fail safely).
    """
    query = getattr(update, "callback_query", None)
    real_message = getattr(query, "message", None)
    if real_message is None:
        return None
    message = SimpleNamespace(
        text=command_text,
        reply_text=real_message.reply_text,
    )
    return SimpleNamespace(
        effective_user=getattr(update, "effective_user", None),
        effective_chat=getattr(update, "effective_chat", None),
        message=message,
    )


async def _open_tasks(shim, context) -> None:
    # Local import: bot.py imports this module — a top-level import
    # would cycle.  bot is fully loaded by the time a press happens.
    import bot

    await bot.list_tasks(shim, context)


async def _open_reviews(shim, context) -> None:
    await admin_review_queue.reviews_command(shim, context)


async def _open_withdrawals(shim, context) -> None:
    await withdrawal_admin.withdrawals_command(shim, context)


async def _open_deposits(shim, context) -> None:
    await deposit_proof_admin.deposits_command(shim, context)


async def _open_paymethods(shim, context) -> None:
    await payment_method_admin.paymethods_command(shim, context)


async def _open_rate(shim, context) -> None:
    await rate_admin.setrate_command(shim, context)


_NAVIGATORS = {
    "tasks": _open_tasks,
    "reviews": _open_reviews,
    "withdrawals": _open_withdrawals,
    "deposits": _open_deposits,
    "paymethods": _open_paymethods,
    "rate": _open_rate,
}


# ── PTB handlers ──────────────────────────────────────────────────────


async def control_command(update, context) -> None:
    """``/control`` — the Admin Control Center dashboard.

    Private admin chat ONLY; group/channel invocations produce ZERO
    replies.  Non-admins get the standard admin-only refusal before
    any metric is read, so no administrative data leaks.  Read-only:
    renders the aggregate snapshot + navigation keyboard; opens no
    transaction and mutates nothing.
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
        text = build_dashboard_text(collect_snapshot())
        markup = build_dashboard_keyboard()
    except Exception:
        # Log actor id + failure only — never echo payloads/tracebacks.
        logger.exception("Control center render failed: admin=%d", actor)
        await message.reply_text(MSG_ERROR)
        return

    await message.reply_text(text, reply_markup=markup)
    logger.info("Control center opened: admin=%d", actor)


async def control_callback(update, context) -> None:
    """``ctl:`` callbacks — private admin chat ONLY, server re-reads all.

    The payload is a fixed surface identifier (no ids, no user input,
    no secrets); authorization re-runs ``config.is_admin`` on every
    press; stale/unknown presses fail safely; and the view is
    DELEGATED to the existing command handler.  This handler renders
    no data of its own and mutates nothing — ever.
    """
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    if _non_private_chat(update):
        await _safe_answer(query, None)
        return
    op = parse_callback(getattr(query, "data", None))
    if op is None:
        await _safe_answer(query, MSG_INVALID)
        return
    actor = _actor_id(getattr(getattr(query, "from_user", None), "id", None))
    if actor is None or not is_admin(actor):
        await _safe_answer(query, MSG_ADMIN_ONLY)
        return

    shim = _nav_update(update, _NAV_COMMANDS[op])
    if shim is None:
        # Stale press: the message behind the button is gone.
        await _safe_answer(query, MSG_INVALID)
        return

    try:
        await _NAVIGATORS[op](shim, context)
    except Exception:
        # Log actor + surface only — never payloads or tracebacks.
        logger.exception(
            "Control navigation failed: admin=%d surface=%s", actor, op
        )
        await _safe_answer(query, MSG_ERROR)
        return
    await _safe_answer(query, None)
    logger.info("Control navigation opened: admin=%d surface=%s", actor, op)
