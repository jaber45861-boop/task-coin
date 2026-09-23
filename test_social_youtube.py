"""
Focused tests — YouTube account linking foundation (SA-YT-01)
=============================================================

Covers the required checklist:

  1.  connect endpoint requires Telegram authentication
  2.  unauthenticated connect rejected
  3.  valid connect creates OAuth state
  4.  state is cryptographically unpredictable
  5.  state expires
  6.  state is single-use
  7.  invalid state rejected
  8.  reused state rejected
  9.  state is bound to Telegram user
  10. callback cannot bind another Telegram user
  11. callback handles OAuth denial
  12. callback handles token exchange failure
  13. YouTube API failure handled safely
  14. authenticated channel retrieved with mine=true
  15. stable YouTube channel ID persisted
  16. channel title persisted
  17. duplicate channel cannot be linked to another Telegram user
  18. duplicate active link for the same user handled safely
  19. tokens never appear in response bodies
  20. tokens never appear in logs/errors (+ encrypted at rest)
  21. credentials are loaded from environment
  22. secrets are not hard-coded
  23. YouTube scope is exactly the intended minimum scope
  24. no unnecessary Google scopes requested
  25. Telegram user remains the owner of the link
  26. linked account can be retrieved
  27. unlink cannot affect another user's account
  28. no YouTube task/reward logic exists
  29. Mini App connect UI uses existing navigation/theme
  30. no regression in existing Mini App tests   (run suite)
  31. no regression in bot tests                 (run suite)
  32. no regression in database tests            (run suite)

All Google/YouTube HTTP calls are mocked — no real credentials are
required to run this file.
"""

import ast
import re
import string
import urllib.parse
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import db
import serve_miniapp
import youtube_oauth
from social_accounts import (
    PROVIDER_YOUTUBE,
    TOKEN_ENCRYPTION_KEY_ENV,
    SocialAccountConfigError,
    SocialAccountService,
)

# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

CONNECT_PATH = "/api/social/youtube/connect"
CALLBACK_PATH = "/api/social/youtube/callback"
ACCOUNTS_PATH = "/api/social/accounts"
UNLINK_PATH = "/api/social/youtube/unlink"

USER_A = 1001  # alice
USER_B = 2002  # bob

# Obviously-fake token markers used to prove they never leak.
ACCESS_TOKEN = "TEST-ACCESS-TOKEN-aa11bb22"
REFRESH_TOKEN = "TEST-REFRESH-TOKEN-cc33dd44"

TEST_CLIENT_ID = "unit-test-client-id.apps.googleusercontent.com"
TEST_CLIENT_SECRET = "unit-test-client-secret-not-a-real-credential"
TEST_REDIRECT_URI = "https://miniapp.test/api/social/youtube/callback"

_TOKEN_OK = {
    "access_token": ACCESS_TOKEN,
    "refresh_token": REFRESH_TOKEN,
    "expires_in": 3600,
    "scope": youtube_oauth.YOUTUBE_SCOPE,
    "token_type": "Bearer",
}

_CHANNEL_OK = {
    "items": [
        {
            "id": "UC0TestChannelId00000001",
            "snippet": {"title": "Test Channel", "customUrl": "@testchannel"},
        }
    ]
}


# ── Fake Google/YouTube HTTP layer ─────────────────────────────────────
class _FakeHTTP:
    """Replaces both HTTP seams in youtube_oauth (no network in tests)."""

    def __init__(self) -> None:
        self.token_result: tuple[int, dict] = (200, dict(_TOKEN_OK))
        self.channel_result: tuple[int, dict] = (200, dict(_CHANNEL_OK))
        self.token_calls: list[tuple[str, dict]] = []
        self.channel_calls: list[tuple[str, dict]] = []
        self.revoke_calls: list[dict] = []

    def post_form(self, url: str, data: dict, headers: dict | None = None):
        if url == youtube_oauth.REVOKE_ENDPOINT:
            self.revoke_calls.append(dict(data))
            return (200, {})
        self.token_calls.append((url, dict(data)))
        return self.token_result

    def get_json(self, url: str, headers: dict | None = None):
        self.channel_calls.append((url, dict(headers or {})))
        return self.channel_result

    @property
    def channel_url(self) -> str:
        return self.channel_calls[0][0]

    @property
    def channel_headers(self) -> dict:
        return self.channel_calls[0][1]


