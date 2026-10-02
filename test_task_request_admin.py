"""
Admin Task-Request Review («إضافة مهمة ➕» — مراجعة الإدارة)
============================================================

Telegram-side review surface for user-proposed tasks
(``task_request_admin``):

  Callback grammar
  - closed ``treq:<op>[:<arg>]`` grammar: valid ops parse, every
    malformed/forged payload returns None

  Authorization & isolation
  - /taskrequests + ``treq:`` presses: admin allowed, non-admin denied
    with an Arabic notice, group/channel chats completely silent
  - invalid payloads answered with the safe invalid notice, missing
    ids with the stale notice, zero mutations either way

  Queue & detail
  - empty queue → MSG_NO_REQUESTS; a pending request renders with its
    title/status and the view button
  - detail card shows owner/status/payload/reward and, while pending,
    the approve/edit/return/reject keyboard; pagination is bounded

  Admin decisions (exactly one implementation, the store)
  - edit → field chooser → prompt → validated field write
    (invalid value rejected, state kept for retry)
  - return with note → changes_requested (reason stored)
  - reject with reason → rejected (terminal, reason stored)
  - any button press cancels a pending text prompt; text input is
    self-gated (non-admin / group / no-state → silent)

  Approval
  - claim CAS → ``task_creation.create_task_from_spec`` → mark, ONE
    transaction: publishes exactly ONE active manual task whose
    approver is the approving admin, funded from the requesting
    user's wallet (reward + commission snapshot)
  - replayed approve → MSG_APPROVED_ALREADY, never a second task or
    charge; missing/rejected id → stale; funding failure → Arabic
    error with the request still pending, zero tasks

  Registration
  - ``/taskrequests`` (group 0), ``^treq:`` (group 5) and the group-9
    self-gated text input are each registered exactly once

Run:
    python3 -m pytest test_task_request_admin.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest import mock

import pytest
from telegram import InlineKeyboardMarkup

import config
import db
import task_request_admin as admin
import task_request_store as store
import wallet


ADMIN_A = 5101
ADMIN_B = 5102
OWNER = 4101
STRANGER = 9999

USDT = wallet.USDT_SCALE
SEED = 100 * USDT
REWARD_UNITS = 50_000_000                      # "0.5" USDT
COMMISSION_UNITS = 15_000_000                  # seeded 3000 bp = 30%
CHARGE = REWARD_UNITS + COMMISSION_UNITS

VALID_PAYLOAD = {
    "title": "متابعة حسابي على Instagram",
    "description": "تابع الحساب ثم أرسل إثبات المتابعة",
    "provider": "instagram",
    "action": "follow",
    "target_ref": "https://instagram.com/example",
    "reward": "0.5",
}


# ── Fixtures & helpers ────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated DB + registered owner + two test admins."""
    db_path = str(tmp_path / "task_request_admin.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(OWNER, "owner", "Owner")
    monkeypatch.setattr(config, "ADMINS", [ADMIN_A, ADMIN_B])
    yield db_path


def _run(coro):
    return asyncio.run(coro)


def _create(**overrides) -> store.TaskRequest:
    payload = dict(VALID_PAYLOAD)
    payload.update(overrides)
    return store.create_request(OWNER, payload)


def _command_reply(actor=ADMIN_A, chat_type="private"):
    """Run /taskrequests; return the captured reply_text mock."""
    update = mock.MagicMock()
    update.effective_chat.type = chat_type
    update.effective_user.id = actor
    update.message.reply_text = mock.AsyncMock()
    _run(admin.taskrequests_command(update, mock.MagicMock()))
    return update.message.reply_text


def _ctx() -> mock.MagicMock:
    context = mock.MagicMock()
    context.user_data = {}
    return context


def _press(data, actor=ADMIN_A, chat_type="private", context=None):
    """Run one ``treq:`` callback press; return (query, context)."""
    query = mock.MagicMock()
    query.data = data
    query.answer = mock.AsyncMock()
    query.edit_message_text = mock.AsyncMock()
    update = mock.MagicMock()
    update.callback_query = query
    update.effective_chat.type = chat_type
    update.effective_user.id = actor
    context = context if context is not None else _ctx()
    _run(admin.task_request_callback(update, context))
    return query, context


def _send_text(text, context, actor=ADMIN_A, chat_type="private"):
    """Run the group-9 text input; return the captured reply_text mock."""
    update = mock.MagicMock()
    update.effective_chat.type = chat_type
    update.effective_user.id = actor
    update.message.text = text
    update.message.reply_text = mock.AsyncMock()
    _run(admin.task_request_text_input(update, context))
    return update.message.reply_text


def _answered(query) -> str | None:
    """The text passed to query.answer(), or None when unanswered."""
    if query.answer.await_count == 0:
        return None
    args = query.answer.await_args.args
    return args[0] if args else None


def _edited(query) -> str | None:
    if query.edit_message_text.await_count == 0:
        return None
    args = query.edit_message_text.await_args.args
    return args[0] if args else None


def _reply_text(reply) -> str:
    assert reply.await_count == 1, f"expected exactly 1 reply, got {reply.await_count}"
    return reply.await_args.args[0]


def _markup(query) -> InlineKeyboardMarkup:
    kwargs = query.edit_message_text.await_args.kwargs
    return kwargs.get("reply_markup")


def _buttons(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def _row_count(sql: str, params: tuple = ()) -> int:
    with db.get_connection() as conn:
        return int(conn.execute(sql, params).fetchone()["n"])


def _wallet_units(user_id=OWNER) -> int | None:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT available_units FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return None if row is None else int(row["available_units"])


# ════════════════════════════════════════════════════════════════════
# A. Callback grammar (closed, parse-verified)
# ════════════════════════════════════════════════════════════════════


class TestParseCallback:
    @pytest.mark.parametrize("data,expected", [
        ("treq:list", ("list", None, None)),
        ("treq:page:2", ("page", 2, None)),
        ("treq:view:7", ("view", 7, None)),
        ("treq:edit:7", ("edit", 7, None)),
        ("treq:approve:7", ("approve", 7, None)),
        ("treq:return:7", ("return", 7, None)),
        ("treq:reject:7", ("reject", 7, None)),
        ("treq:field:title:7", ("field", 7, "title")),
        ("treq:field:target_ref:7", ("field", 7, "target_ref")),
    ])
    def test_valid_payloads_parse(self, data, expected):
        assert admin.parse_callback(data) == expected

    @pytest.mark.parametrize("data", [
        None,
        12345,
        "",
        "treq",
        "treq:",
        "treq:bogus",
        "treq:list:extra",
        "treq:view",
        "treq:view:0",
        "treq:view:-3",
        "treq:view:12x",
        "treq:view:٧",                 # non-ASCII digits rejected
        "treq:page:0",
        "treq:page:1.5",
        "treq:field:password:7",       # unknown field
        "treq:field:title:0",
        "treq:view:9999999999999",     # > 9 digits
        "x:list",
        "ctl:requests",
    ])
    def test_invalid_payloads_rejected(self, data):
        assert admin.parse_callback(data) is None


# ════════════════════════════════════════════════════════════════════
# B. /taskrequests authorization + queue rendering
# ════════════════════════════════════════════════════════════════════


class TestCommand:
    def test_non_admin_denied(self, env):
        reply = _command_reply(actor=STRANGER)
        assert _reply_text(reply) == admin.MSG_ADMIN_ONLY

    def test_group_chat_silent(self, env):
        reply = _command_reply(actor=ADMIN_A, chat_type="group")
        reply.assert_not_awaited()

    def test_empty_queue(self, env):
        reply = _command_reply()
        assert _reply_text(reply) == admin.MSG_NO_REQUESTS

    def test_renders_pending_queue(self, env):
        request = _create()
        reply = _command_reply()
        text = _reply_text(reply)
        assert admin.LIST_HEADER in text
        assert f"#{request.request_id}" in text
        assert VALID_PAYLOAD["title"] in text
        markup = reply.await_args.kwargs["reply_markup"]
        data = _buttons(markup)
        assert f"treq:view:{request.request_id}" in data
        assert "treq:list" in data          # refresh

    def test_queue_hides_non_pending(self, env):
        request = _create()
        store.admin_reject_request(request.request_id, ADMIN_A, "لا يصلح")
        reply = _command_reply()
        assert _reply_text(reply) == admin.MSG_NO_REQUESTS


# ════════════════════════════════════════════════════════════════════
# C. Callback authorization & safe failure
# ════════════════════════════════════════════════════════════════════


class TestCallbackAuth:
    def test_non_admin_press_denied_without_mutation(self, env):
        request = _create()
        query, _ = _press(f"treq:approve:{request.request_id}", actor=STRANGER)
        assert _answered(query) == admin.MSG_ADMIN_ONLY
        assert query.edit_message_text.await_count == 0
        assert store.get_request(request.request_id).status == \
            store.STATUS_PENDING
        assert _row_count("SELECT COUNT(*) AS n FROM tasks") == 0

    def test_group_press_silent(self, env):
        request = _create()
        query, _ = _press(
            f"treq:view:{request.request_id}", chat_type="supergroup"
        )
        assert query.answer.await_count == 1
        assert _answered(query) is None
        assert query.edit_message_text.await_count == 0

    def test_invalid_payload_answered_invalid(self, env):
        query, _ = _press("treq:bogus")
        assert _answered(query) == admin.MSG_INVALID
        assert query.edit_message_text.await_count == 0

    def test_missing_request_answered_stale(self, env):
        query, _ = _press("treq:view:424242")
        assert _answered(query) == admin.MSG_STALE

    def test_missing_approve_answered_stale(self, env):
        query, _ = _press("treq:approve:424242")
        assert _answered(query) == admin.MSG_STALE
        assert _row_count("SELECT COUNT(*) AS n FROM tasks") == 0


# ════════════════════════════════════════════════════════════════════
# D. Detail card, edit chooser and pagination
# ════════════════════════════════════════════════════════════════════


class TestDetailAndNavigation:
    def test_view_renders_detail_with_action_keyboard(self, env):
        request = _create()
        query, _ = _press(f"treq:view:{request.request_id}")
        text = _edited(query)
        assert text.startswith(f"{admin.DETAIL_HEADER} #{request.request_id}")
        assert "الحالة: ⏳ قيد المراجعة" in text
        assert f"صاحب الطلب: {OWNER} · @owner" in text
        assert VALID_PAYLOAD["title"] in text
        assert "Instagram · متابعة" in text
        assert "المكافأة: 0.5 USDT" in text
        data = _buttons(_markup(query))
        rid = request.request_id
        assert data == [
            f"treq:approve:{rid}",
            f"treq:edit:{rid}",
            f"treq:return:{rid}",
            f"treq:reject:{rid}",
            "treq:list",
        ]

    def test_edit_opens_field_chooser(self, env):
        request = _create()
        query, _ = _press(f"treq:edit:{request.request_id}")
        assert _edited(query)  # detail text stays visible
        data = _buttons(_markup(query))
        for field in admin.EDITABLE_FIELDS:
            assert f"treq:field:{field}:{request.request_id}" in data
        assert "treq:list" in data

    def test_pagination_round_trip(self, env):
        ids = [_create(title=f"مهمة رقم {i}").request_id for i in range(7)]
        query, _ = _press("treq:list")
        text = _edited(query)
        assert "الصفحة 1/2" in text
        assert f"treq:view:{ids[0]}" in _buttons(_markup(query))
        assert "treq:page:2" in _buttons(_markup(query))

        query, _ = _press("treq:page:2")
        text = _edited(query)
        assert "الصفحة 2/2" in text
        assert f"treq:view:{ids[5]}" in _buttons(_markup(query))
        assert "treq:page:1" in _buttons(_markup(query))

        query, _ = _press("treq:page:1")
        assert "الصفحة 1/2" in _edited(query)


# ════════════════════════════════════════════════════════════════════
# E. Admin field edit (prompt → validated write)
# ════════════════════════════════════════════════════════════════════


class TestAdminEdit:
    def test_field_prompt_then_apply(self, env):
        request = _create()
        context = _ctx()
        query, _ = _press(
            f"treq:field:reward:{request.request_id}", context=context
        )
        assert context.user_data[admin.INPUT_STATE_KEY] == {
            "op": "field",
            "request_id": request.request_id,
            "field": "reward",
        }
        assert "المكافأة (USDT)" in _edited(query)

        reply = _send_text("1.25", context)
        text = _reply_text(reply)
        assert text.startswith(admin.MSG_EDITED_TOAST)
        assert "المكافأة: 1.25 USDT" in text
        assert admin.INPUT_STATE_KEY not in context.user_data
        stored = store.get_request(request.request_id)
        assert stored.payload["reward_units"] == 125_000_000
        # audit keeps the previous payload
        last = stored.history[-1]
        assert last["event"] == "admin_edit"
        assert last["field"] == "reward"
        assert last["previous_payload"]["reward_units"] == REWARD_UNITS

    def test_invalid_value_rejected_state_kept(self, env):
        request = _create()
        context = _ctx()
        _press(f"treq:field:reward:{request.request_id}", context=context)

        reply = _send_text("abc", context)
        text = _reply_text(reply)
        assert admin.MSG_EDITED_TOAST not in text
        assert "المكافأة" in text
        assert context.user_data[admin.INPUT_STATE_KEY]["field"] == "reward"
        assert store.get_request(request.request_id).payload[
            "reward_units"
        ] == REWARD_UNITS

        reply = _send_text("0.75", context)
        assert _reply_text(reply).startswith(admin.MSG_EDITED_TOAST)
        assert store.get_request(request.request_id).payload[
            "reward_units"
        ] == 75_000_000

    def test_any_button_press_cancels_prompt(self, env):
        request = _create()
        context = _ctx()
        _press(f"treq:field:title:{request.request_id}", context=context)
        assert admin.INPUT_STATE_KEY in context.user_data
        _press("treq:list", context=context)
        assert admin.INPUT_STATE_KEY not in context.user_data

    def test_edit_on_decided_request_stale(self, env):
        request = _create()
        store.admin_return_request(request.request_id, ADMIN_A, "عدّل")
        query, _ = _press(f"treq:edit:{request.request_id}")
        assert _answered(query) == admin.MSG_STALE
        assert query.edit_message_text.await_count == 0


# ════════════════════════════════════════════════════════════════════
# F. Return & reject (prompt → store decision)
# ════════════════════════════════════════════════════════════════════


class TestReturnAndReject:
    def test_return_with_note(self, env):
        request = _create()
        context = _ctx()
        query, _ = _press(
            f"treq:return:{request.request_id}", context=context
        )
        assert context.user_data[admin.INPUT_STATE_KEY]["op"] == "return"
        assert admin.PROMPT_RETURN in _edited(query)

        reply = _send_text("الصورة غير واضحة", context)
        text = _reply_text(reply)
        assert text.startswith(admin.MSG_RETURNED_TOAST)
        stored = store.get_request(request.request_id)
        assert stored.status == store.STATUS_CHANGES_REQUESTED
        assert stored.decision_reason == "الصورة غير واضحة"
        assert stored.decided_by == ADMIN_A
        assert "ملاحظة: الصورة غير واضحة" in text
        # decided request → only the list button remains
        assert _buttons(reply.await_args.kwargs["reply_markup"]) == \
            ["treq:list"]
        assert admin.INPUT_STATE_KEY not in context.user_data

    def test_reject_with_reason(self, env):
        request = _create()
        context = _ctx()
        query, _ = _press(
            f"treq:reject:{request.request_id}", context=context
        )
        assert context.user_data[admin.INPUT_STATE_KEY]["op"] == "reject"
        assert admin.PROMPT_REJECT in _edited(query)

        reply = _send_text("محتوى مخالف", context)
        text = _reply_text(reply)
        assert text.startswith(admin.MSG_REJECTED_TOAST)
        stored = store.get_request(request.request_id)
        assert stored.status == store.STATUS_REJECTED
        assert stored.decision_reason == "محتوى مخالف"
        assert "سبب الرفض: محتوى مخالف" in text
        assert _row_count("SELECT COUNT(*) AS n FROM tasks") == 0

    def test_decided_request_reject_prompt_is_stale(self, env):
        request = _create()
        store.admin_return_request(request.request_id, ADMIN_A, "عدّل")
        context = _ctx()
        context.user_data[admin.INPUT_STATE_KEY] = {
            "op": "reject",
            "request_id": request.request_id,
            "field": None,
        }
        reply = _send_text("سبب متأخر", context)
        assert _reply_text(reply) == admin.MSG_STALE
        assert admin.INPUT_STATE_KEY not in context.user_data
        # unchanged: still the returned request, not rejected
        assert store.get_request(request.request_id).status == \
            store.STATUS_CHANGES_REQUESTED


# ════════════════════════════════════════════════════════════════════
# G. Text input self-gating (groups 3/6/7/8/9 isolation)
# ════════════════════════════════════════════════════════════════════


class TestTextInputGating:
    def test_silent_without_state(self, env):
        reply = _send_text("مرحبا", _ctx())
        reply.assert_not_awaited()

    def test_silent_for_non_admin(self, env):
        context = _ctx()
        context.user_data[admin.INPUT_STATE_KEY] = {
            "op": "reject", "request_id": 1, "field": None,
        }
        reply = _send_text("سبب", context, actor=STRANGER)
        reply.assert_not_awaited()
        assert admin.INPUT_STATE_KEY in context.user_data

    def test_silent_in_group_chat(self, env):
        context = _ctx()
        context.user_data[admin.INPUT_STATE_KEY] = {
            "op": "reject", "request_id": 1, "field": None,
        }
        reply = _send_text("سبب", context, chat_type="group")
        reply.assert_not_awaited()

    def test_silent_on_empty_text(self, env):
        context = _ctx()
        context.user_data[admin.INPUT_STATE_KEY] = {
            "op": "reject", "request_id": 1, "field": None,
        }
        reply = _send_text("   ", context)
        reply.assert_not_awaited()
        assert admin.INPUT_STATE_KEY in context.user_data


# ════════════════════════════════════════════════════════════════════
# H. Approval — ONE transaction, ONE task, funded exactly once
# ════════════════════════════════════════════════════════════════════


class TestApprove:
    def test_approve_publishes_one_manual_task(self, env):
        request = _create()
        wallet.credit_units(OWNER, SEED)

        query, _ = _press(f"treq:approve:{request.request_id}")
        assert _answered(query) == admin.MSG_APPROVED
        text = _edited(query)
        assert admin.MSG_APPROVED in text
        assert "المهمة المنشورة: #" in text

        stored = store.get_request(request.request_id)
        assert stored.status == store.STATUS_APPROVED
        assert stored.decided_by == ADMIN_A
        task_id = stored.published_task_id
        assert isinstance(task_id, int)

        # exactly one active manual task, approver = approving admin
        assert _row_count("SELECT COUNT(*) AS n FROM tasks") == 1
        task = db.get_task(task_id)
        assert task["active"] == 1
        assert task["type"] == "manual"
        assert task["reward_units"] == REWARD_UNITS
        task_data = json.loads(task["task_data"])
        assert task_data["provider"] == "instagram"
        assert task_data["action"] == "follow"
        assert task_data["approver"]["telegram_user_id"] == ADMIN_A

        # funded from the REQUESTING user's wallet: reward + commission
        assert task["commission_units"] == COMMISSION_UNITS
        assert _wallet_units() == SEED - CHARGE

    def test_replayed_approve_is_idempotent(self, env):
        request = _create()
        wallet.credit_units(OWNER, SEED)
        _press(f"treq:approve:{request.request_id}")

        query, _ = _press(f"treq:approve:{request.request_id}")
        assert _answered(query) == admin.MSG_APPROVED_ALREADY
        assert admin.MSG_APPROVED_ALREADY in _edited(query)

        # exactly one task, charged exactly once
        assert _row_count("SELECT COUNT(*) AS n FROM tasks") == 1
        assert _wallet_units() == SEED - CHARGE
        assert store.get_request(request.request_id).status == \
            store.STATUS_APPROVED

    def test_approve_rejected_request_stale(self, env):
        request = _create()
        wallet.credit_units(OWNER, SEED)
        store.admin_reject_request(request.request_id, ADMIN_A, "لا يصلح")

        query, _ = _press(f"treq:approve:{request.request_id}")
        assert _answered(query) == admin.MSG_STALE
        assert _row_count("SELECT COUNT(*) AS n FROM tasks") == 0
        assert _wallet_units() == SEED

    def test_approve_without_funds_keeps_request_pending(self, env):
        request = _create()   # owner registered but wallet never funded

        query, _ = _press(f"treq:approve:{request.request_id}")
        answer = _answered(query)
        assert answer in (
            "رصيد المعلن غير كافٍ لتكلفة هذه المهمة "
            "(المكافأة + العمولة).",
            "تعذر تمويل المهمة من محفظة المعلن.",
            "تعذر تمويل المهمة.",
        )
        # the claim rolled back: still pending, zero tasks, zero charges
        assert store.get_request(request.request_id).status == \
            store.STATUS_PENDING
        assert _row_count("SELECT COUNT(*) AS n FROM tasks") == 0
        assert _row_count(
            "SELECT COUNT(*) AS n FROM task_funding"
        ) == 0

    def test_returned_request_cannot_be_approved_directly(self, env):
        """An approval press only acts on pending — returned requests
        must go through the user's resubmit first."""
        request = _create()
        wallet.credit_units(OWNER, SEED)
        store.admin_return_request(request.request_id, ADMIN_A, "عدّل")

        query, _ = _press(f"treq:approve:{request.request_id}")
        assert _answered(query) == admin.MSG_STALE
        assert _row_count("SELECT COUNT(*) AS n FROM tasks") == 0


# ════════════════════════════════════════════════════════════════════
# I. Bot registration (exactly once, own groups)
# ════════════════════════════════════════════════════════════════════


def _capture_handlers():
    import bot as bot_mod
    from telegram.ext import (
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
    )

    captured: list = []
    app = mock.MagicMock()
    app.add_handler = lambda handler, group=None: captured.append(
        (handler, group)
    )
    builder = mock.MagicMock()
    builder.token.return_value.build.return_value = app
    with mock.patch.dict(
        os.environ, {"TELEGRAM_BOT_TOKEN": "12345:TESTTOKEN"}
    ), mock.patch.object(
        bot_mod, "ApplicationBuilder", return_value=builder
    ), mock.patch.object(
        bot_mod, "run_single_entry"
    ), mock.patch.object(bot_mod, "db"):
        bot_mod.main()
    return captured, bot_mod


class TestRegistration:
    def test_taskrequests_command_registered_once_group0(self):
        from telegram.ext import CommandHandler

        captured, _ = _capture_handlers()
        handlers = [
            (h, g) for h, g in captured
            if isinstance(h, CommandHandler)
            and getattr(h, "commands", None)
            and "taskrequests" in h.commands
        ]
        assert len(handlers) == 1, "/taskrequests must be registered once"
        handler, group = handlers[0]
        assert handler.callback is admin.taskrequests_command
        assert group == 0

    def test_treq_callback_registered_once_group5(self):
        from telegram.ext import CallbackQueryHandler

        captured, _ = _capture_handlers()
        handlers = [
            (h, g) for h, g in captured
            if isinstance(h, CallbackQueryHandler)
            and getattr(h, "pattern", None) is not None
            and h.pattern.pattern == r"^treq:"
        ]
        assert len(handlers) == 1, "treq: must be registered once"
        handler, group = handlers[0]
        assert handler.callback is admin.task_request_callback
        assert group == 5

    def test_text_input_registered_once_group9(self):
        from telegram.ext import MessageHandler

        captured, _ = _capture_handlers()
        handlers = [
            (h, g) for h, g in captured
            if isinstance(h, MessageHandler)
            and getattr(h, "callback", None)
            is admin.task_request_text_input
        ]
        assert len(handlers) == 1, \
            "task_request_text_input must be registered once"
        _handler, group = handlers[0]
        assert group == 9

    def test_registration_literals_unique_in_bot_source(self):
        _captured, bot_mod = _capture_handlers()
        source = open(bot_mod.__file__, encoding="utf-8").read()
        assert source.count('"taskrequests"') == 1
        assert source.count("^treq:") == 1
