"""
Focused tests for the approved Wallet UI (MT-UI-03).

The approved design is the source of truth.  These tests pin the two
states — Home (State A) and Wallet (State B) — plus the navigation
between them, the data-binding discipline (no fake money, no fake
history), the RTL/responsive constraints, and the UI-only scope
boundary (no financial/business logic).

Coverage map (spec §TESTS):

  1.  Home no longer renders the old withdrawal button
  2.  Home no longer renders the old deposit/charge button
  3.  Home renders exactly one wallet button
  4.  Wallet button is interactive
  5.  Wallet button navigates to Wallet page
  6.  Wallet page exists
  7.  Wallet page title is "المحفظة"
  8.  Wallet page has a back button
  9.  Back button returns to Home
  10. Wallet page has "رصيدك الحالي"
  11. Wallet page has withdrawal action
  12. Wallet page has deposit action
  13. Wallet page has "سجل المعاملات"
  14. Wallet page has "عرض كل المعاملات"
  15. Home displays "المستوى: قريباً"
  16. Home has a balance display area next to the profile
  17. Wallet is NOT added to bottom navigation
  18. Existing Home/Tasks/Profile navigation remains intact
  19. RTL layout remains intact
  20. No horizontal overflow at mobile widths (structural checks)
  21. Existing theme remains consistent
  22. No hard-coded fake financial history presented as real data
  23. No wallet/ledger mutation code was added
  24. No withdrawal/deposit business logic was added

Run:
    python -m pytest test_miniapp_wallet.py -v
"""

import os
import re


# ── Helpers ────────────────────────────────────────────────────────────

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _html() -> str:
    return _read("miniapp/index.html")


def _home_js() -> str:
    return _read("miniapp/js/home.js")


def _wallet_js() -> str:
    return _read("miniapp/js/wallet.js")


def _wallet_data_js() -> str:
    return _read("miniapp/js/wallet-data.js")


def _app_js() -> str:
    return _read("miniapp/js/app.js")


def _navigation_js() -> str:
    return _read("miniapp/js/navigation.js")


def _css() -> str:
    return _read("miniapp/css/app.css")


def _has_testid(content: str, tid: str) -> bool:
    """Testid present as HTML attribute or as a setAttribute() call."""
    return (f'data-testid="{tid}"' in content) or \
           (f"'data-testid', '{tid}'" in content)


def _all_js() -> list:
    return [
        os.path.join("miniapp/js", name)
        for name in sorted(os.listdir("miniapp/js"))
        if name.endswith(".js")
    ]


# ══════════════════════════════════════════════════════════════════════
# 1–2. Old Home withdrawal/deposit buttons removed
# ══════════════════════════════════════════════════════════════════════

class TestOldButtonsRemoved:
    """State A must not contain the old header action buttons."""

    def test_home_no_longer_renders_old_withdrawal_button(self):
        """(1) Old red السحب header button is gone — markup and CSS."""
        html = _html()
        assert "السحب" not in html, "index.html still renders old السحب button"
        assert 'id="btn-withdraw"' not in html, "btn-withdraw still exists"
        assert 'data-action="withdraw"' not in html, \
            "old header withdraw wiring still exists in index.html"
        css = _css()
        assert "#btn-withdraw" not in css, "CSS still styles the old button"
        assert ".header-btn" not in css, "dead .header-btn styles remain"

    def test_home_no_longer_renders_old_deposit_charge_button(self):
        """(2) Old green الشحن header button is gone — markup and CSS."""
        html = _html()
        assert "الشحن" not in html, "index.html still renders old الشحن button"
        assert 'id="btn-charge"' not in html, "btn-charge still exists"
        assert 'data-action="charge"' not in html, \
            "old header charge wiring still exists in index.html"
        css = _css()
        assert "#btn-charge" not in css, "CSS still styles the old button"
        assert 'class="header-actions"' not in html, \
            "old header action bar container remains"


# ══════════════════════════════════════════════════════════════════════
# 3–5. Home wallet button
# ══════════════════════════════════════════════════════════════════════