# ── Fixtures ───────────────────────────────────────────────────────────
@pytest.fixture
def env(monkeypatch, tmp_path):
    """Environment + isolated database + clean state store."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", TEST_CLIENT_ID)
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", TEST_CLIENT_SECRET)
    monkeypatch.setenv("YOUTUBE_REDIRECT_URI", TEST_REDIRECT_URI)
    monkeypatch.setenv(
        TOKEN_ENCRYPTION_KEY_ENV, Fernet.generate_key().decode()
    )

    db_path = str(tmp_path / "social_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_A, "alice", "Alice")
    db.register_user(USER_B, "bob", "Bob")

    youtube_oauth.clear_states()
    yield db_path
    youtube_oauth.clear_states()


@pytest.fixture
def fake_http(monkeypatch):
    fake = _FakeHTTP()
    monkeypatch.setattr(youtube_oauth, "_http_post_form", fake.post_form)
    monkeypatch.setattr(youtube_oauth, "_http_get_json", fake.get_json)
    return fake


@pytest.fixture
def client():
    return serve_miniapp.app.test_client()


# ── Helpers ────────────────────────────────────────────────────────────
def _connect(client, user_id: int = USER_A, header_mode: bool = True, **kw):
    init_data = _make_init_data(user_id=user_id, **kw)
    if header_mode:
        return client.get(CONNECT_PATH, headers={INIT_DATA_HEADER: init_data})
    return client.get(CONNECT_PATH + "?" + urllib.parse.urlencode(
        {"init_data": init_data}
    ))


def _authorize_state(response) -> tuple[str, str]:
    """Return (state, authorize_url) from a connect response."""
    assert response.status_code == 200, response.get_data(as_text=True)
    url = response.get_json()["authorize_url"]
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
    return state, url


def _callback(client, state: str, **params):
    query = {"state": state, **params}
    return client.get(CALLBACK_PATH + "?" + urllib.parse.urlencode(query))


def _link(client, user_id: int = USER_A, **callback_params):
    """Run connect + callback end-to-end (the fake_http fixture patches HTTP)."""
    state, _url = _authorize_state(_connect(client, user_id=user_id))
    return _callback(client, state, code="test-auth-code", **callback_params)


def _rows(db_path: str, user_id: int | None = None) -> list[dict]:
    with db.get_connection(db_path) as conn:
        if user_id is None:
            cur = conn.execute("SELECT * FROM social_accounts ORDER BY id")
        else:
            cur = conn.execute(
                "SELECT * FROM social_accounts WHERE user_id = ? ORDER BY id",
                (user_id,),
            )
        return [dict(r) for r in cur.fetchall()]


def _outcome(response) -> str:
    assert response.status_code == 302, response.status_code
    location = response.headers["Location"]
    return location.split("#youtube=", 1)[1]


# ══════════════════════════════════════════════════════════════════════
# 1–2. Authentication on protected endpoints
# ══════════════════════════════════════════════════════════════════════
class TestConnectAuthentication:

    def test_connect_requires_authentication(self, env, client):
        """(1) Connect with no credentials is rejected."""
        resp = client.get(CONNECT_PATH)
        assert resp.status_code == 401
        assert resp.get_json() == {"ok": False, "error": "unauthenticated"}

    def test_connect_rejects_invalid_init_data(self, env, client):
        """(2) Forged initData is rejected."""
        resp = client.get(
            CONNECT_PATH,
            headers={INIT_DATA_HEADER: "auth_date=1&user=%7B%7D&hash=bad"},
        )
        assert resp.status_code == 401

    def test_unauthenticated_user_id_query_param_is_not_trusted(self, env, client):
        """(2) A browser-supplied user_id never authenticates anything."""
        resp = client.get(CONNECT_PATH + f"?user_id={USER_A}")
        assert resp.status_code == 401

    def test_accounts_and_unlink_require_authentication(self, env, client):
        """(2) The read and unlink endpoints reject anonymous callers."""
        assert client.get(ACCOUNTS_PATH).status_code == 401
        assert client.post(UNLINK_PATH).status_code == 401

    def test_query_param_mode_redirects_to_google(self, env, client):
        """Connect can also redirect a plain top-level browser navigation."""
        resp = _connect(client, header_mode=False)
        assert resp.status_code == 302
        assert resp.headers["Location"].startswith(youtube_oauth.AUTH_ENDPOINT)


# ══════════════════════════════════════════════════════════════════════
# 3–9. OAuth state: creation, entropy, expiry, single-use, binding
# ══════════════════════════════════════════════════════════════════════
class TestOAuthState:

    def test_valid_connect_creates_state(self, env, client):
        """(3) A valid connect creates state and an authorize URL."""
        resp = _connect(client)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        state, url = _authorize_state(resp)
        assert len(state) >= 43
        assert url.startswith(youtube_oauth.AUTH_ENDPOINT)
        # The created state is immediately consumable by the callback.
        assert youtube_oauth.consume_state(state) == USER_A

    def test_state_is_cryptographically_unpredictable(self, env):
        """(4) States are high-entropy, unique and contain no identity."""
        allowed = set(string.ascii_letters + string.digits + "-_")
        big_user = 9876543210123  # long id: never embeddable by chance
        states = [youtube_oauth.create_state(big_user) for _ in range(64)]
        assert len(set(states)) == 64, "states must never repeat"
        for state in states:
            assert len(state) >= 43, "at least 256 bits of entropy"
            assert set(state) <= allowed, "url-safe random alphabet"
            assert str(big_user) not in state, "no user identity embedded"

    def test_state_expires(self, env):
        """(5) An expired state is rejected."""
        state = youtube_oauth.create_state(USER_A, ttl_seconds=-1)
        with pytest.raises(youtube_oauth.StateError, match="expired"):
            youtube_oauth.consume_state(state)

    def test_state_is_single_use(self, env):
        """(6) A state can be consumed exactly once."""
        state = youtube_oauth.create_state(USER_A)
        assert youtube_oauth.consume_state(state) == USER_A
        with pytest.raises(youtube_oauth.StateError):
            youtube_oauth.consume_state(state)

    def test_invalid_state_rejected_by_callback(self, env, client):
        """(7) Garbage / missing state is rejected with a fixed outcome."""
        assert _outcome(_callback(client, "not-a-real-state")) == "invalid_state"
        assert _outcome(_callback(client, "")) == "invalid_state"

    def test_reused_state_rejected_by_callback(self, env, fake_http, client):
        """(8) Replaying a consumed state fails and changes nothing."""
        state, _ = _authorize_state(_connect(client))
        first = _callback(client, state, code="test-auth-code")
        assert _outcome(first) == "linked"
        assert len(_rows(env, USER_A)) == 1

        replay = _callback(client, state, code="test-auth-code")
        assert _outcome(replay) == "invalid_state"
        assert len(_rows(env, USER_A)) == 1  # unchanged

    def test_state_is_bound_to_telegram_user(self, env, client):
        """(9) The state carries the authenticated Telegram user."""
        state, _ = _authorize_state(_connect(client, user_id=USER_A))
        assert youtube_oauth.consume_state(state) == USER_A

        state_b, _ = _authorize_state(_connect(client, user_id=USER_B))
        assert youtube_oauth.consume_state(state_b) == USER_B


# ══════════════════════════════════════════════════════════════════════
# 10–13. Callback security and failure handling
# ══════════════════════════════════════════════════════════════════════
class TestCallbackSecurity:

    def test_callback_cannot_bind_another_telegram_user(self, env, fake_http, client):
        """(10) Query parameters cannot rebind the link — state decides."""
        state, _ = _authorize_state(_connect(client, user_id=USER_A))
        resp = _callback(
            client,
            state,
            code="test-auth-code",
            user_id=str(USER_B),           # attacker-supplied identity
            provider_user_id="UC_EVIL",    # attacker-supplied channel
        )
        assert _outcome(resp) == "linked"

        rows_a = _rows(env, USER_A)
        rows_b = _rows(env, USER_B)
        assert len(rows_a) == 1
        assert rows_a[0]["user_id"] == USER_A
        assert rows_b == [], "user B must not receive the link"
        # The channel id comes from the YouTube API response, never the client.
        assert rows_a[0]["provider_user_id"] == _CHANNEL_OK["items"][0]["id"]

    def test_callback_handles_consent_denial(self, env, client):
        """(11) Google's error response is handled cleanly."""
        state, _ = _authorize_state(_connect(client))
        resp = _callback(client, state, error="access_denied")
        assert _outcome(resp) == "denied"
        assert _rows(env) == []
        # The state was still consumed exactly once.
        assert _outcome(_callback(client, state, error="access_denied")) \
            == "invalid_state"

    def test_callback_handles_token_exchange_failure(self, env, fake_http, client, caplog):
        """(12) A failed exchange links nothing and leaks nothing."""
        fake_http.token_result = (400, {"error": "invalid_grant"})
        state, _ = _authorize_state(_connect(client))
        with caplog.at_level("DEBUG"):
            resp = _callback(client, state, code="bad-code")
        assert _outcome(resp) == "oauth_failed"
        assert _rows(env) == []
        assert ACCESS_TOKEN not in caplog.text

    def test_callback_handles_youtube_api_failure(self, env, fake_http, client):
        """(13) A YouTube API failure is handled safely."""
        fake_http.channel_result = (
            403,
            {"error": {"code": 403, "message": "forbidden"}},
        )
        state, _ = _authorize_state(_connect(client))
        resp = _callback(client, state, code="test-auth-code")
        assert _outcome(resp) == "oauth_failed"
        assert _rows(env) == []

    def test_callback_handles_channelless_account(self, env, fake_http, client):
        """(13) An account with no YouTube channel links nothing."""
        fake_http.channel_result = (200, {"items": []})
        state, _ = _authorize_state(_connect(client))
        resp = _callback(client, state, code="test-auth-code")
        assert _outcome(resp) == "oauth_failed"
        assert _rows(env) == []

    def test_callback_missing_code_rejected(self, env, client):
        state, _ = _authorize_state(_connect(client))
        resp = _callback(client, state)
        assert _outcome(resp) == "invalid_request"
        assert _rows(env) == []


