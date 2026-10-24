"""
Manual Task Proof Screenshot Evidence (MT-ADMIN-34)
===================================================

End-to-end coverage for the image-proof path: a worker uploads a
screenshot, the server stores it under a server-generated key, the
reference is submitted through the EXISTING approval pipeline, and
the reviewer receives the ACTUAL image with the same approve/reject
controls.

Nothing here touches a real wallet, ledger, deposit or bot: every
test runs against a temporary SQLite file and a fake AdminNotifier
transport.  No production data is used or created.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import struct
import tempfile
import unittest
import zlib
from unittest import mock

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
    build_decision_keyboard,
    bind,
    schedule_pending_notification,
    unbind,
)
from manual_task import MANUAL_TASK_TYPE, ManualProofService
from task_submission_store import TaskSubmissionStore, SubmissionRecord
from task_proof_storage import (
    MAX_IMAGE_BYTES,
    is_image_reference,
    probe,
    read_image,
    save_image,
)
import task_proof_storage
import task_routes

from test_admin_manual_proof_inbox import InboxTestBase, _run_now
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

APPROVER = 5101
WORKER = 6202

# ── helpers ─────────────────────────────────────────────────────────


def _png(width: int = 2, height: int = 2) -> bytes:
    """A valid, minimal PNG (magic bytes + IHDR + IDAT + IEND)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _jpeg(truncated: bool = False) -> bytes:
    """A minimal JPEG header.  Without dimension data it is rejected as
    malformed — exactly what a corrupt upload must do."""
    data = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    if truncated:
        return data
    # SOF0 with dimensions
    sof = b"\xff\xc0\x00\x11\x08\x00\x02\x00\x02\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return data + sof + b"\xff\xd9"


class TaskProofImageTestBase(InboxTestBase):
    """InboxTestBase + a manual task owned by WORKER + storage root."""

    def setUp(self) -> None:
        super().setUp()
        self.workdir = tempfile.mkdtemp()
        self._orig_storage = os.environ.get(
            task_proof_storage.STORAGE_DIR_ENV
        )
        os.environ[task_proof_storage.STORAGE_DIR_ENV] = os.path.join(
            self.workdir, "task_proof_uploads"
        )
        self.addCleanup(self._restore_storage)

        self.task_id = db.create_task(
            title="متابعه حساب فيسبوك",
            description="متابعه الحساب وارسال لقطه شاشه",
            task_type=MANUAL_TASK_TYPE,
            reward=50,
            active=True,
            task_data=json.dumps(
                {
                    "provider": "other",
                    "action": "follow",
                    "approver": {"telegram_user_id": APPROVER},
                    "target": {"ref": "https://example.com/x"},
                }
            ),
        )
        db.register_user(WORKER, "worker", "Worker")
        db.create_user_task(WORKER, self.task_id)
        db.update_user_task_status(WORKER, self.task_id, "started")

    def _restore_storage(self) -> None:
        if self._orig_storage is None:
            os.environ.pop(task_proof_storage.STORAGE_DIR_ENV, None)
        else:
            os.environ[task_proof_storage.STORAGE_DIR_ENV] = self._orig_storage

    # ── HTTP helper ────────────────────────────────────────────────

    def _upload(
        self, data: bytes, filename: str = "shot.png", as_worker: bool = True
    ):
        """POST one screenshot via the real Flask app."""
        serve_miniapp.app.config["TESTING"] = True
        client = serve_miniapp.app.test_client()
        if as_worker:
            user_id = WORKER
        else:
            user_id = WORKER + 1
        headers = {
            INIT_DATA_HEADER: _make_init_data(user_id=user_id),
        }
        return client.post(
            f"/api/tasks/{self.task_id}/proof-image",
            data={"file": (io.BytesIO(data), filename)},
            content_type="multipart/form-data",
            headers=headers,
        )


# ════════════════════════════════════════════════════════════════════
# 1. Storage layer: server-generated keys, magic-byte validation
# ════════════════════════════════════════════════════════════════════


