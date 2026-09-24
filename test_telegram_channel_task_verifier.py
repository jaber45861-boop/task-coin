"""
Focused tests — telegram_channel task family (MT-TASK-05)
==========================================================

Brings the old repo's Manual Telegram Task behavior into the new Task
architecture, covered end to end:

Task definition (server-side task_data contract)
- valid telegram_channel task_data validates
- missing / wrong provider rejected
- missing / wrong action rejected
- missing / malformed target rejected
- missing / unsafe channel identifier rejected (URLs, @usernames,
  numeric ids and whitespace are never valid targets)
- reward inside task_data rejected (reward lives in tasks.reward)
- unexpected keys rejected
- instructions validated as bounded presentation data
- malformed / missing JSON task_data rejected

Verifier
- registered for telegram_channel (own family, distinct from
  channel_subscription) through the existing registry mechanism
- `import task_verifier` alone resolves telegram_channel (subprocess)
- valid member (member / administrator / creator) → PASSED
- non-member (left / kicked / restricted) → FAILED
- supergroup membership semantics (same as the subscription gate)
- Telegram / infrastructure failure → ERROR
- unconfigured target → ERROR
- malformed definition → ERROR with NO membership check
- client submission data can never override the trusted target
- verifier is side-effect free (no user_tasks / submissions /
  wallet / ledger writes, never completes a task)

Lifecycle (existing pipeline, provider-agnostic)
- available → started
- submit valid membership → completed, reward credited exactly once
- failed membership → stays started, attempt persisted, no reward
- ERROR → stays started, attempt persisted, no reward
- retry after a failed attempt works
- one_time completion is terminal
- repeatable task completes again only after the cooldown

Security
- client cannot change reward / target / identity / completion
- client cannot force PASSED

Mini App API
- safe public join URL for telegram_channel resolved server-side
- no task_data / slug / id leakage in safe API surfaces

No real Telegram API call is ever made — membership checkers are fakes.

Run:
    python3 -m pytest test_telegram_channel_task_verifier.py -v
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys

import pytest
from telegram.error import TelegramError

import db
import wallet
from channel_task_verifier import (
    CHANNEL_TASK_TYPE,
    ChannelTaskVerifier,
    register_channel_task_verifier,
)
from completion_bridge import CompletionBridge
from config import CHANNELS, Channel
from task_catalog import TaskCatalog
from task_completion import VerificationResult, VerificationStatus
from task_lifecycle import TaskLifecycle
from task_start import StartGateError, TaskStartGate
from task_submission import SubmissionError, TaskSubmissionService
from task_verifier import (
    DeterministicTaskVerifier,
    clear_verifiers,
    get_verifier,
    register_verifier,
    verify_task,
)
from telegram_channel_task_verifier import (
    MAX_INSTRUCTIONS_LENGTH,
    TELEGRAM_CHANNEL_TASK_TYPE,
    TelegramChannelTaskDataError,
    TelegramChannelTaskVerifier,
    parse_telegram_channel_task_data,
    register_telegram_channel_task_verifier,
    task_channel_slug,
    validate_telegram_channel_task_data,
)

_USER_ID = 1001
_ADMIN_CHANNEL_ID = -1001234567890
_ATTACKER_CHANNEL_ID = -999999999
_GROUP_ID = -1009876543210
_REWARD_USDT = 50
_REWARD_UNITS = _REWARD_USDT * wallet.USDT_SCALE  # 5,000,000,000
_REPEAT_HOURS = 24


# ── Contract helpers ──────────────────────────────────────────────


def _valid_task_data(
    slug: str = "main",
    instructions: str = "انضم إلى القناة ثم اضغط على تحقق وإتمام",
) -> dict:
    """A contract-conformant telegram_channel task_data payload."""
    return {
        "provider": "telegram",
        "action": "join_channel",
        "target": {"channel_slug": slug},
        "instructions": instructions,
    }


# ── Fixture ───────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Temp DB, one user, configured channels, one valid task."""
    db_path = str(tmp_path / "telegram_channel_task.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(_USER_ID, "alice", "Alice")

    CHANNELS.clear()
    CHANNELS["main"] = Channel(
        slug="main",
        channel_id=_ADMIN_CHANNEL_ID,
        username="main_user",
        title="Main Channel",
        required=True,
    )

    # Registry isolation: known state before every test — both task
    # families registered, exactly like production.
    clear_verifiers()
    register_channel_task_verifier()
    register_telegram_channel_task_verifier()

    task_id = db.create_task(
        title="Join Our Channel",
        description="Subscribe to our Telegram channel",
        task_type=TELEGRAM_CHANNEL_TASK_TYPE,
        reward=_REWARD_USDT,
        task_data=json.dumps(_valid_task_data()),
    )

    yield {"db_path": db_path, "task_id": task_id}

    # Never leak a custom registry into other suites.
    clear_verifiers()
    register_verifier("deterministic", DeterministicTaskVerifier())
    register_channel_task_verifier()
    register_telegram_channel_task_verifier()
    CHANNELS.clear()


# ── Helpers ───────────────────────────────────────────────────────


def _register_fake(status: str = "member"):
    """Register a verifier whose membership check is a fake.

    Returns the call list: [(channel_id, user_id), ...].
    """
    calls: list[tuple[int, int]] = []

    def checker(channel_id: int, user_id: int) -> str:
        calls.append((channel_id, user_id))
        return status

    register_telegram_channel_task_verifier(
        TelegramChannelTaskVerifier(membership_checker=checker)
    )
    return calls


def _register_raising(exc: BaseException):
    """Register a verifier whose membership check raises *exc*."""
    calls: list[tuple[int, int]] = []

    def checker(channel_id: int, user_id: int) -> str:
        calls.append((channel_id, user_id))
        raise exc

    register_telegram_channel_task_verifier(
        TelegramChannelTaskVerifier(membership_checker=checker)
    )
    return calls


def _make_task(task_data, *, reward: int = 25, task_type: str | None = None,
               **kwargs) -> int:
    """Create an extra task with the given raw task_data."""
    if task_data is None:
        raw = None
    elif isinstance(task_data, str):
        raw = task_data
    else:
        raw = json.dumps(task_data)
    return db.create_task(
        title="Telegram Channel Task",
        description="Join the target channel",
        task_type=task_type or TELEGRAM_CHANNEL_TASK_TYPE,
        reward=reward,
        task_data=raw,
        **kwargs,
    )


def _status(task_id: int) -> str | None:
    row = db.get_user_task(_USER_ID, task_id)
    return row["status"] if row is not None else None


def _wallet_units(user_id: int = _USER_ID) -> int:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT available_units FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return 0 if row is None else row["available_units"]


def _task_credits(user_id: int = _USER_ID) -> list[dict]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT amount_units, available_delta FROM ledger "
            "WHERE user_id = ? AND entry_type = 'credit' "
            "AND reference_type = 'task' ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _submissions(task_id: int, user_id: int = _USER_ID) -> list[dict]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT status, verification_reason, completed_at "
            "FROM task_submissions "
            "WHERE user_id = ? AND task_id = ? ORDER BY submission_id",
            (user_id, task_id),
        ).fetchall()
    return [dict(r) for r in rows]


