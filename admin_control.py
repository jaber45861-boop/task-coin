"""
Admin System Foundation (MT-ADMIN-32/33 → MT-ADMIN-34)
====================================================

The architectural foundation of the professional Telegram Admin
System: a deliberately thin, READ-ONLY dashboard and navigation layer
over the EXISTING admin surfaces, stores and services.

Product boundary
----------------
The Mini App is the USER interface (wallet, tasks, withdrawals,
deposits, proof upload, user history).  The Telegram private-chat
admin surface — this module — is for administrators only.  No admin
control moves into the Mini App and no user control moves into here.

Entry point
-----------
``/control`` — exactly one command, registered exactly once in
``bot.main()`` (group 0) — stays the CANONICAL Admin System entry
(``/admin`` remains the pre-existing channel panel; no competing entry
is created).  Private admin chat ONLY; ``config.is_admin`` is the
SOLE authorization model, centralized in two choke-point helpers
(``_authorize_command`` / ``_authorize_actor``) so future role
separation can land in ONE place — while every target handler keeps
re-checking its own authorization (a press is never trusted merely
because it came from the Control Center).  Group/channel invocations
stay silent (MT-ADMIN-02 isolation) and non-admins get the standard
refusal before any data is read.

Module registry (MT-ADMIN-34)
-----------------------------
``MODULES`` is a small, static, frozen registry — stable key, Arabic
label, optional description and the EXISTING command to navigate to
(``None`` = reserved slot rendered with a safe unavailable state).  It
holds NO financial state, NO secrets, NO ids and is NOT a second
database.  ``ctl:<key>`` payloads are exactly the registry keys plus
the fixed ``refresh`` operation — bounded and static, never amounts,
destinations, rows, JSON or user input — and stay clear of every
foreign namespace (``wd:`` / ``dp:`` / ``pm:`` / ``mr(view|vp):`` /
``atw:`` / ``mproof:`` / ``sup:``).  Reserved modules (users, rewards,
broadcast, settings, admins, logs, health) answer with a safe
not-yet-available notice — never fake functionality, never fake
metrics.

Back navigation
---------------
Delegated surfaces own their own navigation; the predictable route
back to ``🎛 مركز إدارة البوت`` is the canonical ``/control``
(successful presses answer with a fixed back-hint toast).  The
dashboard itself refreshes in place via ``ctl:refresh`` — a read-only
re-render.  Fixed bounded callbacks only, no serialized objects.

Data rules (authoritative reads only)
-------------------------------------
* active tasks        → ``db.list_tasks(active_only=True)``
* pending review      → ``admin_review_queue.list_pending_manual_claims()``
* pending withdrawals → ``withdrawal_store.SqliteWithdrawalRepository().list_pending()``
* pending proofs      → ``deposit_proof_store.list_pending_proofs()``
* payment methods     → ``payment_method_store.list_payment_methods()``
  (withdrawal-capable = active — exactly the read behind the
  user-facing payout list; deposit-enabled = active + opt-in)
* rate                → ``rate_store.get_current_quote()`` — value,
  provider and freshness; never constructed, never from
  ``platform_settings`` (fresh BY CONTRACT; stale rows raise)
* users               → no authoritative safe read exists → ``غير متاح``

No aggregate without an authoritative interface is invented (deposit
store exposes no list-query for raw pending deposits, so none is
shown); no wallet or ledger balance is ever computed here.  Opening
the dashboard or a placeholder opens no transaction and mutates
nothing — the sole financial authority stays inside the existing
services (WithdrawalService, deposit review services, ``rate_store``,
the payment-method store).  Failures degrade to ``غير متاح`` / safe
callback answers and are logged with actor id + module key only —
tracebacks are never shown to administrators or users, and no secret,
RPC credential, environment value, proof storage key or private
payment destination can reach the dashboard output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
MSG_MODULE_UNAVAILABLE = "🔒 هذه الوحدة غير متاحة بعد."
BACK_HINT = "↩️ للعودة اكتب: /control"

NA = "غير متاح"

HEADER = "🎛️ مركز إدارة البوت"
SEPARATOR = "━" * 18
OVERVIEW_HEADER = "📊 نظرة عامة"
ADMIN_SECTION = "🔧 الإدارة"
SYSTEM_SECTION = "⚙️ النظام"

TASKS_HEADER = "📋 المهام"
WITHDRAWALS_HEADER = "💸 السحوبات"
DEPOSITS_HEADER = "💵 الإيداعات"
PAYMETHODS_HEADER = "💳 طرق الدفع"
RATE_HEADER = "💱 سعر USDT"
USERS_HEADER = "👤 المستخدمون"

# ── Module registry (MT-ADMIN-34) ─────────────────────────────────────
# Static navigation metadata for ONE admin module.  ``command`` is the
# EXISTING command the Control Center delegates to; ``None`` reserves
# the slot with a safe unavailable state.  No financial state, no
# secrets, no ids — deliberately NOT a database and NOT a framework.


@dataclass(frozen=True)
class AdminModule:
    key: str
    label: str
    description: str = ""
    command: str | None = None


MODULES: tuple[AdminModule, ...] = (
    AdminModule("users", "👥 المستخدمون", "قراءة مستخدمين آمنة (قريباً)"),
    AdminModule("tasks", "📋 المهام", "قائمة المهام الحالية", "/listtasks"),
    AdminModule(
        "reviews", "📋 مراجعات المهام", "طابور المراجعة اليدوية", "/reviews"
    ),
    AdminModule(
        "withdrawals", "💸 السحوبات", "طابور مراجعة السحب", "/withdrawals"
    ),
    AdminModule(
        "deposits", "💵 الإيداعات", "طابور إثباتات الإيداع", "/deposits"
    ),
    AdminModule(
        "paymethods", "💳 طرق الدفع", "إدارة وسائل الدفع", "/paymethods"
    ),
    AdminModule("rate", "💱 سعر USDT", "ضبط سعر الصرف اليدوي", "/setrate"),
    AdminModule("rewards", "🎁 المكافآت", "إدارة المكافآت (قريباً)"),
    AdminModule("broadcast", "📢 الإعلانات", "الإعلانات العامة (قريباً)"),
    AdminModule("settings", "⚙️ الإعدادات", "إعدادات المنصة (قريباً)"),
    AdminModule("admins", "🔐 المشرفون", "صلاحيات المشرفين (قريباً)"),
    AdminModule("logs", "📝 السجلات", "سجل العمليات (قريباً)"),
    AdminModule("health", "🩺 حالة النظام", "حالة الخدمات (قريباً)"),
)

MODULES_BY_KEY: dict[str, AdminModule] = {m.key: m for m in MODULES}

# Closed callback set — registry keys plus the fixed refresh op.
CALLBACK_PREFIX = "ctl:"
OP_REFRESH = "refresh"
_KNOWN_OPS = frozenset(MODULES_BY_KEY) | {OP_REFRESH}


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


# ── Centralized authorization (MT-ADMIN-34: THE choke points) ────────
# ``config.is_admin`` stays the SOLE model; both helpers funnel every
# Control Center entry through it before any read.  Target handlers
# still re-check authorization themselves — a press is never trusted
# merely because it came from here — so future role separation can
# land in these two places without touching every handler.


async def _authorize_command(update) -> tuple[object, int] | None:
    """``/control`` gate.  Returns ``(message, actor)`` to proceed,
    or None — groups/channels and untrusted identities stay silent,
    non-admins get the standard refusal (already sent here), and no
    metric has been read yet.
    """
    if _non_private_chat(update):
        return None
    message = getattr(update, "message", None)
    if message is None:
        return None
    actor = _actor_id(
        getattr(getattr(update, "effective_user", None), "id", None)
    )
    if actor is None:
        return None
    if not is_admin(actor):
        await message.reply_text(MSG_ADMIN_ONLY)
        return None
    return message, actor


async def _authorize_actor(query) -> int | None:
    """``ctl:`` gate.  Returns the trusted admin id, or None (a
    refusal answer has already been sent).  Runs before any read.
    """
    actor = _actor_id(getattr(getattr(query, "from_user", None), "id", None))
    if actor is None or not is_admin(actor):
        await _safe_answer(query, MSG_ADMIN_ONLY)
        return None
    return actor


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
        SEPARATOR,
        "",
        OVERVIEW_HEADER,
        "",
        USERS_HEADER,
        NA,  # no authoritative safe user read exists (§11) — honestly so.
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
        SEPARATOR,
        "",
        f"{ADMIN_SECTION} · {SYSTEM_SECTION}",
    ]
    return "\n".join(lines)


def build_dashboard_keyboard() -> InlineKeyboardMarkup:
    """Registry-driven, section-grouped navigation (fixed payloads).

    Management modules lead, reserved/system modules follow, refresh
    closes the keyboard.  Every button routes to the EXISTING
    operational surface (or a safe unavailable notice) — no review,
    approval or rejection logic lives here.
    """

    def _btn(key: str) -> InlineKeyboardButton:
        module = MODULES_BY_KEY[key]
        return InlineKeyboardButton(
            module.label, callback_data=f"{CALLBACK_PREFIX}{key}"
        )

    rows = [
        [_btn("users"), _btn("tasks")],
        [_btn("reviews"), _btn("withdrawals")],
        [_btn("deposits"), _btn("paymethods")],
        [_btn("rate"), _btn("rewards")],
        [_btn("broadcast")],
        [_btn("settings"), _btn("admins")],
        [_btn("logs"), _btn("health")],
        [
            InlineKeyboardButton(
                "🔄 تحديث", callback_data=f"{CALLBACK_PREFIX}{OP_REFRESH}"
            )
        ],
    ]
    return InlineKeyboardMarkup(rows)


def parse_callback(data: object) -> str | None:
    """Registry key (or ``refresh``) from ``ctl:<op>``, else None.

    The closed op set is the static registry — unknown, malformed and
    foreign-namespace payloads (``wd:``, ``dp:``, ...) fail safely.
    """
    if not isinstance(data, str):
        return None
    if not data.startswith(CALLBACK_PREFIX):
        return None
    op = data[len(CALLBACK_PREFIX):]
    return op if op in _KNOWN_OPS else None


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
    """``/control`` — the canonical Admin System dashboard entry.

    Private admin chat ONLY; group/channel invocations produce ZERO
    replies.  Non-admins get the standard admin-only refusal through
    the CENTRALIZED gate before any metric is read, so no
    administrative data leaks.  Read-only: renders the aggregate
    snapshot + registry keyboard; opens no transaction and mutates
    nothing.
    """
    authorized = await _authorize_command(update)
    if authorized is None:
        return
    message, actor = authorized

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


async def _refresh_dashboard(query, actor: int) -> None:
    """``ctl:refresh`` — read-only in-place re-render of the dashboard.

    Opens no transaction and mutates nothing; a stale or unchanged
    message edit degrades to a safe no-op (logged with actor id only).
    """
    try:
        text = build_dashboard_text(collect_snapshot())
        markup = build_dashboard_keyboard()
    except Exception:
        logger.exception("Control refresh render failed: admin=%d", actor)
        await _safe_answer(query, MSG_ERROR)
        return
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:
        # Stale/unchanged message — a read-only refresh never fails
        # loudly; log actor id only, no payloads.
        logger.info("Control refresh edit skipped: admin=%d", actor)
    await _safe_answer(query, None)
    logger.info("Control dashboard refreshed: admin=%d", actor)


async def control_callback(update, context) -> None:
    """``ctl:`` callbacks — private admin chat ONLY, server re-reads all.

    Payloads are static registry keys (no ids, no user input, no
    secrets, no amounts); the CENTRALIZED gate re-runs
    ``config.is_admin`` before any read; unknown/stale presses fail
    safely; reserved modules answer a safe unavailable notice; and
    implemented modules DELEGATE to the existing command handlers
    (which re-check authorization themselves).  ``ctl:refresh``
    re-renders read-only.  This handler renders no data of its own
    and mutates nothing — ever.
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
    actor = await _authorize_actor(query)
    if actor is None:
        return

    if op == OP_REFRESH:
        await _refresh_dashboard(query, actor)
        return

    module = MODULES_BY_KEY[op]
    if module.command is None:
        # Reserved slot — safe not-yet-implemented state, NO reads.
        await _safe_answer(query, MSG_MODULE_UNAVAILABLE)
        logger.info(
            "Control module unavailable: admin=%d module=%s", actor, op
        )
        return

    shim = _nav_update(update, module.command)
    if shim is None:
        # Stale press: the message behind the button is gone.
        await _safe_answer(query, MSG_INVALID)
        return

    try:
        await _NAVIGATORS[op](shim, context)
    except Exception:
        # Log actor + module only — never payloads or tracebacks.
        logger.exception(
            "Control navigation failed: admin=%d module=%s", actor, op
        )
        await _safe_answer(query, MSG_ERROR)
        return
    await _safe_answer(query, BACK_HINT)
    logger.info("Control navigation opened: admin=%d module=%s", actor, op)
