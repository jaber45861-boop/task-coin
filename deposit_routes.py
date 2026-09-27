"""
Mini App Deposit API (MT-ADMIN-28)
==================================

Production HTTP wiring between the Wallet page of the Mini App and the
persisted deposit-intent model.  Registered on both existing Mini App
servers (``serve_miniapp.py`` and the WispByte single-entry ``bot.py``
app) — no second web server is created.

Endpoints (all under ``/api/deposit``):

- ``GET  /api/deposit/methods``  methods EXPLICITLY configured as
                                  active user-deposit methods (safe
                                  user-facing fields; the platform's
                                  deposit destination is intentionally
                                  included — it is where the user must
                                  SEND funds)
- ``POST /api/deposit``          persist one PENDING deposit intent

Architecture boundary (critical):

    Telegram Mini App initData auth (existing miniapp_auth)
    → transport validation ONLY (shapes/types — no invented policy)
    → deposit_store.create_deposit_request (ONE db.transaction:
      active deposit method → minimum_deposit_units (exact integer
      units) → INSERT snapshot)

The HTTP layer opens NO transaction and NEVER touches wallet/ledger.
There is no authoritative deposit verification source in this
repository, so a created request is always ``pending`` (unverified):
no credit, no ledger entry, no rate, no conversion, no blockchain
interaction.  Future crediting belongs to a dedicated verifier with
idempotency via ``external_tx_id`` + the ledger's reference uniqueness.

Security rules enforced here:

- identity comes only from cryptographically verified initData — a
  browser-supplied ``user_id`` (body, query or header) is ignored
- the method id resolves SERVER-SIDE to an ACTIVE, deposit-enabled
  stored row; asset/network/provider/destination always come from that
  row, never from the client
- responses expose safe user-facing fields only: never
  ``created_by``/``updated_by``, never ``sort_order``, never
  ``is_active``/``deposits_enabled``, never admin/audit columns or
  DB internals; there is no ``user_destination`` concept for deposits
- errors are stable machine codes with concise Arabic messages;
  internal exception details never reach the client
"""

import logging
import os

from flask import Blueprint, jsonify, request

import db
import deposit_store
import miniapp_auth
import payment_method_store
import platform_settings
import wallet
from deposit_store import (
    DepositBelowMinimumError,
    DepositMethodUnavailableError,
    DepositValidationError,
)

logger = logging.getLogger(__name__)

deposit_bp = Blueprint("deposit", __name__)

# Telegram initData carrier for XHR calls (kept out of URLs/logs).
INIT_DATA_HEADER = "X-Telegram-Init-Data"
# Also accepted as a query parameter, mirroring the other routes.
INIT_DATA_QUERY = "init_data"

# ── Arabic user-facing messages (concise, no internal details) ────────

_MSG_UNAUTHENTICATED = "افتح التطبيق من تيليجرام أولاً"
_MSG_SERVER = "حدث خطأ غير متوقع، حاول مرة أخرى"
_MSG_INVALID_REQUEST = "الطلب غير صالح"
_MSG_INVALID_AMOUNT = "المبلغ غير صالح"
_MSG_BELOW_MINIMUM = "المبلغ أقل من الحد الأدنى للإيداع"
_MSG_PM_NOT_FOUND = "وسيلة الإيداع غير موجودة"
_MSG_PM_UNAVAILABLE = "وسيلة الدفع غير متاحة حالياً"
_MSG_NOT_DEPOSIT_METHOD = "هذه الوسيلة غير متاحة للإيداع حالياً"
_MSG_SETTINGS_MISSING = "إعدادات الإيداع غير مكتملة، تواصل مع الإدارة"
_MSG_CREATED = "تم إنشاء طلب الإيداع — بانتظار التحقق من الدفع"


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
        logger.info("Deposit endpoint rejected unauthenticated request")
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

# The ONLY fields a user-facing deposit method entry may expose.
# For deposits the platform's `destination` IS the point of the flow
# (the user sends funds there) — it is operational configuration, not
# a secret.  Admin/audit columns (created_by/updated_by/sort_order),
# the availability flags themselves and is_active are deliberately
# absent — there is no code path that can emit them.
_SAFE_METHOD_FIELDS = (
    "id",
    "display_name",
    "asset",
    "network",
    "provider",
    "destination",
    "instructions",
)


def _safe_method_entry(pm: payment_method_store.PaymentMethod) -> dict:
    return {name: getattr(pm, name) for name in _SAFE_METHOD_FIELDS}


