"""
Focused tests — Mini App «ربط الحسابات» feature flag (hidden by default)
=======================================================================

The Account Linking section on Home (YouTube card + «ربط YouTube»
button) is now controlled by ONE clear flag in ``miniapp/js/home.js``:

    const SHOW_ACCOUNT_LINKING = false;   ← default: hidden

Guarantees proven here:

  A. FLAG (1-4)
     1  the flag exists and is declared exactly once
     2  default value is ``false`` → section hidden out of the box
     3  the section append is gated inside ``if (SHOW_ACCOUNT_LINKING)``
     4  the gated append appears exactly once (no ungated fallback)

  B. RUNTIME — executed with node against a stub DOM (5-8)
     5  flag off → the section element is NOT rendered
     6  flag off → no leftover container/gap of any kind remains,
        and every other Home section still renders in its original order
     7  flag on  → the section renders in its original position
        (between «إضافة مهمة» and «المهام الساخنة») with the exact
        current markup (title, YouTube row, «ربط YouTube», statuses)
     8  flag on  → surrounding sections are byte-identical to flag off

  C. FUNCTIONALITY NOT DELETED (9-13)
     9  the ``_buildAccountLinkingSection`` builder + full markup remain
     10 ``miniapp/js/social.js`` module (SocialAccounts.attach /
        connectYouTube / refresh, OAuth endpoints, initData auth) intact
     11 social.js still loads before home.js in index.html
     12 the linking CSS rules remain in app.css (design restorable)
     13 «إضافة مهمة»، «المهام السائقة» and the 3-item bottom navigation
        are untouched

If ``node`` is unavailable the runtime tests skip cleanly (same
convention as the git-based skip in test_social_youtube.py); the static
assertions still run everywhere.

Run:
    python3 -m pytest test_miniapp_account_linking.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HOME_JS = Path("miniapp/js/home.js")
SOCIAL_JS = Path("miniapp/js/social.js")
INDEX_HTML = Path("miniapp/index.html")
APP_CSS = Path("miniapp/css/app.css")

FLAG_NAME = "SHOW_ACCOUNT_LINKING"
FLAG_DEFAULT = f"const {FLAG_NAME} = false;"


def _home_js() -> str:
    return HOME_JS.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════
# A. FLAG (1-4)
# ══════════════════════════════════════════════════════════════════

class TestFlagDefinition:
    """The feature flag exists, defaults to hidden, and gates the section."""

    def test_1_flag_declared_exactly_once(self):
        """Exactly one declaration — changing ONE value is enough."""
        content = _home_js()
        assert content.count(f"const {FLAG_NAME} =") == 1, \
            f"{FLAG_NAME} must be declared exactly once in home.js"

    def test_2_flag_defaults_to_false(self):
        """Default value is false → section hidden out of the box."""
        assert FLAG_DEFAULT in _home_js(), \
            f"{FLAG_NAME} must default to false (section hidden)"

    def test_3_append_gated_by_flag(self):
        """The linking section is appended only inside the flag guard."""
        content = _home_js()
        guarded = re.search(
            rf"if \({FLAG_NAME}\) \{{\s*"
            r"page\.appendChild\(_buildAccountLinkingSection\(\)\);\s*\}",
            content,
        )
        assert guarded, (
            "page.appendChild(_buildAccountLinkingSection()) must sit "
            f"inside if ({FLAG_NAME}) {{ ... }}"
        )

    def test_4_gated_append_is_the_only_one(self):
        """No ungated duplicate append of the linking section exists."""
        content = _home_js()
        assert content.count(
            "page.appendChild(_buildAccountLinkingSection());"
        ) == 1, "Exactly one (gated) append of the linking section expected"


# ══════════════════════════════════════════════════════════════════
# B. RUNTIME — node harness (5-8)
# ══════════════════════════════════════════════════════════════════

# Minimal DOM stub: evaluates the shipped home.js source and prints the
# rendered page's direct children as [{testid, className, html}].
_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');

const src = fs.readFileSync(0, 'utf8');

function makeElement(tag) {
    return {
        tagName: tag,
        className: '',
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
        querySelector() { return { addEventListener() {} }; }
    };
}

const ctx = { document: { createElement: makeElement }, console };
vm.createContext(ctx);
vm.runInContext(src + '\n;__page = Home.render();', ctx);

const sections = ctx.__page.children.map((child) => ({
    testid: child.getAttribute('data-testid'),
    className: child.className,
    html: child._html
}));
process.stdout.write(JSON.stringify(sections));
"""

