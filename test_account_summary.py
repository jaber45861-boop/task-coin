"""
Focused tests — GET /api/me account summary (Mini App «حسابي»)
===============================================================

The Account page shows the caller's REAL figures — and only real
ones.  Every number this endpoint returns is read from an existing
source of truth:

  Authentication
  - unauthenticated request rejected
  - invalid initData rejected
  - the authenticated Telegram initData user is the identity
  - a client-supplied user_id cannot impersonate another user

  Balance
  - availableUnits is the exact wallets.available_units value
  - held funds are never part of the available balance
  - a user without a wallet row reads as a REAL zero (the wallet
    service contract), never as "unavailable"

  Task counts
  - completedTasks counts the caller's own completed user_tasks
  - inProgressTasks counts the caller's started rows only
  - available rows count towards neither
  - other users' rows never leak in

  Earnings
  - earnedUnits sums the ledger's credit/task entries exactly
  - deposit credits are excluded (a deposit is not an earning)
  - other users' entries never leak in
  - no earnings reads as a REAL zero

  Integration
  - a real channel-task completion (start → submit → CompletionGate
    → TaskRewardService) is reflected exactly: one completed task,
    the reward in the balance AND in the lifetime earnings

  Read-only
  - a GET changes no wallet, ledger, user_tasks or users state

Run:
    python3 -m pytest test_account_summary.py -v
"""

import json

import pytest

import db
import serve_miniapp
import wallet
from account_summary import load_summary
from channel_task_verifier import (
    CHANNEL_TASK_TYPE,
    ChannelTaskVerifier,
    register_channel_task_verifier,
)
from config import CHANNELS, Channel
from ledger import LedgerService
from task_reward import TASK_REFERENCE_TYPE
from wallet import USDT_SCALE

# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

USER_A = 1001
USER_B = 2002

CHANNEL_SLUG = "main"
CHANNEL_ID = -100111
CHANNEL_USERNAME = "taskcoin_ch"


# ── Membership checker (replaces the live Telegram lookup) ───


