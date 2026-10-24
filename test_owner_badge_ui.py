"""
Owner badge — behavioral UI test (real tasks.js executed in Node)
=================================================================

Unlike the source-contract suite (test_miniapp_tasks_page.py), this
test EXECUTES the real ``miniapp/js/tasks.js`` inside Node with a
minimal DOM shim and a stubbed ``fetch``, then inspects the element
tree the page actually built:

  - a task with ``is_owner: true``  → a ``task-owner-badge`` element
    is appended inside that card's ``task-actions``
  - a task with ``is_owner: false`` → no badge element exists
  - a task without the field        → no badge element exists

No network, no Telegram, no database: the API response is stubbed
in-process.  Requires ``node`` on PATH (skips with a clear reason
when absent).

Run:
    python3 -m pytest test_owner_badge_ui.py -v
"""

import json
import os
import shutil
import subprocess

import pytest

_TASKS_JS = os.path.join("miniapp", "js", "tasks.js")

# ── The harness: a purpose-built DOM shim (no external dependency) ──
# It implements exactly the surface tasks.js touches: createElement,
# appendChild, setAttribute, addEventListener, textContent, className,
# hidden and a data-testid-scanning querySelector over innerHTML.
_HARNESS = r"""
'use strict';
const fs = require('fs');

const TASKS_JS = fs.readFileSync(process.argv[2], 'utf8');

function makeEl(tag) {
    const el = {
        tagName: tag,
        className: '',
        textContent: '',
        hidden: false,
        disabled: false,
        _children: [],
        _attrs: {},
        _inner: '',
        _selectors: {},
        setAttribute(k, v) { this._attrs[k] = String(v); },
        getAttribute(k) { return this._attrs[k] ?? null; },
        addEventListener() {},
        appendChild(child) { this._children.push(child); return child; },
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
        set(value) { this._inner = String(value); this._selectors = {}; },
    });
    return el;
}

function collect(node, testid, out) {
    if (node.getAttribute && node.getAttribute('data-testid') === testid) {
        out.push(node);
    }
    for (const child of node._children || []) collect(child, testid, out);
}

async function renderTasks(tasksPayload) {
    globalThis.document = { createElement: (tag) => makeEl(tag) };
    globalThis.window = {};
    globalThis.TelegramApp = { getInitData: () => 'stub-init-data' };
    globalThis.fetch = async () => ({
        ok: true,
        json: async () => ({ ok: true, tasks: tasksPayload }),
    });
    // Fresh module instance per case (the IIFE keeps page state).
    const scoped = TASKS_JS + '\n;globalThis.__Tasks = Tasks;\n';
    eval(scoped);
    const page = globalThis.__Tasks.render();
    // Let the async load() settle through the stubbed fetch chain.
    for (let i = 0; i < 5; i++) {
        await new Promise((resolve) => setImmediate(resolve));
    }
    const cards = [];
    const list = page.querySelector('[data-testid="tasks-list"]');
    if (list) collect(list, 'task-card', cards);
    return cards.map((card) => {
        const actions = card.querySelector('[data-testid="task-actions"]');
        const badges = [];
        if (actions) collect(actions, 'task-owner-badge', badges);
        const status =
            card._attrs['data-status'] || null;
        return {
            status: status,
            hasOwnerBadge: badges.length === 1,
            actionChildren: (actions ? actions._children : []).map(
                (child) => child.getAttribute('data-testid')
            ),
        };
    });
}

async function main() {
    const ownerTask = {
        id: 1, title: 't', description: 'd', type: 'deterministic',
        reward: 1, reward_units: null, status: 'available',
        is_owner: true,
    };
    const nonOwnerTask = {
        id: 2, title: 't', description: 'd', type: 'deterministic',
        reward: 1, reward_units: null, status: 'available',
        is_owner: false,
    };
    const legacyTask = {
        id: 3, title: 't', description: 'd', type: 'deterministic',
        reward: 1, reward_units: null, status: 'available',
    };

    const owner = await renderTasks([ownerTask]);
    const nonOwner = await renderTasks([nonOwnerTask]);
    const legacy = await renderTasks([legacyTask]);

    process.stdout.write(JSON.stringify({ owner, nonOwner, legacy }));
}

main().catch((error) => {
    process.stderr.write(String(error && error.stack || error));
    process.exit(1);
});
"""


@pytest.fixture(scope="module")
def rendered():
    """Run the real tasks.js through the Node harness once."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the UI harness")
    harness_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "_owner_badge_harness.js")
    with open(harness_path, "w", encoding="utf-8") as fh:
        fh.write(_HARNESS)
    try:
        proc = subprocess.run(
            [node, harness_path, _TASKS_JS],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        os.remove(harness_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


class TestOwnerBadgeRendering:
    def test_owner_task_renders_exactly_one_badge(self, rendered):
        assert len(rendered["owner"]) == 1
        card = rendered["owner"][0]
        assert card["hasOwnerBadge"] is True
        # The badge sits inside task-actions (action children carry
        # the badge testid plus the start button for this status).
        assert "task-owner-badge" in card["actionChildren"]
        assert card["status"] == "available"

    def test_non_owner_task_renders_no_badge(self, rendered):
        assert len(rendered["nonOwner"]) == 1
        card = rendered["nonOwner"][0]
        assert card["hasOwnerBadge"] is False
        assert "task-owner-badge" not in card["actionChildren"]
        # Non-owner behavior is unchanged: the start button remains.
        assert "task-start" in card["actionChildren"]

    def test_task_without_the_field_renders_no_badge(self, rendered):
        """A payload lacking is_owner (e.g. cached old API) shows
        no badge — never a default ownership claim."""
        assert len(rendered["legacy"]) == 1
        card = rendered["legacy"][0]
        assert card["hasOwnerBadge"] is False
        assert "task-start" in card["actionChildren"]
