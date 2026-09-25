/**
 * Review Page (MT-TASK-18)
 * -----------------------
 * Reviewer-facing Mini App UI for the existing manual/social proof
 * task family.  It talks ONLY to the existing API — no new endpoints:
 *
 *   GET  /api/tasks                          catalog (auth via initData)
 *   GET  /api/tasks/<id>/claims              safe claim fields — answers
 *                                            ok ONLY for the server-side
 *                                            authorized reviewer (403 for
 *                                            anyone else)
 *   POST /api/tasks/<id>/claims/<sid>/decision   {decision: approve|reject}
 *
 * Boundaries:
 * - identity comes only from the verified Telegram initData header
 *   (TelegramApp.getInitData) — the page never supplies a user id or
 *   reviewer identity, and client-supplied identity is never trusted
 * - authority is decided by the server: reviewer controls render only
 *   for tasks whose claims endpoint answers ok; anyone the server
 *   does not identify as the reviewer gets the denied state with
 *   zero data
 * - only the safe claim fields (claim_id, task_id, submitted_at,
 *   proof_ref) are displayed — never worker identity, server-side
 *   task definitions, reviewer identity or financial data
 * - after every decision the page re-reads the server state; no
 *   decision outcome is kept as client authority and nothing is
 *   persisted in browser storage
 * - business rules stay in the backend; this file renders the
 *   server's Arabic messages (with safe fallbacks)
 */