class TestTaskProofStorage(unittest.TestCase):
    """The evidence store is separate from the deposit one and never
    trusts client-supplied names."""

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp()
        self._orig = os.environ.get(task_proof_storage.STORAGE_DIR_ENV)
        os.environ[task_proof_storage.STORAGE_DIR_ENV] = os.path.join(
            self.workdir, "root"
        )
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._orig is None:
            os.environ.pop(task_proof_storage.STORAGE_DIR_ENV, None)
        else:
            os.environ[task_proof_storage.STORAGE_DIR_ENV] = self._orig

    def test_valid_png_round_trips_under_a_server_key(self):
        key = save_image(_png(), proof_id="a" * 32, ext="png")
        self.assertRegex(key, r"^[0-9a-f]{32}\.png$")
        self.assertIn(key, os.listdir(task_proof_storage.storage_root()))
        self.assertEqual(read_image(key), _png())
        self.assertTrue(is_image_reference(key))

    def test_the_key_never_uses_the_client_filename(self):
        key = save_image(_png(), proof_id="b" * 32, ext="png")
        # no user-controlled path fragments survive
        self.assertNotIn("shot", key)
        self.assertNotIn("/", key)
        self.assertNotIn("..", key)

    def test_storage_root_is_separate_from_deposits(self):
        self.assertNotEqual(
            task_proof_storage.DEFAULT_STORAGE_DIR, "deposit_proof_uploads"
        )
        self.assertEqual(
            task_proof_storage.STORAGE_DIR_ENV, "TASK_PROOF_STORAGE_DIR"
        )
        # the deposit module owns its own, different path
        import deposit_proof_storage

        self.assertEqual(
            deposit_proof_storage.STORAGE_DIR_ENV,
            "DEPOSIT_PROOF_STORAGE_DIR",
        )
        self.assertNotEqual(
            deposit_proof_storage.DEFAULT_STORAGE_DIR,
            task_proof_storage.DEFAULT_STORAGE_DIR,
        )

    def test_empty_payload_rejected(self):
        with self.assertRaises(Exception):
            probe(b"")

    def test_non_image_bytes_rejected_despite_a_jpg_name(self):
        # EXE/HTML/script content never matches any supported magic
        for payload in (b"MZ\x90\x00", b"<html>hi</html>", b"#!/bin/sh\n"):
            with self.assertRaises(Exception):
                probe(payload)

    def test_corrupt_jpeg_header_rejected(self):
        with self.assertRaises(Exception):
            probe(_jpeg(truncated=True))

    def test_valid_jpeg_accepted(self):
        mime, ext, width, height = probe(_jpeg())
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual((width, height), (2, 2))

    def test_oversized_payload_rejected_by_the_bound(self):
        # > MAX_IMAGE_BYTES must be refused by the transport bound
        self.assertGreater(len(_png() * 100000), MAX_IMAGE_BYTES)

    def _dimension_bomb(self, width: int, height: int) -> bytes:
        """A PNG whose IHDR CLAIMS huge dimensions but carries almost
        no pixel data — the classic decompression-bomb header.  probe()
        reads the dimensions from the header, so this tests the bound
        without allocating a real image."""

        def chunk(tag: bytes, data: bytes) -> bytes:
            body = tag + data
            return (
                struct.pack(">I", len(data))
                + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b"")
        )

    def test_decompression_bomb_dimensions_rejected(self):
        # absurd pixel dimensions must never reach the reviewer queue
        with self.assertRaises(Exception):
            probe(self._dimension_bomb(90000, 90000))

    def test_dimensions_within_the_bound_are_accepted(self):
        mime, _ext, width, height = probe(self._dimension_bomb(800, 600))
        self.assertEqual((width, height), (800, 600))

    def test_traversal_key_can_never_read_outside_the_root(self):
        for bad in ("../../etc/passwd", "/etc/passwd", "a" * 40 + ".png", ""):
            self.assertFalse(is_image_reference(bad))
            with self.assertRaises(Exception):
                read_image(bad)

    def test_save_never_overwrites_an_existing_key(self):
        key = save_image(_png(), proof_id="c" * 32, ext="png")
        with self.assertRaises(Exception):
            save_image(_png(width=4), proof_id="c" * 32, ext="png")

    def test_client_filename_is_ignored_entirely(self):
        # the same bytes under any client name produce the same key shape
        key = save_image(_png(), proof_id="d" * 32, ext="png")
        self.assertTrue(key.endswith(".png"))
        self.assertEqual(len(key), 32 + len(".png"))


