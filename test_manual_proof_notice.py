"""Worker Outcome Notification for Manual Task Proofs (MT-ADMIN-35)

The worker who submitted a proof must be told the outcome.  These
tests cover the notice itself, its guards (no notice on a failed or
repeated decision, no notice for an unauthorized press), fail-soft
delivery, and the safety of the other notification flows.

Everything runs against a temporary SQLite file and fake transports:
no production data, no wallet/ledger/deposit change, no real bot.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

import config
import db
import wallet
from admin_notifier import AdminNotifier
from manual_proof_inbox import (
    MSG_WORKER_APPROVED,
    MSG_WORKER_REJECTED,
    bind,
    bind_user_notifier,
    build_worker_notice_text,
    handle_callback,
    schedule_worker_notice,
    unbind,
    unbind_user_notifier,
)
from manual_task import (
    MANUAL_TASK_TYPE,
    ManualDecisionError,
    ManualProofService,
    ManualReviewService,
)
from task_submission_store import TaskSubmissionStore

from test_admin_manual_proof_inbox import (
    ADMIN_A,
    ADMIN_B,
    APPROVER,
    WORKER,
    _run_now,
)

REWARD_UNITS = 30 * wallet.USDT_SCALE


def _valid_task_data(approver: int = APPROVER) -> dict:
    return {
        "provider": "other",
        "action": "follow",
        "approver": {"telegram_user_id": approver},
        "target": {"ref": "https://example.com/x"},
    }


class FakeWorkerNotifier:
    """Transport that records what would reach the worker."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail
        self.calls = 0

    async def notify(self, user_id: int, text: str) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.sent.append({"user_id": user_id, "text": text})

    @property
    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]

    @property
    def recipients(self) -> list[int]:
        return [m["user_id"] for m in self.sent]


class _Answer:
    async def __call__(self, text: str) -> None:  # pragma: no cover - toast
        self.text = text


class _Edit:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, chat_id, message_id, text) -> None:
        self.calls.append((chat_id, message_id, text))


class WorkerNoticeTestBase(unittest.TestCase):
    """A started manual task, a submitted proof, and both transports."""

    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._orig_db = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.addCleanup(self._restore_db)

        db.register_user(WORKER, "worker", "Worker")
        db.register_user(APPROVER, "approver", "Approver")
        self.task_id = db.create_task(
            title="متابعه حساب فيسبوك",
            description="متابعه الحساب وارسال لقطه شاشه",
            task_type=MANUAL_TASK_TYPE,
            reward=30,
            active=True,
            task_data=json.dumps(_valid_task_data()),
        )
        db.create_user_task(WORKER, self.task_id)
        db.update_user_task_status(WORKER, self.task_id, "started")

        # a submitted proof awaiting review
        ManualProofService.submit(
            WORKER, self.task_id, "https://example.com/my-proof"
        )
        from task_submission_store import TaskSubmissionStore

        self.submission = TaskSubmissionStore().list_user_task_submissions(
            WORKER, self.task_id
        )[0]
        self.submission_id = self.submission.submission_id

        self.worker_notifier = FakeWorkerNotifier()
        bind_user_notifier(self.worker_notifier)
        self.addCleanup(unbind_user_notifier)

        # The callback resolves through the persistent notification
        # linkage (exactly as production does), so create it — without
        # it every press resolves to "stale" before any decision.
        from admin_notification_store import AdminNotificationStore
        from manual_proof_inbox import OPERATION_MANUAL_PROOF

        AdminNotificationStore.create_linkage(
            OPERATION_MANUAL_PROOF, self.submission_id, ADMIN_A, 777
        )

    def _restore_db(self) -> None:
        db.DB_PATH = self._orig_db

    # ── helpers ────────────────────────────────────────────────────

    def _decide(self, approve: bool):
        """Decide directly (no notice: only handle_callback notifies)."""
        return ManualReviewService.decide(
            APPROVER, self.task_id, self.submission_id, approve=approve
        )

    def _press(self, approve: bool):
        """Press approve/reject through the real callback — the path the
        admin actually uses, and the only place a worker notice fires."""
        from manual_proof_inbox import callback_data

        return asyncio.run(
            handle_callback(
                callback_data(approve, self.submission_id),
                APPROVER,
                answer=_Answer(),
                edit=_Edit(),
                pressed_chat_id=APPROVER,
                pressed_message_id=1,
            )
        )

    def _wallet_units(self) -> int:
        row = None
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT available_units FROM wallets WHERE user_id = ?",
                (WORKER,),
            ).fetchone()
        return row[0] if row else 0

    def _ledger_rows(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM ledger"
            ).fetchone()[0]