def _backdate_completed(task_id: int, hours: int,
                        user_id: int = _USER_ID) -> None:
    """Move the persisted completed_at into the past (server-side data)."""
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE user_tasks SET completed_at = datetime('now', ?) "
            "WHERE user_id = ? AND task_id = ?",
            (f"-{hours} hours", user_id, task_id),
        )


# ══════════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════════


class TestRegistration:
    def test_task_type_is_distinct_from_channel_subscription(self):
        """telegram_channel is its own family — never an alias."""
        assert TELEGRAM_CHANNEL_TASK_TYPE == "telegram_channel"
        assert TELEGRAM_CHANNEL_TASK_TYPE != CHANNEL_TASK_TYPE

    def test_registered_on_import(self, env):
        verifier = get_verifier(TELEGRAM_CHANNEL_TASK_TYPE)
        assert verifier is not None
        assert isinstance(verifier, TelegramChannelTaskVerifier)

    def test_channel_family_stays_separate(self, env):
        """Registering telegram never touches channel_subscription."""
        register_telegram_channel_task_verifier()
        channel = get_verifier(CHANNEL_TASK_TYPE)
        assert isinstance(channel, ChannelTaskVerifier)

    def test_reregistration_after_clear(self, env):
        clear_verifiers()
        assert get_verifier(TELEGRAM_CHANNEL_TASK_TYPE) is None
        register_telegram_channel_task_verifier()
        assert isinstance(
            get_verifier(TELEGRAM_CHANNEL_TASK_TYPE),
            TelegramChannelTaskVerifier,
        )

    def test_task_verifier_import_registers_telegram_channel(self, env):
        """`import task_verifier` alone must resolve the verifier.

        Runs in a fresh interpreter so no direct import of this module
        can mask a missing registration.
        """
        repo_root = os.path.dirname(os.path.abspath(__file__))
        code = (
            "import task_verifier\n"
            "from task_verifier import get_verifier\n"
            "v = get_verifier('telegram_channel')\n"
            "assert v is not None, 'telegram_channel not registered'\n"
            "assert type(v).__name__ == 'TelegramChannelTaskVerifier'\n"
            "c = get_verifier('channel_subscription')\n"
            "assert c is not None, 'channel_subscription not registered'\n"
            "assert type(c).__name__ == 'ChannelTaskVerifier'\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr


# ══════════════════════════════════════════════════════════════════
# Task definition — server-side task_data contract
# ══════════════════════════════════════════════════════════════════


class TestTaskDataContract:
    def test_valid_contract_validates(self):
        data = _valid_task_data()
        assert validate_telegram_channel_task_data(data) is data

    def test_valid_arabic_instructions_accepted(self):
        data = _valid_task_data(instructions="مهمة يومية: اشترك في القناة")
        assert validate_telegram_channel_task_data(data) == data

    # ── provider ────────────────────────────────────────────────

    def test_missing_provider_rejected(self):
        data = _valid_task_data()
        del data["provider"]
        with pytest.raises(TelegramChannelTaskDataError, match="provider"):
            validate_telegram_channel_task_data(data)

    def test_wrong_provider_rejected(self):
        data = _valid_task_data()
        data["provider"] = "web"
        with pytest.raises(TelegramChannelTaskDataError, match="provider"):
            validate_telegram_channel_task_data(data)

    # ── action ──────────────────────────────────────────────────

    def test_missing_action_rejected(self):
        data = _valid_task_data()
        del data["action"]
        with pytest.raises(TelegramChannelTaskDataError, match="action"):
            validate_telegram_channel_task_data(data)

    def test_wrong_action_rejected(self):
        data = _valid_task_data()
        data["action"] = "subscribe"
        with pytest.raises(TelegramChannelTaskDataError, match="action"):
            validate_telegram_channel_task_data(data)

    # ── target ──────────────────────────────────────────────────

    def test_missing_target_rejected(self):
        data = _valid_task_data()
        del data["target"]
        with pytest.raises(TelegramChannelTaskDataError, match="target"):
            validate_telegram_channel_task_data(data)

    def test_target_not_an_object_rejected(self):
        data = _valid_task_data()
        data["target"] = "main"
        with pytest.raises(TelegramChannelTaskDataError, match="target"):
            validate_telegram_channel_task_data(data)

    def test_missing_channel_identifier_rejected(self):
        data = _valid_task_data()
        data["target"] = {}
        with pytest.raises(TelegramChannelTaskDataError, match="channel"):
            validate_telegram_channel_task_data(data)

    def test_channel_slug_not_a_string_rejected(self):
        data = _valid_task_data()
        data["target"] = {"channel_slug": 12345}
        with pytest.raises(TelegramChannelTaskDataError, match="slug"):
            validate_telegram_channel_task_data(data)

    @pytest.mark.parametrize("slug", [
        "https://evil.example/x",      # arbitrary URL
        "http://evil.example",         # arbitrary URL
        "@main",                       # username, not a slug
        "-1001234567890",              # numeric Telegram id
        "main channel",                # whitespace / ambiguous
        " main ",                      # padded / ambiguous
        "main/x",                      # path fragment
        "ma%20in",                     # encoded separator
        "main.join",                   # not a configured-slug shape
        "x" * 65,                      # over MAX_SLUG_LENGTH
    ])
    def test_unsafe_or_ambiguous_target_rejected(self, slug):
        data = _valid_task_data(slug=slug)
        with pytest.raises(TelegramChannelTaskDataError):
            validate_telegram_channel_task_data(data)

    def test_target_extra_keys_rejected(self):
        """No duplicated channel configuration inside target."""
        data = _valid_task_data()
        data["target"]["channel_id"] = _ADMIN_CHANNEL_ID
        with pytest.raises(TelegramChannelTaskDataError, match="target"):
            validate_telegram_channel_task_data(data)

    def test_target_join_url_rejected(self):
        data = _valid_task_data()
        data["target"]["join_url"] = "https://t.me/sneaky"
        with pytest.raises(TelegramChannelTaskDataError, match="target"):
            validate_telegram_channel_task_data(data)

    # ── reward / unknown keys ───────────────────────────────────

    def test_reward_inside_task_data_rejected(self):
        data = _valid_task_data()
        data["reward"] = 999999
        with pytest.raises(TelegramChannelTaskDataError, match="reward"):
            validate_telegram_channel_task_data(data)

    def test_unexpected_top_level_key_rejected(self):
        data = _valid_task_data()
        data["is_member"] = True
        with pytest.raises(TelegramChannelTaskDataError, match="unexpected"):
            validate_telegram_channel_task_data(data)

    # ── instructions ────────────────────────────────────────────

    def test_missing_instructions_rejected(self):
        data = _valid_task_data()
        del data["instructions"]
        with pytest.raises(TelegramChannelTaskDataError, match="instructions"):
            validate_telegram_channel_task_data(data)

    def test_instructions_not_a_string_rejected(self):
        data = _valid_task_data()
        data["instructions"] = 123
        with pytest.raises(TelegramChannelTaskDataError, match="instructions"):
            validate_telegram_channel_task_data(data)

    @pytest.mark.parametrize("instructions", ["", "   ", "\n\t"])
    def test_empty_instructions_rejected(self, instructions):
        data = _valid_task_data(instructions=instructions)
        with pytest.raises(TelegramChannelTaskDataError, match="instructions"):
            validate_telegram_channel_task_data(data)

    def test_overlong_instructions_rejected(self):
        data = _valid_task_data(instructions="x" * (MAX_INSTRUCTIONS_LENGTH + 1))
        with pytest.raises(TelegramChannelTaskDataError, match="instructions"):
            validate_telegram_channel_task_data(data)

    # ── payload shape / raw parsing ─────────────────────────────

    @pytest.mark.parametrize("payload", [None, "text", 42, ["main"], []])
    def test_non_object_payload_rejected(self, payload):
        with pytest.raises(TelegramChannelTaskDataError):
            validate_telegram_channel_task_data(payload)

    def test_parse_missing_raw_rejected(self):
        with pytest.raises(TelegramChannelTaskDataError, match="missing"):
            parse_telegram_channel_task_data(None)

    def test_parse_blank_raw_rejected(self):
        with pytest.raises(TelegramChannelTaskDataError, match="missing"):
            parse_telegram_channel_task_data("   ")

    def test_parse_bad_json_rejected(self):
        with pytest.raises(TelegramChannelTaskDataError, match="JSON"):
            parse_telegram_channel_task_data("{not json")

    def test_parse_valid_raw_returns_dict(self):
        parsed = parse_telegram_channel_task_data(json.dumps(_valid_task_data()))
        assert parsed["target"]["channel_slug"] == "main"

    # ── safe slug accessor ──────────────────────────────────────

    def test_task_channel_slug_returns_slug(self):
        task = {"task_data": json.dumps(_valid_task_data("main"))}
        assert task_channel_slug(task) == "main"

    @pytest.mark.parametrize("raw", [None, "", "{not json", "[]",
                                     json.dumps({"channel_slug": "main"})])
    def test_task_channel_slug_none_on_unusable(self, raw):
        assert task_channel_slug({"task_data": raw}) is None

    def test_task_channel_slug_none_on_non_dict(self):
        assert task_channel_slug(None) is None


# ══════════════════════════════════════════════════════════════════
# Membership → PASSED / FAILED
# ══════════════════════════════════════════════════════════════════


class TestMembershipResults:
    def test_valid_member_passes(self, env):
        calls = _register_fake("member")
        result = verify_task(_USER_ID, env["task_id"])
        assert result.status == VerificationStatus.PASSED
        # Checked against the CONFIGURED channel, not client data.
        assert calls == [(_ADMIN_CHANNEL_ID, _USER_ID)]

    def test_administrator_passes(self, env):
        _register_fake("administrator")
        result = verify_task(_USER_ID, env["task_id"])
        assert result.status == VerificationStatus.PASSED

    def test_creator_passes(self, env):
        _register_fake("creator")
        result = verify_task(_USER_ID, env["task_id"])
        assert result.status == VerificationStatus.PASSED

    @pytest.mark.parametrize("status", ["left", "kicked", "restricted"])
    def test_non_member_fails(self, env, status):
        # Same semantics as subscription._is_chat_member (fail-closed).
        calls = _register_fake(status)
        result = verify_task(_USER_ID, env["task_id"])
        assert result.status == VerificationStatus.FAILED
        assert "not a member" in result.reason
        assert calls == [(_ADMIN_CHANNEL_ID, _USER_ID)]

    def test_supergroup_membership_passes(self, env):
        """Channel/supergroup semantics match the subscription gate."""
        CHANNELS["group"] = Channel(
            slug="group",
            channel_id=_GROUP_ID,
            username="group_user",
            title="Super Group",
            required=True,
            chat_type="supergroup",
        )
        task_id = _make_task(_valid_task_data("group"))
        calls = _register_fake("member")
        result = verify_task(_USER_ID, task_id)
        assert result.status == VerificationStatus.PASSED
        assert calls == [(_GROUP_ID, _USER_ID)]


# ══════════════════════════════════════════════════════════════════
# Malformed / missing task definitions → ERROR (never pass)
# ══════════════════════════════════════════════════════════════════


class TestMalformedTaskData:
    def _assert_error_without_check(self, task_id: int):
        calls = _register_fake("member")
        result = verify_task(_USER_ID, task_id)
        assert result.status == VerificationStatus.ERROR
        assert calls == [], "membership must not be checked"

    def test_missing_task_data(self, env):
        self._assert_error_without_check(_make_task(None))

    def test_invalid_json_task_data(self, env):
        self._assert_error_without_check(_make_task("{not json"))

    def test_task_data_not_a_dict(self, env):
        self._assert_error_without_check(_make_task(["main"]))

    def test_wrong_provider(self, env):
        data = _valid_task_data()
        data["provider"] = "web"
        self._assert_error_without_check(_make_task(data))

    def test_wrong_action(self, env):
        data = _valid_task_data()
        data["action"] = "subscribe"
        self._assert_error_without_check(_make_task(data))

    def test_missing_target(self, env):
        data = _valid_task_data()
        del data["target"]
        self._assert_error_without_check(_make_task(data))

    def test_missing_channel_slug(self, env):
        data = _valid_task_data()
        data["target"] = {}
        self._assert_error_without_check(_make_task(data))

    def test_unsafe_target(self, env):
        data = _valid_task_data("https://evil.example/x")
        self._assert_error_without_check(_make_task(data))

    def test_missing_instructions(self, env):
        data = _valid_task_data()
        del data["instructions"]
        self._assert_error_without_check(_make_task(data))

    def test_reward_smuggled_into_task_data(self, env):
        data = _valid_task_data()
        data["reward"] = 999999
        self._assert_error_without_check(_make_task(data))


# ══════════════════════════════════════════════════════════════════
# Unconfigured target → ERROR
# ══════════════════════════════════════════════════════════════════


class TestUnknownChannelConfiguration:
    def test_unconfigured_slug_errors(self, env):
        task_id = _make_task(_valid_task_data("does_not_exist"))
        calls = _register_fake("member")
        result = verify_task(_USER_ID, task_id)
        assert result.status == VerificationStatus.ERROR
        assert "not configured" in result.reason
        assert calls == [], "membership must not be checked"


# ══════════════════════════════════════════════════════════════════
# Telegram / infrastructure failures → ERROR
# ══════════════════════════════════════════════════════════════════


class TestInfrastructureErrors:
    def test_telegram_error_is_error(self, env):
        _register_raising(TelegramError("boom"))
        result = verify_task(_USER_ID, env["task_id"])
        assert result.status == VerificationStatus.ERROR
        assert "Telegram membership check failed" in result.reason

    def test_generic_exception_is_error(self, env):
        _register_raising(RuntimeError("network down"))
        result = verify_task(_USER_ID, env["task_id"])
        assert result.status == VerificationStatus.ERROR

    def test_invalid_status_type_is_error(self, env):
        _register_fake(True)  # type: ignore[arg-type]
        # checker returns a non-str — must be ERROR, never PASSED
        result = verify_task(_USER_ID, env["task_id"])
        assert result.status == VerificationStatus.ERROR

    def test_default_checker_without_token_is_error(self, env):
        """The default live checker fails closed when no token exists."""
        register_telegram_channel_task_verifier()  # default env-token checker
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TELEGRAM_BOT_TOKEN", "")
            result = verify_task(_USER_ID, env["task_id"])
        assert result.status == VerificationStatus.ERROR


# ══════════════════════════════════════════════════════════════════
# Client input can never override trusted task data
# ══════════════════════════════════════════════════════════════════


class TestClientCannotOverride:
    def test_submission_fields_cannot_override_target(self, env):
        TaskStartGate().start(_USER_ID, env["task_id"])
        CHANNELS["attacker_channel"] = Channel(
            slug="attacker_channel",
            channel_id=_ATTACKER_CHANNEL_ID,
            username="attacker",
            title="Attacker Channel",
            required=True,
        )
        calls = _register_fake("left")

        result = TaskSubmissionService.submit(
            _USER_ID,
            env["task_id"],
            {
                "channel_slug": "attacker_channel",
                "channel_id": _ATTACKER_CHANNEL_ID,
                "channel_username": "attacker",
                "join_url": "https://t.me/attacker",
                "verified": True,
                "is_member": True,
            },
        )
        # Real membership (left) decides — flags are ignored.
        assert result.status == VerificationStatus.FAILED
        # The trusted configured channel was checked, not the attacker's.
        assert calls == [(_ADMIN_CHANNEL_ID, _USER_ID)]
        # And nothing was completed.
        assert _status(env["task_id"]) == db.USER_TASK_STATUS_STARTED

    def test_client_cannot_change_reward(self, env):
        TaskStartGate().start(_USER_ID, env["task_id"])
        with pytest.raises(SubmissionError):
            TaskSubmissionService.submit(
                _USER_ID, env["task_id"], {"reward": 999999}
            )

    def test_client_cannot_provide_another_users_identity(self, env):
        TaskStartGate().start(_USER_ID, env["task_id"])
        with pytest.raises(SubmissionError):
            TaskSubmissionService.submit(
                _USER_ID, env["task_id"], {"user_id": 424242}
            )

    def test_client_cannot_force_completion_fields(self, env):
        TaskStartGate().start(_USER_ID, env["task_id"])
        with pytest.raises(SubmissionError):
            TaskSubmissionService.submit(
                _USER_ID,
                env["task_id"],
                {"status": "completed", "completed": True},
            )

    def test_client_cannot_force_passed(self, env):
        TaskStartGate().start(_USER_ID, env["task_id"])
        _register_fake("left")
        result = CompletionBridge.complete_after_verification(
            _USER_ID, env["task_id"],
            {"is_member": True, "verified": True, "force_pass": True},
        )
        assert result.status == VerificationStatus.FAILED
        assert _status(env["task_id"]) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units() == 0
        assert _task_credits() == []


# ══════════════════════════════════════════════════════════════════
# Verifier purity — verification mutates nothing
# ══════════════════════════════════════════════════════════════════


class TestVerifierPurity:
    def test_verify_has_no_side_effects(self, env):
        task_id = env["task_id"]
        TaskStartGate().start(_USER_ID, task_id)
        _register_fake("member")
        task_before = db.get_task(task_id)
        utask_before = db.get_user_task(_USER_ID, task_id)

        result = verify_task(_USER_ID, task_id)
        assert result.passed

        # No user_tasks / task definition mutation, no completion.
        assert db.get_task(task_id) == task_before
        assert db.get_user_task(_USER_ID, task_id) == utask_before
        assert _status(task_id) == db.USER_TASK_STATUS_STARTED
        # No submissions, no wallet, no ledger.
        assert _submissions(task_id) == []
        assert _wallet_units() == 0
        assert _task_credits() == []


# ══════════════════════════════════════════════════════════════════
# Lifecycle — full pipeline with reward settlement
# ══════════════════════════════════════════════════════════════════


class TestLifecycle:
    def test_available_to_started(self, env):
        result = TaskStartGate().start(_USER_ID, env["task_id"])
        assert result.success
        assert _status(env["task_id"]) == db.USER_TASK_STATUS_STARTED

    def test_valid_membership_completes_and_credits_once(self, env):
        task_id = env["task_id"]
        TaskStartGate().start(_USER_ID, task_id)
        _register_fake("member")

        result = TaskLifecycle().submit_task(_USER_ID, task_id, {})
        assert result.status == VerificationStatus.PASSED
        assert _status(task_id) == db.USER_TASK_STATUS_COMPLETED

        # Reward credited exactly once through TaskRewardService.
        assert _wallet_units() == _REWARD_UNITS
        credits = _task_credits()
        assert len(credits) == 1
        assert credits[0]["amount_units"] == _REWARD_UNITS

        # Submission history preserved and tied to the completion.
        rows = _submissions(task_id)
        assert len(rows) == 1
        assert rows[0]["status"] == db.SUBMISSION_STATUS_PASSED
        assert rows[0]["completed_at"] is not None

    def test_failed_membership_no_reward_attempt_persisted(self, env):
        task_id = env["task_id"]
        TaskStartGate().start(_USER_ID, task_id)
        _register_fake("left")

        result = TaskLifecycle().submit_task(_USER_ID, task_id, {})
        assert result.status == VerificationStatus.FAILED
        assert _status(task_id) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units() == 0
        assert _task_credits() == []
        rows = _submissions(task_id)
        assert len(rows) == 1
        assert rows[0]["status"] == db.SUBMISSION_STATUS_FAILED
        assert rows[0]["completed_at"] is None

    def test_api_error_no_reward_attempt_persisted(self, env):
        task_id = env["task_id"]
        TaskStartGate().start(_USER_ID, task_id)
        _register_raising(TelegramError("boom"))

        result = TaskLifecycle().submit_task(_USER_ID, task_id, {})
        assert result.status == VerificationStatus.ERROR
        assert _status(task_id) == db.USER_TASK_STATUS_STARTED
        assert _wallet_units() == 0
        assert _task_credits() == []
        rows = _submissions(task_id)
        assert len(rows) == 1
        assert rows[0]["status"] == db.SUBMISSION_STATUS_ERROR

    def test_retry_after_failed_attempt_works(self, env):
        task_id = env["task_id"]
        TaskStartGate().start(_USER_ID, task_id)

        _register_fake("left")
        first = TaskLifecycle().submit_task(_USER_ID, task_id, {})
        assert first.status == VerificationStatus.FAILED
        assert _wallet_units() == 0

        # The user joins the channel, then retries (fresh key).
        _register_fake("member")
        second = TaskLifecycle().submit_task(_USER_ID, task_id, {})
        assert second.status == VerificationStatus.PASSED
        assert _status(task_id) == db.USER_TASK_STATUS_COMPLETED
        assert _wallet_units() == _REWARD_UNITS

        rows = _submissions(task_id)
        assert [r["status"] for r in rows] == [
            db.SUBMISSION_STATUS_FAILED,
            db.SUBMISSION_STATUS_PASSED,
        ]

    def test_one_time_completion_is_terminal(self, env):
        task_id = env["task_id"]
        TaskStartGate().start(_USER_ID, task_id)
        _register_fake("member")
        TaskLifecycle().submit_task(_USER_ID, task_id, {})
        assert _status(task_id) == db.USER_TASK_STATUS_COMPLETED
        assert _wallet_units() == _REWARD_UNITS

        # Cannot start a second cycle…
        with pytest.raises(StartGateError, match="already completed"):
            TaskStartGate().start(_USER_ID, task_id)

        # …and a new submission cannot complete it again.
        again = TaskLifecycle().submit_task(_USER_ID, task_id, {})
        assert not again.passed
        assert _status(task_id) == db.USER_TASK_STATUS_COMPLETED
        assert _wallet_units() == _REWARD_UNITS
        assert len(_task_credits()) == 1

    def test_same_idempotency_key_never_credits_twice(self, env):
        task_id = env["task_id"]
        TaskStartGate().start(_USER_ID, task_id)
        _register_fake("member")
        first = TaskLifecycle().submit_task(
            _USER_ID, task_id, {}, idempotency_key="same-key"
        )
        assert first.passed

        replay = TaskLifecycle().submit_task(
            _USER_ID, task_id, {}, idempotency_key="same-key"
        )
        assert replay.passed
        assert _wallet_units() == _REWARD_UNITS
        assert len(_task_credits()) == 1


# ══════════════════════════════════════════════════════════════════
# Repeat policy — MT-TASK-04 semantics apply unchanged
# ══════════════════════════════════════════════════════════════════


class TestRepeatPolicy:
    def test_repeatable_completes_again_after_cooldown(self, env):
        task_id = _make_task(
            _valid_task_data(),
            reward=10,
            repeat_policy=db.REPEAT_POLICY_REPEATABLE,
            repeat_hours=_REPEAT_HOURS,
        )
        units = 10 * wallet.USDT_SCALE
        TaskStartGate().start(_USER_ID, task_id)

        # First cycle.
        _register_fake("member")
        first = TaskLifecycle().submit_task(_USER_ID, task_id, {})
        assert first.passed
        assert _wallet_units() == units

        # Before the cooldown elapsed: terminal.
        with pytest.raises(StartGateError, match="already completed"):
            TaskStartGate().start(_USER_ID, task_id)

        # After the cooldown: a new cycle may start and complete.
        _backdate_completed(task_id, _REPEAT_HOURS + 1)
        restart = TaskStartGate().start(_USER_ID, task_id)
        assert restart.success
        assert _status(task_id) == db.USER_TASK_STATUS_STARTED

        second = TaskLifecycle().submit_task(_USER_ID, task_id, {})
        assert second.passed
        assert _status(task_id) == db.USER_TASK_STATUS_COMPLETED
        assert _wallet_units() == 2 * units
        assert len(_task_credits()) == 2
        # History from both cycles is preserved.
        rows = _submissions(task_id)
        assert [r["status"] for r in rows] == [
            db.SUBMISSION_STATUS_PASSED,
            db.SUBMISSION_STATUS_PASSED,
        ]

    def test_one_time_never_ready_for_repeat(self, env):
        from task_attempt import TaskAttemptPolicy

        task_id = env["task_id"]
        TaskStartGate().start(_USER_ID, task_id)
        _register_fake("member")
        TaskLifecycle().submit_task(_USER_ID, task_id, {})
        _backdate_completed(task_id, 10_000)
        assert TaskAttemptPolicy.is_repeat_ready(_USER_ID, task_id) is False


# ══════════════════════════════════════════════════════════════════
# Mini App API — safe join URL, no task_data leakage
# ══════════════════════════════════════════════════════════════════


class TestSafeJoinUrl:
    def test_telegram_channel_join_url(self, env):
        from task_routes import _safe_join_url

        url = _safe_join_url(env["task_id"], TELEGRAM_CHANNEL_TASK_TYPE)
        # Exactly the public link — no slug, id or task_data inside it.
        assert url == "https://t.me/main_user"

    def test_channel_without_username_has_no_url(self, env):
        from task_routes import _safe_join_url

        CHANNELS["private"] = Channel(
            slug="private",
            channel_id=-1005550009999,
            username="",
            title="Private Target",
            required=False,
        )
        task_id = _make_task(_valid_task_data("private"))
        assert _safe_join_url(task_id, TELEGRAM_CHANNEL_TASK_TYPE) is None

    def test_unconfigured_slug_has_no_url(self, env):
        from task_routes import _safe_join_url

        task_id = _make_task(_valid_task_data("does_not_exist"))
        assert _safe_join_url(task_id, TELEGRAM_CHANNEL_TASK_TYPE) is None

    def test_malformed_task_data_has_no_url(self, env):
        from task_routes import _safe_join_url

        task_id = _make_task("{not json")
        assert _safe_join_url(task_id, TELEGRAM_CHANNEL_TASK_TYPE) is None

    def test_channel_subscription_join_url_unchanged(self, env):
        """Regression: the existing family keeps its behavior."""
        from task_routes import _safe_join_url

        task_id = _make_task(
            {"channel_slug": "main"}, task_type=CHANNEL_TASK_TYPE
        )
        assert _safe_join_url(task_id, CHANNEL_TASK_TYPE) == (
            "https://t.me/main_user"
        )

    @pytest.mark.parametrize("task_type", ["deterministic", "unknown_type", ""])
    def test_other_task_types_get_no_url(self, env, task_type):
        from task_routes import _safe_join_url

        task_id = _make_task(_valid_task_data(), task_type="deterministic")
        assert _safe_join_url(task_id, task_type) is None


class TestNoUnsafeExposure:
    def test_catalog_summary_exposes_only_safe_fields(self, env):
        """No task_data / instructions / slug in the safe API surface."""
        summaries = TaskCatalog().list_available_tasks()
        assert len(summaries) == 1
        fields = set(dataclasses.asdict(summaries[0]).keys())
        assert fields == {"id", "title", "description", "type", "reward"}
        dumped = json.dumps(dataclasses.asdict(summaries[0]))
        assert "task_data" not in dumped
        assert "instructions" not in dumped
        assert "channel_slug" not in dumped
        assert "provider" not in dumped
