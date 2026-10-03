"""
Official Guide («الدليل الرسمي 📖») — Mini App
=============================================

Contract of the guide feature:
- pressing the Home «الدليل الرسمي» section opens the guide dialog
- ``miniapp/js/guide.js`` renders the COMPLETE official guide,
  verbatim (no line may be shortened or reworded)
- the dialog reuses the existing overlay mechanics: no second
  router, no fetch, no auth, no feature-flag change
- the document stays ``dir="rtl"`` and the dialog inherits it
- the guide is purely presentational

Run:
    python3 -m pytest test_miniapp_guide.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOME_JS = Path("miniapp/js/home.js")
GUIDE_JS = Path("miniapp/js/guide.js")
INDEX_HTML = Path("miniapp/index.html")
APP_CSS = Path("miniapp/css/app.css")


def _home() -> str:
    return HOME_JS.read_text(encoding="utf-8")


def _guide() -> str:
    return GUIDE_JS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _css() -> str:
    return APP_CSS.read_text(encoding="utf-8")


def _guide_builder_block() -> str:
    """Source of the guide builder, from its testid to the next builder."""
    content = _home()
    start = content.find("home-guide")
    assert start != -1, "guide section missing from home.js"
    end = content.find("_buildAddTaskSection", start)
    return content[start:end if end != -1 else start + 1500]


# The official guide, line by line. The shipped guide must contain
# every one of these, verbatim — nothing may be summarised.
OFFICIAL_LINES = (
    "📖 الدليل الرسمي — Market Task",
    "مرحبًا بك في Market Task 👋",
    "هنا يمكنك متابعة المهام وإضافة مهامك وإدارة طلباتك بسهولة من خلال الـMini App.",
    "🏠 1. الرئيسية",
    "من الصفحة الرئيسية ستجد أهم المعلومات الخاصة بحسابك، بالإضافة إلى:",
    "👤 معلومات حسابك ومستواك.",
    "💰 المحفظة والرصيد.",
    "➕ إضافة مهمة.",
    "🔥 المهام الساخنة والمتاحة.",
    "➕ 2. إضافة مهمة",
    "اضغط على «إضافة مهمة» لفتح نموذج المهمة.",
    "أدخل بيانات المهمة المطلوبة بشكل واضح، ثم أرسل الطلب.",
    "بعد الإرسال يتم تسجيل طلب المهمة ومراجعته وفق نظام المنصة.",
    "📋 3. متابعة المهام",
    "من قسم «المهام» يمكنك متابعة المهام المتاحة والمهام التي يمكنك تنفيذها.",
    "اقرأ تفاصيل المهمة وشروطها جيدًا قبل البدء.",
    "🔥 4. المهام الساخنة",
    "هذا القسم يعرض المهام التي يتم إبرازها للمستخدمين.",
    "تابع القسم باستمرار لمعرفة المهام المتاحة لك.",
    "👛 5. المحفظة",
    "المحفظة مخصصة لعرض بيانات رصيدك والمعاملات المرتبطة بحسابك.",
    "«ملاحظة: ظهور بعض بيانات الرصيد أو المكافآت يعتمد على الأنظمة والخصائص المفعّلة في المنصة.»",
    "📝 6. طلبات المهام",
    "عند إرسال مهمة جديدة، قد يمر الطلب بعدة حالات أثناء المراجعة:",
    "🟡 قيد المراجعة.",
    "🟢 تمت الموافقة عليه.",
    "🔴 تم رفضه.",
    "🔄 يحتاج إلى تعديلات وإعادة إرسال.",
    "إذا طُلب منك تعديل الطلب، قم بتحديث البيانات المطلوبة ثم أعد إرساله.",
    "🔐 7. الأمان وتسجيل الدخول",
    "استخدم الـMini App من خلال Telegram فقط، ولا تشارك بيانات حسابك أو أي رموز وصول مع أي شخص.",
    "إذا ظهرت رسالة «تعذر الاتصال بالخادم»، تأكد من فتح التطبيق من داخل Telegram ثم اضغط «إعادة المحاولة».",
    "💡 نصائح مهمة",
    "اقرأ تفاصيل المهمة قبل إرسالها.",
    "تأكد من صحة البيانات قبل الإرسال.",
    "لا ترسل معلومات حساسة داخل وصف المهمة.",
    "تابع حالة طلباتك من قسم المهام.",
    "في حالة وجود مشكلة، أعد فتح التطبيق وحاول مرة أخرى.",
    "🚀 ابدأ الآن",
    "ارجع إلى الرئيسية واضغط:",
    "لبدء إنشاء أول طلب لك.",
)


# ══════════════════════════════════════════════════════════════════
# 1. Home guide section opens the dialog
# ══════════════════════════════════════════════════════════════════


class TestHomeSectionOpensGuide:
    def test_guide_section_still_present(self):
        assert "home-guide" in _home()

    def test_guide_section_has_open_button(self):
        block = _guide_builder_block()
        assert 'data-testid="guide-open"' in block, \
            "guide section should offer an open button"

    def test_pressing_section_opens_guide(self):
        block = _guide_builder_block()
        assert "Guide.open()" in block, \
            "guide section must open the guide dialog"

    def test_coming_soon_placeholder_is_gone(self):
        block = _guide_builder_block()
        assert "قريباً" not in block, \
            "the guide placeholder must be gone once the guide ships"

    def test_guide_script_loaded_before_app(self):
        html = _html()
        guide_pos = html.find('<script src="js/guide.js">')
        app_pos = html.find('<script src="js/app.js">')
        home_pos = html.find('<script src="js/home.js">')
        assert guide_pos != -1, "guide.js must be loaded by index.html"
        assert home_pos != -1 and guide_pos > home_pos, \
            "guide.js loads after home.js"
        assert app_pos != -1 and guide_pos < app_pos, \
            "guide.js must load before app.js"


# ══════════════════════════════════════════════════════════════════
# 2. The official content ships complete and verbatim
# ══════════════════════════════════════════════════════════════════


class TestOfficialContent:
    def test_guide_module_exists(self):
        assert GUIDE_JS.exists(), "miniapp/js/guide.js not found"

    @pytest.mark.parametrize("line", OFFICIAL_LINES)
    def test_official_line_present_verbatim(self, line):
        assert line in _guide(), f"guide line missing or reworded: {line}"

    def test_all_seven_sections_present(self):
        content = _guide()
        for marker in (
            "guide-block-home", "guide-block-add-task", "guide-block-tasks",
            "guide-block-hot", "guide-block-wallet", "guide-block-requests",
            "guide-block-security", "guide-block-tips", "guide-block-start",
        ):
            assert f'data-testid="{marker}"' in content, f"missing {marker}"

    def test_dialog_title_is_the_full_guide_title(self):
        assert "📖 الدليل الرسمي — Market Task" in _guide()


# ══════════════════════════════════════════════════════════════════
# 3. Reuse of the existing architecture (no new system)
# ══════════════════════════════════════════════════════════════════


class TestArchitectureReuse:
    def test_module_exports_open_and_close(self):
        content = _guide()
        assert "function open()" in content
        assert "function close()" in content
        assert "return { open, close }" in content

    def test_overlay_close_button_exists(self):
        assert 'data-testid="guide-close"' in _guide()

    def test_backdrop_click_closes(self):
        assert "event.target === overlay" in _guide()

    def test_reuses_existing_haptic_helper(self):
        content = _guide()
        assert "HapticFeedback" in content
        assert "impactOccurred('light')" in content

    def test_no_network_calls(self):
        content = _guide()
        for banned in ("fetch(", "XMLHttpRequest", "axios", "$.ajax"):
            assert banned not in content, f"{banned} found in guide.js"

    def test_home_still_makes_no_network_calls(self):
        content = _home()
        assert "fetch(" not in content
        assert "XMLHttpRequest" not in content

    def test_no_second_router(self):
        content = _guide()
        assert "pushState" not in content and "window.history" not in content

    def test_daily_registration_flag_untouched(self):
        # the flag may only be mentioned in prose, never declared or
        # assigned inside the guide module
        assert "SHOW_DAILY_REGISTRATION =" not in _guide()
        assert "const SHOW_DAILY_REGISTRATION = false;" in _home()
        assert "SHOW_DAILY_REGISTRATION) {" not in _guide()

    def test_document_is_rtl(self):
        html = _html()
        assert 'dir="rtl"' in html, "document must stay RTL"
        assert 'lang="ar"' in html

    def test_guide_styles_exist(self):
        css = _css()
        for rule in (".guide-overlay", ".guide-dialog", ".guide-doc",
                     ".guide-doc {", ".guide-open"):
            assert rule in css, f"CSS rule '{rule}' missing"
        # overlay must sit above the page content on mobile
        assert "position: fixed" in css[css.find(".guide-overlay"):]


# ══════════════════════════════════════════════════════════════════
# 4. Runtime (node + stub DOM) — same convention as the other
#    Mini App UI tests; skipped cleanly when node is missing.
# ══════════════════════════════════════════════════════════════════

_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');

const src = fs.readFileSync(0, 'utf8');
const script = process.argv[1] || 'Guide.open();';

function makeElement(tag) {
    return {
        tagName: tag,
        className: '',
        _attrs: Object.create(null),
        _html: '',
        _listeners: Object.create(null),
        _child: null,
        setAttribute(name, value) { this._attrs[name] = String(value); },
        getAttribute(name) {
            return name in this._attrs ? this._attrs[name] : null;
        },
        addEventListener(type, fn) {
            (this._listeners[type] = this._listeners[type] || []).push(fn);
        },
        removeEventListener() {},
        remove() { this._removed = true; },
        querySelector() {
            if (!this._child) { this._child = makeElement('button'); }
            return this._child;
        },
        set innerHTML(value) { this._html = String(value); },
        get innerHTML() { return this._html; }
    };
}

const document = {
    _attached: null,
    createElement: (tag) => makeElement(tag),
    body: { appendChild(el) { document._attached = el; } },
    addEventListener() {},
    removeEventListener() {}
};
const window = {};

const ctx = { document, window, console };
vm.createContext(ctx);
vm.runInContext(src + '\n;' + script, ctx);

const overlay = document._attached;
process.stdout.write(JSON.stringify({
    attached: overlay !== null,
    removed: !!(overlay && overlay._removed),
    testid: overlay ? overlay.getAttribute('data-testid') : null,
    same: typeof ctx.__same === 'undefined' ? null : ctx.__same,
    html: overlay ? overlay.innerHTML : ''
}));
"""


def _run(script: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not available in this environment")
    result = subprocess.run(
        ["node", "-e", _HARNESS_JS, script],
        input=_guide(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"guide.js failed to evaluate:\n{result.stderr}"
    )
    return json.loads(result.stdout)


class TestRuntimeRendering:
    def test_open_renders_the_full_guide(self):
        out = _run("Guide.open();")
        assert out["attached"] is True
        assert out["testid"] == "guide-overlay"
        for line in OFFICIAL_LINES:
            assert line in out["html"], f"not rendered: {line}"

    def test_close_removes_the_overlay(self):
        out = _run("Guide.open(); Guide.close();")
        assert out["removed"] is True

    def test_open_is_idempotent(self):
        out = _run(
            "Guide.open(); const first = document._attached;"
            " Guide.open(); __same = (first === document._attached);"
        )
        # second open() must be a no-op — the same overlay stays attached
        assert out["attached"] is True
        assert out["same"] is True