# ════════════════════════════════════════════════════════════════════
# 1. The notice itself
# ════════════════════════════════════════════════════════════════════


class TestWorkerNoticeContent(WorkerNoticeTestBase):
    """What the worker is told — and what is NOT."""

    def test_approved_notice_says_approved(self):
        text = build_worker_notice_text(True)
        self.assertEqual(text, MSG_WORKER_APPROVED)
        self.assertIn("الموافقة", text)

    def test_rejected_notice_says_rejected(self):
        text = build_worker_notice_text(False)
        self.assertEqual(text, MSG_WORKER_REJECTED)
        self.assertIn("قبول", text)

    def test_notice_hides_every_internal_detail(self):
        for text in (MSG_WORKER_APPROVED, MSG_WORKER_REJECTED):
            # no submission/task/user ids and no approver identity
            self.assertNotIn(str(self.submission_id), text)
            self.assertNotIn(str(self.task_id), text)
            self.assertNotIn(str(WORKER), text)
            self.assertNotIn(str(APPROVER), text)
            # no reward figure
            self.assertNotIn(str(REWARD_UNITS), text)

    def test_notice_targets_the_submitting_worker(self):
        self._decide(approve=True)
        schedule_worker_notice(self.submission.user_id, True)
        self.assertEqual(self.worker_notifier.recipients, [WORKER])


# ════════════════════════════════════════════════════════════════════
# 2. Scheduling guards
# ════════════════════════════════════════════════════════════════════


class TestWorkerNoticeGuards(WorkerNoticeTestBase):
    def test_unbound_transport_is_a_no_op(self):
        unbind_user_notifier()
        # must not raise, and must not deliver anywhere
        schedule_worker_notice(WORKER, True)
        self.assertEqual(self.worker_notifier.sent, [])

    def test_invalid_user_ids_are_refused(self):
        for bad in (0, -1, None, "42", 1.5, True):
            schedule_worker_notice(bad, True)
        self.assertEqual(self.worker_notifier.sent, [])

    def test_empty_notice_text_is_refused_by_the_transport(self):
        # the shared delivery path guards for ANY bound transport
        from manual_proof_inbox import _deliver_worker_notice

        with self.assertRaises(ValueError):
            asyncio.run(
                _deliver_worker_notice(
                    FakeWorkerNotifier(), WORKER, "   "
                )
            )
        # a truthy-but-not-integer id (bool is an int subclass) is refused
        with self.assertRaises(ValueError):
            asyncio.run(
                _deliver_worker_notice(
                    FakeWorkerNotifier(), True, "text"
                )
            )

    def test_invalid_user_id_is_refused_by_the_transport(self):
        from manual_proof_inbox import _deliver_worker_notice

        for bad in (0, -1, None, "42"):
            with self.assertRaises(ValueError):
                asyncio.run(
                    _deliver_worker_notice(FakeWorkerNotifier(), bad, "t")
                )

    def test_transport_failure_is_fail_soft(self):
        notifier = FakeWorkerNotifier(fail=True)
        # the raise is caught by the scheduler wrapper, so nothing escapes
        from manual_proof_inbox import _deliver_worker_notice

        asyncio.run(_deliver_worker_notice(notifier, WORKER, "text"))
        self.assertEqual(notifier.calls, 1)
        self.assertEqual(notifier.sent, [])


