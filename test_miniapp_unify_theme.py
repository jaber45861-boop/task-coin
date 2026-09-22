"""
Tests for the unified Tasks & Account UI theme (UNIFY micro-task).

Verifies:
- The old Withdraw (السحب) / Charge (الشحن) header buttons are gone
  from the Home header entirely (moved into the Wallet page —
  MT-UI-03), so they cannot appear on Tasks or Account either
- Tasks & Account use the same dark/black + red-neon theme as Home
- Red active states are applied (bottom navigation)
- Page content/order is unchanged; RTL + mobile responsive preserved
- No Backend or Task Logic files are touched by the theme
"""

import os
import re


# ── Helpers ────────────────────────────────────────────────────────────

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _html() -> str:
    return _read("miniapp/index.html")


def _css() -> str:
    return _read("miniapp/css/app.css")


def _app_js() -> str:
    return _read("miniapp/js/app.js")


def _template(page_id: str) -> str:
    """Return the markup of a <template id=...> block."""
    html = _html()
    start = html.find(f'<template id="{page_id}">')
    if start < 0:
        return ""
    end = html.find("</template>", start)
    return html[start:end]


def _header() -> str:
    html = _html()
    start = html.find("<header")
    end = html.find("</header>", start)
    return html[start:end]


def _rule_block(selector: str) -> str:
    """Return the first rule block whose selector starts at *selector*."""
    css = _css()
    idx = css.find(selector)
    if idx < 0:
        return ""
    end = css.find("}", idx)
    return css[idx:end + 1]


# ══════════════════════════════════════════════════════════════════
# 1. Withdraw / Charge buttons belong to Home only
# ══════════════════════════════════════════════════════════════════

class TestActionButtonsScopedToHome:
    """The header action bar is gone; its hiding rules stay valid."""

    def test_header_hidden_on_tasks_page(self):
        """CSS hides the header action bar when Tasks is active."""
        css = _css()
        assert 'body[data-page="tasks"] .app-header' in css, \
            "Tasks page must hide the header action bar"
        block = _rule_block('body[data-page="tasks"] .app-header')
        assert "display: none" in block, \
            "Header rule for Tasks must set display: none"

    def test_header_hidden_on_profile_page(self):
        """CSS hides the header action bar when Account is active."""
        css = _css()
        assert 'body[data-page="profile"] .app-header' in css, \
            "Account page must hide the header action bar"
        block = _rule_block('body[data-page="profile"] .app-header')
        assert "display: none" in block, \
            "Header rule for Account must set display: none"

    def test_header_offset_removed_on_hidden_pages(self):
        """Content must not keep the header top offset off Home."""
        css = _css()
        for page in ("tasks", "profile"):
            idx = css.find(f'body[data-page="{page}"] .app-content')
            assert idx >= 0, \
                f"Missing .app-content compensation rule for {page}"
            block = css[idx:css.find("}", idx)]
            assert "margin-top: 0" in block, \
                f".app-content on {page} should reset margin-top"

    def test_header_never_hidden_on_home(self):
        """No rule may hide the header on the Home page."""
        css = _css()
        hide_rules = re.findall(
            r"[^{}]+\{[^}]*display:\s*none[^}]*\}", css
        )
        for rule in hide_rules:
            if ".app-header" in rule:
                assert 'data-page="home"' not in rule, \
                    f"Header must stay visible on Home: {rule!r}"

    def test_tasks_template_has_no_action_buttons(self):
        """The Tasks template must contain no withdraw/charge buttons."""
        tpl = _template("page-tasks")
        assert tpl, "page-tasks template missing"
        for banned in ("السحب", "الشحن", "btn-withdraw", "btn-charge",
                       "btn-arrow", "header-btn"):
            assert banned not in tpl, \
                f"Found '{banned}' inside the Tasks template"

    def test_profile_template_has_no_action_buttons(self):
        """The Account template must contain no withdraw/charge buttons."""
        tpl = _template("page-profile")
        assert tpl, "page-profile template missing"
        for banned in ("السحب", "الشحن", "btn-withdraw", "btn-charge",
                       "btn-arrow", "header-btn"):
            assert banned not in tpl, \
                f"Found '{banned}' inside the Account template"

    def test_header_contains_no_action_buttons(self):
        """The header must no longer contain either old action button
        (they moved into the Wallet page — MT-UI-03)."""
        header = _header()
        assert 'id="btn-withdraw"' not in header, \
            "Header must not keep the old withdraw button"
        assert 'id="btn-charge"' not in header, \
            "Header must not keep the old charge button"
        assert "السحب" not in header and "الشحن" not in header

    def test_app_reflects_active_page_on_body(self):
        """app.js exposes the active page so CSS can scope the header."""
        content = _app_js()
        assert "document.body.dataset.page = page" in content, \
            "app.js must set body[data-page] on render"

    def test_home_is_initial_page(self):
        """Home is rendered on init, so the default view shows buttons."""
        content = _app_js()
        assert "renderPage('home')" in content or 'renderPage("home")' in content


# ══════════════════════════════════════════════════════════════════
# 2. Unified dark / red-neon theme on Tasks & Account
# ══════════════════════════════════════════════════════════════════

