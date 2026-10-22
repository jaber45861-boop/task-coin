"""
Mini App owner task editing — behavioral UI + API wiring
========================================================

Verifies the shipped ``miniapp/js/task-request.js`` owner-edit feature
against the TESTED server contract (``POST /api/owner/tasks/<id>/edit``,
covered end-to-end by ``test_task_owner_management.py``):

  Rendering (list contract)
  - the edit / pause / archive controls appear ONLY on the caller's
    OWN approved, published, non-archived rows; a paused row swaps the
    edit/pause pair for resume; pending and archived rows carry no
    owner actions at all
  - the owner-action buttons ship styled (app.css)

  Save flow (real fetch choreography, node + stub DOM)
  - the edit button opens owner-edit mode and the form is prefilled
    with the CURRENT title/description; execution fields (platform,
    action, target link, reward) are LOCKED — they never leave the
    client
  - saving POSTs exactly ``{title, description}`` to
    ``/api/owner/tasks/<id>/edit`` with the initData header, and a
    server-confirmed success shows the SERVER's message and reloads
    the list from the server (one extra GET /api/task-requests)
  - a server rejection or a network failure shows the error, keeps
    edit mode and the typed values, and triggers NO reload and NO
    success banner — the UI never claims success the server didn't
    confirm
  - a double click saves EXACTLY ONCE (busy flag + disabled button),
    and empty fields are rejected client-side with zero requests
  - without initData the owner action is refused before any request

  Static contract audits
  - the payload builder in ``_ownerEdit`` carries only the two
    whitelisted fields; success is gated on ``data.ok === true``;
    the URL matches the tested endpoint; the locked-field list never
    includes title/description; owner-edit mode prefills both fields

Run:
    python3 -m pytest test_miniapp_owner_edit_ui.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TASK_REQUEST_JS = Path("miniapp/js/task-request.js")
APP_CSS = Path("miniapp/css/app.css")

LIST_URL = "/api/task-requests"
INIT_DATA_HEADER = "X-Telegram-Init-Data"
INIT_DATA_VALUE = "test-init-data"

EDIT_SEL = '[data-testid="taskreq-owner-edit"]'
SUBMIT_SEL = '[data-testid="taskreq-submit"]'
TITLE_SEL = '[data-testid="taskreq-title"]'
DESC_SEL = '[data-testid="taskreq-description"]'
ERROR_SEL = '[data-testid="taskreq-form-error"]'
CANCEL_SEL = '[data-testid="taskreq-owner-cancel"]'
LIST_SEL = '[data-testid="taskreq-list"]'

EXEC_FIELD_IDS = (
    "taskreq-provider",
    "taskreq-action",
    "taskreq-target",
    "taskreq-reward",
)


def _src() -> str:
    return TASK_REQUEST_JS.read_text(encoding="utf-8")


def _fn_body(name: str) -> str:
    """Source of `function <name>(...) { ... }` (4-space-indented end)."""
    source = _src()
    start = source.index(f"function {name}(")
    end = source.index("\n    }", start)
    return source[start:end]


# ── Request/response fixtures (mirror the server's _trq_safe shape) ──


def _request(**overrides) -> dict:
    request = {
        "request_id": 7,
        "status": "approved",
        "title": "متابعة حسابي على Instagram",
        "description": "تابع الحساب ثم أرسل إثبات المتابعة",
        "provider": "instagram",
        "action": "follow",
        "target_ref": "https://instagram.com/example",
        "reward": "0.5",
        "reward_units": 50_000_000,
        "reason": None,
        "task_id": 101,
        "created_at": 0,
        "updated_at": 0,
        "paused": False,
        "archived": False,
    }
    request.update(overrides)
    return request


def _ok_list(requests: list[dict]) -> dict:
    return {"ok": True, "payload": {"ok": True, "requests": requests}}


def _ok_edit(message: str = "تم تعديل المهمة") -> dict:
    return {
        "ok": True,
        "status": 200,
        "payload": {"ok": True, "message": message,
                    "published_task_id": 101},
    }


def _reject(message: str, error: str = "invalid_payload") -> dict:
    return {
        "ok": False,
        "status": 400,
        "payload": {"ok": False, "error": error, "message": message},
    }


# ════════════════════════════════════════════════════════════════════
# Runtime — node harness with a stub DOM and a scripted fetch
# ════════════════════════════════════════════════════════════════════

# Evaluates the shipped task-request.js byte-for-byte, opens the
# dialog against a SCRIPTED fetch queue (one response per call, in
# order), then runs a choreography: click the edit button, type into
# the fields, click save N times — and reports the full fetch log
# plus every observable UI state.
_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));

const nodes = new Map();
const fetchLog = [];
let responseIdx = 0;
const state = { overlay: null };

function getOrCreate(sel) {
    if (!nodes.has(sel)) {
        const node = {
            tagName: 'div',
            className: '',
            value: '',
            hidden: false,
            disabled: false,
            textContent: '',
            _attrs: Object.create(null),
            _html: '',
            _listeners: Object.create(null),
            setAttribute(name, value) { this._attrs[name] = String(value); },
            getAttribute(name) {
                return name in this._attrs ? this._attrs[name] : null;
            },
            addEventListener(type, fn) {
                (this._listeners[type] = this._listeners[type] || [])
                    .push(fn);
            },
            remove() {},
            querySelector(sel2) { return getOrCreate(sel2); },
            querySelectorAll(sel2) { return [getOrCreate(sel2)]; },
            set innerHTML(value) { this._html = String(value); },
            get innerHTML() { return this._html; },
        };
        nodes.set(sel, node);
    }
    return nodes.get(sel);
}

function dispatch(sel, type) {
    const node = nodes.get(sel);
    const fns = (node && node._listeners && node._listeners[type]) || [];
    for (const fn of fns.slice()) {
        fn.call(node);
    }
    return fns.length;
}

function makeElement(tag) {
    return getOrCreate('<' + tag + ':' + (state.overlay ? 'child' : 'root') + '>');
}

const ctx = {
    console,
    window: {},
    document: {
        createElement: makeElement,
        body: {
            appendChild(el) { state.overlay = el; }
        }
    },
    fetch: async (url, opts) => {
        opts = opts || {};
        fetchLog.push({
            url: String(url),
            method: opts.method || 'GET',
            body: opts.body || null,
            headers: opts.headers || null,
        });
        const spec = input.responses[responseIdx] ||
            input.responses[input.responses.length - 1];
        responseIdx += 1;
        if (spec.networkError) {
            throw new Error('network down');
        }
        return {
            ok: spec.ok,
            status: spec.status || (spec.ok ? 200 : 400),
            json: async () => spec.payload,
        };
    },
};
if (!input.noInitData) {
    ctx.TelegramApp = { getInitData: () => input.initData || '""" + INIT_DATA_VALUE + r"""' };
}
vm.createContext(ctx);
vm.runInContext(
    input.src + '\n;globalThis.__taskreq = TaskRequestUI;',
    ctx
);

(async () => {
    await ctx.__taskreq.open();

    if (input.editTaskId) {
        getOrCreate('""" + EDIT_SEL + r"""')
            .setAttribute('data-published-task-id', String(input.editTaskId));
    }
    if (input.clickEdit) {
        dispatch('""" + EDIT_SEL + r"""', 'click');
    }
    if (input.type) {
        getOrCreate('""" + TITLE_SEL + r"""').value = input.type.title || '';
        getOrCreate('""" + DESC_SEL + r"""').value =
            input.type.description || '';
    }
    for (let i = 0; i < (input.submitClicks || 0); i++) {
        dispatch('""" + SUBMIT_SEL + r"""', 'click');
    }
    await new Promise((resolve) => setTimeout(resolve, 60));

    const error = nodes.get('""" + ERROR_SEL + r"""');
    const submit = nodes.get('""" + SUBMIT_SEL + r"""');
    const cancel = nodes.get('""" + CANCEL_SEL + r"""');
    const execDisabled = {};
    for (const id of [""" + ", ".join(f"'{i}'" for i in EXEC_FIELD_IDS) + r"""]) {
        const node = nodes.get('[data-testid="' + id + '"]');
        execDisabled[id] = node ? node.disabled : null;
    }
    process.stdout.write(JSON.stringify({
        fetchLog,
        formError: error
            ? { text: error.textContent, hidden: error.hidden } : null,
        submitText: submit ? submit.textContent : null,
        cancelHidden: cancel ? cancel.hidden : null,
        execDisabled,
        titleValue: (nodes.get('""" + TITLE_SEL + r"""') || {}).value,
        descValue: (nodes.get('""" + DESC_SEL + r"""') || {}).value,
        overlayHtml: state.overlay ? state.overlay.innerHTML : '',
        listHtml: (nodes.get('""" + LIST_SEL + r"""') || {}).innerHTML || '',
    }));
})().catch((err) => {
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
});
"""