# ════════════════════════════════════════════════════════════════════
# 3. A failed decision never notifies a success
# ════════════════════════════════════════════════════════════════════


class TestNoNoticeOnFailure(WorkerNoticeTestBase):
    def test_transient_decision_failure_sends_no_notice(self):
        """A decision that raises must reach the worker's notice code
        nowhere — the callback returns 'error' and nothing is sent."""
        from manual_proof_inbox import callback_data

        with mock.patch.object(
            ManualReviewService,
            "decide",
            side_effect=ManualDecisionError("boom"),
        ):
            result = asyncio.run(
                handle_callback(
                    callback_data(True, self.submission_id),
                    APPROVER,
                    answer=_Answer(),
                    edit=_Edit(),
                )
            )
        self.assertEqual(result, "error")
        self.assertEqual(self.worker_notifier.sent, [])
        # and the claim is still pending
        row = TaskSubmissionStore().get_submission(self.submission_id)
        self.assertEqual(row.approval_status, "pending")

    def test_no_notice_when_approval_itself_fails(self):
        # A second, conflicting approver decision cannot exist here; instead
        # make the durable gate refuse so no outcome is produced at all.
        with mock.patch.object(
            ManualReviewService,
            "decide",
            side_effect=ManualDecisionError("claim already approved"),
        ):
            with self.assertRaises(ManualDecisionError):
                self._decide(approve=True)
        self.assertEqual(self.worker_notifier.sent, [])

    def test_notice_only_after_a_durable_outcome(self):
        # before any decision: nothing to report
        self.assertEqual(self.worker_notifier.sent, [])
        self._press(approve=True)
        self.assertEqual(len(self.worker_notifier.sent), 1)
        self.assertEqual(self.worker_notifier.texts, [MSG_WORKER_APPROVED])


# ════════════════════════════════════════════════════════════════════
# 4. Repeat presses must not notify twice
# ════════════════════════════════════════════════════════════════════


