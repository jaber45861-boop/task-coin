"""
Focused tests for the Task Coin Mini App Home page UI.

Verifies:
- Home renders successfully
- Home is the active default section
- All 7 required Home sections exist
- Header remains unchanged
- Bottom navigation remains unchanged
- No fake numeric balances are rendered
- No fake task/reward data is rendered
- No backend API is called by the Home UI
- Existing Mini App shell tests still pass
"""

import os
import re
import pytest


# ── Helpers ────────────────────────────────────────────────────────────

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _html() -> str:
    return _read("miniapp/index.html")


def _home_js() -> str:
    return _read("miniapp/js/home.js")


def _app_js() -> str:
    return _read("miniapp/js/app.js")


def _css() -> str:
    return _read("miniapp/css/app.css")


# ══════════════════════════════════════════════════════════════════════
# 1. Home page structure
# ══════════════════════════════════════════════════════════════════════

class TestHomeStructure:
    """Verify the home.js module exists and is properly structured."""

    def test_home_js_exists(self):
        assert os.path.exists("miniapp/js/home.js"), "miniapp/js/home.js not found"

    def test_home_module_has_render(self):
        content = _home_js()
        assert "render" in content, "Home module should export render"

    def test_home_module_returns_element(self):
        content = _home_js()
        assert "createElement" in content, "Home.render should create DOM elements"


# ══════════════════════════════════════════════════════════════════════
# 2. Home is the active default section
# ══════════════════════════════════════════════════════════════════════

class TestHomeDefault:
    """Verify Home is the default active page."""

    def test_nav_home_active_by_default(self):
        html = _html()
        assert 'class="nav-item active" data-page="home"' in html, \
            "Home nav item should be active by default"

    def test_app_initializes_home(self):
        content = _app_js()
        assert "renderPage('home')" in content or 'renderPage("home")' in content, \
            "app.js should render home on init"


# ══════════════════════════════════════════════════════════════════════
# 3. Required Home sections exist in home.js
# ══════════════════════════════════════════════════════════════════════

class TestHomeSections:
    """Verify all 7 required sections are present in home.js."""

    def test_welcome_section(self):
        content = _home_js()
        assert "home-welcome" in content, "Home should have welcome section"

    def test_balance_section(self):
        content = _home_js()
        assert "home-balance" in content, "Home should have balance section"

    def test_daily_checkin_section(self):
        content = _home_js()
        assert "home-checkin" in content, "Home should have daily check-in section"

    def test_official_guide_section(self):
        content = _home_js()
        assert "home-guide" in content, "Home should have official guide section"

    def test_add_task_section(self):
        content = _home_js()
        assert "home-add-task" in content, "Home should have add task section"

    def test_account_linking_section(self):
        content = _home_js()
        assert "home-account-linking" in content, "Home should have account linking section"

    def test_hot_tasks_section(self):
        content = _home_js()
        assert "home-hot-tasks" in content, "Home should have hot tasks section"

    def test_all_seven_sections(self):
        """Count unique section class markers."""
        content = _home_js()
        markers = [
            "home-welcome",
            "home-balance",
            "home-checkin",
            "home-guide",
            "home-add-task",
            "home-account-linking",
            "home-hot-tasks",
        ]
        for m in markers:
            assert m in content, f"Missing section: {m}"


# ══════════════════════════════════════════════════════════════════════
# 4. Section content — Arabic labels
# ══════════════════════════════════════════════════════════════════════

class TestHomeArabicLabels:
    """Verify all section titles are in Arabic."""

    def test_checkin_title(self):
        content = _home_js()
        assert "التسجيل اليومي" in content

    def test_guide_title(self):
        content = _home_js()
        assert "الدليل الرسمي" in content

    def test_add_task_title(self):
        content = _home_js()
        assert "إضافة مهمة" in content

    def test_account_linking_title(self):
        content = _home_js()
        assert "ربط الحسابات" in content

    def test_hot_tasks_title(self):
        content = _home_js()
        assert "المهام الساخنة" in content


# ══════════════════════════════════════════════════════════════════════
# 5. Header remains unchanged
# ══════════════════════════════════════════════════════════════════════

class TestHeaderUnchanged:
    """Verify header still has الشحن and السحب."""

    def test_header_has_charge(self):
        html = _html()
        assert "الشحن" in html

    def test_header_has_withdraw(self):
        html = _html()
        assert "السحب" in html

    def test_header_charge_button_id(self):
        html = _html()
        assert 'id="btn-charge"' in html

    def test_header_withdraw_button_id(self):
        html = _html()
        assert 'id="btn-withdraw"' in html


# ══════════════════════════════════════════════════════════════════════
# 6. Bottom navigation remains unchanged
# ══════════════════════════════════════════════════════════════════════

class TestNavUnchanged:
    """Verify bottom nav still has exactly 3 items with correct labels."""

    def test_home_label(self):
        html = _html()
        assert "الرئيسية" in html

    def test_tasks_label(self):
        html = _html()
        assert "المهام" in html

    def test_profile_label(self):
        html = _html()
        assert "حسابي" in html

    def test_three_nav_items(self):
        html = _html()
        count = html.count('class="nav-item')
        assert count == 3, f"Expected 3 nav items, got {count}"

    def test_profile_not_user(self):
        html = _html()
        # Check only in nav area — find nav element content
        nav_start = html.find("<nav")
        nav_end = html.find("</nav>", nav_start)
        nav_html = html[nav_start:nav_end] if nav_start >= 0 else html
        assert "المستخدم" not in nav_html


