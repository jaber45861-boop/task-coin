"""
Mini App Withdrawal API (MT-ADMIN-25)
=====================================

Production HTTP wiring between the Wallet page of the Mini App and the
existing ``WithdrawalService``.  Registered on both existing Mini App
servers (``serve_miniapp.py`` and the WispByte single-entry ``bot.py``
app) — no second web server is created.

Endpoints (all under ``/api/withdrawal``):

- ``GET  /api/withdrawal/methods``   active payout methods (safe
                                     user-facing metadata only)
- ``POST /api/withdrawal``           create a PENDING withdrawal
- ``GET  /api/withdrawal/requests``  the authenticated user's own
                                     withdrawal status/list (safe
                                     user-facing facts only)

Authoritative creation flow::

    Telegram Mini App initData auth (existing miniapp_auth)
    → transport validation ONLY (shapes/types — no financial policy)
    → WithdrawalService(quote_loader=rate_store.get_current_quote)
    → ONE service-owned transaction: authoritative quote on the same
      connection → payment method → minimum/fee facts → wallet reserve
      → ledger hold → withdrawal insert

Transaction ownership (MT-ADMIN-25 critical rule): the HTTP layer
opens NO transaction and never mutates wallet/ledger/withdrawal rows
itself.  The service owns the single atomic financial transaction, and
the current manual rate (MT-ADMIN-26) is loaded INSIDE it via
``rate_store.get_current_quote(connection=...)`` — so the quote used
for the financial facts is exactly the persisted authoritative one,
with no quote/transaction race and no second transaction.

Rate rules: the quote comes ONLY from ``rate_store`` server-side —
never from the client, never hardcoded, never from
``platform_settings``.  A missing/stale/invalid rate fails the request
(no stale quote is ever used).

Security rules enforced here:

- identity comes only from cryptographically verified initData — a
  browser-supplied ``user_id`` (body, query or header) is ignored
- responses expose safe user-facing facts only: never
  ``payment_methods.pm_destination`` (the platform's payout
  coordinates), never ``created_by``/``updated_by``, never DB
  internals or secrets
- only ACTIVE methods are listed; only an ACTIVE method can back a
  withdrawal (the service re-checks it inside its transaction)
- errors are stable machine codes with concise Arabic messages;
  internal exception details never reach the client
"""

import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

import db
import miniapp_auth
import payment_method_store
import platform_settings
import rate_quote
import rate_store
import withdrawal_notifications
import withdrawal_rules
import withdrawal_service
import withdrawal_store
from withdrawal_rules import (
    METHOD_USDT_BEP20,
    METHOD_VODAFONE_CASH,
)

logger = logging.getLogger(__name__)

withdrawal_bp = Blueprint("withdrawal", __name__)

# Telegram initData carrier for XHR calls (kept out of URLs/logs).
INIT_DATA_HEADER = "X-Telegram-Init-Data"
# Also accepted as a query parameter, mirroring the task/social routes.
INIT_DATA_QUERY = "init_data"

# ── Arabic user-facing messages (concise, no internal details) ────────

_MSG_UNAUTHENTICATED = "افتح التطبيق من تيليجرام أولاً"
_MSG_SERVER = "حدث خطأ غير متوقع، حاول مرة أخرى"
_MSG_INVALID_REQUEST = "الطلب غير صالح"
_MSG_INVALID_AMOUNT = "المبلغ غير صالح"
_MSG_UNSUPPORTED_METHOD = "طريقة السحب غير مدعومة لهذه الوسيلة"
_MSG_PM_NOT_FOUND = "وسيلة الدفع غير موجودة"
_MSG_PM_UNAVAILABLE = "وسيلة الدفع غير متاحة حالياً"
_MSG_RATE_UNAVAILABLE = "سعر الصرف غير متاح حالياً، حاول لاحقاً"
_MSG_SETTINGS_MISSING = "إعدادات السحب غير مكتملة، تواصل مع الإدارة"
_MSG_COOLDOWN = "تم إنشاء طلب سحب خلال آخر 24 ساعة، حاول لاحقاً"
_MSG_PENDING_EXISTS = "لديك طلب سحب قيد المراجعة بالفعل"
_MSG_INSUFFICIENT_BALANCE = "رصيدك لا يكفي لإتمام هذا السحب"
_MSG_BELOW_MINIMUM = "المبلغ أقل من الحد الأدنى للسحب"
_MSG_CREATED = "تم إنشاء طلب السحب بنجاح"


