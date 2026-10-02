"""
Focused tests — Mini App «التسجيل اليومي» feature flag (hidden by default)
==========================================================================

The Daily Registration section on Home (📅 card with the «قريباً»
status) is now controlled by ONE clear flag in ``miniapp/js/home.js``:

    const SHOW_DAILY_REGISTRATION = false;   ← default: hidden

Guarantees proven here:

  A. FLAG (1-4)
     1  the flag exists and is declared exactly once
     2  default value is ``false`` → section hidden out of the box
     3  the section append is gated inside ``if (SHOW_DAILY_REGISTRATION)``
     4  the gated append appears exactly once (no ungated fallback)

  B. RUNTIME — executed with node against a stub DOM (5-9)
     5  flag off → the section element is NOT rendered
     6  flag off → no leftover container/gap/placeholder of any kind
        remains, and every other Home section still renders in its
        original order
     7  flag on  → the section renders in its original position
        (between the balance card and «الدليل الرسمي») with the exact
        original markup (title, 📅 icon, status «قريباً»)
     8  flag on  → surrounding sections are byte-identical to flag off
     9  flag on  → nothing but the daily-registration slot changes
        (no other flag flips as a side effect)

  C. FUNCTIONALITY NOT DELETED (10-14)
     10 the ``_buildDailyCheckinSection`` builder + full markup remain
     11 the checkin CSS rules/classes remain in app.css (design
        restorable — flag-on reproduces the exact current look)
     12 no API/backend was removed: home.js still makes no network
        calls (the section was, and remains, purely presentational)
     13 «إضافة مهمة»، «المهام الساخنة» and the 3-item bottom
        navigation are untouched
     14 financial/welcome UI (balance card, wallet button) is untouched

  D. REGRESSION — other feature flags unaffected (15-16)
     15 Account Linking stays hidden behind SHOW_ACCOUNT_LINKING=false
     16 Add Task / Hot Tasks appends stay unconditional

If ``node`` is unavailable the runtime tests skip cleanly (same
convention as test_miniapp_account_linking.py); the static assertions
still run everywhere.

Run:
    python3 -m pytest test_miniapp_daily_registration.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HOME_JS = Path("miniapp/js/home.js")
INDEX_HTML = Path("miniapp/index.html")
APP_CSS = Path("miniapp/css/app.css")

FLAG_NAME = "SHOW_DAILY_REGISTRATION"
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
        """The daily registration section is appended only inside the guard."""
        content = _home_js()
        guarded = re.search(
            rf"if \({FLAG_NAME}\) \{{\s*"
            r"page\.appendChild\(_buildDailyCheckinSection\(\)\);\s*\}",
            content,
        )
        assert guarded, (
            "page.appendChild(_buildDailyCheckinSection()) must sit "
            f"inside if ({FLAG_NAME}) {{ ... }}"
        )

    def test_4_gated_append_is_the_only_one(self):
        """No ungated duplicate append of the section exists."""
        content = _home_js()
        assert content.count(
            "page.appendChild(_buildDailyCheckinSection());"
        ) == 1, (
            "Exactly one (gated) append of the daily registration "
            "section expected"
        )


# ══════════════════════════════════════════════════════════════════
# B. RUNTIME — node harness (5-9)
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

# Section order when the daily-registration flag is off: the slot is
# simply absent (Account Linking is hidden by its own flag too).
_ORDER_OFF = [
    "home-welcome",
    "home-balance",
    "home-guide",
    "home-add-task",
    "home-hot-tasks",
]

# Section order when the flag is on: the section is back in its
# original position between the balance card and the official guide.
_ORDER_ON = [
    "home-welcome",
    "home-balance",
    "home-checkin",
    "home-guide",
    "home-add-task",
    "home-hot-tasks",
]

_CHECKIN_MARKERS = (
    "التسجيل اليومي",
    "checkin-card",
    "checkin-header",
    "checkin-body",
    "checkin-status",
    "📅",
    "قريباً",
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
        """Flag off (shipped default): no daily registration section."""
        sections = _render_home(flag_on=False)
        testids = [s["testid"] for s in sections]
        assert "home-checkin" not in testids, \
            "Daily Registration section must be hidden by default"

    def test_6_no_leftover_container_and_order_preserved(self):
        """Flag off: not a trace of the section (no gap, empty
        container or placeholder) and every other section keeps its
        original render order."""
        sections = _render_home(flag_on=False)
        testids = [s["testid"] for s in sections]
        assert testids == _ORDER_OFF, f"section order changed: {testids}"

        blob = json.dumps(sections, ensure_ascii=False)
        for marker in ("home-checkin", "التسجيل اليومي", "checkin",
                       "📅"):
            assert marker not in blob, \
                f"leftover daily-registration marker '{marker}' found"
        # «باقي الأقسام» still present
        for present in ("home-welcome", "home-balance", "home-guide",
                        "home-add-task", "home-hot-tasks"):
            assert present in testids, f"section '{present}' disappeared"
        assert "إضافة مهمة" in blob
        assert "المهام الساخنة" in blob

    def test_7_flag_on_renders_section_in_place(self):
        """Flag on: the section returns in its original position with
        the exact original design/markup."""
        sections = _render_home(flag_on=True)
        testids = [s["testid"] for s in sections]
        assert testids == _ORDER_ON, \
            f"flag-on section order must match the original: {testids}"

        section = next(
            s for s in sections if s["testid"] == "home-checkin"
        )
        assert section["className"] == "home-section home-checkin"
        for marker in _CHECKIN_MARKERS:
            assert marker in section["html"], \
                f"flag-on markup missing '{marker}'"
    def test_8_other_sections_identical_with_flag_on(self):
        """Toggling the flag changes nothing but the daily slot."""
        off = {s["testid"]: s for s in _render_home(flag_on=False)}
        on = {s["testid"]: s for s in _render_home(flag_on=True)}
        for testid, off_section in off.items():
            assert on[testid] == off_section, \
                f"section '{testid}' changed when toggling the flag"

    def test_9_only_the_checkin_slot_differs(self):
        """Flag on adds exactly one section and flips no other flag."""
        off = {s["testid"] for s in _render_home(flag_on=False)}
        on = {s["testid"] for s in _render_home(flag_on=True)}
        assert on - off == {"home-checkin"}, \
            f"unexpected sections added: {sorted(on - off)}"
        assert off - on == set(), \
            f"sections disappeared when flag turned on: {sorted(off - on)}"
        # Account Linking stays hidden (its own flag untouched).
        assert "home-account-linking" not in on


# ══════════════════════════════════════════════════════════════════
# C. FUNCTIONALITY NOT DELETED (10-14)
# ══════════════════════════════════════════════════════════════════

class TestDailyRegistrationPreserved:
    """Hiding must never delete the section's design or behaviour."""

    def test_10_builder_and_full_markup_still_present(self):
        """The builder function and every piece of its markup remain."""
        content = _home_js()
        assert "_buildDailyCheckinSection" in content
        assert "section.setAttribute('data-testid', 'home-checkin')" \
            in content
        assert 'data-testid="checkin-status"' in content
        for marker in _CHECKIN_MARKERS:
            assert marker in content, f"markup '{marker}' was deleted"

    def test_11_checkin_css_rules_remain(self):
        """The design tokens for the section remain, so flag-on
        restores the exact current look."""
        css = APP_CSS.read_text(encoding="utf-8")
        for rule in (".checkin-header", ".checkin-body", ".checkin-status",
                     ".page-home .checkin-body", ".page-home .checkin-status",
                     "--neon-red"):
            assert rule in css, f"CSS rule '{rule}' was deleted"
        # The section's classes are still shared with the surviving
        # guide/add-task headers (proves the rules were not pruned).
        assert ".checkin-header," in css

    def test_12_no_api_removed_or_added(self):
        """The section was purely presentational — home.js still makes
        no network calls, so API behaviour is byte-for-byte unchanged."""
        content = _home_js()
        assert "fetch(" not in content
        assert "XMLHttpRequest" not in content
        assert "$.ajax" not in content
        assert "axios" not in content

    def test_13_untouched_areas_preserved(self):
        """«إضافة مهمة»، «المهام الساخنة» and the bottom navigation are
        unchanged by the flag work."""
        content = _home_js()
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

    def test_14_financial_ui_preserved(self):
        """Balance card + wallet button (financial UI) untouched."""
        content = _home_js()
        assert 'data-testid="home-wallet-balance"' in content
        assert 'data-testid="home-wallet-button"' in content
        assert "Navigation.navigateTo('wallet')" in content
        assert "WalletData.getBalance()" in content
        assert "balance-empty" in content


