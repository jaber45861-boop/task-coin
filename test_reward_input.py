"""
Focused tests — Exact Atomic Task Reward Input (MT-ADMIN-14)
============================================================

Admin reward input is now an EXACT decimal USDT string parsed by the
ONE canonical parser ``task_creation.parse_reward_units`` — the legacy
``/addtask`` pipe, the admin wizard and spec re-validation all share
it (no second parsing implementation):

    input text → canonical parser → atomic reward_units int → db

``tasks.reward_units`` stays the sole accounting authority
(1 USDT = 100,000,000 units, SQLite INTEGER); ``tasks.reward`` remains
the whole-USDT compatibility/display field.  No float, no round(),
no truncation — over-precision is rejected, never rounded.

Coverage (spec sections 9 / 10 / 12):

  Parser (cases 1–15)
    - "1" → 100000000, "0.5" → 50000000, "0.01" → 1000000,
      "0.005" → 500000, "0.0001" → 10000, "0.00000001" → 1,
      "0" → 0
    - rejects: >8 dp, scientific ("1e-8"), negatives, empty,
      malformed, float, bool, signed-int64 overflow
  Creation (16–18)
    - pipe "0.0001" stores reward_units=10000 (display reward 0)
    - integer "1" still stores reward_units=100000000
    - pipe and wizard behave identically (same parser)
  Settlement (19–22)
    - a new 0.0001-USDT task credits exactly 10,000 atomic units
    - a new 1-atomic-unit task credits exactly 1 unit
    - retry stays exactly-once
    - whole-USDT settlement unchanged (50 → 5,000,000,000)
  UI / API (23–24)
    - /api/tasks exposes the exact atomic field for sub-cent display
    - tasks.js derives the display from integer units (no float math)
  Plus: wizard prompt/preview/publish wording says USDT, not points.

Run:
    python -m pytest test_reward_input.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from decimal import Decimal
from unittest import mock

import pytest

import admin_task_wizard
import db
import serve_miniapp
import task_draft_store
import task_taxonomy
import wallet
from bot import add_task
from channel_task_verifier import register_channel_task_verifier
from config import CHANNELS, Channel
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)
from task_creation import (
    TaskCreationError,
    TaskSpec,
    create_task_from_spec,
    parse_reward_units,
    reward_payload_value,
    reward_units_to_text,
    whole_usdt_reward,
)
from task_lifecycle import TaskLifecycle
from task_start import TaskStartGate
from task_submission import TaskSubmissionService
from task_verifier import (
    DeterministicTaskVerifier,
    clear_verifiers,
    register_verifier,
)
from telegram_channel_task_verifier import (
    TELEGRAM_CHANNEL_TASK_TYPE,
    TelegramChannelTaskVerifier,
)
from wallet import USDT_SCALE, units_to_decimal

# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

ADMIN = 7714
USER = 6214

_ROOT = os.path.dirname(os.path.abspath(__file__))

PASSED = VerificationResult(status=VerificationStatus.PASSED)

SUB_CENT_UNITS = 10_000        # 0.0001 USDT
SINGLE_UNIT = 1                # 0.00000001 USDT


def _read(rel_path: str) -> str:
    with open(os.path.join(_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


# ── Verifier (production settlement path needs a passing verifier) ────


class _PassVerifier(DeterministicTaskVerifier):
    def verify(self, context):
        return VerificationResult(status=VerificationStatus.PASSED)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def path(monkeypatch, tmp_path):
    db_path = str(tmp_path / "reward_input_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(ADMIN, "admin14", "Admin 14")
    db.register_user(USER, "worker14", "Worker 14")

    # Roadmap 4: pipe/wizard creation funds the task from the creating
    # admin's wallet (reward + commission snapshot), atomically with
    # the INSERT — seed the registered admin so the creation and
    # settlement tests exercise the production funded path.
    wallet.credit_units(ADMIN, 1_000_000 * USDT_SCALE)

    # Required-channel registry for the legacy pipe.
    CHANNELS.clear()
    CHANNELS["main"] = Channel(
        slug="main",
        channel_id=-100444,
        username="mainchannel",
        title="Main Channel",
        required=True,
    )

    # The pipe handler's admin gate (we call the handler directly).
    monkeypatch.setattr("bot.is_admin", lambda uid: uid == ADMIN)

    clear_verifiers()
    register_verifier("deterministic", _PassVerifier())
    register_verifier(TELEGRAM_CHANNEL_TASK_TYPE, _PassVerifier())

    yield db_path

    # Never leak test verifiers/channels into other suites.
    clear_verifiers()
    register_verifier("deterministic", DeterministicTaskVerifier())
    register_channel_task_verifier()
    register_verifier(TELEGRAM_CHANNEL_TASK_TYPE, TelegramChannelTaskVerifier())
    CHANNELS.clear()


@pytest.fixture
def client(path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


# ── Helpers ───────────────────────────────────────────────────────────


def _pipe(command: str) -> str:
    """Run the legacy /addtask pipe exactly like PTB would; reply text."""
    update = mock.MagicMock()
    update.effective_user.id = ADMIN
    update.message.text = command
    update.message.reply_text = mock.AsyncMock()
    asyncio.run(add_task(update, mock.MagicMock()))
    return update.message.reply_text.await_args.args[0]


def _latest_task() -> dict:
    tasks = db.list_tasks()
    assert tasks, "expected a created task"
    return max(tasks, key=lambda t: t["id"])


def _wizard_draft_at_preview(reward_text: str):
    """Persist a complete wizard draft, validating the reward through
    the wizard's OWN step validation (the canonical parser)."""
    draft = task_draft_store.get_or_create_open_draft(
        ADMIN, admin_task_wizard.STEP_TITLE, {}
    )
    payload = {
        "title": "مهمة MT-ADMIN-14",
        "family": "social",
        "provider": "instagram",
        "target_ref": "https://example.com/task",
        "action": "follow",
        "instructions": "نفذ المهمة وأرسل إثباتاً",
        "verification": task_taxonomy.VERIFICATION_MANUAL,
        "repeat_policy": db.REPEAT_POLICY_ONE_TIME,
    }
    payload = admin_task_wizard._validate_text_step(
        admin_task_wizard.STEP_REWARD, reward_text, payload
    )
    saved = task_draft_store.save_step(
        draft.draft_id, ADMIN, admin_task_wizard.STEP_PREVIEW, payload
    )
    assert saved is not None
    return saved


