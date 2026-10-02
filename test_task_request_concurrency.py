"""
Concurrency & Security Review — «إضافة مهمة ➕» workflow
========================================================

Adversarial tests for the three surfaces of the user task-request
feature: the store/CAS, the Telegram admin review surface, and the
HTTP API.

Concurrency
- ``_approve`` must NEVER hold the ``BEGIN IMMEDIATE`` write lock
  across a Telegram network await: the loser branch used to answer
  the callback inside ``with db.transaction()``, blocking every
  other writer for the duration of the round-trip (up to
  ``BUSY_TIMEOUT_MS`` → SQLITE_BUSY → HTTP 500). Proven by timing a
  concurrent writer while the answer await is deliberately slow.
- static guard: no ``await`` between the transaction open and its
  exit.
- threaded races through the REAL code paths: concurrent approves
  create exactly ONE task and charge once; concurrent resubmits and
  concurrent return/reject each have exactly ONE winner; history
  records exactly one decision event.

Security
- SQL injection payloads round-trip as inert text (parameterized
  statements; tables intact).
- admin prompt state is per-admin: another admin's text can never
  consume or apply someone else's pending prompt.

Run:
    python3 -m pytest test_task_request_concurrency.py -v
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import textwrap
import time
from unittest import mock

import pytest

import config
import db
import task_request_admin as admin
import task_request_store as store
import wallet

from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data


ADMIN_A = 5101
ADMIN_B = 5102
OWNER = 4101
STRANGER = 9999

USDT = wallet.USDT_SCALE
SEED = 100 * USDT
REWARD_UNITS = 50_000_000
COMMISSION_UNITS = 15_000_000
CHARGE = REWARD_UNITS + COMMISSION_UNITS

VALID_PAYLOAD = {
    "title": "متابعة حسابي على Instagram",
    "description": "تابع الحساب ثم أرسل إثبات المتابعة",
    "provider": "instagram",
    "action": "follow",
    "target_ref": "https://instagram.com/example",
    "reward": "0.5",
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    db_path = str(tmp_path / "task_request_concurrency.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(OWNER, "owner", "Owner")
    monkeypatch.setattr(config, "ADMINS", [ADMIN_A, ADMIN_B])
    yield db_path


def _create(**overrides) -> store.TaskRequest:
    payload = dict(VALID_PAYLOAD)
    payload.update(overrides)
    return store.create_request(OWNER, payload)


def _ctx() -> mock.MagicMock:
    context = mock.MagicMock()
    context.user_data = {}
    return context


def _make_query(data: str, answer_delay: float = 0.0):
    """Callback query stand-in; optionally slow ``answer()`` to model
    a Telegram Bot API round-trip."""
    query = mock.MagicMock()
    query.data = data

    async def _answer(text=None, *args, **kwargs):
        if answer_delay:
            await asyncio.sleep(answer_delay)

    query.answer = _answer
    query.edit_message_text = mock.AsyncMock()
    return query


def _make_update(query, actor=ADMIN_A, chat_type="private"):
    update = mock.MagicMock()
    update.callback_query = query
    update.effective_chat.type = chat_type
    update.effective_user.id = actor
    return update


def _row_count(sql: str, params: tuple = ()) -> int:
    with db.get_connection() as conn:
        return int(conn.execute(sql, params).fetchone()["n"])


def _wallet_units(user_id=OWNER) -> int:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT available_units FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row["available_units"]) if row else 0


# ════════════════════════════════════════════════════════════════════
# 1. The write transaction must be released BEFORE any network await
# ════════════════════════════════════════════════════════════════════


class TestTransactionNeverSpansAnAwait:
    def test_write_lock_released_before_telegram_answer(self, env):
        """While ``_approve`` awaits the callback answer on the loser
        branch, another writer must NOT be blocked.

        Reproduces the lost-CAS-race branch: the request is pending,
        the claim fails inside the transaction, and the STALE answer
        is sent — the answer await must happen AFTER the transaction
        exits, never while BEGIN IMMEDIATE holds the write lock.
        """
        request = _create()
        claim_entered = threading.Event()

        def losing_claim(conn, request_id, admin_id):
            # Simulates losing the race: pending row, no winner.
            claim_entered.set()
            return None

        failures: list[BaseException] = []

        def press() -> None:
            try:
                query = _make_query(
                    f"treq:approve:{request.request_id}",
                    answer_delay=0.6,   # a deliberately slow round-trip
                )
                update = _make_update(query)
                with mock.patch.object(
                    store, "claim_for_approval", losing_claim
                ):
                    asyncio.run(
                        admin.task_request_callback(update, _ctx())
                    )
            except BaseException as exc:  # noqa: BLE001 — surfaced below
                failures.append(exc)

        thread = threading.Thread(target=press)
        thread.start()
        assert claim_entered.wait(timeout=5), "approve never reached the txn"
        # Small head start so the press is inside its await (or its
        # commit path) while we attempt an unrelated write.
        time.sleep(0.05)

        started = time.monotonic()
        store.create_request(OWNER, VALID_PAYLOAD)   # concurrent writer
        elapsed = time.monotonic() - started

        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not failures, failures

        assert elapsed < 0.25, (
            f"concurrent writer blocked {elapsed:.2f}s — "
            "BEGIN IMMEDIATE was held across the Telegram answer await"
        )
        # Both requests exist; the approve attempt changed nothing.
        assert _row_count(
            "SELECT COUNT(*) AS n FROM user_task_requests"
        ) == 2
        assert store.get_request(request.request_id).status == \
            store.STATUS_PENDING
        assert _row_count("SELECT COUNT(*) AS n FROM tasks") == 0

    def test_approve_source_has_no_await_inside_transaction(self):
        """Static regression guard: no ``await`` between the
        transaction open and the first ``except`` of ``_approve``."""
        src = textwrap.dedent(inspect.getsource(admin._approve))
        lines = src.splitlines()
        start = next(
            i for i, line in enumerate(lines)
            if "with db.transaction()" in line
        )
        end = next(
            i for i, line in enumerate(lines[start + 1:], start + 1)
            if line.startswith("    except")
        )
        offenders = [
            line for line in lines[start + 1:end]
            if line.strip().startswith("await")
        ]
        assert offenders == [], (
            "await inside the write transaction:\n"
            + "\n".join(offenders)
        )


# ════════════════════════════════════════════════════════════════════
# 2. Threaded races through the REAL code paths
# ════════════════════════════════════════════════════════════════════


class TestThreadedRaces:
    def test_concurrent_approves_create_exactly_one_task(self, env):
        request = _create()
        wallet.credit_units(OWNER, SEED)
        errors: list[BaseException] = []

        def press() -> None:
            try:
                query = _make_query(
                    f"treq:approve:{request.request_id}"
                )
                asyncio.run(
                    admin.task_request_callback(
                        _make_update(query), _ctx()
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=press) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive()

        assert errors == [], errors
        assert _row_count("SELECT COUNT(*) AS n FROM tasks") == 1
        assert _row_count(
            "SELECT COUNT(*) AS n FROM task_funding"
        ) == 1
        assert _wallet_units() == SEED - CHARGE          # charged ONCE
        stored = store.get_request(request.request_id)
        assert stored.status == store.STATUS_APPROVED
        assert isinstance(stored.published_task_id, int)
        # history holds exactly one approval event
        assert [e["event"] for e in stored.history].count(
            "approved"
        ) == 1

    def test_concurrent_resubmits_have_exactly_one_winner(self, env):
        request = _create()
        store.admin_return_request(request.request_id, ADMIN_A, "عدّل")
        barrier = threading.Barrier(4)
        results: list[str] = []
        lock = threading.Lock()

        def attempt() -> None:
            try:
                barrier.wait(timeout=5)
                store.resubmit_request(
                    request.request_id, OWNER, VALID_PAYLOAD
                )
                outcome = "won"
            except store.TaskRequestError:
                outcome = "lost"
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive()

        assert results.count("won") == 1, results
        assert results.count("lost") == 3, results
        stored = store.get_request(request.request_id)
        assert stored.status == store.STATUS_PENDING
        assert stored.decision_reason is None
        assert [e["event"] for e in stored.history].count(
            "resubmitted"
        ) == 1      # no double-append under contention

    def test_concurrent_return_vs_reject_single_winner(self, env):
        request = _create()
        barrier = threading.Barrier(2)
        results: list[str] = []
        lock = threading.Lock()

        def run(op: str) -> None:
            try:
                barrier.wait(timeout=5)
                if op == "return":
                    store.admin_return_request(
                        request.request_id, ADMIN_A, "ملاحظة"
                    )
                else:
                    store.admin_reject_request(
                        request.request_id, ADMIN_A, "سبب"
                    )
                outcome = "won"
            except store.TaskRequestError:
                outcome = "lost"
            with lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=run, args=("return",)),
            threading.Thread(target=run, args=("reject",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive()

        assert results.count("won") == 1, results
        assert results.count("lost") == 1, results
        stored = store.get_request(request.request_id)
        assert stored.status in (
            store.STATUS_CHANGES_REQUESTED, store.STATUS_REJECTED
        )
        # exactly ONE decision event in the audit trail
        decision_events = [
            e["event"] for e in stored.history
            if e["event"] in ("returned", "rejected")
        ]
        assert len(decision_events) == 1, stored.history

    def test_concurrent_admin_edit_vs_approve(self, env):
        """A field edit racing an approve: either the edit lands and
        the task is published with it, or the approve wins first and
        the edit is refused — never a half-applied state."""
        request = _create()
        wallet.credit_units(OWNER, SEED)
        barrier = threading.Barrier(2)
        outcomes: dict[str, str] = {}
        lock = threading.Lock()

        def do_edit() -> None:
            try:
                barrier.wait(timeout=5)
                store.admin_edit_field(
                    request.request_id, ADMIN_A, "reward", "1.25"
                )
                result = "won"
            except store.TaskRequestError:
                result = "lost"
            with lock:
                outcomes["edit"] = result

        def do_approve() -> None:
            try:
                barrier.wait(timeout=5)
                query = _make_query(
                    f"treq:approve:{request.request_id}"
                )
                asyncio.run(
                    admin.task_request_callback(
                        _make_update(query), _ctx()
                    )
                )
                result = "done"
            except BaseException as exc:  # noqa: BLE001
                result = f"error: {exc!r}"
            with lock:
                outcomes["approve"] = result

        threads = [
            threading.Thread(target=do_edit),
            threading.Thread(target=do_approve),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive()

        assert outcomes.get("approve") == "done", outcomes
        assert outcomes.get("edit") in ("won", "lost"), outcomes
        stored = store.get_request(request.request_id)
        assert stored.status == store.STATUS_APPROVED
        task = db.get_task(stored.published_task_id)
        # The published task's reward matches the STORED payload at
        # approval time — never a mix of the two racing values.
        if outcomes["edit"] == "won":
            assert stored.payload["reward_units"] == 125_000_000
            assert task["reward_units"] == 125_000_000
        else:
            assert stored.payload["reward_units"] == REWARD_UNITS
            assert task["reward_units"] == REWARD_UNITS
        assert _row_count("SELECT COUNT(*) AS n FROM tasks") == 1


# ════════════════════════════════════════════════════════════════════
# 3. Security probes
# ════════════════════════════════════════════════════════════════════


class TestSecurity:
    def test_sql_injection_payload_is_inert_text(self, env):
        """Hostile payloads round-trip as data, never as SQL."""
        hostile = {
            "title": "'; DROP TABLE users; --",
            "description": 'x" OR 1=1 -- <script>alert(1)</script>',
            "target_ref": "' UNION SELECT * FROM wallets --",
        }
        request = _create(**hostile)
        stored = store.get_request(request.request_id)
        assert stored.payload["title"] == hostile["title"]
        assert stored.payload["description"] == hostile["description"]
        assert stored.payload["target_ref"] == hostile["target_ref"]
        # every table still exists and reads normally
        assert db.get_user(OWNER) is not None
        assert _row_count(
            "SELECT COUNT(*) AS n FROM user_task_requests"
        ) == 1
        # and the hostile title flows through the admin detail card
        # as plain text (no parse_mode anywhere in the module)
        text = admin.build_detail_text(stored)
        assert hostile["title"] in text

    def test_admin_module_never_uses_parse_mode_markup(self):
        """Arabic answers are plain text — no HTML/Markdown injection
        surface through titles or reasons."""
        source = inspect.getsource(admin)
        assert "parse_mode=" not in source
        assert "Markdown" not in source
        assert "<b>" not in source

    def test_cross_admin_prompt_state_isolated(self, env):
        """Admin A's pending prompt can never be consumed or applied
        by admin B's text (context.user_data is per-user)."""
        request = _create()
        ctx_a = _ctx()
        ctx_b = _ctx()

        query, _ = _press_for_test(
            f"treq:reject:{request.request_id}", ctx_a
        )
        assert admin.INPUT_STATE_KEY in ctx_a.user_data

        # Admin B types something — B has no state → silent.
        reply_b = _send_text_for_test("سبب من B", ctx_b, actor=ADMIN_B)
        reply_b.assert_not_awaited()
        # A's prompt is untouched and the request is still pending.
        assert admin.INPUT_STATE_KEY in ctx_a.user_data
        assert store.get_request(request.request_id).status == \
            store.STATUS_PENDING

        # Non-admin text is silent too, state intact.
        reply_s = _send_text_for_test("سبب", ctx_a, actor=STRANGER)
        reply_s.assert_not_awaited()
        assert admin.INPUT_STATE_KEY in ctx_a.user_data

        # Only A's own text applies the rejection.
        reply_a = _send_text_for_test("سبب من A", ctx_a, actor=ADMIN_A)
        assert reply_a.await_count == 1
        assert store.get_request(request.request_id).status == \
            store.STATUS_REJECTED

    def test_admin_user_id_never_trusted_from_payload(self, env):
        """The HTTP create path rejects identity fields outright —
        re-verified here through the store contract."""
        with pytest.raises(store.TaskRequestError):
            store.create_request(OWNER, _payload_with_user_id())


def _press_for_test(data, context):
    query = _make_query(data)
    asyncio.run(admin.task_request_callback(_make_update(query), context))
    return query, context


def _send_text_for_test(text, context, actor=ADMIN_A,
                        chat_type="private"):
    update = mock.MagicMock()
    update.effective_chat.type = chat_type
    update.effective_user.id = actor
    update.message.text = text
    update.message.reply_text = mock.AsyncMock()
    asyncio.run(admin.task_request_text_input(update, context))
    return update.message.reply_text


def _payload_with_user_id() -> dict:
    payload = dict(VALID_PAYLOAD)
    payload["user_id"] = STRANGER
    return payload