def _run(**scenario) -> dict:
    """Execute the shipped task-request.js through one choreography."""
    if shutil.which("node") is None:
        pytest.skip("node is not available in this environment")
    payload = {"src": _src(), **scenario}
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


def _edit_posts(out: dict) -> list[dict]:
    return [
        call for call in out["fetchLog"]
        if call["method"] == "POST" and "/edit" in call["url"]
    ]


def _list_gets(out: dict) -> list[dict]:
    return [
        call for call in out["fetchLog"]
        if call["method"] == "GET" and call["url"] == LIST_URL
    ]


# ════════════════════════════════════════════════════════════════════
# Rendering — who gets the owner controls
# ════════════════════════════════════════════════════════════════════


class TestOwnerControlsRendering:
    def test_edit_button_only_on_own_published_approved_row(self):
        out = _run(
            responses=[_ok_list([
                _request(request_id=1, status="pending", task_id=None),
                _request(request_id=2, task_id=101),
            ])],
        )
        html = out["listHtml"]
        assert html.count("taskreq-owner-edit") == 1
        assert 'data-published-task-id="101"' in html
        # the pending row carries no owner controls at all
        assert html.count('<div class="taskreq-item"') == 2

    def test_archived_row_has_no_owner_controls(self):
        out = _run(
            responses=[_ok_list([
                _request(task_id=101, archived=True),
            ])],
        )
        assert "taskreq-owner" not in out["listHtml"]

    def test_paused_row_offers_resume_instead_of_edit(self):
        out = _run(
            responses=[_ok_list([
                _request(task_id=101, paused=True),
                _request(request_id=8, task_id=102),
            ])],
        )
        html = out["listHtml"]
        assert html.count("taskreq-owner-resume") == 1
        assert html.count("taskreq-owner-edit") == 1  # only row #2
        assert html.count("taskreq-owner-archive") == 2

    def test_owner_controls_are_styled(self):
        css = APP_CSS.read_text(encoding="utf-8")
        assert ".taskreq-owner-actions" in css
        assert ".taskreq-owner-btn" in css