class TestUnifiedTheme:
    """Tasks & Account share Home's dark/black + red-neon theme."""

    def test_tasks_page_dark_background(self):
        block = _rule_block(".page-tasks,\n.page-profile")
        assert block, "Missing shared dark background rule"
        assert "var(--home-bg)" in block, \
            "Tasks/Account must use the Home dark background"

    def test_profile_page_styled_by_same_rule(self):
        block = _rule_block(".page-tasks,\n.page-profile")
        assert ".page-profile" in block, \
            "Account page must be part of the shared dark rule"

    def test_tasks_cards_have_neon_border_and_glow(self):
        block = _rule_block(".page-tasks .page-content")
        assert block, "Missing .page-tasks .page-content rule"
        assert "neon-red-border" in block, \
            "Tasks cards must use the red neon border"
        assert "box-shadow" in block and "neon-red-glow" in block, \
            "Tasks cards must glow like Home cards"

    def test_profile_cards_have_neon_border_and_glow(self):
        block = _rule_block(".page-profile .page-content")
        assert block, "Missing .page-profile .page-content rule"
        assert "neon-red-border" in block, \
            "Account cards must use the red neon border"
        assert "box-shadow" in block and "neon-red-glow" in block, \
            "Account cards must glow like Home cards"

    def test_cards_share_home_shape(self):
        """Same border-radius (14px) as Home cards."""
        for sel in (".page-tasks .page-content", ".page-profile .page-content"):
            block = _rule_block(sel)
            assert "border-radius: 14px" in block, \
                f"{sel} must match the Home card radius"

    def test_placeholder_text_readable_on_dark(self):
        css = _css()
        idx = css.find(".page-tasks .placeholder-text")
        assert idx >= 0
        block = css[idx:css.find("}", idx)]
        assert "--home-text-secondary" in block, \
            "Placeholder text must use the Home secondary text colour"

    def test_red_active_nav_light_theme(self):
        css = _css()
        root = css[css.find(":root"):]
        root = root[:root.find("}")]
        assert "--nav-active-color: #ff2d2d" in root, \
            "Active navigation state must be red (light theme)"

    def test_red_active_nav_dark_theme(self):
        css = _css()
        idx = css.find("body.dark")
        assert idx >= 0, "Dark mode block missing"
        block = css[idx:css.find("}", idx)]
        assert "--nav-active-color: #ff2d2d" in block, \
            "Active navigation state must be red (dark theme)"

    def test_action_button_colors_retained(self):
        """Unification must not drop the red/green action gradients
        (now living on the Wallet page buttons)."""
        css = _css()
        w = css[css.find(".wallet-action-withdraw"):]
        assert "#ff5252" in w[:400] and "#d50000" in w[:400], \
            "Withdraw must stay RED"
        c = css[css.find(".wallet-action-deposit"):]
        assert "#4dff9f" in c[:400] and "#009e4f" in c[:400], \
            "Deposit must stay GREEN"


# ══════════════════════════════════════════════════════════════════
# 3. Content, order and constraints unchanged
# ══════════════════════════════════════════════════════════════════

class TestContentAndConstraints:
    """Tasks/Account content & order untouched; RTL/responsive kept."""

    def test_tasks_content_unchanged(self):
        tpl = _template("page-tasks")
        assert '<h2>المهام</h2>' in tpl, "Tasks title changed"
        assert "قائمة المهام" in tpl, "Tasks placeholder text changed"
        assert 'class="page page-tasks"' in tpl, "Tasks page class changed"

    def test_tasks_order_unchanged(self):
        tpl = _template("page-tasks")
        header_pos = tpl.find("page-header")
        content_pos = tpl.find("page-content")
        placeholder_pos = tpl.find("placeholder-text")
        assert 0 < header_pos < content_pos < placeholder_pos, \
            "Tasks section order changed"

    def test_profile_content_unchanged(self):
        tpl = _template("page-profile")
        assert '<h2>حسابي</h2>' in tpl, "Account title changed"
        assert "معلومات الحساب" in tpl, "Account placeholder text changed"
        assert 'class="page page-profile"' in tpl, "Account page class changed"

    def test_profile_order_unchanged(self):
        tpl = _template("page-profile")
        header_pos = tpl.find("page-header")
        content_pos = tpl.find("page-content")
        placeholder_pos = tpl.find("placeholder-text")
        assert 0 < header_pos < content_pos < placeholder_pos, \
            "Account section order changed"

    def test_nav_structure_unchanged(self):
        html = _html()
        assert html.count('class="nav-item') == 3
        assert 'class="nav-item active" data-page="home"' in html

    def test_rtl_preserved(self):
        html = _html()
        assert 'dir="rtl"' in html
        assert 'lang="ar"' in html

    def test_viewport_preserved(self):
        html = _html()
        assert "width=device-width" in html

    def test_responsive_media_queries_preserved(self):
        assert "@media" in _css()

    def test_no_action_button_labels_in_templates(self):
        """Neither template gained any action-button markup."""
        for page_id in ("page-tasks", "page-profile"):
            tpl = _template(page_id)
            assert "header-actions" not in tpl
            assert "data-action" not in tpl

    def test_no_backend_or_task_logic_touched(self):
        """Backend/task-logic files must not reference the UI theme."""
        for path in ("db.py", "bot.py", "task_verifier.py",
                     "task_completion.py", "task_lifecycle.py"):
            if not os.path.exists(path):
                continue
            content = _read(path).lower()
            for token in ("neon-red", "page-tasks", "page-profile",
                          "btn-withdraw", "btn-charge", "data-page"):
                assert token not in content, \
                    f"UI theme token '{token}' leaked into {path}"
