"""
Home stats cards — «المتاح» (available) / «المكافآت» (rewards)
==============================================================

Verifies the two Home summary cards against the SAME data source the
Tasks screen uses (GET /api/tasks):

Wiring (source level):
- index.html loads task-stats.js before home.js (and app.js last)
- home.js stays fetch-free and fills the cards through TaskStats
- task-stats.js reads the exact tasks endpoint + initData header the
  Tasks page sends and derives availability from the backend's own
  status field only

Runtime (node, skips cleanly when node is unavailable):
The shipped wallet-data.js + task-stats.js + home.js are evaluated
together against a backend-shaped payload, then:
- «المتاح» shows exactly the number of tasks the payload reports
  with status "available" (started/completed rows never count)
- «المكافآت» shows the exact atomic sum of THOSE SAME tasks' rewards
  (reward_units when present, whole-USDT reward fallback, integer
  math, unchanged USDT currency)
- an empty backend list shows the real zero ("0" / "0.00000000 USDT")
- a failed or unauthenticated read keeps the existing neutral «—»
  placeholder — no mock value, no invented fallback number
- every read goes to '/api/tasks' with the verified initData header

End-to-end (real backend + real database):
- the production Flask endpoint (isolated temp DB) answers GET
  /api/tasks and the shipped JS renders the cards from that exact
  payload; values are cross-checked against TaskCatalog / the DB
- starting + completing a task through the real API, adding a task
  and deactivating a task are all reflected on the next card read
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import db
import serve_miniapp
from config import CHANNELS, Channel
from channel_task_verifier import (
    CHANNEL_TASK_TYPE,
    ChannelTaskVerifier,
    register_channel_task_verifier,
)
# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data


MINIAPP = Path("miniapp")
HOME_JS = MINIAPP / "js" / "home.js"
TASK_STATS_JS = MINIAPP / "js" / "task-stats.js"
WALLET_DATA_JS = MINIAPP / "js" / "wallet-data.js"
TASKS_JS = MINIAPP / "js" / "tasks.js"
INDEX_HTML = MINIAPP / "index.html"

INIT_DATA_HEADER = "X-Telegram-Init-Data"
# Stub identity value: only the header channel itself is under test.
FAKE_INIT_DATA = "111111111:test-init-data-hash"
AVAIL_NODE = '[data-testid="balance-available"] .balance-value'
REWARD_NODE = '[data-testid="balance-reward"] .balance-value'


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _require_node() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not available in this environment")


# ── Backend-shaped payloads (exactly what GET /api/tasks returns) ────

_FIXTURE_MIXED = {
    "ok": True,
    "tasks": [
        # whole-USDT reward with exact atomic units
        {"id": 1, "title": "t1", "description": "", "type": "telegram_channel",
         "reward": 1, "reward_units": 100_000_000, "status": "available"},
        # sub-cent reward (0.25 USDT) — exact atomic units only
        {"id": 2, "title": "t2", "description": "", "type": "manual",
         "reward": 0, "reward_units": 25_000_000, "status": "available"},
        # legacy row without atomic units → whole-USDT fallback
        {"id": 3, "title": "t3", "description": "", "type": "deterministic",
         "reward": 3, "reward_units": None, "status": "available"},
        # backend says started — must not count
        {"id": 4, "title": "t4", "description": "", "type": "manual",
         "reward": 5, "reward_units": 500_000_000, "status": "started"},
        # backend says completed — must not count
        {"id": 5, "title": "t5", "description": "", "type": "deterministic",
         "reward": 7, "reward_units": 700_000_000, "status": "completed"},
        # awaiting a decision but still "available" per the backend → counts
        {"id": 6, "title": "t6", "description": "", "type": "referral_task",
         "reward": 11, "reward_units": 1_100_000_000, "status": "available",
         "awaiting_decision": True},
    ],
}

_FIXTURE_NONE_AVAILABLE = {
    "ok": True,
    "tasks": [
        {"id": 1, "title": "t1", "description": "", "type": "manual",
         "reward": 5, "reward_units": 500_000_000, "status": "started"},
        {"id": 2, "title": "t2", "description": "", "type": "manual",
         "reward": 7, "reward_units": 700_000_000, "status": "completed"},
    ],
}

_FIXTURE_EMPTY = {"ok": True, "tasks": []}


def _expected(fixture: dict) -> tuple[int, int | None]:
    """Independent Python mirror of the documented contract.

    availableCount = tasks the backend reports with status "available"
    rewardUnits    = exact atomic sum of those same tasks' rewards
                     (reward_units, else whole-USDT reward × scale;
                     None when any component is not an exact integer)
    """
    from wallet import USDT_SCALE

    available = [
        t for t in fixture.get("tasks", [])
        if t.get("status") == "available"
    ]
    total = 0
    exact = True
    for task in available:
        units = task.get("reward_units")
        if isinstance(units, int) and not isinstance(units, bool):
            total += units
            continue
        reward = task.get("reward")
        if isinstance(reward, int) and not isinstance(reward, bool):
            total += reward * USDT_SCALE
        else:
            exact = False
    return len(available), (total if exact else None)


def _expected_text(units: int | None) -> str | None:
    """Mirror of WalletData.formatUsdt + the unchanged USDT label."""
    if units is None:
        return None
    from wallet import USDT_SCALE

    whole, frac = divmod(units, USDT_SCALE)
    return f"{whole}.{frac:08d} USDT"


# ── node harness: shipped JS evaluated together, DOM stub records ────

_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));

const __urls = [];
const __headers = [];

function fetch(url, opts) {
    __urls.push(url);
    __headers.push((opts && opts.headers) || {});
    if (input.mode === 'error') {
        return Promise.reject(new Error('network down'));
    }
    if (input.mode === 'unauth') {
        return Promise.resolve({
            ok: false,
            status: 401,
            json: async () => ({ ok: false, error: 'unauthenticated' })
        });
    }
    return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => input.fixture
    });
}

// Minimal DOM stub: innerHTML is kept as text; querySelector records
// the selector and hands back a node that records its mutations.
function makeElement(tag) {
    const el = {
        tagName: tag,
        className: '',
        _attrs: Object.create(null),
        children: [],
        _html: '',
        _queries: [],
        _nodes: Object.create(null),
        setAttribute(name, value) { this._attrs[name] = String(value); },
        getAttribute(name) {
            return name in this._attrs ? this._attrs[name] : null;
        },
        appendChild(child) { this.children.push(child); return child; },
        addEventListener() {},
        set innerHTML(value) { this._html = String(value); },
        get innerHTML() { return this._html; },
        querySelector(sel) {
            this._queries.push(sel);
            if (!this._nodes[sel]) {
                const node = {
                    textContent: '',
                    removed: [],
                    addEventListener() {}
                };
                node.classList = {
                    remove: (cls) => { node.removed.push(cls); }
                };
                this._nodes[sel] = node;
            }
            return this._nodes[sel];
        }
    };
    return el;
}

const ctx = {
    console,
    setTimeout,
    document: { createElement: makeElement },
    TelegramApp: {
        getUser: () => ({ first_name: 'Tester', username: 'tester' }),
        getInitData: () => input.initData
    },
    fetch,
    __urls,
    __headers
};
vm.createContext(ctx);

const code = [
    input.walletJs,
    input.statsJs,
    input.homeJs,
    `;__done = (async () => {
        const page = Home.render();
        const balance = page.children.find(
            (c) => c.getAttribute('data-testid') === 'home-balance'
        );
        // Second read through the same module (identical payload).
        const stats = await TaskStats.load();
        await new Promise((resolve) => setTimeout(resolve, 25));
        __out = {
            stats: stats,
            formatted: stats
                ? TaskStats.formatRewards(stats.rewardUnits)
                : null,
            urls: __urls,
            headers: __headers,
            queries: balance ? balance._queries : [],
            nodes: balance ? balance._nodes : {},
            balanceHtml: balance ? balance._html : ''
        };
    })();`
].join('\n');

vm.runInContext(code, ctx);

ctx.__done.then(
    () => process.stdout.write(JSON.stringify(ctx.__out)),
    (error) => {
        process.stderr.write(String((error && error.stack) || error));
        process.exit(1);
    }
);
"""

