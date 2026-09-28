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
``atw:`` / ``mproof:`` / ``sup:``).  Reserved modules (rewards,
broadcast, settings, admins, logs, health) answer with a safe
not-yet-available notice — never fake functionality, never fake
metrics.  The ``users`` module (MT-ADMIN-35) is rendered IN PLACE by
this module itself over a small closed ``ctl:users[:…]`` sub-grammar
(bounded digits only — never JSON, never free text, never amounts or
destinations) built strictly on the authoritative read-only store
interfaces: ``db.count_users`` / ``db.list_users`` / ``db.get_user``.
The ``tasks`` module (MT-ADMIN-36) is likewise rendered in place
over its own closed ``ctl:tasks[:…]`` grammar (fixed operation
tokens + bounded digits): reads from ``db.list_tasks`` /
``db.get_task``; the ONLY mutation is the existing
``db.update_task`` store contract, reached solely through a
confirmation card plus a fresh re-read (stale presses never write,
confirmations are single-use); creation delegates to the canonical
``/addtask`` wizard → ``task_creation`` service.  Authorization
runs BEFORE payload parsing, so a non-admin never reaches a read.

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
* users               → ``db.count_users()`` (the dashboard metric),
  ``db.list_users()`` (one bounded, deterministically ordered page)
  and ``db.get_user()`` (detail) — identity fields only; no wallet,
  no destinations, no fabricated activity/new-user classifications
* tasks               → ``db.list_tasks()`` (panel; sorted by id
  HERE for deterministic pagination) and ``db.get_task()`` (detail);
  mutations only via the existing ``db.update_task()`` contract
  (the same call ``/offtask`` makes) after confirmation + re-read;
  creation → the existing ``/addtask`` wizard delegation

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
import re
from dataclasses import dataclass
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import MessageHandler, filters

import admin_review_queue
import db
import deposit_proof_admin
import deposit_proof_store
import payment_method_admin
import payment_method_store
import rate_admin
import rate_store
import task_taxonomy
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
MSG_USER_NOT_FOUND = "⛔ المستخدم غير موجود."
# MT-ADMIN-36 task-management messages — stable Arabic answers, never
# a traceback, SQL or filesystem detail.
MSG_TASK_NOT_FOUND = "⚠️ المهمة غير موجودة."
MSG_STALE_TASK = (
    "⚠️ تغيّرت حالة المهمة. حدّث القائمة وحاول مرة أخرى."
)
MSG_NO_PENDING = "⚠️ لا توجد عملية معلّقة للتأكيد."
TOAST_ENABLED = "✅ تم التفعيل"
TOAST_DISABLED = "✅ تم التعطيل"
TOAST_EDITED = "✅ تم التعديل"
TOAST_CANCELLED = "❌ تم الإلغاء"
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
USERS_PANEL_HEADER = "👤 إدارة المستخدمين"
USER_DETAIL_HEADER = "👤 المستخدم"
# Small fixed page: bounded reads, mobile-friendly rendering.  The
# page INDEX is clamped server-side, never trusted from the payload.
USERS_PAGE_SIZE = 5
TASKS_PANEL_HEADER = "📋 إدارة المهام"
TASK_DETAIL_HEADER = "📋 تفاصيل المهمة"
# Same small-fixed-page convention as the users module: bounded rows
# per render, page index clamped server-side (MT-ADMIN-36).
TASKS_PAGE_SIZE = 5
# The ONLY task fields the edit flow exposes — both explicitly safe
# in the existing db.update_task contract (no reward, no type, no
# repeat policy — see the MT-ADMIN-36 report for the exact blockers).
_TASK_EDIT_FIELDS = {"title": "عنوان المهمة", "desc": "وصف المهمة"}

# ── Module registry (MT-ADMIN-34) ─────────────────────────────────────
# Static navigation metadata for ONE admin module.  ``command`` is the
# EXISTING command the Control Center delegates to; ``None`` marks a
# slot this module renders in place itself (``users`` MT-ADMIN-35,
# ``tasks`` MT-ADMIN-36) or a reserved unavailable slot.  No
# financial state, no secrets, no ids — deliberately NOT a database
# and NOT a framework.


@dataclass(frozen=True)
class AdminModule:
    key: str
    label: str
    description: str = ""
    command: str | None = None


