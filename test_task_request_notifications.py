"""
Task Request Admin Notifications («إضافة مهمة ➕» → ADMINS push)
================================================================

Contract of ``task_request_notifications``:

  Creation / resubmit
  - a valid create → request stored ``pending`` + exactly ONE admin
    notification (request id, owner, title, description, provider ·
    action, reward, status ⏳ قيد المراجعة)
  - the inline button is a VALID ``treq:`` callback of the EXISTING
    review path (no new decision surface)
  - a replay/retry of the same pending cycle never double-sends
  - resubmit back to ``pending`` → exactly ONE more notification
  - GET/reload never notifies

  Fail-soft (the request row must survive every notification failure)
  - notifier raising        → request still created, error logged
  - scheduler raising       → request still created, error logged
  - ``config.ADMINS`` empty → request still created, warning logged
  - bridge not bound        → request still created, warning logged

  No notification on rejected paths
  - validation failure, rate limit (429), unauthenticated request

Run:
    python3 -m pytest test_task_request_notifications.py -v
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import db
import serve_miniapp
import task_request_notifications as trn
import task_request_rate_limit as rate_limit
import task_request_store
from admin_notification_store import AdminNotificationStore
from task_request_admin import parse_callback
from task_taxonomy import ACTION_LABELS, PROVIDER_LABELS

from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

USER_A = 4101
ADMIN_CHAT = 999001
RETURN_ADMIN = 999002

VALID_PAYLOAD = {
    "title": "متابعة حسابي على Instagram",
    "description": "تابع الحساب ثم أرسل إثبات المتابعة",
    "provider": "instagram",
    "action": "follow",
    "target_ref": "https://instagram.com/example",
    "reward": "0.5",
}


# ── Test doubles (AdminNotifier-shaped, no network) ───────────────────


class FakeNotifier:
    """Records ``notify_system`` calls like the real AdminNotifier."""

    def __init__(self, admin_ids=(ADMIN_CHAT,), fail: bool = False):
        self._admin_ids = list(admin_ids)
        self.fail = fail
        self.sent: list[dict] = []

    @property
    def admin_ids(self):
        return tuple(self._admin_ids)

    async def notify_system(self, text, *, reply_markup=None, targets=None):
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.sent.append({"text": text, "reply_markup": reply_markup})
        return [
            (chat_id, 5000 + len(self.sent)) for chat_id in self._admin_ids
        ]


def _sync_scheduler(coro):
    """Run the delivery coroutine inline — deterministic in tests."""
    asyncio.run(coro)
    return None


def _broken_scheduler(coro):
    coro.close()
    raise RuntimeError("event loop down")


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated DB + registered user + clean notification bridge."""
    trn.unbind()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "task_request_notify.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_A, "alice", "Alice")
    rate_limit.reset()
    yield db_path
    trn.unbind()
    rate_limit.reset()


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


@pytest.fixture
def notifier(env):
    """Default bound notifier (created AFTER env so binding survives)."""
    fake = FakeNotifier()
    trn.bind(fake, _sync_scheduler)
    yield fake
    trn.unbind()


# ── Helpers ───────────────────────────────────────────────────────────


def _auth(user_id: int = USER_A) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


def _payload(**overrides) -> dict:
    payload = dict(VALID_PAYLOAD)
    payload.update(overrides)
    return payload


def _create(client, **overrides) -> dict:
    response = client.post(
        "/api/task-requests", headers=_auth(), json=_payload(**overrides)
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["request"]


def _linkages(operation_id: int) -> list:
    return AdminNotificationStore.list_for_operation(
        trn.OPERATION_TASK_REQUEST, operation_id
    )


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name == "task_request_notifications"
    ]


def _errors(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.ERROR
        and r.name == "task_request_notifications"
    ]


# ════════════════════════════════════════════════════════════════════
# 1–3. Creation → pending + ONE notification with the right content
# ════════════════════════════════════════════════════════════════════