_SCALE_HARNESS_JS = r"""
const vm = require('vm');
const src = require('fs').readFileSync(0, 'utf8');
const ctx = {};
vm.createContext(ctx);
vm.runInContext(
    src + '\n;__out = { scale: WalletData.USDT_UNITS_PER_USDT };',
    ctx
);
process.stdout.write(JSON.stringify(ctx.__out));
"""

_EXPORTS_HARNESS_JS = r"""
const vm = require('vm');
const src = require('fs').readFileSync(0, 'utf8');
const ctx = {};
vm.createContext(ctx);
vm.runInContext(src + '\n;__out = Object.keys(TaskStats).sort();', ctx);
process.stdout.write(JSON.stringify(ctx.__out));
"""


def _run(fixture: dict | None, mode: str = "ok") -> dict:
    """Evaluate the shipped JS in node; return what Home displayed."""
    _require_node()
    payload = {
        "walletJs": _read(WALLET_DATA_JS),
        "statsJs": _read(TASK_STATS_JS),
        "homeJs": _read(HOME_JS),
        "fixture": fixture,
        "mode": mode,
        "initData": FAKE_INIT_DATA,
    }
    result = subprocess.run(
        ["node", "-e", _HARNESS_JS],
        input=json.dumps(payload, ensure_ascii=True),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"home/stats harness failed:\n{result.stderr}"
    )
    return json.loads(result.stdout)