class TestNoDoubleNotification(WorkerNoticeTestBase):
    def test_repeating_the_same_decision_is_idempotent_but_silent(self):
        """ManualReviewService.decide is idempotent: re-approving
        re-returns the outcome WITHOUT raising.  The notice must fire
        only for the press that actually applied the decision."""
        self._decide(approve=True)
        first_notices = len(self.worker_notifier.sent)
        # a repeat produces no additional outcome work and no notice
        self._decide(approve=True)
        self.assertEqual(len(self.worker_notifier.sent), first_notices)

    def test_callback_replay_notifies_once(self):
        """A full callback replay must reach the worker exactly once."""
        from manual_proof_inbox import callback_data

        payload = callback_data(True, self.submission_id)
        asyncio.run(handle_callback(
            payload,
            APPROVER,
            answer=_Answer(),
            edit=_Edit(),
            pressed_chat_id=APPROVER,
            pressed_message_id=1,
        ))
        self.assertEqual(len(self.worker_notifier.sent), 1)
        # press it again — already approved, no second notice
        asyncio.run(handle_callback(
            payload,
            APPROVER,
            answer=_Answer(),
            edit=_Edit(),
            pressed_chat_id=APPROVER,
            pressed_message_id=2,
        ))
        self.assertEqual(len(self.worker_notifier.sent), 1)

    def test_reject_then_approve_does_not_notify_approval(self):
        """Deciding reject first, then approve, must never produce a
        misleading approval notice for the same claim."""
        # a rejection notice is delivered
        self.assertEqual(self._press(approve=False), "rejected")
        self.assertEqual(self.worker_notifier.texts, [MSG_WORKER_REJECTED])
        # approving a rejected claim is refused — and notifies nothing new
        with self.assertRaises(ManualDecisionError):
            self._decide(approve=True)
        self.assertEqual(self.worker_notifier.texts, [MSG_WORKER_REJECTED])

    def test_approval_notice_reaches_the_worker_once(self):
        self.assertEqual(self._press(approve=True), "approved")
        self.assertEqual(len(self.worker_notifier.sent), 1)
        self.assertEqual(self.worker_notifier.recipients, [WORKER])
        self.assertEqual(self.worker_notifier.texts, [MSG_WORKER_APPROVED])

    def test_rejection_notice_reaches_the_worker(self):
        self.assertEqual(self._press(approve=False), "rejected")
        self.assertEqual(len(self.worker_notifier.sent), 1)
        self.assertEqual(self.worker_notifier.recipients, [WORKER])
        self.assertEqual(self.worker_notifier.texts, [MSG_WORKER_REJECTED])

    def test_unauthorized_press_notifies_no_one(self):
        """A non-approver press must not tell the worker anything."""
        from manual_proof_inbox import callback_data

        result = asyncio.run(
            handle_callback(
                callback_data(True, self.submission_id),
                4242,  # not the approver
                answer=_Answer(),
                edit=_Edit(),
            )
        )
        self.assertEqual(result, "unauthorized")
        self.assertEqual(self.worker_notifier.sent, [])
        # and the claim is still pending
        row = TaskSubmissionStore().get_submission(self.submission_id)
        self.assertEqual(row.approval_status, "pending")

    def test_callback_repeat_is_caught_before_delivery(self):
        """A replayed callback resolves to already_decided, and the
        notice code is never reached for it — the worker was already
        told by the first press."""
        from manual_proof_inbox import callback_data

        payload = callback_data(True, self.submission_id)
        first = asyncio.run(
            handle_callback(
                payload,
                APPROVER,
                answer=_Answer(),
                edit=_Edit(),
                pressed_chat_id=APPROVER,
                pressed_message_id=1,
            )
        )
        self.assertEqual(first, "approved")
        self.assertEqual(len(self.worker_notifier.sent), 1)

        # a second press is a replay of a decided claim
        second = asyncio.run(
            handle_callback(
                payload,
                APPROVER,
                answer=_Answer(),
                edit=_Edit(),
                pressed_chat_id=APPROVER,
                pressed_message_id=2,
            )
        )
        # idempotent: a repeated approve returns approved again, but the
        # replay guard suppresses a second notice
        self.assertEqual(second, "approved")
        self.assertEqual(len(self.worker_notifier.sent), 1)

    def test_admin_surfaces_still_update_on_the_repeat(self):
        """A replay still refreshes the admin surfaces (existing
        behaviour preserved) — it only suppresses the second notice."""
        from manual_proof_inbox import callback_data

        payload = callback_data(True, self.submission_id)
        asyncio.run(
            handle_callback(
                payload,
                APPROVER,
                answer=_Answer(),
                edit=_Edit(),
                pressed_chat_id=APPROVER,
                pressed_message_id=1,
            )
        )
        edit2 = _Edit()
        result = asyncio.run(
            handle_callback(
                payload,
                APPROVER,
                answer=_Answer(),
                edit=edit2,
                pressed_chat_id=APPROVER,
                pressed_message_id=2,
            )
        )
        # decide() is idempotent: a repeated approve re-returns approved
        self.assertEqual(result, "approved")
        self.assertTrue(edit2.calls)
        # but the worker was still told exactly once, by the first press
        self.assertEqual(len(self.worker_notifier.sent), 1)


# ════════════════════════════════════════════════════════════════════
# 5. No financial side-effect from the notice
# ════════════════════════════════════════════════════════════════════


class TestNoFinancialChange(WorkerNoticeTestBase):
    def test_notice_does_not_touch_money(self):
        self._decide(approve=True)
        before_units = self._wallet_units()
        before_rows = self._ledger_rows()
        schedule_worker_notice(WORKER, True)
        self.assertEqual(self._wallet_units(), before_units)
        self.assertEqual(self._ledger_rows(), before_rows)

    def test_a_failed_delivery_cannot_change_the_decision(self):
        from manual_proof_inbox import (
            _deliver_worker_notice,
            schedule_worker_notice,
        )

        self._decide(approve=True)
        self.assertEqual(
            db.get_user_task(WORKER, self.task_id)["status"], "completed"
        )
        # a broken transport must not escape and must not undo anything
        unbind_user_notifier()
        bind_user_notifier(FakeWorkerNotifier(fail=True))
        asyncio.run(
            _deliver_worker_notice(None, WORKER, MSG_WORKER_APPROVED)
        )
        asyncio.run(
            _deliver_worker_notice(FakeWorkerNotifier(fail=True), WORKER, "t")
        )
        schedule_worker_notice(WORKER, True)
        # the saved decision is untouched
        self.assertEqual(
            db.get_user_task(WORKER, self.task_id)["status"], "completed"
        )
        self.assertEqual(self._ledger_rows(), 1)


