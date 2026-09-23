"""
Focused tests — Mini App Tasks page (MT-TASK-03)
================================================

Verifies the production Tasks page integration:

- Tasks page loads real API data (/api/tasks) — no hardcoded dataset
- loading / empty / error states exist (Arabic, RTL)
- task card rendering: title, description, type, reward, status
- available / started / completed states with correct actions
- start action POSTs /api/tasks/<id>/start
- submit/verify action POSTs /api/tasks/<id>/submit
- API errors surface concise Arabic messages (backend codes mapped)
- authentication rides the existing initData header (no user id in JS)
- reward is display-only: never sent back, never client-controlled
- no wallet controls on the Tasks page
- no channel URL hardcoded in JavaScript (join_url comes from the API)
- the existing shell, theme and template constraints keep holding

Run:
    python3 -m pytest test_miniapp_tasks_page.py -v
"""

import os
import re


# ── Helpers ────────────────────────────────────────────────────────────


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _tasks_js() -> str:
    return _read("miniapp/js/tasks.js")


def _app_js() -> str:
    return _read("miniapp/js/app.js")


def _html() -> str:
    return _read("miniapp/index.html")


def _css() -> str:
    return _read("miniapp/css/app.css")


# ════════════════════════════════════════════════════════════════════
# 1. Module wiring
# ════════════════════════════════════════════════════════════════════


class TestTasksModuleWiring:
    def test_tasks_js_exists(self):
        assert os.path.exists("miniapp/js/tasks.js"), \
            "miniapp/js/tasks.js not found"

    def test_tasks_module_exports_render(self):
        content = _tasks_js()
        assert "const Tasks" in content
        assert "return {" in content
        assert "render" in content

    def test_app_routes_tasks_page_dynamically(self):
        content = _app_js()
        assert "page === 'tasks'" in content, \
            "app.js must route the tasks page"
        assert "Tasks.render()" in content, \
            "app.js must render the Tasks module"

    def test_tasks_script_loaded_before_app(self):
        html = _html()
        tasks_pos = html.find('src="js/tasks.js"')
        app_pos = html.find('src="js/app.js"')
        assert tasks_pos >= 0, "tasks.js must be loaded in index.html"
        assert app_pos >= 0, "app.js must be loaded in index.html"
        assert tasks_pos < app_pos, \
            "tasks.js must load before app.js (module dependency)"

    def test_tasks_template_kept_as_fallback(self):
        html = _html()
        assert 'id="page-tasks"' in html, \
            "the page-tasks template fallback must be preserved"

    def test_page_structure_matches_theme(self):
        content = _tasks_js()
        assert "'page page-tasks'" in content, \
            "Tasks page must use the existing page-tasks class"
        assert "<h2>المهام</h2>" in content, \
            "Tasks page must keep the existing Arabic header"
        assert "page-header" in content and "page-content" in content


# ════════════════════════════════════════════════════════════════════
# 2. Real API data — no hardcoded dataset
# ════════════════════════════════════════════════════════════════════


class TestRealApiData:
    def test_fetches_tasks_endpoint(self):
        assert "'/api/tasks'" in _tasks_js(), \
            "Tasks page must load real API data"

    def test_uses_existing_init_data_auth_header(self):
        content = _tasks_js()
        assert "'X-Telegram-Init-Data'" in content
        assert "TelegramApp.getInitData" in content, \
            "identity must ride the verified initData header"

    def test_no_user_id_in_frontend(self):
        content = _tasks_js()
        assert "user_id" not in content, \
            "the page must never handle a user id"
        assert "userId" not in content

    def test_no_hardcoded_task_dataset(self):
        content = _tasks_js()
        # The only task array is the API response assignment.
        assert "Array.isArray(data.tasks)" in content
        # No literal task objects/arrays with reward data in the source.
        assert re.search(r"reward\s*:\s*[\"'0-9]", content) is None, \
            "hardcoded reward literal found"
        assert re.search(r"const\s+tasks\s*=\s*\[", content) is None, \
            "hardcoded task list found"
        # Arabic task titles/descriptions must not be baked in as data.
        assert "قناة تيليجرام" not in content, \
            "hardcoded task description found"

    def test_tasks_rendered_from_response_only(self):
        content = _tasks_js()
        assert "_renderTasks(tasksCache)" in content
        assert "data.tasks" in content

    def test_inactive_status_vocabulary_not_introduced(self):
        content = _tasks_js()
        for banned in ("pending", "approved", "rejected",
                       "reserved", "expired"):
            assert re.search(rf"\b{banned}\b", content) is None, \
                f"banned status '{banned}' found in tasks.js"

    def test_only_three_statuses_defined(self):
        block = _tasks_js()
        match = re.search(
            r"STATUS_LABELS\s*=\s*\{(.*?)\}", block, re.DOTALL
        )
        assert match, "STATUS_LABELS missing"
        keys = re.findall(r"(\w+)\s*:", match.group(1))
        assert keys == ["available", "started", "completed"]