# ══════════════════════════════════════════════════════════════════════
# 14–16. Channel lookup and persistence
# ══════════════════════════════════════════════════════════════════════
class TestChannelLookup:

    def test_channel_lookup_uses_mine_true(self, env, fake_http, client):
        """(14) channels.list is called with mine=true + snippet only."""
        resp = _link(client)
        assert _outcome(resp) == "linked"

        parsed = urllib.parse.urlparse(fake_http.channel_url)
        query = urllib.parse.parse_qs(parsed.query)
        assert parsed.scheme == "https"
        assert parsed.netloc == "www.googleapis.com"
        assert parsed.path == "/youtube/v3/channels"
        assert query["mine"] == ["true"]
        assert query["part"] == ["snippet"]
        # The authenticated token is the only credential used.
        assert fake_http.channel_headers["Authorization"] == \
            f"Bearer {ACCESS_TOKEN}"

    def test_channel_id_persisted(self, env, fake_http, client):
        """(15) The stable channel id is the stored identity."""
        assert _outcome(_link(client)) == "linked"
        rows = _rows(env, USER_A)
        assert len(rows) == 1
        assert rows[0]["provider"] == PROVIDER_YOUTUBE
        assert rows[0]["provider_user_id"] == _CHANNEL_OK["items"][0]["id"]

    def test_channel_title_and_handle_persisted(self, env, fake_http, client):
        """(16) Display name and handle are stored for the UI."""
        assert _outcome(_link(client)) == "linked"
        row = _rows(env, USER_A)[0]
        assert row["display_name"] == "Test Channel"
        assert row["username"] == "@testchannel"
        assert row["status"] == "linked"
        assert row["scopes"] == youtube_oauth.YOUTUBE_SCOPE
        assert row["token_expires_at"] is not None
        assert row["last_verified_at"] is not None


