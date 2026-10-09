"""
Focused tests — Mini App reviewer UI (MT-TASK-18)
==================================================

Reviewer-facing Mini App UI for the existing manual/social proof
family, against the unchanged reviewer API contract:

- entry + routing wired through the existing Navigation module
- the Account entry ships HIDDEN and is revealed only after the server
  itself identifies the caller as a reviewer (same ok /claims verdict
  the review page renders from) — never offered to a non-reviewer
- authority decided ONLY by the server: reviewer controls render
  only from an ok GET /api/tasks/<id>/claims response; anyone else
  gets the denied state with zero data
- claims fetched from the existing endpoint with the initData header
- only the safe claim fields (claim_id, task_id, submitted_at,
  proof_ref) are read/displayed — proof shown in full for inspection
- approve/reject POST only {decision} to the existing decision
  endpoint, with duplicate-click protection while in flight
- after every decision the page re-reads server state; no client
  authority, no browser storage
- loading / empty / denied / error states, Arabic RTL, theme tokens
- no worker/reviewer identity, task_data or reward/wallet leakage
- worker UI (tasks.js) and the other task families stay unchanged

Run:
    python3 -m pytest test_miniapp_reviewer_ui.py -v
"""

import os
import re


# ── Helpers ────────────────────────────────────────────────────────────


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _review_js() -> str:
    return _read("miniapp/js/review.js")


def _tasks_js() -> str:
    return _read("miniapp/js/tasks.js")


def _app_js() -> str:
    return _read("miniapp/js/app.js")


def _navigation_js() -> str:
    return _read("miniapp/js/navigation.js")


def _html() -> str:
    return _read("miniapp/index.html")


def _css() -> str:
    return _read("miniapp/css/app.css")


def _branch(content: str, start: str, end: str | None = None) -> str:
    begin = content.find(start)
    assert begin >= 0, f"marker not found: {start!r}"
    if end is None:
        return content[begin:]
    stop = content.find(end, begin)
    assert stop > begin, f"branch end marker not found: {end!r}"
    return content[begin:stop]


def _load_fn() -> str:
    return _branch(_review_js(), "async function load()", "/* ── Claim list")


def _decide_fn() -> str:
    return _branch(_review_js(), "async function decide", "return {")


# ════════════════════════════════════════════════════════════════════
# 1. Module wiring — entry, route, script, active tab
# ════════════════════════════════════════════════════════════════════


class TestReviewerModuleWiring:
    def test_review_js_exists(self):
        assert os.path.exists("miniapp/js/review.js"), \
            "miniapp/js/review.js not found"

    def test_review_module_exports_render(self):
        content = _review_js()
        assert "const Review" in content
        assert "return {" in content
        assert "render" in content

    def test_review_script_loaded_before_app(self):
        html = _html()
        tasks_pos = html.find('src="js/tasks.js"')
        review_pos = html.find('src="js/review.js"')
        app_pos = html.find('src="js/app.js"')
        assert review_pos >= 0, "review.js must be loaded in index.html"
        assert tasks_pos >= 0 and app_pos >= 0
        assert tasks_pos < review_pos < app_pos, \
            "scripts must load in dependency order (tasks < review < app)"

    def test_app_routes_review_page(self):
        content = _app_js()
        assert "page === 'review'" in content, \
            "app.js must route the review page"
        assert "Review.render()" in content, \
            "app.js must render the Review module"

    def test_account_entry_opens_review_page(self):
        html = _html()
        tpl_start = html.find('<template id="page-profile">')
        tpl_end = html.find("</template>", tpl_start)
        assert tpl_start >= 0 and tpl_end > tpl_start
        tpl = html[tpl_start:tpl_end]
        assert 'data-goto="review"' in tpl, \
            "the Account page must hold the reviewer entry"
        assert "مراجعة الإثباتات" in tpl, "the entry must be Arabic"

    def test_entry_reuses_existing_router(self):
        """No second router: the entry navigates via Navigation."""
        app = _app_js()
        assert "[data-goto]" in app
        assert "Navigation.navigateTo(el.dataset.goto)" in app
        assert "pushState" not in app and "window.history" not in app

    def test_review_not_a_bottom_nav_tab(self):
        """The nav keeps exactly three tabs; review is not one."""
        html = _html()
        assert html.count('class="nav-item') == 3
        nav = html[html.find("<nav"):html.find("</nav>")]
        assert "review" not in nav

    def test_review_keeps_account_tab_active(self):
        nav = _navigation_js()
        assert "'wallet' ? 'home'" in nav, \
            "wallet mapping must stay untouched"
        assert "page === 'review' ? 'profile'" in nav, \
            "the review page must keep the Account tab active"


