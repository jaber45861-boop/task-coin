"""
Social account linking endpoints (SA-YT-01)
===========================================

HTTP surface for linking a Telegram Mini App user's YouTube account
with Google OAuth 2.0.  Registered on the existing Mini App web server
(``serve_miniapp.py``) — no second web server is created.

Endpoints (all under ``/api/social``):

- ``GET  /youtube/connect``   authenticate the Telegram user, create a
  single-use bound OAuth state, send the browser to Google
- ``GET  /youtube/callback``  validate state, exchange the code, resolve
  the authenticated channel, persist the link, return to the Mini App
- ``GET  /accounts``          the caller's linked accounts (no tokens)
- ``POST /youtube/unlink``    remove the caller's own link only

Security rules enforced here:

- every protected endpoint authenticates the Telegram Mini App user via
  the existing ``miniapp_auth`` initData verification — a browser-
  supplied ``user_id`` is never trusted
- the OAuth ``state`` is the only channel that carries Telegram
  identity into the callback; it is single-use, expiring and bound
  server-side to the user who started the flow
- tokens never appear in responses, redirects or logs
- post-OAuth redirects only ever target the fixed, allowlisted Mini App
  destination built by ``_result_url`` (no user input is interpolated)
"""

import logging
import os

from flask import Blueprint, jsonify, redirect, request

import db
import miniapp_auth
import youtube_oauth
from social_accounts import (
    PROVIDER_YOUTUBE,
    DuplicateLinkError,
    SocialAccountConfigError,
    SocialAccountError,
    SocialAccountService,
    TokenCipher,
)

logger = logging.getLogger(__name__)

social_bp = Blueprint("social", __name__, url_prefix="/api/social")

# Telegram initData carrier for XHR calls (kept out of URLs/logs).
INIT_DATA_HEADER = "X-Telegram-Init-Data"
# Also accepted as a query parameter so a plain top-level navigation can
# be authenticated — it is still HMAC-verified, never trusted raw.
INIT_DATA_QUERY = "init_data"

# Fixed, allowlisted Mini App destinations after the OAuth round trip.
# Only these literal values are ever used — nothing user-controlled.
_RESULT_PATH = "/"


def _result_url(outcome: str) -> str:
    """Build the fixed internal return URL for an OAuth outcome."""
    return _RESULT_PATH + "#youtube=" + outcome


def _get_init_data() -> str | None:
    value = request.headers.get(INIT_DATA_HEADER)
    if value:
        return value
    value = request.args.get(INIT_DATA_QUERY)
    if value:
        return value
    return None


