"""
Real-browser journey — owner task editing (Chromium, mobile screen)
====================================================================

Drives the SHIPPED Mini App in a real headless Chromium at a phone
viewport (390x844, DPR 3, touch, iPhone UA) against a real Flask
server (``serve_miniapp.app`` on an ephemeral port, isolated temp
database), covering what the node stub-DOM suite cannot:

  Journey (owner → server → worker)
  - the dialog opens from the «إضافة مهمة ➕» CTA, the edit control is
    visible on the owner's published row and its STYLES are actually
    applied (computed display/border-radius/cursor from app.css) and
    it fits the phone screen
  - tapping edit prefills the CURRENT title/description, locks the
    execution fields, and switches the primary button
  - saving round-trips the real POST /api/owner/tasks/<id>/edit
    (asserted via expect_response → 200), shows the SERVER's success
    message, and refreshes the list in place
  - a WORKER's catalog page (GET /api/tasks → tasks.js) shows the
    EDITED title and description afterwards — the propagation contract
    proven through the real read path

  Rejections (never a fake success, in a real browser)
  - a non-owner gets 404 owner_task_not_found from the API and has no
    owner controls in their own dialog
  - an aborted (network-failed) save and a 400 server rejection each
    surface the error with NO success banner and no list refresh

  Lifecycle (current archive policy inspected, NOT changed)
  - pause hides the task from the worker's catalog and swaps the row
    to resume; resume restores it
  - archive (with the browser's native confirm) hides the row while
    the task row, the approved request and the money stay intact —
    the owner path contains NO DELETE (append-only history event)

Gated exactly like ``test_miniapp_reviewer_chromium.py``: set
``RUN_CHROMIUM_TESTS=1``; skipped cleanly when playwright or the
browser is unavailable.  Evidence screenshots land in
``/tmp/phase_owner_edit_journey/``.

Run:
    RUN_CHROMIUM_TESTS=1 python3 -m pytest test_miniapp_owner_edit_chromium.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import config
import db
import task_request_admin as admin
import task_request_store as store
import wallet
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

OWNER = 6201
WORKER = 6202
STRANGER = 6203
ADMIN_ID = 5101

USDT = wallet.USDT_SCALE
SEED = 1000 * USDT

ORIGINAL_TITLE = "متابعة حسابي على Instagram"
ORIGINAL_DESC = "تابع الحساب ثم أرسل إثبات المتابعة"
NEW_TITLE = "عنوان جديد بعد التعديل"
NEW_DESC = "وصف جديد بعد التعديل من الموبايل"

SHOTS = Path("/tmp/phase_owner_edit_journey")

MOBILE = {
    "viewport": {"width": 390, "height": 844},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
    "user_agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
}


def _telegram_stub(user_id: int, init_data: str) -> str:
    """Inject window.Telegram.WebApp BEFORE any page script, so
    TelegramApp.getInitData() returns the signed initData (the
    telegram.org SDK itself is blocked by a route — no network SDK).
    The data half is JSON (brace-safe); the callable half is merged
    in with Object.assign so the object is always valid JS."""
    data = json.dumps(
        {
            "initData": init_data,
            "initDataUnsafe": {
                "user": {
                    "id": user_id,
                    "first_name": "Tester",
                    "is_bot": False,
                },
                "query_id": "test-query",
            },
            "themeParams": {
                "bg_color": "#ffffff",
                "text_color": "#111111",
                "hint_color": "#888888",
                "secondary_bg_color": "#f1f1f1",
                "button_color": "#3388cc",
                "button_text_color": "#ffffff",
            },
            "colorScheme": "light",
        },
        ensure_ascii=False,
    )
    return (
        "(() => { const WebApp = Object.assign(" + data + ", {"
        "ready() {}, expand() {}, close() {}, onEvent() {}, "
        "setHeaderColor() {}, setBackgroundColor() {}, "
        "HapticFeedback: { impactOccurred() {}, "
        "notificationOccurred() {}, selectionChanged() {} }, "
        "BackButton: { show() {}, hide() {}, onClick() {} }"
        "}); window.Telegram = { WebApp }; })();"
    )


@pytest.fixture(scope="module")
def journey(tmp_path_factory):
    """Isolated DB + one published owner task + a real HTTP server."""
    if os.environ.get("RUN_CHROMIUM_TESTS") != "1":
        pytest.skip("set RUN_CHROMIUM_TESTS=1 to run the real-browser suite")
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright is not installed for the browser suite")

    mp = pytest.MonkeyPatch()
    mp.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path_factory.mktemp("owner_edit") / "journey.db")
    mp.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    mp.setattr(config, "ADMINS", [ADMIN_ID])

    db.register_user(OWNER, "owner", "Owner")
    wallet.credit_units(OWNER, SEED)

    request = store.create_request(
        OWNER,
        {
            "title": ORIGINAL_TITLE,
            "description": ORIGINAL_DESC,
            "provider": "instagram",
            "action": "follow",
            "target_ref": "https://instagram.com/example",
            "reward": "0.5",
        },
    )
    query = mock.MagicMock()
    query.answer = mock.AsyncMock()
    query.edit_message_text = mock.AsyncMock()
    asyncio.run(admin._approve(query, ADMIN_ID, request.request_id))
    stored = store.get_request(request.request_id)
    assert stored.status == store.STATUS_APPROVED
    task_id = stored.published_task_id
    assert isinstance(task_id, int)

    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, serve_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    # Fail fast if the browser cannot actually launch.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        except Exception as exc:  # pragma: no cover - env dependent
            pytest.skip(f"chromium cannot launch here: {exc}")

    SHOTS.mkdir(parents=True, exist_ok=True)
    yield SimpleNamespace(
        url=base_url,
        task_id=task_id,
        request_id=request.request_id,
        shots=SHOTS,
        mp=mp,
    )
    server.shutdown()
    thread.join(timeout=5)
    mp.undo()


def serve_app():
    import serve_miniapp

    return serve_miniapp.app


@pytest.fixture(scope="module")
def browser(journey):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        instance = pw.chromium.launch(headless=True)
        yield instance
        instance.close()


def _identity(browser, journey, user_id: int):
    """A fresh mobile context for one verified Telegram identity."""
    context = browser.new_context(**MOBILE)
    context.add_init_script(
        _telegram_stub(user_id, _make_init_data(user_id=user_id))
    )
    # The production SDK comes from telegram.org — stub it out so the
    # injected WebApp object is the only one.
    context.route(
        "https://telegram.org/**",
        lambda route: route.abort(),
    )
    return context


def _open_dialog(page, journey):
    page.goto(journey.url)
    page.wait_for_selector('[data-testid="add-task-cta"]', timeout=15_000)
    page.tap('[data-testid="add-task-cta"]')
    page.wait_for_selector(
        '[data-testid="taskreq-overlay"]', timeout=15_000
    )
    page.wait_for_selector(
        '[data-testid="taskreq-owner-edit"]', timeout=15_000
    )


def _start_edit(page):
    page.tap('[data-testid="taskreq-owner-edit"]')
    page.wait_for_selector(
        '[data-testid="taskreq-owner-cancel"]:not([hidden])', timeout=10_000
    )


def _save(page):
    with page.expect_response(
        "**/api/owner/tasks/*/edit", timeout=15_000
    ) as info:
        page.tap('[data-testid="taskreq-submit"]')
    return info.value


# ════════════════════════════════════════════════════════════════════
# 1. Journey: owner UI → server → worker-visible task data
# ════════════════════════════════════════════════════════════════════


class TestOwnerEditBrowserJourney:
    def test_full_journey_edit_shows_on_worker_catalog(
        self, journey, browser
    ):
        # ── Owner side ──────────────────────────────────────────
        owner = _identity(browser, journey, OWNER)
        page = owner.new_page()
        _open_dialog(page, journey)

        edit_btn = page.locator('[data-testid="taskreq-owner-edit"]')
        assert edit_btn.count() == 1
        assert (
            edit_btn.get_attribute("data-published-task-id")
            == str(journey.task_id)
        )

        # The styles really come from app.css (computed styles).
        actions_style = page.eval_on_selector(
            ".taskreq-owner-actions",
            "el => getComputedStyle(el).display",
        )
        assert actions_style == "flex"
        btn_style = page.eval_on_selector(
            '[data-testid="taskreq-owner-edit"]',
            """el => { const s = getComputedStyle(el);
                 return { radius: s.borderRadius, border: s.borderTopWidth,
                          cursor: s.cursor }; }""",
        )
        assert btn_style["radius"] == "10px"
        assert btn_style["border"] == "1px"
        assert btn_style["cursor"] == "pointer"

        # Phone screen: the control fits a 390px viewport.
        box = edit_btn.bounding_box()
        assert box is not None
        assert box["x"] >= 0 and box["x"] + box["width"] <= 391
        page.screenshot(path=str(journey.shots / "01_owner_list.png"))

        # ── Edit form ───────────────────────────────────────────
        _start_edit(page)
        assert page.input_value('[data-testid="taskreq-title"]') == ORIGINAL_TITLE
        assert (
            page.input_value('[data-testid="taskreq-description"]')
            == ORIGINAL_DESC
        )
        for locked in ("taskreq-provider", "taskreq-action",
                       "taskreq-target", "taskreq-reward"):
            assert page.is_disabled(f'[data-testid="{locked}"]'), locked
        assert (
            page.inner_text('[data-testid="taskreq-submit"]')
            == "💾 حفظ التعديلات"
        )
        page.screenshot(path=str(journey.shots / "02_edit_mode.png"))

        page.fill('[data-testid="taskreq-title"]', NEW_TITLE)
        page.fill('[data-testid="taskreq-description"]', NEW_DESC)

        # ── Save → REAL server round-trip ───────────────────────
        response = _save(page)
        assert response.status == 200
        body = response.json()
        assert body["ok"] is True
        assert body["message"] == "تم تعديل المهمة"

        page.wait_for_selector(
            '[data-testid="taskreq-success"]', timeout=10_000
        )
        assert "تم تعديل المهمة" in page.inner_text(
            '[data-testid="taskreq-success"]'
        )
        # The list refreshed from the server with the new title.
        page.wait_for_function(
            """(title) => Array.from(
                    document.querySelectorAll('.taskreq-item-title')
                ).some((el) => el.textContent === title)""",
            arg=NEW_TITLE,
            timeout=10_000,
        )
        # Edit mode is over: the primary button is back.
        assert (
            page.inner_text('[data-testid="taskreq-submit"]')
            == "إرسال للمراجعة"
        )
        page.screenshot(path=str(journey.shots / "03_owner_success.png"))

        # Server-side truth: payload + published row both carry the edit.
        owner_api = owner.request.get(
            f"{journey.url}/api/owner/tasks/{journey.task_id}",
            headers={INIT_DATA_HEADER: _make_init_data(user_id=OWNER)},
        )
        assert owner_api.status == 200
        assert owner_api.json()["request"]["title"] == NEW_TITLE
        task_row = db.get_task(journey.task_id)
        assert task_row["title"] == NEW_TITLE
        assert task_row["description"] == NEW_DESC

        # ── Worker side: the EDITED data through the real read path
        worker = _identity(browser, journey, WORKER)
        worker_page = worker.new_page()
        worker_page.goto(journey.url)
        worker_page.tap('[data-page="tasks"]')
        worker_page.wait_for_selector(
            '[data-testid="task-title"]', timeout=15_000
        )
        worker_page.wait_for_function(
            """(title) => Array.from(
                    document.querySelectorAll('[data-testid="task-title"]')
                ).some((el) => el.textContent === title)""",
            arg=NEW_TITLE,
            timeout=10_000,
        )
        descriptions = worker_page.eval_on_selector_all(
            '[data-testid="task-description"]',
            "els => els.map((el) => el.textContent)",
        )
        assert NEW_DESC in descriptions
        worker_page.screenshot(
            path=str(journey.shots / "04_worker_catalog.png")
        )

        worker.close()
        owner.close()


# ════════════════════════════════════════════════════════════════════
# 2. Rejections — a real browser never fakes a success
# ════════════════════════════════════════════════════════════════════


class TestOwnerEditBrowserRejections:
    def test_non_owner_network_failure_and_server_rejection(
        self, journey, browser
    ):
        # ── Non-owner: the API refuses outright ─────────────────
        stranger = _identity(browser, journey, STRANGER)
        refused = stranger.request.post(
            f"{journey.url}/api/owner/tasks/{journey.task_id}/edit",
            headers={
                INIT_DATA_HEADER: _make_init_data(user_id=STRANGER),
                "Content-Type": "application/json",
            },
            data=json.dumps({"title": "خبيث", "description": "محاولة"}),
        )
        assert refused.status == 404
        assert refused.json()["error"] == "owner_task_not_found"

        # …and a stranger's own dialog offers zero owner controls.
        stranger_page = stranger.new_page()
        stranger_page.goto(journey.url)
        stranger_page.tap('[data-testid="add-task-cta"]')
        stranger_page.wait_for_selector(
            '[data-testid="taskreq-overlay"]', timeout=15_000
        )
        stranger_page.wait_for_selector(
            '[data-testid="taskreq-empty"]', timeout=15_000
        )
        assert stranger_page.locator(
            '[data-testid="taskreq-owner-edit"]'
        ).count() == 0

        # ── Network failure in a real browser ───────────────────
        owner = _identity(browser, journey, OWNER)
        page = owner.new_page()
        page.route(
            "**/api/owner/tasks/*/edit",
            lambda route: route.abort(),
        )
        _open_dialog(page, journey)
        _start_edit(page)
        page.fill('[data-testid="taskreq-title"]', "عنوان لن يُحفظ")
        page.fill('[data-testid="taskreq-description"]', "وصف لن يُحفظ")
        page.tap('[data-testid="taskreq-submit"]')
        page.wait_for_selector(
            '[data-testid="taskreq-form-error"]:not([hidden])',
            timeout=10_000,
        )
        assert page.locator('[data-testid="taskreq-success"]').count() == 0
        assert (
            page.inner_text('[data-testid="taskreq-submit"]')
            == "💾 حفظ التعديلات"
        )
        page.screenshot(path=str(journey.shots / "05_network_failure.png"))
        page.unroute("**/api/owner/tasks/*/edit")

        # ── Server rejection (400) in a real browser ────────────
        page.route(
            "**/api/owner/tasks/*/edit",
            lambda route: route.fulfill(
                status=400,
                content_type="application/json",
                body=json.dumps(
                    {
                        "ok": False,
                        "error": "invalid_payload",
                        "message": "بيانات التعديل غير صالحة",
                    }
                ),
            ),
        )
        page.fill('[data-testid="taskreq-title"]', "عنوان مرفوض")
        page.fill('[data-testid="taskreq-description"]', "وصف مرفوض")
        with page.expect_response(
            "**/api/owner/tasks/*/edit", timeout=15_000
        ) as info:
            page.tap('[data-testid="taskreq-submit"]')
        assert info.value.status == 400
        page.wait_for_function(
            """() => {
                const box = document.querySelector(
                    '[data-testid="taskreq-form-error"]');
                return box && !box.hidden &&
                    box.textContent.includes('بيانات التعديل غير صالحة');
            }""",
            timeout=10_000,
        )
        assert page.locator('[data-testid="taskreq-success"]').count() == 0
        page.screenshot(path=str(journey.shots / "06_server_reject.png"))

        owner.close()
        stranger.close()


# ════════════════════════════════════════════════════════════════════
# 3. Lifecycle — pause / resume / archive (policy inspected, unchanged)
# ════════════════════════════════════════════════════════════════════


class TestOwnerLifecycleBrowser:
    def test_pause_hides_resume_restores_archive_hides_forever(
        self, journey, browser
    ):
        ledger_before = _row_count("SELECT COUNT(*) AS n FROM ledger")
        wallet_before = _wallet_units(OWNER)

        def worker_sees_task(worker_page) -> bool:
            """Navigate to the worker catalog and report whether ANY task
            is visible.  Waits for the list to SETTLE (tasks.js hides
            `tasks-loading` only once /api/tasks resolves), so a slow
            fetch can't be misread as 'hidden' and a rendered error can't
            be misread as 'empty'."""
            worker_page.goto(journey.url)
            worker_page.wait_for_selector(
                '[data-testid="home-page"]', timeout=15_000
            )
            worker_page.tap('[data-page="tasks"]')
            try:
                # `tasks-loading` is visible on entry and cleared only
                # when load() finishes rendering list OR empty OR error.
                worker_page.wait_for_function(
                    """() => {
                        const q = (id) => document.querySelector(
                            `[data-testid="${id}"]`);
                        const settled = [
                            'task-title', 'tasks-empty', 'tasks-error',
                        ].some((id) => {
                            const el = q(id);
                            return el && el.offsetParent !== null;
                        });
                        const loading = q('tasks-loading');
                        return settled || (loading && loading.hidden);
                    }""",
                    timeout=15_000,
                )
            except Exception:
                worker_page.screenshot(
                    path=str(journey.shots / "worker_state_on_fail.png")
                )
                return False
            titles = worker_page.eval_on_selector_all(
                '[data-testid="task-title"]',
                "els => els.filter((el) => el.offsetParent !== null)"
                ".map((el) => el.textContent)",
            )
            return len(titles) > 0

        owner = _identity(browser, journey, OWNER)
        page = owner.new_page()
        worker = _identity(browser, journey, WORKER)
        worker_page = worker.new_page()

        # Baseline: the worker sees the task (it was edited earlier).
        assert worker_sees_task(worker_page) is True

        # ── Pause: hidden from the worker, row swaps to resume ──
        _open_dialog(page, journey)
        with page.expect_response(
            "**/api/owner/tasks/*/pause", timeout=15_000
        ) as info:
            page.tap('[data-testid="taskreq-owner-pause"]')
        assert info.value.status == 200
        page.wait_for_selector(
            '[data-testid="taskreq-success"]', timeout=10_000
        )
        assert "تم إيقاف المهمة" in page.inner_text(
            '[data-testid="taskreq-success"]'
        )
        page.wait_for_selector(
            '[data-testid="taskreq-owner-resume"]', timeout=10_000
        )
        assert page.locator('[data-testid="taskreq-owner-edit"]').count() == 0
        page.screenshot(path=str(journey.shots / "07_paused_row.png"))
        assert worker_sees_task(worker_page) is False

        # ── Resume: restored for the worker, edit returns ───────
        with page.expect_response(
            "**/api/owner/tasks/*/resume", timeout=15_000
        ) as info:
            page.tap('[data-testid="taskreq-owner-resume"]')
        assert info.value.status == 200
        page.wait_for_selector(
            '[data-testid="taskreq-owner-edit"]', timeout=10_000
        )
        assert worker_sees_task(worker_page) is True

        # ── Archive: native confirm → hidden forever, NO delete ─
        page.on("dialog", lambda dialog: dialog.accept())
        with page.expect_response(
            "**/api/owner/tasks/*/archive", timeout=15_000
        ) as info:
            page.tap('[data-testid="taskreq-owner-archive"]')
        assert info.value.status == 200
        page.wait_for_selector(
            '[data-testid="taskreq-empty"]', timeout=10_000
        )
        page.screenshot(path=str(journey.shots / "08_archived_list.png"))
        assert worker_sees_task(worker_page) is False

        # Archive policy (INSPECTED, unchanged): a logical hide only —
        # the task row, the approved request and the money survive.
        task_row = db.get_task(journey.task_id)
        assert task_row is not None
        stored = store.get_request(journey.request_id)
        assert stored.status == store.STATUS_APPROVED
        events = [e.get("event") for e in stored.history]
        assert "owner_archive" in events
        assert _row_count("SELECT COUNT(*) AS n FROM ledger") == ledger_before
        assert _wallet_units(OWNER) == wallet_before

        # The owner can still READ the archived task (hide ≠ delete).
        archived = owner.request.get(
            f"{journey.url}/api/owner/tasks/{journey.task_id}",
            headers={INIT_DATA_HEADER: _make_init_data(user_id=OWNER)},
        )
        assert archived.status == 200
        assert archived.json()["request"]["archived"] is True

        owner.close()
        worker.close()


def _row_count(sql: str, params: tuple = ()) -> int:
    with db.get_connection() as conn:
        return int(conn.execute(sql, params).fetchone()["n"])


def _wallet_units(user_id: int) -> int | None:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT available_units FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return None if row is None else int(row["available_units"])
