"""
Focused tests — Mini App manual proof UI (MT-TASK-17)
=====================================================

Worker-facing Mini App UI for the existing ``manual`` task family,
against the unchanged Manual Task API contract:

- Arabic type label for ``manual`` (no new task types added)
- available manual task shows the existing start action
- started manual task renders the bounded text proof input + submit
- the proof posts through the existing submit endpoint as proof_ref
  (exactly that one field, JSON, with the standard headers)
- missing/invalid proof surfaces the server's Arabic response
- awaiting_decision renders from the server response only
- completed stays terminal; a rejected attempt allows a retry
- telegram_channel / referral_task / wallet presentation unchanged
- the client never exposes approver, task_data, reviewer identity,
  user identity fields or the approval vocabulary

Run:
    python3 -m pytest test_miniapp_manual_proof_ui.py -v
"""

import os
import re


# ── Helpers ────────────────────────────────────────────────────────────


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _tasks_js() -> str:
    return _read("miniapp/js/tasks.js")


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


def _type_labels() -> dict:
    match = re.search(
        r"TYPE_LABELS\s*=\s*\{(.*?)\}", _tasks_js(), re.DOTALL
    )
    assert match, "TYPE_LABELS missing"
    return dict(re.findall(r"(\w+)\s*:\s*'([^']+)'", match.group(1)))


def _proof_submit_fn() -> str:
    return _branch(_tasks_js(), "async function submitProof", "return {")


def _proof_failure_tail() -> str:
    return _branch(_proof_submit_fn(), "// Failure (invalid proof")


# ════════════════════════════════════════════════════════════════════
# 1. Arabic label for the existing manual type
# ════════════════════════════════════════════════════════════════════


class TestManualTypeLabel:
    def test_manual_type_exists(self):
        assert "manual" in _type_labels(), \
            "manual must have a human-readable type label"

    def test_manual_label_is_arabic(self):
        label = _type_labels()["manual"]
        assert re.search(r"[؀-ۿ]", label), \
            f"manual label must be Arabic: {label!r}"

    def test_manual_label_distinct_from_other_families(self):
        labels = _type_labels()
        others = [v for k, v in labels.items() if k != "manual"]
        assert labels["manual"] not in others, \
            "manual needs its own distinct label"

    def test_no_new_task_types_added(self):
        """Only labels for server-known families — nothing invented."""
        assert set(_type_labels()) == {
            "channel_subscription",
            "deterministic",
            "manual",
            "referral_task",
            "telegram_channel",
        }


# ════════════════════════════════════════════════════════════════════
# 2. available state — existing start action for every family
# ════════════════════════════════════════════════════════════════════


class TestAvailableState:
    def test_available_offers_start_action(self):
        available = _branch(
            _tasks_js(),
            "if (task.status === 'available')",
            "if (task.status === 'started')",
        )
        assert "'task-start'" in available
        assert "ابدأ المهمة" in available

    def test_available_has_no_proof_ui(self):
        """Proof submission only appears once the server says started."""
        available = _branch(
            _tasks_js(),
            "if (task.status === 'available')",
            "if (task.status === 'started')",
        )
        assert "task-proof-input" not in available
        assert "submitProof" not in available

    def test_available_start_is_not_type_gated(self):
        """One start path for all families — manual included."""
        available = _branch(
            _tasks_js(),
            "if (task.status === 'available')",
            "if (task.status === 'started')",
        )
        assert "task.type" not in available


# ════════════════════════════════════════════════════════════════════
# 3. started manual task — proof submission UI
# ════════════════════════════════════════════════════════════════════


class TestStartedManualProofUi:
    @staticmethod
    def _started() -> str:
        return _branch(
            _tasks_js(),
            "if (task.status === 'started')",
            "// completed — terminal",
        )

    def test_started_manual_renders_proof_input(self):
        started = self._started()
        assert "task.type === 'manual'" in started
        assert "'task-proof-input'" in started

    def test_started_manual_renders_proof_submit_button(self):
        started = self._started()
        assert "'task-proof-submit'" in started
        assert "submitProof" in started

    def test_proof_controls_labelled_in_arabic(self):
        started = self._started()
        assert "إرسال الإثبات" in started, \
            "the proof submit button must be Arabic"
        assert "رابط الإثبات" in started, \
            "the proof input must carry an Arabic placeholder"

    def test_proof_input_is_bounded_single_line_text(self):
        """Bounded text/URL only — never a file/photo upload."""
        content = _tasks_js()
        assert "proofInput.type = 'text'" in content
        assert "proofInput.maxLength = 500" in content
        for banned in ("FormData", "FileReader", "multipart",
                       "type = 'file'"):
            assert banned not in content, \
                f"file upload mechanism found: {banned}"

    def test_non_manual_started_keeps_verify_action(self):
        started = self._started()
        assert "'task-submit'" in started
        assert "تحقق وإتمام" in started