class TestHomeWalletButton:
    """The circular wallet button beside the profile on Home."""

    def test_home_renders_exactly_one_wallet_button(self):
        """(3) Exactly one wallet button is rendered by Home."""
        content = _home_js()
        count = content.count('class="wallet-button"')
        assert count == 1, f"Expected exactly one wallet button, found {count}"
        # and none live in the static shell (it is dynamic)
        assert 'class="wallet-button"' not in _html(), \
            "Wallet button must be rendered by home.js, not the shell"

    def test_wallet_button_is_interactive(self):
        """(4) It is a real semantic <button>, keyboard accessible."""
        content = _home_js()
        idx = content.find('class="wallet-button"')
        assert idx >= 0
        preceding = content[max(0, idx - 80):idx]
        assert "<button" in preceding, \
            "Wallet button must be a <button> element, not a div"
        assert 'type="button"' in preceding, \
            "Wallet button must be type='button'"
        # click wiring attached after render
        assert "addEventListener('click'" in content, \
            "Wallet button must be wired for click/keyboard activation"
        # visible label from the approved design
        label_zone = content[idx:idx + 400]
        assert "المحفظة" in label_zone, \
            "Wallet button must carry the المحفظة label"
        # accessible name for screen readers comes from the label text;
        # focus state must exist in CSS
        assert ".wallet-button:focus-visible" in _css(), \
            "Wallet button must have a visible keyboard focus state"

    def test_wallet_button_navigates_to_wallet_page(self):
        """(5) Clicking it performs real navigation to the wallet state."""
        content = _home_js()
        assert "Navigation.navigateTo('wallet')" in content, \
            "Wallet button must navigate via the existing router"
        # router knows the wallet page
        assert "page === 'wallet'" in _app_js(), \
            "app.js must route the 'wallet' page"
        assert "Wallet.render" in _app_js(), \
            "app.js must render the Wallet module"
        # navigation module treats wallet as a child of home (not a tab)
        assert "'wallet' ? 'home'" in _navigation_js(), \
            "navigation.js must keep Home active while wallet is open"


# ══════════════════════════════════════════════════════════════════════
# 6–14. Wallet page structure (State B)
# ══════════════════════════════════════════════════════════════════════

class TestWalletPageStructure:
    """The dedicated Wallet page opened from the Home wallet icon."""

    def test_wallet_page_exists(self):
        """(6) The wallet.js module and page element exist."""
        assert os.path.exists("miniapp/js/wallet.js"), \
            "miniapp/js/wallet.js not found"
        content = _wallet_js()
        assert "page-wallet" in content, "Wallet page class missing"
        assert _has_testid(content, "wallet-page"), \
            "Wallet page testid missing"
        assert "wallet-data.js" in _html() and "wallet.js" in _html(), \
            "Wallet scripts must be loaded by index.html"

    def test_wallet_page_title(self):
        """(7) Title is الممحفظة — 'المحفظة'."""
        content = _wallet_js()
        idx = content.find('data-testid="wallet-title"')
        assert idx >= 0, "wallet-title element missing"
        nearby = content[idx:idx + 120]
        assert "المحفظة" in nearby, "Wallet title must be المحفظة"

    def test_wallet_page_has_back_button(self):
        """(8) A semantic, accessible back button exists."""
        content = _wallet_js()
        idx = content.find('data-testid="wallet-back"')
        assert idx >= 0, "wallet-back button missing"
        preceding = content[max(0, idx - 80):idx]
        assert "<button" in preceding, "Back control must be a <button>"
        zone = content[idx:idx + 300]
        assert "aria-label" in zone, "Back button must have an aria-label"

    def test_back_button_returns_to_home(self):
        """(9) Back performs real navigation to Home."""
        content = _wallet_js()
        assert "Navigation.navigateTo('home')" in content, \
            "Back button must navigate to home via the existing router"

    def test_wallet_has_current_balance_card(self):
        """(10) The 'رصيدك الحالي' balance card exists."""
        content = _wallet_js()
        assert "رصيدك الحالي" in content, "رصيدك الحالي card title missing"
        assert _has_testid(content, "wallet-balance-card"), \
            "Balance card testid missing"
        assert 'data-testid="wallet-balance-amount"' in content
        # USDT primary / EGP display-only labels
        assert ">USDT<" in content, "Primary currency label USDT missing"
        assert "EGP" in content, "Display currency label EGP missing"

    def test_wallet_has_withdrawal_action(self):
        """(11) Red السحب action with up arrow inside the Wallet page."""
        content = _wallet_js()
        assert "wallet-action-withdraw" in content, "Withdraw action missing"
        idx = content.find("wallet-action-withdraw")
        zone = content[idx:idx + 300]
        assert "السحب" in zone, "Withdraw label missing"
        assert "⬆" in zone, "Withdraw up arrow missing"
        assert 'data-testid="wallet-withdraw"' in zone
        # it must NOT be on Home
        assert "السحب" not in _html(), "Withdraw action leaked into Home shell"

    def test_wallet_has_deposit_action(self):
        """(12) Green الإيداع action with down arrow inside the Wallet page."""
        content = _wallet_js()
        assert "wallet-action-deposit" in content, "Deposit action missing"
        idx = content.find("wallet-action-deposit")
        zone = content[idx:idx + 300]
        assert "الإيداع" in zone, "Deposit label missing"
        assert "⬇" in zone, "Deposit down arrow missing"
        assert 'data-testid="wallet-deposit"' in zone
        # approved captions under the buttons
        assert "تحويل إلى محفظتك" in content, "Withdraw caption missing"
        assert "شحن رصيدك" in content, "Deposit caption missing"

    def test_wallet_has_transaction_history(self):
        """(13) The 'سجل المعاملات' card exists."""
        content = _wallet_js()
        assert "سجل المعاملات" in content, "سجل المعاملات heading missing"
        assert _has_testid(content, "wallet-history"), \
            "History card testid missing"
        assert 'data-testid="wallet-history-list"' in content

    def test_wallet_has_view_all_transactions(self):
        """(14) The 'عرض كل المعاملات' affordance exists."""
        content = _wallet_js()
        assert "عرض كل المعاملات" in content, "View-all label missing"
        assert 'data-testid="wallet-history-more"' in content
        # list/history icon accompanies it (SVG, no generated assets)
        assert "wallet-history-more-icon" in content


