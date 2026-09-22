"""
Focused tests for the Task Coin Mini App Home page UI.

Verifies:
- Home renders successfully
- Home is the active default section
- All 7 required Home sections exist
- Old header withdraw/charge buttons are removed (MT-UI-03)
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
# 5. Old header withdraw/charge buttons removed (MT-UI-03)
# ══════════════════════════════════════════════════════════════════════

class TestHeaderButtonsRemoved:
    """The old header withdrawal/charge buttons must be fully gone."""

    def test_header_has_no_charge(self):
        html = _html()
        assert "الشحن" not in html, \
            "Old charge button must be removed from Home markup"

    def test_header_has_no_withdraw(self):
        html = _html()
        assert "السحب" not in html, \
            "Old withdraw button must be removed from Home markup"

    def test_header_no_charge_button_id(self):
        html = _html()
        assert 'id="btn-charge"' not in html, \
            "btn-charge element must be removed"

    def test_header_no_withdraw_button_id(self):
        html = _html()
        assert 'id="btn-withdraw"' not in html, \
            "btn-withdraw element must be removed"

    def test_header_actions_container_removed(self):
        html = _html()
        assert 'class="header-actions"' not in html, \
            "Old header action bar container must be removed"


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
        # Should have a fallback to dash when first_name is unavailable
        # The code uses: const displayName = firstName || '—'
        assert "|| '—'" in content or '|| "—"' in content, \
            "Username should fallback to '—' when first_name is unavailable"

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

    def test_account_linking_coming_soon(self):
        content = _home_js()
        idx = content.find("home-account-linking")
        nearby = content[idx:idx + 700]
        assert "قريباً" in nearby


# ══════════════════════════════════════════════════════════════════════
# 12. Add Task CTA Button
# ══════════════════════════════════════════════════════════════════════
class TestAddTaskCTAButton:
    """Verify the + إضافة مهمة CTA button exists and is UI-only."""

    def test_cta_button_exists(self):
        """Add Task section should contain a CTA button element."""
        content = _home_js()
        assert "add-task-cta" in content, \
            "Add Task section should contain a CTA button"

    def test_cta_button_has_testid(self):
        """CTA button should have a data-testid for testability."""
        content = _home_js()
        assert 'data-testid="add-task-cta"' in content, \
            "CTA button should have data-testid='add-task-cta'"

    def test_cta_button_text_contains_label(self):
        """CTA button should display '➕ إضافة مهمة'."""
        content = _home_js()
        idx = content.find("add-task-cta")
        nearby = content[idx:idx + 200]
        assert "➕" in nearby, "CTA button should contain ➕ emoji"
        assert "إضافة مهمة" in nearby, \
            "CTA button should contain 'إضافة مهمة' text"

    def test_cta_button_is_disabled(self):
        """CTA button must be disabled — no business logic yet."""
        content = _home_js()
        idx = content.find("add-task-cta")
        nearby = content[idx:idx + 200]
        assert "disabled" in nearby, \
            "CTA button should be disabled (UI-only placeholder)"

    def test_cta_button_is_html_button_element(self):
        """CTA should be a <button> element for proper semantics."""
        content = _home_js()
        assert "add-task-cta" in content
        # Verify it's inside a <button tag
        idx = content.find("add-task-cta")
        preceding = content[max(0, idx - 60):idx]
        assert "<button" in preceding, \
            "CTA should be a <button> element"

    def test_no_api_calls_in_home(self):
        """Home module must not introduce any API calls."""
        content = _home_js()
        assert "fetch(" not in content, "Home should not call fetch()"
        assert "XMLHttpRequest" not in content
        assert "axios" not in content

    def test_no_task_mutation_in_home(self):
        """Home module must not contain task creation/mutation logic."""
        content = _home_js()
        mutations = [
            "createTask",
            "submitTask",
            "startTask",
            "completeTask",
            "deleteTask",
            "updateTask",
            "create_task",
            "submit_task",
        ]
        for m in mutations:
            assert m not in content, \
                f"Home should not contain task mutation: {m}"

    def test_add_task_section_still_has_header(self):
        """Add Task section header should remain intact."""
        content = _home_js()
        idx = content.find("home-add-task")
        section = content[idx:idx + 600]
        assert "add-task-header" in section
        assert "section-icon" in section
        assert "section-title" in section


# ══════════════════════════════════════════════════════════════════════
# 13. Profile Summary — Telegram WebApp user integration
# ══════════════════════════════════════════════════════════════════════
class TestProfileSummaryTelegramIntegration:
    """Verify Profile Summary uses Telegram WebApp user data."""

    def test_uses_telegram_app_get_user(self):
        """Home should call TelegramApp.getUser() to get user data."""
        content = _home_js()
        assert "TelegramApp.getUser()" in content, \
            "Home should call TelegramApp.getUser()"

    def test_first_name_displayed_when_available(self):
        """Home should use first_name from Telegram user when available."""
        content = _home_js()
        assert "first_name" in content, \
            "Home should reference first_name from Telegram user"

    def test_username_displayed_when_available(self):
        """Home should display @username only when provided by Telegram."""
        content = _home_js()
        assert "username" in content, \
            "Home should reference username from Telegram user"

    def test_photo_url_used_for_avatar(self):
        """Home should use photo_url for avatar when available."""
        content = _home_js()
        assert "photo_url" in content, \
            "Home should reference photo_url from Telegram user"

    def test_avatar_img_element_for_photo(self):
        """When photo_url is available, Home should render an img element."""
        content = _home_js()
        assert "avatar-img" in content, \
            "Home should have avatar-img class for Telegram profile photo"

    def test_avatar_img_has_testid(self):
        """Avatar img should have data-testid for testability."""
        content = _home_js()
        assert 'data-testid="home-avatar-img"' in content, \
            "Avatar img should have data-testid='home-avatar-img'"

    def test_username_handle_element(self):
        """Home should have a welcome-username element for @handle."""
        content = _home_js()
        assert "welcome-username" in content, \
            "Home should have welcome-username class"

    def test_username_handle_testid(self):
        """Username handle should have data-testid for testability."""
        content = _home_js()
        assert 'data-testid="home-username-handle"' in content, \
            "Username handle should have data-testid='home-username-handle'"

    def test_username_prefixed_with_at(self):
        """Username should be displayed with @ prefix."""
        content = _home_js()
        idx = content.find("welcome-username")
        nearby = content[idx:idx + 100]
        assert "@" in nearby, \
            "Username should be prefixed with @"

    def test_fallback_to_dash_when_no_name(self):
        """When first_name is unavailable, display dash placeholder."""
        content = _home_js()
        # The code uses: const displayName = firstName || '—'
        assert "|| '—'" in content or '|| "—"' in content, \
            "Home should fallback to '—' when first_name is unavailable"

    def test_no_fabricated_names(self):
        """Home should not contain fabricated user names."""
        content = _home_js()
        # These are fake names that should never appear
        fake_names = ["أحمد", "محمد", "علي", "خالد", "أحمد"  ]
        # Only check in the welcome section
        welcome_idx = content.find("home-welcome")
        if welcome_idx > 0:
            welcome_section = content[welcome_idx:welcome_idx + 800]
            for name in fake_names:
                # The name should not appear as hardcoded text
                # (it's OK if it appears in variable names or comments)
                assert f">{name}<" not in welcome_section, \
                    f"Found fabricated name '{name}' in welcome section"

    def test_no_fabricated_usernames(self):
        """Home should not contain fabricated Telegram usernames."""
        content = _home_js()
        fake_usernames = ["@user123", "@test_user", "@admin"]
        welcome_idx = content.find("home-welcome")
        if welcome_idx > 0:
            welcome_section = content[welcome_idx:welcome_idx + 800]
            for username in fake_usernames:
                assert username not in welcome_section, \
                    f"Found fabricated username '{username}' in welcome section"

    def test_no_fabricated_avatar_urls(self):
        """Home should not contain fabricated avatar URLs."""
        content = _home_js()
        fake_urls = [
            "https://example.com/avatar",
            "https://t.me/i/userpic",
            "https://ui-avatars.com",
        ]
        for url in fake_urls:
            assert url not in content, \
                f"Found fabricated avatar URL '{url}' in home.js"

    def test_telegram_user_data_sourced_safely(self):
        """User data should come from TelegramApp.getUser(), not DOM/QS."""
        content = _home_js()
        # Should NOT parse query strings or DOM for user data
        assert "URLSearchParams" not in content, \
            "Home should not parse URL query strings for user data"
        assert "querySelector" not in content or "querySelectorAll" not in content, \
            "Home should not use querySelector for user data"
        # Should use TelegramApp.getUser()
        assert "TelegramApp.getUser()" in content, \
            "Home must use TelegramApp.getUser() for trusted user data"

    def test_no_bot_token_exposed(self):
        """Home should not expose bot tokens or secrets."""
        content = _home_js()
        assert "bot_token" not in content.lower(), \
            "Home should not expose bot tokens"
        assert "initData" not in content, \
            "Home should not expose initData to UI"

    def test_avatar_img_has_empty_alt(self):
        """Avatar img should have empty alt attribute (decorative)."""
        content = _home_js()
        assert 'alt=""' in content, \
            "Avatar img should have empty alt (decorative image)"

    def test_css_has_avatar_img_style(self):
        """CSS should have avatar-img style for profile photos."""
        css = _css()
        assert ".avatar-img" in css, \
            "CSS should define .avatar-img style"

    def test_css_has_welcome_username_style(self):
        """CSS should have welcome-username style for @handle."""
        css = _css()
        assert ".welcome-username" in css, \
            "CSS should define .welcome-username style"


# ══════════════════════════════════════════════════════════════════
# 14. Home Dark/Black theme with Red neon accents
# ══════════════════════════════════════════════════════════════════
class TestHomeDarkNeonTheme:
    """Verify the Home UI uses a dark/black background with red neon accents."""

    def _home_theme(self) -> str:
        """Return the Home-theme portion of the stylesheet."""
        css = _css()
        idx = css.find(".page-home")
        return css[idx:] if idx >= 0 else ""

    def test_home_bg_variable_is_black(self):
        """Home background variable must be a near-black colour."""
        css = _css()
        assert re.search(r"--home-bg:\s*#0a0a0a", css), \
            "--home-bg must be #0a0a0a (dark/black)"

    def test_page_home_uses_dark_background(self):
        """.page-home must paint the dark background."""
        theme = self._home_theme()
        assert ".page-home" in theme
        assert "background-color: var(--home-bg)" in theme, \
            ".page-home should use the dark --home-bg background"

    def test_red_neon_variables_defined(self):
        """Red neon accent variables must be defined."""
        css = _css()
        assert "--neon-red" in css, "CSS must define --neon-red accent"
        assert "--neon-red-glow" in css, "CSS must define --neon-red-glow"

    def test_home_cards_have_neon_border_and_glow(self):
        """Home cards must have a red neon border + glow shadow."""
        theme = self._home_theme()
        idx = theme.find(".page-home .welcome-card")
        assert idx >= 0, "Home cards must be styled within .page-home"
        block = theme[idx:idx + 500]
        assert "neon-red-border" in block, \
            "Home cards should use the red neon border"
        assert "box-shadow" in block and "neon-red-glow" in block, \
            "Home cards should glow with red neon shadow"

    def test_home_section_icons_are_red(self):
        """Section icons inside Home should be red neon."""
        theme = self._home_theme()
        idx = theme.find(".page-home .section-icon")
        assert idx >= 0, ".page-home .section-icon must exist"
        nearby = theme[idx:idx + 200]
        assert "--neon-red" in nearby

    def test_theme_scoped_to_the_three_pages(self):
        """Dark theme may only target Home/Tasks/Account (+ theme vars).

        Tasks & Account were unified with the Home theme by the
        UNIFY TASKS & ACCOUNT UI micro-task; no other selector may
        use the dark background.
        """
        css = _css()
        theme = self._home_theme()
        allowed = (".page-home", ".page-tasks", ".page-profile",
                   ".page-wallet", ":root")
        for rule in re.findall(r"([^{}]+)\{[^}]*--home-bg[^}]*\}", theme):
            assert any(a in rule for a in allowed), \
                f"Unexpected selector using --home-bg: {rule!r}"


# ══════════════════════════════════════════════════════════════════
# 15. Withdraw (RED + up arrow) / Deposit (GREEN + down arrow) — Wallet
# ══════════════════════════════════════════════════════════════════
class TestWalletActionButtonsTheme:
    """Verify السحب is red with an up arrow, الإيداع is green with a
    down arrow — now inside the Wallet page (MT-UI-03)."""

    def test_withdraw_button_is_red(self):
        """The Wallet withdraw action must use a red gradient background."""
        css = _css()
        idx = css.find(".wallet-action-withdraw")
        assert idx >= 0, "CSS must style .wallet-action-withdraw"
        block = css[idx:idx + 400]
        assert "linear-gradient" in block, \
            "Withdraw button should use a gradient fill"
        assert "#ff5252" in block and "#d50000" in block, \
            "Withdraw button must be RED"

    def test_deposit_button_is_green(self):
        """The Wallet deposit action must use a green gradient background."""
        css = _css()
        idx = css.find(".wallet-action-deposit")
        assert idx >= 0, "CSS must style .wallet-action-deposit"
        block = css[idx:idx + 400]
        assert "linear-gradient" in block, \
            "Deposit button should use a gradient fill"
        assert "#4dff9f" in block and "#009e4f" in block, \
            "Deposit button must be GREEN"

    def test_withdraw_button_has_up_arrow(self):
        """السحب button must render an up arrow (⬆) near its label."""
        content = _read("miniapp/js/wallet.js")
        idx = content.find("wallet-action-withdraw")
        assert idx >= 0
        block = content[idx:idx + 300]
        assert "⬆" in block, \
            "Withdraw button must contain an up arrow (⬆)"
        assert "السحب" in block, "Withdraw button must keep its label"

    def test_deposit_button_has_down_arrow(self):
        """الإيداع button must render a down arrow (⬇) near its label."""
        content = _read("miniapp/js/wallet.js")
        idx = content.find("wallet-action-deposit")
        assert idx >= 0
        block = content[idx:idx + 300]
        assert "⬇" in block, \
            "Deposit button must contain a down arrow (⬇)"
        assert "الإيداع" in block, "Deposit button must keep its label"

    def test_arrows_are_decorative_spans(self):
        """Arrows should live in .btn-arrow spans (aria-hidden)."""
        content = _read("miniapp/js/wallet.js")
        assert content.count('class="btn-arrow"') == 2, \
            "Exactly two action arrow spans expected (withdraw + deposit)"

    def test_button_action_wiring_preserved(self):
        """data-action wiring kept on the Wallet action buttons."""
        content = _read("miniapp/js/wallet.js")
        assert 'data-action="deposit"' in content
        assert 'data-action="withdraw"' in content


# ══════════════════════════════════════════════════════════════════
# 16. Constraints — RTL, responsive, no branding, structure unchanged
# ══════════════════════════════════════════════════════════════════
class TestThemeConstraints:
    """Verify RTL, mobile responsiveness, no external branding, stable structure."""

    def test_rtl_preserved(self):
        html = _html()
        assert 'dir="rtl"' in html, "RTL direction must be preserved"
        assert 'lang="ar"' in html, "Arabic language must be preserved"

    def test_viewport_preserved(self):
        html = _html()
        assert "width=device-width" in html, \
            "Mobile viewport meta tag must be preserved"

    def test_responsive_media_queries_preserved(self):
        css = _css()
        assert "@media" in css, \
            "Responsive media queries must remain in the stylesheet"

    def test_no_external_branding(self):
        """No Vodafone Cash or any external branding in Mini App files."""
        banned = ["vodafone", "فودافون", "vodafone cash", "فودافون كاش"]
        for path in [
            "miniapp/index.html",
            "miniapp/css/app.css",
            "miniapp/js/home.js",
            "miniapp/js/header.js",
        ]:
            content = _read(path).lower()
            for word in banned:
                assert word not in content, \
                    f"External branding '{word}' found in {path}"

    def test_home_section_order_unchanged(self):
        """The 7 Home sections must keep their original render order."""
        content = _home_js()
        order = re.findall(r"page\.appendChild\(_build(\w+)\(\)\)", content)
        assert order == [
            "WelcomeSection",
            "BalanceSection",
            "DailyCheckinSection",
            "OfficialGuideSection",
            "AddTaskSection",
            "AccountLinkingSection",
            "HotTasksSection",
        ], f"Home section order changed: {order}"

    def test_home_js_content_unchanged_by_theme(self):
        """Theme work must not inject logic into home.js."""
        content = _home_js()
        assert "fetch(" not in content
        assert "style=" not in content, \
            "Inline styles belong in CSS, not home.js"

    def test_no_backend_files_reference_theme(self):
        """Backend/task-logic modules must not be touched by the theme."""
        for path in ["task_verifier.py", "task_completion.py", "db.py"]:
            if os.path.exists(path):
                content = _read(path).lower()
                assert "neon" not in content and "page-home" not in content, \
                    f"Theme references leaked into backend file {path}"