def _authenticate() -> dict | None:
    """Verify the Telegram Mini App user; returns ``None`` when untrusted.

    Uses the existing ``miniapp_auth`` HMAC validation — the caller's
    identity comes only from cryptographically verified initData.
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
        logger.info("Social endpoint rejected unauthenticated request")
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


def _unauthenticated():
    return jsonify({"ok": False, "error": "unauthenticated"}), 401


# ── Connect: start the Google OAuth flow ───────────────────────────────
@social_bp.get("/youtube/connect")
def youtube_connect():
    """Authenticate, create the bound state, go to Google.

    Responses:

    - authenticated via the ``X-Telegram-Init-Data`` header →
      ``200 {"ok": true, "authorize_url": "..."}`` (the Mini App then
      navigates the browser there)
    - authenticated via the ``init_data`` query parameter →
      ``302`` directly to Google's authorization endpoint
    - anything else → ``401``
    - missing/invalid server configuration → ``503`` (fail closed, no
      state is created and no secret detail is revealed)
    """
    user = _authenticate()
    if user is None:
        return _unauthenticated()

    header_mode = bool(request.headers.get(INIT_DATA_HEADER))

    try:
        config = youtube_oauth.get_config()
        # Fail closed if tokens could not be encrypted at rest.
        TokenCipher.from_environment()
        user_id = _ensure_user(user)
        state = youtube_oauth.create_state(user_id)
        authorize_url = youtube_oauth.build_authorization_url(config, state)
    except (
        youtube_oauth.YouTubeConfigError,
        SocialAccountConfigError,
    ) as exc:
        # The message names the missing variable only — never a value.
        logger.error("YouTube connect unavailable: %s", exc)
        return jsonify(
            {"ok": False, "error": "server_configuration"}
        ), 503

    if header_mode:
        return jsonify({"ok": True, "authorize_url": authorize_url}), 200
    return redirect(authorize_url, 302)


# ── Callback: finish the flow ──────────────────────────────────────────
@social_bp.get("/youtube/callback")
def youtube_callback():
    """Validate state, exchange the code, link the channel, return home.

    The Telegram identity comes exclusively from the consumed state —
    no query parameter can rebind the link to a different user.
    """
    # 1. State first: single-use, expiring, bound to the starter.
    try:
        user_id = youtube_oauth.consume_state(
            request.args.get("state") or ""
        )
    except youtube_oauth.StateError:
        logger.info("YouTube callback rejected: invalid state")
        return redirect(_result_url("invalid_state"), 302)

    # 2. Consent denial is handled cleanly (state already consumed).
    if request.args.get("error"):
        logger.info("YouTube consent was denied by the user")
        return redirect(_result_url("denied"), 302)

    code = request.args.get("code") or ""
    if not code:
        logger.info("YouTube callback missing authorization code")
        return redirect(_result_url("invalid_request"), 302)

    # 3. Exchange the code and resolve the authenticated channel.
    try:
        config = youtube_oauth.get_config()
        grant = youtube_oauth.exchange_code(config, code)
        channel = youtube_oauth.fetch_channel(grant.access_token)
    except youtube_oauth.YouTubeConfigError as exc:
        logger.error("YouTube callback configuration error: %s", exc)
        return redirect(_result_url("config_error"), 302)
    except (
        youtube_oauth.YouTubeOAuthError,
        youtube_oauth.YouTubeAPIError,
    ) as exc:
        # Message holds only a status/Google error code — no secrets.
        logger.error("YouTube callback failed: %s", exc)
        return redirect(_result_url("oauth_failed"), 302)

    # 4. Persist (tokens encrypted; uniqueness enforced atomically).
    if db.get_user(user_id) is None:
        db.register_user(user_id, None, None)
    try:
        SocialAccountService.link(
            user_id=user_id,
            provider=PROVIDER_YOUTUBE,
            provider_user_id=channel.channel_id,
            username=channel.handle,
            display_name=channel.title,
            scopes=grant.scopes,
            access_token=grant.access_token,
            refresh_token=grant.refresh_token,
            expires_in=grant.expires_in,
        )
    except SocialAccountConfigError as exc:
        logger.error("YouTube token storage unavailable: %s", exc)
        return redirect(_result_url("config_error"), 302)
    except DuplicateLinkError:
        logger.info("YouTube channel already linked to another account")
        return redirect(_result_url("channel_taken"), 302)
    except SocialAccountError as exc:
        logger.error("Failed to persist YouTube link: %s", exc)
        return redirect(_result_url("storage_failed"), 302)

    logger.info("YouTube channel linked for Telegram user %s", user_id)
    return redirect(_result_url("linked"), 302)


# ── Read: the caller's linked accounts (no token material) ─────────────
@social_bp.get("/accounts")
def list_accounts():
    """Return the authenticated user's linked accounts (safe fields)."""
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    accounts = SocialAccountService.list_accounts(int(user["user_id"]))
    return (
        jsonify(
            {
                "ok": True,
                "accounts": [
                    {
                        "provider": a.provider,
                        "display_name": a.display_name,
                        "username": a.username,
                        "status": a.status,
                        "linked_at": a.created_at,
                    }
                    for a in accounts
                ],
            }
        ),
        200,
    )


# ── Unlink: the caller's own link only ─────────────────────────────────
@social_bp.post("/youtube/unlink")
def youtube_unlink():
    """Remove the authenticated user's YouTube link and its tokens."""
    user = _authenticate()
    if user is None:
        return _unauthenticated()
    user_id = int(user["user_id"])

    # Best-effort revocation of the stored token before deleting it.
    try:
        tokens = SocialAccountService.load_tokens(
            user_id, PROVIDER_YOUTUBE
        )
    except SocialAccountConfigError:
        tokens = None  # nothing decryptable — still allow local removal
    if tokens is not None:
        token = tokens.refresh_token or tokens.access_token
        youtube_oauth.revoke_token(token)

    removed = SocialAccountService.unlink(user_id, PROVIDER_YOUTUBE)
    return jsonify({"ok": True, "removed": removed}), 200