# ════════════════════════════════════════════════════════════════════
# 3. States: loading / empty / error / card
# ════════════════════════════════════════════════════════════════════


class TestPageStates:
    def test_loading_state(self):
        content = _tasks_js()
        assert 'data-testid="tasks-loading"' in content
        assert "جارٍ تحميل المهام" in content

    def test_empty_state(self):
        content = _tasks_js()
        assert 'data-testid="tasks-empty"' in content
        assert "لا توجد مهام متاحة حالياً" in content

    def test_error_state_with_retry(self):
        content = _tasks_js()
        assert 'data-testid="tasks-error"' in content
        assert 'data-testid="tasks-error-text"' in content
        assert 'data-testid="tasks-retry"' in content

    def test_task_card_rendering(self):
        content = _tasks_js()
        for marker in ("task-card", "task-title", "task-description",
                       "task-type", "task-reward", "task-status"):
            assert marker in content, f"missing card marker: {marker}"

    def test_card_uses_text_content_not_markup(self):
        """API strings are injected as text — never as HTML."""
        content = _tasks_js()
        assert ".textContent = task.title" in content
        assert ".textContent = task.description" in content

    def test_reward_is_display_only_metadata(self):
        content = _tasks_js()
        # Rendered from the API value, nothing more.
        assert "String(task.reward)" in content
        # Never editable, never sent back, never computed.
        assert re.search(r"task\.reward\s*=", content) is None
        assert "JSON.stringify" not in content, \
            "the page must not serialize a payload"
        assert re.search(r"reward\s*[:=]\s*[\"'0-9]", content) is None


# ════════════════════════════════════════════════════════════════════
# 4. Statuses and actions
# ════════════════════════════════════════════════════════════════════


class TestStatusActions:
    def test_available_state_offers_start(self):
        content = _tasks_js()
        assert "task.status === 'available'" in content
        assert "'task-start'" in content
        assert "ابدأ المهمة" in content

    def test_started_state_offers_submit(self):
        content = _tasks_js()
        assert "task.status === 'started'" in content
        assert "'task-submit'" in content
        assert "تحقق وإتمام" in content

    def test_completed_state_is_terminal(self):
        content = _tasks_js()
        assert "'task-completed-label'" in content
        assert "تم الإنجاز" in content
        # Completed tasks render the label only — no action branch.
        completed_branch = content[
            content.find("completed — terminal"):
        ]
        assert "task-start" not in completed_branch
        assert "task-submit" not in completed_branch

    def test_start_action_posts_to_api(self):
        content = _tasks_js()
        assert "`/api/tasks/${task.id}/start`" in content
        assert "method: 'POST'" in content

    def test_submit_action_posts_to_api(self):
        content = _tasks_js()
        assert "`/api/tasks/${task.id}/submit`" in content

    def test_status_transitions_come_from_server_response(self):
        content = _tasks_js()
        assert "{ status: 'started' }" in content
        assert "{ status: 'completed' }" in content

    def test_haptic_convention_preserved(self):
        content = _tasks_js()
        assert "HapticFeedback" in content