# ══════════════════════════════════════════════════════════════════════
# 17–18. Uniqueness rules
# ══════════════════════════════════════════════════════════════════════
class TestLinkUniqueness:

    def test_duplicate_channel_cannot_link_another_user(self, env, fake_http, client):
        """(17) One YouTube channel can belong to only one Telegram user."""
        assert _outcome(_link(client, user_id=USER_A)) == "linked"

        resp = _link(client, user_id=USER_B)  # same channel from the API
        assert _outcome(resp) == "channel_taken"

        rows_a = _rows(env, USER_A)
        assert len(rows_a) == 1
        assert rows_a[0]["user_id"] == USER_A
        assert _rows(env, USER_B) == []

    def test_relink_replaces_previous_active_link(self, env, fake_http, client):
        """(18) A user never accumulates duplicate active YouTube links."""
        assert _outcome(_link(client, user_id=USER_A)) == "linked"

        fake_http.channel_result = (
            200,
            {
                "items": [
                    {
                        "id": "UC0TestChannelId00000002",
                        "snippet": {
                            "title": "Second Channel",
                            "customUrl": "@secondchannel",
                        },
                    }
                ]
            },
        )
        assert _outcome(_link(client, user_id=USER_A)) == "linked"

        rows = _rows(env, USER_A)
        assert len(rows) == 1, "exactly one active link per user/provider"
        assert rows[0]["provider_user_id"] == "UC0TestChannelId00000002"
        assert len(_rows(env)) == 1


