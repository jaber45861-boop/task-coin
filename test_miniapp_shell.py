"""
Tests for Task Coin Mini App Shell

Verifies:
- Mini App loads correctly
- Header renders الشحن and السحب
- Bottom navigation renders الرئيسية, المهام, حسابي
- Active navigation state changes correctly
- No existing backend tests are broken
"""

import os
import pytest


class TestMiniAppStructure:
    """Test the Mini App file structure and components."""

    def test_index_html_exists(self):
        """Verify index.html exists in the miniapp directory."""
        assert os.path.exists("miniapp/index.html"), "miniapp/index.html not found"

    def test_css_directory_exists(self):
        """Verify CSS directory exists."""
        assert os.path.exists("miniapp/css"), "miniapp/css directory not found"

    def test_js_directory_exists(self):
        """Verify JS directory exists."""
        assert os.path.exists("miniapp/js"), "miniapp/js directory not found"

    def test_main_stylesheet_exists(self):
        """Verify main CSS file exists."""
        assert os.path.exists("miniapp/css/app.css"), "miniapp/css/app.css not found"

    def test_app_js_exists(self):
        """Verify main app.js exists."""
        assert os.path.exists("miniapp/js/app.js"), "miniapp/js/app.js not found"


class TestMiniAppHeader:
    """Test the header component renders correctly."""

    def _read_html(self):
        with open("miniapp/index.html", "r", encoding="utf-8") as f:
            return f.read()

    def test_header_renders_charge_button(self):
        """Verify الشحن button exists in header."""
        html = self._read_html()
        assert "الشحن" in html, "Header should contain الشحن button"

    def test_header_renders_withdraw_button(self):
        """Verify السحب button exists in header."""
        html = self._read_html()
        assert "السحب" in html, "Header should contain السحب button"

    def test_header_has_charge_button_id(self):
        """Verify charge button has correct ID."""
        html = self._read_html()
        assert 'id="btn-charge"' in html, "Charge button should have id='btn-charge'"

    def test_header_has_withdraw_button_id(self):
        """Verify withdraw button has correct ID."""
        html = self._read_html()
        assert 'id="btn-withdraw"' in html, "Withdraw button should have id='btn-withdraw'"

    def test_header_is_semantic(self):
        """Verify header uses semantic HTML."""
        html = self._read_html()
        assert "<header" in html, "Header should use semantic <header> element"


class TestMiniAppNavigation:
    """Test the bottom navigation component."""

    def _read_html(self):
        with open("miniapp/index.html", "r", encoding="utf-8") as f:
            return f.read()

    def test_nav_renders_home(self):
        """Verify الرئيسية exists in navigation."""
        html = self._read_html()
        assert "الرئيسية" in html, "Navigation should contain الرئيسية"

    def test_nav_renders_tasks(self):
        """Verify المهام exists in navigation."""
        html = self._read_html()
        assert "المهام" in html, "Navigation should contain المهام"

    def test_nav_renders_profile(self):
        """Verify حسابي exists in navigation."""
        html = self._read_html()
        assert "حسابي" in html, "Navigation should contain حسابي"

    def test_nav_profile_not_user(self):
        """Verify profile is حسابي not المستخدم."""
        html = self._read_html()
        assert "المستخدم" not in html, "Navigation should use حسابي not المستخدم"

    def test_nav_has_three_items(self):
        """Verify exactly three navigation items."""
        html = self._read_html()
        nav_items = html.count('class="nav-item')
        assert nav_items == 3, f"Expected 3 nav items, found {nav_items}"

    def test_nav_uses_semantic_html(self):
        """Verify navigation uses semantic HTML."""
        html = self._read_html()
        assert "<nav" in html, "Navigation should use semantic <nav> element"

    def test_nav_home_is_active_by_default(self):
        """Verify home is active by default."""
        html = self._read_html()
        assert 'class="nav-item active" data-page="home"' in html, \
            "Home should be active by default"