# ── Authentication (existing miniapp_auth initData validation) ────────


def _get_init_data() -> str | None:
    value = request.headers.get(INIT_DATA_HEADER)
    if value:
        return value
    value = request.args.get(INIT_DATA_QUERY)
    if value:
        return value
    return None


def _authenticate() -> dict | None:
    """Verify the Telegram Mini App user; ``None`` when untrusted.

    Uses the existing ``miniapp_auth`` HMAC validation — the caller's
    identity comes only from cryptographically verified initData, so a
    browser-supplied ``user_id`` anywhere in the request is ignored.
    """
    init_data = _get_init_data()
    if not init_data:
        return None
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not configured")
        return None
    is_valid, user, _error = miniapp_auth.validate_init_data(
        init_data, bot_token
    )
    if not is_valid or not user:
        logger.info("Withdrawal endpoint rejected unauthenticated request")
        return None
    return user


def _ensure_user(user: dict) -> int:
    """Make sure the verified Telegram user exists as a users row."""
    user_id = int(user["user_id"])
    if db.get_user(user_id) is None:
        db.register_user(
            user_id=user_id,
            username=user.get("username"),
            first_name=user.get("first_name"),
            referred_by=None,
        )
    return user_id


# ── Response helpers ──────────────────────────────────────────────────


def _error(code: str, message: str, http_status: int, **extra):
    payload = {"ok": False, "error": code, "message": message}
    payload.update(extra)
    return jsonify(payload), http_status


def _unauthenticated():
    return _error("unauthenticated", _MSG_UNAUTHENTICATED, 401)


def _server_error():
    return _error("server_error", _MSG_SERVER, 500)


def _decimal_text(value) -> str:
    """Exact plain text for a Decimal — never a float round-trip."""
    return str(value)


# ── Safe serialization ────────────────────────────────────────────────

# The ONLY fields a user-facing method list may expose.  The platform's
# payout coordinates (destination) and every audit/admin column are
# deliberately absent — there is no code path that can emit them.
_SAFE_METHOD_FIELDS = (
    "id",
    "category",
    "display_name",
    "asset",
    "network",
    "provider",
    "instructions",
)


def _safe_method_entry(pm: payment_method_store.PaymentMethod) -> dict:
    return {name: getattr(pm, name) for name in _SAFE_METHOD_FIELDS}


def _request_payload(request_row) -> dict:
    """Safe user-facing facts for one created withdrawal.

    Never includes ``pm_destination`` (platform payout coordinates),
    the user's own destination echo, or any admin/audit column.
    """
    return {
        "request_id": request_row.request_id,
        "method": request_row.method,
        "status": request_row.status.value,
        "amount": _decimal_text(request_row.amount_native),
        "fee": _decimal_text(request_row.fee_native),
        "native_unit": request_row.native_unit,
        "wallet_debit_units": int(request_row.wallet_debit_units),
        "created_at": request_row.created_at.isoformat(),
        "payment_method_id": request_row.payment_method_id,
        "display_name": request_row.pm_display_name,
        "category": request_row.pm_category,
        "asset": request_row.pm_asset,
        "network": request_row.pm_network,
        "provider": request_row.pm_provider,
        # The quote facts PINNED at creation (the exact authoritative
        # rate the financial math used — read from rate_store inside
        # the service's transaction, never from the client).
        "rate": {
            "usdt_egp": request_row.wallet_rate_usdt_egp,
            "provider": request_row.rate_provider,
            "captured_at": request_row.rate_captured_at,
        },
    }