# ══════════════════════════════════════════════════════════════════════
# 19–20. Token security
# ══════════════════════════════════════════════════════════════════════
class TestTokenSecurity:

    def test_tokens_never_appear_in_responses(self, env, fake_http, client):
        """(19) No response body or redirect ever carries token material."""
        state, url = _authorize_state(_connect(client))
        assert ACCESS_TOKEN not in url

        callback = _callback(client, state, code="test-auth-code")
        assert _outcome(callback) == "linked"
        assert ACCESS_TOKEN not in callback.headers["Location"]
        assert REFRESH_TOKEN not in callback.headers["Location"]

        accounts = client.get(
            ACCOUNTS_PATH, headers={INIT_DATA_HEADER: _make_init_data()}
        )
        assert accounts.status_code == 200
        text = accounts.get_data(as_text=True)
        assert ACCESS_TOKEN not in text
        assert REFRESH_TOKEN not in text
        # And no token-shaped fields at all.
        body = accounts.get_json()
        for account in body["accounts"]:
            assert set(account) == {
                "provider", "display_name", "username", "status", "linked_at",
            }

    def test_tokens_never_appear_in_logs_or_errors(
        self, env, fake_http, client, caplog
    ):
        """(20) Logs stay free of token material across the whole flow."""
        with caplog.at_level("DEBUG"):
            _link(client)
        text = caplog.text
        assert ACCESS_TOKEN not in text
        assert REFRESH_TOKEN not in text

    def test_tokens_are_encrypted_at_rest(self, env, fake_http, client):
        """(20) Stored tokens are ciphertext, never plaintext."""
        assert _outcome(_link(client)) == "linked"
        blobs = b""
        for suffix in ("", "-wal", "-shm"):
            path = Path(env + suffix)
            if path.exists():
                blobs += path.read_bytes()
        assert ACCESS_TOKEN.encode() not in blobs
        assert REFRESH_TOKEN.encode() not in blobs

        row = _rows(env, USER_A)[0]
        assert row["access_token_encrypted"]
        assert row["access_token_encrypted"] != ACCESS_TOKEN

        # ...yet the service can decrypt them for backend use.
        tokens = SocialAccountService.load_tokens(USER_A, PROVIDER_YOUTUBE)
        assert tokens.access_token == ACCESS_TOKEN
        assert tokens.refresh_token == REFRESH_TOKEN

    def test_missing_encryption_key_fails_closed(self, env, monkeypatch, client):
        """(20) Without a key nothing is stored and no state is minted."""
        monkeypatch.delenv(TOKEN_ENCRYPTION_KEY_ENV)
        resp = _connect(client)
        assert resp.status_code == 503
        assert resp.get_json() == {
            "ok": False, "error": "server_configuration",
        }
        assert len(youtube_oauth._states) == 0, "no state without storage"

        with pytest.raises(SocialAccountConfigError):
            SocialAccountService.link(
                user_id=USER_A,
                provider=PROVIDER_YOUTUBE,
                provider_user_id="UC_X",
                scopes=youtube_oauth.YOUTUBE_SCOPE,
                access_token="x",
            )
        assert _rows(env) == []