# ════════════════════════════════════════════════════════════════════
# Save flow — real fetch choreography
# ════════════════════════════════════════════════════════════════════


class TestOwnerEditSaveFlow:
    def test_successful_save_posts_only_allowed_fields_then_reloads(self):
        out = _run(
            responses=[
                _ok_list([_request(task_id=101)]),
                _ok_edit("تم تعديل المهمة"),
                _ok_list([_request(task_id=101,
                                   title="عنوان جديد",
                                   description="وصف جديد")]),
            ],
            editTaskId=101,
            clickEdit=True,
            type={"title": "عنوان جديد", "description": "وصف جديد"},
            submitClicks=1,
        )

        # Exactly: list, edit POST, reload list.
        assert [c["method"] for c in out["fetchLog"]] == [
            "GET", "POST", "GET",
        ]
        post = _edit_posts(out)[0]
        assert post["url"] == "/api/owner/tasks/101/edit"
        sent = json.loads(post["body"])
        assert set(sent) == {"title", "description"}
        assert sent == {"title": "عنوان جديد", "description": "وصف جديد"}
        assert post["headers"]["Content-Type"] == "application/json"
        assert post["headers"][INIT_DATA_HEADER] == INIT_DATA_VALUE
        # A reload happened from the server (one extra list GET).
        assert len(_list_gets(out)) == 2

        # Success is the SERVER's message; edit mode is over.
        assert "taskreq-success" in out["overlayHtml"]
        assert "تم تعديل المهمة" in out["overlayHtml"]
        assert out["formError"]["hidden"] is True
        assert out["formError"]["text"] == ""
        assert out["submitText"] == "إرسال للمراجعة"
        assert out["cancelHidden"] is True
        assert all(flag is False for flag in out["execDisabled"].values())

    def test_server_rejection_never_claims_success_or_reloads(self):
        out = _run(
            responses=[
                _ok_list([_request(task_id=101)]),
                _reject("بيانات التعديل غير صالحة"),
            ],
            editTaskId=101,
            clickEdit=True,
            type={"title": "عنوان مرفوض", "description": "وصف مرفوض"},
            submitClicks=1,
        )

        # No third call: a rejection never triggers the list reload.
        assert len(out["fetchLog"]) == 2
        assert len(_list_gets(out)) == 1

        assert out["formError"]["hidden"] is False
        assert out["formError"]["text"] == "بيانات التعديل غير صالحة"
        assert "taskreq-success" not in out["overlayHtml"]
        # Still in edit mode with the typed values intact.
        assert out["submitText"] == "💾 حفظ التعديلات"
        assert out["cancelHidden"] is False
        assert out["titleValue"] == "عنوان مرفوض"
        assert out["descValue"] == "وصف مرفوض"
        assert all(flag is True for flag in out["execDisabled"].values())

    def test_network_failure_never_claims_success(self):
        out = _run(
            responses=[
                _ok_list([_request(task_id=101)]),
                {"networkError": True},
            ],
            editTaskId=101,
            clickEdit=True,
            type={"title": "عنوان", "description": "وصف"},
            submitClicks=1,
        )
        assert len(out["fetchLog"]) == 2
        assert "taskreq-success" not in out["overlayHtml"]
        assert out["formError"]["hidden"] is False

    def test_double_click_saves_exactly_once(self):
        out = _run(
            responses=[
                _ok_list([_request(task_id=101)]),
                _ok_edit(),
                _ok_list([_request(task_id=101)]),
            ],
            editTaskId=101,
            clickEdit=True,
            type={"title": "عنوان", "description": "وصف"},
            submitClicks=2,
        )
        assert len(_edit_posts(out)) == 1
        assert len(out["fetchLog"]) == 3

    def test_empty_fields_blocked_client_side_with_zero_requests(self):
        out = _run(
            responses=[_ok_list([_request(task_id=101)])],
            editTaskId=101,
            clickEdit=True,
            type={"title": "   ", "description": ""},
            submitClicks=1,
        )
        assert len(out["fetchLog"]) == 1  # the initial list only
        assert out["formError"]["hidden"] is False
        assert out["formError"]["text"] == "أدخل عنوان المهمة ووصفها"
        assert "taskreq-success" not in out["overlayHtml"]

    def test_missing_initdata_refuses_the_owner_action(self):
        out = _run(
            responses=[_ok_list([_request(task_id=101)])],
            editTaskId=101,
            clickEdit=True,
            noInitData=True,
        )
        assert len(out["fetchLog"]) == 1  # nothing was sent
        assert out["formError"]["hidden"] is False
        assert out["formError"]["text"] == "افتح التطبيق من تيليجرام أولاً"

    def test_edit_mode_locks_execution_fields_and_prefills(self):
        out = _run(
            responses=[_ok_list([_request(task_id=101)])],
            editTaskId=101,
            clickEdit=True,
            type={"title": "", "description": ""},
        )
        # Prefilled from the CURRENT cached row before our typing
        # (typing empty strings overwrote them — reload fresh values).
        assert out["execDisabled"] == {
            "taskreq-provider": True,
            "taskreq-action": True,
            "taskreq-target": True,
            "taskreq-reward": True,
        }
        assert out["submitText"] == "💾 حفظ التعديلات"
        assert out["cancelHidden"] is False


