"""
Tests for Telegram Mini App Authentication Backend.

Run:
    python -m pytest test_miniapp_auth.py -v
    # or
    python -m unittest test_miniapp_auth.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlencode, quote

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
    _parse_init_data,
    _extract_flat_params,
)

# Test constants
_TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
_TEST_USER_ID = 123456789
_TEST_USERNAME = "test_user"
_TEST_FIRST_NAME = "Test"


def _make_init_data(
    bot_token: str = _TEST_BOT_TOKEN,
    user_id: int = _TEST_USER_ID,
    username: str = _TEST_USERNAME,
    first_name: str = _TEST_FIRST_NAME,
    auth_date: int | None = None,
    include_hash: bool = True,
    include_user: bool = True,
    include_auth_date: bool = True,
    extra_params: dict | None = None,
) -> str:
    """
    Generate a valid Telegram Mini App initData string.

    This simulates what Telegram generates for a Mini App.
    """
    if auth_date is None:
        auth_date = int(time.time())

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
        params["user"] = json.dumps(user_data)

    if extra_params:
        params.update(extra_params)

    # Compute hash
    if include_hash:
        sorted_params = sorted(params.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_params)
        secret_key = _compute_secret_key(bot_token)
        computed_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["hash"] = computed_hash

    return urlencode(params)


class TestComputeSecretKey(unittest.TestCase):
    """Tests for the secret key computation."""

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


class TestParseInitData(unittest.TestCase):
    """Tests for initData parsing."""

    def test_basic_parse(self) -> None:
        """Basic query string is parsed correctly."""
        data = "user=%7B%22id%22%3A123%7D&auth_date=1234567890"
        parsed = _parse_init_data(data)
        self.assertIn("user", parsed)
        self.assertIn("auth_date", parsed)

    def test_empty_string(self) -> None:
        """Empty string returns empty dict."""
        parsed = _parse_init_data("")
        self.assertEqual(parsed, {})

    def test_extract_flat_params(self) -> None:
        """Flatten parse_qs output correctly."""
        parsed = {"auth_date": ["1234567890"], "user": ["test"]}
        flat = _extract_flat_params(parsed)
        self.assertEqual(flat, {"auth_date": "1234567890", "user": "test"})


class TestValidateInitData(unittest.TestCase):
    """Tests for initData validation logic."""

    def test_valid_init_data(self) -> None:
        """Valid initData passes validation."""
        init_data = _make_init_data()
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertTrue(is_valid)
        self.assertIsNone(error)
        self.assertIsNotNone(user_data)
        self.assertEqual(user_data["user_id"], _TEST_USER_ID)
        self.assertEqual(user_data["username"], _TEST_USERNAME)
        self.assertEqual(user_data["first_name"], _TEST_FIRST_NAME)

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
        # Tamper with the hash
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
        init_data = _make_init_data(bot_token=_TEST_BOT_TOKEN)
        is_valid, user_data, error = validate_init_data(
            init_data, "wrong_token"
        )
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
        # With max_age=60, 100 seconds old should fail
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

    def test_invalid_auth_date_format(self) -> None:
        """Non-numeric auth_date fails validation."""
        init_data = _make_init_data()
        # Replace auth_date with non-numeric
        init_data = init_data.replace("auth_date=", "auth_date=abc")
        # Recompute hash for the modified data
        parsed = _parse_init_data(init_data)
        flat = _extract_flat_params(parsed)
        flat.pop("hash", None)
        sorted_params = sorted(flat.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_params)
        secret_key = _compute_secret_key(_TEST_BOT_TOKEN)
        computed_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        flat["hash"] = computed_hash
        new_init_data = urlencode(flat)

        is_valid, user_data, error = validate_init_data(
            new_init_data, _TEST_BOT_TOKEN
        )
        # The hash will be computed with auth_date=abc but auth_date
        # is not a valid int, so it should fail
        self.assertFalse(is_valid)
        self.assertIn("auth_date", error.lower())

    def test_missing_user(self) -> None:
        """Missing user parameter fails validation."""
        # Build params without user
        auth_date = int(time.time())
        params = {"auth_date": str(auth_date)}
        sorted_params = sorted(params.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_params)
        secret_key = _compute_secret_key(_TEST_BOT_TOKEN)
        computed_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["hash"] = computed_hash
        init_data = urlencode(params)

        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)
        self.assertIn("user", error.lower())

    def test_malformed_user_json(self) -> None:
        """Malformed user JSON fails validation."""
        auth_date = int(time.time())
        params = {
            "auth_date": str(auth_date),
            "user": "not_valid_json",
        }
        sorted_params = sorted(params.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_params)
        secret_key = _compute_secret_key(_TEST_BOT_TOKEN)
        computed_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["hash"] = computed_hash
        init_data = urlencode(params)

        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertFalse(is_valid)
        self.assertIn("JSON", error)

    def test_user_id_extraction(self) -> None:
        """User ID is correctly extracted from verified data."""
        user_id = 987654321
        init_data = _make_init_data(user_id=user_id)
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertTrue(is_valid)
        self.assertEqual(user_data["user_id"], user_id)

    def test_username_extraction(self) -> None:
        """Username is correctly extracted from verified data."""
        username = "my_test_user"
        init_data = _make_init_data(username=username)
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertTrue(is_valid)
        self.assertEqual(user_data["username"], username)

    def test_first_name_extraction(self) -> None:
        """First name is correctly extracted from verified data."""
        first_name = "Alice"
        init_data = _make_init_data(first_name=first_name)
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertTrue(is_valid)
        self.assertEqual(user_data["first_name"], first_name)

    def test_malformed_init_data(self) -> None:
        """Completely malformed initData fails gracefully."""
        is_valid, user_data, error = validate_init_data(
            "not_a_valid_query_string!!!", _TEST_BOT_TOKEN
        )
        # Should either fail with hash missing or invalid hash
        self.assertFalse(is_valid)

    def test_trust_not_in_frontend_user_id(self) -> None:
        """User ID comes from verified data, not from frontend."""
        # Create initData with one user_id
        init_data = _make_init_data(user_id=111111)
        is_valid, user_data, error = validate_init_data(
            init_data, _TEST_BOT_TOKEN
        )
        self.assertTrue(is_valid)
        # The user_id should match what's in the initData, not any frontend value
        self.assertEqual(user_data["user_id"], 111111)


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
        self.assertEqual(data["user"]["user_id"], _TEST_USER_ID)
        self.assertEqual(data["user"]["username"], _TEST_USERNAME)
        self.assertEqual(data["user"]["first_name"], _TEST_FIRST_NAME)

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

        # Verify user exists in DB
        user = db.get_user(user_id)
        self.assertIsNotNone(user)
        self.assertEqual(user["user_id"], user_id)

    def test_existing_user_not_duplicated(self) -> None:
        """Existing user is not re-registered."""
        user_id = 888888888
        # Pre-register user
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
        # Tamper with hash
        tampered = init_data.replace(
            "hash=", "hash=invalid"
        )
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
        # Should not contain bot token or secret info
        self.assertNotIn(_TEST_BOT_TOKEN, json.dumps(data))

    def test_username_optional_in_response(self) -> None:
        """Response handles user without username."""
        user_id = 777777777
        init_data = _make_init_data(user_id=user_id, username="")
        response = self.client.post(
            "/api/auth",
            json={"initData": init_data},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])

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

        # Register user as the endpoint would
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