MODULES: tuple[AdminModule, ...] = (
    # ``command=None``: users and tasks have no delegation command —
    # this module renders BOTH in place (MT-ADMIN-35 / MT-ADMIN-36),
    # unlike the reserved slots below which answer a safe notice.
    AdminModule("users", "👥 المستخدمون", "إدارة مستخدمين (قراءة فقط)"),
    AdminModule("tasks", "📋 المهام", "إدارة المهام (عرض/تفعيل/تعطيل)"),
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

# MT-ADMIN-35: the in-place users module owns a small closed
# sub-grammar under the SAME ctl: namespace — bounded digits only,
# never JSON, never user-supplied text, never foreign families:
#   ctl:users            user dashboard + first page
#   ctl:users:p:<page>   one page (index clamped server-side)
#   ctl:users:v:<id>     one bounded Telegram user id (detail view)
#   ctl:users:back       back to the user list
# Bounds keep stale/oversized presses out of the handler entirely:
# pages ≤ 9 digits, ids ≤ 15 digits.
OP_USERS = "users"
USERS_BACK_OP = "users:back"
_USERS_PAGE_RE = re.compile(r"users:p:([0-9]{1,9})")
_USERS_DETAIL_RE = re.compile(r"users:v:([0-9]{1,15})")

# MT-ADMIN-36: the in-place tasks module owns its own closed
# sub-grammar under the SAME ctl: namespace — fixed operation tokens
# and bounded digits only, never JSON, never free text, never
# amounts, destinations or SQL fragments:
#   ctl:tasks                                 task panel + first page
#   ctl:tasks:p:<page>                        one page (clamped)
#   ctl:tasks:v:<task_id>                     task detail
#   ctl:tasks:back                            back to the task list
#   ctl:tasks:new                             delegate to /addtask
#   ctl:tasks:enable:<task_id>                enable confirmation
#   ctl:tasks:disable:<task_id>               disable confirmation
#   ctl:tasks:edit:<task_id>                  edit field menu
#   ctl:tasks:field:<title|desc>:<task_id>    arm one text input
#   ctl:tasks:confirm:<enable|disable|edit>:<task_id>  confirmation
#   ctl:tasks:cancel:<task_id>                cancel pending / back
# Bounds keep stale/oversized presses out of the handler entirely:
# pages ≤ 9 digits, ids ≤ 15 digits, operations from fixed sets.
OP_TASKS = "tasks"
TASKS_BACK_OP = "tasks:back"
TASKS_NEW_OP = "tasks:new"
_TASKS_PAGE_RE = re.compile(r"tasks:p:([0-9]{1,9})")
_TASKS_VIEW_RE = re.compile(r"tasks:v:([0-9]{1,15})")
_TASKS_ENABLE_RE = re.compile(r"tasks:enable:([0-9]{1,15})")
_TASKS_DISABLE_RE = re.compile(r"tasks:disable:([0-9]{1,15})")
_TASKS_EDIT_RE = re.compile(r"tasks:edit:([0-9]{1,15})")
_TASKS_FIELD_RE = re.compile(r"tasks:field:(title|desc):([0-9]{1,15})")
_TASKS_CONFIRM_RE = re.compile(
    r"tasks:confirm:(enable|disable|edit):([0-9]{1,15})"
)
_TASKS_CANCEL_RE = re.compile(r"tasks:cancel:([0-9]{1,15})")


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


def _count_users() -> int:
    """Authoritative total users — the ONE source behind BOTH the
    dashboard metric and the users module (no cached count)."""
    return db.count_users()


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
        "users_total": _metric("users_total", _count_users),
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
        # MT-ADMIN-35: authoritative count from db.count_users() via
        # collect_snapshot — the same source the users module reads.
        f"{USERS_HEADER}: {_num(snapshot.get('users_total'))}",
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
    """Registry key, ``refresh`` or a canonical users/tasks sub-op,
    else None.

    The closed op set is the static registry plus the bounded-digits
    ``users`` (MT-ADMIN-35) and ``tasks`` (MT-ADMIN-36) grammars.
    Unknown, malformed, oversized and foreign-namespace payloads
    (``wd:``, ``dp:``, ...) fail safely with None.  Numeric payloads
    are canonicalized (``tasks:p:007`` → ``tasks:p:7``) so one
    page/id/op has exactly one spelling.
    """
    if not isinstance(data, str):
        return None
    if not data.startswith(CALLBACK_PREFIX):
        return None
    op = data[len(CALLBACK_PREFIX):]
    if op in _KNOWN_OPS:
        return op
    # ── users grammar (MT-ADMIN-35) ──
    if op == USERS_BACK_OP:
        return op
    match = _USERS_PAGE_RE.fullmatch(op)
    if match:
        return f"{OP_USERS}:p:{int(match.group(1))}"
    match = _USERS_DETAIL_RE.fullmatch(op)
    if match:
        return f"{OP_USERS}:v:{int(match.group(1))}"
    # ── tasks grammar (MT-ADMIN-36) ──
    if op in (TASKS_BACK_OP, TASKS_NEW_OP):
        return op
    for pattern, template in (
        (_TASKS_PAGE_RE, f"{OP_TASKS}:p:%d"),
        (_TASKS_VIEW_RE, f"{OP_TASKS}:v:%d"),
        (_TASKS_ENABLE_RE, f"{OP_TASKS}:enable:%d"),
        (_TASKS_DISABLE_RE, f"{OP_TASKS}:disable:%d"),
        (_TASKS_EDIT_RE, f"{OP_TASKS}:edit:%d"),
        (_TASKS_CANCEL_RE, f"{OP_TASKS}:cancel:%d"),
    ):
        match = pattern.fullmatch(op)
        if match:
            return template % int(match.group(1))
    match = _TASKS_FIELD_RE.fullmatch(op)
    if match:
        return f"{OP_TASKS}:field:{match.group(1)}:{int(match.group(2))}"
    match = _TASKS_CONFIRM_RE.fullmatch(op)
    if match:
        return (
            f"{OP_TASKS}:confirm:{match.group(1)}:{int(match.group(2))}"
        )
    return None


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
    "reviews": _open_reviews,
    "withdrawals": _open_withdrawals,
    "deposits": _open_deposits,
    "paymethods": _open_paymethods,
    "rate": _open_rate,
}