# ── Payment-method → withdrawal-method mapping (transport level) ─────


def withdrawal_method_for(pm: payment_method_store.PaymentMethod) -> str | None:
    """Map an ACTIVE method row to the closed withdrawal-method set.

    The ONE payout-rail routing decision, shared by BOTH transports
    (this Mini App route and the Telegram ``/withdraw`` command):
    category/asset decide the rail — a user can never submit a
    ``method`` value of their own; the server derives it from the
    trusted stored row.  ``None`` = this method cannot back a
    withdrawal today.
    """
    if pm.category == "cash":
        return METHOD_VODAFONE_CASH
    if pm.category == "crypto" and str(pm.asset).upper() == "USDT":
        return METHOD_USDT_BEP20
    return None


# ── Domain-error mapping (stable codes → Arabic messages) ────────────


def _create_error(exc: Exception):
    """Translate a withdrawal-domain error into a safe API error.

    Returns ``None`` only for genuinely unexpected exceptions (the
    caller falls back to the generic server error).  Subclasses are
    checked before their bases (InvalidRateError/InvalidAmountError
    are ValidationErrors).
    """
    if isinstance(exc, (withdrawal_rules.InvalidRateError,
                        rate_quote.RateQuoteError)):
        # Missing/stale/invalid persisted rate — no stale quote is
        # ever used, so the request simply cannot be created yet.
        return _error("rate_unavailable", _MSG_RATE_UNAVAILABLE, 503)
    if isinstance(exc, platform_settings.SettingNotFoundError):
        return _error("withdrawal_settings_missing", _MSG_SETTINGS_MISSING, 503)
    if isinstance(exc, withdrawal_rules.CooldownError):
        retry = getattr(exc, "retry_after_seconds", None)
        extra = {}
        if isinstance(retry, int) and not isinstance(retry, bool) and retry >= 0:
            extra["retry_after_seconds"] = retry
        return _error("cooldown", _MSG_COOLDOWN, 409, **extra)
    if isinstance(exc, withdrawal_rules.PendingWithdrawalExistsError):
        return _error("pending_exists", _MSG_PENDING_EXISTS, 409)
    if isinstance(exc, withdrawal_rules.InsufficientBalanceError):
        return _error("insufficient_balance", _MSG_INSUFFICIENT_BALANCE, 409)
    if isinstance(exc, withdrawal_rules.PaymentMethodUnavailableError):
        return _error("payment_method_unavailable", _MSG_PM_UNAVAILABLE, 409)
    if isinstance(exc, withdrawal_rules.InvalidAmountError):
        message = _MSG_INVALID_AMOUNT
        lowered = str(exc).lower()
        if "below the minimum" in lowered:
            message = _MSG_BELOW_MINIMUM
        return _error("invalid_amount", message, 400)
    if isinstance(exc, withdrawal_rules.ValidationError):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)
    return None


# ── GET /api/withdrawal/methods — active payout methods ──────────────