def _wizard_publish(reward_text: str) -> int:
    """Full wizard creation path: validation → persisted draft →
    spec rebuild → CAS publish."""
    draft = _wizard_draft_at_preview(reward_text)
    return admin_task_wizard.publish_draft(draft.draft_id, ADMIN)


def _start(tid: int) -> None:
    TaskStartGate().start(USER, tid)


def _submit(tid: int, key: str):
    return TaskLifecycle().submit_task(USER, tid, {"actual": "done"}, key)


def _wallet_available() -> int:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT available_units FROM wallets WHERE user_id = ?", (USER,)
        ).fetchall()
    return rows[0]["available_units"] if rows else 0


def _ledger_credits() -> list[dict]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE user_id = ? ORDER BY id", (USER,)
        ).fetchall()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════
# Canonical parser (cases 1–15)
# ════════════════════════════════════════════════════════════════════


class TestCanonicalParser:

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1", 100_000_000),            # 1
            ("0.5", 50_000_000),           # 2
            ("0.01", 1_000_000),           # 3
            ("0.005", 500_000),            # 4
            ("0.0001", 10_000),            # 5
            ("0.00000001", 1),             # 6
            ("0", 0),                      # 7
        ],
    )
    def test_exact_decimal_conversions(self, text, expected):
        assert parse_reward_units(text) == expected
        # Never a float, never rounded: exact int in, exact int out.
        result = parse_reward_units(text)
        assert isinstance(result, int) and not isinstance(result, bool)
        assert result == expected

    def test_integer_input_means_whole_usdt(self):
        assert parse_reward_units(1) == 100_000_000
        assert parse_reward_units(0) == 0
        assert parse_reward_units(50) == 5_000_000_000

    def test_arabic_indic_digits_normalized_exactly(self):
        assert parse_reward_units("٠.٠٠٠١") == 10_000
        assert parse_reward_units("١") == 100_000_000

    @pytest.mark.parametrize(
        "bad",
        [
            "0.000000001",      # 8: 9 decimal places — rejected, not rounded
            "1e-8",             # 9: scientific notation
            "-1",               # 10: negative
            "-0.1",             # 10: negative decimal
            "",                 # 11: empty
            "   ",              # whitespace-only
            "abc",              # 12: malformed
            "1.2.3",            # malformed
            "5.",               # malformed
            "1,5",              # malformed (locale comma)
            "+5",               # malformed sign
            "NaN",              # not a finite decimal
            "Infinity",
            "-Infinity",
            0.5,                # 13: float (even a "simple" one)
            True,               # 14: bool
            False,
            None,
        ],
    )
    def test_rejected_inputs(self, bad):
        with pytest.raises(ValueError):
            parse_reward_units(bad)

    def test_signed_int64_overflow_rejected(self):
        # 2^63 atomic units does not fit a signed SQLite INTEGER.
        with pytest.raises(ValueError):
            parse_reward_units("92233720368.54775808")   # == 2**63 units
        with pytest.raises(ValueError):
            parse_reward_units(92_233_720_369)           # whole-USDT overflow
        # The exact int64 boundary itself is accepted (no wrap-around).
        assert (
            parse_reward_units("92233720368.54775807")
            == 9_223_372_036_854_775_807
        )

    def test_error_messages_name_the_field(self):
        with pytest.raises(ValueError) as exc:
            parse_reward_units("abc")
        assert "المكافأة" in str(exc.value)
        with pytest.raises(ValueError) as exc:
            parse_reward_units("-5", field="النقاط")
        assert "النقاط" in str(exc.value)

    def test_parser_source_contains_no_float_or_round(self):
        """Section 10 safety: the canonical parser body has no float
        conversion, no round(), no quantize()."""
        source = _read("task_creation.py")
        body = source.split("def parse_reward_units", 1)[1]
        body = body.split("def whole_usdt_reward", 1)[0]
        assert "round(" not in body
        assert "float(" not in body
        assert "quantize" not in body
        assert "to_integral" not in body

    def test_display_helpers_are_exact_integer_math(self):
        assert reward_units_to_text(SUB_CENT_UNITS) == "0.0001"
        assert reward_units_to_text(1) == "0.00000001"
        assert reward_units_to_text(100_000_000) == "1"
        assert whole_usdt_reward(SUB_CENT_UNITS) == 0
        assert whole_usdt_reward(100_000_000) == 1
        # Payload form: whole → legacy int, sub-cent → exact string.
        assert reward_payload_value(100_000_000) == 1
        assert reward_payload_value(SUB_CENT_UNITS) == "0.0001"
        assert reward_payload_value(0) == 0