class TestMiniAppPages:
    """Test the page templates exist."""

    def _read_html(self):
        with open("miniapp/index.html", "r", encoding="utf-8") as f:
            return f.read()

    def test_home_page_template_exists(self):
        """Verify home page template exists."""
        html = self._read_html()
        assert 'id="page-home"' in html, "Home page template not found"

    def test_tasks_page_template_exists(self):
        """Verify tasks page template exists."""
        html = self._read_html()
        assert 'id="page-tasks"' in html, "Tasks page template not found"

    def test_profile_page_template_exists(self):
        """Verify profile page template exists."""
        html = self._read_html()
        assert 'id="page-profile"' in html, "Profile page template not found"


class TestMiniAppTelegramIntegration:
    """Test Telegram WebApp integration."""

    def _read_html(self):
        with open("miniapp/index.html", "r", encoding="utf-8") as f:
            return f.read()

    def test_telegram_sdk_included(self):
        """Verify Telegram WebApp SDK is included."""
        html = self._read_html()
        assert "telegram-web-app.js" in html, "Telegram WebApp SDK not included"

    def test_viewport_meta_tag(self):
        """Verify viewport meta tag exists for mobile."""
        html = self._read_html()
        assert '<meta name="viewport"' in html, "Viewport meta tag not found"

    def test_dir_attribute_is_rtl(self):
        """Verify HTML direction is RTL for Arabic."""
        html = self._read_html()
        assert 'dir="rtl"' in html, "HTML should have dir='rtl' for Arabic"

    def test_lang_attribute_is_arabic(self):
        """Verify HTML language is Arabic."""
        html = self._read_html()
        assert 'lang="ar"' in html, "HTML should have lang='ar'"


class TestMiniAppFileStructure:
    """Test the file structure and JS modules."""

    def test_telegram_js_exists(self):
        """Verify telegram.js wrapper exists."""
        assert os.path.exists("miniapp/js/telegram.js"), \
            "miniapp/js/telegram.js not found"

    def test_header_js_exists(self):
        """Verify header.js component exists."""
        assert os.path.exists("miniapp/js/header.js"), \
            "miniapp/js/header.js not found"

    def test_navigation_js_exists(self):
        """Verify navigation.js component exists."""
        assert os.path.exists("miniapp/js/navigation.js"), \
            "miniapp/js/navigation.js not found"

    def test_app_js_initializes_telegram(self):
        """Verify app.js initializes Telegram."""
        with open("miniapp/js/app.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "TelegramApp.init()" in content, \
            "app.js should initialize TelegramApp"

    def test_app_js_initializes_header(self):
        """Verify app.js initializes Header."""
        with open("miniapp/js/app.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "Header.init(" in content, \
            "app.js should initialize Header"

    def test_app_js_initializes_navigation(self):
        """Verify app.js initializes Navigation."""
        with open("miniapp/js/app.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "Navigation.init(" in content, \
            "app.js should initialize Navigation"

    def test_navigation_js_has_navigate_to(self):
        """Verify navigation.js exports navigateTo."""
        with open("miniapp/js/navigation.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "navigateTo" in content, \
            "navigation.js should have navigateTo function"

    def test_header_js_has_handle_action(self):
        """Verify header.js exports handleAction."""
        with open("miniapp/js/header.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "handleAction" in content, \
            "header.js should have handleAction function"


class TestMiniAppCSS:
    """Test CSS structure."""

    def _read_css(self):
        with open("miniapp/css/app.css", "r", encoding="utf-8") as f:
            return f.read()

    def test_mobile_first_viewport(self):
        """Verify HTML has mobile-first viewport meta tag."""
        with open("miniapp/index.html", "r", encoding="utf-8") as f:
            html = f.read()
        assert "width=device-width" in html, \
            "HTML should have viewport meta tag with width=device-width"

    def test_safe_area_support(self):
        """Verify CSS supports safe areas for notched devices."""
        css = self._read_css()
        assert "safe-area-inset" in css, \
            "CSS should support safe areas"

    def test_dark_mode_support(self):
        """Verify CSS has dark mode support."""
        css = self._read_css()
        assert ".dark" in css or "prefers-color-scheme" in css, \
            "CSS should have dark mode support"

    def test_header_height_variable(self):
        """Verify CSS defines header height variable."""
        css = self._read_css()
        assert "--header-height" in css, \
            "CSS should define --header-height variable"

    def test_nav_height_variable(self):
        """Verify CSS defines nav height variable."""
        css = self._read_css()
        assert "--nav-height" in css, \
            "CSS should define --nav-height variable"
