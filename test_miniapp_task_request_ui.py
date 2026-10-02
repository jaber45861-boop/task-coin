"""
Mini App «إضافة مهمة ➕» — task-request UI escaping (XSS defense)
=================================================================

Security contract of ``miniapp/js/task-request.js``: server data may
never reach the DOM as markup.

Static audit
- every ``${...}`` interpolation in the file is either wrapped in
  ``_esc(...)`` or is on the explicit known-safe list (helpers that
  escape internally, literal blocks, selector/URL/textContent sinks)
- the status-chip ``label`` (whose fallback is the RAW server
  ``status``) is escaped at the injection point — defense-in-depth,
  independent of any backend constraint on the value
- dynamic text that has a dedicated node uses ``textContent``; no
  ``insertAdjacentHTML`` / ``document.write`` / ``eval`` exists

Runtime (node + stub DOM, same convention as
``test_miniapp_account_linking.py``; skipped cleanly when node is
missing — the static audit still runs everywhere)
- an XSS payload in ``status`` / ``title`` / ``reason`` renders as
  inert entity-encoded text: no executable tag, no broken markup
- normal values (Arabic labels, the four known status chips, plain
  titles/reasons) render exactly as before
- a server error message containing HTML is escaped too

Run:
    python3 -m pytest test_miniapp_task_request_ui.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TASK_REQUEST_JS = Path("miniapp/js/task-request.js")


def _src() -> str:
    return TASK_REQUEST_JS.read_text(encoding="utf-8")


def _fn_body(name: str) -> str:
    """Source of `function <name>(...) { ... }` (4-space-indented end)."""
    source = _src()
    start = source.index(f"function {name}(")
    end = source.index("\n    }", start)
    return source[start:end]


# Interpolations that are NOT wrapped in _esc() but are safe by
# construction. Anything else in the file must carry _esc(...) —
# new unescaped interpolations fail the audit until they are either
# escaped or consciously added here (a visible review decision).
_SAFE_INTERPOLATIONS = {
    "_cardStart('إضافة مهمة')",  # helper escapes its title argument
    "justSubmittedBlock",         # literal block — no server data, no ${}
    "chipClass",                  # built from _esc(r.status)
    "reason",                     # built only from _esc(r.reason)
    "testid",                     # code-supplied selector, never server data
    "request.reason",             # assigned to textContent, never HTML
    "LIST_URL",                   # fetch URL, not an HTML sink
    "editRequestId",              # fetch URL, not an HTML sink
}


# ════════════════════════════════════════════════════════════════════
# Static audit
# ════════════════════════════════════════════════════════════════════


class TestStaticEscapingAudit:
    def test_every_interpolation_is_escaped_or_known_safe(self):
        bodies = re.findall(r"\$\{([^{}]*)\}", _src())
        assert bodies, (
            "no interpolations found — the audit pattern needs updating"
        )
        unescaped = [
            b for b in bodies
            if "_esc(" not in b and b not in _SAFE_INTERPOLATIONS
        ]
        assert unescaped == [], (
            f"interpolations reaching markup without _esc(): {unescaped}"
        )

    def test_status_label_fallback_is_escaped_at_injection(self):
        """`label` falls back to the RAW server status when the value
        is unknown — it must be escaped where it enters the markup."""
        source = _src()
        assert "${_esc(label)}" in source
        assert not re.search(r"\$\{\s*label\s*\}", source), \
            "raw ${label} interpolation found"
        # The lookup itself (and therefore the rendered text of the
        # four known statuses) is unchanged.
        assert "STATUS_LABELS[r.status] || r.status" in source

    def test_esc_helper_escapes_all_five_entities(self):
        body = _fn_body("_esc")
        for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            assert entity in body, f"_esc() no longer emits {entity}"

    def test_card_start_escapes_its_title(self):
        assert "_esc(title)" in _fn_body("_cardStart")

    def test_message_renderer_escapes_its_argument(self):
        # _renderMessage receives server error messages.
        assert "_esc(message)" in _fn_body("_renderMessage")

    def test_just_submitted_block_contains_no_interpolation(self):
        match = re.search(
            r"const justSubmittedBlock = justSubmitted \? `(.*?)` :",
            _src(),
            re.DOTALL,
        )
        assert match, "justSubmittedBlock template not found"
        assert "${" not in match.group(1)

    def test_dynamic_text_uses_text_content_and_never_raw_sinks(self):
        source = _src()
        # The nodes that receive server/state text use textContent.
        assert "note.textContent" in source
        assert "box.textContent" in source
        # No alternative HTML-injection sinks anywhere in the file.
        assert "insertAdjacentHTML" not in source
        assert "document.write" not in source
        assert not re.search(r"\beval\s*\(", source)
        assert not re.search(r"new\s+Function\s*\(", source)


# ════════════════════════════════════════════════════════════════════
# Runtime — node harness with a stub DOM
# ════════════════════════════════════════════════════════════════════

# Evaluates the shipped task-request.js byte-for-byte, opens the
# dialog against a stubbed fetch, and prints the rendered HTML of the
# overlay and of the request list.
_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = new Map();
const state = { overlay: null };

function makeElement(tag) {
    return {
        tagName: tag,
        className: '',
        value: '',
        hidden: false,
        disabled: false,
        textContent: '',
        _attrs: Object.create(null),
        children: [],
        _html: '',
        setAttribute(name, value) { this._attrs[name] = String(value); },
        getAttribute(name) {
            return name in this._attrs ? this._attrs[name] : null;
        },
        appendChild(child) { this.children.push(child); return child; },
        set innerHTML(value) { this._html = String(value); },
        get innerHTML() { return this._html; },
        addEventListener() {},
        remove() {},
        querySelector(sel) {
            if (!nodes.has(sel)) nodes.set(sel, makeElement('div'));
            return nodes.get(sel);
        }
    };
}

const ctx = {
    console,
    window: {},
    document: {
        createElement: makeElement,
        body: { appendChild(el) { state.overlay = el; } }
    },
    fetch: async () => ({
        ok: input.response.ok,
        json: async () => input.response.payload
    })
};
vm.createContext(ctx);
vm.runInContext(
    input.src + '\n;globalThis.__taskreq = TaskRequestUI;',
    ctx
);

ctx.__taskreq.open().then(() => {
    const list = nodes.get('[data-testid="taskreq-list"]');
    process.stdout.write(JSON.stringify({
        overlayHtml: state.overlay ? state.overlay.innerHTML : '',
        listHtml: list ? list.innerHTML : '',
        editNote: (nodes.get('[data-testid="taskreq-editnote"]') || {})
            .textContent || ''
    }));
}).catch((err) => {
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
});
"""


