"""
Google OAuth 2.0 + YouTube Data API integration (SA-YT-01)
==========================================================

Everything Google/YouTube-specific lives here:

- configuration read from the environment (never hard-coded)
- the single, minimum YouTube scope
- server-side, single-use, expiring OAuth ``state`` bound to the
  Telegram user that started the flow (CSRF + session binding)
- authorization-code exchange with Google
- the authenticated channel lookup (``channels.list?mine=true``)
- best-effort token revocation

External calls go through the small ``_http_post_form`` /
``_http_get_json`` seams so unit tests can mock Google completely.

Reference (official docs only):
https://developers.google.com/youtube/v3/guides/auth/server-side-web-apps
https://developers.google.com/youtube/v3/guides/authentication
https://developers.google.com/youtube/v3/getting-started
"""

import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Scope: exactly the minimum needed to read the user's channel ──────
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"

# ── Endpoints (public, non-secret) ─────────────────────────────────────
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# ── Environment variable names ─────────────────────────────────────────
CLIENT_ID_ENV = "YOUTUBE_CLIENT_ID"
CLIENT_SECRET_ENV = "YOUTUBE_CLIENT_SECRET"
REDIRECT_URI_ENV = "YOUTUBE_REDIRECT_URI"

# OAuth state lifetime and HTTP timeout.
STATE_TTL_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 15


# ── Errors (never carry token material) ────────────────────────────────
class YouTubeOAuthError(Exception):
    """Base error for the YouTube OAuth flow."""


class YouTubeConfigError(YouTubeOAuthError):
    """Required OAuth configuration is missing from the environment."""


class YouTubeAPIError(YouTubeOAuthError):
    """The YouTube Data API call failed or returned no channel."""


class StateError(YouTubeOAuthError):
    """OAuth state is missing, malformed, expired or already used."""


# ── Configuration ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