class TestCreationNotification:
    def test_valid_create_is_pending_and_notifies_once(self, client, notifier):
        request = _create(client)
        assert request["status"] == "pending"

        stored = task_request_store.get_request(request["request_id"])
        assert stored is not None
        assert stored.status == task_request_store.STATUS_PENDING

        assert len(notifier.sent) == 1
        assert len(_linkages(trn.operation_id_for(stored))) == 1

    def test_notification_contains_request_id_and_core_fields(
        self, client, notifier
    ):
        request = _create(client)
        rid = request["request_id"]
        text = notifier.sent[0]["text"]

        assert f"رقم الطلب: #{rid}" in text
        assert VALID_PAYLOAD["title"] in text
        assert VALID_PAYLOAD["description"] in text
        assert PROVIDER_LABELS["instagram"] in text
        assert ACTION_LABELS["follow"] in text
        assert "المكافأة" in text and "USDT" in text
        assert "⏳ قيد المراجعة" in text
        # owner: verified user id + username from the users table
        assert str(USER_A) in text
        assert "@alice" in text

    def test_notification_has_valid_review_button(self, client, notifier):
        request = _create(client)
        rid = request["request_id"]
        markup = notifier.sent[0]["reply_markup"]
        flat = [btn for row in markup.inline_keyboard for btn in row]
        assert flat, "notification must carry inline buttons"

        # Primary button → the EXISTING review detail screen.
        parsed = parse_callback(flat[0].callback_data)
        assert parsed == ("view", rid, None)
        assert f"#{rid}" in flat[0].text
        # Secondary button → the EXISTING pending queue.
        assert any(
            parse_callback(btn.callback_data) == ("list", None, None)
            for btn in flat
        )


# ════════════════════════════════════════════════════════════════════
# 4–5. Idempotency + resubmit cycle
# ════════════════════════════════════════════════════════════════════


class TestIdempotencyAndResubmit:
    def test_replayed_notification_is_not_sent_twice(self, client, notifier):
        request = _create(client)
        rid = request["request_id"]
        assert len(notifier.sent) == 1

        stored = task_request_store.get_request(rid)
        trn.notify_pending(stored)  # retry / replay
        trn.notify_pending(stored)

        assert len(notifier.sent) == 1
        assert len(_linkages(trn.operation_id_for(stored))) == 1

    def test_resubmit_to_pending_notifies_once_more(self, client, notifier):
        request = _create(client)
        rid = request["request_id"]
        assert len(notifier.sent) == 1
        assert trn.HEADER_NEW in notifier.sent[0]["text"]

        # Admin returns it for changes → user edits → resubmits.
        task_request_store.admin_return_request(
            rid, RETURN_ADMIN, "أضف الرابط كاملًا"
        )
        response = client.patch(
            f"/api/task-requests/{rid}",
            headers=_auth(),
            json=_payload(title="عنوان معدل"),
        )
        assert response.status_code == 200, response.get_data(as_text=True)

        assert len(notifier.sent) == 2
        assert trn.HEADER_RESUBMIT in notifier.sent[1]["text"]
        stored = task_request_store.get_request(rid)
        assert stored.status == task_request_store.STATUS_PENDING
        # A replay of the NEW cycle still sends nothing extra.
        trn.notify_pending(stored)
        assert len(notifier.sent) == 2
        # Both cycles keep their own linkage.
        assert len(_linkages(trn.operation_id_for(stored))) == 1
        first_cycle = rid * 1000 + 1
        assert len(_linkages(first_cycle)) == 1

    def test_reload_get_sends_no_notification(self, client, notifier):
        _create(client)
        assert len(notifier.sent) == 1
        assert client.get("/api/task-requests", headers=_auth()).status_code == 200
        assert client.get("/api/task-requests", headers=_auth()).status_code == 200
        assert len(notifier.sent) == 1


# ════════════════════════════════════════════════════════════════════
# 6–8. Fail-soft: creation always survives
# ════════════════════════════════════════════════════════════════════


