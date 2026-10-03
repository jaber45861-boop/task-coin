"""Balance-guard tests for POST /api/task-requests (MT-TRANS-01).

Scope: creation-time only, server-side only.

- the guard parses the raw ``reward`` with the canonical exact parser
  (``task_creation.parse_reward_units``) and compares it against
  ``wallets.available_units`` — the ONE balance source (SQLite INTEGER,
  1 USDT = 100_000_000 atomic units) — BEFORE the INSERT, BEFORE any
  pending state and BEFORE ``notify_pending``
- balance < reward       -> HTTP 400 ``balance_insufficient``, zero
  rows written, no notify, wallet untouched
- balance == / > reward  -> success (unchanged happy path, notify path
  kept)
- missing wallet row     -> treated as 0 balance -> HTTP 400
- malformed reward       -> the guard defers to ``validate_payload``
  (``invalid_payload``), never a balance error
- validation failure     -> nothing persisted, notify never called
"""

import pytest

import db
import task_request_notifications
import task_request_store
import serve_miniapp
import wallet

from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"
USER_A = 3101
USER_B = 3102
USER_C = 3103  # registered WITHOUT a wallet row -> 0 balance

VALID_PAYLOAD = {
    "title": "متابعة حسابي على Instagram",
    "description": "تابع الحساب ثم أرسل إثبات المتابعة",
    "provider": "instagram",
    "action": "follow",
    "target_ref": "https://instagram.com/example",
    "reward": "0.5",
}

# reward_units: 1 USDT = 100_000_000 atomic units
W1 = 100_000_000
W05 = 50_000_000


