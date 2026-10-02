"""
Telegram User Withdrawal Flow
=============================

Private-chat entry point that connects Telegram users to the existing
``WithdrawalService`` — the authoritative business layer.  This module
is transport ONLY: it never opens a transaction, never touches the
wallet or the ledger, and duplicates no minimum/fee/cooldown/accounting
rule; every mutation is one ``WithdrawalService.create`` call.

Commands (stateless pipe-form, same convention as ``/addpm``)::

    /withdraw
        Inspect eligibility: available balance, pending-request status,
        the ACTIVE payout methods (safe metadata only) and usage.

    /withdraw <amount> <payment_method_id> <destination>
        Submit a withdrawal.  ``<amount>`` is exact decimal TEXT in the
        method's own unit (EGP for cash, USDT for BEP-20);
        ``<payment_method_id>`` is the id shown by ``/withdraw``;
        ``<destination>`` is the user's own payout destination (rest
        of the line).

Identity & authorization:

- identity comes ONLY from ``update.effective_user`` — no user id is
  ever parsed from the command text;
- private chat only: group/channel invocations are silent (the
  repository's isolation convention);
- the payout rail is derived SERVER-SIDE from the trusted stored
  payment-method row via the shared
  ``withdrawal_routes.withdrawal_method_for`` mapping — the user can
  never supply a ``method`` of their own.

State machine (unchanged, service-owned)::

    create (ONE db.transaction: validate → settings+rate+pm facts →
            cooldown → wallet reserve → ledger hold → insert → COMMIT)
        → pending  ── admin complete → completed (terminal)
                   ── admin reject   → rejected  (terminal)

After a successful create the module schedules the best-effort admin
notice through ``withdrawal_notifications`` — strictly post-commit, a
notification failure never affects the committed withdrawal.

Security: replies and logs carry safe facts only (amounts, fees,
method display name, short id) — never platform payout coordinates,
never credentials, never internal exception details.
"""

import logging
from datetime import datetime, timezone

import payment_method_store
import platform_settings
import rate_quote
import rate_store
import wallet
import withdrawal_notifications
import withdrawal_rules
import withdrawal_service
from withdrawal_routes import withdrawal_method_for
from withdrawal_store import SqliteWithdrawalRepository

logger = logging.getLogger(__name__)

# ── Arabic user-facing messages (safe facts only, no internals) ───────

MSG_USAGE = (
    "💸 طلب السحب\n\n"
    "عرض الرصيد ووسائل السحب:\n"
    "/withdraw\n\n"
    "إنشاء طلب سحب:\n"
    "/withdraw <المبلغ> <رقم الوسيلة> <جهة الاستلام>\n\n"
    "مثال:\n"
    "/withdraw 10 3 +201001234567"
)
MSG_INVALID_FORM = (
    "⛔ صيغة الطلب غير صالح، استخدم /withdraw لعرض التعليمات."
)
MSG_PM_NOT_FOUND = "⛔ وسيلة الدفع غير موجودة."
MSG_PM_UNAVAILABLE = "⛔ وسيلة الدفع غير متاحة حالياً."
MSG_UNSUPPORTED_METHOD = "⛔ طريقة السحب غير مدعومة لهذه الوسيلة."
MSG_RATE_UNAVAILABLE = "⛔ سعر الصرف غير متاح حالياً، حاول لاحقاً."
MSG_SETTINGS_MISSING = "⛔ إعدادات السحب غير مكتملة، تواصل مع الإدارة."
MSG_COOLDOWN = "⛔ تم إنشاء طلب سحب خلال آخر 24 ساعة، حاول لاحقاً."
MSG_PENDING_EXISTS = "⛔ لديك طلب سحب قيد المراجعة بالفعل."
MSG_INSUFFICIENT_BALANCE = "⛔ رصيدك لا يكفي لإتمام هذا السحب."
MSG_BELOW_MINIMUM = "⛔ المبلغ أقل من الحد الأدنى للسحب."
MSG_INVALID_AMOUNT = "⛔ المبلغ غير صالح."
MSG_INVALID_REQUEST = "⛔ الطلب غير صالح."
MSG_ERROR = "⛔ حدث خطأ غير متوقع، حاول مرة أخرى."