class TestFailSoft:
    def test_notifier_failure_does_not_fail_creation(self, env, client, caplog):
        broken = FakeNotifier(fail=True)
        trn.bind(broken, _sync_scheduler)
        with caplog.at_level(
            logging.ERROR, logger="task_request_notifications"
        ):
            request = _create(client)

        assert request["status"] == "pending"
        stored = task_request_store.get_request(request["request_id"])
        assert stored is not None
        assert stored.status == task_request_store.STATUS_PENDING
        assert broken.sent == []
        errors = _errors(caplog)
        assert any(
            "delivery failed" in msg
            and str(request["request_id"]) in msg
            for msg in errors
        ), errors

    def test_scheduler_failure_does_not_fail_creation(self, env, client, caplog):
        fake = FakeNotifier()
        trn.bind(fake, _broken_scheduler)
        with caplog.at_level(
            logging.ERROR, logger="task_request_notifications"
        ):
            request = _create(client)

        assert request["status"] == "pending"
        assert task_request_store.get_request(request["request_id"]) is not None
        assert fake.sent == []
        assert any(
            "Failed to schedule" in msg for msg in _errors(caplog)
        ), _errors(caplog)

    def test_empty_admins_creates_with_warning(self, env, client, caplog):
        empty = FakeNotifier(admin_ids=())
        trn.bind(empty, _sync_scheduler)
        with caplog.at_level(
            logging.WARNING, logger="task_request_notifications"
        ):
            request = _create(client)

        assert request["status"] == "pending"
        assert task_request_store.get_request(request["request_id"]) is not None
        assert empty.sent == []
        warnings = _warnings(caplog)
        assert any("config.ADMINS is empty" in msg for msg in warnings), warnings
        assert any(
            str(request["request_id"]) in msg for msg in warnings
        ), warnings

    def test_unbound_bridge_is_fail_soft(self, env, client, caplog):
        trn.unbind()
        with caplog.at_level(
            logging.WARNING, logger="task_request_notifications"
        ):
            request = _create(client)

        assert request["status"] == "pending"
        assert task_request_store.get_request(request["request_id"]) is not None
        warnings = _warnings(caplog)
        assert any("not bound" in msg for msg in warnings), warnings

    def test_not_bound_by_default_outside_bot(self, env):
        """The module starts unbound — serve_miniapp alone never sends."""
        assert trn.is_bound() is False


# ════════════════════════════════════════════════════════════════════
# 9–11. No notification when the request never becomes pending
# ════════════════════════════════════════════════════════════════════


class TestNoNotificationOnRejectedPaths:
    def test_validation_failure_sends_no_notification(self, client, notifier):
        response = client.post(
            "/api/task-requests", headers=_auth(), json=_payload(title="")
        )
        assert response.status_code == 400
        assert notifier.sent == []
        assert task_request_store.count_pending() == 0

    def test_rate_limit_failure_sends_no_notification(
        self, client, notifier, monkeypatch
    ):
        monkeypatch.setattr(rate_limit, "MAX_ATTEMPTS_PER_WINDOW", 2)
        for _ in range(2):
            assert (
                client.post(
                    "/api/task-requests", headers=_auth(), json=_payload()
                ).status_code
                == 200
            )
        assert len(notifier.sent) == 2

        response = client.post(
            "/api/task-requests", headers=_auth(), json=_payload()
        )
        assert response.status_code == 429
        # The rejected attempt notified nobody and created nothing.
        assert len(notifier.sent) == 2
        assert task_request_store.count_pending() == 2

    def test_unauthenticated_rejected_without_notification(
        self, client, notifier
    ):
        response = client.post("/api/task-requests", json=VALID_PAYLOAD)
        assert response.status_code == 401
        assert notifier.sent == []
        assert task_request_store.count_pending() == 0

    def test_decided_request_is_never_notified(self, env, notifier):
        """A non-pending request handed to the notifier is a no-op."""
        created = task_request_store.create_request(USER_A, VALID_PAYLOAD)
        task_request_store.admin_return_request(
            created.request_id, RETURN_ADMIN, "تعديل"
        )
        decided = task_request_store.get_request(created.request_id)
        trn.notify_pending(decided)
        assert notifier.sent == []


# ════════════════════════════════════════════════════════════════════
# 12. Wiring: bot.py binds this module to the SAME AdminNotifier
# ════════════════════════════════════════════════════════════════════


class TestBotWiring:
    def test_bot_binds_and_unbinds_task_request_notifications(self):
        import inspect

        import bot

        source = inspect.getsource(bot)
        assert "task_request_notifications.bind(" in source
        assert "task_request_notifications.unbind()" in source
        # Same notifier instance as the other server-side flows.
        assert source.index("task_request_notifications.bind(") > source.index(
            "AdminNotifier(_send_text, markup_send=_send_markup)"
        )

    def test_routes_call_notify_pending_on_create_and_resubmit(self):
        import inspect

        import task_routes

        source = inspect.getsource(task_routes)
        assert source.count("task_request_notifications.notify_pending(") == 2
        create_src = inspect.getsource(task_routes.create_task_request)
        assert "notify_pending(created)" in create_src
        resubmit_src = inspect.getsource(task_routes.resubmit_task_request)
        assert "notify_pending(updated)" in resubmit_src