class _Members:
    """Fake Telegram membership lookup recording every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.statuses: dict[int, str] = {}
        self.error: Exception | None = None

    def __call__(self, channel_id: int, user_id: int) -> str:
        self.calls.append((channel_id, user_id))
        if self.error is not None:
            raise self.error
        return self.statuses.get(user_id, "left")


@pytest.fixture
def members():
    return _Members()


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path, members):
    """Environment + isolated database + configured channel + fake
    verifier (no real Telegram API call is ever made)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)

    db_path = str(tmp_path / "account_summary_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_A, "alice", "Alice")
    db.register_user(USER_B, "bob", "Bob")

    CHANNELS.clear()
    CHANNELS[CHANNEL_SLUG] = Channel(
        slug=CHANNEL_SLUG,
        channel_id=CHANNEL_ID,
        username=CHANNEL_USERNAME,
        title="TaskCoin",
        required=True,
    )
    register_channel_task_verifier(
        ChannelTaskVerifier(membership_checker=members)
    )

    yield db_path

    CHANNELS.clear()
    register_channel_task_verifier()  # restore the default registration


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


def _auth(user_id: int = USER_A) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


def _create_task(reward: int = 10) -> int:
    return db.create_task(
        title="مهمة بسيطة",
        description="وصف المهمة",
        task_type="deterministic",
        reward=reward,
        active=True,
        task_data=json.dumps({"expected": "secret"}),
    )


def _create_channel_task(active: bool = True) -> int:
    return db.create_task(
        title="اشترك في القناة الرسمية",
        description="انضم إلى قناة تيليجرام وأكّد الاشتراك",
        task_type=CHANNEL_TASK_TYPE,
        reward=50,
        active=active,
        task_data=json.dumps({"channel_slug": CHANNEL_SLUG}),
    )


def _set_status(user_id: int, task_id: int, status: str) -> None:
    """Put one user_task row into the requested state."""
    db.create_user_task(user_id, task_id)
    if status == db.USER_TASK_STATUS_AVAILABLE:
        return
    db.update_user_task_status(
        user_id, task_id, db.USER_TASK_STATUS_STARTED
    )
    if status == db.USER_TASK_STATUS_COMPLETED:
        db.update_user_task_status(
            user_id,
            task_id,
            db.USER_TASK_STATUS_COMPLETED,
            _allow_completion=True,
        )


def _credit_task_earnings(
    user_id: int, units: int, reference_id: str
) -> None:
    """Mirror exactly what TaskRewardService writes on a completion:
    one wallet credit plus one ledger credit/task entry."""
    wallet.credit_units(user_id, units)
    LedgerService().record_credit(
        user_id,
        amount_units=units,
        reference_type=TASK_REFERENCE_TYPE,
        reference_id=reference_id,
        idempotency_key=f"task_reward:{user_id}:{reference_id}",
    )


def _snapshot() -> dict:
    """Every money/state table, as comparable tuples."""
    with db.get_connection() as conn:
        return {
            "users": [
                tuple(r) for r in conn.execute(
                    "SELECT user_id FROM users ORDER BY user_id"
                )
            ],
            "wallets": [
                tuple(r) for r in conn.execute(
                    "SELECT user_id, available_units, held_units "
                    "FROM wallets ORDER BY user_id"
                )
            ],
            "ledger": [
                tuple(r) for r in conn.execute(
                    "SELECT user_id, entry_type, amount_units, "
                    "       reference_type, reference_id "
                    "FROM ledger ORDER BY id"
                )
            ],
            "user_tasks": [
                tuple(r) for r in conn.execute(
                    "SELECT user_id, task_id, status "
                    "FROM user_tasks ORDER BY user_id, task_id"
                )
            ],
        }


# ════════════════════════════════════════════════════════════
# Authentication
# ════════════════════════════════════════════════════════════


class TestAuthentication:
    def test_unauthenticated_rejected(self, client):
        response = client.get("/api/me")
        assert response.status_code == 401
        data = response.get_json()
        assert data["ok"] is False
        assert data["error"] == "unauthenticated"
        assert data["message"]  # Arabic, user-facing

    def test_invalid_init_data_rejected(self, client):
        response = client.get(
            "/api/me", headers={INIT_DATA_HEADER: "not-valid"}
        )
        assert response.status_code == 401
        assert response.get_json()["error"] == "unauthenticated"

    def test_identity_comes_from_init_data(self, client):
        response = client.get("/api/me", headers=_auth(USER_A))
        assert response.status_code == 200
        assert response.get_json()["user"]["id"] == USER_A

    def test_client_user_id_cannot_impersonate(self, client):
        """A query user_id is ignored — initData decides identity
        and every figure stays the initData user's own."""
        _set_status(USER_B, _create_task(),
                    db.USER_TASK_STATUS_COMPLETED)
        _credit_task_earnings(USER_B, 5_000_000_000, "2:1")

        response = client.get(
            "/api/me",
            headers=_auth(USER_A),
            query_string={"user_id": USER_B},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["user"]["id"] == USER_A
        assert data["stats"]["completedTasks"] == 0
        assert data["stats"]["earnedUnits"] == 0
        assert data["wallet"]["availableUnits"] == 0


# ════════════════════════════════════════════════════════════
# Balance
# ════════════════════════════════════════════════════════════


class TestBalance:
    def test_no_wallet_reads_as_a_real_zero(self, client):
        """A user without a wallet row has a REAL zero balance —
        the wallet service contract — not an unknown value."""
        response = client.get("/api/me", headers=_auth())
        assert response.status_code == 200
        assert response.get_json()["wallet"]["availableUnits"] == 0

    def test_available_balance_is_exact(self, client):
        wallet.ensure_wallet(USER_A)
        wallet.credit_units(USER_A, 12_500_000_000)  # 125.0 USDT
        response = client.get("/api/me", headers=_auth())
        assert response.get_json()["wallet"]["availableUnits"] == (
            12_500_000_000
        )

    def test_held_funds_are_not_the_available_balance(self, client):
        wallet.ensure_wallet(USER_A)
        wallet.credit_units(USER_A, 1_000_000_000)  # 10 USDT
        wallet.reserve(USER_A, "2.5")  # 2.5 USDT moved to held
        response = client.get("/api/me", headers=_auth())
        # available = 10 - 2.5 = 7.5 USDT, held never shown
        assert response.get_json()["wallet"]["availableUnits"] == (
            750_000_000
        )


# ════════════════════════════════════════════════════════════
# Task counts
# ════════════════════════════════════════════════════════════


class TestTaskCounts:
    def test_counts_reflect_own_rows_only(self, client):
        completed = _create_task()
        started = _create_task()
        available = _create_task()
        _set_status(USER_A, completed, db.USER_TASK_STATUS_COMPLETED)
        _set_status(USER_A, started, db.USER_TASK_STATUS_STARTED)
        _set_status(USER_A, available, db.USER_TASK_STATUS_AVAILABLE)
        # USER_B's rows never leak into USER_A's summary.
        _set_status(USER_B, _create_task(),
                    db.USER_TASK_STATUS_COMPLETED)

        response = client.get("/api/me", headers=_auth())
        stats = response.get_json()["stats"]
        assert stats["completedTasks"] == 1
        assert stats["inProgressTasks"] == 1

    def test_available_rows_count_towards_neither(self, client):
        _set_status(USER_A, _create_task(),
                    db.USER_TASK_STATUS_AVAILABLE)
        response = client.get("/api/me", headers=_auth())
        stats = response.get_json()["stats"]
        assert stats["completedTasks"] == 0
        assert stats["inProgressTasks"] == 0

    def test_user_with_no_tasks(self, client):
        response = client.get("/api/me", headers=_auth())
        stats = response.get_json()["stats"]
        assert stats["completedTasks"] == 0
        assert stats["inProgressTasks"] == 0


# ════════════════════════════════════════════════════════════
# Earnings
# ════════════════════════════════════════════════════════════


class TestEarnings:
    def test_earned_units_sum_task_credits(self, client):
        _credit_task_earnings(USER_A, 1_250_000_000, "1:7")
        _credit_task_earnings(USER_A, 750_000_000, "1:8")
        response = client.get("/api/me", headers=_auth())
        assert response.get_json()["stats"]["earnedUnits"] == (
            2_000_000_000
        )

    def test_deposit_credits_are_not_earnings(self, client):
        """A deposit credits the wallet but is NOT an earning:
        it stays out of earnedUnits while honestly counting in
        the available balance."""
        _credit_task_earnings(USER_A, 1_000_000_000, "1:7")
        wallet.credit_units(USER_A, 5_000_000_000)
        LedgerService().record_credit(
            USER_A,
            amount_units=5_000_000_000,
            reference_type="deposit",
            reference_id="deposit:tx-1",
            idempotency_key="deposit:tx-1",
        )

        response = client.get("/api/me", headers=_auth())
        data = response.get_json()
        assert data["stats"]["earnedUnits"] == 1_000_000_000
        assert data["wallet"]["availableUnits"] == 6_000_000_000

    def test_other_users_earnings_never_leak(self, client):
        _credit_task_earnings(USER_B, 9_000_000_000, "2:1")
        response = client.get("/api/me", headers=_auth())
        assert response.get_json()["stats"]["earnedUnits"] == 0

    def test_no_earnings_reads_as_a_real_zero(self, client):
        response = client.get("/api/me", headers=_auth())
        assert response.get_json()["stats"]["earnedUnits"] == 0


# ════════════════════════════════════════════════════════════
# Integration — the real completion pipeline
# ════════════════════════════════════════════════════════════


class TestRealPipeline:
    def test_completion_through_the_http_pipeline_feeds_the_summary(
        self, client, members
    ):
        """A real channel-task completion (start → submit →
        CompletionGate → TaskRewardService) is reflected exactly:
        one completed task, the server-defined reward in the
        balance AND in the lifetime earnings."""
        task_id = _create_channel_task()   # reward=50 (whole USDT)
        members.statuses[USER_A] = "member"
        client.post(f"/api/tasks/{task_id}/start", headers=_auth())
        response = client.post(
            f"/api/tasks/{task_id}/submit", headers=_auth()
        )
        assert response.status_code == 200

        response = client.get("/api/me", headers=_auth())
        data = response.get_json()
        assert data["stats"]["completedTasks"] == 1
        assert data["stats"]["inProgressTasks"] == 0
        assert data["stats"]["earnedUnits"] == 50 * USDT_SCALE
        assert data["wallet"]["availableUnits"] == 50 * USDT_SCALE


# ════════════════════════════════════════════════════════════
# Service composition (the read-only account_summary service)
# ════════════════════════════════════════════════════════════


class TestServiceComposition:
    """The service composes every figure from the existing
    sources of truth — real zeros for a fresh account."""

    def test_service_shape_and_real_zeros(self, env):
        assert load_summary(USER_A) == {
            "wallet": {"available_units": 0},
            "stats": {
                "completed_tasks": 0,
                "in_progress_tasks": 0,
                "earned_units": 0,
            },
        }

    def test_service_composes_all_figures(self, env):
        completed = _create_task()
        started = _create_task()
        _set_status(USER_A, completed,
                    db.USER_TASK_STATUS_COMPLETED)
        _set_status(USER_A, started,
                    db.USER_TASK_STATUS_STARTED)
        _credit_task_earnings(USER_A, 1_250_000_000, "1:7")
        # A deposit raises the balance but is NOT an earning.
        wallet.credit_units(USER_A, 5_000_000_000)

        summary = load_summary(USER_A)
        assert summary["wallet"]["available_units"] == (
            6_250_000_000
        )
        assert summary["stats"]["completed_tasks"] == 1
        assert summary["stats"]["in_progress_tasks"] == 1
        assert summary["stats"]["earned_units"] == 1_250_000_000


# ════════════════════════════════════════════════════════════
# Response shape + read-only contract
# ════════════════════════════════════════════════════════════


class TestResponseShape:
    def test_safe_fields_only(self, client):
        response = client.get("/api/me", headers=_auth())
        data = response.get_json()
        assert data["ok"] is True
        assert set(data.keys()) == {"ok", "user", "wallet", "stats"}
        assert set(data["user"].keys()) == {"id", "username", "firstName"}
        assert set(data["wallet"].keys()) == {"availableUnits"}
        assert set(data["stats"].keys()) == {
            "completedTasks", "inProgressTasks", "earnedUnits",
        }

    def test_money_is_integer_units_never_float(self, client):
        _credit_task_earnings(USER_A, 1_250_000_000, "1:7")
        response = client.get("/api/me", headers=_auth())
        data = response.get_json()
        assert isinstance(data["wallet"]["availableUnits"], int)
        assert isinstance(data["stats"]["earnedUnits"], int)
        # no float notation leaks into the wire format
        assert ".0" not in response.get_data(as_text=True)


class TestReadOnly:
    def test_get_changes_no_state(self, client):
        """The summary is a pure read: wallet, ledger, user_tasks
        and users are byte-identical before and after."""
        _set_status(USER_A, _create_task(),
                    db.USER_TASK_STATUS_STARTED)
        _credit_task_earnings(USER_A, 1_000_000_000, "1:7")
        before = _snapshot()

        client.get("/api/me", headers=_auth())
        client.get("/api/me", headers=_auth(USER_B))
        client.get("/api/me")  # unauthenticated attempt

        assert _snapshot() == before


class TestSourceContract:
    def test_route_source_stays_read_only(self):
        """Source-level guard: the account summary performs no
        user_tasks/wallet/ledger mutation and never references
        the financial primitives the transport guard bans here —
        the figures are composed by the read-only
        ``account_summary`` service."""
        with open("task_routes.py", "r", encoding="utf-8") as fh:
            source = fh.read()
        assert '@tasks_bp.get("/api/me")' in source
        assert "UPDATE user_tasks" not in source
        assert "INSERT INTO user_tasks" not in source
        assert "update_user_task_status" not in source
        assert "CompletionGate(" not in source
        assert "credit_units(" not in source
        assert "record_credit(" not in source
        assert "LedgerService" not in source
        assert "load_summary(user_id)" in source

    def test_service_composes_only_existing_read_apis(self):
        """The service reads ONLY through the existing
        services' public read APIs — no SQL of its own, no
        mutation call, no money logic."""
        with open("account_summary.py", "r", encoding="utf-8") as fh:
            source = fh.read()
        assert "wallet.wallet_units(user_id)" in source
        assert "db.list_user_tasks(user_id)" in source
        assert "LedgerService().list_user_entries(user_id)" in source
        for banned in (
            "INSERT INTO", "UPDATE ", "DELETE FROM",
            "BEGIN IMMEDIATE", "COMMIT",
            "credit_units(", "reserve(", "record_credit(",
            "record_debit(", "record_hold(", "record_release(",
            "record_settlement(", "ensure_wallet(",
        ):
            assert banned not in source, \
                f"mutation/SQL in the read-only service: {banned}"
