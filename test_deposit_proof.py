"""
Focused tests — Manual Deposit Proof & Admin Verification (MT-ADMIN-31)
=======================================================================

The MANUAL evidence path that runs ALONGSIDE the automatic BSC
verification — and can never credit anything by itself:

    user uploads screenshot (Mini App, initData auth)
        → deposit_proofs row (pending_review) + image bytes on disk
        → /deposits admin queue (config.is_admin, private chat only)
        → ✅ اعتماد الدفع → deposit_manual_review.approve
              → deposit_verification.verify_and_credit  (the EXISTING
                atomic credit boundary — persisted amount_units only)
        → ❌ رفض الإثبات → deposit_proof_store.mark_reviewed
              (evidence decision only — no wallet, no ledger, no
              deposit status change, evidence never deleted)

Coverage required by MT-ADMIN-31 (36 required cases + focused extras):

A. AUTHENTICATION (1–4)
   1  missing initData rejected
   2  invalid initData rejected
   3  body/query user_id ignored
   4  proof associated with the authenticated Telegram user

B. OWNERSHIP (5–6)
   5  user cannot submit proof for another user's request
   6  nonexistent request rejected

C. UPLOAD VALIDATION (7–11)
   7  valid screenshot accepted (PNG/JPEG/GIF; client filename never
      trusted or stored)
   8  unsupported type rejected
   9  oversized image rejected
  10  malformed upload rejected
  11  empty upload rejected

D. FINANCIAL ISOLATION (12–14)
  12  proof upload does not credit wallet
  13  proof upload does not write ledger
  14  proof upload does not change deposit financial status

E. PROOF LIFECYCLE (15–18)
  15  proof becomes pending review
  16  admin sees the submitted proof (queue + screenshot detail)
  17  rejection records reviewer and time
  18  rejected proof never credits wallet

F. MANUAL APPROVAL (19–24)
  19  approval credits the exact persisted amount_units
  20  approval writes exactly one ledger credit
  21  approval marks the deposit credited
  22  approval is atomic (concurrent double approval → one credit)
  23  wallet failure rolls back everything
  24  ledger failure rolls back everything

G. IDEMPOTENCY / RACES (25–29)
  25  double approval cannot double-credit
  26  stale callback cannot double-credit
  27  already-credited deposit cannot be credited again
  28  automatic BSC credit followed by manual approval does not
      double-credit (external tx id preserved)
  29  manual credit followed by automatic verification does not
      double-credit

H. AUTHORIZATION (30–32)
  30  non-admin cannot approve
  31  group/channel admin actions are blocked (private chat only)
  32  unauthorized user cannot see another user's screenshot

I. EVIDENCE INTEGRITY (33–36)
  33  persisted proof remains associated with the correct request
  34  rejected evidence is not silently deleted
  35  admin reviewer identity is persisted
  36  review timestamp is persisted

J. EXTRAS (37–48) — discovered edge cases:
  37  repeat upload while pending is a stable 409 (one active proof)
  38  a new proof is allowed after rejection (old row kept)
  39  upload for a credited (terminal) deposit rejected
  40  the upload response never exposes storage metadata
  41  admin detail card shows required facts, never pm_destination
  42  bot.py registers /deposits and the dp: callback family
  43  Mini App proof UI contract (only the review-pending claim)
  44  the upload route cannot import the financial boundary
  45  storage keys can never traverse outside the evidence root
  46  image dimension bound enforced end-to-end
  47  stable error codes + approval API carries no amount/tx/facts
  48  concurrent duplicate submission: exactly one pending proof

Temp databases only; no production destinations or balances are used.
Money math is int-only.  Existing automatic BSC verification
(``deposit_verification`` / ``bsc_deposit_adapter``) is untouched.

Run:
    python3 -m pytest test_deposit_proof.py -v
"""

from __future__ import annotations

import ast
import inspect
import io
import re
import sqlite3
import struct
import threading
import uuid

import pytest
from telegram import InlineKeyboardMarkup, InputFile

import config
import db
import deposit_manual_review
import deposit_proof_admin
import deposit_proof_storage
import deposit_proof_store
import deposit_routes
import deposit_store
import deposit_verification
import ledger
import serve_miniapp
import wallet

from config import is_admin
from deposit_proof_admin import (
    DETAIL_HEADER,
    LIST_HEADER,
    MSG_ADMIN_ONLY,
    view_callback_data,
)
from test_deposit import (
    ADMIN_ID,
    MIN_UNITS,
    PM_DESTINATION,
    USER_A,
    _auth,
    _make_pm,
    _read_js,
)
from test_deposit_verification import (
    AMOUNT,
    AMOUNT_UNITS,
    FUND,
    TX_OK,
    _deposit_ledger,
    _fund,
    _row,
    _wallet_units,
)
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data
from test_payment_methods import (
    _answered,
    _callback,
    _edited,
    _reply,
    _run,
    _update,
)

INIT_DATA_HEADER = "X-Telegram-Init-Data"
USER_B = 4402
STRANGER = 999999
ADMIN_2 = 900_029
IMPERSONATED = 7777

SUBMITTED_MSG = "تم إرسال إثبات الدفع للمراجعة"
# User-facing claims that an upload may NEVER produce.
FORBIDDEN_USER_CLAIMS = (
    "تم استلام الأموال",
    "تمت إضافة",
    "أُضيف",
    "تم الإيداع بنجاح",
    "تم اعتماد الدفع",
)
# The claims test_deposit already pins for the whole file (kept in
# lock-step so this task cannot weaken them).
FORBIDDEN_JS_CLAIMS = (
    "تمت إضافة",
    "أُضيف",
    "تم الإيداع بنجاح",
    "credited",
    "تمت إضافة رصيدك",
)

_STORAGE_KEY_RE = re.compile(r"[0-9a-f]{32}\.(png|jpg|gif)\Z")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\Z")


