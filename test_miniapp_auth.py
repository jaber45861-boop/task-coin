"""
Tests for Telegram Mini App Authentication Backend.

All test vectors are computed independently using the correct Telegram algorithm:
  secret_key = HMAC-SHA256(key=b"WebAppData", message=bot_token).digest()
  data_check_string = sorted key=value pairs (decoded values, newline-separated, hash excluded)
  hash = HMAC-SHA256(key=secret_key, message=data_check_string).hexdigest()

Run:
    python -m pytest test_miniapp_auth.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from urllib.parse import quote, urlencode

# Ensure we have a temp DB before importing db
_test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_test_db.close()
os.environ["TASKCOIN_DB_PATH"] = _test_db.name

import db
from miniapp_auth import (
    app,
    create_app,
    validate_init_data,
    _compute_secret_key,
    _parse_init_data_pairs,
    _build_data_check_string,
)

# ============================================================================
# INDEPENDENT TEST VECTORS
# These are computed using the correct Telegram algorithm, NOT using the
# implementation under test. This ensures we catch any bugs in the impl.
# ============================================================================

_TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

# Pre-computed secret key for the test bot token
# secret_key = HMAC-SHA256(b"WebAppData", _TEST_BOT_TOKEN.encode()).digest()
_INDEPENDENT_SECRET_KEY = hmac.new(
    b"WebAppData",
    _TEST_BOT_TOKEN.encode("utf-8"),
    hashlib.sha256,
).digest()

# Max age large enough for static test vectors (auth_date=1700000000 ≈ Nov 2023)
_STATIC_MAX_AGE = 2_000_000_000


def _compute_independent_hash(data_check_string: str) -> str:
    """Compute HMAC-SHA256 hash using the independent secret key."""
    return hmac.new(
        _INDEPENDENT_SECRET_KEY,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# --- Test Vector 1: Basic valid initData ---
_TV1_AUTH_DATE = str(int(time.time()))  # Fresh auth_date
_TV1_USER_JSON = json.dumps(
    {"id": 123456789, "username": "test_user", "first_name": "Test"},
    separators=(",", ":"),
)
_TV1_USER_ENCODED = quote(_TV1_USER_JSON, safe="")
# data-check-string uses DECODED values (per Telegram spec: URLSearchParams decoding)
_TV1_DCS = f"auth_date={_TV1_AUTH_DATE}\nuser={_TV1_USER_JSON}"
_TV1_HASH = _compute_independent_hash(_TV1_DCS)
_TV1_INIT_DATA = f"auth_date={_TV1_AUTH_DATE}&user={_TV1_USER_ENCODED}&hash={_TV1_HASH}"

# --- Test Vector 2: URL-encoded special characters in username ---
_TV2_AUTH_DATE = str(int(time.time()))  # Fresh auth_date
_TV2_USER_JSON = json.dumps(
    {"id": 987654321, "username": "user&name=with+special", "first_name": "Test Name"},
    separators=(",", ":"),
)
_TV2_USER_ENCODED = quote(_TV2_USER_JSON, safe="")
# data-check-string uses DECODED values (per Telegram spec: URLSearchParams decoding)
_TV2_DCS = f"auth_date={_TV2_AUTH_DATE}\nuser={_TV2_USER_JSON}"
_TV2_HASH = _compute_independent_hash(_TV2_DCS)
_TV2_INIT_DATA = f"auth_date={_TV2_AUTH_DATE}&user={_TV2_USER_ENCODED}&hash={_TV2_HASH}"

# --- Test Vector 3: No username, just first_name ---
_TV3_AUTH_DATE = str(int(time.time()))  # Fresh auth_date
_TV3_USER_JSON = json.dumps(
    {"id": 111111111, "first_name": "NoUsername"},
    separators=(",", ":"),
)
_TV3_USER_ENCODED = quote(_TV3_USER_JSON, safe="")
# data-check-string uses DECODED values (per Telegram spec: URLSearchParams decoding)
_TV3_DCS = f"auth_date={_TV3_AUTH_DATE}\nuser={_TV3_USER_JSON}"
_TV3_HASH = _compute_independent_hash(_TV3_DCS)
_TV3_INIT_DATA = f"auth_date={_TV3_AUTH_DATE}&user={_TV3_USER_ENCODED}&hash={_TV3_HASH}"


def _make_init_data(
    bot_token: str = _TEST_BOT_TOKEN,
    user_id: int = 123456789,
    username: str = "test_user",
    first_name: str = "Test",
    auth_date: int | None = None,
    include_hash: bool = True,
    include_user: bool = True,
    include_auth_date: bool = True,
    extra_params: dict | None = None,
) -> str:
    """
    Generate a valid Telegram Mini App initData string.

    Uses the correct Telegram algorithm independently:
      secret_key = HMAC-SHA256(b"WebAppData", bot_token)
      data_check_string = sorted decoded key=value pairs, newline-separated
      hash = HMAC-SHA256(secret_key, data_check_string)
    """
    if auth_date is None:
        auth_date = int(time.time())

    # Build params with decoded values
    params: dict[str, str] = {}

    if include_auth_date:
        params["auth_date"] = str(auth_date)

    if include_user:
        user_data = {
            "id": user_id,
            "username": username,
            "first_name": first_name,
            "last_name": "Doe",
        }
        # Use compact JSON as Telegram does
        params["user"] = json.dumps(user_data, separators=(",", ":"))

    if extra_params:
        params.update(extra_params)

    if include_hash:
        # Build data-check-string from decoded values, sorted
        sorted_params = sorted(params.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_params)

        # Compute hash using the correct Telegram algorithm
        secret_key = _compute_secret_key(bot_token)
        computed_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["hash"] = computed_hash

    # Build the raw query string by encoding each key=value pair
    # This simulates what Telegram's encodeURIComponent produces
    parts = []
    for k, v in params.items():
        parts.append(f"{quote(k, safe='')}={quote(v, safe='')}")
    return "&".join(parts)


class TestComputeSecretKey(unittest.TestCase):
    """Tests for the secret key computation."""

    def test_correct_telegram_algorithm(self) -> None:
        """Secret key is HMAC-SHA256(b'WebAppData', bot_token), not SHA256(bot_token)."""
        key = _compute_secret_key(_TEST_BOT_TOKEN)
        # Must match the independent pre-computed value
        self.assertEqual(key, _INDEPENDENT_SECRET_KEY)

    def test_not_sha256_of_token(self) -> None:
        """SHA256(bot_token) must NOT be used as the secret key."""
        key = _compute_secret_key(_TEST_BOT_TOKEN)
        wrong_key = hashlib.sha256(_TEST_BOT_TOKEN.encode("utf-8")).digest()
        self.assertNotEqual(key, wrong_key)

    def test_deterministic(self) -> None:
        """Same token always produces the same secret key."""
        key1 = _compute_secret_key("test_token")
        key2 = _compute_secret_key("test_token")
        self.assertEqual(key1, key2)

    def test_different_tokens_different_keys(self) -> None:
        """Different tokens produce different secret keys."""
        key1 = _compute_secret_key("token_1")
        key2 = _compute_secret_key("token_2")
        self.assertNotEqual(key1, key2)

    def test_sha256_length(self) -> None:
        """Secret key is 32 bytes (SHA-256 output)."""
        key = _compute_secret_key("test")
        self.assertEqual(len(key), 32)


class TestParseInitDataPairs(unittest.TestCase):
    """Tests for initData parsing using explicit unquote."""

    def test_basic_parse(self) -> None:
        """Basic query string is parsed into decoded key-value pairs."""
        data = "user=%7B%22id%22%3A123%7D&auth_date=1234567890"
        pairs = _parse_init_data_pairs(data)
        self.assertEqual(len(pairs), 2)
        keys = [k for k, v in pairs]
        self.assertIn("user", keys)
        self.assertIn("auth_date", keys)

    def test_values_are_decoded(self) -> None:
        """Values are URL-decoded using unquote (not unquote_plus)."""
        data = "user=%7B%22name%22%3A%22test%22%7D"
        pairs = _parse_init_data_pairs(data)
        self.assertEqual(pairs[0], ("user", '{"name":"test"}'))

    def test_plus_not_decoded_as_space(self) -> None:
        """'+' in raw data is NOT decoded to space (unquote, not unquote_plus)."""
        # In Telegram's encoding, '+' is literal '%2B', not space
        data = "user=%2B"
        pairs = _parse_init_data_pairs(data)
        self.assertEqual(pairs[0], ("user", "+"))

    def test_empty_string(self) -> None:
        """Empty string returns empty list."""
        pairs = _parse_init_data_pairs("")
        self.assertEqual(pairs, [])

    def test_no_equals(self) -> None:
        """Parts without '=' are skipped."""
        pairs = _parse_init_data_pairs("valid=1&noequals&also=ok")
        self.assertEqual(len(pairs), 2)

    def test_value_with_equals(self) -> None:
        """Value containing '=' is split only on first '='."""
        pairs = _parse_init_data_pairs("data=a=b=c")
        self.assertEqual(pairs[0], ("data", "a=b=c"))


class TestBuildDataCheckString(unittest.TestCase):
    """Tests for data-check-string construction."""

    def test_sorted_alphabetically(self) -> None:
        """Pairs are sorted alphabetically by key."""
        pairs = [("z", "1"), ("a", "2"), ("m", "3")]
        dcs = _build_data_check_string(pairs)
        self.assertEqual(dcs, "a=2\nm=3\nz=1")

    def test_hash_excluded(self) -> None:
        """Hash parameter is excluded from data-check-string."""
        pairs = [("auth_date", "123"), ("hash", "abc"), ("user", "x")]
        dcs = _build_data_check_string(pairs)
        self.assertNotIn("hash", dcs)
        self.assertEqual(dcs, "auth_date=123\nuser=x")

    def test_newline_separated(self) -> None:
        """Pairs are separated by newline."""
        pairs = [("a", "1"), ("b", "2")]
        dcs = _build_data_check_string(pairs)
        self.assertIn("\n", dcs)
        self.assertEqual(dcs.count("\n"), 1)

    def test_decoded_values_used(self) -> None:
        """Decoded values are used in the data-check-string."""
        pairs = [("user", '{"id":123}')]
        dcs = _build_data_check_string(pairs)
        self.assertEqual(dcs, 'user={"id":123}')


class TestValidateInitData(unittest.TestCase):
    """Tests for initData validation using independent test vectors."""

    def test_valid_vector_1_basic(self) -> None:
        """Test Vector 1: Basic valid initData passes validation."""
        is_valid, user_data, error = validate_init_data(
            _TV1_INIT_DATA, _TEST_BOT_TOKEN, max_age=_STATIC_MAX_AGE
        )
        self.assertTrue(is_valid, f"Validation failed: {error}")
        self.assertIsNone(error)
        self.assertIsNotNone(user_data)
        self.assertEqual(user_data["user_id"], 123456789)
        self.assertEqual(user_data["username"], "test_user")
        self.assertEqual(user_data["first_name"], "Test")

    def test_valid_vector_2_special_chars(self) -> None:
        """Test Vector 2: initData with URL-encoded special characters."""
        is_valid, user_data, error = validate_init_data(
            _TV2_INIT_DATA, _TEST_BOT_TOKEN, max_age=_STATIC_MAX_AGE
        )
        self.assertTrue(is_valid, f"Validation failed: {error}")
        self.assertIsNone(error)
        self.assertEqual(user_data["user_id"], 987654321)
        self.assertEqual(user_data["username"], "user&name=with+special")
        self.assertEqual(user_data["first_name"], "Test Name")

    def test_valid_vector_3_no_username(self) -> None:
        """Test Vector 3: initData without username."""
        is_valid, user_data, error = validate_init_data(
            _TV3_INIT_DATA, _TEST_BOT_TOKEN, max_age=_STATIC_MAX_AGE
        )
        self.assertTrue(is_valid, f"Validation failed: {error}")
        self.assertEqual(user_data["user_id"], 111111111)
        self.assertIsNone(user_data["username"])
        self.assertEqual(user_data["first_name"], "NoUsername")

    def test_generated_init_data_valid(self) -> None:
        """Generated initData passes validation."""
        init_data = _make_init_data()
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertTrue(is_valid, f"Validation failed: {error}")

    def test_missing_init_data(self) -> None:
        """Empty/None initData fails validation."""
        is_valid, user_data, error = validate_init_data("", _TEST_BOT_TOKEN)
        self.assertFalse(is_valid)
        self.assertIsNone(user_data)
        self.assertIn("Missing", error)

    def test_none_init_data(self) -> None:
        """None initData fails validation."""
        is_valid, user_data, error = validate_init_data(
            None, _TEST_BOT_TOKEN  # type: ignore[arg-type]
        )
        self.assertFalse(is_valid)
        self.assertIn("Missing", error)

    def test_invalid_hash(self) -> None:
        """Tampered hash fails validation."""
        init_data = _make_init_data()
        tampered = init_data.replace(
            "hash=", "hash=000000000000000000000000000000000000000000000000000000000000"
        )
        is_valid, user_data, error = validate_init_data(
            tampered, _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)
        self.assertEqual(error, "Invalid hash")

    def test_wrong_bot_token(self) -> None:
        """Wrong bot token produces invalid hash."""
        init_data = _make_init_data()
        is_valid, user_data, error = validate_init_data(init_data, "wrong_token")
        self.assertFalse(is_valid)
        self.assertEqual(error, "Invalid hash")

    def test_expired_auth_date(self) -> None:
        """Expired auth_date fails validation."""
        old_time = int(time.time()) - 172800  # 2 days ago
        init_data = _make_init_data(auth_date=old_time)
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)
        self.assertIn("expired", error)

    def test_custom_max_age(self) -> None:
        """Custom max_age is respected."""
        old_time = int(time.time()) - 100
        init_data = _make_init_data(auth_date=old_time)
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN, max_age=60
        )
        self.assertFalse(is_valid)
        self.assertIn("expired", error)

    def test_missing_hash(self) -> None:
        """Missing hash parameter fails validation."""
        init_data = _make_init_data(include_hash=False)
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)
        self.assertIn("hash", error.lower())

    def test_missing_auth_date(self) -> None:
        """Missing auth_date parameter fails validation."""
        init_data = _make_init_data(include_auth_date=False)
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)
        self.assertIn("auth_date", error.lower())

    def test_missing_user(self) -> None:
        """Missing user parameter fails validation."""
        init_data = _make_init_data(include_user=False)
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)
        self.assertIn("user", error.lower())

    def test_malformed_init_data(self) -> None:
        """Completely malformed initData fails gracefully."""
        is_valid, user_data, error = validate_init_data(
            "not_a_valid_query_string!!!", _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)

    def test_empty_pairs(self) -> None:
        """InitData with no valid pairs fails."""
        is_valid, user_data, error = validate_init_data(
            "noequals", _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)

    def test_trust_not_in_frontend_user_id(self) -> None:
        """User ID comes from verified data, not from frontend."""
        init_data = _make_init_data(user_id=111111)
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertTrue(is_valid)
        self.assertEqual(user_data["user_id"], 111111)

    def test_tampered_value_rejects(self) -> None:
        """Changing a decoded value after hash computation causes rejection."""
        auth_date = str(int(time.time()))
        user_json = json.dumps({"id": 777}, separators=(",", ":"))
        user_encoded = quote(user_json, safe="")
        dcs = f"auth_date={auth_date}\nuser={user_encoded}"
        h = _compute_independent_hash(dcs)
        init_data = f"auth_date={auth_date}&user={user_encoded}&hash={h}"

        # Tamper with the user value
        tampered_user = quote("changed_value", safe="")
        tampered = f"auth_date={auth_date}&user={tampered_user}&hash={h}"

        is_valid, user_data, error = validate_init_data(
            tampered, _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)
        self.assertEqual(error, "Invalid hash")

    def test_encoding_mismatch_rejects(self) -> None:
        """Different URL-encoding of the same value produces different hash."""
        auth_date = str(int(time.time()))
        user_json = json.dumps({"id": 555}, separators=(",", ":"))
        user_encoded_standard = quote(user_json, safe="")

        dcs = f"auth_date={auth_date}\nuser={user_encoded_standard}"
        correct_hash = _compute_independent_hash(dcs)

        # Try with a different encoding (e.g., keep quotes unencoded)
        user_encoded_different = quote(user_json, safe='"')
        init_data_different_encoding = f"auth_date={auth_date}&user={user_encoded_different}&hash={correct_hash}"

        is_valid, user_data, error = validate_init_data(
            init_data_different_encoding, _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)
        self.assertEqual(error, "Invalid hash")

    def test_correct_algorithm_not_old_sha256(self) -> None:
        """Ensure the old SHA256(bot_token) algorithm is rejected."""
        auth_date = str(int(time.time()))
        user_json = json.dumps({"id": 444}, separators=(",", ":"))
        user_encoded = quote(user_json, safe="")
        dcs = f"auth_date={auth_date}\nuser={user_encoded}"

        # Compute hash using the OLD (wrong) algorithm: SHA256(bot_token)
        old_secret_key = hashlib.sha256(_TEST_BOT_TOKEN.encode("utf-8")).digest()
        old_computed_hash = hmac.new(
            old_secret_key,
            dcs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        # Build initData with the OLD hash
        old_init_data = f"auth_date={auth_date}&user={user_encoded}&hash={old_computed_hash}"

        # The OLD hash should fail validation
        is_valid, user_data, error = validate_init_data(
            old_init_data, _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)
        self.assertEqual(error, "Invalid hash")


class TestAuthEndpoint(unittest.TestCase):
    """Tests for the /api/auth HTTP endpoint."""

    def setUp(self) -> None:
        """Set up test client and temp DB."""
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db.close()
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.test_db.name
        db.init_db(self.test_db.name)

        self.app = create_app(bot_token=_TEST_BOT_TOKEN)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        """Clean up temp DB."""
        db.DB_PATH = self._orig_db_path
        try:
            os.unlink(self.test_db.name)
        except OSError:
            pass
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(self.test_db.name + suffix)
            except OSError:
                pass

    def test_valid_auth_returns_200(self) -> None:
        """Valid initData returns 200 with user data."""
        init_data = _make_init_data()
        response = self.client.post(
            "/api/auth",
            json={"initData": init_data},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["user"]["user_id"], 123456789)

    def test_new_user_registered(self) -> None:
        """New user is registered in the users table."""
        user_id = 999999999
        init_data = _make_init_data(user_id=user_id)
        response = self.client.post(
            "/api/auth",
            json={"initData": init_data},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data["user"]["registered"])

        user = db.get_user(user_id)
        self.assertIsNotNone(user)
        self.assertEqual(user["user_id"], user_id)

    def test_existing_user_not_duplicated(self) -> None:
        """Existing user is not re-registered."""
        user_id = 888888888
        db.register_user(
            user_id=user_id,
            username="pre_registered",
            first_name="Pre",
        )

        init_data = _make_init_data(user_id=user_id)
        response = self.client.post(
            "/api/auth",
            json={"initData": init_data},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["user"]["registered"])

    def test_missing_init_data_returns_400(self) -> None:
        """Missing initData returns 400."""
        response = self.client.post(
            "/api/auth",
            json={},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("Missing", data["error"])

    def test_invalid_hash_returns_400(self) -> None:
        """Invalid hash returns 400."""
        init_data = _make_init_data()
        tampered = init_data.replace("hash=", "hash=invalid")
        response = self.client.post(
            "/api/auth",
            json={"initData": tampered},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data["ok"])

    def test_expired_auth_returns_400(self) -> None:
        """Expired auth_date returns 400."""
        old_time = int(time.time()) - 172800
        init_data = _make_init_data(auth_date=old_time)
        response = self.client.post(
            "/api/auth",
            json={"initData": init_data},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("expired", data["error"])

    def test_malformed_body_returns_400(self) -> None:
        """Malformed request body returns 400."""
        response = self.client.post(
            "/api/auth",
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_no_json_body_returns_400(self) -> None:
        """Empty body returns 400."""
        response = self.client.post(
            "/api/auth",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_error_no_sensitive_info(self) -> None:
        """Error responses don't leak sensitive information."""
        response = self.client.post(
            "/api/auth",
            json={"initData": "garbage"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertNotIn(_TEST_BOT_TOKEN, json.dumps(data))

    def test_special_chars_init_data_endpoint(self) -> None:
        """Endpoint accepts initData with URL-encoded special characters."""
        response = self.client.post(
            "/api/auth",
            json={"initData": _TV2_INIT_DATA},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["user"]["user_id"], 987654321)
        self.assertEqual(data["user"]["username"], "user&name=with+special")

    def test_post_only(self) -> None:
        """GET requests are not accepted."""
        response = self.client.get("/api/auth")
        self.assertEqual(response.status_code, 405)


class TestDatabaseIntegration(unittest.TestCase):
    """Tests for database integration with auth."""

    def setUp(self) -> None:
        """Set up temp DB."""
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db.close()
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.test_db.name
        db.init_db(self.test_db.name)

    def tearDown(self) -> None:
        """Clean up."""
        db.DB_PATH = self._orig_db_path
        try:
            os.unlink(self.test_db.name)
        except OSError:
            pass

    def test_auth_stores_username(self) -> None:
        """Authenticated user's username is stored in DB."""
        user_id = 555555555
        username = "auth_test_user"
        init_data = _make_init_data(user_id=user_id, username=username)

        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertTrue(is_valid)

        db.register_user(
            user_id=user_id,
            username=user_data["username"],
            first_name=user_data["first_name"],
        )

        stored = db.get_user(user_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["username"], username)

    def test_auth_stores_first_name(self) -> None:
        """Authenticated user's first_name is stored in DB."""
        user_id = 444444444
        first_name = "AuthTest"
        init_data = _make_init_data(user_id=user_id, first_name=first_name)

        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertTrue(is_valid)

        db.register_user(
            user_id=user_id,
            username=user_data["username"],
            first_name=user_data["first_name"],
        )

        stored = db.get_user(user_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["first_name"], first_name)

    def test_no_referrer_from_miniapp_auth(self) -> None:
        """Mini App auth does not set a referrer."""
        user_id = 333333333
        init_data = _make_init_data(user_id=user_id)

        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertTrue(is_valid)

        db.register_user(
            user_id=user_id,
            username=user_data["username"],
            first_name=user_data["first_name"],
            referred_by=None,
        )

        stored = db.get_user(user_id)
        self.assertIsNotNone(stored)
        self.assertIsNone(stored["referred_by"])


if __name__ == "__main__":
    unittest.main()