def _node_value(out: dict, selector: str) -> dict | None:
    return out["nodes"].get(selector)


# ════════════════════════════════════════════════════════════════════
# 1. Wiring — same source as the Tasks screen, Home stays fetch-free
# ════════════════════════════════════════════════════════════════════

class TestWiring:
    def test_index_loads_task_stats_before_home(self):
        html = _read(INDEX_HTML)
        stats_pos = html.find('src="js/task-stats.js"')
        home_pos = html.find('src="js/home.js"')
        app_pos = html.find('src="js/app.js"')
        assert stats_pos != -1, "task-stats.js must be loaded by index.html"
        assert home_pos != -1 and stats_pos < home_pos, \
            "task-stats.js loads before home.js"
        assert app_pos != -1 and stats_pos < app_pos, \
            "task-stats.js loads before app.js"

    def test_home_fills_cards_through_task_stats(self):
        content = _read(HOME_JS)
        assert "TaskStats.load" in content, \
            "Home must read the cards through TaskStats"
        assert "TaskStats.formatRewards" in content, \
            "reward card must use the shared USDT formatter"
        assert "_fillBalanceValues" in content
        # Home itself stays fetch-free (data ownership lives in
        # task-stats.js, exactly like TaskRequestUI for the CTA).
        assert "fetch(" not in content
        assert "initData" not in content

    def test_home_keeps_original_card_design_and_placeholder(self):
        content = _read(HOME_JS)
        idx = content.find("home-balance")
        markup_end = content.find("`;", idx)
        markup = content[idx:markup_end]
        assert 'data-testid="balance-reward"' in markup
        assert 'data-testid="balance-available"' in markup
        assert ">المكافآت<" in markup
        assert ">المتاح<" in markup
        assert markup.count("balance-value balance-empty") == 2, \
            "both cards keep the neutral placeholder markup"
        assert "—" in markup, "the em-dash placeholder markup must stay"

    def test_task_stats_reads_the_tasks_screen_endpoint(self):
        stats = _read(TASK_STATS_JS)
        tasks = _read(TASKS_JS)
        assert "LIST_URL = '/api/tasks'" in stats
        assert "'/api/tasks'" in tasks, "Tasks page reads the same URL"
        assert INIT_DATA_HEADER in stats
        assert INIT_DATA_HEADER in tasks, \
            "both screens send the same identity header"
        assert "task.status" in stats, \
            "availability must come from the backend status field"
        # The scale comes from the shared WalletData source — never a
        # second hard-coded conversion constant.
        assert "USDT_UNITS_PER_USDT" in stats
        assert "100000000" not in stats

    def test_task_stats_module_exports(self):
        _require_node()
        result = subprocess.run(
            ["node", "-e", _EXPORTS_HARNESS_JS],
            input=_read(TASK_STATS_JS),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == ["formatRewards", "load"]

    def test_walletdata_scale_matches_backend_scale(self):
        """JS atomic scale must equal wallet.USDT_SCALE exactly."""
        _require_node()
        from wallet import USDT_SCALE

        result = subprocess.run(
            ["node", "-e", _SCALE_HARNESS_JS],
            input=_read(WALLET_DATA_JS),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["scale"] == USDT_SCALE


# ════════════════════════════════════════════════════════════════════
# 2. Runtime — the cards show exactly the backend payload's numbers
# ════════════════════════════════════════════════════════════════════

class TestRuntimeValues:
    def test_cards_match_backend_payload_exactly(self):
        count, units = _expected(_FIXTURE_MIXED)
        assert (count, units) == (4, 1_525_000_000)  # documented contract

        out = _run(_FIXTURE_MIXED)

        # Raw stats: backend availability + exact atomic sum.
        assert out["stats"] == {"availableCount": count,
                                "rewardUnits": units}
        assert out["formatted"] == _expected_text(units)

        # What the Home cards actually display.
        available = _node_value(out, AVAIL_NODE)
        assert available is not None, "«المتاح» card was never filled"
        assert available["textContent"] == str(count)
        assert "balance-empty" in available["removed"]

        reward = _node_value(out, REWARD_NODE)
        assert reward is not None, "«المكافآت» card was never filled"
        assert reward["textContent"] == _expected_text(units)
        assert reward["textContent"].endswith(" USDT"), \
            "the reward currency label must stay USDT"
        assert "balance-empty" in reward["removed"]

        # Same source as the Tasks screen: every read is GET /api/tasks
        # carrying the verified initData identity header.
        assert out["urls"], "no backend read happened"
        assert set(out["urls"]) == {"/api/tasks"}
        for headers in out["headers"]:
            assert headers.get(INIT_DATA_HEADER) == FAKE_INIT_DATA

    def test_backend_statuses_never_invented(self):
        """Started/completed rows never count; only the server decides."""
        count, units = _expected(_FIXTURE_NONE_AVAILABLE)
        assert (count, units) == (0, 0)

        out = _run(_FIXTURE_NONE_AVAILABLE)
        assert out["stats"] == {"availableCount": 0, "rewardUnits": 0}
        assert _node_value(out, AVAIL_NODE)["textContent"] == "0"
        assert _node_value(out, REWARD_NODE)["textContent"] == "0.00000000 USDT"

    def test_empty_catalog_shows_real_zero(self):
        out = _run(_FIXTURE_EMPTY)
        assert out["stats"] == {"availableCount": 0, "rewardUnits": 0}
        assert out["formatted"] == "0.00000000 USDT"
        assert _node_value(out, AVAIL_NODE)["textContent"] == "0"
        assert _node_value(out, REWARD_NODE)["textContent"] == "0.00000000 USDT"

    @pytest.mark.parametrize("mode", ["error", "unauth"])
    def test_failed_read_keeps_neutral_placeholder(self, mode):
        """No backend confirmation → the existing «—» stays; no number."""
        out = _run(_FIXTURE_MIXED, mode=mode)
        assert out["stats"] is None
        assert out["formatted"] is None
        assert out["queries"] == [], \
            "Home must not write any value it did not receive"
        assert out["nodes"] == {}
        assert "—" in out["balanceHtml"], \
            "the placeholder markup must remain untouched"
        assert set(out["urls"]) == {"/api/tasks"}

    def test_sum_is_recomputed_fresh_on_every_render(self):
        """Adding/removing tasks is reflected on the next Home render."""
        first = _run(_FIXTURE_MIXED)
        second = _run(_FIXTURE_EMPTY)
        assert first["stats"]["availableCount"] == 4
        assert second["stats"]["availableCount"] == 0
        assert _node_value(second, AVAIL_NODE)["textContent"] == "0"
        assert _node_value(first, REWARD_NODE)["textContent"] != \
            _node_value(second, REWARD_NODE)["textContent"]


# ════════════════════════════════════════════════════════════════════
# 3. End-to-end — real Flask endpoint + real DB → shipped JS → cards
# ════════════════════════════════════════════════════════════════════

E2E_USER = 9001
E2E_CHANNEL_SLUG = "main"


class _Members:
    """Fake Telegram membership lookup — no live Telegram API call."""

    def __init__(self) -> None:
        self.statuses: dict[int, str] = {}

    def __call__(self, channel_id: int, user_id: int) -> str:
        return self.statuses.get(user_id, "left")


@pytest.fixture
def backend(monkeypatch, tmp_path):
    """The production Mini App backend: real Flask app + isolated DB +
    configured channel verifier (same pattern as test_task_routes)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "home_stats_e2e.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(E2E_USER, "e2e_worker", "E2E")

    members = _Members()
    CHANNELS.clear()
    CHANNELS[E2E_CHANNEL_SLUG] = Channel(
        slug=E2E_CHANNEL_SLUG,
        channel_id=-100222,
        username="taskcoin_e2e",
        title="TaskCoin E2E",
        required=True,
    )
    register_channel_task_verifier(
        ChannelTaskVerifier(membership_checker=members)
    )
    serve_miniapp.app.config["TESTING"] = True
    yield serve_miniapp.app.test_client(), members
    CHANNELS.clear()
    register_channel_task_verifier()  # restore the default registration


def _auth() -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=E2E_USER)}


def _get_payload(client) -> dict:
    """Authenticated GET /api/tasks — the Tasks screen's exact read."""
    response = client.get("/api/tasks", headers=_auth())
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert isinstance(payload.get("tasks"), list)
    return payload


def _seed_tasks() -> dict[str, int]:
    """Active tasks (whole-USDT, sub-cent, channel) + one inactive."""
    plain = db.create_task(
        title="مهمة بسيطة",
        description="وصف المهمة",
        task_type="deterministic",
        reward=2,
        task_data=json.dumps({"expected": "secret"}),
    )
    sub_cent = db.create_task(
        title="مهمة بمكافأة جزئية",
        description="وصف المهمة",
        task_type="deterministic",
        reward=0,
        reward_units=50_000_000,  # exact atomic value stored verbatim
        task_data=json.dumps({"expected": "secret"}),
    )
    channel = db.create_task(
        title="اشترك في القناة الرسمية",
        description="انضم إلى قناة تيليجرام",
        task_type=CHANNEL_TASK_TYPE,
        reward=7,
        task_data=json.dumps({"channel_slug": E2E_CHANNEL_SLUG}),
    )
    inactive = db.create_task(
        title="مهمة موقوفة",
        description="لا تظهر في الكتالوج",
        task_type="deterministic",
        reward=99,
        task_data=json.dumps({"expected": "secret"}),
        active=False,
    )
    return {"plain": plain, "sub_cent": sub_cent,
            "channel": channel, "inactive": inactive}


def _catalog_units() -> int:
    """Expected atomic sum straight from TaskCatalog (the same source
    the endpoint iterates) — independent of the JS under test."""
    from wallet import USDT_SCALE
    from task_catalog import TaskCatalog

    summaries = TaskCatalog().list_available_tasks()
    total = 0
    for summary in summaries:
        if summary.reward_units is not None:
            total += summary.reward_units
        else:
            total += summary.reward * USDT_SCALE
    return total


class TestEndToEndBackendToCards:
    def test_cards_match_the_real_endpoint_and_database(self, backend):
        client, _members = backend
        _seed_tasks()

        payload = _get_payload(client)
        # Backend availability: inactive task excluded, active ones
        # reported with the server-decided status.
        assert len(payload["tasks"]) == 3
        assert all(t["status"] == "available" for t in payload["tasks"])

        # Independent expectation straight from the database/catalog.
        from task_catalog import TaskCatalog

        summaries = TaskCatalog().list_available_tasks()
        assert len(summaries) == 3
        db_units = _catalog_units()
        assert db_units == 950_000_000  # 2 + 0.5 + 7 USDT exactly

        out = _run(payload)
        count, units = _expected(payload)
        assert (count, units) == (3, db_units)
        assert _node_value(out, AVAIL_NODE)["textContent"] == "3"
        assert _node_value(out, REWARD_NODE)["textContent"] == \
            "9.50000000 USDT"

    def test_cards_update_after_a_task_is_executed(self, backend):
        client, members = backend
        ids = _seed_tasks()
        members.statuses[E2E_USER] = "member"

        # Execute the channel task through the real API — the same
        # start + submit calls the Tasks screen makes.
        start = client.post(
            f"/api/tasks/{ids['channel']}/start", headers=_auth()
        )
        assert start.status_code == 200
        assert start.get_json()["ok"] is True
        submit = client.post(
            f"/api/tasks/{ids['channel']}/submit", headers=_auth()
        )
        assert submit.status_code == 200
        assert submit.get_json()["ok"] is True

        payload = _get_payload(client)
        statuses = {t["id"]: t["status"] for t in payload["tasks"]}
        assert statuses[ids["channel"]] == "completed"

        out = _run(payload)
        count, units = _expected(payload)
        assert (count, units) == (2, 250_000_000)  # completed drops out
        assert _node_value(out, AVAIL_NODE)["textContent"] == "2"
        assert _node_value(out, REWARD_NODE)["textContent"] == \
            "2.50000000 USDT"

    def test_cards_update_when_tasks_are_added_and_removed(self, backend):
        client, _members = backend
        ids = _seed_tasks()
        assert len(_get_payload(client)["tasks"]) == 3

        # ➕ Admin publishes a new task → next card read sees it.
        db.create_task(
            title="مهمة جديدة",
            description="أُضيفت للتو",
            task_type="deterministic",
            reward=4,
            task_data=json.dumps({"expected": "secret"}),
        )
        payload = _get_payload(client)
        out = _run(payload)
        count, units = _expected(payload)
        assert (count, units) == (4, 1_350_000_000)
        assert _node_value(out, AVAIL_NODE)["textContent"] == "4"
        assert _node_value(out, REWARD_NODE)["textContent"] == \
            "13.50000000 USDT"

        # ➖ Task deactivated (removed from the catalog) → next read
        # reflects it without touching any stored reward value.
        assert db.update_task(ids["plain"], active=False) is True
        payload = _get_payload(client)
        out = _run(payload)
        count, units = _expected(payload)
        assert (count, units) == (3, 1_150_000_000)
        assert _node_value(out, AVAIL_NODE)["textContent"] == "3"
        assert _node_value(out, REWARD_NODE)["textContent"] == \
            "11.50000000 USDT"