def _run(response: dict) -> dict:
    """Execute the shipped task-request.js against a stubbed fetch."""
    if shutil.which("node") is None:
        pytest.skip("node is not available in this environment")
    payload = {"src": _src(), "response": response}
    result = subprocess.run(
        ["node", "-e", _HARNESS_JS],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, (
        f"task-request.js failed to evaluate:\n{result.stderr}"
    )
    return json.loads(result.stdout)


def _ok_with(requests: list[dict]) -> dict:
    return {"ok": True, "payload": {"ok": True, "requests": requests}}


def _request(**overrides) -> dict:
    request = {
        "request_id": 1,
        "status": "pending",
        "title": "طلب عادي",
        "description": "وصف عادي للمهمة",
        "provider": "instagram",
        "action": "follow",
        "target_ref": "https://instagram.com/example",
        "reward": "0.5",
        "reward_units": 50_000_000,
        "reason": None,
        "task_id": None,
        "created_at": 0,
        "updated_at": 0,
    }
    request.update(overrides)
    return request


class TestRuntimeEscaping:
    def test_xss_payload_never_becomes_executable_markup(self):
        """Payloads in status (unknown → raw fallback), title and
        reason render entity-encoded; markup structure survives."""
        out = _run(_ok_with([
            _request(
                request_id=1,
                status='<img src=x onerror="alert(1)">',
                title="<script>alert(2)</script>",
                reason='"><svg onload=alert(3)>',
            ),
            _request(request_id=2, status="pending", title="طلب عادي"),
        ]))
        html = out["listHtml"]

        # No executable tag ever appears in the markup.
        assert "<img" not in html
        assert "<script" not in html
        assert "<svg" not in html

        # The payloads are present — as inert text.
        assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
        assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html
        assert "&quot;&gt;&lt;svg onload=alert(3)&gt;" in html
        assert ">طلب عادي<" in html  # the clean item survived untouched

        # Markup is not broken: exactly two well-formed items.
        assert html.count('<div class="taskreq-item"') == 2
        assert html.count("</div>") == 2
        # The hostile status stayed inside its quoted attribute.
        assert 'data-status="&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"' \
            in html

        # The dialog itself still rendered (form + submit reachable).
        assert 'data-testid="taskreq-title"' in out["overlayHtml"]
        assert 'data-testid="taskreq-submit"' in out["overlayHtml"]

    def test_normal_values_render_exactly_as_before(self):
        """The four known status chips, plain titles and reasons are
        byte-identical after escaping (escaping is a no-op for them)."""
        out = _run(_ok_with([
            _request(request_id=1, status="pending"),
            _request(request_id=2, status="approved"),
            _request(request_id=3, status="rejected",
                     reason="عدم الالتزام بالوصف"),
            _request(request_id=4, status="changes_requested",
                     reason="عدّل الوصف ثم أعد الإرسال"),
        ]))
        html = out["listHtml"]

        assert ">⏳ قيد المراجعة<" in html
        assert ">✅ منشورة<" in html
        assert ">❌ مرفوضة<" in html
        assert ">✏️ تحتاج إلى تعديل<" in html
        assert ">طلب عادي<" in html
        assert "عدم الالتزام بالوصف" in html
        assert "عدّل الوصف ثم أعد الإرسال" in html
        # No entity-mangling of normal content.
        assert "&amp;" not in html
        assert html.count('<div class="taskreq-item"') == 4

        # edits-preflow: the changes_requested item prefills the note
        # through textContent (server reason appended as text).
        assert "عدّل الوصف ثم أعد الإرسال" in out["editNote"]

    def test_server_error_message_is_escaped(self):
        out = _run({
            "ok": False,
            "payload": {
                "ok": False,
                "error": "server_error",
                "message": "<b>انفجار</b> في الخادم",
            },
        })
        html = out["overlayHtml"]
        assert "&lt;b&gt;انفجار&lt;/b&gt; في الخادم" in html
        assert "<b>" not in html
        # The message card still rendered with its retry control.
        assert 'data-testid="taskreq-retry"' in html