# ════════════════════════════════════════════════════════════════════
# 2. Server decides authority — authorized rendering vs denied state
# ════════════════════════════════════════════════════════════════════


class TestServerDecidedAuthority:
    def test_manual_tasks_only_are_probed(self):
        content = _review_js()
        assert "task.type === 'manual'" in content, \
            "only manual-family tasks may be probed for claims"

    def test_controls_render_only_from_ok_claims_response(self):
        load = _load_fn()
        gate = "if (claimsOk && Array.isArray(claimsData.claims))"
        assert gate in load, "claims must be gated on the server's ok"
        assert load.find(gate) < load.find("_renderSections(pending)"), \
            "reviewer controls must never render before the verdict"

    def test_unauthorized_gets_denied_state_with_zero_data(self):
        content = _review_js()
        load = _load_fn()
        assert "if (sections.length === 0)" in load
        denied_pos = load.find("_showState('denied')")
        render_pos = load.find("_renderSections(pending)")
        assert denied_pos >= 0, "denied state missing"
        assert denied_pos < render_pos, \
            "the denied branch must return before any claim rendering"
        assert 'data-testid="review-denied"' in content
        assert "لا توجد لديك صلاحية مراجعة هذه المهام" in content, \
            "the denied state must be Arabic"

    def test_not_approver_code_has_arabic_fallback(self):
        content = _review_js()
        match = re.search(
            r"ERROR_MESSAGES\s*=\s*\{(.*?)\n\s*\}", content, re.DOTALL
        )
        assert match, "ERROR_MESSAGES map missing"
        entry = re.search(r"not_approver:\s*'([^']+)'", match.group(1))
        assert entry, "not_approver must map to an Arabic message"
        assert re.search(r"[؀-ۿ]", entry.group(1))

    def test_identity_never_comes_from_the_client(self):
        """Authority rides the verified initData header only."""
        content = _review_js()
        assert "'X-Telegram-Init-Data'" in content
        assert "TelegramApp.getInitData" in content
        assert "_headers()" in content
        for forbidden in ("user_id", "userId", "telegram_user_id"):
            assert forbidden not in content, \
                f"client identity field present: {forbidden}"


# ════════════════════════════════════════════════════════════════════
# 3. Claims fetch contract
# ════════════════════════════════════════════════════════════════════


class TestClaimsFetchContract:
    def test_fetches_claims_from_existing_endpoint(self):
        content = _review_js()
        assert "`/api/tasks/${task.id}/claims`" in content

    def test_no_new_or_invented_endpoints(self):
        """Only the three existing endpoints, nothing else.

        The Account-entry probe re-reads two of them (catalog +
        claims) instead of inventing an access endpoint, so this
        guards WHERE every fetch goes rather than how many there are.
        """
        content = _review_js()
        allowed = ("LIST_URL",
                   "/api/tasks/${task.id}/claims",
                   "/api/tasks/${taskId}/claims/${claimId}/decision")
        for call in re.findall(r"fetch\((.{0,80})", content, re.DOTALL):
            assert any(target in call for target in allowed), \
                f"fetch call to an unexpected target: {call!r}"
        urls = re.findall(r"['\"`]/api/[^'\"`]+['\"`]", content)
        assert set(urls) == {
            "'/api/tasks'",
            "`/api/tasks/${task.id}/claims`",
            "`/api/tasks/${taskId}/claims/${claimId}/decision`",
        }, f"unexpected API URLs: {urls}"

    def test_claims_request_rides_existing_auth(self):
        content = _review_js()
        pos = content.find("`/api/tasks/${task.id}/claims`")
        assert pos >= 0
        window = content[pos:pos + 200]
        assert "headers: _headers()" in window, \
            "the claims probe must ride the initData headers"

    def test_catalog_used_to_discover_manual_tasks(self):
        load = _load_fn()
        assert "fetch(LIST_URL" in load
        assert "LIST_URL = '/api/tasks'" in _review_js()
        assert "Array.isArray(data.tasks)" in load, \
            "tasks list must come from the API response"