# ── Small transport helpers (repo convention: per-module copies) ──────


def _non_private_chat(update) -> bool:
    """True only when *update* positively targets a group/channel.

    MT-ADMIN-02 isolation semantics, inlined (no import cycle with
    bot.py): unknown chat types are NOT treated as groups.
    """
    chat = getattr(update, "effective_chat", None)
    chat_type = getattr(chat, "type", None)
    return isinstance(chat_type, str) and chat_type != "private"


def _command_body(message) -> str:
    """Everything after the command word
    (``/withdraw 10 3 dest`` → ``10 3 dest``)."""
    text = getattr(message, "text", None)
    if not isinstance(text, str):
        return ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _units_text(units) -> str:
    """Exact wallet units (int) → 8-decimal USDT text (no floats)."""
    value = int(units)
    whole, frac = divmod(value, wallet.USDT_SCALE)
    return f"{whole}.{frac:08d}"


def _short(request_id: str) -> str:
    return str(request_id)[:8]


# ── Eligibility info (reads only) ─────────────────────────────────────


def build_info_text(user_id: int) -> str:
    """Balance + pending status + ACTIVE payout methods + usage.

    Read-only (``wallet.balance_of`` / repository ``latest_for`` /
    ``payment_method_store.list_payment_methods``); never exposes the
    platform's payout coordinates or any internal column.
    """
    lines = ["💳 معلومات السحب", ""]

    balance = wallet.balance_of(user_id)
    lines.append(f"الرصيد المتاح: {balance:.8f} USDT")

    latest = SqliteWithdrawalRepository().latest_for(user_id)
    if latest is not None and latest.status is withdrawal_rules.RequestStatus.PENDING:
        lines.append("⏳ لديك طلب سحب قيد المراجعة بالفعل.")

    methods = payment_method_store.list_payment_methods(active_only=True)
    usable = [
        pm
        for pm in methods
        if withdrawal_method_for(pm) is not None
    ]
    if not usable:
        lines.append("")
        lines.append("📭 لا توجد وسائل سحب متاحة حالياً.")
    else:
        lines.append("")
        lines.append("🏦 وسائل السحب المتاحة:")
        for pm in usable:
            lines.append(
                f"{pm.id}. {pm.display_name} ({pm.asset} — {pm.category})"
            )

    lines.append("")
    lines.append(
        "للطلب: /withdraw <المبلغ> <رقم الوسيلة> <جهة الاستلام>"
    )
    return "\n".join(lines)


def build_created_text(request) -> str:
    """Confirmation for a freshly created PENDING request (safe facts)."""
    unit = request.native_unit
    return (
        "✅ تم إنشاء طلب السحب\n"
        f"رقم الطلب: #{_short(request.request_id)}\n"
        f"المبلغ: {request.amount_native} {unit}\n"
        f"الرسوم: {request.fee_native} {unit}\n"
        f"المحجوز من الرصيد: {_units_text(request.wallet_debit_units)} USDT\n"
        "الحالة: ⏳ قيد المراجعة — سيتم إشعارك عند المراجعة."
    )


# ── Domain error → safe Arabic text (mirrors the Mini App mapping) ────


