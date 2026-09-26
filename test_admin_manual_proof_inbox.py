"""
Focused tests — Admin Manual-Proof Inbox (MT-ADMIN-03)
======================================================

Covers the Telegram admin review surface for manual proof claims:

    worker → ManualProofService.submit → pending claim
           → persistent admin_notifications linkage
           → AdminNotifier (config.ADMINS private chats ONLY)
           → Arabic message with ✅ Approve / ❌ Reject buttons
           → untrusted callback → linkage-resolved server state
           → task_data.approver authorization (NO admin bypass)
           → ManualReviewService.decide (SOLE decision path)

Coverage required by MT-ADMIN-03:

A. Notification persistence
   - fresh pending claim creates exactly one linkage per admin
   - linkage survives a fresh DB connection / re-init
   - correct operation / claim / admin chat / message mapping
B. Submission integration
   - first valid manual proof submission notifies (HTTP end-to-end)
   - duplicate idempotent submission does not notify twice
   - invalid proof does not notify
   - referral claims never touch the manual-proof notification path
   - notification exposes only safe fields (no worker, no approver,
     no raw task_data)
C. Authorization
   - authorized task approver can approve / reject (even when NOT a
     config admin — task-specific authority)
   - non-approver admin cannot decide
   - non-admin cannot decide
   - callback cannot override actor identity
D. Security
   - forged claim ids / forged payload shapes cannot operate
   - linkage must exist server-side (resolved, never trusted)
   - a non-manual claim cannot be decided through this handler
   - required channels/groups receive zero messages
E. Race / idempotency
   - approve twice → one completion / one reward
   - reject twice → one rejection
   - conflicting decision → already-decided, no state change
   - stale callback after a decision mutates nothing
   - notification replay creates no duplicate pending notification
F. Telegram UX
   - pending message carries Approve/Reject buttons
   - decisions edit the message to an inert, keyboard-free state
   - Arabic responses for unauthorized / stale / already-decided
   - proof_ref rendered verbatim as plain text (no markup parsing)

Run:
    python3 -m pytest test_admin_manual_proof_inbox.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from telegram import InlineKeyboardMarkup

import config
import db
import serve_miniapp
import wallet
from admin_notification_store import AdminNotificationStore
from admin_notifier import AdminNotifier
from config import Channel
from manual_proof_inbox import (
    BUTTON_APPROVE,
    BUTTON_REJECT,
    MSG_ALREADY_DECIDED,
    MSG_INVALID,
    MSG_STALE,
    MSG_UNAUTHORIZED,
    OPERATION_MANUAL_PROOF,
    build_pending_text,
    callback_data,
    handle_callback,
    parse_callback,
    schedule_pending_notification,
    unbind,
    bind,
)
from manual_task import MANUAL_TASK_TYPE, ManualProofError, ManualProofService
from referral_task import REFERRAL_TASK_TYPE, ReferralClaimService
from task_start import TaskStartGate
from task_submission_store import TaskSubmissionStore

# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

# Task approver — deliberately NOT a config admin: authorization for
# decisions must come from task_data.approver, never from ADMINS.
APPROVER = 5101
WORKER = 6202
ADMIN_A = 7707  # config admin, NOT the approver
ADMIN_B = 7708  # second config admin (multi-admin notification)
STRANGER = 8808  # neither admin nor approver
REFERRED = 9909  # referred_by = WORKER (genuine referral)

REQUIRED_CHANNEL_ID = -1001234567890

REWARD_USDT = 30
REWARD_UNITS = REWARD_USDT * wallet.USDT_SCALE

PROOF = "https://t.me/c/1234567890/99"
# A proof that looks like Telegram markup must be rendered verbatim —
# this surface never uses a parse mode.
PROOF_MARKUPY = "*bold* _ital_ https://example.com/p?x=1&y=2"


def _valid_manual_task_data(approver: int = APPROVER) -> dict:
    return {
        "provider": "telegram",
        "action": "proof",
        "approver": {"telegram_user_id": approver},
    }


def _valid_referral_task_data(approver: int = APPROVER) -> dict:
    return {
        "provider": "telegram",
        "action": "referral",
        "target": {"bot_username": "my_task_bot"},
        "approver": {"telegram_user_id": approver},
    }


def _run_now(coroutine):
    """Test scheduler: run the delivery coroutine synchronously."""
    return asyncio.run(coroutine)


# ── Shared fixture ───────────────────────────────────────────────────


class InboxTestBase(unittest.TestCase):
    """Temp DB + patched ADMINS + fake AdminNotifier transports bound
    to the inbox (exactly what bot.py wires in production)."""

    def setUp(self) -> None:
        # Temp database.
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.addCleanup(self._restore_db)

        # Patch config.ADMINS **in place** (the list object admin_notifier
        # imported at module load) so targeting checks see the test admins.
        self._orig_admins = list(config.ADMINS)
        config.ADMINS[:] = [ADMIN_A, ADMIN_B]
        self.addCleanup(self._restore_admins)

        # Fake Telegram transports captured through the REAL AdminNotifier.
        self.sent: list[dict] = []
        self._msg_seq = 0

        async def _send_text(chat_id: int, text: str) -> None:
            self.sent.append(
                {
                    "kind": "text",
                    "chat_id": chat_id,
                    "text": text,
                    "reply_markup": None,
                    "message_id": None,
                }
            )

        async def _send_markup(
            chat_id: int, text: str, reply_markup
        ) -> int:
            self._msg_seq += 1
            self.sent.append(
                {
                    "kind": "markup",
                    "chat_id": chat_id,
                    "text": text,
                    "reply_markup": reply_markup,
                    "message_id": self._msg_seq,
                }
            )
            return self._msg_seq

        self.notifier = AdminNotifier(_send_text, markup_send=_send_markup)
        bind(self.notifier, _run_now)
        self.addCleanup(unbind)

        # initData environment for the HTTP submission test.
        env_patcher = mock.patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": _TEST_BOT_TOKEN}
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        serve_miniapp.app.config["TESTING"] = True

        # Users + one manual proof task + one referral task.
        db.register_user(APPROVER, "approver", "Approver")
        db.register_user(WORKER, "worker", "Worker")
        db.register_user(STRANGER, "stranger", "Stranger")
        db.register_user(
            REFERRED, "referred", "Referred", referred_by=WORKER
        )

        self.task_id = db.create_task(
            title="مهمة إثبات يدوية",
            description="أرسل إثباتاً وانتظر مراجعة المشرف",
            task_type=MANUAL_TASK_TYPE,
            reward=REWARD_USDT,
            task_data=json.dumps(_valid_manual_task_data()),
        )
        self.referral_task_id = db.create_task(
            title="مهمة إحالة",
            description="أحضر مستخدمًا جديدًا",
            task_type=REFERRAL_TASK_TYPE,
            reward=REWARD_USDT,
            task_data=json.dumps(_valid_referral_task_data()),
        )

    # ── cleanup ──────────────────────────────────────────────────────

    def _restore_db(self) -> None:
        db.DB_PATH = self._orig_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def _restore_admins(self) -> None:
        config.ADMINS[:] = self._orig_admins

    # ── flow helpers ─────────────────────────────────────────────────

    def _start(self, user_id: int = WORKER, task_id: int | None = None) -> None:
        TaskStartGate().start(user_id, task_id or self.task_id)

    def _submit(
        self, proof: str = PROOF, key: str = "k1"
    ) -> ManualProofService.submit:
        return ManualProofService.submit(WORKER, self.task_id, proof, key)

    def _http_submit(self, proof: str, key: str | None = None):
        client = serve_miniapp.app.test_client()
        headers = {INIT_DATA_HEADER: _make_init_data(user_id=WORKER)}
        if key is not None:
            headers["Idempotency-Key"] = key
        return client.post(
            f"/api/tasks/{self.task_id}/submit",
            json={"proof_ref": proof},
            headers=headers,
        )

    def _press(self, data, actor_user_id: int):
        """Run one untrusted callback through the inbox handler."""
        answers: list[str] = []
        edits: list[tuple[int, int, str]] = []

        async def answer(text: str) -> None:
            answers.append(text)

        async def edit(chat_id: int, message_id: int, text: str) -> None:
            edits.append((chat_id, message_id, text))

        status = asyncio.run(
            handle_callback(data, actor_user_id, answer=answer, edit=edit)
        )
        return status, answers, edits

    # ── state readers ────────────────────────────────────────────────

    def _linkages(self, submission_id: int):
        return AdminNotificationStore.list_for_operation(
            OPERATION_MANUAL_PROOF, submission_id
        )

    def _all_linkage_rows(self) -> list[sqlite3.Row]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM admin_notifications ORDER BY id"
            ).fetchall()

    def _record(self, submission_id: int):
        return TaskSubmissionStore.get_submission(submission_id)

    def _user_task_status(self, user_id: int, task_id: int | None = None):
        row = db.get_user_task(user_id, task_id or self.task_id)
        return row["status"] if row else None

    def _credits(self, user_id: int = WORKER) -> list[int]:
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT amount_units FROM ledger "
                "WHERE user_id = ? AND entry_type = 'credit' "
                "AND reference_type = 'task' ORDER BY id",
                (user_id,),
            ).fetchall()
        return [r["amount_units"] for r in rows]

    def _pending_notifications(self) -> list[dict]:
        return [s for s in self.sent if s["kind"] == "markup"]


# ══════════════════════════════════════════════════════════════════
# A. Notification persistence
# ══════════════════════════════════════════════════════════════════


class TestNotificationPersistence(InboxTestBase):

    def test_fresh_pending_claim_creates_one_linkage_per_admin(self) -> None:
        """A fresh pending claim links exactly ONE message per admin."""
        self._start()
        outcome = self._submit()
        self.assertEqual(outcome.state, "pending")

        rows = self._all_linkage_rows()
        self.assertEqual(len(rows), len(config.ADMINS))
        self.assertEqual(
            {r["admin_chat_id"] for r in rows}, {ADMIN_A, ADMIN_B}
        )

    def test_linkage_survives_fresh_connection_and_reinit(self) -> None:
        """Linkage is durable SQLite state, not in-memory state."""
        self._start()
        sid = self._submit().submission_id

        # Idempotent re-init on the same DB file.
        db.init_db(self.db_path)

        # Brand-new raw connection (a fresh "process" view).
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT operation_type, operation_id, admin_chat_id, "
                "message_id, created_at FROM admin_notifications "
                "ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["operation_type"], OPERATION_MANUAL_PROOF)
            self.assertEqual(row["operation_id"], sid)
            self.assertIn(row["admin_chat_id"], (ADMIN_A, ADMIN_B))
            self.assertGreater(row["message_id"], 0)
            self.assertTrue(row["created_at"])

    def test_linkage_maps_operation_claim_chat_and_message(self) -> None:
        """Operation / claim / chat / message mapping is exact."""
        self._start()
        sid = self._submit().submission_id

        links = self._linkages(sid)
        self.assertEqual(
            {(l.admin_chat_id, l.message_id) for l in links},
            {(s["chat_id"], s["message_id"]) for s in self._pending_notifications()},
        )
        for link in links:
            self.assertEqual(link.operation_type, OPERATION_MANUAL_PROOF)
            self.assertEqual(link.operation_id, sid)
            self.assertGreater(link.id, 0)

    def test_linkage_is_unique_per_operation_and_chat(self) -> None:
        """UNIQUE(operation, claim, chat): a replay inserts nothing."""
        self._start()
        sid = self._submit().submission_id
        before = len(self._all_linkage_rows())

        duplicated = AdminNotificationStore.create_linkage(
            OPERATION_MANUAL_PROOF, sid, ADMIN_A, 4242
        )
        self.assertIsNone(duplicated)
        self.assertEqual(len(self._all_linkage_rows()), before)


# ══════════════════════════════════════════════════════════════════
# B. Submission integration
# ══════════════════════════════════════════════════════════════════


class TestSubmissionIntegration(InboxTestBase):

    def test_first_valid_submission_notifies_admins_http(self) -> None:
        """End-to-end: POST submit → exactly one notification per admin."""
        self._start()
        resp = self._http_submit(PROOF, key="http-1")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["approval"], "pending")

        notices = self._pending_notifications()
        self.assertEqual(len(notices), 2)
        self.assertEqual(
            {n["chat_id"] for n in notices}, {ADMIN_A, ADMIN_B}
        )
        text = notices[0]["text"]
        self.assertIn("مهمة إثبات يدوية", text)
        self.assertIn(f"#{self.task_id}", text)
        self.assertIn(PROOF, text)

        # One linkage row per notified admin, mapped to the sent messages.
        sid = TaskSubmissionStore.get_latest_claim(
            WORKER, self.task_id
        ).submission_id
        links = self._linkages(sid)
        self.assertEqual(
            {(l.admin_chat_id, l.message_id) for l in links},
            {(n["chat_id"], n["message_id"]) for n in notices},
        )

    def test_duplicate_idempotent_submission_does_not_notify_twice(self) -> None:
        """Same Idempotency-Key replay resolves the claim — no re-notify."""
        self._start()
        first = self._submit(key="dup")
        self.assertEqual(first.state, "pending")

        replay = ManualProofService.submit(WORKER, self.task_id, PROOF, "dup")
        self.assertEqual(replay.submission_id, first.submission_id)

        self.assertEqual(len(self._pending_notifications()), 2)
        self.assertEqual(len(self._all_linkage_rows()), 2)

    def test_resubmit_while_pending_does_not_notify_again(self) -> None:
        """A different key resolving the existing open claim re-notifies nothing."""
        self._start()
        self._submit(key="a")
        resolved = ManualProofService.submit(WORKER, self.task_id, PROOF, "b")
        self.assertEqual(resolved.state, "pending")

        self.assertEqual(len(self._pending_notifications()), 2)
        self.assertEqual(len(self._all_linkage_rows()), 2)

    def test_invalid_proof_does_not_notify(self) -> None:
        """No pending claim → no notification, no linkage."""
        self._start()
        for bad in ("", "   ", "x" * 501, "line\nbreak"):
            with self.assertRaises(ManualProofError):
                ManualProofService.submit(WORKER, self.task_id, bad, "bad")
        self.assertEqual(self.sent, [])
        self.assertEqual(self._all_linkage_rows(), [])

    def test_referral_claim_never_notifies_manual_path(self) -> None:
        """Referral claims go through ReferralClaimService — zero inbox traffic."""
        self._start()
        TaskStartGate().start(WORKER, self.referral_task_id)
        outcome = ReferralClaimService.submit(
            WORKER, self.referral_task_id, "ref-key-1"
        )
        self.assertEqual(outcome.state, "pending")

        self.assertEqual(self.sent, [])
        self.assertEqual(self._all_linkage_rows(), [])

    def test_notification_exposes_only_safe_fields(self) -> None:
        """No worker identity, no approver id, no raw task_data in the message."""
        self._start()
        self._submit()
        text = self._pending_notifications()[0]["text"]

        self.assertNotIn(str(WORKER), text)
        self.assertNotIn(str(APPROVER), text)
        self.assertNotIn("task_data", text)
        self.assertNotIn("approver", text)
        self.assertNotIn("reward", text.lower())


# ══════════════════════════════════════════════════════════════════
# C. Authorization
# ══════════════════════════════════════════════════════════════════


class TestAuthorization(InboxTestBase):

    def _claim(self) -> int:
        self._start()
        return self._submit().submission_id

    def test_authorized_approver_can_approve(self) -> None:
        """The task approver decides — even though they are NOT in ADMINS."""
        self.assertNotIn(APPROVER, config.ADMINS)
        sid = self._claim()

        status, answers, edits = self._press(
            callback_data(True, sid), APPROVER
        )
        self.assertEqual(status, "approved")
        self.assertEqual(self._record(sid).approval_status, "approved")
        self.assertEqual(
            self._user_task_status(WORKER), db.USER_TASK_STATUS_COMPLETED
        )
        self.assertEqual(self._credits(), [REWARD_UNITS])
        self.assertTrue(edits)

    def test_authorized_approver_can_reject(self) -> None:
        sid = self._claim()

        status, answers, edits = self._press(
            callback_data(False, sid), APPROVER
        )
        self.assertEqual(status, "rejected")
        record = self._record(sid)
        self.assertEqual(record.approval_status, "rejected")
        self.assertEqual(record.status, db.SUBMISSION_STATUS_FAILED)
        # No completion, no reward; the worker may retry.
        self.assertEqual(
            self._user_task_status(WORKER), db.USER_TASK_STATUS_STARTED
        )
        self.assertEqual(self._credits(), [])
        self.assertTrue(edits)

    def test_non_approver_admin_cannot_decide(self) -> None:
        """ADMINS receive the notification but hold NO decision authority."""
        sid = self._claim()

        status, answers, edits = self._press(
            callback_data(True, sid), ADMIN_A
        )
        self.assertEqual(status, "unauthorized")
        self.assertEqual(answers, [MSG_UNAUTHORIZED])
        self.assertEqual(edits, [], "unauthorized presses must not edit")
        self.assertEqual(self._record(sid).approval_status, "pending")
        self.assertEqual(self._credits(), [])

    def test_non_admin_cannot_decide(self) -> None:
        sid = self._claim()

        status, answers, edits = self._press(
            callback_data(True, sid), STRANGER
        )
        self.assertEqual(status, "unauthorized")
        self.assertEqual(answers, [MSG_UNAUTHORIZED])
        self.assertEqual(edits, [])
        self.assertEqual(self._record(sid).approval_status, "pending")

    def test_actor_identity_comes_only_from_the_press(self) -> None:
        """Identical callback data, different actors → different outcomes."""
        sid = self._claim()
        data = callback_data(True, sid)

        status_stranger, _, _ = self._press(data, STRANGER)
        self.assertEqual(status_stranger, "unauthorized")
        self.assertEqual(self._record(sid).approval_status, "pending")

        status_approver, _, _ = self._press(data, APPROVER)
        self.assertEqual(status_approver, "approved")
        self.assertEqual(self._record(sid).approval_status, "approved")


# ══════════════════════════════════════════════════════════════════
# D. Security
# ══════════════════════════════════════════════════════════════════


class TestSecurity(InboxTestBase):

    def test_forged_unknown_claim_id_is_stale(self) -> None:
        """A claim id the server never linked cannot be operated."""
        status, answers, edits = self._press(
            "mproof:approve:999999", APPROVER
        )
        self.assertEqual(status, "stale")
        self.assertEqual(answers, [MSG_STALE])

    def test_existing_claim_without_linkage_is_stale(self) -> None:
        """Server-side linkage is required even for the real approver."""
        # Create a real claim while the inbox is NOT bound → no linkage.
        unbind()
        self._start()
        sid = self._submit(key="nolink").submission_id
        bind(self.notifier, _run_now)

        status, answers, edits = self._press(
            callback_data(True, sid), APPROVER
        )
        self.assertEqual(status, "stale")
        self.assertEqual(answers, [MSG_STALE])
        self.assertEqual(self._record(sid).approval_status, "pending")
        self.assertEqual(self._credits(), [])

    def test_forged_payload_shapes_are_rejected(self) -> None:
        """Callback payloads carry ONLY action + claim id; all else invalid."""
        forged = [
            "mproof:approve:1:extra",
            "mproof:approve:abc",
            "mproof:approve:-5",
            "mproof:approve:0",
            "mproof:approve:",
            "mproof:decide:1",
            "mproof:approve:١٢",  # non-ASCII digits
            "admin_panel:list",
            "lang:ar",
            None,
            123,
        ]
        for data in forged:
            self.assertIsNone(parse_callback(data), repr(data))

        status, answers, edits = self._press(forged[0], APPROVER)
        self.assertEqual(status, "invalid")
        self.assertEqual(answers, [MSG_INVALID])
        self.assertEqual(edits, [])

    def test_non_manual_claim_cannot_be_decided_via_handler(self) -> None:
        """Even with a forged linkage row, a referral claim stays closed."""
        TaskStartGate().start(WORKER, self.referral_task_id)
        referral = ReferralClaimService.submit(
            WORKER, self.referral_task_id, "ref-sec-1"
        )
        self.assertEqual(referral.state, "pending")

        # Worst case: a linkage row pointing at the NON-manual claim.
        AdminNotificationStore.create_linkage(
            OPERATION_MANUAL_PROOF, referral.submission_id, ADMIN_A, 777
        )

        status, answers, edits = self._press(
            callback_data(True, referral.submission_id), APPROVER
        )
        self.assertEqual(status, "stale")
        self.assertEqual(answers, [MSG_STALE])
        # The referral claim is untouched: still pending, no reward.
        row = TaskSubmissionStore.get_submission(referral.submission_id)
        self.assertEqual(row.approval_status, "pending")
        self.assertEqual(self._credits(), [])

    def test_required_channels_receive_zero_messages(self) -> None:
        """Full flow: every delivered/edited chat is a config.ADMINS private chat."""
        db.save_channel(
            Channel(
                slug="main",
                channel_id=REQUIRED_CHANNEL_ID,
                username="taskcoin_main",
                title="قناة الاشتراك",
            ),
            self.db_path,
        )

        self._start()
        sid = self._submit().submission_id
        status, _, _ = self._press(callback_data(True, sid), APPROVER)
        self.assertEqual(status, "approved")

        touched = {s["chat_id"] for s in self.sent}
        touched |= {l.admin_chat_id for l in self._linkages(sid)}
        self.assertTrue(touched)
        self.assertTrue(touched.issubset({ADMIN_A, ADMIN_B}))
        self.assertNotIn(REQUIRED_CHANNEL_ID, touched)


# ══════════════════════════════════════════════════════════════════
# E. Race / idempotency
# ══════════════════════════════════════════════════════════════════


class TestRaceIdempotency(InboxTestBase):

    def _claim(self) -> int:
        self._start()
        return self._submit().submission_id

    def test_approve_twice_completes_and_rewards_once(self) -> None:
        sid = self._claim()

        first, _, _ = self._press(callback_data(True, sid), APPROVER)
        second, _, _ = self._press(callback_data(True, sid), APPROVER)

        self.assertEqual([first, second], ["approved", "approved"])
        self.assertEqual(self._credits(), [REWARD_UNITS])
        self.assertEqual(
            self._user_task_status(WORKER), db.USER_TASK_STATUS_COMPLETED
        )
        self.assertEqual(self._record(sid).approval_status, "approved")

    def test_reject_twice_records_single_rejection(self) -> None:
        sid = self._claim()

        first, _, _ = self._press(callback_data(False, sid), APPROVER)
        second, _, _ = self._press(callback_data(False, sid), APPROVER)

        self.assertEqual([first, second], ["rejected", "rejected"])
        record = self._record(sid)
        self.assertEqual(record.approval_status, "rejected")
        self.assertEqual(record.status, db.SUBMISSION_STATUS_FAILED)
        self.assertEqual(
            self._user_task_status(WORKER), db.USER_TASK_STATUS_STARTED
        )
        self.assertEqual(self._credits(), [])

    def test_conflicting_decision_is_already_decided(self) -> None:
        """Reject after approve: CAS keeps the FIRST terminal decision."""
        sid = self._claim()
        approve_status, _, _ = self._press(callback_data(True, sid), APPROVER)
        self.assertEqual(approve_status, "approved")

        status, answers, edits = self._press(
            callback_data(False, sid), APPROVER
        )
        self.assertEqual(status, "already_decided")
        self.assertEqual(answers, [MSG_ALREADY_DECIDED])
        self.assertEqual(self._record(sid).approval_status, "approved")
        self.assertEqual(self._credits(), [REWARD_UNITS])
        # The stale buttons were made inert with the ACTUAL outcome.
        self.assertTrue(edits)
        self.assertIn("تمت الموافقة", edits[-1][2])

    def test_stale_callback_after_external_decision_mutates_nothing(self) -> None:
        """Decision made through another surface; a later press changes nothing."""
        from manual_task import ManualReviewService

        sid = self._claim()
        ManualReviewService.decide(APPROVER, self.task_id, sid, approve=True)

        credits_before = self._credits()
        status, answers, edits = self._press(
            callback_data(False, sid), APPROVER
        )
        self.assertEqual(status, "already_decided")
        self.assertEqual(answers, [MSG_ALREADY_DECIDED])
        self.assertEqual(self._credits(), credits_before)
        self.assertEqual(self._record(sid).approval_status, "approved")

    def test_notification_replay_creates_no_duplicate(self) -> None:
        """Replaying the schedule hook for a linked claim sends nothing new."""
        self._start()
        task = db.get_task(self.task_id)
        sid = self._submit().submission_id
        record = TaskSubmissionStore.get_submission(sid)
        self.assertEqual(len(self._pending_notifications()), 2)

        # Direct replay of the notification hook (worst-case caller).
        schedule_pending_notification(task, record)
        schedule_pending_notification(task, record)

        self.assertEqual(len(self._pending_notifications()), 2)
        self.assertEqual(len(self._all_linkage_rows()), 2)


# ══════════════════════════════════════════════════════════════════
# F. Telegram UX
# ══════════════════════════════════════════════════════════════════


class TestTelegramUX(InboxTestBase):

    def test_pending_message_has_approve_reject_buttons(self) -> None:
        self._start()
        sid = self._submit().submission_id

        notices = self._pending_notifications()
        self.assertEqual(len(notices), 2)
        markup = notices[0]["reply_markup"]
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual(len(markup.inline_keyboard), 1)
        buttons = markup.inline_keyboard[0]
        self.assertEqual(len(buttons), 2)
        self.assertIn(BUTTON_APPROVE, buttons[0].text)
        self.assertIn(BUTTON_REJECT, buttons[1].text)
        self.assertEqual(
            buttons[0].callback_data, f"mproof:approve:{sid}"
        )
        self.assertEqual(
            buttons[1].callback_data, f"mproof:reject:{sid}"
        )

    def test_decision_edits_are_inert_and_keyboard_free(self) -> None:
        """A decided notification is replaced by plain text — no buttons."""
        self._start()
        sid = self._submit().submission_id
        links = self._linkages(sid)

        status, _, edits = self._press(callback_data(True, sid), APPROVER)
        self.assertEqual(status, "approved")

        # Every admin copy was edited: (chat_id, message_id, text) only —
        # the edit surface cannot carry a keyboard at all.
        self.assertEqual(
            {(c, m) for c, m, _ in edits},
            {(l.admin_chat_id, l.message_id) for l in links},
        )
        for _chat, _msg, text in edits:
            self.assertIn("تمت الموافقة", text)
            self.assertIn(f"#{self.task_id}", text)

    def test_arabic_responses_for_error_states(self) -> None:
        """Unauthorized / stale / already-decided answer in Arabic."""
        self._start()
        sid = self._submit().submission_id

        _, answers, _ = self._press(callback_data(True, sid), ADMIN_A)
        self.assertEqual(answers, [MSG_UNAUTHORIZED])

        _, answers, _ = self._press("mproof:approve:424242", APPROVER)
        self.assertEqual(answers, [MSG_STALE])

        from manual_task import ManualReviewService

        ManualReviewService.decide(APPROVER, self.task_id, sid, approve=True)
        _, answers, _ = self._press(callback_data(False, sid), APPROVER)
        self.assertEqual(answers, [MSG_ALREADY_DECIDED])

        for text in (MSG_UNAUTHORIZED, MSG_STALE, MSG_ALREADY_DECIDED):
            self.assertTrue(any("\u0600" <= ch <= "\u06ff" for ch in text))

    def test_proof_ref_rendered_verbatim_as_plain_text(self) -> None:
        """Markdown-looking proof text is never interpreted as markup."""
        self._start()
        self._submit(proof=PROOF_MARKUPY, key="mk1")

        text = self._pending_notifications()[0]["text"]
        self.assertIn(PROOF_MARKUPY, text)
        # The transport signature has no parse mode at all — the fake
        # captured only (chat_id, text, reply_markup), no parse_mode key.
        for notice in self._pending_notifications():
            self.assertNotIn("parse_mode", notice)


if __name__ == "__main__":
    unittest.main()