def _pay(**overrides):
    payload = dict(VALID_PAYLOAD)
    payload.update(overrides)
    return payload


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(monkeypatch, tmp_path):
    """Isolated DB: A and B each hold exactly 1.00000000 USDT, C none."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    path = str(tmp_path / "balance_guard.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    db.register_user(USER_A, "alice", "Alice")
    db.register_user(USER_B, "bob", "Bob")
    db.register_user(USER_C, "carol", "Carol")
    wallet.credit_units(USER_A, W1)
    wallet.credit_units(USER_B, W1)
    yield path


@pytest.fixture
def client(db_path):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


def _auth(user_id: int = USER_A) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


# ── helpers ─────────────────────────────────────────────────────────────


def _create(client, user_id: int = USER_A, **overrides):
    response = client.post(
        "/api/task-requests",
        headers=_auth(user_id),
        json=_pay(**overrides),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["request"]


def _spy_notify(monkeypatch):
    """Replace ``notify_pending`` with a spy; returns the call list."""
    calls = []
    monkeypatch.setattr(
        task_request_notifications,
        "notify_pending",
        lambda request: calls.append(request),
    )
    return calls


def _count_requests(db_path, user_id):
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM user_task_requests WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row["n"])


def _available_units(db_path, user_id):
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT available_units FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row["available_units"]) if row else 0


# ═══════════════════════════════════════════════════════════════════════
# balance > reward / balance == reward — success path unchanged
# ═══════════════════════════════════════════════════════════════════════


class TestBalanceGreaterThanReward:
    def test_success_creates_pending_and_notifies(self, client, monkeypatch):
        calls = _spy_notify(monkeypatch)

        created = _create(client)  # reward 0.5 < balance 1.0

        assert created["status"] == "pending"
        assert created["reward_units"] == W05
        stored = task_request_store.get_request(created["request_id"])
        assert stored.user_id == USER_A
        assert stored.status == "pending"
        # The existing fail-soft notify path is still reached exactly
        # once on the happy path.
        assert len(calls) == 1
        assert calls[0].request_id == created["request_id"]
        assert calls[0].status == "pending"


class TestBalanceExactlyEqualReward:
    def test_success_creates_pending(self, client, monkeypatch):
        calls = _spy_notify(monkeypatch)

        created = _create(client, user_id=USER_B, reward="1")

        assert created["status"] == "pending"
        assert created["reward_units"] == W1
        stored = task_request_store.get_request(created["request_id"])
        assert stored.status == "pending"
        assert len(calls) == 1


# ═══════════════════════════════════════════════════════════════════════
# balance < reward — HTTP 400, nothing persisted, nothing notified
# ═══════════════════════════════════════════════════════════════════════


class TestBalanceLessThanReward:
    def test_http_400_no_record_no_notify_no_balance_change(
        self, client, monkeypatch, db_path
    ):
        calls = _spy_notify(monkeypatch)
        before = _available_units(db_path, USER_A)

        response = client.post(
            "/api/task-requests",
            headers=_auth(USER_A),
            json=_pay(reward="2"),  # 2.0 USDT > balance 1.0 USDT
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["ok"] is False
        assert body["error"] == "balance_insufficient"
        assert "متاح" in body["message"]
        assert body["available_balance"] == 1.0
        assert body["required_reward"] == 2.0
        # No record was written by the rejected attempt.
        assert _count_requests(db_path, USER_A) == 0
        # notify_pending was never reached.
        assert calls == []
        # The guard is read-only: the wallet itself is untouched.
        assert _available_units(db_path, USER_A) == before == W1


class TestMissingWalletRow:
    def test_zero_balance_without_wallet_row_is_rejected(
        self, client, monkeypatch, db_path
    ):
        calls = _spy_notify(monkeypatch)

        response = client.post(
            "/api/task-requests",
            headers=_auth(USER_C),
            json=_pay(reward="0.5"),
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["error"] == "balance_insufficient"
        assert body["available_balance"] == 0.0
        assert _count_requests(db_path, USER_C) == 0
        assert calls == []


class TestRejectedRequestPersistsNothing:
    def test_no_row_even_with_valid_payload(self, client, monkeypatch, db_path):
        """The wallet is read BEFORE the INSERT, so a low-balance
        caller gets 400 before ``task_request_store`` ever opens a
        transaction."""
        calls = _spy_notify(monkeypatch)

        response = client.post(
            "/api/task-requests",
            headers=_auth(USER_A),
            json=_pay(reward="2"),
        )

        assert response.status_code == 400
        assert response.get_json()["error"] == "balance_insufficient"
        assert _count_requests(db_path, USER_A) == 0
        assert calls == []


# ═══════════════════════════════════════════════════════════════════════
# server-side protection — keyed on the authenticated caller
# ═══════════════════════════════════════════════════════════════════════


class TestServerSideProtection:
    def test_same_payload_accepted_for_funded_user_rejected_for_broke_user(
        self, client, monkeypatch, db_path
    ):
        """The very same body succeeds for B (balance == reward) and is
        rejected for C (no balance): the decision is server-side and
        keyed on the verified identity — not a client artifact."""
        created_b = _create(client, user_id=USER_B, reward="1")
        assert created_b["reward_units"] == W1
        assert (
            task_request_store.get_request(created_b["request_id"]).status
            == "pending"
        )

        response = client.post(
            "/api/task-requests",
            headers=_auth(USER_C),
            json=_pay(reward="1"),
        )

        assert response.status_code == 400
        assert response.get_json()["error"] == "balance_insufficient"
        # C still has zero rows; B's row is untouched.
        assert _count_requests(db_path, USER_C) == 0
        assert _count_requests(db_path, USER_B) == 1


# ═══════════════════════════════════════════════════════════════════════
# validation failures still short-circuit before anything is persisted
# ═══════════════════════════════════════════════════════════════════════


class TestValidationFailureCreatesNothing:
    def test_invalid_payload_creates_no_record_and_no_notify(
        self, client, monkeypatch, db_path
    ):
        calls = _spy_notify(monkeypatch)

        response = client.post(
            "/api/task-requests",
            headers=_auth(USER_A),
            json=_pay(title=""),
        )

        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_payload"
        assert _count_requests(db_path, USER_A) == 0
        assert calls == []


class TestMalformedRewardDefersToValidation:
    def test_unparseable_reward_is_invalid_payload_not_balance_error(
        self, client, monkeypatch, db_path
    ):
        """A reward the exact parser rejects cannot be compared, so the
        guard defers to ``validate_payload`` — the user gets the normal
        validation error, never a misleading balance error."""
        calls = _spy_notify(monkeypatch)

        response = client.post(
            "/api/task-requests",
            headers=_auth(USER_C),  # zero balance
            json=_pay(reward="abc"),
        )

        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_payload"
        assert _count_requests(db_path, USER_C) == 0
        assert calls == []