def _request_payload(row: deposit_store.DepositRequest) -> dict:
    """Safe user-facing facts for one created deposit intent.

    Never includes ``pm_destination`` echo, any admin/audit column, or
    anything client-controlled.  ``status`` is always ``pending`` from
    this flow — no response ever claims funds were received.
    """
    return {
        "request_id": row.request_id,
        "status": row.status,
        "payment_method_id": row.payment_method_id,
        "amount_units": int(row.amount_units),
        "amount": _decimal_text(wallet.units_to_decimal(row.amount_units)),
        "display_name": row.pm_display_name,
        "asset": row.pm_asset,
        "network": row.pm_network,
        "provider": row.pm_provider,
        "created_at": row.created_at,
    }


# ── Domain-error mapping (stable codes → Arabic messages) ────────────


def _create_error(exc: Exception):
    """Translate a deposit-store error into a safe API error.

    Returns ``None`` only for genuinely unexpected exceptions (the
    caller falls back to the generic server error).  Subclasses are
    checked before their bases.
    """
    if isinstance(exc, DepositBelowMinimumError):
        return _error("below_minimum", _MSG_BELOW_MINIMUM, 400)
    if isinstance(exc, DepositMethodUnavailableError):
        return _error(
            "deposit_method_unavailable", _MSG_NOT_DEPOSIT_METHOD, 409
        )
    if isinstance(exc, payment_method_store.PaymentMethodNotFoundError):
        return _error("payment_method_not_found", _MSG_PM_NOT_FOUND, 404)
    if isinstance(exc, payment_method_store.PaymentMethodInactiveError):
        return _error(
            "payment_method_unavailable", _MSG_PM_UNAVAILABLE, 409
        )
    if isinstance(exc, DepositValidationError):
        return _error("invalid_amount", _MSG_INVALID_AMOUNT, 400)
    if isinstance(exc, payment_method_store.PaymentMethodValidationError):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)
    if isinstance(exc, platform_settings.SettingNotFoundError):
        return _error(
            "deposit_settings_missing", _MSG_SETTINGS_MISSING, 503
        )
    return None


# ── GET /api/deposit/methods — explicit active deposit methods ───────


@deposit_bp.get("/api/deposit/methods")
def list_deposit_methods():
    """Active, deposit-enabled methods with safe metadata only.

    A method appears ONLY when it is both active AND explicitly
    opted in as a deposit method (``deposits_enabled``) — an active
    withdrawal-only method never exposes its destination here.
    Inactive methods are excluded entirely.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()

    try:
        methods = [
            pm
            for pm in payment_method_store.list_payment_methods(
                active_only=True
            )
            if pm.deposits_enabled
        ]
    except Exception:
        logger.exception("Failed to list deposit methods")
        return _server_error()

    return jsonify(
        {
            "ok": True,
            "methods": [_safe_method_entry(m) for m in methods],
        }
    ), 200


# ── POST /api/deposit — persist a PENDING deposit intent ─────────────


@deposit_bp.post("/api/deposit")
def create_deposit():
    """Persist one PENDING deposit intent — no wallet, no ledger.

    The body carries ONLY user-controlled request facts
    (``payment_method_id``, ``amount``); a ``user_id`` if present is
    ignored — identity comes from initData.  The server resolves the
    method to the trusted stored row (active + deposit-enabled) and
    enforces the exact integer minimum setting inside the store's own
    transaction.  This handler opens NO transaction and never mutates
    any financial row.
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = _ensure_user(user)

    # ── transport validation only (types/shapes, no finance policy) ──
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("invalid_request", _MSG_INVALID_REQUEST, 400)

    for key in ("payment_method_id", "amount"):
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

    # ── the store owns the ONE transaction (method + minimum + row) ──
    try:
        created = deposit_store.create_deposit_request(
            user_id=user_id,
            payment_method_id=payment_method_id,
            amount=amount,
        )
    except Exception as exc:
        api_error = _create_error(exc)
        if api_error is not None:
            logger.info(
                "Deposit request rejected: user=%s method=%s — %s",
                user_id, payment_method_id, type(exc).__name__,
            )
            return api_error
        logger.exception(
            "Deposit request failed unexpectedly: user=%s method=%s",
            user_id, payment_method_id,
        )
        return _server_error()

    return jsonify(
        {
            "ok": True,
            "message": _MSG_CREATED,
            "request": _request_payload(created),
        }
    ), 200