# ════════════════════════════════════════════════════════════════════
# Creation: pipe + wizard through the ONE parser (16–18)
# ════════════════════════════════════════════════════════════════════


class TestCreation:

    def test_pipe_sub_cent_stores_exact_units(self, path):
        """(16) /addtask | 0.0001 | → reward_units = 10000 exactly."""
        reply = _pipe("/addtask مهمة فرعية | وصف المهمة | 0.0001 | main")
        assert "تم إنشاء المهمة" in reply
        task = _latest_task()
        assert task["reward_units"] == 10_000
        assert task["reward_units"] == SUB_CENT_UNITS
        # reward stays the whole-USDT display field (0 for sub-cent).
        assert task["reward"] == 0
        assert db.get_task(task["id"])["reward_units"] == 10_000

    def test_pipe_whole_integer_input_still_exact(self, path):
        """(17) integer "1" keeps working: 100000000 units."""
        _pipe("/addtask مهمة صحيحة | وصف المهمة | 1 | main")
        task = _latest_task()
        assert task["reward_units"] == 100_000_000
        assert task["reward"] == 1

    def test_wizard_sub_cent_stores_exact_units(self, path):
        tid = _wizard_publish("0.0001")
        task = db.get_task(tid)
        assert task["reward_units"] == 10_000
        assert task["reward"] == 0

    def test_wizard_whole_integer_input_still_exact(self, path):
        tid = _wizard_publish("1")
        task = db.get_task(tid)
        assert task["reward_units"] == 100_000_000
        assert task["reward"] == 1

    def test_wizard_and_pipe_share_the_one_parser(self, path):
        """(18) the same input through both entry points yields the
        same atomic value — one source of truth, no drift."""
        _pipe("/addtask من الأنبوب | وصف | 0.0001 | main")
        pipe_task = _latest_task()
        wizard_tid = _wizard_publish("0.0001")
        wizard_task = db.get_task(wizard_tid)

        expected = parse_reward_units("0.0001")
        assert expected == 10_000
        assert pipe_task["reward_units"] == expected
        assert wizard_task["reward_units"] == expected

        # Both reject the same invalid input.
        with pytest.raises(ValueError) as exc:
            parse_reward_units("1e-8")
        assert "المكافأة" in str(exc.value)
        with pytest.raises(ValueError, match="المكافأة"):
            admin_task_wizard._validate_text_step(
                admin_task_wizard.STEP_REWARD, "1e-8", {}
            )
        before = len(db.list_tasks())
        reply = _pipe("/addtask مرفوض | وصف | 1e-8 | main")
        assert "النقاط" in reply
        assert len(db.list_tasks()) == before

    def test_db_create_task_validates_explicit_units(self, path):
        base = dict(
            title="وحدات صريحة", description="d", task_type="deterministic",
            reward=0,
        )
        for bad in (True, -1, 1.5, 2 ** 63, "10000"):
            with pytest.raises(ValueError):
                db.create_task(**base, reward_units=bad)
        tid = db.create_task(**base, reward_units=10_000)
        assert db.get_task(tid)["reward_units"] == 10_000
        assert db.get_task(tid)["reward"] == 0

    def test_legacy_spec_without_units_derives_whole_usdt(self, path):
        """Existing callers that build TaskSpec(reward=50) keep their
        exact MT-ADMIN-13 behavior (compatibility, not a second money
        representation — the authority is still written as int units)."""
        spec = TaskSpec(
            title="قديم",
            description="وصف",
            provider="telegram",
            action="join_channel",
            target={"channel_slug": "main"},
            verification=task_taxonomy.VERIFICATION_AUTO,
            reward=50,
        )
        tid = create_task_from_spec(spec)
        task = db.get_task(tid)
        assert task["reward"] == 50
        assert task["reward_units"] == 50 * USDT_SCALE

    def test_spec_rejects_bogus_reward_units(self, path):
        spec = TaskSpec(
            title="سقيم",
            description="وصف",
            provider="telegram",
            action="join_channel",
            target={"channel_slug": "main"},
            verification=task_taxonomy.VERIFICATION_AUTO,
            reward=1,
            reward_units=-5,
        )
        with pytest.raises(TaskCreationError):
            create_task_from_spec(spec)