# ── Fixtures / helpers ────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated DB + evidence storage + admin configuration."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    monkeypatch.setenv(
        "DEPOSIT_PROOF_STORAGE_DIR", str(tmp_path / "proofs")
    )
    db_path = str(tmp_path / "deposit_proof.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_A, "alice", "Alice")
    monkeypatch.setattr(config, "ADMINS", [ADMIN_ID])
    yield db_path


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


def _png(width: int = 64, height: int = 48) -> bytes:
    """A probe-valid PNG payload (magic + IHDR + dimensions)."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * 16
    )


def _jpeg(width: int = 80, height: int = 60) -> bytes:
    """A probe-valid JPEG payload (SOF0 frame header)."""
    return (
        b"\xff\xd8"
        + b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x00" * 16
    )


def _gif(width: int = 32, height: int = 32) -> bytes:
    """A probe-valid GIF payload."""
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 8


def _request(db_path: str, *, amount: str = AMOUNT, user_id: int = USER_A):
    """One PENDING deposit intent through the production creator."""
    pm = _make_pm(db_path, min_units=MIN_UNITS)
    return deposit_store.create_deposit_request(
        user_id=user_id,
        payment_method_id=pm.id,
        amount=amount,
        db_path=db_path,
    )


def _new_proof(request_id: str, user_id: int = USER_A):
    """A pending proof row WITHOUT touching disk (service-level setup).

    ``deposit_proof_store`` never reads the file — only the admin
    rendering path does — so a synthetic server-shaped key keeps the
    financial/review tests independent of the HTTP upload.
    """
    return deposit_proof_store.submit_proof(
        request_id=request_id,
        user_id=user_id,
        storage_key=f"{uuid.uuid4().hex}.png",
        mime_type="image/png",
        size_bytes=2048,
        width=64,
        height=48,
    )


def _upload(
    client,
    request_id: str,
    data: bytes = b"",
    *,
    filename: str = "receipt.png",
    headers: dict | None = None,
    fields: dict | None = None,
    query: str = "",
):
    """One multipart POST /api/deposit/proof (no client content type)."""
    payload: dict = {"request_id": request_id}
    if fields:
        payload.update(fields)
    if data is not None:
        payload["file"] = (io.BytesIO(data), filename)
    return client.post(
        f"/api/deposit/proof{query}",
        data=payload,
        headers=headers if headers is not None else _auth(),
    )


def _proof_rows(db_path: str, request_id: str | None = None) -> list:
    sql = "SELECT * FROM deposit_proofs"
    params: tuple = ()
    if request_id is not None:
        sql += " WHERE request_id = ?"
        params = (request_id,)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _stored_files() -> list:
    root = deposit_proof_storage.storage_root()
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir())


def _context():
    from unittest.mock import AsyncMock, MagicMock

    context = MagicMock()
    context.bot.send_photo = AsyncMock()
    context.bot.send_message = AsyncMock()
    return context


def _markup_of_reply(update) -> InlineKeyboardMarkup | None:
    return update.message.reply_text.call_args[1].get("reply_markup")


# ══════════════════════════════════════════════════════════════════
# A. Authentication (1–4)
# ══════════════════════════════════════════════════════════════════


class TestAuthentication:
    def test_01_missing_init_data_rejected(self, client, env):
        """1. No initData → the existing 401 contract; no proof row,
        no evidence file."""
        req = _request(env)
        response = _upload(
            client, req.request_id, _png(), headers={}
        )
        assert response.status_code == 401
        data = response.get_json()
        assert data["ok"] is False
        assert data["error"] == "unauthenticated"
        assert _proof_rows(env, req.request_id) == []
        assert _stored_files() == []

    def test_02_invalid_init_data_rejected(self, client, env):
        """2. Forged initData fails HMAC validation → same 401, and
        no proof record is ever created."""
        req = _request(env)
        response = _upload(
            client,
            req.request_id,
            _png(),
            headers={INIT_DATA_HEADER: "user=%7B%22id%22%3A4401%7D&hash=bad"},
        )
        assert response.status_code == 401
        assert response.get_json()["error"] == "unauthenticated"
        assert _proof_rows(env, req.request_id) == []
        assert _stored_files() == []

    def test_03_body_and_query_user_id_ignored(self, client, env):
        """3. A client-supplied user_id (form AND query string) is
        ignored — identity comes only from verified initData."""
        req = _request(env)
        response = _upload(
            client,
            req.request_id,
            _png(),
            fields={"user_id": str(IMPERSONATED)},
            query=f"?user_id={IMPERSONATED}",
        )
        assert response.status_code == 200
        rows = _proof_rows(env, req.request_id)
        assert len(rows) == 1
        assert rows[0]["user_id"] == USER_A
        assert rows[0]["user_id"] != IMPERSONATED

    def test_04_proof_associated_with_authenticated_user(self, client, env):
        """4. The persisted proof carries the Telegram identity from
        validated initData and the exact request id."""
        req = _request(env)
        response = _upload(client, req.request_id, _png())
        assert response.status_code == 200
        body = response.get_json()
        assert body["proof"]["request_id"] == req.request_id

        rows = _proof_rows(env, req.request_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["user_id"] == USER_A
        assert row["request_id"] == req.request_id
        # a SECOND authenticated user's upload is a different identity
        db.register_user(USER_B, "bob", "Bob")
        req_b = _request(env, user_id=USER_B)
        response_b = _upload(
            client, req_b.request_id, _png(), headers=_auth(USER_B)
        )
        assert response_b.status_code == 200
        assert _proof_rows(env, req_b.request_id)[0]["user_id"] == USER_B


# ══════════════════════════════════════════════════════════════════
# B. Ownership (5–6)
# ══════════════════════════════════════════════════════════════════


class TestOwnership:
    def test_05_cannot_submit_for_another_users_request(self, client, env):
        """5. Another user's request → 403 and zero side effects
        (nothing persisted, no evidence file written)."""
        db.register_user(USER_B, "bob", "Bob")
        req_b = _request(env, user_id=USER_B)

        response = _upload(client, req_b.request_id, _png())
        assert response.status_code == 403
        data = response.get_json()
        assert data["error"] == "request_forbidden"
        assert _proof_rows(env, req_b.request_id) == []
        assert _stored_files() == []

    def test_06_nonexistent_request_rejected(self, client, env):
        """6. Unknown request id → 404, no proof, no file."""
        response = _upload(client, "0" * 32, _png())
        assert response.status_code == 404
        assert response.get_json()["error"] == "request_not_found"
        assert _proof_rows(env) == []
        assert _stored_files() == []


# ══════════════════════════════════════════════════════════════════
# C. Upload validation (7–11)
# ══════════════════════════════════════════════════════════════════


class TestUploadValidation:
    @pytest.mark.parametrize(
        "payload, ext",
        [(_png(), "png"), (_jpeg(), "jpg"), (_gif(), "gif")],
        ids=["png", "jpeg", "gif"],
    )
    def test_07_valid_screenshot_accepted(
        self, client, env, payload, ext
    ):
        """7. A real image is accepted; the CLIENT filename is never
        stored or echoed — the key is server-generated."""
        req = _request(env)
        # hostile client filename: must have zero effect
        response = _upload(
            client, req.request_id, payload, filename=f"../../../evil.{ext}"
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["message"] == SUBMITTED_MSG
        assert data["proof"]["status"] == "pending_review"
        for claim in FORBIDDEN_USER_CLAIMS:
            assert claim not in response.get_data(as_text=True)

        row = _proof_rows(env, req.request_id)[0]
        assert _STORAGE_KEY_RE.fullmatch(row["storage_key"])
        assert "evil" not in row["storage_key"]
        assert row["mime_type"] == {
            "png": "image/png",
            "jpg": "image/jpeg",
            "gif": "image/gif",
        }[ext]
        assert row["size_bytes"] == len(payload)
        assert row["storage_key"] in _stored_files()

    def test_08_unsupported_type_rejected(self, client, env):
        """8. Script/HTML content is rejected by MAGIC BYTES, never
        by the claimed filename/type."""
        req = _request(env)
        for payload in (
            b"#!/bin/sh\necho pwned\n",
            b"<html><body>hi</body></html>",
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            b"PK\x03\x04zip-archive-bytes",
        ):
            response = _upload(client, req.request_id, payload)
            assert response.status_code == 400
            assert response.get_json()["error"] == "unsupported_image_type"
        assert _proof_rows(env, req.request_id) == []
        assert _stored_files() == []

    def test_09_oversized_image_rejected(self, client, env):
        """9. Payloads above MAX_IMAGE_BYTES are refused before any
        sniffing/persistence."""
        req = _request(env)
        huge = b"\x89PNG\r\n\x1a\n" + b"\x00" * (
            deposit_proof_storage.MAX_IMAGE_BYTES + 1
        )
        response = _upload(client, req.request_id, huge)
        assert response.status_code == 400
        assert response.get_json()["error"] == "file_too_large"
        assert _proof_rows(env, req.request_id) == []
        assert _stored_files() == []

    @pytest.mark.parametrize(
        "payload",
        [
            b"\x89PNG\r\n\x1a\nshort",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 4 + b"XXXX" + b"\x00" * 16,
            b"\xff\xd8\xff\xe0\x00\x04AB" + b"CDEFGH" * 4,
        ],
        ids=["png-truncated", "png-bad-ihdr", "jpeg-no-frame"],
    )
    def test_10_malformed_upload_rejected(self, client, env, payload):
        """10. Structurally broken images are rejected with a stable
        code — nothing persisted."""
        req = _request(env)
        response = _upload(client, req.request_id, payload)
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_upload"
        assert _proof_rows(env, req.request_id) == []
        assert _stored_files() == []

    def test_11_empty_upload_rejected(self, client, env):
        """11. Empty file, missing file part, missing request_id and
        malformed multipart all fail transport validation."""
        req = _request(env)

        response = _upload(client, req.request_id, b"")
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_upload"

        response = _upload(client, req.request_id, None)
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_upload"

        response = _upload(client, "", _png())
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_request"

        response = client.post(
            "/api/deposit/proof",
            data="--BROKEN\r\nnot-a-part",
            content_type="multipart/form-data; boundary=BROKEN",
            headers=_auth(),
        )
        assert response.status_code == 400
        assert _proof_rows(env, req.request_id) == []
        assert _stored_files() == []


# ══════════════════════════════════════════════════════════════════
# D. Financial isolation (12–14)
# ══════════════════════════════════════════════════════════════════


class TestFinancialIsolation:
    def test_12_proof_upload_does_not_credit_wallet(self, client, env):
        """12. Upload never moves money — an untouched wallet stays
        untouched and an existing balance is unchanged."""
        req = _request(env)
        before = _wallet_units(env)
        assert before in (None, (0, 0))
        assert _upload(client, req.request_id, _png()).status_code == 200
        assert _wallet_units(env) == before

        _fund()
        req2 = _request(env)
        assert _upload(client, req2.request_id, _png()).status_code == 200
        assert _wallet_units(env) == (FUND, 0)

    def test_13_proof_upload_does_not_write_ledger(self, client, env):
        """13. No ledger entry of any kind appears for the request."""
        _fund()
        req = _request(env)
        before = len(_raw_ledger(env))
        assert _upload(client, req.request_id, _png()).status_code == 200
        assert _deposit_ledger(env, req.request_id) == []
        assert len(_raw_ledger(env)) == before

    def test_14_proof_upload_does_not_change_deposit_status(
        self, client, env
    ):
        """14. The deposit row keeps its exact financial facts."""
        req = _request(env)
        before = _row(env, req.request_id)
        assert _upload(client, req.request_id, _png()).status_code == 200
        after = _row(env, req.request_id)
        assert after["status"] == "pending"
        assert after["status"] == before["status"]
        assert after["amount_units"] == before["amount_units"]
        assert after["external_tx_id"] is None
        assert after["external_tx_id"] == before["external_tx_id"]


def _raw_ledger(db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT * FROM ledger").fetchall()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
# E. Proof lifecycle (15–18)
# ══════════════════════════════════════════════════════════════════


class TestProofLifecycle:
    def test_15_proof_becomes_pending_review(self, client, env):
        """15. The uploaded proof lands in pending_review while the
        deposit stays financially pending."""
        req = _request(env)
        assert _upload(client, req.request_id, _png()).status_code == 200

        proof = deposit_proof_store.get_pending_proof(req.request_id)
        assert proof is not None
        assert proof.status == deposit_proof_store.STATUS_PENDING_REVIEW
        assert deposit_store.get_deposit_request(req.request_id).status == (
            "pending"
        )

    def test_16_admin_sees_submitted_proof(self, client, env):
        """16. /deposits shows the pending proof to the admin and the
        view action renders the actual screenshot."""
        req = _request(env)
        assert _upload(client, req.request_id, _png()).status_code == 200
        proof = deposit_proof_store.get_pending_proof(req.request_id)
        assert proof is not None

        update = _update(ADMIN_ID, "/deposits")
        _run(deposit_proof_admin.deposits_command(update, _context()))
        text = _reply(update)
        assert LIST_HEADER in text
        assert proof.proof_id[:8] in text
        assert str(USER_A) in text
        markup = _markup_of_reply(update)
        buttons = [
            button
            for row in markup.inline_keyboard
            for button in row
        ]
        assert any(
            button.callback_data == view_callback_data(proof.proof_id)
            for button in buttons
        )

        # detail view sends the stored screenshot as a photo message
        context = _context()
        callback = _callback(
            ADMIN_ID, deposit_proof_admin.view_callback_data(proof.proof_id)
        )
        _run(deposit_proof_admin.proof_callback(callback, context))
        context.bot.send_photo.assert_awaited_once()
        kwargs = context.bot.send_photo.await_args[1]
        assert isinstance(kwargs["photo"], InputFile)
        assert str(kwargs["photo"].filename).endswith(
            f".{proof.storage_key.rsplit('.', 1)[-1]}"
        )
        assert DETAIL_HEADER in kwargs["caption"]
        assert req.request_id in kwargs["caption"]

    def test_17_rejection_records_reviewer_and_time(self, env):
        """17. A rejection persists WHO decided and WHEN (+ note)."""
        req = _request(env)
        proof = _new_proof(req.request_id)
        updated = deposit_manual_review.reject(
            proof.proof_id,
            admin_id=ADMIN_ID,
            review_note="المبلغ المُرسل لا يطابق الطلب",
            db_path=env,
        )
        assert updated.status == deposit_proof_store.STATUS_REJECTED

        row = _proof_rows(env, req.request_id)[0]
        assert row["status"] == "rejected"
        assert row["reviewed_by"] == ADMIN_ID
        assert row["reviewed_at"] is not None
        assert _TIMESTAMP_RE.fullmatch(row["reviewed_at"])
        assert row["review_note"] == "المبلغ المُرسل لا يطابق الطلب"

    def test_18_rejected_proof_never_credits(self, client, env):
        """18. Rejecting evidence changes no money and no deposit
        state — and the evidence row itself survives."""
        _fund()
        req = _request(env)
        assert _upload(client, req.request_id, _png()).status_code == 200
        proof = deposit_proof_store.get_pending_proof(req.request_id)

        deposit_manual_review.reject(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )
        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, req.request_id) == []
        assert deposit_store.get_deposit_request(req.request_id).status == (
            "pending"
        )
        assert len(_proof_rows(env, req.request_id)) == 1


# ══════════════════════════════════════════════════════════════════
# F. Manual approval (19–24)
# ══════════════════════════════════════════════════════════════════


class TestManualApproval:
    def test_19_approval_credits_exact_persisted_amount(self, env):
        """19. The credited delta IS deposit_requests.amount_units —
        nothing else exists in the approval API."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)
        assert req.amount_units == AMOUNT_UNITS

        outcome = deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )
        assert outcome.credited is True
        assert outcome.already_processed is False

        balance = _wallet_units(env)
        assert balance == (FUND + AMOUNT_UNITS, 0)
        assert balance[0] - FUND == req.amount_units
        # server-generated deterministic external id — never client input
        assert _row(env, req.request_id)["external_tx_id"] == (
            f"manual-review:{req.request_id}"
        )

    def test_20_approval_writes_exactly_one_ledger_credit(self, env):
        """20. Exactly ONE deposit ledger credit through the existing
        idempotency key."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)
        deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )

        rows = _deposit_ledger(env, req.request_id)
        assert len(rows) == 1
        entry = rows[0]
        assert entry["entry_type"] == "credit"
        assert entry["reference_type"] == "deposit"
        assert entry["reference_id"] == req.request_id
        assert entry["amount_units"] == AMOUNT_UNITS
        assert entry["available_delta"] == AMOUNT_UNITS
        assert entry["idempotency_key"] == f"deposit:{req.request_id}"

    def test_21_approval_marks_deposit_credited(self, env):
        """21. Deposit → credited; the proof record → approved."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)
        deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )

        row = _row(env, req.request_id)
        assert row["status"] == "credited"
        assert row["updated_at"] is not None
        stored = deposit_proof_store.get_proof(proof.proof_id, db_path=env)
        assert stored.status == deposit_proof_store.STATUS_APPROVED

    def test_22_approval_is_atomic(self, env):
        """22. Two concurrent approvals → exactly ONE financial
        credit, one ledger entry, one state transition."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)
        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            barrier.wait()
            try:
                result = deposit_manual_review.approve(
                    proof.proof_id, admin_id=ADMIN_ID, db_path=env
                )
                outcomes.append(("ok", result))
            except Exception as exc:  # pragma: no cover - diagnostic
                outcomes.append(("err", exc))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(outcomes) == 2, outcomes
        assert all(label == "ok" for label, _ in outcomes), outcomes
        credited = [r for _, r in outcomes if r.credited]
        replays = [r for _, r in outcomes if not r.credited]
        assert len(credited) == 1, outcomes
        assert len(replays) == 1, outcomes
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1
        assert _row(env, req.request_id)["status"] == "credited"
        stored = deposit_proof_store.get_proof(proof.proof_id, db_path=env)
        assert stored.status == deposit_proof_store.STATUS_APPROVED

    def test_23_wallet_failure_rolls_back_everything(self, env):
        """23. A failing wallet credit rolls back deposit + ledger,
        leaves the proof pending, and the approval can then converge
        on a retry (no partial state, no double credit)."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)

        def boom(*args, **kwargs):
            raise RuntimeError("wallet backend down")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(wallet, "credit_units", boom)
            with pytest.raises(RuntimeError):
                deposit_manual_review.approve(
                    proof.proof_id, admin_id=ADMIN_ID, db_path=env
                )

        assert _row(env, req.request_id)["status"] == "pending"
        assert _row(env, req.request_id)["external_tx_id"] is None
        assert _deposit_ledger(env, req.request_id) == []
        assert _wallet_units(env) == (FUND, 0)
        stored = deposit_proof_store.get_proof(proof.proof_id, db_path=env)
        assert stored.status == deposit_proof_store.STATUS_PENDING_REVIEW
        assert stored.reviewed_by is None

        # convergence: once the backend is healthy the SAME approval
        # credits exactly once.
        outcome = deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )
        assert outcome.credited is True
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1

    def test_24_ledger_failure_rolls_back_everything(self, env):
        """24. A failing ledger write rolls back wallet + deposit,
        leaves the proof pending, and retries cleanly."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)

        def boom(self, *args, **kwargs):
            raise RuntimeError("ledger backend down")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ledger.LedgerService, "record_credit", boom)
            with pytest.raises(RuntimeError):
                deposit_manual_review.approve(
                    proof.proof_id, admin_id=ADMIN_ID, db_path=env
                )

        assert _row(env, req.request_id)["status"] == "pending"
        assert _row(env, req.request_id)["external_tx_id"] is None
        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, req.request_id) == []
        stored = deposit_proof_store.get_proof(proof.proof_id, db_path=env)
        assert stored.status == deposit_proof_store.STATUS_PENDING_REVIEW

        outcome = deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )
        assert outcome.credited is True
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1


# ══════════════════════════════════════════════════════════════════
# G. Idempotency / races (25–29)
# ══════════════════════════════════════════════════════════════════


class TestIdempotency:
    def test_25_double_approval_cannot_double_credit(self, env):
        """25. A double press is an idempotent replay: zero new
        financial mutation."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)

        first = deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )
        second = deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )
        assert first.credited is True
        assert second.credited is False
        assert second.already_processed is True
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1
        assert _row(env, req.request_id)["status"] == "credited"

    def test_26_stale_callback_cannot_double_credit(self, env):
        """26. A replayed Telegram doa-callback (stale card) is
        answered as already-processed and moves no money."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)
        data = deposit_proof_admin.do_approve_callback_data(proof.proof_id)

        first = _callback(ADMIN_ID, data)
        _run(deposit_proof_admin.proof_callback(first, _context()))
        assert "تم اعتماد الدفع" in _edited(first.callback_query)
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)

        stale = _callback(ADMIN_ID, data)
        _run(deposit_proof_admin.proof_callback(stale, _context()))
        assert "تمت معالجة هذا الطلب مسبقاً" in _edited(
            stale.callback_query
        )
        assert "لم تُجرَ أي عملية مالية جديدة" in _edited(
            stale.callback_query
        )
        # exactly ONE credit ever
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1
        assert _row(env, req.request_id)["status"] == "credited"

    def test_27_already_credited_deposit_cannot_be_credited_again(
        self, env, monkeypatch
    ):
        """27. When the deposit is already credited (by another
        verification / another admin), approval is a deterministic
        no-op with zero financial mutation."""
        monkeypatch.setattr(config, "ADMINS", [ADMIN_ID, ADMIN_2])
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)

        # some earlier verification already credited this deposit
        deposit_verification.verify_and_credit(
            req.request_id,
            amount_units=req.amount_units,
            external_tx_id=TX_OK,
            facts={"source": "bsc-deposit-adapter"},
            db_path=env,
        )
        balance_after_chain = _wallet_units(env)
        assert balance_after_chain == (FUND + AMOUNT_UNITS, 0)

        # a SECOND admin pressing approve on the stale card
        outcome = deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_2, db_path=env
        )
        assert outcome.credited is False
        assert outcome.already_processed is True
        assert outcome.deposit_status == "credited"
        assert _wallet_units(env) == balance_after_chain
        assert len(_deposit_ledger(env, req.request_id)) == 1
        # the chain tx id is never overwritten by the manual path
        assert _row(env, req.request_id)["external_tx_id"] == TX_OK
        # the evidence decision is still recorded
        stored = deposit_proof_store.get_proof(proof.proof_id, db_path=env)
        assert stored.status == deposit_proof_store.STATUS_APPROVED

    def test_28_bsc_credit_first_then_manual_approval_no_double_credit(
        self, env
    ):
        """28. The automatic BSC path stays authoritative: it credits
        first, the later manual approval records evidence ONLY."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)  # user already uploaded

        # automatic BSC verification credits the deposit (MT-ADMIN-30
        # path: adapter → verify_and_credit)
        chain = deposit_verification.verify_and_credit(
            req.request_id,
            amount_units=req.amount_units,
            external_tx_id=TX_OK,
            facts={"source": "bsc-deposit-adapter"},
            db_path=env,
        )
        assert chain.status == "credited"
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1

        outcome = deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )
        assert outcome.credited is False
        assert outcome.already_processed is True
        # exactly one credit — wallet, ledger, external tx unchanged
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1
        assert _row(env, req.request_id)["external_tx_id"] == TX_OK
        # and the pending proof is resolved as approved evidence
        stored = deposit_proof_store.get_proof(proof.proof_id, db_path=env)
        assert stored.status == deposit_proof_store.STATUS_APPROVED
        assert stored.reviewed_by == ADMIN_ID

    def test_29_manual_credit_then_automatic_verification_no_double_credit(
        self, env
    ):
        """29. The inverse order: manual approval credits first, a
        later automatic verification with a DIFFERENT tx id cannot
        credit again (existing conflict rule intact)."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)

        outcome = deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )
        assert outcome.credited is True
        manual_tx = f"manual-review:{req.request_id}"
        assert _row(env, req.request_id)["external_tx_id"] == manual_tx

        with pytest.raises(deposit_verification.DepositAlreadyCreditedError):
            deposit_verification.verify_and_credit(
                req.request_id,
                amount_units=req.amount_units,
                external_tx_id=TX_OK,
                facts={"source": "bsc-deposit-adapter"},
                db_path=env,
            )
        assert _wallet_units(env) == (FUND + AMOUNT_UNITS, 0)
        assert len(_deposit_ledger(env, req.request_id)) == 1
        assert _row(env, req.request_id)["external_tx_id"] == manual_tx
        assert _row(env, req.request_id)["status"] == "credited"


# ══════════════════════════════════════════════════════════════════
# H. Authorization (30–32)
# ══════════════════════════════════════════════════════════════════


class TestAuthorization:
    def test_30_non_admin_cannot_approve(self, env):
        """30. A non-admin gets the standard refusal and can never
        move money or see queue data."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)
        assert is_admin(STRANGER) is False

        # command
        command = _update(STRANGER, "/deposits")
        _run(deposit_proof_admin.deposits_command(command, _context()))
        assert _reply(command) == MSG_ADMIN_ONLY

        # decision callbacks
        for data in (
            deposit_proof_admin.do_approve_callback_data(proof.proof_id),
            deposit_proof_admin.do_reject_callback_data(proof.proof_id),
        ):
            callback = _callback(STRANGER, data)
            _run(deposit_proof_admin.proof_callback(callback, _context()))
            assert _answered(callback.callback_query) == MSG_ADMIN_ONLY

        # zero financial mutation, proof untouched
        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, req.request_id) == []
        assert deposit_store.get_deposit_request(req.request_id).status == (
            "pending"
        )
        stored = deposit_proof_store.get_proof(proof.proof_id, db_path=env)
        assert stored.status == deposit_proof_store.STATUS_PENDING_REVIEW

    def test_31_group_and_channel_actions_blocked(self, env):
        """31. Private chat only (MT-ADMIN-02 isolation): a group/
        channel invocation produces ZERO replies and ZERO data."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)

        command = _update(
            ADMIN_ID, "/deposits", chat_type="supergroup", chat_id=-100123
        )
        _run(deposit_proof_admin.deposits_command(command, _context()))
        command.message.reply_text.assert_not_called()

        # even the ADMIN gets nothing through a group callback
        for data in (
            deposit_proof_admin.view_callback_data(proof.proof_id),
            deposit_proof_admin.do_approve_callback_data(proof.proof_id),
        ):
            callback = _callback(
                ADMIN_ID, data, chat_type="channel", chat_id=-100456
            )
            context = _context()
            _run(deposit_proof_admin.proof_callback(callback, context))
            assert _answered(callback.callback_query) is None
            callback.callback_query.edit_message_text.assert_not_called()
            context.bot.send_photo.assert_not_called()
            context.bot.send_message.assert_not_called()

        assert _wallet_units(env) == (FUND, 0)
        assert _deposit_ledger(env, req.request_id) == []
        stored = deposit_proof_store.get_proof(proof.proof_id, db_path=env)
        assert stored.status == deposit_proof_store.STATUS_PENDING_REVIEW

    def test_32_unauthorized_user_cannot_see_screenshot(self, env):
        """32. A non-admin never receives the screenshot, the detail
        card or any storage reference."""
        req = _request(env)
        proof = _new_proof(req.request_id)

        callback = _callback(
            STRANGER,
            deposit_proof_admin.view_callback_data(proof.proof_id),
        )
        context = _context()
        _run(deposit_proof_admin.proof_callback(callback, context))

        assert _answered(callback.callback_query) == MSG_ADMIN_ONLY
        context.bot.send_photo.assert_not_called()
        context.bot.send_message.assert_not_called()
        callback.callback_query.edit_message_text.assert_not_called()
        answer_text = _answered(callback.callback_query) or ""
        assert proof.storage_key not in answer_text
        assert PM_DESTINATION not in answer_text


# ══════════════════════════════════════════════════════════════════
# I. Evidence integrity (33–36)
# ══════════════════════════════════════════════════════════════════


class TestEvidenceIntegrity:
    def test_33_persisted_proof_bound_to_correct_request(self, env):
        """33. The row keeps its exact request/user/metadata binding;
        the storage key is server-shaped, never client-provided."""
        payload = _png(width=120, height=90)
        req = _request(env)
        proof = deposit_proof_store.submit_proof(
            request_id=req.request_id,
            user_id=USER_A,
            storage_key=f"{uuid.uuid4().hex}.png",
            mime_type="image/png",
            size_bytes=len(payload),
            width=120,
            height=90,
            db_path=env,
        )
        row = _proof_rows(env, req.request_id)[0]
        assert row["proof_id"] == proof.proof_id
        assert row["request_id"] == req.request_id
        assert row["user_id"] == USER_A
        assert _STORAGE_KEY_RE.fullmatch(row["storage_key"])
        assert row["mime_type"] == "image/png"
        assert row["size_bytes"] == len(payload)
        assert row["width"] == 120
        assert row["height"] == 90
        assert row["status"] == "pending_review"
        assert row["reviewed_by"] is None
        assert row["reviewed_at"] is None
        assert _TIMESTAMP_RE.fullmatch(row["created_at"])

    def test_34_rejected_evidence_is_not_deleted(self, env):
        """34. Rejection keeps BOTH the row and the image file."""
        payload = _png()
        req = _request(env)
        proof = deposit_proof_store.submit_proof(
            request_id=req.request_id,
            user_id=USER_A,
            storage_key=f"{uuid.uuid4().hex}.png",
            mime_type="image/png",
            size_bytes=len(payload),
            width=64,
            height=48,
            db_path=env,
        )
        deposit_proof_storage.save_image(
            payload,
            proof_id=proof.storage_key.rsplit(".", 1)[0],
            ext="png",
        )
        deposit_manual_review.reject(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )

        rows = _proof_rows(env, req.request_id)
        assert len(rows) == 1
        assert rows[0]["status"] == "rejected"
        stored_key = deposit_proof_store.get_proof(
            proof.proof_id, db_path=env
        ).storage_key
        assert deposit_proof_storage.read_image(stored_key) == payload
        assert stored_key in _stored_files()
        # and the store still reports the historical count
        assert (
            deposit_proof_store.count_proofs_for_request(
                req.request_id, db_path=env
            )
            == 1
        )

    def test_35_reviewer_identity_persisted(self, env):
        """35. Both decisions record the deciding admin id."""
        req_a = _request(env)
        proof_a = _new_proof(req_a.request_id)
        deposit_manual_review.reject(
            proof_a.proof_id, admin_id=ADMIN_ID, db_path=env
        )
        assert (
            _proof_rows(env, req_a.request_id)[0]["reviewed_by"] == ADMIN_ID
        )

        _fund()
        req_b = _request(env)
        proof_b = _new_proof(req_b.request_id)
        deposit_manual_review.approve(
            proof_b.proof_id, admin_id=ADMIN_ID, db_path=env
        )
        assert (
            _proof_rows(env, req_b.request_id)[0]["reviewed_by"] == ADMIN_ID
        )

    def test_36_review_timestamp_persisted(self, env):
        """36. reviewed_at is a real persisted timestamp at/after the
        upload time."""
        req = _request(env)
        proof = _new_proof(req.request_id)
        created_at = _proof_rows(env, req.request_id)[0]["created_at"]

        deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )
        row = _proof_rows(env, req.request_id)[0]
        assert row["reviewed_at"] is not None
        assert _TIMESTAMP_RE.fullmatch(row["reviewed_at"])
        assert row["reviewed_at"] >= created_at


# ══════════════════════════════════════════════════════════════════
# J. Extras — edge cases discovered during implementation (37–48)
# ══════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_37_repeat_upload_while_pending_is_stable_409(self, client, env):
        """37. ONE active proof per request: a repeated upload is a
        deterministic business error, never ambiguous evidence."""
        req = _request(env)
        first = _upload(client, req.request_id, _png())
        assert first.status_code == 200
        first_proof_id = first.get_json()["proof"]["proof_id"]

        for payload in (_png(), _jpeg(), _gif()):
            again = _upload(client, req.request_id, payload)
            assert again.status_code == 409
            assert again.get_json()["error"] == "proof_pending_review"

        rows = _proof_rows(env, req.request_id)
        assert len(rows) == 1
        assert rows[0]["proof_id"] == first_proof_id
        assert rows[0]["status"] == "pending_review"

    def test_38_new_proof_allowed_after_rejection(self, client, env):
        """38. After a rejection the user may re-submit; the rejected
        evidence row is kept as-is."""
        req = _request(env)
        first = _upload(client, req.request_id, _png())
        first_proof_id = first.get_json()["proof"]["proof_id"]
        deposit_manual_review.reject(
            first_proof_id, admin_id=ADMIN_ID, db_path=env
        )

        second = _upload(client, req.request_id, _gif(), filename="x.gif")
        assert second.status_code == 200
        second_proof_id = second.get_json()["proof"]["proof_id"]
        assert second_proof_id != first_proof_id

        rows = {
            row["proof_id"]: row
            for row in _proof_rows(env, req.request_id)
        }
        assert len(rows) == 2
        assert rows[first_proof_id]["status"] == "rejected"
        assert rows[first_proof_id]["reviewed_by"] == ADMIN_ID
        assert rows[second_proof_id]["status"] == "pending_review"
        assert rows[second_proof_id]["reviewed_by"] is None
        # financial state untouched by the whole cycle
        assert deposit_store.get_deposit_request(req.request_id).status == (
            "pending"
        )

    def test_39_upload_for_terminal_deposit_rejected(self, client, env):
        """39. A credited deposit is terminal for evidence too."""
        _fund()
        req = _request(env)
        proof = _new_proof(req.request_id)
        deposit_manual_review.approve(
            proof.proof_id, admin_id=ADMIN_ID, db_path=env
        )

        response = _upload(client, req.request_id, _png())
        assert response.status_code == 409
        assert response.get_json()["error"] == "request_processed"
        assert len(_proof_rows(env, req.request_id)) == 1

    def test_40_upload_response_never_exposes_storage_metadata(
        self, client, env
    ):
        """40. The user response carries only the safe proof summary —
        no storage key, no path, no internal reference."""
        req = _request(env)
        response = _upload(client, req.request_id, _png())
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        body = response.get_json()
        assert body["message"] == SUBMITTED_MSG
        assert set(body["proof"]) == {
            "proof_id",
            "request_id",
            "status",
            "created_at",
        }
        assert (
            deposit_routes._MSG_PROOF_SUBMITTED == SUBMITTED_MSG
        )
        storage_key = deposit_proof_store.get_pending_proof(
            req.request_id
        ).storage_key
        assert storage_key not in text
        for forbidden in (
            "storage",
            "deposit_proof_uploads",
            PM_DESTINATION,
            "Traceback",
            "sqlite",
        ):
            assert forbidden not in text, forbidden

    def test_41_detail_card_shows_required_facts_and_no_secrets(self, env):
        """41. The admin card exposes the full review context from
        persisted facts — and never pm_destination or the key."""
        req = _request(env)
        proof = _new_proof(req.request_id)
        deposit = deposit_store.get_deposit_request(req.request_id)

        text = deposit_proof_admin.build_detail_text(proof, deposit)
        # required review facts
        assert req.request_id in text
        assert str(USER_A) in text
        assert AMOUNT in text          # requested amount
        assert "USDT" in text          # asset
        assert "BEP20" in text         # network
        assert deposit.pm_display_name in text  # payment method
        assert proof.proof_id[:8] in text
        # proof state + financial state are distinct
        assert "⏳ بانتظار المراجعة" in text
        assert "⏳ قيد الانتظار" in text
        # secrets / admin metadata never rendered
        assert PM_DESTINATION not in text
        assert proof.storage_key not in text
        for forbidden in (
            "deposit_proof_uploads",
            "created_by",
            "updated_by",
            "sort_order",
            "Traceback",
            "sqlite",
        ):
            assert forbidden not in text, forbidden
        # the same for the queue text
        queue_text, markup = deposit_proof_admin.build_list_page([proof], 1)
        assert LIST_HEADER in queue_text
        assert proof.storage_key not in queue_text
        assert PM_DESTINATION not in queue_text
        assert isinstance(markup, InlineKeyboardMarkup)

    def test_42_bot_registers_deposits_command_and_dp_callback(self):
        """42. bot.py wires /deposits (group 0) and the dp: callback
        family (group 5) — same conventions as /withdrawals + wd:."""
        with open("bot.py", encoding="utf-8") as handle:
            source = handle.read()
        assert "import deposit_proof_admin" in source
        assert '"deposits", deposit_proof_admin.deposits_command' in source
        assert (
            "deposit_proof_admin.proof_callback, pattern=r\"^dp:\""
            in source
        )
        # payload contract: lookup pointers only
        assert deposit_proof_admin.parse_callback(
            deposit_proof_admin.do_approve_callback_data("a" * 32)
        ) == ("doa", "a" * 32)
        assert deposit_proof_admin.parse_callback("dp:doa:") is None
        assert deposit_proof_admin.parse_callback("dp:zzz:x") is None
        assert deposit_proof_admin.parse_callback("wd:page:1") is None
        assert deposit_proof_admin.parse_callback(
            "dp:page:0"
        ) is None

    def test_43_miniapp_proof_ui_only_claims_review_pending(self):
        """43. The proof UI submits ONLY request_id + file, and its
        only success copy is the review-pending claim."""
        deposit = _read_js("deposit.js")
        assert "const PROOF_URL = '/api/deposit/proof'" in deposit
        for testid in (
            "deposit-proof-section",
            "deposit-proof-file",
            "deposit-proof-error",
            "deposit-proof-submit",
            "deposit-proof-sent",
        ):
            assert f'data-testid="{testid}"' in deposit
        assert SUBMITTED_MSG in deposit
        assert "accept=\"image/png,image/jpeg,image/gif\"" in deposit
        for claim in FORBIDDEN_JS_CLAIMS:
            assert claim not in deposit, claim
        assert "تم استلام الأموال" not in deposit
        assert "تم اعتماد الدفع" not in deposit

        # the upload body: exactly request_id + file, nothing else
        body = deposit[
            deposit.index("async function _uploadProof") :
            deposit.index("function _renderProofSent")
        ]
        appended = re.findall(r"form\.append\('([^']+)'", body)
        assert appended == ["request_id", "file"]
        # initData header only — no JSON content type on the upload
        assert "INIT_DATA_HEADER] = _initData()" in body
        assert "Content-Type" not in body

    def test_44_upload_route_cannot_import_financial_boundary(self):
        """44. deposit_routes (the public HTTP surface) never imports
        the credit boundary, the ledger or the chain adapter — the
        upload route structurally cannot credit."""
        with open("deposit_routes.py", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(
                    alias.name.split(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {
            "deposit_verification",
            "ledger",
            "bsc_deposit_adapter",
            "deposit_manual_review",
        }
        assert not (imported & forbidden), imported & forbidden
        assert "deposit_verification" not in source
        assert "verify_and_credit" not in source
        # the string guard the MT-ADMIN-29 contract already pins
        for path in ("deposit_routes.py", "serve_miniapp.py", "bot.py"):
            with open(path, encoding="utf-8") as handle:
                assert "deposit_verification" not in handle.read(), path

    def test_45_storage_keys_never_escape_evidence_root(self, env):
        """45. A corrupted/malicious storage key can never read or
        delete outside the storage root."""
        for bad in (
            "../secret.png",
            "..\\secret.png",
            "/etc/passwd",
            "sub/dir.png",
            "A" * 32 + ".PNG",
            "a" * 31 + ".png",
            "a" * 32 + ".exe",
            "a" * 32 + ".png.exe",
            "",
            None,
            123,
        ):
            with pytest.raises(
                deposit_proof_storage.InvalidStorageKeyError
            ):
                deposit_proof_storage.read_image(bad)
            with pytest.raises(
                deposit_proof_storage.InvalidStorageKeyError
            ):
                deposit_proof_storage.delete_image(bad)
        # a well-shaped but missing file is a plain miss, not a leak
        with pytest.raises(FileNotFoundError):
            deposit_proof_storage.read_image("f" * 32 + ".png")

    def test_46_image_dimension_bound_enforced_end_to_end(
        self, client, env
    ):
        """46. Decompression-bomb dimensions are refused (bounded
        evidence) before anything persists."""
        req = _request(env)
        response = _upload(
            client,
            req.request_id,
            _png(
                width=deposit_proof_storage.MAX_IMAGE_DIMENSION + 1,
                height=10,
            ),
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "file_too_large"
        assert _proof_rows(env, req.request_id) == []
        assert _stored_files() == []

    def test_47_stable_error_codes_and_amount_free_approval_api(self):
        """47. Machine codes are stable and the approval API cannot
        possibly carry an amount, a tx id or verification facts."""
        expected = {
            deposit_proof_store.ProofRequestNotFoundError: "request_not_found",
            deposit_proof_store.ProofOwnershipError: "request_forbidden",
            deposit_proof_store.ProofDepositNotPendingError: (
                "request_processed"
            ),
            deposit_proof_store.ProofAlreadyPendingError: (
                "proof_pending_review"
            ),
            deposit_proof_store.ProofNotFoundError: "proof_not_found",
            deposit_proof_store.ProofStateError: "proof_state_changed",
            deposit_proof_store.ProofValidationError: (
                "invalid_proof_request"
            ),
            deposit_proof_storage.UnsupportedImageTypeError: (
                "unsupported_image_type"
            ),
            deposit_proof_storage.ImageTooLargeError: "file_too_large",
            deposit_proof_storage.MalformedImageError: "invalid_upload",
            deposit_manual_review.ProofReviewNotFoundError: (
                "proof_not_found"
            ),
            deposit_manual_review.ProofReviewConflictError: (
                "proof_state_changed"
            ),
        }
        for exc_type, code in expected.items():
            assert exc_type.code == code, exc_type

        approve_params = inspect.signature(
            deposit_manual_review.approve
        ).parameters
        assert set(approve_params) == {"proof_id", "admin_id", "db_path"}
        reject_params = inspect.signature(
            deposit_manual_review.reject
        ).parameters
        assert set(reject_params) == {
            "proof_id",
            "admin_id",
            "review_note",
            "db_path",
        }
        # deterministic server-side external id prefix only
        assert deposit_manual_review.MANUAL_TX_ID_PREFIX == "manual-review:"
        # the proof review lifecycle never gains a financial state
        assert set(deposit_proof_store.REVIEW_STATUSES) == {
            "pending_review",
            "approved",
            "rejected",
        }
        assert set(deposit_proof_store.REVIEW_STATUSES).isdisjoint(
            {"credited", "credited_by_screenshot"}
        )

    def test_48_concurrent_duplicate_submission_single_pending(self, env):
        """48. The partial UNIQUE index is the race backstop: exactly
        one pending proof ever exists per request."""
        req = _request(env)
        barrier = threading.Barrier(2)
        results = []

        def worker(suffix: str):
            barrier.wait()
            try:
                proof = deposit_proof_store.submit_proof(
                    request_id=req.request_id,
                    user_id=USER_A,
                    storage_key=f"{uuid.uuid4().hex}.png",
                    mime_type="image/png",
                    size_bytes=100 + len(suffix),
                    width=16,
                    height=16,
                    db_path=env,
                )
                results.append(("ok", proof))
            except deposit_proof_store.ProofAlreadyPendingError as exc:
                results.append(("dup", exc))
            except Exception as exc:  # pragma: no cover - diagnostic
                results.append(("err", exc))

        threads = [
            threading.Thread(target=worker, args=("a",)),
            threading.Thread(target=worker, args=("b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(results) == 2, results
        assert sorted(label for label, _ in results) == ["dup", "ok"], results
        pending = deposit_proof_store.list_pending_proofs(db_path=env)
        assert len(pending) == 1
        assert pending[0].request_id == req.request_id
        assert len(_proof_rows(env, req.request_id)) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