# ══════════════════════════════════════════════════════════════════════
# 21–24. Environment configuration and scope discipline
# ══════════════════════════════════════════════════════════════════════
class TestConfigurationAndScope:

    def test_credentials_loaded_from_environment(self, env, client, monkeypatch):
        """(21) Client id / redirect URI come from the environment."""
        _state, url = _authorize_state(_connect(client))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query["client_id"] == [TEST_CLIENT_ID]
        assert query["redirect_uri"] == [TEST_REDIRECT_URI]

        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "other-id.apps.googleusercontent.com")
        _state2, url2 = _authorize_state(_connect(client))
        query2 = urllib.parse.parse_qs(urllib.parse.urlparse(url2).query)
        assert query2["client_id"] == ["other-id.apps.googleusercontent.com"]
        assert query2["client_id"] != query["client_id"]

    def test_token_exchange_uses_configured_credentials(
        self, env, fake_http, client
    ):
        """(21) The exchange posts environment credentials, not literals."""
        _link(client)
        url, data = fake_http.token_calls[0]
        assert url == youtube_oauth.TOKEN_ENDPOINT
        assert data["client_id"] == TEST_CLIENT_ID
        assert data["client_secret"] == TEST_CLIENT_SECRET
        assert data["redirect_uri"] == TEST_REDIRECT_URI
        assert data["grant_type"] == "authorization_code"

    def test_missing_credentials_fail_closed(self, env, monkeypatch, client):
        """(21) Missing configuration is reported without any secret."""
        monkeypatch.delenv("YOUTUBE_CLIENT_SECRET")
        resp = _connect(client)
        assert resp.status_code == 503
        assert resp.get_json()["error"] == "server_configuration"
        assert len(youtube_oauth._states) == 0

    def test_no_hardcoded_secrets_in_source(self, env, monkeypatch):
        """(22) No credential value is committed to the repository."""
        sources = [
            "social_accounts.py",
            "youtube_oauth.py",
            "social_routes.py",
            "serve_miniapp.py",
            "db.py",
            "miniapp/js/social.js",
            "miniapp/js/home.js",
        ]
        forbidden_patterns = [
            r"apps\.googleusercontent\.com",  # real client id shape
            r"ya29\.",                        # Google access-token prefix
            r"1//",                           # Google refresh-token shape
            r"AIza",                          # Google API-key prefix
        ]
        for path in sources:
            text = Path(path).read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                assert re.search(pattern, text) is None, \
                    f"{path} contains a credential-shaped literal ({pattern})"
        # Config must be read from the environment by name.
        oauth_src = Path("youtube_oauth.py").read_text(encoding="utf-8")
        for var in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
                    "YOUTUBE_REDIRECT_URI"):
            assert var in oauth_src
        assert "os.environ" in oauth_src
        accounts_src = Path("social_accounts.py").read_text(encoding="utf-8")
        assert TOKEN_ENCRYPTION_KEY_ENV in accounts_src
        # No actual key material (a Fernet key is 43 base64 chars + '=')
        # is hard-coded anywhere — only the env var *name* appears.
        assert re.search(r"[\"'][A-Za-z0-9_-]{43}=[\"']", accounts_src) is None
        assert re.search(r"[\"'][A-Za-z0-9_-]{43}=[\"']", oauth_src) is None
        # The key value is only ever read from the environment.
        assert "os.environ.get(TOKEN_ENCRYPTION_KEY_ENV" in accounts_src

    def test_scope_is_exactly_the_minimum(self, env, client):
        """(23) The requested scope is exactly youtube.readonly."""
        _state, url = _authorize_state(_connect(client))
        scope = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"]
        assert scope == ["https://www.googleapis.com/auth/youtube.readonly"]
        assert youtube_oauth.YOUTUBE_SCOPE == \
            "https://www.googleapis.com/auth/youtube.readonly"

    def test_no_unnecessary_google_scopes_requested(self):
        """(24) Nothing beyond youtube.readonly exists in the sources."""
        scope_re = re.compile(
            r"https://www\.googleapis\.com/auth/[A-Za-z0-9_.\-]+"
        )
        found: set[str] = set()
        for path in [
            "youtube_oauth.py", "social_routes.py", "social_accounts.py",
            "miniapp/js/social.js", "miniapp/js/home.js", "miniapp/index.html",
        ]:
            found.update(scope_re.findall(Path(path).read_text(encoding="utf-8")))
        assert found == {"https://www.googleapis.com/auth/youtube.readonly"}