# Section order when the flag is off: the linking slot is simply absent.
_ORDER_OFF = [
    "home-welcome",
    "home-balance",
    "home-checkin",
    "home-guide",
    "home-add-task",
    "home-hot-tasks",
]

# Section order when the flag is on: original 7-section layout.
_ORDER_ON = [
    "home-welcome",
    "home-balance",
    "home-checkin",
    "home-guide",
    "home-add-task",
    "home-account-linking",
    "home-hot-tasks",
]

_LINKING_MARKERS = (
    "ربط الحسابات",
    "social-youtube-row",
    "YouTube",
    "social-youtube-connect",
    "ربط YouTube",
    "غير مرتبط",
    "منصات أخرى قريباً",
)


def _render_home(flag_on: bool) -> list[dict]:
    """Execute home.js in node (stub DOM) with the flag forced to the
    requested value; return the rendered sections."""
    if shutil.which("node") is None:
        pytest.skip("node is not available in this environment")

    source = _home_js()
    assert FLAG_DEFAULT in source, \
        "flag default not found — flag test needs updating"
    if flag_on:
        source = source.replace(
            FLAG_DEFAULT,
            f"const {FLAG_NAME} = true;",
            1,
        )
    # Sanity: flag-off runs the shipped file byte-for-byte.
    if not flag_on:
        assert source == _home_js()

    result = subprocess.run(
        ["node", "-e", _HARNESS_JS],
        input=source,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"home.js failed to evaluate:\n{result.stderr}"
    )
    return json.loads(result.stdout)


class TestRuntimeRendering:
    """Real evaluation of home.js proves hidden-by-default / shown-by-flag."""

    def test_5_section_hidden_by_default(self):
        """Flag off (shipped default): no linking section is rendered."""
        sections = _render_home(flag_on=False)
        testids = [s["testid"] for s in sections]
        assert "home-account-linking" not in testids, \
            "Account Linking section must be hidden by default"

    def test_6_no_leftover_container_and_order_preserved(self):
        """Flag off: not a trace of the section (no gap/container) and
        every other section keeps its original render order."""
        sections = _render_home(flag_on=False)
        testids = [s["testid"] for s in sections]
        assert testids == _ORDER_OFF, f"section order changed: {testids}"

        blob = json.dumps(sections, ensure_ascii=False)
        assert "account-linking" not in blob, \
            "leftover account-linking container/gap found"
        assert "ربط YouTube" not in blob, \
            "YouTube button leaked while flag is off"
        # «إضافة مهمة» and «المهام السائقة» still present
        assert "إضافة مهمة" in blob
        assert "المهام الساخنة" in blob

    def test_7_flag_on_renders_section_in_place(self):
        """Flag on: the section returns in its original position with
        the exact current design/markup."""
        sections = _render_home(flag_on=True)
        testids = [s["testid"] for s in sections]
        assert testids == _ORDER_ON, \
            f"flag-on section order must match the original: {testids}"

        section = next(
            s for s in sections if s["testid"] == "home-account-linking"
        )
        assert section["className"] == "home-section home-account-linking"
        for marker in _LINKING_MARKERS:
            assert marker in section["html"], \
                f"flag-on markup missing '{marker}'"

    def test_8_other_sections_identical_with_flag_on(self):
        """Toggling the flag changes nothing but the linking section."""
        off = {s["testid"]: s for s in _render_home(flag_on=False)}
        on = {s["testid"]: s for s in _render_home(flag_on=True)}
        for testid, off_section in off.items():
            assert on[testid] == off_section, \
                f"section '{testid}' changed when toggling the flag"