@withdrawal_bp.get("/api/withdrawal/methods")
def list_withdrawal_methods():
    """Active payout methods with safe user-facing metadata only.

    Fields: id, category, display_name, asset, network, provider,
    instructions.  NEVER the platform's ``destination``, never admin
    audit columns, never anything client-controlled.  Inactive methods
    are excluded entirely.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()

    try:
        methods = payment_method_store.list_payment_methods(active_only=True)
    except Exception:
        logger.exception("Failed to list withdrawal methods")
        return _server_error()

    return jsonify(
        {
            "ok": True,
            "methods": [_safe_method_entry(m) for m in methods],
        }
    ), 200


# ── POST /api/withdrawal — create through WithdrawalService ──────────


@withdrawal_bp.post("/api/withdrawal")
def create_withdrawal():
    """Create a PENDING withdrawal — transport → service, one transaction.

    The body carries ONLY user-controlled request facts
    (``payment_method_id``, ``amount``, ``user_destination``); a
    ``user_id`` if present is ignored — identity comes from initData.
    The server derives the payout method from the trusted stored
    payment-method row and the authoritative rate from ``rate_store``
    inside the service's own transaction.  This handler opens NO
    transaction and never mutates any row directly.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    # ── transport validation only (types/shapes, no finance policy) ──
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)

    for key in ("payment_method_id", "amount", "user_destination"):
        if key not in body:
            return _error("invalid_request", _MSG_INVALID_REQUEST, 400)

    payment_method_id = body["payment_method_id"]
    if (
        isinstance(payment_method_id, bool)
        or not isinstance(payment_method_id, int)
        or payment_method_id <= 0
    ):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)

    amount = body["amount"]
    # JSON numbers arrive as Python floats — forbidden in financial
    # math, so the amount must be exact decimal TEXT (or an int).
    if isinstance(amount, bool) or not isinstance(amount, (str, int)):
        return _error("invalid_amount", _MSG_INVALID_AMOUNT, 400)

    user_destination = body["user_destination"]
    if not isinstance(user_destination, str) or not user_destination.strip():
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)

    # ── trusted stored row decides the payout rail ────────────────────
    try:
        pm = payment_method_store.get_active_payment_method(payment_method_id)
    except payment_method_store.PaymentMethodNotFoundError:
        return _error("payment_method_not_found", _MSG_PM_NOT_FOUND, 404)
    except payment_method_store.PaymentMethodInactiveError:
        return _error(
            "payment_method_unavailable", _MSG_PM_UNAVAILABLE, 409
        )
    except payment_method_store.PaymentMethodValidationError:
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)
    except Exception:
        logger.exception("Payment method lookup failed")
        return _server_error()

    method = withdrawal_method_for(pm)
    if method is None:
        return _error("unsupported_method", _MSG_UNSUPPORTED_METHOD, 400)

    # ── the service owns the ONE financial transaction; the quote
    #    loader reads rate_store on that transaction's connection ─────
    service = withdrawal_service.WithdrawalService(
        quote_loader=rate_store.get_current_quote,
    )
    try:
        created = service.create(
            user_id,
            method,
            amount,
            payment_method_id=payment_method_id,
            user_destination=user_destination,
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:
        api_error = _create_error(exc)
        if api_error is not None:
            logger.info(
                "Withdrawal rejected: user=%s method=%s — %s",
                user_id, method, exc,
            )
            return api_error
        logger.exception(
            "Withdrawal failed unexpectedly: user=%s method=%s",
            user_id,
            method,
        )
        return _server_error()

    # Post-commit side effect: best-effort admin notice for the new
    # pending request.  Never raises — a notification failure must not
    # affect the already-committed financial transaction.
    withdrawal_notifications.notify_submission(created)

    return jsonify(
        {
            "ok": True,
            "message": _MSG_CREATED,
            "request": _request_payload(created),
        }
    ), 200


@withdrawal_bp.get("/api/withdrawal/requests")
def list_withdrawal_requests():
    """The AUTHENTICATED user's own withdrawal status/list.

    Transport only: initData identity (a client-supplied ``user_id``
    query parameter is ignored) → bounded read through the repository
    → the same safe ``_request_payload`` facts used by create.  Opens
    no transaction and mutates nothing.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    # Optional bound: transport validation only.
    limit_raw = request.args.get("limit", "10")
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)
    if isinstance(limit, bool) or not (1 <= limit <= 20):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)

    rows = withdrawal_store.SqliteWithdrawalRepository().list_for_user(
        user_id, limit=limit
    )
    return jsonify(
        {
            "ok": True,
            "requests": [_request_payload(row) for row in rows],
        }
    ), 200
