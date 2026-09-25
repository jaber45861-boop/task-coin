"""
Focused tests — Mini App reviewer UI (MT-TASK-18)
==================================================

Reviewer-facing Mini App UI for the existing manual/social proof
family, against the unchanged reviewer API contract:

- entry + routing wired through the existing Navigation module
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
        """Exactly the three existing endpoints, nothing else."""
        content = _review_js()
        assert len(re.findall(r"fetch\(", content)) == 3, \
            "expected exactly: catalog, claims, decision"
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
        assert "--neon-green" in approve, \
            "approve must keep the neon-green language"
        reject = css[css.find(".review-reject-btn {"):]
        reject = reject[:reject.find("}")]
        assert "255, 45, 45" in reject, \
            "reject must keep the neon-red language"

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