# ════════════════════════════════════════════════════════════════════
# 4. Safe claim fields only + proof display
# ════════════════════════════════════════════════════════════════════


class TestSafeFieldsAndProofDisplay:
    def test_only_safe_claim_fields_are_read(self):
        fields = set(re.findall(r"claim\.(\w+)", _review_js()))
        assert fields == {"claim_id", "task_id", "submitted_at",
                          "proof_ref"}, \
            f"unexpected claim fields read: {fields}"

    def test_only_catalog_display_fields_are_read(self):
        fields = set(re.findall(r"task\.(\w+)", _review_js()))
        assert fields == {"id", "type", "title"}, \
            f"unexpected task fields read: {fields}"

    def test_claim_card_renders_each_safe_field(self):
        content = _review_js()
        for tid in ("review-claim-id", "review-claim-task",
                    "review-claim-date", "review-proof-ref"):
            assert f"'{tid}'" in content, f"missing claim field: {tid}"

    def test_proof_rendered_in_full_as_text_or_safe_link(self):
        content = _review_js()
        assert "proof.textContent =" in content, \
            "the proof must be rendered as text, never as markup"
        assert "startsWith('https://')" in content, \
            "links must be accepted only over https"
        assert "proof.href = claim.proof_ref" in content
        assert "proof.rel = 'noopener'" in content
        assert "insertAdjacentHTML" not in content

    def test_api_strings_never_become_markup(self):
        content = _review_js()
        assignments = re.findall(r"\.innerHTML\s*=\s*([^;]+);", content)
        for value in assignments:
            assert value.strip() in ("''",) or value.strip().startswith("`"), \
                f"innerHTML assigned from dynamic data: {value!r}"
        # The dynamic parts are always textContent.
        assert ".textContent = task.title" in content
        assert ".textContent = typeof claim.proof_ref" in content


# ════════════════════════════════════════════════════════════════════
# 5. Approve / reject requests through the existing decision endpoint
# ════════════════════════════════════════════════════════════════════


class TestDecisionRequests:
    def test_decision_posts_to_existing_endpoint(self):
        decide = _decide_fn()
        assert "`/api/tasks/${taskId}/claims/${claimId}/decision`" in decide
        assert "method: 'POST'" in decide
        assert "'Content-Type'" in decide
        assert "application/json" in decide

    def test_body_is_only_the_decision_field(self):
        decide = _decide_fn()
        body_fields = re.findall(r"JSON\.stringify\(\{\s*(\w+):", decide)
        assert body_fields == ["decision"], \
            f"only the decision field may be sent, found: {body_fields}"
        assert "JSON.stringify({ decision: decision })" in decide

    def test_controls_offer_both_contract_decisions(self):
        content = _review_js()
        assert "'approve'" in content, "the accept action is required"
        assert "'reject'" in content, "the reject action is required"
        assert "decide(taskId, claim.claim_id, 'approve')" in content
        assert "decide(taskId, claim.claim_id, 'reject')" in content

    def test_decision_buttons_are_arabic(self):
        content = _review_js()
        assert "'قبول'" in content
        assert "'رفض'" in content
        assert re.search(r"[؀-ۿ]", "قبول")
        assert re.search(r"[؀-ۿ]", "رفض")


# ════════════════════════════════════════════════════════════════════
# 6. Duplicate-click protection + server-driven refresh
# ════════════════════════════════════════════════════════════════════