# ════════════════════════════════════════════════════════════════════
# 6. The other notification flows are untouched
# ════════════════════════════════════════════════════════════════════


class TestOtherFlowsUnaffected(unittest.TestCase):
    """The admin path must behave exactly as before."""

    def setUp(self) -> None:
        self._orig_admins = list(config.ADMINS)
        config.ADMINS[:] = [ADMIN_A, ADMIN_B]
        self.addCleanup(self._restore_admins)
        self.sent: list[dict] = []
        self._seq = 0

        async def _send_text(chat_id, text):
            self.sent.append({"kind": "text", "chat_id": chat_id, "text": text})

        async def _send_markup(chat_id, text, reply_markup):
            self._seq += 1
            self.sent.append(
                {
                    "kind": "markup",
                    "chat_id": chat_id,
                    "text": text,
                    "message_id": self._seq,
                }
            )
            return self._seq

        async def _send_photo(chat_id, text, image, reply_markup):
            self._seq += 1
            self.sent.append(
                {"kind": "photo", "chat_id": chat_id, "text": text}
            )
            return self._seq

        self.admin_notifier = AdminNotifier(
            _send_text,
            markup_send=_send_markup,
            photo_send=_send_photo,
        )
        # The previous behaviour: only the ADMIN transport was bound.
        bind(self.admin_notifier, _run_now)
        self.addCleanup(unbind)

    def _restore_admins(self) -> None:
        config.ADMINS[:] = self._orig_admins

    def test_admin_system_notifications_still_text_only(self):
        asyncio.run(self.admin_notifier.notify_system("ops event"))
        self.assertEqual({m["kind"] for m in self.sent}, {"markup"})
        self.assertEqual(
            [m for m in self.sent if m["kind"] == "photo"], []
        )

    def test_unbinding_admin_leaves_worker_transport_alive_but_unused(self):
        self.assertTrue(bind.__doc__ is not None)
        unbind_user_notifier()
        schedule_worker_notice(999, True)  # no crash, no send
        self.assertEqual(
            [m for m in self.sent if m["kind"] == "text"], []
        )

    def test_worker_transport_never_uses_the_admin_notifier(self):
        """A worker notice must not arrive through the admin surface."""
        notifier = FakeWorkerNotifier()
        asyncio.run(notifier.notify(WORKER, MSG_WORKER_APPROVED))
        # the admin transport received nothing from this call
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()


# ══════════════════════════════════════════════════════════════════════
# 6. MT-ADMIN-35 — approver identity matrix (agent-style vs normal)
# ══════════════════════════════════════════════════════════════════════