def _error_text(exc: Exception) -> str | None:
    """Translate a withdrawal-domain error to a safe reply.

    Returns ``None`` only for genuinely unexpected exceptions (the
    caller logs those and replies with the generic error).
    Subclasses are checked before their bases.
    """
    if isinstance(
        exc, (withdrawal_rules.InvalidRateError, rate_quote.RateQuoteError)
    ):
        # Missing/stale/invalid persisted rate — never used silently.
        return MSG_RATE_UNAVAILABLE
    if isinstance(exc, platform_settings.SettingNotFoundError):
        return MSG_SETTINGS_MISSING
    if isinstance(exc, withdrawal_rules.CooldownError):
        return MSG_COOLDOWN
    if isinstance(exc, withdrawal_rules.PendingWithdrawalExistsError):
        return MSG_PENDING_EXISTS
    if isinstance(exc, withdrawal_rules.InsufficientBalanceError):
        return MSG_INSUFFICIENT_BALANCE
    if isinstance(exc, withdrawal_rules.PaymentMethodUnavailableError):
        return MSG_PM_UNAVAILABLE
    if isinstance(exc, withdrawal_rules.InvalidAmountError):
        lowered = str(exc).lower()
        if "below the minimum" in lowered:
            return MSG_BELOW_MINIMUM
        return MSG_INVALID_AMOUNT
    if isinstance(exc, withdrawal_rules.ValidationError):
        return MSG_INVALID_REQUEST
    return None


# ── The command ───────────────────────────────────────────────────────


async def withdraw_command(update, context) -> None:
    """``/withdraw`` — eligibility info, or a pipe-form submission.

    Transport only: parse → trusted lookup → ONE service call →
    reply.  All financial validation and mutation belong to
    ``WithdrawalService``.
    """
    if _non_private_chat(update):
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return

    body = _command_body(message)
    if not body:
        await message.reply_text(build_info_text(user_id))
        return

    # ── transport validation ONLY (shape/type of the pipe form) ──────
    parts = body.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply_text(MSG_INVALID_FORM)
        return
    amount_text, pm_id_text, destination = parts[0], parts[1], parts[2]
    destination = destination.strip()
    if not destination:
        await message.reply_text(MSG_INVALID_FORM)
        return
    if not pm_id_text.isdigit():
        await message.reply_text(MSG_INVALID_FORM)
        return
    payment_method_id = int(pm_id_text)

    # ── trusted stored row decides the payout rail (server-side) ─────
    try:
        pm = payment_method_store.get_active_payment_method(
            payment_method_id
        )
    except payment_method_store.PaymentMethodNotFoundError:
        await message.reply_text(MSG_PM_NOT_FOUND)
        return
    except payment_method_store.PaymentMethodInactiveError:
        await message.reply_text(MSG_PM_UNAVAILABLE)
        return
    except payment_method_store.PaymentMethodValidationError:
        await message.reply_text(MSG_INVALID_FORM)
        return
    except Exception:
        logger.exception(
            "Withdrawal method lookup failed: user=%d pm=%s",
            user_id,
            payment_method_id,
        )
        await message.reply_text(MSG_ERROR)
        return

    method = withdrawal_method_for(pm)
    if method is None:
        await message.reply_text(MSG_UNSUPPORTED_METHOD)
        return

    # ── the service owns the ONE financial transaction; the quote
    #    loader reads rate_store on that transaction's connection ─────
    service = withdrawal_service.WithdrawalService(
        quote_loader=rate_store.get_current_quote,
    )
    try:
        created = service.create(
            user_id,
            method,
            amount_text,
            payment_method_id=payment_method_id,
            user_destination=destination,
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:
        text = _error_text(exc)
        if text is None:
            logger.exception(
                "Withdrawal create failed unexpectedly: user=%d pm=%s",
                user_id,
                payment_method_id,
            )
            await message.reply_text(MSG_ERROR)
            return
        logger.info(
            "Withdrawal rejected: user=%d pm=%d — %s",
            user_id,
            payment_method_id,
            exc,
        )
        await message.reply_text(text)
        return

    # Post-commit side effect only — never raises, never re-runs the
    # committed financial transaction.
    withdrawal_notifications.notify_submission(created)
    await message.reply_text(build_created_text(created))