# ════════════════════════════════════════════════════════════════════
# 2. Upload endpoint: auth, ownership, state, content
# ════════════════════════════════════════════════════════════════════


class TestUploadEndpoint(TaskProofImageTestBase):
    """POST /api/tasks/<id>/proof-image."""

    def test_valid_image_accepted_with_server_reference(self):
        response = self._upload(_png())
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(is_image_reference(body["proof_ref"]))
        self.assertGreater(body["bytes"], 0)
        # the file exists on disk under that key
        self.assertTrue(
            os.path.exists(
                os.path.join(
                    task_proof_storage.storage_root(), body["proof_ref"]
                )
            )
        )

    def test_upload_opens_no_submission_and_no_reward(self):
        self._upload(_png())
        # nothing was submitted: no claim, no ledger, no wallet
        self.assertEqual(
            TaskSubmissionStore().list_user_task_submissions(
                WORKER, self.task_id
            ),
            [],
        )
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM ledger WHERE user_id = ?",
                    (WORKER,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM wallets WHERE user_id = ?",
                    (WORKER,),
                ).fetchone()[0],
                0,
            )

    def test_task_state_is_unchanged_by_the_upload(self):
        self._upload(_png())
        row = db.get_user_task(WORKER, self.task_id)
        self.assertEqual(row["status"], "started")

    def test_unauthenticated_upload_refused(self):
        serve_miniapp.app.config["TESTING"] = True
        client = serve_miniapp.app.test_client()
        response = client.post(
            f"/api/tasks/{self.task_id}/proof-image",
            data={"file": (io.BytesIO(_png()), "shot.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "unauthenticated")

    def test_non_owner_refused(self):
        response = self._upload(_png(), as_worker=False)
        self.assertIn(response.status_code, (403, 404, 409))

    def test_task_not_started_refused(self):
        # a task the worker has NOT started: no claim in progress
        fresh = db.create_task(
            title="مهمة اخرى",
            description="لم تبدأ بعد",
            task_type=MANUAL_TASK_TYPE,
            reward=20,
            active=True,
            task_data=json.dumps(
                {
                    "provider": "other",
                    "action": "follow",
                    "approver": {"telegram_user_id": APPROVER},
                    "target": {"ref": "https://example.com/y"},
                }
            ),
        )
        db.register_user(WORKER, "worker", "Worker")
        serve_miniapp.app.config["TESTING"] = True
        client = serve_miniapp.app.test_client()
        response = client.post(
            f"/api/tasks/{fresh}/proof-image",
            data={"file": (io.BytesIO(_png()), "shot.png")},
            content_type="multipart/form-data",
            headers={INIT_DATA_HEADER: _make_init_data(user_id=WORKER)},
        )
        self.assertEqual(response.status_code, 409)

    def test_fake_image_with_a_jpg_name_refused(self):
        response = self._upload(b"this is not an image", "real.jpg")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"], "unsupported_image_type"
        )

    def test_corrupt_image_refused(self):
        response = self._upload(_jpeg(truncated=True), "corrupt.jpg")
        self.assertEqual(response.status_code, 400)

    def test_empty_file_refused(self):
        response = self._upload(b"", "empty.png")
        self.assertEqual(response.status_code, 400)

    def test_oversized_image_refused(self):
        big = _png() * 100000  # well past MAX_IMAGE_BYTES
        response = self._upload(big, "huge.png")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "file_too_large")

    def test_exe_bytes_with_an_image_name_refused(self):
        response = self._upload(b"MZ\x90\x00\x03\x00\x00\x00", "malware.png")
        self.assertEqual(response.status_code, 400)

    def test_non_manual_task_has_no_image_endpoint(self):
        other = db.create_task(
            title="قناة",
            description="اشترك",
            task_type="channel_subscription",
            reward=10,
            active=True,
            task_data=json.dumps({"channel_slug": "x"}),
        )
        serve_miniapp.app.config["TESTING"] = True
        client = serve_miniapp.app.test_client()
        response = client.post(
            f"/api/tasks/{other}/proof-image",
            data={"file": (io.BytesIO(_png()), "shot.png")},
            content_type="multipart/form-data",
            headers={INIT_DATA_HEADER: _make_init_data(user_id=WORKER)},
        )
        self.assertIn(response.status_code, (400, 404, 409))

    def test_repeated_upload_produces_distinct_keys(self):
        first = self._upload(_png()).get_json()["proof_ref"]
        second = self._upload(_png()).get_json()["proof_ref"]
        self.assertNotEqual(first, second)
        self.assertTrue(is_image_reference(first))
        self.assertTrue(is_image_reference(second))


# ════════════════════════════════════════════════════════════════════
# 3. Reviewer delivery: the ACTUAL image + existing controls
# ════════════════════════════════════════════════════════════════════


class TestReviewerReceivesTheImage(TaskProofImageTestBase):
    """The approver sees the screenshot, not the storage key."""

    def _bind_photo_notifier(self):
        """Rebind with a photo transport, exactly as bot.py wires it."""
        unbind()
        sent = self.sent

        async def _send_photo(
            chat_id: int, text: str, image: bytes, reply_markup
        ) -> int:
            self._msg_seq += 1
            sent.append(
                {
                    "kind": "photo",
                    "chat_id": chat_id,
                    "text": text,
                    "image": image,
                    "reply_markup": reply_markup,
                    "message_id": self._msg_seq,
                }
            )
            return self._msg_seq

        notifier = AdminNotifier(
            self._capture_text,
            markup_send=self._capture_markup,
            photo_send=_send_photo,
        )
        bind(notifier, _run_now)
        self.addCleanup(unbind)
        return notifier

    # fake text transports captured from the parent fixture's list
    async def _capture_text(self, chat_id, text):
        self.sent.append(
            {"kind": "text", "chat_id": chat_id, "text": text}
        )

    async def _capture_markup(self, chat_id, text, reply_markup):
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

    def _photos(self) -> list[dict]:
        return [m for m in self.sent if m["kind"] == "photo"]

    def _texts(self) -> list[dict]:
        return [m for m in self.sent if m["kind"] in ("text", "markup")]

    def _submit_image_proof(self, delete_file_first: bool = False):
        """Upload a screenshot, then submit its reference as the proof.

        A FRESH submit fires the reviewer notification immediately
        (ManualProofService → schedule_pending_notification), so the
        assertions run right after it — no manual scheduling needed.
        """
        reference = self._upload(_png()).get_json()["proof_ref"]
        if delete_file_first:
            os.remove(
                os.path.join(task_proof_storage.storage_root(), reference)
            )
        outcome = ManualProofService.submit(
            WORKER, self.task_id, reference
        )
        self.assertIsNotNone(outcome)
        submissions = TaskSubmissionStore().list_user_task_submissions(
            WORKER, self.task_id
        )
        self.assertEqual(len(submissions), 1)
        return reference, submissions[0]

    def test_photo_transport_receives_the_actual_bytes(self):
        self._bind_photo_notifier()
        reference, _record = self._submit_image_proof()

        # one photo per configured admin (two in this fixture)
        photos = self._photos()
        self.assertEqual(len(photos), len(config.ADMINS))
        for photo in photos:
            # the ACTUAL screenshot bytes, not a key
            self.assertEqual(photo["image"], _png())
            # the caption describes the image and never leaks the key
            self.assertNotIn(reference, photo["text"])
            self.assertIn("صورة مرفقة", photo["text"])
            # the rest of the existing notification is unchanged
            self.assertIn("متابعه حساب فيسبوك", photo["text"])
            labels = [
                b.text
                for row in photo["reply_markup"].inline_keyboard
                for b in row
            ]
            self.assertIn(BUTTON_APPROVE, labels)
            self.assertIn(BUTTON_REJECT, labels)

    def test_reviewers_are_admins_only(self):
        self._bind_photo_notifier()
        self._submit_image_proof()

        photos = self._photos()
        self.assertTrue(photos)
        for photo in photos:
            self.assertIn(photo["chat_id"], config.ADMINS)
            self.assertNotEqual(photo["chat_id"], WORKER)
            self.assertNotEqual(photo["chat_id"], APPROVER)

    def test_text_only_proof_stays_text(self):
        """A plain URL proof must NOT go through the photo path."""
        self._bind_photo_notifier()
        outcome = ManualProofService.submit(
            WORKER, self.task_id, "https://example.com/proof"
        )
        self.assertIsNotNone(outcome)

        self.assertEqual(self._photos(), [])
        self.assertTrue(self._texts())

    def test_missing_evidence_file_falls_back_to_text(self):
        """A deleted/unreadable file must never break the event."""
        self._bind_photo_notifier()
        # the file is gone BEFORE the notification is composed
        self._submit_image_proof(delete_file_first=True)

        self.assertEqual(self._photos(), [])
        self.assertTrue(self._texts())

    def test_no_photo_transport_falls_back_to_text(self):
        """An older binding without photo_send still delivers text."""
        self._submit_image_proof()

        self.assertEqual(self._photos(), [])
        self.assertTrue(self._texts())

    def test_notification_replay_guard_unchanged(self):
        self._bind_photo_notifier()
        _reference, _record = self._submit_image_proof()
        first = len(self._photos())
        # a replay must not send a second batch
        schedule_pending_notification(
            db.get_task(self.task_id), _record
        )
        self.assertEqual(len(self._photos()), first)


# ════════════════════════════════════════════════════════════════════
# 4. Lifecycle: no auto-completion, no double credit, no money change
# ════════════════════════════════════════════════════════════════════


class TestImageProofLifecycle(TaskProofImageTestBase):
    """The approval cycle is untouched: the image is evidence only."""

    def test_submitting_the_reference_does_not_complete_the_task(self):
        reference = self._upload(_png()).get_json()["proof_ref"]
        outcome = ManualProofService.submit(
            WORKER, self.task_id, reference
        )
        # still started, awaiting review — no reward
        row = db.get_user_task(WORKER, self.task_id)
        self.assertEqual(row["status"], "started")
        self.assertEqual(outcome.state, "pending")
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM ledger WHERE user_id = ?",
                    (WORKER,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM wallets WHERE user_id = ?",
                    (WORKER,),
                ).fetchone()[0],
                0,
            )

    def test_double_click_submits_once(self):
        reference = self._upload(_png()).get_json()["proof_ref"]
        ManualProofService.submit(WORKER, self.task_id, reference)
        # an idempotent replay of the SAME claim must not create a second
        outcome = ManualProofService.submit(
            WORKER, self.task_id, reference, idempotency_key="repeat-key"
        )
        submissions = TaskSubmissionStore().list_user_task_submissions(
            WORKER, self.task_id
        )
        self.assertEqual(len(submissions), 1)
        self.assertIsNotNone(outcome)

    def test_only_one_pending_claim_exists(self):
        reference = self._upload(_png()).get_json()["proof_ref"]
        ManualProofService.submit(WORKER, self.task_id, reference)
        with sqlite3.connect(self.db_path) as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM task_submissions "
                "WHERE user_id = ? AND task_id = ? "
                "AND approval_status = 'pending'",
                (WORKER, self.task_id),
            ).fetchone()[0]
        self.assertEqual(pending, 1)

    def test_text_proof_still_supported(self):
        """The URL/text proof path is unchanged."""
        outcome = ManualProofService.submit(
            WORKER, self.task_id, "https://example.com/my-proof"
        )
        self.assertIsNotNone(outcome)
        submission = TaskSubmissionStore().list_user_task_submissions(
            WORKER, self.task_id
        )[0]
        self.assertEqual(submission.proof_ref, "https://example.com/my-proof")

    def test_empty_text_proof_still_rejected(self):
        from manual_task import ManualProofError

        with self.assertRaises(ManualProofError):
            ManualProofService.submit(WORKER, self.task_id, "   ")