# ════════════════════════════════════════════════════════════════════
# Wizard wording: USDT, not points (section 4)
# ════════════════════════════════════════════════════════════════════


class TestWizardWording:

    def test_reward_prompt_states_usdt_decimal(self, path):
        draft = task_draft_store.get_or_create_open_draft(
            ADMIN, admin_task_wizard.STEP_REWARD, {}
        )
        text, _markup = admin_task_wizard.render_step(draft)
        assert "USDT" in text
        assert "8 منازل عشرية" in text
        assert "نقطة" not in text

    def test_preview_and_publish_show_exact_usdt(self, path):
        draft = _wizard_draft_at_preview("0.0001")
        preview = admin_task_wizard.build_preview_text(draft)
        assert "0.0001 USDT" in preview
        assert "نقطة" not in preview

        tid = admin_task_wizard.publish_draft(draft.draft_id, ADMIN)
        published = admin_task_wizard._published_text(tid)
        assert "0.0001 USDT" in published
        assert "نقطة" not in published

    def test_whole_reward_preview_stays_whole(self, path):
        draft = _wizard_draft_at_preview("100")
        preview = admin_task_wizard.build_preview_text(draft)
        assert "100 USDT" in preview


# ════════════════════════════════════════════════════════════════════
# Settlement from a NEWLY CREATED task (19–22)
# ════════════════════════════════════════════════════════════════════