class TestInFlightAndRefresh:
    def test_busy_flag_blocks_duplicate_decisions(self):
        decide = _decide_fn()
        assert "if (busy) {" in decide, "re-entry guard missing"
        assert decide.find("if (busy)") < decide.find("fetch("), \
            "the guard must run before the request"
        assert "busy = true;" in decide
        assert "busy = false;" in decide

    def test_buttons_disabled_while_in_flight(self):
        decide = _decide_fn()
        disable_pos = decide.find("_setDecisionButtonsEnabled(false)")
        fetch_pos = decide.find("fetch(")
        assert disable_pos >= 0, "decision controls must be disabled"
        assert disable_pos < fetch_pos, \
            "disable must happen before the request goes out"

    def test_decision_re_reads_server_state(self):
        decide = _decide_fn()
        assert "await load();" in decide, \
            "the page must refresh from the server after a decision"
        assert decide.find("fetch(") < decide.find("await load();"), \
            "the refresh must happen after the decision response"

    def test_no_client_authority_or_storage(self):
        content = _review_js()
        for banned in ("localStorage", "sessionStorage", "indexedDB",
                       "let claimsCache", "claimsCache"):
            assert banned not in content, \
                f"client-side decision state found: {banned}"
        # The decision outcome is never written into the card locally.
        decide = _decide_fn()
        assert "_replaceTask" not in decide
        assert re.search(r"approval\s*[:=]", decide) is None, \
            "the approval outcome must not become client state"


# ════════════════════════════════════════════════════════════════════
# 7. Loading / empty / error states (Arabic)
# ════════════════════════════════════════════════════════════════════


class TestPageStates:
    def test_all_state_nodes_exist(self):
        content = _review_js()
        for tid in ("review-loading", "review-empty", "review-denied",
                    "review-error", "review-error-text", "review-retry",
                    "review-notice", "review-list"):
            assert f'data-testid="{tid}"' in content, \
                f"missing state node: {tid}"

    def test_states_switch_through_one_helper(self):
        content = _review_js()
        match = re.search(
            r"const states = \[([^\]]+)\]", content
        )
        assert match, "state helper missing"
        keys = re.findall(r"'(\w+)'", match.group(1))
        assert keys == ["loading", "empty", "denied", "error", "list"]

    def test_states_are_arabic(self):
        content = _review_js()
        for text in ("جارٍ تحميل الطلبات", "لا توجد طلبات بانتظار المراجعة",
                     "لا توجد لديك صلاحية مراجعة هذه المهام"):
            assert text in content, f"missing Arabic state: {text}"

    def test_error_state_has_retry_and_server_message(self):
        load = _load_fn()
        assert "_setErrorText(_messageFor(data))" in load
        assert "_setErrorText(ERROR_MESSAGES.network)" in load
        content = _review_js()
        assert 'data-testid="review-retry"' in content
        assert "load();" in content

    def test_server_message_wins_when_provided(self):
        content = _review_js()
        assert "data.message" in content, \
            "the backend's Arabic message must be preferred"

    def test_empty_state_when_no_claims_await_review(self):
        load = _load_fn()
        assert "_showState('empty')" in load
        assert "section.claims.length" in load


# ════════════════════════════════════════════════════════════════════
# 8. No worker/reviewer identity or sensitive data leakage
# ════════════════════════════════════════════════════════════════════


class TestNoIdentityLeakage:
    def test_no_worker_or_reviewer_identity_fields(self):
        content = _review_js()
        for forbidden in ("user_id", "userId", "username", "chat_id",
                          "telegram_user_id", "first_name", "last_name"):
            assert forbidden not in content, forbidden

    def test_no_task_data_or_approver_identity(self):
        content = _review_js()
        assert "task_data" not in content
        assert "channel_slug" not in content
        assert "channel_id" not in content
        assert "approver.telegram_user_id" not in content
        # The only occurrence of "approver" allowed anywhere is the
        # backend's stable error code fallback (not_approver).
        assert "approver" not in content.replace("not_approver", ""), \
            "reviewer identity language leaked into the client"

    def test_no_wallet_or_reward_surface(self):
        content = _review_js().lower()
        for banned in ("wallet", "reward", "balance", "deposit",
                       "withdraw", "المحفظة", "الرصيد", "السحب",
                       "الشحن", "المكافأة"):
            assert banned not in content, \
                f"wallet/reward control leaked into review: {banned}"

    def test_no_sql_or_ledger_tokens(self):
        content = _review_js()
        for banned in ("sqlite", "SELECT ", "record_credit",
                       "reserve_units", "available_units", "COMMIT"):
            assert banned not in content, banned

    def test_no_backend_or_upload_surface(self):
        content = _review_js()
        for banned in ("FormData", "FileReader", "multipart",
                       "type = 'file'"):
            assert banned not in content, banned