# ── Users module (MT-ADMIN-35): read-only, rendered in place ───────
# Authorization is re-checked by the caller BEFORE any read here.
# Every value comes from the authoritative bounded store reads — no
# SQL, no cache, no mutation, no invented "active"/"new" semantics
# (the schema has no last-seen column and no defined recency window).


def _user_label(row: dict) -> str:
    """One safe identity row — username (or first name) + Telegram id.

    Only schema identity fields; never wallet, destination, language
    or any other column.
    """
    username = row.get("username")
    name = f"@{username}" if username else (row.get("first_name") or NA)
    return f"👤 {name}\n🆔 {row.get('user_id')}"


def _page_count(total: int) -> int:
    """Pages for *total* users — always at least one so an empty state
    still renders a stable page label."""
    return max(1, -(-int(total) // USERS_PAGE_SIZE))


def collect_users_page(page: int) -> dict:
    """Read-only users-page snapshot from the authoritative store.

    The page index is CLAMPED to the real range, so oversized or stale
    presses land on the last page instead of failing.  Both reads are
    the bounded db interfaces; this module issues no SQL itself.
    """
    if isinstance(page, bool) or not isinstance(page, int):
        raise TypeError("page must be an int")
    total = db.count_users()
    pages = _page_count(total)
    page = min(max(page, 0), pages - 1)
    rows = db.list_users(USERS_PAGE_SIZE, page * USERS_PAGE_SIZE)
    return {"total": total, "page": page, "pages": pages, "rows": rows}


def build_users_text(view: dict) -> str:
    """Arabic user-management panel — identity fields only.

    "النشطون"/"الجدد" have no authoritative definition (no last-seen
    column, no defined recency window), so both honestly read
    ``غير متاح`` — never a fabricated number.
    """
    lines = [
        USERS_PANEL_HEADER,
        "",
        f"👥 إجمالي المستخدمين: {view['total']}",
        f"🟢 النشطون: {NA}",
        f"📅 الجدد: {NA}",
        "",
        "اختر مستخدمًا لعرض التفاصيل:",
    ]
    if not view["rows"]:
        lines.append("لا يوجد مستخدمون بعد.")
    lines += [
        "",
        SEPARATOR,
        f"صفحة {view['page'] + 1}/{view['pages']}",
    ]
    return "\n".join(lines)


def build_users_keyboard(view: dict) -> InlineKeyboardMarkup:
    """Tappable identity rows + bounded page nav + canonical back.

    Payloads are the fixed ctl:users grammar only — user ids come
    from the store rows, never from client input.
    """
    rows = [
        [
            InlineKeyboardButton(
                _user_label(row),
                callback_data=(
                    f"{CALLBACK_PREFIX}{OP_USERS}:v:{row['user_id']}"
                ),
            )
        ]
        for row in view["rows"]
    ]
    nav = []
    if view["page"] > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ السابق",
                callback_data=(
                    f"{CALLBACK_PREFIX}{OP_USERS}:p:{view['page'] - 1}"
                ),
            )
        )
    if view["page"] + 1 < view["pages"]:
        nav.append(
            InlineKeyboardButton(
                "التالي ➡️",
                callback_data=(
                    f"{CALLBACK_PREFIX}{OP_USERS}:p:{view['page'] + 1}"
                ),
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                "↩️ مركز الإدارة",
                callback_data=f"{CALLBACK_PREFIX}{OP_REFRESH}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_user_detail_text(user: dict | None) -> str:
    """Safe administrative profile — existing ``db.get_user`` fields.

    Last activity honestly reads ``غير متاح``: the schema has no
    authoritative last-seen column and none is inferred from
    unrelated events.  Unknown user → safe fixed notice.
    """
    if not user:
        return MSG_USER_NOT_FOUND
    username = user.get("username")
    return "\n".join(
        [
            USER_DETAIL_HEADER,
            "",
            f"🆔 Telegram ID: {user.get('user_id')}",
            f"👤 Username: {'@' + username if username else NA}",
            f"📛 الاسم: {user.get('first_name') or NA}",
            f"📅 التسجيل: {user.get('created_at') or NA}",
            f"🕒 آخر نشاط: {NA}",
        ]
    )


def build_user_detail_keyboard() -> InlineKeyboardMarkup:
    """Back to the user list — the only navigation a detail needs."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "↩️ المستخدمون",
                    callback_data=f"{CALLBACK_PREFIX}{USERS_BACK_OP}",
                )
            ]
        ]
    )


async def _edit_view_or_skip(query, text: str, markup, actor: int,
                             view: str, toast: str | None = None) -> None:
    """Edit in place; a stale/unchanged message degrades to a safe
    no-op — a view never fails loudly (refresh convention).
    *toast* optionally answers the press with a short result note."""
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:
        logger.info("Control view edit skipped: admin=%d view=%s",
                    actor, view)
    await _safe_answer(query, toast)


async def _render_users_view(query, op: str, actor: int) -> None:
    """``ctl:users*`` — the read-only user-management surface.

    The caller has ALREADY re-checked authorization before any read.
    Opens no transaction and mutates nothing; failures degrade to a
    safe error answer.  Logs carry the admin id + view kind only —
    never user records, payloads or tracebacks.
    """
    view = "users:list"
    try:
        if op == OP_USERS or op == USERS_BACK_OP:
            snapshot = collect_users_page(0)
            text = build_users_text(snapshot)
            markup = build_users_keyboard(snapshot)
        elif op.startswith(f"{OP_USERS}:p:"):
            snapshot = collect_users_page(int(op.rsplit(":", 1)[1]))
            text = build_users_text(snapshot)
            markup = build_users_keyboard(snapshot)
        elif op.startswith(f"{OP_USERS}:v:"):
            view = "users:detail"
            user = db.get_user(int(op.rsplit(":", 1)[1]))
            text = build_user_detail_text(user)
            markup = build_user_detail_keyboard()
        else:
            # Defense in depth — the parser already rejects this.
            await _safe_answer(query, MSG_INVALID)
            return
    except Exception:
        logger.exception("Control users view failed: admin=%d view=%s",
                         actor, view)
        await _safe_answer(query, MSG_ERROR)
        return
    await _edit_view_or_skip(query, text, markup, actor, view)
    logger.info("Control users view opened: admin=%d view=%s",
                actor, view)


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

    AUTH + READ ORDER (MT-ADMIN-36): private chat first, then the
    CENTRALIZED ``config.is_admin`` gate, THEN payload grammar — so a
    non-admin can never even reach payload parsing, let alone a read.
    Payloads are static registry keys plus the bounded ``users``
    (MT-ADMIN-35) and ``tasks`` (MT-ADMIN-36) sub-grammars (no
    amounts, no destinations, no secrets); unknown/stale presses
    fail safely; reserved modules answer a safe unavailable notice;
    the ``users`` and ``tasks`` modules render in place from
    authoritative reads (``tasks`` mutates only via confirmation +
    the existing ``db.update_task`` contract); and the remaining
    implemented modules DELEGATE to the existing command handlers
    (which re-check authorization themselves).  ``ctl:refresh``
    re-renders read-only.  No path owns a financial transaction.
    """
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    if _non_private_chat(update):
        await _safe_answer(query, None)
        return
    actor = await _authorize_actor(query)  # auth BEFORE grammar/reads
    if actor is None:
        return
    op = parse_callback(getattr(query, "data", None))
    if op is None:
        await _safe_answer(query, MSG_INVALID)
        return

    if op == OP_REFRESH:
        await _refresh_dashboard(query, actor)
        return

    if op == OP_USERS or op.startswith(f"{OP_USERS}:"):
        # MT-ADMIN-35: rendered in place by this module — the auth
        # gate above already re-checked config.is_admin BEFORE any
        # user data was read.
        await _render_users_view(query, op, actor)
        return

    if op == OP_TASKS or op.startswith(f"{OP_TASKS}:"):
        # MT-ADMIN-36: rendered in place by this module — the auth
        # gate above already re-checked config.is_admin BEFORE any
        # task data was read or any mutation was confirmed.  First
        # tasks press also attaches the edit-text catch-all ONCE
        # (lazy, idempotent — no extra bot.py registration).
        _ensure_text_input_handler(context)
        await _handle_tasks_op(update, context, query, op, actor)
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


# ── Tasks module (MT-ADMIN-36): management, rendered in place ───────
# Authorization is re-checked by the caller BEFORE any read here.
# Reads come from the authoritative task store (db.list_tasks /
# db.get_task); the ONLY mutation this module ever performs is the
# EXISTING store contract ``db.update_task`` — the very call
# ``/offtask`` makes — and only after a confirmation step plus a
# fresh re-read of the row (a pressed state is never trusted and a
# confirmation is single-use).  No SQL, no financial primitive and
# no reward semantics lives here: reward/type/repeat editing are
# deliberately NOT exposed (no safe contract — see the MT-ADMIN-36
# report), and creation delegates to the canonical ``/addtask``
# wizard → ``task_creation`` service (one creation path, never two).

# One pending text edit per private chat — the in-memory bridge
# between a field prompt and its confirmation.  It holds ONLY the
# bounded task id, the fixed field token, the admin id and the
# validated new text — never financial values, never secrets.
_PENDING_TASK_EDITS: dict[int, dict] = {}


def _task_ref(task: dict) -> str:
    """Truncated title + id — the only task identity in buttons and
    confirmation cards (never the full description, never payloads)."""
    title = str(task.get("title") or NA)
    if len(title) > 40:
        title = title[:40] + "…"
    return f"{title} · #{task.get('id')}"


def _task_row_label(task: dict) -> str:
    """One tappable task row for the list."""
    return f"📋 {_task_ref(task)}"


def _task_page_count(total: int) -> int:
    """Pages for *total* tasks — always at least one so an empty
    state still renders a stable page label."""
    return max(1, -(-int(total) // TASKS_PAGE_SIZE))


def collect_tasks_page(page: int) -> dict:
    """Read-only task-page snapshot from the authoritative store.

    ``db.list_tasks()`` is the ONE existing task list read (the same
    operation ``/listtasks`` and the dashboard use); rows are sorted
    by id HERE so pagination order is deterministic, and the page
    index is CLAMPED so oversized/stale presses land on the last
    page instead of failing.  Read-only: no write of any kind.
    """
    if isinstance(page, bool) or not isinstance(page, int):
        raise TypeError("page must be an int")
    tasks = sorted(db.list_tasks(), key=lambda t: t["id"])
    total = len(tasks)
    pages = _task_page_count(total)
    page = min(max(page, 0), pages - 1)
    start = page * TASKS_PAGE_SIZE
    return {
        "total": total,
        "active": sum(1 for t in tasks if t["active"]),
        "page": page,
        "pages": pages,
        "rows": tasks[start:start + TASKS_PAGE_SIZE],
    }


def build_tasks_text(view: dict) -> str:
    """Arabic task-management panel built from the authoritative
    list — counts are derived from the same read, never cached."""
    lines = [
        TASKS_PANEL_HEADER,
        "",
        f"📊 إجمالي المهام: {view['total']}",
        f"🟢 النشطات: {view['active']}",
        f"🔴 المعطلات: {view['total'] - view['active']}",
        "",
        "اختر مهمة لعرض التفاصيل:",
    ]
    if not view["rows"]:
        lines.append("📭 لا توجد مهام بعد.")
    lines += [
        "",
        SEPARATOR,
        f"صفحة {view['page'] + 1}/{view['pages']}",
    ]
    return "\n".join(lines)


def build_tasks_keyboard(view: dict) -> InlineKeyboardMarkup:
    """Tappable task rows + bounded page nav + create + canonical
    control-center back.  Payloads come from store rows only."""
    rows = [
        [
            InlineKeyboardButton(
                _task_row_label(task),
                callback_data=(
                    f"{CALLBACK_PREFIX}{OP_TASKS}:v:{task['id']}"
                ),
            )
        ]
        for task in view["rows"]
    ]
    nav = []
    if view["page"] > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ السابق",
                callback_data=(
                    f"{CALLBACK_PREFIX}{OP_TASKS}:p:{view['page'] - 1}"
                ),
            )
        )
    if view["page"] + 1 < view["pages"]:
        nav.append(
            InlineKeyboardButton(
                "التالي ➡️",
                callback_data=(
                    f"{CALLBACK_PREFIX}{OP_TASKS}:p:{view['page'] + 1}"
                ),
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                "➕ مهمة جديدة",
                callback_data=f"{CALLBACK_PREFIX}{TASKS_NEW_OP}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "↩️ مركز الإدارة",
                callback_data=f"{CALLBACK_PREFIX}{OP_REFRESH}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_task_detail_text(task: dict | None) -> str:
    """Task card from the authoritative ``db.get_task`` row —
    persisted administrative fields only.

    Missing/unreadable fields read ``غير متاح``; an unknown task
    answers the safe fixed notice.  Reward is displayed with the
    EXISTING whole-USDT contract value — never recomputed here.
    """
    if not task:
        return MSG_TASK_NOT_FOUND
    reward = task.get("reward")
    reward_text = (
        f"{reward} USDT"
        if isinstance(reward, int) and not isinstance(reward, bool)
        else NA
    )
    state = "🟢 مفعّلة" if task.get("active") else "🔴 معطلة"
    policy = task.get("repeat_policy")
    hours = task.get("repeat_hours")
    if policy == "repeatable" and isinstance(hours, int):
        repeat = f"🔁 التكرار: كل {hours} ساعة"
    elif policy in ("one_time", "repeatable"):
        repeat = "🔁 التكرار: مرة واحدة"
    else:
        repeat = f"🔁 التكرار: {NA}"
    description = str(task.get("description") or "")
    if len(description) > 500:
        description = description[:500] + "…"
    return "\n".join(
        [
            TASK_DETAIL_HEADER,
            "",
            f"🆔 المعرف: #{task.get('id')}",
            f"📌 العنوان: {task.get('title') or NA}",
            f"📝 الوصف: {description or NA}",
            f"💰 المكافأة: {reward_text}",
            f"⚡ الحالة: {state}",
            f"📡 النوع: {task.get('type') or NA}",
            repeat,
            f"📅 تاريخ الإنشاء: {task.get('created_at') or NA}",
        ]
    )


def build_task_detail_keyboard(task_id: int, task: dict | None):
    """Only mutations the repository can safely perform: the state
    toggle (existing ``db.update_task(active=...)``) and the bounded
    title/description edit menu.  No reward, no type, no delete."""
    rows = []
    if task is not None:
        if task.get("active"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🔴 تعطيل",
                        callback_data=(
                            f"{CALLBACK_PREFIX}{OP_TASKS}"
                            f":disable:{task_id}"
                        ),
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        "🟢 تفعيل",
                        callback_data=(
                            f"{CALLBACK_PREFIX}{OP_TASKS}"
                            f":enable:{task_id}"
                        ),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "✏️ تعديل",
                    callback_data=(
                        f"{CALLBACK_PREFIX}{OP_TASKS}:edit:{task_id}"
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "⬅️ رجوع",
                callback_data=f"{CALLBACK_PREFIX}{TASKS_BACK_OP}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _tasks_back_keyboard() -> InlineKeyboardMarkup:
    """Back-to-list button for stale/not-found/pending cards."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ رجوع",
                    callback_data=f"{CALLBACK_PREFIX}{TASKS_BACK_OP}",
                )
            ]
        ]
    )


def build_task_edit_menu_text(task: dict) -> str:
    """Field chooser — only fields the existing update contract
    supports for administrative edits (title, description)."""
    return "\n".join(
        [
            "✏️ تعديل المهمة",
            "",
            f"المهمة: {_task_ref(task)}",
            "",
            "اختر الحقل المراد تعديله:",
            "ستُعرض المراجعة قبل الحفظ.",
        ]
    )


def build_task_edit_menu_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📌 العنوان",
                    callback_data=(
                        f"{CALLBACK_PREFIX}{OP_TASKS}"
                        f":field:title:{task_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "📝 الوصف",
                    callback_data=(
                        f"{CALLBACK_PREFIX}{OP_TASKS}"
                        f":field:desc:{task_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ رجوع",
                    callback_data=(
                        f"{CALLBACK_PREFIX}{OP_TASKS}:v:{task_id}"
                    ),
                )
            ],
        ]
    )


def build_task_prompt_text(task: dict, field: str) -> str:
    """Input prompt after a field press — arms ONE pending edit."""
    label = _TASK_EDIT_FIELDS.get(field, NA)
    return "\n".join(
        [
            f"✏️ أرسل النص الجديد لـ{label}:",
            "",
            f"المهمة: {_task_ref(task)}",
            "ستُعرض المراجعة قبل الحفظ.",
        ]
    )


def build_task_prompt_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ إلغاء",
                    callback_data=(
                        f"{CALLBACK_PREFIX}{OP_TASKS}:cancel:{task_id}"
                    ),
                )
            ]
        ]
    )


def build_task_confirm_text(task: dict, op_label: str,
                            value: str | None = None) -> str:
    """Confirmation card — the callback carries ONLY the fixed
    operation + bounded id; the reviewed value lives in the card
    text (server-side pending state), never in the payload."""
    lines = [
        "⚠️ تأكيد العملية",
        "",
        f"المهمة: {_task_ref(task)}",
        f"العملية: {op_label}",
    ]
    if value is not None:
        preview = value if len(value) <= 200 else value[:200] + "…"
        lines += ["القيمة الجديدة:", preview]
    lines += ["", "هل تريد المتابعة؟"]
    return "\n".join(lines)


def build_task_confirm_keyboard(op_kind: str, task_id: int):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ تأكيد",
                    callback_data=(
                        f"{CALLBACK_PREFIX}{OP_TASKS}"
                        f":confirm:{op_kind}:{task_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ إلغاء",
                    callback_data=(
                        f"{CALLBACK_PREFIX}{OP_TASKS}:cancel:{task_id}"
                    ),
                )
            ],
        ]
    )


# Marker on Application.bot_data: the edit-text catch-all has been
# attached to the LIVE application (single-shot, idempotent).
_TEXT_HANDLER_MARK = "admin_tasks_edit_text_handler"


def _ensure_text_input_handler(context) -> None:
    """Attach ``task_edit_text_input`` to the live Application ONCE.

    python-telegram-bot documents ``Application.add_handler`` as
    safe to call at any time, so the text catch-all is registered
    LAZY and idempotent (bot_data marker) from the first Control
    Center tasks press — no second static registration in bot.py is
    needed, and ``^ctl:`` stays the single callback entry.  A
    context without a live Application (unit-test shims) degrades to
    a no-op.  Group 3 mirrors the wizard (0) / support (2)
    catch-all pattern; the body stays silent without pending state.
    """
    app = getattr(context, "application", None)
    bot_data = getattr(app, "bot_data", None)
    if app is None or not isinstance(bot_data, dict):
        return  # no live application (or a test shim) — no-op
    if bot_data.get(_TEXT_HANDLER_MARK):
        return  # single-shot: never double-register
    bot_data[_TEXT_HANDLER_MARK] = True
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            task_edit_text_input,
        ),
        group=3,
    )


def _apply_task_confirm(task_id: int, kind: str, chat_id, actor: int):
    """Confirmation step → the EXISTING ``db.update_task`` contract.

    Fresh re-read first: a pressed card whose state already changed
    (or whose task vanished, or whose edit value was already
    consumed) returns the safe notice and writes NOTHING — double
    confirmations can never double-apply.  Returns
    ``(text, markup, toast)`` for the caller to render.
    """
    text: str = MSG_INVALID
    markup = _tasks_back_keyboard()
    toast: str | None = None
    result = "invalid"
    if kind in ("enable", "disable"):
        desired = kind == "enable"
        task = db.get_task(task_id)
        if task is None:
            text, result = MSG_TASK_NOT_FOUND, "missing"
        elif bool(task["active"]) == desired:
            # The row already matches the pressed operation — the
            # state changed since the card was rendered.
            text, result = MSG_STALE_TASK, "stale-state"
        elif not db.update_task(task_id, active=desired):
            text, result = MSG_STALE_TASK, "stale-race"
        else:
            toast = TOAST_ENABLED if desired else TOAST_DISABLED
            result = "applied"
    elif kind == "edit":
        pending = (
            _PENDING_TASK_EDITS.pop(chat_id, None)
            if isinstance(chat_id, int)
            else None
        )
        if (
            not pending
            or pending.get("task_id") != task_id
            or "value" not in pending
        ):
            # Single-use: a second confirm finds no staged value.
            text, result = MSG_NO_PENDING, "no-pending"
        else:
            task = db.get_task(task_id)
            if task is None:
                text, result = MSG_TASK_NOT_FOUND, "missing"
            else:
                field = pending.get("field")
                kwargs = (
                    {"title": pending["value"]}
                    if field == "title"
                    else {"description": pending["value"]}
                )
                if not db.update_task(task_id, **kwargs):
                    text, result = MSG_STALE_TASK, "stale-race"
                else:
                    toast, result = TOAST_EDITED, "applied"
    if result == "applied":
        # Render the refreshed card from the authoritative re-read.
        task = db.get_task(task_id)
        text = build_task_detail_text(task)
        markup = build_task_detail_keyboard(task_id, task)
    logger.info(
        "Task management: admin=%d task=%d op=%s result=%s",
        actor, task_id, kind, result,
    )
    return text, markup, toast


async def _handle_tasks_op(update, context, query, op: str,
                           actor: int) -> None:
    """``ctl:tasks*`` — the task-management surface (MT-ADMIN-36).

    The caller has ALREADY re-checked authorization (private chat +
    config.is_admin) before any read here.  Reads use the
    authoritative store; mutations go through confirmation + fresh
    re-read into ``db.update_task`` only.  Failures degrade to a safe
    error answer.  Logs carry the admin id + view kind only — never
    task contents, payloads or tracebacks.
    """
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    view = "tasks:list"
    toast: str | None = None
    try:
        if op == TASKS_NEW_OP:
            # Creation delegates to the EXISTING /addtask entry →
            # admin_task_wizard → task_creation service.  The target
            # re-checks admin + private chat itself.
            view = "tasks:new"
            shim = _nav_update(update, "/addtask")
            if shim is None:
                await _safe_answer(query, MSG_INVALID)
                return
            import bot  # local: bot.py imports this module (cycle)

            await bot.add_task(shim, context)
            await _safe_answer(query, BACK_HINT)
            logger.info("Task creation opened: admin=%d", actor)
            return
        if op == OP_TASKS or op == TASKS_BACK_OP:
            snapshot = collect_tasks_page(0)
            text = build_tasks_text(snapshot)
            markup = build_tasks_keyboard(snapshot)
        elif op.startswith(f"{OP_TASKS}:p:"):
            snapshot = collect_tasks_page(int(op.rsplit(":", 1)[1]))
            text = build_tasks_text(snapshot)
            markup = build_tasks_keyboard(snapshot)
        elif op.startswith(f"{OP_TASKS}:v:"):
            view = "tasks:detail"
            task_id = int(op.rsplit(":", 1)[1])
            task = db.get_task(task_id)
            text = build_task_detail_text(task)
            markup = build_task_detail_keyboard(task_id, task)
        elif (
            op.startswith(f"{OP_TASKS}:enable:")
            or op.startswith(f"{OP_TASKS}:disable:")
        ):
            view = "tasks:confirm"
            kind = "enable" if ":enable:" in op else "disable"
            task_id = int(op.rsplit(":", 1)[1])
            task = db.get_task(task_id)
            desired = kind == "enable"
            if task is None:
                text, markup = MSG_TASK_NOT_FOUND, _tasks_back_keyboard()
            elif bool(task["active"]) == desired:
                # State already changed since the detail was rendered.
                text, markup = MSG_STALE_TASK, _tasks_back_keyboard()
            else:
                label = "تفعيل المهمة" if desired else "تعطيل المهمة"
                text = build_task_confirm_text(task, label)
                markup = build_task_confirm_keyboard(kind, task_id)
        elif op.startswith(f"{OP_TASKS}:edit:"):
            view = "tasks:editmenu"
            task_id = int(op.rsplit(":", 1)[1])
            task = db.get_task(task_id)
            if task is None:
                text, markup = MSG_TASK_NOT_FOUND, _tasks_back_keyboard()
            else:
                text = build_task_edit_menu_text(task)
                markup = build_task_edit_menu_keyboard(task_id)
        elif op.startswith(f"{OP_TASKS}:field:"):
            view = "tasks:input"
            parts = op.split(":")  # tasks / field / <f> / <id>
            field, task_id = parts[2], int(parts[3])
            task = db.get_task(task_id)
            if task is None:
                text, markup = MSG_TASK_NOT_FOUND, _tasks_back_keyboard()
            elif not isinstance(chat_id, int):
                text, markup = MSG_ERROR, _tasks_back_keyboard()
            else:
                # Arm ONE pending edit for this chat (a later field
                # choice replaces an abandoned one).  NO mutation —
                # the confirm callback applies it after re-reading.
                _PENDING_TASK_EDITS[chat_id] = {
                    "task_id": task_id,
                    "field": field,
                    "admin": actor,
                }
                text = build_task_prompt_text(task, field)
                markup = build_task_prompt_keyboard(task_id)
        elif op.startswith(f"{OP_TASKS}:confirm:"):
            view = "tasks:result"
            parts = op.split(":")  # tasks / confirm / <kind> / <id>
            kind, task_id = parts[2], int(parts[3])
            text, markup, toast = _apply_task_confirm(
                task_id, kind, chat_id, actor
            )
        elif op.startswith(f"{OP_TASKS}:cancel:"):
            view = "tasks:detail"
            task_id = int(op.rsplit(":", 1)[1])
            if isinstance(chat_id, int):
                _PENDING_TASK_EDITS.pop(chat_id, None)
            task = db.get_task(task_id)
            text = build_task_detail_text(task)
            markup = build_task_detail_keyboard(task_id, task)
            toast = TOAST_CANCELLED
        else:
            # Defense in depth — the parser already rejects this.
            await _safe_answer(query, MSG_INVALID)
            return
    except Exception:
        logger.exception(
            "Control tasks view failed: admin=%d view=%s", actor, view
        )
        await _safe_answer(query, MSG_ERROR)
        return
    await _edit_view_or_skip(query, text, markup, actor, view, toast)
    logger.info("Control tasks view: admin=%d view=%s", actor, view)


async def task_edit_text_input(update, context) -> None:
    """Pending task-edit text (MT-ADMIN-36).

    Registered in bot.py in its OWN handler group — the same
    catch-all pattern as the wizard (group 0) and support (group 2).
    Completely SILENT unless THIS private chat holds a pending edit,
    so ordinary chat, the anti-bot flow, the wizard and support are
    untouched.  Authorization is re-checked before anything is
    validated or staged; NOTHING is mutated here — the text only
    arms the confirmation step, and the confirm callback re-reads
    the task and calls the existing ``db.update_task`` contract.
    """
    message = getattr(update, "message", None)
    if message is None:
        return
    if _non_private_chat(update):
        return
    actor = _actor_id(
        getattr(getattr(update, "effective_user", None), "id", None)
    )
    if actor is None:
        return
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if not isinstance(chat_id, int):
        return
    pending = _PENDING_TASK_EDITS.get(chat_id)
    if not pending:
        return  # not our state — stay silent like the other catch-alls
    if not is_admin(actor):
        return  # never validate or arm a mutation without auth
    field = pending.get("field")
    raw = message.text or ""
    try:
        value = (
            task_taxonomy.validate_title(raw)
            if field == "title"
            else task_taxonomy.validate_instructions(raw)
        )
    except ValueError as exc:
        # The EXISTING validator's Arabic message; the pending state
        # is KEPT so the admin can simply retry with a valid text.
        await message.reply_text(str(exc))
        return
    task_id = pending.get("task_id")
    task = db.get_task(task_id) if isinstance(task_id, int) else None
    if task is None:
        _PENDING_TASK_EDITS.pop(chat_id, None)
        await message.reply_text(MSG_TASK_NOT_FOUND)
        return
    pending["value"] = value  # arms the single-use confirmation
    label = f"تعديل {_TASK_EDIT_FIELDS.get(field, NA)}"
    await message.reply_text(
        build_task_confirm_text(task, label, value=value),
        reply_markup=build_task_confirm_keyboard("edit", task_id),
    )
    logger.info(
        "Task edit staged: admin=%d task=%d field=%s",
        actor, task_id, field,
    )