# ════════════════════════════════════════════════════════════════════
# Static contract audits (run everywhere, no node needed)
# ════════════════════════════════════════════════════════════════════


class TestOwnerEditStaticContract:
    def test_payload_carries_only_title_and_description(self):
        body = _fn_body("_ownerEdit")
        payload_block = body[body.index("const payload = {"):]
        payload_block = payload_block[:payload_block.index("};") + 1]
        assert "taskreq-title" in payload_block
        assert "taskreq-description" in payload_block
        for forbidden in ("provider", "action", "target_ref", "reward"):
            assert forbidden not in payload_block, forbidden

    def test_success_requires_the_server_ok_flag(self):
        body = _fn_body("_ownerEdit")
        gate = body.index("data.ok !== true")
        success = body.index("_setSuccess(")
        assert gate < success, "success is set before the server gate"

    def test_edit_url_matches_the_tested_endpoint(self):
        body = _fn_body("_ownerEditUrl")
        assert "/api/owner/tasks/" in body
        assert "/edit" in body
        assert "publishedTaskId" in body

    def test_locked_field_list_excludes_title_and_description(self):
        body = _fn_body("_setExecutionFieldsDisabled")
        for field in EXEC_FIELD_IDS:
            assert field in body, field
        assert "taskreq-title" not in body
        assert "taskreq-description" not in body

    def test_owner_edit_mode_prefills_both_editable_fields(self):
        body = _fn_body("_applyOwnerEditMode")
        assert "_setField('taskreq-title', request.title)" in body
        assert "_setField('taskreq-description', request.description)" in body

    def test_edit_and_submit_share_the_busy_guard(self):
        assert "if (busy" in _fn_body("_ownerEdit")
        assert "btn.disabled = true" in _fn_body("_ownerEdit")
        assert "if (busy)" in _fn_body("_submit")