# ════════════════════════════════════════════════════════════════════
# 9. Existing worker UI and task families unchanged
# ════════════════════════════════════════════════════════════════════


class TestExistingFamiliesUnchanged:
    def test_worker_tasks_page_has_no_reviewer_surface(self):
        content = _tasks_js()
        assert "/decision" not in content
        assert "/claims" not in content
        for quoted in ("'approve'", "'reject'", '"approve"',
                       '"reject"', "'approved'", "'rejected'"):
            assert quoted not in content, quoted

    def test_worker_type_labels_untouched(self):
        match = re.search(
            r"TYPE_LABELS\s*=\s*\{(.*?)\}", _tasks_js(), re.DOTALL
        )
        assert match, "TYPE_LABELS missing"
        labels = dict(re.findall(r"(\w+)\s*:\s*'([^']+)'", match.group(1)))
        assert labels["channel_subscription"] == "اشتراك في قناة"
        assert labels["deterministic"] == "مهمة تحقق"
        assert labels["referral_task"] == "مهمة إحالة"
        assert labels["telegram_channel"] == "انضمام عبر تيليجرام"
        assert labels["manual"] == "مهمة يدوية"

    def test_review_module_does_not_touch_worker_module(self):
        content = _review_js()
        assert "Tasks." not in content, \
            "the reviewer page must not drive the worker Tasks module"

    def test_tasks_template_and_route_kept(self):
        html = _html()
        assert 'id="page-tasks"' in html
        assert 'id="page-profile"' in html
        app = _app_js()
        assert "page === 'tasks'" in app
        assert "Tasks.render()" in app
        assert "document.body.dataset.page = page" in app

    def test_profile_template_has_no_withdraw_charge_buttons(self):
        html = _html()
        start = html.find('<template id="page-profile">')
        end = html.find("</template>", start)
        tpl = html[start:end]
        for banned in ("السحب", "الشحن", "btn-withdraw", "btn-charge",
                       "btn-arrow", "header-btn"):
            assert banned not in tpl, f"Found '{banned}' in profile template"


# ════════════════════════════════════════════════════════════════════
# 10. Arabic RTL styling within the existing theme
# ════════════════════════════════════════════════════════════════════


class TestReviewStyling:
    def test_review_styles_exist(self):
        css = _css()
        for selector in (".review-entry", ".review-list", ".review-task",
                         ".review-claim", ".review-proof",
                         ".review-action-btn", ".review-approve-btn",
                         ".review-reject-btn"):
            assert selector in css, f"missing Review style: {selector}"

    def test_review_cards_use_theme_tokens(self):
        css = _css()
        block = _branch(css[css.find(".review-task {"):], ".review-task {")
        block = block[:block.find("}")]
        assert "--home-card-inner-bg" in block, \
            "review cards must use the existing dark card token"
        approve = css[css.find(".review-approve-btn {"):]
        approve = approve[:approve.find("}")]
        assert "--success-color" in approve, \
            "approve must keep the success-green language"
        reject = css[css.find(".review-reject-btn {"):]
        reject = reject[:reject.find("}")]
        assert "--danger-color" in reject, \
            "reject must keep the danger-red language"

    def test_review_shell_reuses_existing_state_classes(self):
        content = _review_js()
        for cls in ("tasks-state", "tasks-retry-btn", "tasks-notice"):
            assert f'class="{cls}' in content or f"{cls} " in content, \
                f"review shell must reuse the existing {cls} styling"

    def test_rtl_and_language_preserved(self):
        html = _html()
        assert 'dir="rtl"' in html
        assert 'lang="ar"' in html

    def test_page_header_is_arabic(self):
        content = _review_js()
        assert "<h2>مراجعة الإثباتات</h2>" in content


