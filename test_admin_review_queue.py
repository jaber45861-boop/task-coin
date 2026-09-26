"""
Focused tests — Admin Manual Review Queue (MT-ADMIN-04)
=======================================================

Cross-task pending manual-review queue on the Telegram Admin Control
Plane — discovery/navigation ONLY:

    /reviews → persistent pending manual claims → 🔍 مراجعة #<id>
             → server-side re-read + admin/approver gates
             → existing MT-ADMIN-03 inbox card (Approve/Reject)
             → ManualReviewService.decide (unchanged, sole decision path)

Coverage required by MT-ADMIN-04:

A. Queue discovery (read-only)
   - empty / single / cross-task results, deterministic oldest-first
   - approved, rejected, referral, non-manual, inactive-task and
     invalid-definition claims all excluded
B. /reviews authorization
   - admin private chat allowed; non-admin denied without queue data
   - group/channel invocation completely silent (zero replies)
   - bounded page size with deterministic pagination
C. Safe queue data
   - exactly the safe field set (no reward/task_data/approver)
   - rendered text hides internal fields
   - forged callback payloads rejected as untrusted lookup data
D. Review callback
   - valid pending claim opens the EXISTING review representation
   - claim id is a lookup pointer only (no task/proof substitution)
   - forged/missing/dead claims fail safely with no mutation
   - non-approver admin / non-admin cannot open; non-approver cannot
     decide from the card; Review NEVER mutates claim/reward state
E. Stale / concurrent state
   - already decided → Arabic already-decided + inert card
   - decision between list and click handled safely
   - open→approve grants exactly one reward; reject stays single
   - listing/paging/opening never mutates claim or ledger state
F. Regression — existing MT-ADMIN-03 + task/reward suites stay green
   (verified by the full-suite run)

Run:
    python3 -m pytest test_admin_review_queue.py -v
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import unittest
from unittest import mock

from telegram import InlineKeyboardMarkup

import admin_review_queue
import db
from admin_notification_store import AdminNotificationStore
from admin_review_queue import (
    MSG_ADMIN_ONLY,
    MSG_NO_REVIEWS,
    MSG_QUEUE_HEADER,
    MSG_REVIEW_UNAUTHORIZED,
    PAGE_SIZE,
    PendingManualClaim,
    build_queue_page,
    handle_page_callback,
    handle_review_callback,
    list_pending_manual_claims,
    parse_page_callback,
    parse_review_callback,
    review_callback_data,
)
from manual_proof_inbox import (
    MSG_ALREADY_DECIDED,
    MSG_EDIT_CLOSED,
    MSG_STALE,
    callback_data as mproof_callback_data,
    handle_callback as inbox_handle_callback,
    bind,
    unbind,
)
from manual_task import (
    MANUAL_TASK_TYPE,
    ManualProofService,
    ManualReviewService,
)
from referral_task import ReferralClaimService
from task_start import TaskStartGate
from task_submission_store import TaskSubmissionStore

# Shared fixture + constants from the MT-ADMIN-03 suite.
from test_admin_manual_proof_inbox import (
    ADMIN_A,
    ADMIN_B,
    APPROVER,
    InboxTestBase,
    PROOF,
    REWARD_UNITS,
    STRANGER,
    WORKER,
    _run_now,
    _valid_manual_task_data,
)

# Extra worker identities for pagination volume.
PAGE_WORKERS = tuple(3000 + i for i in range(8))


class QueueTestBase(InboxTestBase):
    """MT-ADMIN-03 fixture + a manual task whose approver IS a config
    admin (ADMIN_A): queue happy paths need an actor who may both see
    the queue (admin) and open/decide the review (task approver)."""

    def setUp(self) -> None:
        super().setUp()
        self.queue_task_id = self._make_manual_task(
            approver=ADMIN_A, title="مهمة مراجعة تجريبية"
        )
        # TaskStartGate requires an existing user row: register the
        # extra worker identities used by the pagination-volume tests.
        for i, worker in enumerate(PAGE_WORKERS):
            db.register_user(worker, f"pw{i}", f"Worker {i}")

    # ── helpers ──────────────────────────────────────────────────────

    def _make_manual_task(self, approver=ADMIN_A, title="مهمة يدوية"):
        return db.create_task(
            title=title,
            description="أرسل إثباتاً",
            task_type=MANUAL_TASK_TYPE,
            reward=db.get_task(self.task_id)["reward"],
            task_data=json.dumps(_valid_manual_task_data(approver=approver)),
        )

    def _open_claim(
        self, task_id=None, worker=WORKER, proof=PROOF, key="q1"
    ) -> int:
        task_id = self.queue_task_id if task_id is None else task_id
        TaskStartGate().start(worker, task_id)
        outcome = ManualProofService.submit(worker, task_id, proof, key)
        self.assertEqual(outcome.state, "pending")
        return outcome.submission_id

    def _open_referral_claim(self) -> int:
        TaskStartGate().start(WORKER, self.referral_task_id)
        outcome = ReferralClaimService.submit(
            WORKER, self.referral_task_id, "ref-q-1"
        )
        self.assertEqual(outcome.state, "pending")
        return outcome.submission_id

    def _press_review(self, data, actor, chat_id=ADMIN_A, message_id=888):
        """Run one mrview press through the core queue handler."""
        answers: list[str] = []
        edits: list[tuple] = []

        async def answer(text: str) -> None:
            answers.append(text)

        async def edit(cid, mid, text, reply_markup=None) -> None:
            edits.append((cid, mid, text, reply_markup))

        status = asyncio.run(
            handle_review_callback(
                data,
                actor,
                chat_id=chat_id,
                message_id=message_id,
                answer=answer,
                edit=edit,
            )
        )
        return status, answers, edits

    def _press_page(self, data, actor, chat_id=ADMIN_A, message_id=888):
        answers: list[str] = []
        edits: list[tuple] = []

        async def answer(text: str) -> None:
            answers.append(text)

        async def edit(cid, mid, text, reply_markup=None) -> None:
            edits.append((cid, mid, text, reply_markup))

        status = asyncio.run(
            handle_page_callback(
                data,
                actor,
                chat_id=chat_id,
                message_id=message_id,
                answer=answer,
                edit=edit,
            )
        )
        return status, answers, edits

    def _run_reviews_command(self, actor_id, chat_type="private"):
        update = mock.MagicMock()
        update.effective_chat.type = chat_type
        update.effective_user.id = actor_id
        update.message.reply_text = mock.AsyncMock()
        asyncio.run(
            admin_review_queue.reviews_command(update, mock.MagicMock())
        )
        return update.message.reply_text

    def _press_via_adapter(self, data, actor_id, chat_type="private",
                           chat_id=ADMIN_A, message_id=424):
        query = mock.MagicMock()
        query.data = data
        query.from_user.id = actor_id
        query.answer = mock.AsyncMock()
        query.message.chat.id = chat_id
        query.message.message_id = message_id

        update = mock.MagicMock()
        update.callback_query = query
        update.effective_chat.type = chat_type

        context = mock.MagicMock()
        context.bot.edit_message_text = mock.AsyncMock()
        asyncio.run(
            admin_review_queue.review_queue_callback(update, context)
        )
        return query, context

    def _press_inbox(self, data, actor):
        """Press an Approve/Reject (mproof) button via MT-ADMIN-03."""
        answers: list[str] = []
        edits: list[tuple] = []

        async def answer(text: str) -> None:
            answers.append(text)

        async def edit(cid, mid, text) -> None:
            edits.append((cid, mid, text))

        status = asyncio.run(
            inbox_handle_callback(
                data, actor, answer=answer, edit=edit
            )
        )
        return status, answers, edits

    def _claim_snapshot(self, sid: int) -> tuple:
        record = TaskSubmissionStore.get_submission(sid)
        return (
            record.status,
            record.approval_status,
            record.submitted_at,
            record.approval_decided_at,
            record.completed_at,
        )


# ══════════════════════════════════════════════════════════════════
# A. Queue discovery (read-only)
# ══════════════════════════════════════════════════════════════════


class TestQueueDiscovery(QueueTestBase):

    def test_empty_queue_returns_no_claims(self) -> None:
        self.assertEqual(list_pending_manual_claims(), [])

    def test_single_pending_manual_claim_is_returned(self) -> None:
        sid = self._open_claim()
        claims = list_pending_manual_claims()
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim.claim_id, sid)
        self.assertEqual(claim.task_id, self.queue_task_id)
        self.assertEqual(claim.task_title, "مهمة مراجعة تجريبية")
        self.assertTrue(claim.submitted_at)
        self.assertEqual(claim.proof_ref, PROOF)

    def test_cross_task_results(self) -> None:
        sid_a = self._open_claim(task_id=self.queue_task_id, key="xa")
        sid_b = self._open_claim(task_id=self.task_id, key="xb")
        claims = list_pending_manual_claims()
        self.assertEqual(
            [c.claim_id for c in claims], [sid_a, sid_b]
        )
        self.assertEqual(
            {c.task_id for c in claims},
            {self.queue_task_id, self.task_id},
        )

    def test_results_deterministic_and_oldest_first(self) -> None:
        first = self._open_claim(key="d1")
        second = self._open_claim(worker=PAGE_WORKERS[0], key="d2")
        third = self._open_claim(worker=PAGE_WORKERS[1], key="d3")

        claims = list_pending_manual_claims()
        self.assertEqual(
            [c.claim_id for c in claims], [first, second, third]
        )
        # Deterministic: repeated reads are identical and sorted by id.
        again = list_pending_manual_claims()
        self.assertEqual(claims, again)
        self.assertEqual(
            [c.claim_id for c in claims],
            sorted(c.claim_id for c in claims),
        )

    def test_approved_claim_excluded(self) -> None:
        sid = self._open_claim()
        ManualReviewService.decide(
            ADMIN_A, self.queue_task_id, sid, approve=True
        )
        self.assertEqual(list_pending_manual_claims(), [])

    def test_rejected_claim_excluded(self) -> None:
        sid = self._open_claim()
        ManualReviewService.decide(
            ADMIN_A, self.queue_task_id, sid, approve=False
        )
        self.assertEqual(list_pending_manual_claims(), [])

    def test_referral_claim_excluded(self) -> None:
        referral_sid = self._open_referral_claim()
        manual_sid = self._open_claim(key="m1")
        claims = list_pending_manual_claims()
        self.assertEqual([c.claim_id for c in claims], [manual_sid])
        self.assertNotIn(
            referral_sid, [c.claim_id for c in claims]
        )

    def test_non_manual_submission_excluded(self) -> None:
        # A plain, non-approval-gated submission (approval_status NULL).
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO task_submissions "
                "(user_id, task_id, attempt_number, status, idempotency_key) "
                "VALUES (?, ?, 1, 'passed', 'plain-key-1')",
                (STRANGER, self.referral_task_id),
            )
        manual_sid = self._open_claim(key="m2")
        claims = list_pending_manual_claims()
        self.assertEqual([c.claim_id for c in claims], [manual_sid])

    def test_inactive_task_claim_excluded(self) -> None:
        sid = self._open_claim()
        db.update_task(self.queue_task_id, active=False)
        self.assertEqual(list_pending_manual_claims(), [])

    def test_invalid_task_definition_claim_excluded(self) -> None:
        sid = self._open_claim()
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET task_data = '{}' WHERE id = ?",
                (self.queue_task_id,),
            )
        # Dead definition → no trusted approver → not in the queue.
        self.assertEqual(list_pending_manual_claims(), [])


# ══════════════════════════════════════════════════════════════════
# B. /reviews authorization + bounded UI
# ══════════════════════════════════════════════════════════════════


class TestReviewsCommand(QueueTestBase):

    def test_admin_private_chat_gets_queue_with_review_button(self) -> None:
        sid = self._open_claim()
        reply = self._run_reviews_command(ADMIN_A)

        reply.assert_awaited_once()
        text = reply.await_args.args[0]
        markup = reply.await_args.kwargs.get("reply_markup")
        self.assertIn(MSG_QUEUE_HEADER, text)
        self.assertIn("مهمة مراجعة تجريبية", text)
        self.assertIn(f"الطلب: #{sid}", text)
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        buttons = [
            b for row in markup.inline_keyboard for b in row
        ]
        self.assertIn(
            review_callback_data(sid),
            [b.callback_data for b in buttons],
        )

    def test_empty_queue_shows_concise_arabic_message(self) -> None:
        reply = self._run_reviews_command(ADMIN_A)
        reply.assert_awaited_once()
        self.assertEqual(reply.await_args.args[0], MSG_NO_REVIEWS)
        self.assertIsNone(
            reply.await_args.kwargs.get("reply_markup")
        )

    def test_non_admin_denied_without_queue_data(self) -> None:
        sid = self._open_claim()
        reply = self._run_reviews_command(STRANGER)
        reply.assert_awaited_once()
        text = reply.await_args.args[0]
        self.assertEqual(text, MSG_ADMIN_ONLY)
        self.assertNotIn(str(sid), text)
        self.assertNotIn(MSG_QUEUE_HEADER, text)

    def test_group_and_channel_invocations_are_silent(self) -> None:
        self._open_claim()
        for chat_type in ("supergroup", "group", "channel"):
            with self.subTest(chat_type=chat_type):
                reply = self._run_reviews_command(
                    ADMIN_A, chat_type=chat_type
                )
                reply.assert_not_awaited()

    def test_page_size_is_bounded_and_deterministic(self) -> None:
        sids = []
        for i, worker in enumerate(PAGE_WORKERS[:7]):
            sids.append(
                self._open_claim(worker=worker, key=f"page-{i}")
            )
        self.assertEqual(len(list_pending_manual_claims()), 7)

        reply = self._run_reviews_command(ADMIN_A)
        text = reply.await_args.args[0]
        markup = reply.await_args.kwargs.get("reply_markup")

        # Bounded: only PAGE_SIZE entries, oldest first.
        self.assertEqual(text.count("الطلب: #"), PAGE_SIZE)
        self.assertIn("(1–5 من 7)", text)
        self.assertIn(f"#{sids[0]}", text)
        self.assertNotIn(f"#{sids[5]}", text)
        # PAGE_SIZE review buttons + one navigation row.
        self.assertEqual(len(markup.inline_keyboard), PAGE_SIZE + 1)
        nav = markup.inline_keyboard[-1]
        self.assertEqual(len(nav), 1)
        self.assertEqual(nav[0].callback_data, "mrvp:2")

    def test_next_page_renders_remaining_claims(self) -> None:
        sids = [
            self._open_claim(worker=PAGE_WORKERS[i], key=f"pg-{i}")
            for i in range(7)
        ]
        status, answers, edits = self._press_page("mrvp:2", ADMIN_A)
        self.assertEqual(status, "page")
        text, markup = edits[0][2], edits[0][3]
        self.assertIn(f"#{sids[5]}", text)
        self.assertIn(f"#{sids[6]}", text)
        self.assertNotIn(f"#{sids[0]}", text)
        nav = [
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data.startswith("mrvp:")
        ]
        self.assertEqual(nav, ["mrvp:1"])  # prev only, no next

    def test_page_navigation_requires_admin(self) -> None:
        self._open_claim()
        status, answers, edits = self._press_page("mrvp:1", STRANGER)
        self.assertEqual(status, "forbidden")
        self.assertEqual(answers, [MSG_ADMIN_ONLY])
        self.assertEqual(edits, [])

    def test_forged_page_ids_are_clamped_or_rejected(self) -> None:
        sids = [
            self._open_claim(worker=PAGE_WORKERS[i], key=f"cf-{i}")
            for i in range(7)
        ]
        # Out-of-range page clamps deterministically to the last page.
        status, _, edits = self._press_page("mrvp:999", ADMIN_A)
        self.assertEqual(status, "page")
        self.assertIn(f"#{sids[6]}", edits[0][2])
        # Malformed page payloads are rejected outright.
        status, answers, edits = self._press_page("mrvp:abc", ADMIN_A)
        self.assertEqual(status, "invalid")
        self.assertEqual(edits, [])

    def test_page_nav_refreshes_snapshot_after_decision(self) -> None:
        sid = self._open_claim()
        other = self._open_claim(
            worker=PAGE_WORKERS[0], key="snap-2"
        )
        ManualReviewService.decide(
            ADMIN_A, self.queue_task_id, sid, approve=True
        )
        status, _, edits = self._press_page("mrvp:1", ADMIN_A)
        self.assertEqual(status, "page")
        self.assertNotIn(f"#{sid}", edits[0][2])
        self.assertIn(f"#{other}", edits[0][2])


# ══════════════════════════════════════════════════════════════════
# C. Safe queue data
# ══════════════════════════════════════════════════════════════════


class TestSafeQueueData(QueueTestBase):

    def test_queue_data_fields_are_exactly_the_safe_set(self) -> None:
        field_names = {
            f.name for f in dataclasses.fields(PendingManualClaim)
        }
        self.assertEqual(
            field_names,
            {"claim_id", "task_id", "task_title", "submitted_at",
             "proof_ref"},
        )
        self._open_claim()
        claim = list_pending_manual_claims()[0]
        for forbidden in (
            "reward", "task_data", "approver", "user_id", "token",
        ):
            self.assertFalse(hasattr(claim, forbidden), forbidden)

    def test_rendered_queue_text_hides_internal_fields(self) -> None:
        self._open_claim()
        text, _markup = build_queue_page(list_pending_manual_claims(), 1)
        self.assertNotIn("task_data", text)
        self.assertNotIn(str(APPROVER), text)
        self.assertNotIn("reward", text.lower())
        self.assertNotIn("approver", text.lower())

    def test_forged_callback_payloads_rejected(self) -> None:
        forged = [
            "mrview:abc",
            "mrview:-3",
            "mrview:0",
            "mrview:1:2",
            "mrview:",
            "mrview:\u0661\u0662",  # non-ASCII digits
            "mrvp:1",
            "mproof:approve:1",
            "junk",
            None,
            7,
        ]
        for data in forged:
            self.assertIsNone(
                parse_review_callback(data), repr(data)
            )
        self.assertIsNone(parse_page_callback("mrvp:abc"))
        self.assertIsNone(parse_page_callback("mrvp:-1"))

        status, answers, edits = self._press_review("mrview:abc", ADMIN_A)
        self.assertEqual(status, "invalid")
        self.assertEqual(edits, [])


# ══════════════════════════════════════════════════════════════════
# D. Review callback
# ══════════════════════════════════════════════════════════════════


class TestReviewCallback(QueueTestBase):

    def test_valid_pending_claim_opens_existing_review_path(self) -> None:
        sid = self._open_claim()
        snapshot = self._claim_snapshot(sid)

        status, answers, edits = self._press_review(
            review_callback_data(sid), ADMIN_A,
            chat_id=ADMIN_A, message_id=501,
        )
        self.assertEqual(status, "opened")
        self.assertEqual(len(edits), 1)
        chat_id, message_id, text, markup = edits[0]
        self.assertEqual((chat_id, message_id), (ADMIN_A, 501))
        # The EXISTING inbox representation: proof card + mproof buttons.
        self.assertIn("مهمة مراجعة تجريبية", text)
        self.assertIn(PROOF, text)
        # The inbox card renders "رقم الطلب: <id>" (no # prefix — that
        # format belongs to the queue list only).
        self.assertIn(f"رقم الطلب: {sid}", text)
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual(
            [b.callback_data for b in markup.inline_keyboard[0]],
            [
                mproof_callback_data(True, sid),
                mproof_callback_data(False, sid),
            ],
        )
        # Opening reviews NOTHING: claim/user/reward state untouched.
        self.assertEqual(self._claim_snapshot(sid), snapshot)
        self.assertEqual(self._credits(), [])
        self.assertEqual(
            self._user_task_status(WORKER, self.queue_task_id),
            db.USER_TASK_STATUS_STARTED,
        )

    def test_claim_id_is_lookup_only_no_substitution(self) -> None:
        proof_two = "https://example.com/proof-two"
        sid_base = self._open_claim(
            task_id=self.task_id, proof=PROOF, key="sub-1"
        )
        sid_queue = self._open_claim(
            proof=proof_two, key="sub-2"
        )
        status, _, edits = self._press_review(
            review_callback_data(sid_queue), ADMIN_A
        )
        self.assertEqual(status, "opened")
        text = edits[0][2]
        self.assertIn("مهمة مراجعة تجريبية", text)
        self.assertIn(proof_two, text)
        # The OTHER claim's task/proof never leaks into this card.
        self.assertNotIn(PROOF, text)
        self.assertNotIn("مهمة إثبات يدوية", text)

    def test_missing_claim_fails_safely(self) -> None:
        status, answers, edits = self._press_review(
            "mrview:999999", ADMIN_A,
            chat_id=ADMIN_A, message_id=502,
        )
        self.assertEqual(status, "stale")
        self.assertEqual(answers, [MSG_STALE])
        self.assertEqual(
            edits[0][:3], (ADMIN_A, 502, MSG_EDIT_CLOSED)
        )

    def test_dead_task_definition_fails_safely(self) -> None:
        sid = self._open_claim()
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET task_data = '{}' WHERE id = ?",
                (self.queue_task_id,),
            )
        status, answers, edits = self._press_review(
            review_callback_data(sid), ADMIN_A
        )
        self.assertEqual(status, "stale")
        self.assertEqual(answers, [MSG_STALE])
        self.assertEqual(
            self._record(sid).approval_status, "pending"
        )

    def test_inactive_task_claim_fails_safely(self) -> None:
        sid = self._open_claim()
        db.update_task(self.queue_task_id, active=False)
        status, answers, _ = self._press_review(
            review_callback_data(sid), ADMIN_A
        )
        self.assertEqual(status, "stale")
        self.assertEqual(answers, [MSG_STALE])
        self.assertEqual(
            self._record(sid).approval_status, "pending"
        )

    def test_review_creates_linkage_for_unnotified_claim(self) -> None:
        # Claim created while the inbox is unbound → no notification.
        unbind()
        sid = self._open_claim(key="nolink-q")
        bind(self.notifier, _run_now)
        self.assertEqual(self._linkages(sid), [])

        status, _, _ = self._press_review(
            review_callback_data(sid), ADMIN_A,
            chat_id=ADMIN_A, message_id=606,
        )
        self.assertEqual(status, "opened")
        links = self._linkages(sid)
        self.assertEqual(
            [(l.admin_chat_id, l.message_id) for l in links],
            [(ADMIN_A, 606)],
        )
        # …and the card's Approve button works through the existing path.
        press_status, _, _ = self._press_inbox(
            mproof_callback_data(True, sid), ADMIN_A
        )
        self.assertEqual(press_status, "approved")
        self.assertEqual(self._credits(), [REWARD_UNITS])

    def test_non_approver_admin_cannot_open_review(self) -> None:
        # self.task_id's approver is APPROVER — not ADMIN_A.
        sid = self._open_claim(task_id=self.task_id, key="na-1")
        status, answers, edits = self._press_review(
            review_callback_data(sid), ADMIN_A
        )
        self.assertEqual(status, "unauthorized")
        self.assertEqual(answers, [MSG_REVIEW_UNAUTHORIZED])
        self.assertEqual(edits, [], "no card, no state change")
        self.assertEqual(
            self._record(sid).approval_status, "pending"
        )

    def test_non_admin_cannot_open_review(self) -> None:
        sid = self._open_claim()
        status, answers, edits = self._press_review(
            review_callback_data(sid), STRANGER
        )
        self.assertEqual(status, "forbidden")
        self.assertEqual(answers, [MSG_ADMIN_ONLY])
        self.assertEqual(edits, [])

    def test_non_approver_cannot_decide_from_card(self) -> None:
        sid = self._open_claim()
        status, _, _ = self._press_review(
            review_callback_data(sid), ADMIN_A
        )
        self.assertEqual(status, "opened")

        # A different config admin presses ✅ on the card → denied by
        # the EXISTING MT-ADMIN-03 authorization (task approver only).
        press_status, answers, _ = self._press_inbox(
            mproof_callback_data(True, sid), ADMIN_B
        )
        self.assertEqual(press_status, "unauthorized")
        self.assertEqual(self._record(sid).approval_status, "pending")
        self.assertEqual(self._credits(), [])

    def test_callback_adapter_opens_card_in_private_chat(self) -> None:
        sid = self._open_claim()
        query, context = self._press_via_adapter(
            review_callback_data(sid), ADMIN_A,
            chat_id=ADMIN_A, message_id=777,
        )
        query.answer.assert_awaited_once()
        context.bot.edit_message_text.assert_awaited_once()
        kwargs = context.bot.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], ADMIN_A)
        self.assertEqual(kwargs["message_id"], 777)
        self.assertIsInstance(kwargs["reply_markup"], InlineKeyboardMarkup)
        self.assertEqual(
            [
                b.callback_data
                for b in kwargs["reply_markup"].inline_keyboard[0]
            ],
            [
                mproof_callback_data(True, sid),
                mproof_callback_data(False, sid),
            ],
        )

    def test_callback_adapter_silent_outside_private_chat(self) -> None:
        sid = self._open_claim()
        query, context = self._press_via_adapter(
            review_callback_data(sid), ADMIN_A,
            chat_type="supergroup",
        )
        # Dismissed without text; nothing edited or decided.
        query.answer.assert_awaited_once()
        self.assertEqual(query.answer.await_args.kwargs, {})
        context.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(
            self._record(sid).approval_status, "pending"
        )


# ══════════════════════════════════════════════════════════════════
# E. Stale / concurrent state
# ══════════════════════════════════════════════════════════════════


class TestStaleAndConcurrency(QueueTestBase):

    def test_already_approved_claim_shows_already_decided(self) -> None:
        sid = self._open_claim()
        ManualReviewService.decide(
            ADMIN_A, self.queue_task_id, sid, approve=True
        )
        snapshot = self._claim_snapshot(sid)

        status, answers, edits = self._press_review(
            review_callback_data(sid), ADMIN_A
        )
        self.assertEqual(status, "already_decided")
        self.assertEqual(answers, [MSG_ALREADY_DECIDED])
        text, markup = edits[0][2], edits[0][3]
        self.assertIn("تمت الموافقة", text)
        self.assertIsNone(markup, "decided card carries no buttons")
        self.assertEqual(self._claim_snapshot(sid), snapshot)
        self.assertEqual(self._credits(), [REWARD_UNITS])

    def test_already_rejected_claim_shows_already_decided(self) -> None:
        sid = self._open_claim()
        ManualReviewService.decide(
            ADMIN_A, self.queue_task_id, sid, approve=False
        )
        snapshot = self._claim_snapshot(sid)

        status, answers, edits = self._press_review(
            review_callback_data(sid), ADMIN_A
        )
        self.assertEqual(status, "already_decided")
        self.assertEqual(answers, [MSG_ALREADY_DECIDED])
        self.assertIn("تم الرفض", edits[0][2])
        self.assertIsNone(edits[0][3])
        self.assertEqual(self._claim_snapshot(sid), snapshot)
        self.assertEqual(self._credits(), [])

    def test_decision_between_list_and_click_is_safe(self) -> None:
        sid = self._open_claim()
        claims = list_pending_manual_claims()
        self.assertIn(sid, [c.claim_id for c in claims])

        # The claim is decided AFTER the list snapshot was taken.
        ManualReviewService.decide(
            ADMIN_A, self.queue_task_id, sid, approve=True
        )
        snapshot = self._claim_snapshot(sid)
        credits = self._credits()

        status, answers, _ = self._press_review(
            review_callback_data(sid), ADMIN_A
        )
        self.assertEqual(status, "already_decided")
        self.assertEqual(answers, [MSG_ALREADY_DECIDED])
        self.assertEqual(self._claim_snapshot(sid), snapshot)
        self.assertEqual(self._credits(), credits)
        # The fresh queue no longer lists it.
        self.assertNotIn(
            sid, [c.claim_id for c in list_pending_manual_claims()]
        )

    def test_open_then_approve_grants_exactly_one_reward(self) -> None:
        # Claim with NO notification (inbox unbound) → queue is the
        # only way to reach it; the review open persists the linkage.
        unbind()
        sid = self._open_claim(key="chain-1")
        bind(self.notifier, _run_now)

        status, _, _ = self._press_review(
            review_callback_data(sid), ADMIN_A,
            chat_id=ADMIN_A, message_id=505,
        )
        self.assertEqual(status, "opened")

        first, _, _ = self._press_inbox(
            mproof_callback_data(True, sid), ADMIN_A
        )
        self.assertEqual(first, "approved")
        self.assertEqual(self._credits(), [REWARD_UNITS])

        # Replay + queue re-open must not duplicate reward/transition.
        second, _, _ = self._press_inbox(
            mproof_callback_data(True, sid), ADMIN_A
        )
        self.assertEqual(second, "approved")
        self.assertEqual(self._credits(), [REWARD_UNITS])
        self.assertEqual(
            self._user_task_status(WORKER, self.queue_task_id),
            db.USER_TASK_STATUS_COMPLETED,
        )

        reopen, reopen_answers, _ = self._press_review(
            review_callback_data(sid), ADMIN_A
        )
        self.assertEqual(reopen, "already_decided")
        self.assertEqual(reopen_answers, [MSG_ALREADY_DECIDED])
        self.assertEqual(list_pending_manual_claims(), [])

    def test_open_then_reject_single_terminal_state(self) -> None:
        sid = self._open_claim()
        status, _, _ = self._press_review(
            review_callback_data(sid), ADMIN_A
        )
        self.assertEqual(status, "opened")

        press, _, _ = self._press_inbox(
            mproof_callback_data(False, sid), ADMIN_A
        )
        self.assertEqual(press, "rejected")
        record = self._record(sid)
        self.assertEqual(record.approval_status, "rejected")
        self.assertEqual(record.status, db.SUBMISSION_STATUS_FAILED)
        self.assertEqual(self._credits(), [])

        reopen, answers, _ = self._press_review(
            review_callback_data(sid), ADMIN_A
        )
        self.assertEqual(reopen, "already_decided")
        self.assertEqual(answers, [MSG_ALREADY_DECIDED])
        self.assertEqual(list_pending_manual_claims(), [])

    def test_list_page_and_open_never_mutate_claim_state(self) -> None:
        sid = self._open_claim()
        record_before = self._claim_snapshot(sid)
        credits_before = self._credits()
        status_before = self._user_task_status(
            WORKER, self.queue_task_id
        )

        # Read the queue, move a page, open the review — all read-only
        # (plus idempotent linkage persistence) for claim state.
        list_pending_manual_claims()
        self._press_page("mrvp:1", ADMIN_A)
        status, _, _ = self._press_review(
            review_callback_data(sid), ADMIN_A
        )
        self.assertEqual(status, "opened")

        self.assertEqual(self._claim_snapshot(sid), record_before)
        self.assertEqual(self._credits(), credits_before)
        self.assertEqual(
            self._user_task_status(WORKER, self.queue_task_id),
            status_before,
        )
        self.assertEqual(
            self._record(sid).approval_status, "pending"
        )


if __name__ == "__main__":
    unittest.main()