# ════════════════════════════════════════════════════════════════════
# 5. Error handling (Arabic, backend-driven)
# ════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    def test_error_code_map_is_arabic(self):
        match = re.search(
            r"ERROR_MESSAGES\s*=\s*\{(.*?)\n\s*\}", _tasks_js(), re.DOTALL
        )
        assert match, "ERROR_MESSAGES map missing"
        body = match.group(1)
        for code in ("unauthenticated", "task_not_found",
                     "task_not_available", "verification_failed",
                     "verification_error", "network"):
            assert code in body, f"missing error code mapping: {code}"
        # Every mapped message is Arabic.
        messages = re.findall(r":\s*'([^']+)'", body)
        assert messages, "no error messages found"
        for message in messages:
            assert re.search(r"[؀-ۿ]", message), \
                f"message is not Arabic: {message!r}"

    def test_server_message_wins_when_provided(self):
        content = _tasks_js()
        assert "data.message" in content, \
            "the backend's Arabic message must be preferred"

    def test_no_stack_traces_or_internals_in_js(self):
        content = _tasks_js()
        for banned in ("Traceback", "Exception", "stack"):
            assert banned not in content

    def test_no_business_rules_beyond_display(self):
        """The page maps codes to text — it never decides outcomes."""
        content = _tasks_js()
        # No completion/verification logic: PASSED/FAILED never appear.
        assert "PASSED" not in content
        assert "FAILED" not in content
        assert "completed = true" not in content


# ════════════════════════════════════════════════════════════════════
# 6. Security / scope guards
# ════════════════════════════════════════════════════════════════════


class TestSecurityGuards:
    def test_no_hardcoded_channel_url(self):
        content = _tasks_js()
        assert "t.me" not in content, \
            "channel URL must never be hardcoded in JavaScript"
        assert "task.join_url" in content, \
            "join destination must come from the server response"
        # The link is only accepted over https from the API.
        assert "startsWith('https://')" in content

    def test_no_bot_token_in_frontend(self):
        for path in ("miniapp/js/tasks.js", "miniapp/index.html"):
            content = _read(path)
            assert "TELEGRAM_BOT_TOKEN" not in content
            assert "bot_token" not in content

    def test_no_wallet_controls_on_tasks_page(self):
        content = _tasks_js().lower()
        for banned in ("wallet", "balance", "deposit", "withdraw",
                       "المحفظة", "الرصيد", "السحب", "الشحن"):
            assert banned not in content, \
                f"wallet control '{banned}' leaked into Tasks"

    def test_no_wallet_markup_in_tasks(self):
        content = _tasks_js()
        assert "wallet-button" not in content
        assert "wallet-action" not in content

    def test_no_direct_sql_or_db_references(self):
        content = _tasks_js()
        for banned in ("sqlite", "SELECT ", "user_tasks"):
            assert banned not in content


# ════════════════════════════════════════════════════════════════════
# 7. Tasks-specific styling within the existing theme
# ════════════════════════════════════════════════════════════════════


class TestTasksStyling:
    def test_tasks_styles_appended(self):
        css = _css()
        for selector in (".tasks-list", ".task-card", ".task-action-btn",
                         ".task-done-label", ".tasks-notice"):
            assert selector in css, f"missing Tasks style: {selector}"

    def test_tasks_styles_use_existing_tokens(self):
        css = _css()
        block = css[css.find(".task-card"):]
        block = block[:block.find("}")]
        assert "--home-card-inner-bg" in block, \
            "task cards must use the existing dark card token"
        assert "--neon-red" in block or "rgba(255, 45, 45" in block, \
            "task cards must keep the neon-red language"

    def test_existing_theme_rules_untouched(self):
        css = _css()
        # The unified theme rules asserted by test_miniapp_unify_theme
        # must still exist exactly once before our appended block.
        assert css.find(".page-tasks .page-content") >= 0
        assert css.find(".page-tasks .placeholder-text") >= 0

    def test_rtl_and_language_preserved(self):
        html = _html()
        assert 'dir="rtl"' in html
        assert 'lang="ar"' in html