const Review = (() => {
    const LIST_URL = '/api/tasks';
    const INIT_DATA_HEADER = 'X-Telegram-Init-Data';

    // Arabic fallbacks keyed by the backend's stable error codes.
    // The server-provided Arabic `message` always wins when present.
    const ERROR_MESSAGES = {
        unauthenticated: 'افتح التطبيق من تيليجرام أولاً',
        task_not_found: 'المهمة غير موجودة',
        task_inactive: 'المهمة غير متاحة حالياً',
        invalid_task_state: 'لا يمكن تنفيذ هذا الإجراء الآن',
        invalid_request: 'الطلب غير صالح',
        invalid_decision: 'القرار غير صالح',
        not_approver: 'ليست لديك صلاحية اتخاذ هذا القرار',
        claim_not_found: 'الطلب غير موجود',
        claim_already_approved: 'تمت الموافقة على هذا الطلب مسبقاً',
        claim_already_rejected: 'تم رفض هذا الطلب مسبقاً',
        server_error: 'حدث خطأ غير متوقع، حاول مرة أخرى',
        network: 'تعذر الاتصال بالخادم، حاول مرة أخرى'
    };

    let pageEl = null;
    let busy = false;

    /** Verified Telegram initData, using the existing auth infrastructure. */
    function _initData() {
        return (typeof TelegramApp !== 'undefined' && TelegramApp.getInitData)
            ? TelegramApp.getInitData()
            : '';
    }

    function _headers() {
        const headers = {};
        headers[INIT_DATA_HEADER] = _initData();
        return headers;
    }

    /** Existing project haptic helper pattern (same as navigation.js). */
    function _haptic() {
        if (window.Telegram?.WebApp?.HapticFeedback) {
            window.Telegram.WebApp.HapticFeedback.impactOccurred('light');
        }
    }

    function _node(testid) {
        return pageEl ? pageEl.querySelector(`[data-testid="${testid}"]`) : null;
    }

    function _show(node, visible) {
        if (node) {
            node.hidden = !visible;
        }
    }

    /** Show exactly one of the page states; hides all the others. */
    function _showState(name) {
        const states = ['loading', 'empty', 'denied', 'error', 'list'];
        for (const key of states) {
            _show(_node('review-' + key), key === name);
        }
    }

    function _messageFor(data) {
        if (data && typeof data.message === 'string' && data.message) {
            return data.message;
        }
        if (data && typeof data.error === 'string' && ERROR_MESSAGES[data.error]) {
            return ERROR_MESSAGES[data.error];
        }
        return ERROR_MESSAGES.server_error;
    }

    async function _parse(response) {
        try {
            return await response.json();
        } catch (error) {
            return null;
        }
    }

    function _showNotice(message, kind) {
        const notice = _node('review-notice');
        if (!notice) {
            return;
        }
        notice.textContent = message;
        notice.classList.remove('tasks-notice-error', 'tasks-notice-success');
        notice.classList.add(kind === 'success' ? 'tasks-notice-success' : 'tasks-notice-error');
        _show(notice, true);
    }

    function _hideNotice() {
        _show(_node('review-notice'), false);
    }

    function _setErrorText(message) {
        const text = _node('review-error-text');
        if (text) {
            text.textContent = message;
        }
    }

    function _setDecisionButtonsEnabled(enabled) {
        if (!pageEl) {
            return;
        }
        const buttons = pageEl.querySelectorAll('.review-action-btn');
        buttons.forEach((button) => {
            button.disabled = !enabled;
        });
    }

    /* ── Page shell ─────────────────────────────────────────────── */

    function render() {
        const page = document.createElement('div');
        page.className = 'page page-review';
        page.setAttribute('data-testid', 'review-page');

        page.innerHTML = `
            <div class="page-header">
                <h2>مراجعة الإثباتات</h2>
            </div>
            <div class="page-content">
                <div class="tasks-state tasks-loading" data-testid="review-loading">جارٍ تحميل الطلبات…</div>
                <div class="tasks-state tasks-empty" data-testid="review-empty" hidden>لا توجد طلبات بانتظار المراجعة</div>
                <div class="tasks-state tasks-denied" data-testid="review-denied" hidden>لا توجد لديك صلاحية مراجعة هذه المهام</div>
                <div class="tasks-state tasks-error" data-testid="review-error" hidden>
                    <span class="tasks-error-text" data-testid="review-error-text"></span>
                    <button type="button" class="tasks-retry-btn" data-testid="review-retry">إعادة المحاولة</button>
                </div>
                <div class="tasks-notice" data-testid="review-notice" hidden></div>
                <div class="review-list" data-testid="review-list" hidden></div>
            </div>
        `;

        const retry = page.querySelector('[data-testid="review-retry"]');
        if (retry) {
            retry.addEventListener('click', () => {
                _haptic();
                load();
            });
        }

        pageEl = page;
        load();
        return page;
    }

    /* ── Loading / states ───────────────────────────────────────── */

    async function load() {
        _hideNotice();
        _showState('loading');

        // 1) Catalog: which manual tasks exist at all (auth via initData).
        let data = null;
        let ok = false;
        try {
            const response = await fetch(LIST_URL, { headers: _headers() });
            data = await _parse(response);
            ok = response.ok && data && data.ok === true;
        } catch (error) {
            ok = false;
        }

        if (!ok) {
            _setErrorText(_messageFor(data));
            _showState('error');
            return;
        }

        const tasks = Array.isArray(data.tasks) ? data.tasks : [];
        const manualTasks = tasks.filter((task) => task && task.type === 'manual');

        // 2) Authority probe: the claims endpoint answers ok ONLY for
        //    the server-authorized reviewer of that task.  A denial
        //    (or a task without claims) yields no data and no
        //    reviewer controls — the server verdict is the gate.
        const sections = [];
        let probeFailed = false;
        for (const task of manualTasks) {
            let claimsData = null;
            let claimsOk = false;
            try {
                const response = await fetch(`/api/tasks/${task.id}/claims`, {
                    headers: _headers()
                });
                claimsData = await _parse(response);
                claimsOk = response.ok && claimsData && claimsData.ok === true;
            } catch (error) {
                probeFailed = true;
            }
            if (claimsOk && Array.isArray(claimsData.claims)) {
                sections.push({ task, claims: claimsData.claims });
            }
        }

        if (sections.length === 0) {
            if (probeFailed) {
                _setErrorText(ERROR_MESSAGES.network);
                _showState('error');
            } else {
                // Never identified as the reviewer by the server.
                _showState('denied');
            }
            return;
        }

        const pending = sections.filter((section) => section.claims.length > 0);
        if (pending.length === 0) {
            _showState('empty');
            return;
        }

        _renderSections(pending);
        _showState('list');
    }

    /* ── Claim list rendering ───────────────────────────────────── */

    function _renderSections(sections) {
        const list = _node('review-list');
        if (!list) {
            return;
        }
        list.innerHTML = '';
        for (const section of sections) {
            list.appendChild(_buildSection(section.task, section.claims));
        }
    }

    function _buildSection(task, claims) {
        const section = document.createElement('section');
        section.className = 'review-task';
        section.setAttribute('data-testid', 'review-task');
        section.setAttribute('data-task-id', String(task.id));

        const head = document.createElement('div');
        head.className = 'review-task-top';

        const title = document.createElement('span');
        title.className = 'review-task-title';
        title.setAttribute('data-testid', 'review-task-title');
        title.textContent = task.title || '';

        const idLabel = document.createElement('span');
        idLabel.className = 'review-task-id';
        idLabel.setAttribute('data-testid', 'review-task-id');
        idLabel.textContent = 'المهمة #' + String(task.id);

        head.appendChild(title);
        head.appendChild(idLabel);
        section.appendChild(head);

        for (const claim of claims) {
            if (!claim || typeof claim !== 'object') {
                continue;
            }
            section.appendChild(_buildClaim(task.id, claim));
        }
        return section;
    }

    function _buildClaim(taskId, claim) {
        const card = document.createElement('article');
        card.className = 'review-claim';
        card.setAttribute('data-testid', 'review-claim');

        // Safe fields only: claim_id, task_id, submitted_at, proof_ref.
        const meta = document.createElement('div');
        meta.className = 'review-claim-meta';

        const claimId = document.createElement('span');
        claimId.setAttribute('data-testid', 'review-claim-id');
        claimId.textContent = 'طلب #' + String(claim.claim_id);

        const taskIdLabel = document.createElement('span');
        taskIdLabel.setAttribute('data-testid', 'review-claim-task');
        taskIdLabel.textContent = 'مهمة #' + String(
            claim.task_id !== undefined && claim.task_id !== null
                ? claim.task_id : taskId
        );

        const when = document.createElement('span');
        when.setAttribute('data-testid', 'review-claim-date');
        when.textContent = String(
            claim.submitted_at !== undefined && claim.submitted_at !== null
                ? claim.submitted_at : ''
        );

        meta.appendChild(claimId);
        meta.appendChild(taskIdLabel);
        meta.appendChild(when);

        // The bounded proof, shown in full so the reviewer can inspect
        // it; a safe https reference is opened as a link, anything
        // else is plain selectable text (textContent — never markup).
        const proofIsLink = typeof claim.proof_ref === 'string'
            && claim.proof_ref.startsWith('https://');
        const proof = document.createElement(proofIsLink ? 'a' : 'div');
        proof.className = 'review-proof';
        proof.setAttribute('data-testid', 'review-proof-ref');
        proof.textContent = typeof claim.proof_ref === 'string'
            ? claim.proof_ref : '';
        if (proofIsLink) {
            proof.href = claim.proof_ref;
            proof.target = '_blank';
            proof.rel = 'noopener';
        }

        const actions = document.createElement('div');
        actions.className = 'review-actions';

        const approveBtn = document.createElement('button');
        approveBtn.type = 'button';
        approveBtn.className = 'review-action-btn review-approve-btn';
        approveBtn.setAttribute('data-testid', 'review-approve');
        approveBtn.textContent = 'قبول';
        approveBtn.addEventListener('click', () => decide(taskId, claim.claim_id, 'approve'));

        const rejectBtn = document.createElement('button');
        rejectBtn.type = 'button';
        rejectBtn.className = 'review-action-btn review-reject-btn';
        rejectBtn.setAttribute('data-testid', 'review-reject');
        rejectBtn.textContent = 'رفض';
        rejectBtn.addEventListener('click', () => decide(taskId, claim.claim_id, 'reject'));

        actions.appendChild(approveBtn);
        actions.appendChild(rejectBtn);

        card.appendChild(meta);
        card.appendChild(proof);
        card.appendChild(actions);
        return card;
    }

    /* ── Decision ───────────────────────────────────────────────── */

    /**
     * Send one decision through the existing decision endpoint.
     *
     * Only the backend-required `{decision}` body is sent — identity,
     * authority and completion stay server-side.  While a
     * request is in flight every decision control is disabled and the
     * busy flag blocks re-entry, so duplicate clicks cannot fire.
     * After ANY attempt the claims are re-read from the server: the
     * fresh server response, not client memory, is the authority.
     */
    async function decide(taskId, claimId, decision) {
        if (busy) {
            return;
        }
        busy = true;
        _haptic();
        _hideNotice();
        _setDecisionButtonsEnabled(false);

        let data = null;
        let ok = false;
        try {
            const headers = _headers();
            headers['Content-Type'] = 'application/json';
            const response = await fetch(
                `/api/tasks/${taskId}/claims/${claimId}/decision`,
                {
                    method: 'POST',
                    headers: headers,
                    body: JSON.stringify({ decision: decision })
                }
            );
            data = await _parse(response);
            ok = response.ok && data && data.ok === true;
        } catch (error) {
            ok = false;
        }

        // Server-driven refresh: re-read claims (and the access
        // verdict) before reporting the outcome.
        await load();

        busy = false;
        if (ok) {
            _showNotice(data.message || 'تم تنفيذ القرار', 'success');
        } else {
            _showNotice(_messageFor(data), 'error');
        }
    }

    return {
        render,
        load,
        decide
    };
})();