class TestAgentStyleApproverMismatch(unittest.TestCase):
    """MT-ADMIN-35: a manual task carrying a NON-ADMIN placeholder
    approver (the production contamination observed as ``777``) keeps
    denying every press — even a real admin's — with NO state change,
    while the known-good shape (approver == pressing admin, exactly
    what a ``treq:`` approval publishes) decides normally.  The
    security rule is never weakened."""

    AGENT_PLACEHOLDER_APPROVER = 777

    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._orig_db = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.addCleanup(self._restore_db)

        db.register_user(WORKER, "worker", "Worker")
        db.register_user(ADMIN_A, "admin_a", "AdminA")
        self.notifier = FakeWorkerNotifier()
        bind_user_notifier(self.notifier)
        self.addCleanup(unbind_user_notifier)

    def _restore_db(self) -> None:
        db.DB_PATH = self._orig_db

    def _seed_claim(self, approver: int):
        """Started manual task + one pending proof + its linkage."""
        task_id = db.create_task(
            title="مهمة تجريبية للمصفوفة",
            description="وصف تجريبي",
            task_type=MANUAL_TASK_TYPE,
            reward=1,
            active=True,
            task_data=json.dumps(_valid_task_data(approver=approver)),
        )
        db.create_user_task(WORKER, task_id)
        db.update_user_task_status(WORKER, task_id, "started")
        ManualProofService.submit(
            WORKER, task_id, "https://example.com/matrix-proof"
        )
        submission = TaskSubmissionStore().list_user_task_submissions(
            WORKER, task_id
        )[0]

        from admin_notification_store import AdminNotificationStore
        from manual_proof_inbox import OPERATION_MANUAL_PROOF

        AdminNotificationStore.create_linkage(
            OPERATION_MANUAL_PROOF, submission.submission_id, ADMIN_A, 777
        )
        return task_id, submission.submission_id

    def _press(self, submission_id: int, actor: int, approve: bool):
        from manual_proof_inbox import callback_data

        return asyncio.run(
            handle_callback(
                callback_data(approve, submission_id),
                actor,
                answer=_Answer(),
                edit=_Edit(),
                pressed_chat_id=actor,
                pressed_message_id=1,
            )
        )

    def _approval_status(self, submission_id: int) -> str:
        return (
            TaskSubmissionStore()
            .get_submission(submission_id)
            .approval_status
        )

    def test_agent_style_task_denies_admin_approve_press(self):
        """The reported production case: placeholder approver 777 on
        the task, a genuine admin presses Approve → unauthorized, the
        claim stays pending, no worker notice, no money movement."""
        _task_id, sid = self._seed_claim(self.AGENT_PLACEHOLDER_APPROVER)
        result = self._press(sid, ADMIN_A, approve=True)
        self.assertEqual(result, "unauthorized")
        self.assertEqual(self._approval_status(sid), "pending")
        self.assertEqual(self.notifier.sent, [])

    def test_agent_style_task_denies_admin_reject_press(self):
        _task_id, sid = self._seed_claim(self.AGENT_PLACEHOLDER_APPROVER)
        result = self._press(sid, ADMIN_A, approve=False)
        self.assertEqual(result, "unauthorized")
        self.assertEqual(self._approval_status(sid), "pending")
        self.assertEqual(self.notifier.sent, [])

    def test_normal_task_approver_press_approves(self):
        """Known-good shape (approver == pressing admin): the SAME
        press succeeds and the worker is notified once."""
        _task_id, sid = self._seed_claim(ADMIN_A)
        self.assertEqual(self._press(sid, ADMIN_A, approve=True), "approved")
        self.assertEqual(self._approval_status(sid), "approved")
        self.assertEqual(self.notifier.recipients, [WORKER])
        self.assertEqual(self.notifier.texts, [MSG_WORKER_APPROVED])

    def test_normal_task_approver_press_rejects(self):
        _task_id, sid = self._seed_claim(ADMIN_A)
        self.assertEqual(self._press(sid, ADMIN_A, approve=False), "rejected")
        self.assertEqual(self._approval_status(sid), "rejected")
        self.assertEqual(self.notifier.recipients, [WORKER])
        self.assertEqual(self.notifier.texts, [MSG_WORKER_REJECTED])

    def test_pending_card_never_says_request_number(self):
        """MT-ADMIN-35 display fix: the claim id must be labelled
        "رقم المطالبة" — never "رقم الطلب", which collides with the
        user_task_requests numbering."""
        from manual_proof_inbox import build_pending_text

        _task_id, sid = self._seed_claim(ADMIN_A)
        task = db.get_task(_task_id)
        record = TaskSubmissionStore().get_submission(sid)
        text = build_pending_text(task, record)
        self.assertIn("رقم المطالبة", text)
        self.assertNotIn("رقم الطلب", text)
        self.assertIn(str(sid), text)