# ══════════════════════════════════════════════════════════════════════
# 25–27. Ownership, retrieval, unlink
# ══════════════════════════════════════════════════════════════════════
class TestOwnershipAndUnlink:

    def test_telegram_user_remains_owner(self, env, fake_http, client):
        """(25) Each user only ever sees their own links."""
        assert _outcome(_link(client, user_id=USER_A)) == "linked"

        as_a = client.get(
            ACCOUNTS_PATH, headers={INIT_DATA_HEADER: _make_init_data(user_id=USER_A)}
        )
        as_b = client.get(
            ACCOUNTS_PATH, headers={INIT_DATA_HEADER: _make_init_data(user_id=USER_B)}
        )
        assert [a["provider"] for a in as_a.get_json()["accounts"]] == ["youtube"]
        assert as_b.get_json()["accounts"] == []
        assert _rows(env, USER_A)[0]["user_id"] == USER_A

    def test_linked_account_can_be_retrieved(self, env, fake_http, client):
        """(26) The service returns the link — without token fields."""
        assert _outcome(_link(client)) == "linked"
        account = SocialAccountService.get(USER_A, PROVIDER_YOUTUBE)
        assert account is not None
        assert account.user_id == USER_A
        assert account.provider == "youtube"
        assert account.provider_user_id == _CHANNEL_OK["items"][0]["id"]
        assert account.display_name == "Test Channel"
        assert account.is_linked
        # Never any token material on the account object.
        assert not hasattr(account, "access_token")
        assert not hasattr(account, "refresh_token")
        # And the other user sees nothing.
        assert SocialAccountService.get(USER_B, PROVIDER_YOUTUBE) is None

    def test_unlink_cannot_affect_another_user(
        self, env, fake_http, client, monkeypatch
    ):
        """(27) Unlink only ever removes the caller's own row."""
        revocations: list[str] = []
        monkeypatch.setattr(
            youtube_oauth, "revoke_token",
            lambda token: revocations.append(token) or True,
        )
        assert _outcome(_link(client, user_id=USER_A)) == "linked"

        # Bob unlinks "his" account — Alice must be untouched.
        resp_b = client.post(
            UNLINK_PATH, headers={INIT_DATA_HEADER: _make_init_data(user_id=USER_B)}
        )
        assert resp_b.status_code == 200
        assert resp_b.get_json() == {"ok": True, "removed": False}
        assert len(_rows(env, USER_A)) == 1
        assert revocations == []

        # Alice unlinks her own account: row and tokens are destroyed.
        resp_a = client.post(
            UNLINK_PATH, headers={INIT_DATA_HEADER: _make_init_data(user_id=USER_A)}
        )
        assert resp_a.get_json() == {"ok": True, "removed": True}
        assert _rows(env, USER_A) == []
        assert revocations == [REFRESH_TOKEN]
        assert SocialAccountService.get(USER_A, PROVIDER_YOUTUBE) is None


