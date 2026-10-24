"""Controlled two-account task-visibility diagnosis, isolated to tmp_path."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

import db
import serve_miniapp
import wallet
from task_creation import TaskSpec, create_task_from_spec
from task_taxonomy import VERIFICATION_MANUAL
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"
USER_A = 8101
USER_B = 8102
USDT = wallet.USDT_SCALE
_TASKS_JS = Path(__file__).resolve().parent / "miniapp" / "js" / "tasks.js"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Route every database access through this test's SQLite file."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "visibility_diag.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_A, "viewer", "Viewer")
    db.register_user(USER_B, "creator", "Creator")
    yield db_path


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


def _auth(user_id: int) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


def _catalog_titles(client, user_id: int) -> set:
    response = client.get("/api/tasks", headers=_auth(user_id))
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    return {task["title"] for task in data["tasks"]}


def _funded_catalog_task_by_b(title: str) -> int:
    """Create a real, funded catalog task owned by account B."""
    wallet.credit_units(USER_B, 100 * USDT)
    whole, _fraction = divmod(USDT, USDT)
    spec = TaskSpec(
        title=title,
        description="وصف المهمة",
        provider="instagram",
        action="follow",
        target={},
        verification=VERIFICATION_MANUAL,
        reward=whole,
        approver_id=USER_B,
        reward_units=USDT,
    )
    return create_task_from_spec(spec, funding_advertiser_id=USER_B)


class TestCatalogTaskVisibleToBoth:
    def test_funded_task_by_b_appears_in_a_list(self, client):
        _funded_catalog_task_by_b("مهمة الكتالوج")
        assert "مهمة الكتالوج" in _catalog_titles(client, USER_A)
        assert "مهمة الكتالوج" in _catalog_titles(client, USER_B)

    def test_owner_flag_does_not_hide_from_a(self, client):
        task_id = _funded_catalog_task_by_b("مهمة ملكية")
        entries = {
            task["id"]: task
            for task in client.get(
                "/api/tasks", headers=_auth(USER_A)
            ).get_json()["tasks"]
        }
        assert entries[task_id]["is_owner"] is False
        assert task_id in entries


class TestTaskRequestIsNotACatalogTask:
    def _create_request_by_b(self, client, title: str) -> dict:
        wallet.credit_units(USER_B, 100 * USDT)
        response = client.post(
            "/api/task-requests",
            headers={**_auth(USER_B), "Content-Type": "application/json"},
            json={
                "title": title,
                "description": "وصف طلب المهمة",
                "provider": "instagram",
                "action": "follow",
                "reward": 1,
            },
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        body = response.get_json()
        assert body["ok"] is True
        return body["request"]

    def test_request_visible_to_no_one_until_admin_approval(self, client):
        created = self._create_request_by_b(client, "طلب مهمة جديد")
        assert created["status"] == "pending"
        assert created["task_id"] is None
        assert "طلب مهمة جديد" not in _catalog_titles(client, USER_B)
        assert "طلب مهمة جديد" not in _catalog_titles(client, USER_A)

    def test_creator_sees_own_request_in_their_own_queue(self, client):
        self._create_request_by_b(client, "طلبي الخاص")
        response = client.get("/api/task-requests", headers=_auth(USER_B))
        assert response.status_code == 200
        titles = {request["title"] for request in response.get_json()["requests"]}
        assert "طلبي الخاص" in titles
        response = client.get("/api/task-requests", headers=_auth(USER_A))
        assert {request["title"] for request in response.get_json()["requests"]} == set()


class TestDeactivatedTaskInvisibleToAll:
    def test_active_zero_hides_from_creator_and_viewer(self, client):
        task_id = _funded_catalog_task_by_b("مهمة موقوفة")
        with db.get_connection() as conn:
            conn.execute("UPDATE tasks SET active = 0 WHERE id = ?", (task_id,))
        for user_id in (USER_A, USER_B):
            assert "مهمة موقوفة" not in _catalog_titles(client, user_id)


_HARNESS = r"""
'use strict';
const fs = require('fs');
const TASKS_JS = fs.readFileSync(process.argv[2], 'utf8');
function makeEl(tag) {
    const el = {
        tagName: tag, className: '', textContent: '', hidden: false,
        disabled: false, _children: [], _attrs: {}, _inner: '',
        _selectors: {},
        setAttribute(k, v) { this._attrs[k] = String(v); },
        getAttribute(k) { return this._attrs[k] ?? null; },
        addEventListener() {},
        appendChild(c) { this._children.push(c); return c; },
        querySelector(sel) {
            const m = /\[data-testid="([^"]+)"\]/.exec(sel);
            if (!m) return null;
            const id = m[1];
            if (!(id in this._selectors)) {
                this._selectors[id] = this._inner.includes(
                    'data-testid="' + id + '"'
                ) ? makeEl('stub') : null;
                if (this._selectors[id]) {
                    this._selectors[id].setAttribute('data-testid', id);
                }
            }
            return this._selectors[id];
        },
    };
    Object.defineProperty(el, 'innerHTML', {
        get() { return this._inner; },
        set(v) { this._inner = String(v); this._selectors = {}; },
    });
    return el;
}
function collect(node, testid, out) {
    if (node.getAttribute && node.getAttribute('data-testid') === testid) {
        out.push(node);
    }
    for (const child of node._children || []) collect(child, testid, out);
}
async function main() {
    globalThis.document = { createElement: (tag) => makeEl(tag) };
    globalThis.window = {};
    globalThis.TelegramApp = { getInitData: () => 'stub-init-data' };
    globalThis.fetch = async () => ({
        ok: true,
        json: async () => ({
            ok: true,
            tasks: [
                { id: 1, title: 'b-task', description: 'd',
                  type: 'deterministic', reward: 1, reward_units: null,
                  status: 'available', is_owner: false },
                { id: 2, title: 'a-task', description: 'd',
                  type: 'deterministic', reward: 1, reward_units: null,
                  status: 'available', is_owner: true },
            ],
        }),
    });
    eval(TASKS_JS + '\n;globalThis.__Tasks = Tasks;\n');
    const page = globalThis.__Tasks.render();
    for (let i = 0; i < 5; i++) {
        await new Promise((resolve) => setImmediate(resolve));
    }
    const cards = [];
    const list = page.querySelector('[data-testid="tasks-list"]');
    if (list) collect(list, 'task-card', cards);
    process.stdout.write(JSON.stringify({ cardCount: cards.length }));
}
main().catch((error) => {
    process.stderr.write(String(error && error.stack || error));
    process.exit(1);
});
"""


class TestUiRendersEverythingTheApiReturns:
    def test_no_client_side_task_filtering(self, tmp_path):
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is required to execute the UI harness")
        harness_path = tmp_path / "visibility_harness.js"
        harness_path.write_text(_HARNESS, encoding="utf-8")
        proc = subprocess.run(
            [node, harness_path, _TASKS_JS],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        assert result["cardCount"] == 2, (
            "the page dropped a task the API returned — a client-side "
            "visibility filter exists"
        )
