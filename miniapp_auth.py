"""
Telegram Mini App Authentication Backend
==========================================

Provides an HTTP endpoint that validates Telegram Mini App initData
and returns a verified user identity.

Validation follows the official Telegram Bot API specification:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Security checks:
- HMAC-SHA256 hash verification using bot token as secret
- auth_date freshness check (configurable max age, default 24 hours)
- No trust in frontend-sent user_id alone — identity extracted from verified data
"""

import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qs, unquote

from flask import Flask, jsonify, request

import db

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Maximum age of auth_date in seconds (default: 24 hours)
MAX_AUTH_AGE = int(os.environ.get("MINIAPP_AUTH_MAX_AGE", 86400))


def _get_bot_token() -> str:
    """Retrieve the bot token from environment."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")
    return token


def _compute_secret_key(bot_token: str) -> bytes:
    """Compute the secret key: SHA256(bot_token)."""
    return hashlib.sha256(bot_token.encode("utf-8")).digest()


def _parse_init_data(init_data: str) -> dict[str, list[str]]:
    """
    Parse Telegram Mini App initData query string.

    The initData is a URL-encoded query string like:
        user=%7B%22id%22%3A123...%7D&auth_date=1234567890&hash=abc...

    Returns a dict of key -> list of values (from parse_qs).
    """
    parsed = parse_qs(init_data, keep_blank_values=True)
    return parsed


def _extract_flat_params(parsed: dict[str, list[str]]) -> dict[str, str]:
    """Flatten parse_qs output (take first value for each key)."""
    return {k: v[0] for k, v in parsed.items()}


def validate_init_data(
    init_data: str,
    bot_token: str,
    max_age: int = MAX_AUTH_AGE,
) -> tuple[bool, dict | None, str | None]:
    """
    Validate Telegram Mini App initData.

    Args:
        init_data: The raw initData query string from the Mini App.
        bot_token: The bot token for HMAC verification.
        max_age: Maximum allowed age of auth_date in seconds.

    Returns:
        (is_valid, user_data_or_none, error_message_or_none)
    """
    if not init_data or not init_data.strip():
        return False, None, "Missing initData"

    try:
        parsed = _parse_init_data(init_data)
    except Exception:
        return False, None, "Malformed initData"

    flat = _extract_flat_params(parsed)

    # Extract and remove hash
    provided_hash = flat.pop("hash", None)
    if not provided_hash:
        return False, None, "Missing hash parameter"

    # Check auth_date freshness
    auth_date_str = flat.get("auth_date")
    if not auth_date_str:
        return False, None, "Missing auth_date parameter"

    try:
        auth_date = int(auth_date_str)
    except (ValueError, TypeError):
        return False, None, "Invalid auth_date format"

    current_time = int(time.time())
    if current_time - auth_date > max_age:
        return False, None, "auth_date expired"

    # Build the data-check-string
    # Sort by key, join key=value with newlines
    sorted_params = sorted(flat.items())
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_params)

    # Compute HMAC-SHA256
    secret_key = _compute_secret_key(bot_token)
    computed_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison
    if not hmac.compare_digest(computed_hash, provided_hash):
        return False, None, "Invalid hash"

    # Extract user identity from the verified data
    user_json = flat.get("user")
    if not user_json:
        return False, None, "Missing user parameter"

    try:
        user_data = json.loads(unquote(user_json))
    except (json.JSONDecodeError, TypeError):
        return False, None, "Invalid user JSON"

    # Validate required user fields
    user_id = user_data.get("id")
    if not user_id:
        return False, None, "Missing user id"

    return True, {
        "user_id": int(user_id),
        "username": user_data.get("username"),
        "first_name": user_data.get("first_name"),
        "last_name": user_data.get("last_name"),
    }, None


@app.route("/api/auth", methods=["POST"])
def authenticate():
    """
    POST /api/auth

    Validates Telegram Mini App initData and returns verified user identity.

    Request body (JSON):
        { "initData": "<telegram_init_data_string>" }

    Success response (200):
        {
            "ok": true,
            "user": {
                "user_id": 12345678,
                "username": "john_doe",
                "first_name": "John",
                "registered": true
            }
        }

    Error responses:
        400 - Invalid or missing initData
        500 - Internal server error
    """
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"ok": False, "error": "Invalid request body"}), 400

    init_data = body.get("initData")
    if not init_data:
        return jsonify({"ok": False, "error": "Missing initData"}), 400

    try:
        bot_token = _get_bot_token()
    except RuntimeError:
        logger.error("TELEGRAM_BOT_TOKEN not configured")
        return jsonify({"ok": False, "error": "Server configuration error"}), 500

    is_valid, user_data, error = validate_init_data(init_data, bot_token)

    if not is_valid:
        logger.info("Auth rejected: %s", error)
        return jsonify({"ok": False, "error": error}), 400

    # Look up or register the user in the existing users table
    user_id = user_data["user_id"]
    existing_user = db.get_user(user_id)
    registered = existing_user is not None

    if not registered:
        # Register the new user (no referrer from Mini App auth)
        db.register_user(
            user_id=user_id,
            username=user_data.get("username"),
            first_name=user_data.get("first_name"),
            referred_by=None,
        )

    return jsonify({
        "ok": True,
        "user": {
            "user_id": user_id,
            "username": user_data.get("username"),
            "first_name": user_data.get("first_name"),
            "registered": registered,
        },
    }), 200


def create_app(bot_token: str | None = None) -> Flask:
    """
    Application factory for testing.

    When bot_token is provided, it is injected into the environment
    so validate_init_data can access it without reading os.environ.
    """
    if bot_token:
        os.environ["TELEGRAM_BOT_TOKEN"] = bot_token
    return app