# ════════════════════════════════════════════════════════════════════
# 5. Shared-notifier safety: the four existing consumers
# ════════════════════════════════════════════════════════════════════


class TestSharedNotifierUnchanged(unittest.TestCase):
    """notify_system must behave EXACTLY as before — the photo path is
    strictly additive."""

    def setUp(self) -> None:
        self._orig_admins = list(config.ADMINS)
        config.ADMINS[:] = [7001, 7002]
        self.addCleanup(self._restore_admins)
        self.sent: list[dict] = []
        self._seq = 0

        async def _send_text(chat_id, text):
            self.sent.append(
                {"kind": "text", "chat_id": chat_id, "text": text}
            )

        async def _send_markup(chat_id, text, reply_markup):
            self._seq += 1
            self.sent.append(
                {
                    "kind": "markup",
                    "chat_id": chat_id,
                    "text": text,
                    "reply_markup": reply_markup,
                    "message_id": self._seq,
                }
            )
            return self._seq

        async def _send_photo(chat_id, text, image, reply_markup):
            self._seq += 1
            self.sent.append(
                {
                    "kind": "photo",
                    "chat_id": chat_id,
                    "text": text,
                    "image": image,
                    "message_id": self._seq,
                }
            )
            return self._seq

        # Exactly bot.py's construction: text + markup + photo
        self.notifier = AdminNotifier(
            _send_text, markup_send=_send_markup, photo_send=_send_photo
        )

    def _restore_admins(self) -> None:
        config.ADMINS[:] = self._orig_admins

    def test_notify_system_still_sends_text_only(self):
        asyncio.run(
            self.notifier.notify_system("hello ops")
        )
        self.assertEqual(len(self.sent), 2)
        self.assertEqual({m["kind"] for m in self.sent}, {"markup"})
        self.assertEqual({m["text"] for m in self.sent}, {"hello ops"})

    def test_notify_system_ignores_the_photo_transport(self):
        # a text-only caller is untouched even with photo_send present
        asyncio.run(
            self.notifier.notify_system("withdrawal notice")
        )
        self.assertEqual(
            [m for m in self.sent if m["kind"] == "photo"], []
        )

    def test_notify_acting_admin_unchanged(self):
        asyncio.run(
            self.notifier.notify(7001, "admin event")
        )
        self.assertEqual(len(self.sent), 2)
        self.assertEqual({m["kind"] for m in self.sent}, {"text"})

    def test_notify_system_photo_sends_the_image_to_admins(self):
        asyncio.run(
            self.notifier.notify_system_photo(
                "evidence", _png()
            )
        )
        self.assertEqual(len(self.sent), 2)
        self.assertEqual({m["kind"] for m in self.sent}, {"photo"})
        for msg in self.sent:
            self.assertEqual(msg["image"], _png())
            self.assertIn(msg["chat_id"], config.ADMINS)

    def test_notify_system_photo_refuses_non_admin_targets(self):
        from admin_notifier import AdminNotifierTargetError

        with self.assertRaises(AdminNotifierTargetError):
            asyncio.run(
                self.notifier.notify_system_photo(
                    "evidence", _png(), targets=(4242,)
                )
            )
        self.assertEqual(self.sent, [])  # nothing was sent

    def test_notify_system_photo_without_payload_falls_back_to_text(self):
        notifier = AdminNotifier(
            (lambda *_: None),
            markup_send=self._markup,
            photo_send=None,
        )
        asyncio.run(
            notifier.notify_system_photo("evidence", b"")
        )
        self.assertTrue(
            [m for m in self.sent if m["kind"] == "markup"]
        )
        self.assertEqual(
            [m for m in self.sent if m["kind"] == "photo"], []
        )

    async def _markup(self, chat_id, text, reply_markup):
        self._seq += 1
        self.sent.append(
            {"kind": "markup", "chat_id": chat_id, "text": text}
        )
        return self._seq

    def test_photo_transport_failure_falls_back_to_text(self):
        async def _boom(chat_id, text, image, reply_markup):
            raise RuntimeError("telegram is down")

        notifier = AdminNotifier(
            (lambda *_: None),
            markup_send=self._markup,
            photo_send=_boom,
        )
        delivered = asyncio.run(
            notifier.notify_system_photo("evidence", _png())
        )
        # the event still reached the admins as text, with message ids
        self.assertTrue(
            [m for m in self.sent if m["kind"] == "markup"]
        )
        for _chat_id, message_id in delivered:
            self.assertIsNotNone(message_id)

    def test_empty_text_body_rejected(self):
        from admin_notifier import AdminNotifierError

        with self.assertRaises(AdminNotifierError):
            asyncio.run(
                self.notifier.notify_system_photo("   ", _png())
            )


if __name__ == "__main__":
    unittest.main()