# ══════════════════════════════════════════════════════════════════
# C. FUNCTIONALITY NOT DELETED (9-13)
# ══════════════════════════════════════════════════════════════════

class TestLinkingFunctionalityPreserved:
    """Hiding must never delete the linking feature itself."""

    def test_9_builder_and_full_markup_still_present(self):
        """The builder function and every piece of its markup remain."""
        content = _home_js()
        assert "_buildAccountLinkingSection" in content
        assert "section.setAttribute('data-testid', 'home-account-linking')" \
            in content
        assert 'data-testid="social-youtube-row"' in content
        assert 'data-testid="social-youtube-provider"' in content
        assert 'data-testid="social-youtube-state"' in content
        assert 'data-testid="social-youtube-connect"' in content
        for marker in _LINKING_MARKERS:
            assert marker in content, f"markup '{marker}' was deleted"

    def test_10_social_js_module_intact(self):
        """social.js still defines the whole linking module."""
        js = SOCIAL_JS.read_text(encoding="utf-8")
        assert "const SocialAccounts" in js
        for symbol in ("attach", "connectYouTube", "refresh"):
            assert f"function {symbol}" in js, f"{symbol}() was deleted"
        assert "/api/social/youtube/connect" in js
        assert "/api/social/accounts" in js
        assert "TelegramApp.getInitData" in js
        assert "X-Telegram-Init-Data" in js
        assert "HapticFeedback" in js

    def test_10b_social_js_module_still_evaluates(self):
        """Runtime: SocialAccounts exports its full API (not stubbed out)."""
        if shutil.which("node") is None:
            pytest.skip("node is not available in this environment")
        js = SOCIAL_JS.read_text(encoding="utf-8")
        script = (
            js
            + "\n;__api = Object.keys(SocialAccounts).sort();"
        )
        result = subprocess.run(
            ["node", "-e", (
                "const vm=require('vm');const src=require('fs')"
                ".readFileSync(0,'utf8');const ctx={};"
                "vm.createContext(ctx);vm.runInContext(src,ctx);"
                "process.stdout.write(JSON.stringify(ctx.__api));"
            )],
            input=script,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [
            "attach", "connectYouTube", "refresh"
        ]

    def test_11_index_still_loads_social_before_home(self):
        """index.html wiring untouched: social.js before home.js."""
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert 'src="js/social.js"' in html
        assert 'src="js/home.js"' in html
        assert html.index('src="js/social.js"') < html.index(
            'src="js/home.js"'
        )

    def test_12_linking_css_rules_remain(self):
        """The design tokens for the section remain, so flag-on
        restores the exact current look."""
        css = APP_CSS.read_text(encoding="utf-8")
        assert ".social-connect-btn" in css
        assert ".social-account-row" in css
        assert ".account-linking-header" in css
        assert ".account-linking-body" in css
        assert "--neon-red" in css

    def test_13_untouched_areas_preserved(self):
        """«إضافة مهمة»، «المهام السائقة» and the bottom navigation are
        unchanged by the flag work."""
        content = _home_js()
        # Add Task + Hot Tasks appends still unconditional (flag only
        # wraps the linking append — see TestFlagDefinition#3).
        assert content.count(
            "page.appendChild(_buildAddTaskSection());"
        ) == 1
        assert content.count(
            "page.appendChild(_buildHotTasksSection());"
        ) == 1
        assert 'data-testid="add-task-cta"' in content
        assert 'data-testid="hot-tasks-list"' in content

        html = INDEX_HTML.read_text(encoding="utf-8")
        assert html.count('class="nav-item') == 3
        assert html.count("data-page=") == 3
        for label in ("الرئيسية", "المهام", "حسابي"):
            assert label in html