# ══════════════════════════════════════════════════════════════════════
# 7. No fake numeric balances
# ══════════════════════════════════════════════════════════════════════

class TestNoFakeData:
    """Ensure no invented numeric balances, rewards, or task data."""

    def test_no_hardcoded_balance_numbers(self):
        content = _home_js()
        # Should not contain standalone digits that look like amounts
        # Allow digits inside HTML tags or CSS selectors, but not bare amounts
        balance_section_start = content.find("home-balance")
        if balance_section_start > 0:
            balance_section = content[balance_section_start:balance_section_start + 500]
            # No digits that look like currency amounts (e.g., 100, 500, 1000)
            amounts = re.findall(r'(?<!\d)\d{2,}(?!\d)', balance_section)
            assert len(amounts) == 0, \
                f"Found hardcoded amounts in balance section: {amounts}"

    def test_no_fake_reward_values(self):
        content = _home_js()
        assert "500" not in content or "500" not in content.split("balance")[1][:200] if "balance" in content else True

    def test_placeholder_dashes_for_empty_values(self):
        """Empty balance values should show dashes, not numbers."""
        content = _home_js()
        assert "balance-empty" in content, \
            "Balance section should use balance-empty class for placeholders"

    def test_checkin_status_is_coming_soon(self):
        content = _home_js()
        # Find the checkin section and verify status
        checkin_idx = content.find("home-checkin")
        checkin_section = content[checkin_idx:checkin_idx + 800]
        assert "قريباً" in checkin_section

    def test_hot_tasks_empty_state(self):
        content = _home_js()
        hot_idx = content.find("home-hot-tasks")
        hot_section = content[hot_idx:hot_idx + 900]
        assert "لا توجد مهام حالياً" in hot_section

    def test_no_invented_currencies(self):
        """No fake currency names should appear in the home module."""
        content = _home_js()
        currencies = ["دولار", "يورو", "جنيه", "درهم", "دينار", "coins", "token"]
        for c in currencies:
            assert c.lower() not in content.lower(), \
                f"Found invented currency '{c}' in home.js"


# ══════════════════════════════════════════════════════════════════════
# 8. No backend API calls in Home UI
# ══════════════════════════════════════════════════════════════════════

class TestNoBackendCalls:
    """Verify Home UI does not make backend API calls."""

    def test_no_fetch_calls(self):
        content = _home_js()
        assert "fetch(" not in content, "Home should not call fetch()"

    def test_no_xmlhttp(self):
        content = _home_js()
        assert "XMLHttpRequest" not in content

    def test_no_ajax(self):
        content = _home_js()
        assert "$.ajax" not in content
        assert "axios" not in content


# ══════════════════════════════════════════════════════════════════════
# 9. App.js integration — Home is dynamic
# ══════════════════════════════════════════════════════════════════════

class TestAppIntegration:
    """Verify app.js correctly integrates the Home module."""

    def test_app_uses_home_render(self):
        content = _app_js()
        assert "Home.render" in content, \
            "app.js should call Home.render() for the home page"

    def test_app_has_home_special_case(self):
        content = _app_js()
        assert "home" in content and "Home" in content, \
            "app.js should special-case the home page"


# ══════════════════════════════════════════════════════════════════════
# 10. CSS has Home-specific styles
# ══════════════════════════════════════════════════════════════════════

class TestHomeCSS:
    """Verify CSS contains Home-specific style rules."""

    def test_welcome_card_style(self):
        css = _css()
        assert ".welcome-card" in css

    def test_balance_card_style(self):
        css = _css()
        assert ".balance-card" in css

    def test_section_card_style(self):
        css = _css()
        assert ".section-card" in css

    def test_checkin_card_style(self):
        css = _css()
        assert ".checkin-card" in css or ".checkin-header" in css

    def test_hot_tasks_card_style(self):
        css = _css()
        assert ".hot-tasks-card" in css or ".hot-tasks-body" in css


# ══════════════════════════════════════════════════════════════════════
# 11. Structural states — neutral/empty indicators
# ══════════════════════════════════════════════════════════════════════

class TestStructuralStates:
    """Verify neutral structural states are used instead of fake data."""

    def test_username_placeholder(self):
        content = _home_js()
        assert "home-username" in content
        # Should use a dash or placeholder, not a fake name
        username_idx = content.find("home-username")
        nearby = content[username_idx:username_idx + 200]
        assert "—" in nearby or "قريباً" in nearby, \
            "Username should use a neutral placeholder"

    def test_level_placeholder(self):
        content = _home_js()
        assert "home-level" in content
        level_idx = content.find("home-level")
        nearby = content[level_idx:level_idx + 200]
        assert "قريباً" in nearby, \
            "Level should show 'قريباً' as placeholder"

    def test_guide_coming_soon(self):
        content = _home_js()
        guide_idx = content.find("home-guide")
        nearby = content[guide_idx:guide_idx + 700]
        assert "قريباً" in nearby

    def test_add_task_coming_soon(self):
        content = _home_js()
        idx = content.find("home-add-task")
        nearby = content[idx:idx + 700]
        assert "قريباً" in nearby

    def test_account_linking_coming_soon(self):
        content = _home_js()
        idx = content.find("home-account-linking")
        nearby = content[idx:idx + 700]
        assert "قريباً" in nearby