# ════════════════════════════════════════════════════════════════════
# 4. Submission goes through the existing endpoint contract
# ════════════════════════════════════════════════════════════════════


class TestProofSubmissionContract:
    def test_posts_to_existing_submit_endpoint(self):
        fn = _proof_submit_fn()
        assert "`/api/tasks/${task.id}/submit`" in fn
        assert "method: 'POST'" in fn
        assert "'Idempotency-Key'" in fn
        assert "'Content-Type'" in fn
        assert "application/json" in fn

    def test_body_carries_exactly_the_proof_ref_field(self):
        content = _tasks_js()
        fields = re.findall(r"'\{\"(\w+)\"", content)
        assert fields == ["proof_ref"], \
            f"client must serialize only proof_ref, found: {fields}"

    def test_only_one_request_body_in_the_page(self):
        bodies = re.findall(r"body:\s*([^,\n]+)", _tasks_js())
        assert bodies, "expected the proof request body"
        assert all(b.strip() == "_proofBody(input.value)" for b in bodies), \
            f"unexpected request body: {bodies}"

    def test_identity_still_rides_init_data_header(self):
        fn = _proof_submit_fn()
        assert "_headers()" in fn
        content = _tasks_js()
        assert "'X-Telegram-Init-Data'" in content
        assert "TelegramApp.getInitData" in content
        assert "user_id" not in content
        assert "userId" not in content

    def test_task_identity_comes_from_server_response(self):
        """The path is built from the API-provided task id only."""
        fn = _proof_submit_fn()
        assert "`/api/tasks/${task.id}/submit`" in fn
        assert re.search(r"\bid\s*[:=]\s*\d", fn) is None, \
            "no client-hardcoded task id allowed"


# ════════════════════════════════════════════════════════════════════
# 5. Server response drives every state (incl. missing/invalid proof)
# ════════════════════════════════════════════════════════════════════


class TestServerDrivenStates:
    def test_success_reads_server_status_and_awaiting_flag(self):
        fn = _proof_submit_fn()
        assert "data.awaiting_decision === true" in fn
        assert "data.status" in fn

    def test_waiting_branch_keys_off_server_field_first(self):
        content = _tasks_js()
        awaiting_pos = content.find("task.awaiting_decision === true")
        available_pos = content.find("if (task.status === 'available')")
        started_pos = content.find("if (task.status === 'started')")
        assert awaiting_pos >= 0
        assert awaiting_pos < available_pos < started_pos, \
            "the server-provided waiting state must gate the actions"

    def test_awaiting_renders_arabic_under_review_state(self):
        content = _tasks_js()
        assert "'task-awaiting'" in content
        pattern = (
            r"task\.type === 'manual'\s*\?\s*'[^']*'"
            r"\s*:\s*'بانتظار موافقة العميل'"
        )
        assert re.search(pattern, content), \
            "manual awaiting state must render Arabic under-review text"
        manual_text = re.search(
            r"task\.type === 'manual'\s*\?\s*'([^']+)'", content
        )
        assert manual_text, "manual under-review wording missing"
        assert re.search(r"[؀-ۿ]", manual_text.group(1))

    def test_no_client_side_state_storage(self):
        """awaiting/status come from the server, never local caches."""
        content = _tasks_js()
        for banned in ("localStorage", "sessionStorage", "indexedDB"):
            assert banned not in content, \
                f"client-side state store found: {banned}"

    def test_invalid_proof_maps_to_arabic_fallback(self):
        match = re.search(
            r"ERROR_MESSAGES\s*=\s*\{(.*?)\n\s*\}", _tasks_js(), re.DOTALL
        )
        assert match, "ERROR_MESSAGES map missing"
        entry = re.search(
            r"invalid_proof:\s*'([^']+)'", match.group(1)
        )
        assert entry, "invalid_proof must map to an Arabic message"
        assert re.search(r"[؀-ۿ]", entry.group(1))

    def test_failure_surfaces_server_response(self):
        """Server message first; mapped code only as a fallback."""
        fn = _proof_submit_fn()
        assert "_messageFor(data)" in fn
        content = _tasks_js()
        assert "data.message" in content, \
            "the backend's Arabic message must be preferred"


# ════════════════════════════════════════════════════════════════════
# 6. Rejected attempt allows a retry; completed stays terminal
# ════════════════════════════════════════════════════════════════════