# ══════════════════════════════════════════════════════════════════════
# 15–16. Home level + balance beside the profile
# ══════════════════════════════════════════════════════════════════════

class TestHomeProfileArea:
    """Level and balance render between wallet icon and avatar."""

    def test_home_displays_level(self):
        """(15) Home shows 'المستوى: قريباً'."""
        content = _home_js()
        idx = content.find('data-testid="home-level"')
        assert idx >= 0, "home-level element missing"
        nearby = content[idx:idx + 120]
        assert "المستوى: قريباً" in nearby

    def test_home_has_balance_display_next_to_profile(self):
        """(16) A balance block sits inside the welcome/profile card."""
        content = _home_js()
        welcome = content.find("home-welcome")
        assert welcome >= 0, "welcome section missing"
        balance_section = content.find("home-balance")
        assert balance_section > welcome
        region = content[welcome:balance_section]
        assert 'data-testid="home-wallet-balance"' in region, \
            "Balance display must live in the profile/welcome area"
        assert 'data-testid="home-wallet-usdt"' in region, \
            "USDT line missing from profile balance"
        assert 'data-testid="home-wallet-egp"' in region, \
            "EGP line missing from profile balance"
        assert "USDT" in region and "EGP" in region
        # the wallet button is in the same profile row
        assert 'data-testid="home-wallet-button"' in region, \
            "Wallet button must sit in the profile area"

    def test_home_and_wallet_share_one_data_source(self):
        """The two screens can never contradict each other."""
        assert "WalletData.getBalance()" in _home_js(), \
            "Home must read the shared WalletData source"
        assert "WalletData.getBalance()" in _wallet_js(), \
            "Wallet page must read the shared WalletData source"


# ══════════════════════════════════════════════════════════════════════
# 17–19. Bottom navigation + RTL
# ══════════════════════════════════════════════════════════════════════

class TestNavigationAndRtl:
    """Wallet is not a tab; existing nav and RTL stay intact."""

    def test_wallet_not_in_bottom_navigation(self):
        """(17) Exactly three tabs — no wallet tab."""
        html = _html()
        count = html.count('class="nav-item')
        assert count == 3, f"Expected 3 nav items, found {count}"
        nav_start = html.find("<nav")
        nav_end = html.find("</nav>", nav_start)
        nav_html = html[nav_start:nav_end]
        assert 'data-page="wallet"' not in nav_html, \
            "Wallet must NOT be a bottom-navigation tab"

    def test_existing_navigation_intact(self):
        """(18) Home/Tasks/Profile tabs and templates remain."""
        html = _html()
        for label in ("الرئيسية", "المهام", "حسابي"):
            assert label in html, f"Missing nav label {label}"
        assert 'data-page="home"' in html
        assert 'data-page="tasks"' in html
        assert 'data-page="profile"' in html
        assert 'id="page-tasks"' in html
        assert 'id="page-profile"' in html
        assert 'class="nav-item active" data-page="home"' in html, \
            "Home must stay the default active tab"

    def test_rtl_layout_intact(self):
        """(19) Document stays Arabic RTL."""
        html = _html()
        assert 'dir="rtl"' in html
        assert 'lang="ar"' in html