# ════════════════════════════════════════════════════════════════════
# 11. Account entry visibility — the server decides who sees it
# ════════════════════════════════════════════════════════════════════


def _profile_template() -> str:
    html = _html()
    start = html.find('<template id="page-profile">')
    end = html.find("</template>", start)
    assert start >= 0 and end > start, "page-profile template not found"
    return html[start:end]


def _profile_js() -> str:
    return _read("miniapp/js/profile.js")


def _probe_fn() -> str:
    return _branch(_review_js(), "async function probeAccess",
                   "/* ── Loading / states")


class TestAccountEntryVisibility:
    """The reviewer entry is a reviewer-only surface.

    It used to ship visible in the static Account template, so EVERY
    user — reviewer or not — was offered «مراجعة الإثباتات» on their
    profile page and only discovered the denial after tapping it.  The
    entry now ships hidden and is revealed exclusively from the
    server's own authority verdict.
    """

    def test_entry_ships_hidden_on_the_account_page(self):
        tpl = _profile_template()
        assert re.search(
            r'<button[^>]*data-testid="review-entry"[^>]*\bhidden\b', tpl
        ), ("the reviewer entry must ship hidden — no user may be "
            "offered the reviewer surface before the server answers")
        assert 'data-goto="review"' in tpl, \
            "the hidden entry still routes to the review page"

    def test_hidden_entry_gets_its_own_display_none_rule(self):
        """`.review-entry` sets `display: flex`, which outranks the
        user-agent `[hidden] { display: none }` rule — without a
        matching rule the `hidden` attribute would not hide it."""
        css = _css()
        assert "display: flex" in _branch(css, ".review-entry {", "}"), \
            "the entry is a flex button, so [hidden] needs its own rule"
        assert "display: none" in _branch(css, ".review-entry[hidden] {", "}"), \
            ".review-entry[hidden] must actually hide the entry"

    def test_entry_is_revealed_only_from_the_server_verdict(self):
        app = _app_js()
        assert "page === 'profile'" in app, \
            "the reveal belongs to the Account page only"
        assert "Review.probeAccess()" in app, \
            "visibility must follow the server probe, not the client"
        reveal = _branch(app, "Review.probeAccess().then", "});")
        assert "allowed === true" in reveal, \
            "the entry is unlocked only on an explicit true verdict"
        assert "_unlockReviewEntry(entry)" in reveal
        assert "entry.hidden = !allowed" not in app, \
            "visibility must not be derived from a truthy/falsy value"

    def test_entry_is_locked_inline_not_only_by_the_hidden_attribute(self):
        """The critical case: a client holding the OLD app.css.

        `.review-entry { display: flex }` is an author rule, so it
        outranks the user-agent `[hidden] { display: none }` rule — the
        attribute alone leaves the button rendered there.  The lock must
        therefore be inline `!important` state, which no cached
        stylesheet can override.
        """
        app = _app_js()
        lock = _branch(app, "function _lockReviewEntry", "function _unlockReviewEntry")
        assert "entry.hidden = true" in lock
        assert "entry.style.setProperty('display', 'none', 'important')" in lock, \
            "the lock must be inline !important state, independent of CSS"
        unlock = _branch(app, "function _unlockReviewEntry", "\n    }")
        assert "entry.hidden = false" in unlock
        assert "entry.style.removeProperty('display')" in unlock

    def test_lock_runs_before_and_independently_of_the_probe(self):
        """MIX A guard: new app.js + OLD review.js + OLD app.css.

        Telegram was serving exactly that mix, and the old review.js has
        no `probeAccess` at all.  The lock must therefore run while
        building the page — NOT inside a probe guard — so a missing or
        stale Review module still leaves the entry unrenderable.
        """
        app = _app_js()
        gate = _branch(app, "if (page === 'profile') {", "// Add enter animation")
        lock_at = gate.find("_lockReviewEntry(entry);")
        probe_at = gate.find("Review.probeAccess()")
        assert lock_at >= 0, "the entry must be locked on the Account page"
        assert probe_at > lock_at >= 0, \
            "the entry is locked before the authority probe is even called"
        assert "typeof Review.probeAccess === 'function'" in gate, \
            "a stale/absent Review module must skip the probe, not the lock"
        # Nothing inside the probe guard may contain the lock itself.
        assert "_lockReviewEntry" not in gate[probe_at:], \
            "the lock must not live inside the probe guard"

    def test_no_other_path_can_reveal_the_entry(self):
        app = _app_js()
        assert app.count("_unlockReviewEntry") == 2, \
            "one definition and exactly one call site (the true verdict)"
        for banned in ("entry.style.display", "removeAttribute('hidden')",
                       "entry.removeAttribute"):
            assert banned not in app, \
                f"visibility must only change through the lock helpers: {banned!r}"

    def test_probe_uses_the_unchanged_reviewer_contract(self):
        probe = _probe_fn()
        assert "fetch(LIST_URL, { headers: _headers() })" in probe, \
            "the catalog read carries the verified initData header"
        assert "entry.type === 'manual'" in probe, \
            "only manual-family tasks carry the reviewer surface"
        assert "/api/tasks/${task.id}/claims" in probe, \
            "authority is probed through the existing claims endpoint"
        assert "response.ok && claimsData && claimsData.ok === true" in probe, \
            "only an ok claims answer may count as reviewer authority"

    def test_probe_fails_closed(self):
        probe = _probe_fn()
        assert probe.count("return false") >= 2, \
            "a failed catalog read or a failed verdict must stay hidden"
        for banned in ("localStorage", "sessionStorage", "indexedDB",
                       "reviewer_id", "user_id", "approver"):
            assert banned not in probe, \
                f"the probe must not carry authority or identity: {banned!r}"

    def test_probe_is_exported_for_the_router(self):
        assert "probeAccess," in _branch(_review_js(), "return {", "})();"), \
            "app.js needs Review.probeAccess to gate the entry"