def get_config() -> OAuthConfig:
    """Load OAuth credentials from the environment (fail closed).

    Raises:
        YouTubeConfigError:            naming the missing variable — the values
            themselves are never included in the error.
    """
    missing = [
        name
        for name in (CLIENT_ID_ENV, CLIENT_SECRET_ENV, REDIRECT_URI_ENV)
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise YouTubeConfigError(
            "missing required environment variable(s): " + ", ".join(missing)
        )
    return OAuthConfig(
        client_id=os.environ[CLIENT_ID_ENV].strip(),
        client_secret=os.environ[CLIENT_SECRET_ENV].strip(),
        redirect_uri=os.environ[REDIRECT_URI_ENV].strip(),
    )


# ── Server-side OAuth state (single-use, expiring, user-bound) ────────
# Keyed by SHA-256(state): even a leaked store cannot be replayed.
_state_lock = threading.Lock()
_states: dict[str, tuple[int, float]] = {}


def _state_key(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def create_state(user_id: int, ttl_seconds: int = STATE_TTL_SECONDS) -> str:
    """Create a cryptographically random state bound to ``user_id``."""
    if not user_id:
        raise StateError("user_id is required to create an OAuth state")
    state = secrets.token_urlsafe(32)  # 256 bits of entropy
    expires_at = time.time() + ttl_seconds
    with _state_lock:
        _prune_expired()
        _states[_state_key(state)] = (int(user_id), expires_at)
    return state


def consume_state(state: str) -> int:
    """Consume a state and return the bound Telegram ``user_id``.

    Single-use: the record is removed *before* validation, so a state
    can never be replayed — not even an expired one.

    Raises:
        StateError: missing, malformed, unknown, reused or expired state.
    """
    if not state or not isinstance(state, str):
        raise StateError("state is missing or malformed")
    with _state_lock:
        record = _states.pop(_state_key(state), None)
    if record is None:
        raise StateError("state is invalid, already used, or unknown")
    user_id, expires_at = record
    if time.time() > expires_at:
        raise StateError("state has expired")
    return user_id


def _prune_expired() -> None:
    """Drop expired records (caller holds the lock)."""
    now = time.time()
    stale = [k for k, (_, exp) in _states.items() if now > exp]
    for key in stale:
        _states.pop(key, None)


def clear_states() -> None:
    """Forget every outstanding state (used by tests / maintenance)."""
    with _state_lock:
        _states.clear()


def build_authorization_url(config: OAuthConfig, state: str) -> str:
    """Build Google's authorization URL with the single YouTube scope."""
    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": YOUTUBE_SCOPE,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


# ── Token grant ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OAuthGrant:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scopes: str


def exchange_code(config: OAuthConfig, code: str) -> OAuthGrant:
    """Exchange the authorization code for tokens."""
    if not code:
        raise YouTubeOAuthError("authorization code is missing")
    status, body = _http_post_form(
        TOKEN_ENDPOINT,
        {
            "code": code,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "redirect_uri": config.redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    access_token = body.get("access_token")
    if status != 200 or not access_token:
        # Only Google's error *code* is surfaced — never the response
        # body, and never any token material.
        error_code = body.get("error") or "unexpected_response"
        raise YouTubeOAuthError(f"token exchange failed ({error_code})")
    raw_expires = body.get("expires_in")
    try:
        expires_in = int(raw_expires) if raw_expires is not None else None
    except (TypeError, ValueError):
        expires_in = None
    return OAuthGrant(
        access_token=access_token,
        refresh_token=body.get("refresh_token") or None,
        expires_in=expires_in,
        scopes=body.get("scope") or YOUTUBE_SCOPE,
    )


# ── Authenticated channel lookup ───────────────────────────────────────
@dataclass(frozen=True)
class YouTubeChannel:
    channel_id: str
    title: str | None
    handle: str | None


def fetch_channel(access_token: str) -> YouTubeChannel:
    """Resolve the authenticated user's channel via ``mine=true``."""
    if not access_token:
        raise YouTubeAPIError("access token is required for channel lookup")
    query = urllib.parse.urlencode({"part": "snippet", "mine": "true"})
    status, body = _http_get_json(
        CHANNELS_ENDPOINT + "?" + query,
        {"Authorization": "Bearer " + access_token},
    )
    if status != 200:
        raise YouTubeAPIError(f"channel lookup failed (http {status})")
    items = body.get("items")
    if not items:
        raise YouTubeAPIError(
            "no YouTube channel is associated with this Google account"
        )
    first = items[0] or {}
    channel_id = first.get("id")
    if not channel_id:
        raise YouTubeAPIError("channel response did not include a channel id")
    snippet = first.get("snippet") or {}
    handle = snippet.get("customUrl") or None
    return YouTubeChannel(
        channel_id=str(channel_id),
        title=snippet.get("title") or None,
        handle=handle,
    )


def revoke_token(token: str) -> bool:
    """Best-effort token revocation; never raises, never logs the token."""
    if not token:
        return False
    try:
        status, _body = _http_post_form(REVOKE_ENDPOINT, {"token": token})
        return status == 200
    except YouTubeOAuthError:
        return False


# ── Minimal HTTP seams (mocked in tests) ───────────────────────────────
def _http_post_form(
    url: str, data: dict, headers: dict | None = None
) -> tuple[int, dict]:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            **(headers or {}),
        },
    )
    return _http_json(request)


def _http_get_json(url: str, headers: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", **(headers or {})},
    )
    return _http_json(request)


def _http_json(request: urllib.request.Request) -> tuple[int, dict]:
    """Perform a request; return ``(status, parsed_json_or_empty)``."""
    try:
        with urllib.request.urlopen(
            request, timeout=HTTP_TIMEOUT_SECONDS
        ) as response:
            raw = response.read().decode("utf-8", "replace")
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except (urllib.error.URLError, OSError):
        # The underlying reason can embed the request URL/headers, so it
        # is deliberately not chained or logged here.
        raise YouTubeOAuthError("could not reach the OAuth provider") from None
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return int(status), body