# ══════════════════════════════════════════════════════════════════
# D. REGRESSION — other flags/sections unaffected (15-16)
# ══════════════════════════════════════════════════════════════════

class TestOtherFlagsUnaffected:
    """The new flag must not disturb the neighbouring feature flags."""

    def test_15_account_linking_still_hidden(self):
        """Account Linking stays behind SHOW_ACCOUNT_LINKING=false."""
        content = _home_js()
        assert "const SHOW_ACCOUNT_LINKING = false;" in content, \
            "Account Linking flag changed unexpectedly"
        guarded = re.search(
            r"if \(SHOW_ACCOUNT_LINKING\) \{\s*"
            r"page\.appendChild\(_buildAccountLinkingSection\(\)\);\s*\}",
            content,
        )
        assert guarded, "Account Linking append is no longer gated"

    def test_16_add_task_and_hot_tasks_still_unconditional(self):
        """Those appends are NOT wrapped by any flag."""
        content = _home_js()
        # Direct children of render() — appear exactly once, ungated.
        assert content.count(
            "page.appendChild(_buildAddTaskSection());"
        ) == 1
        assert content.count(
            "page.appendChild(_buildHotTasksSection());"
        ) == 1
        # Neither sits inside an if-guard.
        assert not re.search(
            r"if \([^)]*\) \{\s*"
            r"page\.appendChild\(_build(?:AddTask|HotTasks)Section\(\)\);",
            content,
        ), "Add Task / Hot Tasks must stay unconditional"

    def test_16b_source_still_static_only(self):
        """No injected inline styles or logic crept into home.js."""
        content = _home_js()
        assert "style=" not in content