# ══════════════════════════════════════════════════════════════
# 12. Account page identity — the caller's own Telegram data
# ══════════════════════════════════════════════════════════════


class TestProfileIdentity:
    """The Account page renders the caller's own Telegram identity.

    The page used to be a static «معلومات الحساب» placeholder.
    The Profile module renders the same identity Home's
    welcome card shows — from the SAME TelegramApp.getUser()
    source, with the same neutral placeholders.  The account
    figures (balance / earnings / task counts) come from the
    real GET /api/me summary — see TestProfileAccountData —
    and no XP/level exists anywhere in the backend, so none
    is ever shown.
    """

    def test_profile_js_exists(self):
        assert os.path.exists("miniapp/js/profile.js"), \
            "miniapp/js/profile.js not found"

    def test_profile_module_exports_render(self):
        content = _profile_js()
        assert "const Profile" in content
        assert "return {" in content
        assert "render" in content

    def test_profile_script_loaded_before_app(self):
        html = _html()
        telegram_pos = html.find('src="js/telegram.js"')
        profile_pos = html.find('src="js/profile.js"')
        app_pos = html.find('src="js/app.js"')
        assert profile_pos >= 0, "profile.js must be loaded in index.html"
        assert telegram_pos < profile_pos < app_pos, \
            "scripts must load in dependency order (telegram < profile < app)"

    def test_app_routes_profile_page_to_the_module(self):
        content = _app_js()
        assert "page === 'profile'" in content
        assert "Profile.render()" in content

    def test_identity_uses_the_home_extraction_convention(self):
        """Same data source and optional-chaining fallbacks as Home."""
        content = _profile_js()
        assert "TelegramApp.getUser()" in content
        for field in ("first_name", "last_name", "username",
                      "id", "photo_url"):
            assert f"user?.{field}" in content, \
                f"missing identity field: {field}"

    def test_identity_values_never_become_markup(self):
        content = _profile_js()
        for assignment in re.findall(r"\.innerHTML\s*=\s*([^;]+);", content):
            assert "${" not in assignment, \
                f"innerHTML interpolated with dynamic data: {assignment!r}"
        assert ".textContent =" in content

    def test_photo_is_used_only_over_https(self):
        content = _profile_js()
        assert "startsWith('https://')" in content

    def test_username_row_renders_only_when_present(self):
        content = _profile_js()
        assert 'data-testid="profile-username"' in content
        assert "usernameEl.hidden = false" in content
        assert "usernameEl.hidden = true" in content

    def test_missing_identity_keeps_neutral_placeholders(self):
        content = _profile_js()
        assert "'—'" in content

    def test_no_invented_figures_and_no_level(self):
        """No XP/level exists anywhere in the backend, so no
        level surface may appear; the wallet page itself
        (المحفظة) and EGP equivalents stay out of the
        Account page — every figure comes from the confirmed
        GET /api/me response (TestProfileAccountData)."""
        content = _profile_js()
        for banned in ("المحفظة", "المستوى", "EGP", "level"):
            assert banned not in content, \
                f"invented wallet-page/level surface in profile: {banned}"

    def test_reviewer_entry_stays_fail_closed(self):
        content = _profile_js()
        assert 'data-testid="review-entry"' in content
        assert 'data-goto="review"' in content
        assert re.search(
            r'<button[^>]*data-testid="review-entry"[^>]*\bhidden\b', content
        ), "the reviewer entry must ship hidden in the Profile render"
        assert "مراجعة الإثباتات" in content

    def test_profile_styles_reuse_the_home_tokens(self):
        css = _css()
        for selector in (".profile-section", ".profile-field",
                         ".profile-field-label", ".profile-field-value"):
            assert selector in css, f"missing Profile style: {selector}"
        card = _branch(css, ".page-profile .welcome-card {", "}")
        assert "var(--home-card-bg)" in card
        assert "var(--border-color)" in card
        field = _branch(css, ".profile-field {", "}")
        assert "var(--home-card-inner-bg)" in field
        assert "var(--border-color)" in field