# ══════════════════════════════════════════════════════════════════════
# 20–21. Responsive + theme
# ══════════════════════════════════════════════════════════════════════

class TestResponsiveAndTheme:
    """Mobile-first widths and the existing dark/neon theme."""

    def test_no_horizontal_overflow_at_mobile_widths(self):
        """(20) Structural guards against overflow down to 320px."""
        css = _css()
        # global box model + horizontal clipping
        assert "box-sizing: border-box" in css
        assert "overflow-x: hidden" in css
        # profile column yields instead of overflowing
        assert "min-width: 0" in css, \
            "Flexible profile/info columns must be allowed to shrink"
        # wallet action row uses fractional columns, not fixed px
        assert "grid-template-columns: 1fr 1fr" in css, \
            "Wallet actions must use fluid 1fr columns"
        # no wallet/welcome rule may force a width above the 320px floor
        rules = re.findall(r"(?:\.wallet-|\.welcome-)[^{]*\{[^}]*\}", css)
        assert rules, "Expected wallet/welcome CSS rules"
        for rule in rules:
            for m in re.finditer(r"(?<!max-)(?<!min-)(?<!min-)width:\s*(\d+)px", rule):
                assert int(m.group(1)) <= 320, \
                    f"Fixed width risks 320px overflow: {rule!r}"

    def test_circles_and_text_cannot_overlap(self):
        """(20) Profile pieces have shrink guards + ellipsis clipping."""
        css = _css()
        # fixed circular frames
        assert re.search(r"\.wallet-button\s*\{[^}]*width:\s*52px", css), \
            "Wallet button must match the 52px avatar circle"
        # the middle column truncates instead of pushing siblings out
        assert ".welcome-info" in css and "min-width: 0" in css
        assert "text-overflow: ellipsis" in css, \
            "Profile text must ellipsize on narrow screens"

    def test_theme_consistent(self):
        """(21) Wallet page reuses the dark/neon system."""
        css = _css()
        # dark page background
        page_wallet = css[css.find(".page-wallet"):]
        page_wallet = page_wallet[:page_wallet.find("}") + 1]
        assert "var(--home-bg)" in page_wallet, \
            "Wallet page must use the shared dark background"
        # red neon card
        balance = css[css.find(".wallet-balance-card"):]
        balance = balance[:balance.find("}") + 1]
        assert "neon-red-border" in balance, "Balance card must use neon border"
        assert "box-shadow" in balance and "neon-red-glow" in balance, \
            "Balance card must glow like Home cards"
        # wallet button ring matches the avatar ring language
        button = css[css.find(".wallet-button"):]
        button = button[:button.find("}") + 1]
        assert "var(--neon-red)" in button, \
            "Wallet button must keep the neon red ring"
        # green accent + gold EGP display exist
        assert "--neon-green" in css
        assert "--home-gold" in css


# ══════════════════════════════════════════════════════════════════════
# 22. No fake financial data
# ══════════════════════════════════════════════════════════════════════

class TestNoFakeFinancialData:
    """Never present invented money or history as real user data."""

    def test_no_hardcoded_balance_amounts(self):
        """(22) Example amounts from the mock are not hard-coded."""
        for path in ("miniapp/js/home.js", "miniapp/js/wallet.js",
                     "miniapp/js/wallet-data.js"):
            content = _read(path)
            for literal in ("12.50000000", "625.00", "12.5", "625"):
                assert literal not in content, \
                    f"Hard-coded example amount '{literal}' found in {path}"

    def test_no_fake_transaction_history(self):
        """(22) No invented rows are presented as real transactions."""
        data = _wallet_data_js()
        # the data source is explicitly empty until a real API exists
        assert re.search(
            r"function getTransactions\(\)\s*\{\s*return \[\];", data
        ), "getTransactions() must return an empty list — no fake rows"
        wallet = _wallet_js()
        for literal in ("-5.00000000", "+10.00000000", "-2.50000000",
                        "+5.00000000", "-1.00000000", "2026-08", "2026-07"):
            assert literal not in wallet, \
                f"Fake transaction literal '{literal}' found in wallet.js"

    def test_empty_history_state_rendered(self):
        """(22) The correct future-ready empty state exists."""
        content = _wallet_js()
        assert _has_testid(content, "wallet-history-empty"), \
            "Empty history state element missing"
        assert "لا توجد معاملات بعد" in content, \
            "Empty history state text missing"

    def test_balance_placeholder_never_invents_numbers(self):
        """(22) Unknown balance renders an em-dash, not a number."""
        data = _wallet_data_js()
        assert "return '—'" in data, \
            "formatUsdt/formatEgp must fall back to an em-dash"
        assert "availableUnits: null" in data, \
            "Balance must start unknown — no fake persistent balance"