# ══════════════════════════════════════════════════════════════════════
# 28. No task / reward / wallet business logic in this feature
# ══════════════════════════════════════════════════════════════════════
class TestNoTaskOrRewardLogic:

    FORBIDDEN_IMPORTS = {
        "wallet", "ledger", "withdrawal_rules", "task_lifecycle",
        "task_completion", "task_submission", "task_start", "task_attempt",
        "task_catalog", "completion_bridge", "task_verifier",
    }

    def test_backend_modules_do_not_import_business_modules(self):
        """(28) Linking code cannot reach wallet/ledger/task machinery."""
        for path in ["social_accounts.py", "youtube_oauth.py",
                     "social_routes.py"]:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(
                        alias.name.split(".")[0] for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            assert imported & self.FORBIDDEN_IMPORTS == set(), \
                f"{path} imports business modules: {imported}"

    def test_only_social_accounts_table_is_touched(self):
        """(28) All SQL in the new modules targets social_accounts only."""
        sql_re = re.compile(
            r"\b(?:FROM|INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_]*)",
            re.IGNORECASE,
        )
        statement_re = re.compile(
            r"\s*(?:SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE
        )
        for path in ["social_accounts.py", "social_routes.py",
                     "youtube_oauth.py"]:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
            sql_strings = [
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and statement_re.match(node.value)
            ]
            tables = set()
            for sql in sql_strings:
                tables.update(sql_re.findall(sql))
            tables.discard("SET")  # "DO UPDATE SET" is not a table
            assert tables <= {"social_accounts"}, f"{path}: {tables}"

    def test_javascript_has_no_reward_or_wallet_logic(self):
        """(28) The UI module only links accounts — nothing else."""
        raw = Path("miniapp/js/social.js").read_text(encoding="utf-8")
        # Strip comments so documentation about boundaries is allowed.
        code = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)
        code = re.sub(r"//.*?$", "", code, flags=re.MULTILINE)
        lowered = code.lower()
        for word in ("reward", "points", "wallet", "ledger",
                     "deposit", "withdraw", "task_"):
            assert word not in lowered, f"unexpected {word!r} in social.js"
        assert "المكافآت" not in code

    def test_existing_business_files_untouched(self):
        """(28) wallet/ledger/withdrawal/bot/task lifecycle stay clean."""
        import subprocess
        import os
        repo_root = os.path.dirname(os.path.abspath(__file__))
        forbidden = [
            "wallet.py", "ledger.py", "withdrawal_rules.py",
            "task_lifecycle.py", "bot.py",
        ]
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "--", *forbidden],
                cwd=repo_root, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pytest.skip("git is not available in this environment")
        if result.returncode != 0:
            pytest.skip(f"git status failed: {result.stderr.strip()}")
        assert result.stdout.strip() == "", \
            f"forbidden files modified:\n{result.stdout}"


# ══════════════════════════════════════════════════════════════════════
# 29. Mini App UI uses the existing architecture and theme
# ══════════════════════════════════════════════════════════════════════
class TestMiniAppConnectUI:

    def test_index_loads_social_module_before_home(self):
        html = Path("miniapp/index.html").read_text(encoding="utf-8")
        assert 'src="js/social.js"' in html
        assert html.index('src="js/social.js"') < html.index('src="js/home.js"')

    def test_home_has_youtube_connect_button(self):
        home = Path("miniapp/js/home.js").read_text(encoding="utf-8")
        assert 'data-testid="social-youtube-connect"' in home
        assert "ربط YouTube" in home
        assert 'type="button"' in home
        assert 'data-testid="social-youtube-row"' in home
        assert "home-account-linking" in home
        assert "قريباً" in home  # coming-soon marker still present

    def test_connect_uses_existing_telegram_auth(self):
        js = Path("miniapp/js/social.js").read_text(encoding="utf-8")
        assert "TelegramApp.getInitData" in js
        assert INIT_DATA_HEADER in js
        assert CONNECT_PATH in js
        assert ACCOUNTS_PATH in js
        # The page never supplies its own identity.
        assert "user_id=" not in js
        # Existing haptic helper pattern (no new haptic system).
        assert "HapticFeedback" in js

    def test_theme_stays_on_existing_tokens(self):
        css = Path("miniapp/css/app.css").read_text(encoding="utf-8")
        assert ".social-connect-btn" in css
        assert ".social-account-row" in css
        assert "--neon-red" in css
        # Uses project variables rather than inventing a palette.
        assert "var(--neon-red)" in css
        assert "var(--neon-green)" in css

    def test_wallet_is_not_a_new_navigation_item(self):
        """Still exactly three tabs — linking is not a nav item."""
        html = Path("miniapp/index.html").read_text(encoding="utf-8")
        assert html.count("nav-item") == 3  # الرئيسية / المهام / حسابي
        assert html.count("data-page=") == 3
        assert "youtube" not in html.lower().split("bottom-nav", 1)[-1]


# ══════════════════════════════════════════════════════════════════════
# 30–32. Regressions are run via the suite (see the task report):
#   python -m pytest test_miniapp_*.py  (Mini App)
#   python -m pytest test_addchannel.py test_subscription.py ... (bot)
#   python -m pytest test_db.py test_db_transactions.py test_wallet*.py
#                     test_ledger.py    (database)
# ══════════════════════════════════════════════════════════════════════