class TestProfileAccountData:
    """The Account page shows the caller's REAL account
    figures from GET /api/me — the read-only account
    summary — and keeps its neutral placeholders whenever
    the backend did not confirm a value."""

    def test_account_section_ships_with_placeholders(self):
        content = _profile_js()
        assert 'data-testid="profile-account"' in content
        for testid in ("profile-balance", "profile-earnings",
                       "profile-completed", "profile-progress"):
            assert f'data-testid="{testid}"' in content
            # every figure starts neutral — never a guessed number
            row = _branch(content, f'data-testid="{testid}"',
                          "</span>")
            assert row.rstrip().endswith("—"), \
                f"{testid} must ship as a neutral placeholder"

    def test_account_labels(self):
        content = _profile_js()
        for label in ("الرصيد المتاح", "إجمالي الأرباح",
                      "المهام المكتملة", "قيد التنفيذ"):
            assert label in content, f"missing account label: {label}"

    def test_account_data_comes_from_the_api(self):
        content = _profile_js()
        assert "'/api/me'" in content
        assert "X-Telegram-Init-Data" in content
        assert "TelegramApp.getInitData()" in content
        assert "fetch(" in content

    def test_figures_replace_placeholders_only_on_confirmed_data(self):
        content = _profile_js()
        # the loader gates every write on a confirmed ok response
        assert "response.ok" in content
        assert "data.ok === true" in content
        # exact integer checks — no float maths, no guessing
        assert "Number.isSafeInteger" in content

    def test_money_uses_the_shared_exact_formatter(self):
        """USDT figures go through the shared integer-exact
        WalletData formatter — the same one Home and the
        Tasks page use — never float maths."""
        content = _profile_js()
        assert "WalletData.formatUsdt" in content
        assert "parseFloat" not in content
        assert "toFixed" not in content

    def test_no_level_or_xp_surface(self):
        content = _profile_js()
        for banned in ("المستوى", "level"):
            assert banned not in content, \
                f"invented level/XP surface in profile: {banned}"

    def test_account_styles_keep_the_home_tokens(self):
        css = _css()
        money = _branch(css, ".profile-money {", "}")
        assert "direction: ltr" in money
        assert "unicode-bidi: isolate" in money
        stacked = _branch(css, ".profile-field + .profile-field {",
                          "}")
        assert "margin-top" in stacked
