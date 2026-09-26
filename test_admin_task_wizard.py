"""
Focused tests — Generic Telegram Admin Task Creation Wizard
(MT-ADMIN-05)
============================================================

The wizard replaces the rigid /addtask pipe with a persisted,
step-by-step creation flow for configured admins:

    /addtask → title → family → provider → target → action
             → instructions → verification → (approver) → reward
             → repeat → preview → confirm/cancel

Coverage (spec groups A–I):

A. Authorization      — admin private start; non-admin denied; groups
                        silent; cross-admin draft access denied.
B. Persistence        — drafts live ONLY in admin_task_drafts (fresh
                        connections, module reload = restart
                        simulation), cancel deletes, stale/corrupt
                        drafts fail safe.
C. Wizard flow        — every step, preview, confirm, cancel, edit.
D. Generic providers  — telegram/instagram/website/google_play/crypto/
                        other creation contracts.
E. Telegram compat    — exact MT-TASK-05 nested contract, reader-side
                        validator accepts, unregistered slug rejected,
                        auto never offered where unsupported.
F. Manual compat      — generic manual task flows through the existing
                        ManualProofService/ManualReviewService with
                        task-specific approver authorization intact.
G. Validation         — empty title/instructions, control chars,
                        malformed/negative reward, bad repeat_hours,
                        incompatible verification, bounds.
H. Security           — forged draft ids, forged admin identity,
                        forged provider/reward tokens, cross-admin,
                        callback payloads can never carry reward.
I. Publish idempotency— one confirm → one task; replay → same task;
                        concurrent confirm → one task; failed
                        validation → zero tasks.
J. Regression         — full suite (run separately).

Run:
    python3 -m pytest test_admin_task_wizard.py -v
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from telegram import InlineKeyboardMarkup

import admin_task_wizard
import db
import task_draft_store
import task_taxonomy
from config import CHANNELS, ADMINS, Channel
from manual_task import (
    MANUAL_TASK_TYPE,
    ManualDecisionError,
    ManualProofService,
    ManualReviewService,
)
from task_start import TaskStartGate
from task_taxonomy import (
    FAMILY_PROVIDERS,
    VERIFICATION_APPROVAL,
    VERIFICATION_AUTO,
    VERIFICATION_MANUAL,
)
from telegram_channel_task_verifier import (
    TELEGRAM_CHANNEL_TASK_TYPE,
    parse_telegram_channel_task_data,
    validate_telegram_channel_task_data,
)

# Explicit test-only admins — never depends on ADMINS contents.
ADMIN_A = 7711
ADMIN_B = 7712
STRANGER = 9901
WORKER = 6202

TITLE = "مهمة تجريبية"
INSTRUCTIONS = "نفذ المهمة وأرسل إثباتاً"
TARGET = "https://example.com/target"
PROOF_REF = "https://t.me/c/1234567890/5"


def _provider_family(provider: str) -> str:
    for family, providers in FAMILY_PROVIDERS.items():
        if provider in providers:
            return family
    raise AssertionError(f"provider without family: {provider}")


class WizardTestBase(unittest.TestCase):
    """Temp DB + ADMINS/CHANNELS registry + handler-driving helpers.

    All handlers are driven exactly like PTB would: fresh MagicMock
    updates per message/press, ``asyncio.run`` per call — proving no
    handler depends on in-process state between invocations.
    """

    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)

        self._original_admins = list(ADMINS)
        ADMINS[:] = [ADMIN_A, ADMIN_B]

        self._original_channels = dict(CHANNELS)
        CHANNELS.clear()
        CHANNELS["main"] = Channel(
            slug="main", channel_id=-100111,
            username="mainchannel", title="Main Channel", required=True,
        )
        CHANNELS["second"] = Channel(
            slug="second", channel_id=-100222,
            username="secondchannel", title="Second Channel",
            required=True,
        )

        db.register_user(ADMIN_A, "admin_a", "Admin A")
        db.register_user(ADMIN_B, "admin_b", "Admin B")
        db.register_user(STRANGER, "stranger", "Stranger")
        db.register_user(WORKER, "worker", "Worker")

    def tearDown(self) -> None:
        db.DB_PATH = self._original_db_path
        ADMINS[:] = self._original_admins
        CHANNELS.clear()
        CHANNELS.update(self._original_channels)
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    # ── handler drivers ───────────────────────────────────────────────

    def _start(self, uid=ADMIN_A, chat_type="private"):
        """Run the /addtask wizard entry (start_wizard)."""
        update = mock.MagicMock()
        update.effective_chat.type = chat_type
        update.effective_user.id = uid
        update.message.text = "/addtask"
        update.message.reply_text = mock.AsyncMock()
        asyncio.run(admin_task_wizard.start_wizard(update, mock.MagicMock()))
        return update.message.reply_text

    def _send(self, text, uid=ADMIN_A, chat_type="private"):
        """Run the wizard free-text input handler."""
        update = mock.MagicMock()
        update.effective_chat.type = chat_type
        update.effective_user.id = uid
        update.message.text = text
        update.message.reply_text = mock.AsyncMock()
        asyncio.run(
            admin_task_wizard.wizard_text_input(update, mock.MagicMock())
        )
        return update.message.reply_text

    def _press(self, data, uid=ADMIN_A, chat_type="private"):
        """Run one atw: callback press; returns the callback query mock."""
        query = mock.MagicMock()
        query.data = data
        query.from_user.id = uid
        query.answer = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock()
        update = mock.MagicMock()
        update.callback_query = query
        update.effective_chat.type = chat_type
        update.effective_user.id = uid
        asyncio.run(
            admin_task_wizard.wizard_callback(update, mock.MagicMock())
        )
        return query

    def _answers(self, query) -> list[str]:
        return [
            call.args[0] for call in query.answer.await_args_list
            if call.args
        ]

    # ── draft helpers ─────────────────────────────────────────────────

    def _open(self, uid=ADMIN_A) -> int:
        draft = task_draft_store.get_open_draft(uid)
        self.assertIsNotNone(draft, "expected an open draft")
        return draft.draft_id

    def _draft(self, draft_id) -> task_draft_store.TaskDraft:
        draft = task_draft_store.get_draft(draft_id)
        self.assertIsNotNone(draft)
        return draft

    def _payload(self, draft_id) -> dict:
        return self._draft(draft_id).payload

    def _step(self, draft_id) -> str:
        return self._draft(draft_id).step

    def _tasks(self) -> list[dict]:
        return db.list_tasks()

    # ── full-flow driver ──────────────────────────────────────────────

    def _drive(
        self,
        *,
        title=TITLE,
        family=None,
        provider="instagram",
        target=TARGET,
        action="follow",
        instructions=INSTRUCTIONS,
        verification=VERIFICATION_MANUAL,
        approver=None,
        reward="100",
        repeat=db.REPEAT_POLICY_ONE_TIME,
        hours=None,
        uid=ADMIN_A,
    ) -> int:
        """Walk the wizard to preview; returns the draft id."""
        family = family or _provider_family(provider)
        self._start(uid)
        draft_id = self._open(uid)
        self._send(title, uid)
        self._press(f"atw:{draft_id}:family:{family}", uid)
        self._press(f"atw:{draft_id}:provider:{provider}", uid)
        self._send(target, uid)
        self._press(f"atw:{draft_id}:action:{action}", uid)
        self._send(instructions, uid)
        self._press(f"atw:{draft_id}:verif:{verification}", uid)
        if verification == VERIFICATION_APPROVAL:
            self._press(
                f"atw:{draft_id}:approver:{approver or uid}", uid
            )
        self._send(reward, uid)
        self._press(f"atw:{draft_id}:repeat:{repeat}", uid)
        if repeat == db.REPEAT_POLICY_REPEATABLE:
            self._send(str(hours), uid)
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_PREVIEW)
        return draft_id

    def _confirm(self, draft_id, uid=ADMIN_A, chat_type="private"):
        return self._press(f"atw:{draft_id}:confirm", uid, chat_type)


# ══════════════════════════════════════════════════════════════════
# A. Authorization
# ══════════════════════════════════════════════════════════════════


class TestAuthorization(WizardTestBase):

    def test_admin_private_chat_can_start_wizard(self) -> None:
        reply = self._start(ADMIN_A)
        reply.assert_awaited_once()
        text = reply.await_args.args[0]
        markup = reply.await_args.kwargs.get("reply_markup")
        self.assertIn("عنوان", text)
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        draft = task_draft_store.get_open_draft(ADMIN_A)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.step, admin_task_wizard.STEP_TITLE)
        self.assertEqual(draft.admin_user_id, ADMIN_A)

    def test_non_admin_denied_without_draft(self) -> None:
        reply = self._start(STRANGER)
        reply.assert_awaited_once()
        self.assertIn("للمشرفين فقط", reply.await_args.args[0])
        self.assertIsNone(task_draft_store.get_open_draft(STRANGER))
        self.assertEqual(task_draft_store.get_draft(1), None)

    def test_group_and_channel_invocations_are_silent(self) -> None:
        for chat_type in ("supergroup", "group", "channel"):
            with self.subTest(chat_type=chat_type):
                reply = self._start(ADMIN_A, chat_type=chat_type)
                reply.assert_not_awaited()
        # …and no draft was created by any of them.
        self.assertIsNone(task_draft_store.get_open_draft(ADMIN_A))

    def test_other_admin_cannot_touch_another_admins_draft(self) -> None:
        draft_id = self._drive()
        before = self._payload(draft_id)
        query = self._press(f"atw:{draft_id}:cancel", uid=ADMIN_B)
        answers = self._answers(query)
        self.assertEqual(len(answers), 1)
        self.assertIn("صلاحية", answers[0])
        query.edit_message_text.assert_not_awaited()
        self.assertEqual(self._payload(draft_id), before)
        self.assertIsNotNone(task_draft_store.get_draft(draft_id))

    def test_group_callback_is_silently_dismissed(self) -> None:
        draft_id = self._drive()
        query = self._press(
            f"atw:{draft_id}:confirm", uid=ADMIN_A,
            chat_type="supergroup",
        )
        # Plain dismiss: answer with no text, no edit, no mutation.
        for call in query.answer.await_args_list:
            self.assertEqual(call.args, ())
        query.edit_message_text.assert_not_awaited()
        self.assertEqual(self._tasks(), [])
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_PREVIEW)

    def test_bare_addtask_command_opens_wizard_via_bot(self) -> None:
        from bot import add_task
        update = mock.MagicMock()
        update.effective_chat.type = "private"
        update.effective_user.id = ADMIN_A
        update.message.text = "/addtask"
        update.message.reply_text = mock.AsyncMock()
        asyncio.run(add_task(update, mock.MagicMock()))
        update.message.reply_text.assert_awaited_once()
        self.assertIsNotNone(task_draft_store.get_open_draft(ADMIN_A))
        self.assertEqual(self._tasks(), [])

    def test_bare_addtask_in_group_stays_silent_via_bot(self) -> None:
        from bot import add_task
        update = mock.MagicMock()
        update.effective_chat.type = "supergroup"
        update.effective_user.id = ADMIN_A
        update.message.text = "/addtask"
        update.message.reply_text = mock.AsyncMock()
        asyncio.run(add_task(update, mock.MagicMock()))
        update.message.reply_text.assert_not_awaited()
        self.assertIsNone(task_draft_store.get_open_draft(ADMIN_A))


# ══════════════════════════════════════════════════════════════════
# B. Persistence
# ══════════════════════════════════════════════════════════════════


class TestPersistence(WizardTestBase):

    def test_draft_lives_in_db_and_survives_fresh_connections(self) -> None:
        self._start()
        draft_id = self._open()
        self._send(TITLE)

        # A completely raw SQLite connection (no shared state) sees it.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT admin_user_id, step, status, payload_json "
            "FROM admin_task_drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["admin_user_id"], ADMIN_A)
        self.assertEqual(row["step"], admin_task_wizard.STEP_FAMILY)
        self.assertEqual(row["status"], "open")
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["title"], TITLE)

    def test_step_survives_handler_recreation_and_restart(self) -> None:
        self._start()
        draft_id = self._open()
        self._send(TITLE)
        self._press(f"atw:{draft_id}:family:social")
        self._press(f"atw:{draft_id}:provider:instagram")

        # Restart simulation: reload the handler module (fresh module
        # state, exactly like a process restart) — then continue the
        # SAME draft with brand-new handler invocations.
        importlib.reload(admin_task_wizard)
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_TARGET)
        self._send(TARGET)
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_ACTION)
        payload = self._payload(draft_id)
        self.assertEqual(payload["provider"], "instagram")
        self.assertEqual(payload["target_ref"], TARGET)

    def test_cancel_deletes_the_draft(self) -> None:
        draft_id = self._drive()
        query = self._press(f"atw:{draft_id}:cancel")
        self.assertIsNone(task_draft_store.get_draft(draft_id))
        self.assertIsNone(task_draft_store.get_open_draft(ADMIN_A))
        self.assertIn("تم إلغاء", query.edit_message_text.await_args.args[0])
        self.assertEqual(self._tasks(), [])

    def test_stale_and_corrupt_drafts_fail_safely(self) -> None:
        # Missing draft id → safe stale answer, zero tasks, no crash.
        query = self._press("atw:999999:confirm")
        answers = self._answers(query)
        self.assertEqual(len(answers), 1)
        self.assertIn("مسودة", answers[0])
        self.assertEqual(self._tasks(), [])

        # Corrupt payload_json → treated as missing, still safe.
        self._start()
        draft_id = self._open()
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE admin_task_drafts SET payload_json = '{broken' "
                "WHERE draft_id = ?",
                (draft_id,),
            )
        self.assertIsNone(task_draft_store.get_draft(draft_id))
        query = self._press(f"atw:{draft_id}:confirm")
        self.assertEqual(len(self._answers(query)), 1)
        self.assertEqual(self._tasks(), [])

    def test_only_one_open_draft_per_admin(self) -> None:
        self._start()
        first = self._open()
        again = task_draft_store.get_or_create_open_draft(
            ADMIN_A, admin_task_wizard.STEP_TITLE, {}
        )
        self.assertEqual(again.draft_id, first)


# ══════════════════════════════════════════════════════════════════
# C. Wizard flow
# ══════════════════════════════════════════════════════════════════


class TestWizardFlow(WizardTestBase):

    def test_full_manual_flow_reaches_preview(self) -> None:
        draft_id = self._drive()
        text, markup = admin_task_wizard.render_step(
            self._draft(draft_id)
        )
        self.assertIn(TITLE, text)
        self.assertIn(INSTRUCTIONS, text)
        self.assertIn("instagram", text)
        self.assertIn(TARGET, text)
        self.assertIn("مراجعة يدوية", text)
        self.assertIn("100", text)
        self.assertIn("مرة واحدة", text)
        self.assertIn(f"المراجع المختص: {ADMIN_A}", text)
        # [✅ نشر] [✏️ تعديل] [❌ إلغاء]
        flat = [b for row in markup.inline_keyboard for b in row]
        callbacks = [b.callback_data for b in flat]
        self.assertEqual(
            callbacks,
            [
                f"atw:{draft_id}:confirm",
                f"atw:{draft_id}:edit:menu",
                f"atw:{draft_id}:cancel",
            ],
        )

    def test_provider_step_offers_family_providers_only(self) -> None:
        self._start()
        draft_id = self._open()
        self._send(TITLE)
        self._press(f"atw:{draft_id}:family:app")
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_PROVIDER)
        query = self._press(f"atw:{draft_id}:provider:instagram")
        # instagram is NOT in the app family → rejected, step unchanged.
        answers = self._answers(query)
        self.assertTrue(answers and "غير متاح" in answers[0], answers)
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_PROVIDER)
        self._press(f"atw:{draft_id}:provider:google_play")
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_TARGET)

    def test_approval_flow_selects_explicit_approver(self) -> None:
        draft_id = self._drive(
            verification=VERIFICATION_APPROVAL, approver=ADMIN_B
        )
        payload = self._payload(draft_id)
        self.assertEqual(payload["approver_id"], ADMIN_B)
        text, _markup = admin_task_wizard.render_step(self._draft(draft_id))
        self.assertIn(f"المراجع المختص: {ADMIN_B}", text)

    def test_approver_step_offers_configured_admins(self) -> None:
        self._start()
        draft_id = self._open()
        self._send(TITLE)
        self._press(f"atw:{draft_id}:family:telegram")
        self._press(f"atw:{draft_id}:provider:telegram")
        self._send("main")
        self._press(f"atw:{draft_id}:action:join_channel")
        self._send(INSTRUCTIONS)
        self._press(f"atw:{draft_id}:verif:approval")
        self.assertEqual(
            self._step(draft_id), admin_task_wizard.STEP_APPROVER
        )
        draft = self._draft(draft_id)
        _text, markup = admin_task_wizard.render_step(draft)
        flat = [b for row in markup.inline_keyboard for b in row]
        approver_buttons = [
            b.callback_data for b in flat
            if ":approver:" in b.callback_data
        ]
        self.assertEqual(
            approver_buttons,
            [
                f"atw:{draft_id}:approver:{ADMIN_A}",
                f"atw:{draft_id}:approver:{ADMIN_B}",
            ],
        )
        # A non-admin id can never be chosen.
        query = self._press(f"atw:{draft_id}:approver:{STRANGER}")
        answers = self._answers(query)
        self.assertTrue(answers and "مشرف" in answers[0], answers)
        self.assertEqual(
            self._step(draft_id), admin_task_wizard.STEP_APPROVER
        )

    def test_repeatable_flow_asks_for_hours(self) -> None:
        draft_id = self._drive(
            repeat=db.REPEAT_POLICY_REPEATABLE, hours=24
        )
        payload = self._payload(draft_id)
        self.assertEqual(payload["repeat_policy"], "repeatable")
        self.assertEqual(payload["repeat_hours"], 24)
        text, _ = admin_task_wizard.render_step(self._draft(draft_id))
        self.assertIn("كل 24 ساعة", text)

    def test_invalid_text_at_button_step_is_ignored(self) -> None:
        self._start()
        draft_id = self._open()
        self._send(TITLE)
        reply = self._send("not-a-number")
        # We are on the family step (buttons) — free text is ignored,
        # the draft does not advance or crash.
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_FAMILY)
        reply.assert_not_awaited()

    def test_invalid_text_at_text_step_keeps_step(self) -> None:
        draft_id = self._drive(reward="100")
        self._press(f"atw:{draft_id}:edit:reward")
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_REWARD)
        reply = self._send("abc")
        self.assertIn("المكافأة", reply.await_args.args[0])
        # Still on reward: the previous valid value is untouched.
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_REWARD)
        self.assertEqual(self._payload(draft_id)["reward"], 100)

    def test_edit_menu_roundtrip_returns_to_preview(self) -> None:
        draft_id = self._drive(reward="100")
        # Open the edit menu from preview.
        query = self._press(f"atw:{draft_id}:edit:menu")
        markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        flat = [b for row in markup.inline_keyboard for b in row]
        edit_ops = [b.callback_data for b in flat]
        self.assertIn(f"atw:{draft_id}:edit:reward", edit_ops)
        self.assertIn(f"atw:{draft_id}:edit:back", edit_ops)

        # Jump to reward, type a new value → straight back to preview.
        self._press(f"atw:{draft_id}:edit:reward")
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_REWARD)
        self._send("450")
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_PREVIEW)
        text, _ = admin_task_wizard.render_step(self._draft(draft_id))
        self.assertIn("450", text)
        self.assertNotIn(": 100 نقطة", text)

    def test_reselecting_provider_resets_dependent_fields(self) -> None:
        draft_id = self._drive()
        # Edit provider → pick another provider: action + verification
        # must be re-chosen (the old mapping cannot be trusted).
        self._press(f"atw:{draft_id}:edit:family")
        self._press(f"atw:{draft_id}:family:website")
        payload = self._payload(draft_id)
        self.assertEqual(payload["family"], "website")
        self.assertNotIn("provider", payload)
        self.assertNotIn("action", payload)
        self.assertNotIn("verification", payload)
        # …and the flow re-enters provider selection.
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_PROVIDER)


# ══════════════════════════════════════════════════════════════════
# D. Generic providers
# ══════════════════════════════════════════════════════════════════


class TestGenericProviders(WizardTestBase):

    def test_creation_contracts_for_generic_providers(self) -> None:
        cases = [
            # (provider, action, target)
            ("telegram", "visit", "https://t.me/example"),
            ("instagram", "follow", "https://instagram.com/brand"),
            ("website", "visit", "https://example.com/landing"),
            ("google_play", "download", "com.example.app"),
            ("crypto", "open", "https://example-exchange.com/signup"),
            ("other", "submit_proof", "some-identifier"),
        ]
        for provider, action, target in cases:
            with self.subTest(provider=provider):
                draft_id = self._drive(
                    provider=provider,
                    action=action,
                    target=target,
                    title=f"مهمة {provider}",
                )
                self._confirm(draft_id)
                tasks = self._tasks()
                self.assertEqual(len(tasks), 1, tasks)
                task = tasks[0]
                self.assertEqual(task["type"], MANUAL_TASK_TYPE)
                data = json.loads(task["task_data"])
                self.assertEqual(data["provider"], provider)
                self.assertEqual(data["action"], action)
                self.assertEqual(
                    data["target"], {"ref": target}
                )
                self.assertEqual(
                    data["approver"], {"telegram_user_id": ADMIN_A}
                )
                # Reader-side parser accepts the stored contract.
                from manual_task import parse_manual_task_data
                parse_manual_task_data(task["task_data"])
                # Visible through the existing read-only catalog API.
                from task_catalog import TaskCatalog
                catalog = TaskCatalog().list_available_tasks()
                self.assertIn(
                    task["title"], [t.title for t in catalog]
                )
                # Clean slate for the next provider.
                with db.get_connection() as conn:
                    conn.execute("DELETE FROM tasks")
                    conn.execute("DELETE FROM admin_task_drafts")


# ══════════════════════════════════════════════════════════════════
# E. Telegram task compatibility
# ══════════════════════════════════════════════════════════════════


class TestTelegramCompatibility(WizardTestBase):

    def test_auto_join_uses_exact_nested_contract(self) -> None:
        draft_id = self._drive(
            family="telegram",
            provider="telegram",
            action="join_channel",
            target="@main",
            verification=VERIFICATION_AUTO,
            title="انضم لقناتنا",
        )
        self._confirm(draft_id)
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["type"], TELEGRAM_CHANNEL_TASK_TYPE)
        data = json.loads(task["task_data"])
        # EXACT MT-TASK-05 shape — nested target, no flat keys, no
        # wizard extras (approver/target.ref never leak in).
        self.assertEqual(
            data,
            {
                "provider": "telegram",
                "action": "join_channel",
                "target": {"channel_slug": "main"},
                "instructions": INSTRUCTIONS,
            },
        )
        # The existing reader accepts it (parsed round-trip).
        parse_telegram_channel_task_data(task["task_data"])

    def test_existing_telegram_verifier_is_registered(self) -> None:
        from task_verifier import get_verifier
        self.assertIsNotNone(get_verifier(TELEGRAM_CHANNEL_TASK_TYPE))

    def test_unregistered_channel_slug_rejected_for_auto(self) -> None:
        self._start()
        draft_id = self._open()
        self._send("انضم")
        self._press(f"atw:{draft_id}:family:telegram")
        self._press(f"atw:{draft_id}:provider:telegram")
        self._send("no_such_slug")
        self._press(f"atw:{draft_id}:action:join_channel")
        self._send(INSTRUCTIONS)
        query = self._press(f"atw:{draft_id}:verif:auto")
        answers = self._answers(query)
        self.assertTrue(
            answers and "غير موجود في سجل القنوات" in answers[0], answers
        )
        # Still on verification, never publishable → zero tasks.
        self.assertEqual(
            self._step(draft_id), admin_task_wizard.STEP_VERIFICATION
        )
        self._confirm(draft_id)
        self.assertEqual(self._tasks(), [])

    def test_auto_capability_never_offered_for_other_providers(self) -> None:
        self._start()
        draft_id = self._open()
        self._send(TITLE)
        self._press(f"atw:{draft_id}:family:website")
        self._press(f"atw:{draft_id}:provider:website")
        self._send(TARGET)
        self._press(f"atw:{draft_id}:action:visit")
        self._send(INSTRUCTIONS)
        self.assertEqual(
            self._step(draft_id), admin_task_wizard.STEP_VERIFICATION
        )
        draft = self._draft(draft_id)
        _text, markup = admin_task_wizard.render_step(draft)
        flat = [b for row in markup.inline_keyboard for b in row]
        verif_ops = [
            b.callback_data for b in flat if ":verif:" in b.callback_data
        ]
        self.assertNotIn(f"atw:{draft_id}:verif:auto", verif_ops)
        self.assertIn(f"atw:{draft_id}:verif:manual", verif_ops)
        self.assertIn(f"atw:{draft_id}:verif:approval", verif_ops)

    def test_legacy_pipe_still_creates_the_same_contract(self) -> None:
        """The retained /addtask pipe delegates into task_creation —
        identical reader-accepted output as before MT-ADMIN-05."""
        from bot import add_task
        update = mock.MagicMock()
        update.effective_chat.type = "private"
        update.effective_user.id = ADMIN_A
        update.message.text = "/addtask عنوان | وصف | 500 | main"
        update.message.reply_text = mock.AsyncMock()
        asyncio.run(add_task(update, mock.MagicMock()))
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["type"], TELEGRAM_CHANNEL_TASK_TYPE)
        self.assertEqual(task["reward"], 500)
        data = json.loads(task["task_data"])
        self.assertEqual(data["target"], {"channel_slug": "main"})
        parse_telegram_channel_task_data(task["task_data"])


# ══════════════════════════════════════════════════════════════════
# F. Manual task compatibility
# ══════════════════════════════════════════════════════════════════


class TestManualCompatibility(WizardTestBase):

    def _publish_generic_manual_task(self) -> int:
        draft_id = self._drive(
            provider="instagram",
            action="follow",
            target="https://instagram.com/brand",
            verification=VERIFICATION_MANUAL,
        )
        self._confirm(draft_id)
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        return tasks[0]["id"]

    def test_generic_task_flows_through_existing_services(self) -> None:
        task_id = self._publish_generic_manual_task()

        # Worker side: start gate + manual proof submission (the
        # EXISTING MT-TASK-15 services, untouched by the wizard).
        TaskStartGate().start(WORKER, task_id)
        outcome = ManualProofService.submit(
            WORKER, task_id, PROOF_REF, "wiz-proof-1"
        )
        self.assertEqual(outcome.state, "pending")

        # Reviewer side: only the task-specific approver may decide.
        with self.assertRaises(ManualDecisionError):
            ManualReviewService.decide(
                ADMIN_B, task_id, outcome.submission_id, approve=True
            )
        ManualReviewService.decide(
            ADMIN_A, task_id, outcome.submission_id, approve=True
        )
        from task_submission_store import TaskSubmissionStore
        record = TaskSubmissionStore.get_submission(outcome.submission_id)
        self.assertEqual(record.approval_status, "approved")
        self.assertEqual(record.approver_user_id, ADMIN_A)

    def test_generic_manual_target_and_action_preserved(self) -> None:
        task_id = self._publish_generic_manual_task()
        task = db.get_task(task_id)
        data = json.loads(task["task_data"])
        self.assertEqual(data["provider"], "instagram")
        self.assertEqual(data["action"], "follow")
        self.assertEqual(
            data["target"], {"ref": "https://instagram.com/brand"}
        )
        # Worker-visible instructions live in description.
        self.assertEqual(task["description"], INSTRUCTIONS)

    def test_approval_mode_publishes_same_authority_contract(self) -> None:
        draft_id = self._drive(
            provider="reddit",
            action="comment",
            target="https://reddit.com/r/test",
            verification=VERIFICATION_APPROVAL,
            approver=ADMIN_B,
        )
        self._confirm(draft_id)
        data = json.loads(self._tasks()[0]["task_data"])
        self.assertEqual(
            data["approver"], {"telegram_user_id": ADMIN_B}
        )
        # Authorization is STILL contract-based: the creator (ADMIN_A)
        # is not the approver and cannot decide.
        task_id = self._tasks()[0]["id"]
        TaskStartGate().start(WORKER, task_id)
        outcome = ManualProofService.submit(
            WORKER, task_id, PROOF_REF, "wiz-proof-2"
        )
        with self.assertRaises(ManualDecisionError):
            ManualReviewService.decide(
                ADMIN_A, task_id, outcome.submission_id, approve=True
            )
        ManualReviewService.decide(
            ADMIN_B, task_id, outcome.submission_id, approve=True
        )


# ══════════════════════════════════════════════════════════════════
# G. Validation
# ══════════════════════════════════════════════════════════════════


class TestValidation(WizardTestBase):

    def _start_draft(self) -> int:
        self._start()
        return self._open()

    def test_empty_title_rejected(self) -> None:
        draft_id = self._start_draft()
        reply = self._send("   ")
        self.assertIn("العنوان", reply.await_args.args[0])
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_TITLE)

    def test_empty_instructions_rejected(self) -> None:
        draft_id = self._drive()
        # Re-enter the instructions step and send whitespace only.
        self._press(f"atw:{draft_id}:edit:instructions")
        reply = self._send("\n   \n")
        self.assertIn("التعليمات", reply.await_args.args[0])
        self.assertEqual(
            self._step(draft_id), admin_task_wizard.STEP_INSTRUCTIONS
        )

    def test_control_characters_rejected(self) -> None:
        self._start()
        draft_id = self._open()
        self._send(TITLE)
        self._press(f"atw:{draft_id}:family:social")
        self._press(f"atw:{draft_id}:provider:instagram")
        reply = self._send("bad\x00target\x1f")
        self.assertTrue(
            "غير مسموحة" in reply.await_args.args[0], reply.await_args.args[0]
        )
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_TARGET)

    def test_empty_target_rejected(self) -> None:
        self._start()
        draft_id = self._open()
        self._send(TITLE)
        self._press(f"atw:{draft_id}:family:social")
        self._press(f"atw:{draft_id}:provider:instagram")
        reply = self._send("   ")
        self.assertIn("الهدف", reply.await_args.args[0])
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_TARGET)

    def test_malformed_and_negative_rewards_rejected(self) -> None:
        draft_id = self._drive()
        self._press(f"atw:{draft_id}:edit:reward")
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_REWARD)
        for bad in ("abc", "-5", "5.5", "", "٥.٥"):
            with self.subTest(reward=bad):
                reply = self._send(bad)
                self.assertIn("المكافأة", reply.await_args.args[0])
                self.assertEqual(
                    self._step(draft_id), admin_task_wizard.STEP_REWARD
                )
                self.assertEqual(self._payload(draft_id)["reward"], 100)

    def test_invalid_repeat_hours_rejected(self) -> None:
        self._start()
        draft_id = self._open()
        self._send(TITLE)
        self._press(f"atw:{draft_id}:family:social")
        self._press(f"atw:{draft_id}:provider:instagram")
        self._send(TARGET)
        self._press(f"atw:{draft_id}:action:follow")
        self._send(INSTRUCTIONS)
        self._press(f"atw:{draft_id}:verif:manual")
        self._send("50")
        self._press(f"atw:{draft_id}:repeat:repeatable")
        for bad in ("0", "-1", "abc"):
            with self.subTest(hours=bad):
                reply = self._send(bad)
                self.assertIn("الساعات", reply.await_args.args[0])
                self.assertEqual(
                    self._step(draft_id),
                    admin_task_wizard.STEP_REPEAT_HOURS,
                )
                self.assertIsNone(
                    self._payload(draft_id).get("repeat_hours")
                )

    def test_incompatible_verification_rejected(self) -> None:
        self._start()
        draft_id = self._open()
        self._send(TITLE)
        self._press(f"atw:{draft_id}:family:crypto")
        self._press(f"atw:{draft_id}:provider:crypto")
        self._send("https://example.com")
        self._press(f"atw:{draft_id}:action:open")
        self._send(INSTRUCTIONS)
        query = self._press(f"atw:{draft_id}:verif:auto")
        answers = self._answers(query)
        self.assertTrue(answers and "التحقق التلقائي" in answers[0], answers)
        self.assertEqual(
            self._step(draft_id), admin_task_wizard.STEP_VERIFICATION
        )
        self.assertNotIn("verification", self._payload(draft_id))

    def test_overlong_title_and_instructions_rejected(self) -> None:
        draft_id = self._start_draft()
        reply = self._send("ب" * (task_taxonomy.MAX_TITLE_LENGTH + 1))
        self.assertIn("العنوان", reply.await_args.args[0])
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_TITLE)

        self._send(TITLE)
        self._press(f"atw:{draft_id}:family:social")
        self._press(f"atw:{draft_id}:provider:instagram")
        self._send(TARGET)
        self._press(f"atw:{draft_id}:action:follow")
        reply = self._send("ب" * (task_taxonomy.MAX_INSTRUCTIONS_LENGTH + 1))
        self.assertIn("التعليمات", reply.await_args.args[0])
        self.assertEqual(
            self._step(draft_id), admin_task_wizard.STEP_INSTRUCTIONS
        )


# ══════════════════════════════════════════════════════════════════
# H. Security
# ══════════════════════════════════════════════════════════════════


class TestSecurity(WizardTestBase):

    def test_forged_draft_id_is_lookup_only_and_fails_safe(self) -> None:
        for data in (
            "atw:424242:confirm",
            "atw:0:confirm",
            "atw:-5:cancel",
            "atw:abc:confirm",
            "atw:1:2:3:4",
            "atw:",
            "atw:1:unknown",
            "atw:1:confirm:extra",
            "other:1:confirm",
            None,
            7,
        ):
            with self.subTest(data=data):
                query = self._press(data, uid=ADMIN_A)
                self.assertTrue(self._answers(query))
                query.edit_message_text.assert_not_awaited()
        self.assertEqual(self._tasks(), [])

    def test_forged_provider_and_action_tokens_rejected(self) -> None:
        draft_id = self._drive()
        before = dict(self._payload(draft_id))
        for data in (
            f"atw:{draft_id}:provider:web",
            f"atw:{draft_id}:provider:nope",
            f"atw:{draft_id}:action:referral",
            f"atw:{draft_id}:action:join_channel",  # not instagram's set
            f"atw:{draft_id}:verif:auto",            # capability gate
            f"atw:{draft_id}:approver:{STRANGER}",
            f"atw:{draft_id}:repeat:forever",
        ):
            with self.subTest(data=data):
                query = self._press(data)
                self.assertTrue(self._answers(query))
                query.edit_message_text.assert_not_awaited()
                self.assertEqual(self._payload(draft_id), before)
        # Still exactly where it was — preview, ready to publish.
        self.assertEqual(self._step(draft_id), admin_task_wizard.STEP_PREVIEW)

    def test_callback_data_cannot_carry_reward_or_target(self) -> None:
        # The callback protocol has no op that writes reward/target/
        # title — unknown ops are rejected outright (parse level).
        draft_id = self._drive()
        before = dict(self._payload(draft_id))
        for data in (
            f"atw:{draft_id}:reward:99999",
            f"atw:{draft_id}:target:evil.example",
            f"atw:{draft_id}:title:hacked",
            f"atw:{draft_id}:reward",
        ):
            with self.subTest(data=data):
                query = self._press(data)
                self.assertIn(
                    admin_task_wizard.MSG_INVALID, self._answers(query)
                )
                query.edit_message_text.assert_not_awaited()
        self.assertEqual(self._payload(draft_id), before)
        # And the values it CAN carry never bypass validation (above).

    def test_non_admin_identity_gets_no_state_changes(self) -> None:
        draft_id = self._drive()
        before = dict(self._payload(draft_id))
        for op in ("confirm", "cancel", "edit:menu", "family:social"):
            query = self._press(f"atw:{draft_id}:{op}", uid=STRANGER)
            answers = self._answers(query)
            self.assertTrue(
                answers and "للمشرفين فقط" in answers[0], answers
            )
            query.edit_message_text.assert_not_awaited()
        self.assertEqual(self._payload(draft_id), before)
        self.assertEqual(self._tasks(), [])
        self.assertIsNotNone(task_draft_store.get_draft(draft_id))

    def test_cross_admin_confirm_cannot_publish(self) -> None:
        draft_id = self._drive()
        query = self._confirm(draft_id, uid=ADMIN_B)
        answers = self._answers(query)
        self.assertTrue(answers and "صلاحية" in answers[0], answers)
        query.edit_message_text.assert_not_awaited()
        self.assertEqual(self._tasks(), [])
        # The owner can still publish afterwards.
        self._confirm(draft_id, uid=ADMIN_A)
        self.assertEqual(len(self._tasks()), 1)


# ══════════════════════════════════════════════════════════════════
# I. Publish idempotency
# ══════════════════════════════════════════════════════════════════


class TestPublishIdempotency(WizardTestBase):

    def test_one_confirm_creates_exactly_one_task(self) -> None:
        draft_id = self._drive()
        query = self._confirm(draft_id)
        self.assertEqual(len(self._tasks()), 1)
        # Success message replaces the preview (no buttons left).
        text = query.edit_message_text.await_args.args[0]
        markup = query.edit_message_text.await_args.kwargs.get(
            "reply_markup"
        )
        self.assertIn("تم نشر المهمة", text)
        self.assertIsNone(markup)
        self.assertEqual(
            self._draft(draft_id).status, "published"
        )
        self.assertEqual(
            self._draft(draft_id).published_task_id,
            self._tasks()[0]["id"],
        )

    def test_replayed_confirm_returns_the_same_task(self) -> None:
        draft_id = self._drive()
        first = self._confirm(draft_id)
        first_text = first.edit_message_text.await_args.args[0]
        self.assertEqual(len(self._tasks()), 1)

        # The button press is replayed (Telegram redelivery).
        second = self._confirm(draft_id)
        self.assertEqual(len(self._tasks()), 1,
                         "replay must not create a second task")
        second_text = second.edit_message_text.await_args.args[0]
        self.assertEqual(first_text, second_text,
                         "replay must surface the SAME task id")

    def test_concurrent_confirmation_creates_one_task(self) -> None:
        draft_id = self._drive()
        results: list[int] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def contender() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(
                    admin_task_wizard.publish_draft(draft_id, ADMIN_A)
                )
            except BaseException as exc:  # noqa: BLE001 — recorded
                errors.append(exc)

        threads = [
            threading.Thread(target=contender),
            threading.Thread(target=contender),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 1,
                         "both contenders must resolve to one task")
        self.assertEqual(len(self._tasks()), 1)

    def test_failed_validation_creates_zero_tasks(self) -> None:
        draft_id = self._drive()
        # Corrupt the persisted payload server-side (simulating stale/
        # broken state): reward becomes a non-integer.
        payload = dict(self._payload(draft_id))
        payload["reward"] = "not-a-number"
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE admin_task_drafts SET payload_json = ? "
                "WHERE draft_id = ?",
                (json.dumps(payload, ensure_ascii=False), draft_id),
            )
        query = self._confirm(draft_id)
        answers = self._answers(query)
        self.assertTrue(answers and "تعذر النشر" in answers[0], answers)
        self.assertEqual(self._tasks(), [])
        # Draft stays open for correction — never half-published.
        self.assertEqual(self._draft(draft_id).status, "open")

    def test_approval_draft_falls_back_to_creating_admin(self) -> None:
        # An approval draft missing its explicit approver choice can
        # still publish — falling back to the creating admin, who is
        # an authorized admin and the draft's owner.  Authorization
        # never weakens to "anyone".
        self._start()
        draft_id = self._open()
        payload = dict(self._payload(draft_id))
        payload.update({
            "title": TITLE,
            "family": "social",
            "provider": "instagram",
            "target_ref": TARGET,
            "action": "follow",
            "instructions": INSTRUCTIONS,
            "verification": VERIFICATION_APPROVAL,
            "reward": 10,
            "repeat_policy": db.REPEAT_POLICY_ONE_TIME,
            # approver_id intentionally missing
        })
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE admin_task_drafts SET payload_json = ?, step = ? "
                "WHERE draft_id = ?",
                (json.dumps(payload, ensure_ascii=False),
                 admin_task_wizard.STEP_PREVIEW, draft_id),
            )
        # Fallback to the creating admin still publishes exactly one
        # authorized task — authorization never weakens.
        task_id = admin_task_wizard.publish_draft(draft_id, ADMIN_A)
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], task_id)
        data = json.loads(tasks[0]["task_data"])
        self.assertEqual(
            data["approver"], {"telegram_user_id": ADMIN_A}
        )


if __name__ == "__main__":
    unittest.main()