class TestSettlement:

    def test_new_sub_cent_task_credits_10000_units(self, path):
        """(19) a task created from "| 0.0001 |" credits exactly
        10,000 atomic units — no rounding anywhere."""
        _pipe("/addtask دقة عشرية | وصف المهمة | 0.0001 | main")
        tid = _latest_task()["id"]
        assert db.get_task(tid)["reward_units"] == 10_000

        _start(tid)
        result = _submit(tid, "subcent")
        assert result.passed
        assert db.get_user_task(USER, tid)["status"] == (
            db.USER_TASK_STATUS_COMPLETED
        )
        assert _wallet_available() == 10_000
        entries = _ledger_credits()
        assert len(entries) == 1
        assert entries[0]["entry_type"] == "credit"
        assert entries[0]["amount_units"] == 10_000
        assert entries[0]["available_delta"] == 10_000
        meta = json.loads(entries[0]["metadata"])
        assert meta["reward_units"] == 10_000
        # Exact Decimal round-trip (display precision ≠ accounting).
        assert units_to_decimal(_wallet_available()) == Decimal("0.0001")

    def test_new_single_unit_task_credits_exactly_one(self, path):
        """(20) 1 atomic unit == 0.00000001 USDT, credited as exactly 1."""
        _pipe("/addtask أصغر مكافأة | وصف المهمة | 0.00000001 | main")
        tid = _latest_task()["id"]
        assert db.get_task(tid)["reward_units"] == 1

        _start(tid)
        result = _submit(tid, "single")
        assert result.passed
        assert _wallet_available() == 1
        entries = _ledger_credits()
        assert len(entries) == 1
        assert entries[0]["amount_units"] == 1
        assert units_to_decimal(1) == Decimal("0.00000001")

    def test_retry_stays_exactly_once(self, path):
        """(21) idempotent replay + rejected second completion: the
        10,000-unit credit happens exactly once."""
        _pipe("/addtask مرة واحدة | وصف المهمة | 0.0001 | main")
        tid = _latest_task()["id"]
        _start(tid)
        assert _submit(tid, "once").passed

        replay = TaskSubmissionService.replay_result(USER, tid, "once")
        assert replay is not None
        assert replay.passed
        with pytest.raises(CompletionGateError):
            CompletionGate().complete(USER, tid, PASSED)

        assert _wallet_available() == 10_000
        entries = _ledger_credits()
        assert len(entries) == 1
        assert entries[0]["amount_units"] == 10_000

    def test_whole_usdt_settlement_unchanged(self, path):
        """(22) existing whole-USDT tasks settle exactly as before."""
        _pipe("/addtask مهمة صحيحة | وصف المهمة | 50 | main")
        tid = _latest_task()["id"]
        assert db.get_task(tid)["reward_units"] == 5_000_000_000

        _start(tid)
        result = _submit(tid, "whole")
        assert result.passed
        assert _wallet_available() == 5_000_000_000
        entries = _ledger_credits()
        assert len(entries) == 1
        assert entries[0]["amount_units"] == 5_000_000_000
        assert units_to_decimal(_wallet_available()) == Decimal("50")


# ════════════════════════════════════════════════════════════════════
# Mini App / API: exact atomic exposure, integer-only JS (23–24)
# ════════════════════════════════════════════════════════════════════


class TestUiAndApi:

    def test_api_exposes_exact_atomic_field(self, path, client):
        """(23) /api/tasks carries reward_units so the page can show
        sub-cent rewards exactly; reward stays the display field."""
        units = parse_reward_units("0.0001")
        db.create_task(
            "مهمة فرعية",
            "وصف دقيق",
            "deterministic",
            whole_usdt_reward(units),
            task_data=json.dumps({"expected": "x"}),
            reward_units=units,
        )
        response = client.get(
            "/api/tasks",
            headers={INIT_DATA_HEADER: _make_init_data(user_id=USER)},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        task = data["tasks"][0]
        assert task["reward_units"] == 10_000
        assert task["reward"] == 0          # whole-USDT display only
        raw = response.get_data(as_text=True)
        assert "reward_units" in raw

    def test_api_whole_usdt_task_exposes_units_too(self, path, client):
        db.create_task(
            "مهمة صحيحة", "وصف", "deterministic", 50,
            task_data=json.dumps({"expected": "x"}),
        )
        response = client.get(
            "/api/tasks",
            headers={INIT_DATA_HEADER: _make_init_data(user_id=USER)},
        )
        task = response.get_json()["tasks"][0]
        assert task["reward"] == 50
        assert task["reward_units"] == 50 * USDT_SCALE

    def test_tasks_page_derives_display_from_integer_units(self):
        """(24) the Tasks page renders from the atomic field using the
        shared integer formatter — no JS float math, compat fallback
        kept for rows without an atomic value."""
        content = _read("miniapp/js/tasks.js")
        assert "WalletData.formatUsdt(task.reward_units)" in content
        assert "String(task.reward)" in content
        assert "toFixed" not in content
        assert "parseFloat" not in content
        # Never hardcoded reward data, never an assignment to reward.
        assert re.search(r"reward\s*[:=]\s*[\"'0-9]", content) is None
        assert re.search(r"task\.reward\s*=", content) is None
        # The integer-only formatter loads before the tasks page.
        html = _read("miniapp/index.html")
        assert html.index("js/wallet-data.js") < html.index("js/tasks.js")

    def test_wallet_formatter_is_integer_only(self):
        """The formatter the Tasks page relies on: exact integer math
        (floor / modulo on safe integers), no rounding primitives."""
        content = _read("miniapp/js/wallet-data.js")
        assert "USDT_UNITS_PER_USDT = 100000000" in content
        assert "formatUsdt" in content
        assert "round(" not in content
        assert "toFixed" not in content
        assert "parseFloat" not in content