class TestRetryAndTerminalStates:
    def test_failure_does_not_change_task_state(self):
        """No local rewrite on error — the input stays for a retry."""
        tail = _proof_failure_tail()
        assert "_replaceTask" not in tail, \
            "a failed attempt must not mutate the rendered task"
        assert "_messageFor(data)" in tail

    def test_failure_reenables_the_submit_button(self):
        tail = _proof_failure_tail()
        assert "button.disabled = false" in tail, \
            "the proof input must be submittable again after an error"

    def test_completed_branch_is_terminal(self):
        completed = _branch(_tasks_js(), "// completed — terminal")
        assert "'task-completed-label'" in completed
        assert "تم الإنجاز" in completed
        for banned in ("task-start", "task-submit",
                       "task-proof-input", "task-proof-submit",
                       "task-join"):
            assert banned not in completed, \
                f"completed branch must stay terminal: {banned}"

    def test_completed_state_comes_from_server(self):
        content = _tasks_js()
        assert "{ status: 'completed' }" in content
        assert "{ status: 'started' }" in content


# ════════════════════════════════════════════════════════════════════
# 7. telegram_channel / referral presentation unchanged
# ════════════════════════════════════════════════════════════════════


class TestExistingFamiliesUnchanged:
    def test_existing_type_labels_untouched(self):
        labels = _type_labels()
        assert labels["channel_subscription"] == "اشتراك في قناة"
        assert labels["deterministic"] == "مهمة تحقق"
        assert labels["referral_task"] == "مهمة إحالة"
        assert labels["telegram_channel"] == "انضمام عبر تيليجرام"

    def test_join_link_rendering_untouched(self):
        content = _tasks_js()
        guard = (
            "typeof task.join_url === 'string'"
            " && task.join_url.startsWith('https://')"
        )
        assert guard in content
        assert "joinLink.href = task.join_url;" in content
        assert "انضم للقناة" in content
        assert content.count("'task-join'") == 1
        assert "t.me" not in content

    def test_referral_waiting_wording_untouched(self):
        content = _tasks_js()
        assert "'بانتظار موافقة العميل'" in content, \
            "referral awaiting wording must not change"
        pattern = (
            r"task\.type === 'manual'\s*\?\s*'[^']*'"
            r"\s*:\s*'بانتظار موافقة العميل'"
        )
        assert re.search(pattern, content), \
            "the non-manual path must keep the original wording"

    def test_no_wallet_changes(self):
        content = _tasks_js().lower()
        for banned in ("wallet", "balance", "deposit", "withdraw"):
            assert banned not in content


# ════════════════════════════════════════════════════════════════════
# 8. No reviewer / approver / identity exposure, no reviewer UI
# ════════════════════════════════════════════════════════════════════


class TestNoReviewerExposure:
    def test_no_decision_surface(self):
        """The Tasks page holds no approval capability of any kind."""
        content = _tasks_js()
        assert "/decision" not in content
        assert "/claims" not in content
        for quoted in ("'approve'", "'reject'", '"approve"',
                       '"reject"', "'approved'", "'rejected'"):
            assert quoted not in content, quoted

    def test_no_approver_or_task_data(self):
        content = _tasks_js()
        for forbidden in ("task_data", "approver", "reviewer",
                          "channel_slug", "channel_id",
                          "username", "chat_id"):
            assert forbidden not in content, forbidden

    def test_approval_vocabulary_stays_server_side(self):
        content = _tasks_js()
        for banned in ("pending", "approved", "rejected",
                       "reserved", "expired"):
            assert re.search(rf"\b{banned}\b", content) is None, \
                f"banned status '{banned}' leaked into the client"

    def test_no_identity_or_serialized_task_data(self):
        content = _tasks_js()
        assert "user_id" not in content
        assert "userId" not in content
        assert "JSON.stringify" not in content
        assert "sqlite" not in content
        assert "user_tasks" not in content


# ════════════════════════════════════════════════════════════════════
# 9. Styling within the existing theme
# ════════════════════════════════════════════════════════════════════


class TestProofStyling:
    def test_proof_input_styled_with_theme_tokens(self):
        css = _css()
        assert ".task-proof-input" in css
        idx = css.find(".task-proof-input {")
        assert idx >= 0, "proof input rule missing"
        block = css[idx:css.find("}", idx)]
        assert "--home-text" in block, \
            "proof input must use the existing text token"
        assert "255, 45, 45" in block, \
            "proof input must keep the neon-red language"

    def test_existing_task_selectors_kept(self):
        css = _css()
        for selector in (".tasks-list", ".task-card", ".task-action-btn",
                         ".task-done-label", ".tasks-notice"):
            assert selector in css, f"missing Tasks style: {selector}"

    def test_tasks_js_file_exists(self):
        assert os.path.exists("miniapp/js/tasks.js")