# ══════════════════════════════════════════════════════════════════════
# 23–24. UI-only scope boundary
# ══════════════════════════════════════════════════════════════════════

class TestUiOnlyScope:
    """MT-UI-03 adds no financial/business logic."""

    def test_no_wallet_or_ledger_mutation_code(self):
        """(23) No SQL/wallet/ledger mutation appears anywhere in the UI."""
        banned = [
            "INSERT INTO", "UPDATE wallets", "DELETE FROM",
            "BEGIN IMMEDIATE", "COMMIT", "sqlite",
            "reserve_units", "settle_units", "release_units",
            "record_credit", "record_debit", "record_hold",
            "record_release", "record_settlement",
            "available_units", "held_units",
        ]
        for path in _all_js():
            content = _read(path)
            for token in banned:
                assert token not in content, \
                    f"Wallet/ledger mutation token '{token}' found in {path}"

    def test_no_withdrawal_deposit_business_logic(self):
        """(24) No financial flows, float maths, or network calls."""
        banned = [
            "fetch(", "XMLHttpRequest", "axios", "$.ajax",
            "parseFloat", "toFixed", "Math.round",
            "exchangeRate", "exchange_rate", "usdt_egp",
            "createWithdrawal", "createDeposit", "processPayment",
        ]
        for path in ("miniapp/js/wallet.js", "miniapp/js/wallet-data.js",
                     "miniapp/js/home.js"):
            content = _read(path)
            for token in banned:
                assert token not in content, \
                    f"Business-logic token '{token}' found in {path}"

    def test_formatting_is_integer_only(self):
        """Formatting uses integer-exact operations (no float maths)."""
        data = _wallet_data_js()
        assert "USDT_UNITS_PER_USDT = 100000000" in data, \
            "Integer unit constant missing"
        assert "EGP_MINOR_PER_EGP = 100" in data, \
            "Integer EGP minor-unit constant missing"
        # integer ops only
        assert "Math.floor" in data
        assert "parseFloat" not in data and "toFixed" not in data

    def test_action_buttons_are_ui_only(self):
        """(24) Wallet actions are semantic buttons with haptic-only
        acknowledgement — the existing haptic helper, no new system."""
        content = _wallet_js()
        assert 'type="button"' in content
        assert "HapticFeedback" in content, \
            "Must reuse the existing Telegram haptic helper"
        assert "impactOccurred('light')" in content, \
            "Use the same haptic call as the rest of the app"
        # nothing else happens on click besides navigation/haptic
        assert "fetch(" not in content


# ══════════════════════════════════════════════════════════════════════
# Architecture reuse checks
# ══════════════════════════════════════════════════════════════════════

class TestArchitectureReuse:
    """The wallet integrates into the existing Mini App architecture."""

    def test_scripts_loaded_in_dependency_order(self):
        html = _html()
        order = [
            "js/telegram.js", "js/header.js", "js/navigation.js",
            "js/wallet-data.js", "js/wallet.js", "js/home.js", "js/app.js",
        ]
        positions = [html.find(f'<script src="{src}">') for src in order]
        assert all(p >= 0 for p in positions), "A required script is missing"
        assert positions == sorted(positions), \
            "Scripts must load in dependency order"

    def test_no_second_navigation_architecture(self):
        """Routing stays inside the existing Navigation module."""
        content = _navigation_js()
        assert "function navigateTo" in content
        # wallet page uses it — no pushState/window.history API introduced
        for path in ("miniapp/js/home.js", "miniapp/js/wallet.js"):
            js = _read(path)
            assert "pushState" not in js and "window.history" not in js, \
                f"{path} must not introduce a second router"

    def test_empty_header_collapses(self):
        """The removed header bar must not leave a dead 56px strip."""
        css = _css()
        assert ".app-header:empty" in css, "Empty header must collapse"
        assert re.search(r"\.app-content\s*\{[^}]*margin-top:\s*0", css), \
            ".app-content must start at the top without the old header"

    def test_wallet_icon_is_inline_svg_or_text(self):
        """No generated image assets — inline SVG only."""
        content = _wallet_js()
        assert "<svg" in content, "Wallet icon should be inline SVG"
        assert "WalletIcon" in content, "Shared icon markup missing"
        for path in _all_js():
            js = _read(path)
            assert ".png" not in js and ".jpg" not in js \
                and ".webp" not in js and ".gif" not in js, \
                f"Generated image asset referenced in {path}"
