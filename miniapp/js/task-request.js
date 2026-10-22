/**
 * Task Request Form (Mini App «إضافة مهمة ➕»)
 * ============================================
 * Overlay dialog opened from the Home «إضافة مهمة» CTA: the user
 * proposes a task, it is sent to admin review, and the dialog shows
 * the request's live status (قيد المراجعة / مرفوضة / تحتاج إلى تعديل /
 * منشورة) plus the user's own request list.
 *
 * Boundaries (mirrors tasks.js / withdrawal.js):
 * - identity comes only from the verified Telegram initData header
 *   (TelegramApp.getInitData); this file never supplies a user id
 * - POST   /api/task-requests            → create a pending request
 * - GET    /api/task-requests            → the caller's own requests
 * - PATCH  /api/task-requests/<id>       → edit + resubmit a request
 *   the admin returned for changes
 * - POST   /api/owner/tasks/<id>/edit    → owner edit of a published
 *   task (title/description ONLY — the server rejects financial and
 *   execution-semantics fields outright)
 * - POST   /api/owner/tasks/<id>/pause   → owner pause (idempotent)
 * - POST   /api/owner/tasks/<id>/resume  → owner resume (idempotent)
 * - POST   /api/owner/tasks/<id>/archive → owner logical hide (never
 *   deletes anything; the server drops it from this list fetch)
 * - owner controls render only for the caller's OWN approved requests
 *   (the list itself is ownership-filtered server-side), and the
 *   server re-verifies ownership on every single call
 * - there is NO approve/reject control here: those exist only on the
 *   Telegram admin side, and the server rejects any such attempt
 * - every field is validated server-side; the small client checks
 *   below are only for quick feedback, never for security
 * - server Arabic messages (with stable-code fallbacks) decide every
 *   state; API strings are injected as text — never as markup
 */
const TaskRequestUI = (() => {
    const LIST_URL = '/api/task-requests';
    const INIT_DATA_HEADER = 'X-Telegram-Init-Data';

    // Arabic fallbacks keyed by the backend's stable error codes.
    const ERROR_MESSAGES = {
        unauthenticated: 'افتح التطبيق من تيليجرام أولاً',
        invalid_request: 'الطلب غير صالح',
        invalid_payload: 'بيانات المهمة غير صالحة',
        request_not_found: 'الطلب غير موجود',
        invalid_status: 'لا يمكن تعديل هذا الطلب في وضعه الحالي',
        server_error: 'حدث خطأ غير متوقع، حاول مرة أخرى',
        network: 'تعذر الاتصال بالخادم، حاول مرة أخرى'
    };

    const STATUS_LABELS = {
        pending: '⏳ قيد المراجعة',
        approved: '✅ منشورة',
        rejected: '❌ مرفوضة',
        changes_requested: '✏️ تحتاج إلى تعديل'
    };

    // Labels/whitelists mirror task_taxonomy (server stays authoritative).
    const PROVIDER_LABELS = {
        telegram: 'Telegram',
        instagram: 'Instagram',
        tiktok: 'TikTok',
        vk: 'VK',
        linkedin: 'LinkedIn',
        reddit: 'Reddit',
        likee: 'Likee',
        youtube: 'YouTube',
        website: '🌐 موقع ويب',
        google_play: 'Google Play',
        app_store: 'App Store',
        crypto: '🪙 منصة كريبتو',
        other: 'أخرى'
    };

    const ACTION_LABELS = {
        join_channel: 'انضمام لقناة',
        follow: 'متابعة',
        like: 'إعجاب',
        comment: 'تعليق',
        visit: 'زيارة',
        open: 'فتح',
        download: 'تحميل',
        watch: 'مشاهدة',
        start: 'بدء',
        submit_proof: 'إرسال إثبات'
    };

    const ACTIONS_BY_PROVIDER = {
        telegram: ['join_channel', 'start', 'visit', 'submit_proof'],
        instagram: ['follow', 'like', 'comment', 'visit', 'submit_proof'],
        tiktok: ['follow', 'like', 'comment', 'visit', 'submit_proof'],
        vk: ['follow', 'like', 'comment', 'visit', 'submit_proof'],
        linkedin: ['follow', 'like', 'comment', 'visit', 'submit_proof'],
        reddit: ['follow', 'like', 'comment', 'visit', 'submit_proof'],
        likee: ['follow', 'like', 'comment', 'visit', 'submit_proof'],
        youtube: ['watch', 'like', 'comment', 'visit', 'submit_proof'],
        website: ['visit', 'open', 'submit_proof'],
        google_play: ['download', 'open', 'submit_proof'],
        app_store: ['download', 'open', 'submit_proof'],
        crypto: ['visit', 'open', 'submit_proof'],
        other: ['visit', 'open', 'download', 'watch', 'start',
                'follow', 'like', 'comment', 'submit_proof']
    };

    let overlay = null;
    let busy = false;
    let requestsCache = [];
    // When the admin returned a request for changes, the form edits
    // that request (PATCH); null → a fresh proposal (POST).
    let editRequestId = null;
    let justSubmitted = false;
    // Success-banner text: the SERVER's own message for the last
    // completed action (create / owner edit / pause / resume /
    // archive).  Never set from a failed response.
    let statusMessage = '';
    // Owner-edit mode: the published task id being edited through the
    // form (string) or null.  While set, the form's primary button
    // saves title/description to /api/owner/tasks/<id>/edit instead
    // of creating a new proposal.
    let ownerEditTaskId = null;

    // Owner management (Phase 17). The request list only ever holds
    // the caller's OWN rows (ownership-filtered server-side from the
    // verified initData), so these buttons are visible to the owner
    // alone — and the server re-verifies ownership on every call.
    function _ownerActionsHtml(request) {
        if (request.status !== 'approved' || !request.task_id) {
            return '';
        }
        // A logically archived task is hidden from the owner view.
        if (request.archived) {
            return '';
        }
        // Lifecycle state comes from the server's read-only flags
        // (history-derived), never from a guessed status string.
        const paused = request.paused === true;

        const parts = [];
        parts.push('<div class="taskreq-owner-actions">');

        if (paused) {
            parts.push(
                `<button type="button" class="taskreq-owner-btn taskreq-resume"
                     data-testid="taskreq-owner-resume"
                     data-published-task-id="${_esc(String(request.task_id))}"
                     aria-label="استئناف المهمة">▶️ استئناف</button>`
            );
        } else {
            parts.push(
                `<button type="button" class="taskreq-owner-btn taskreq-edit"
                     data-testid="taskreq-owner-edit"
                     data-published-task-id="${_esc(String(request.task_id))}"
                     aria-label="تعديل المهمة">✏️ تعديل</button>`
            );
            parts.push(
                `<button type="button" class="taskreq-owner-btn taskreq-pause"
                     data-testid="taskreq-owner-pause"
                     data-published-task-id="${_esc(String(request.task_id))}"
                     aria-label="إيقاف المهمة">⏸️ إيقاف</button>`
            );
        }
        parts.push(
            `<button type="button" class="taskreq-owner-btn taskreq-archive"
                 data-testid="taskreq-owner-archive"
                 data-published-task-id="${_esc(String(request.task_id))}"
                 aria-label="إخفاء المهمة من عرضك">📁 إخفاء</button>`
        );

        parts.push('</div>');
        return parts.join('');
    }

    /** The initData gate every owner action must pass (same
     * convention as the rest of the file: identity never comes from
     * the client — the header is the only credential). */
    function _requireInitData() {
        if (typeof TelegramApp !== 'undefined' && TelegramApp.getInitData) {
            return true;
        }
        _formError('افتح التطبيق من تيليجرام أولاً');
        return false;
    }

    /**
     * Shared POST for the idempotent owner lifecycle actions
     * (pause / resume / archive).  Returns the parsed body on
     * success, or null with the error already surfaced — a failed
     * response never flips any UI state and never triggers a
     * refresh, so the list only changes after server confirmation.
     */
    async function _ownerPost(btn, url, busyText, idleText) {
        _haptic();
        busy = true;
        _formError('');
        if (btn) {
            btn.disabled = true;
            btn.textContent = busyText;
        }
        try {
            const response = await fetch(url, {
                method: 'POST',
                headers: _headers(),
            });
            const data = await _parse(response);
            if (!response.ok || !data || data.ok !== true) {
                _formError(_messageFor(data));
                return null;
            }
            return data;
        } catch (error) {
            _formError(_messageFor(null));
            return null;
        } finally {
            busy = false;
            if (btn) {
                btn.disabled = false;
                btn.textContent = idleText;
            }
        }
    }

    function _setSuccess(message) {
        statusMessage = message || '';
        justSubmitted = false;
    }

    function _ownerEditUrl(publishedTaskId) {
        return `/api/owner/tasks/${publishedTaskId}/edit`;
    }

    function _ownerPauseUrl(publishedTaskId) {
        return `/api/owner/tasks/${publishedTaskId}/pause`;
    }

    function _ownerResumeUrl(publishedTaskId) {
        return `/api/owner/tasks/${publishedTaskId}/resume`;
    }

    function _ownerArchiveUrl(publishedTaskId) {
        return `/api/owner/tasks/${publishedTaskId}/archive`;
    }

    /** Enter owner-edit mode for one published task.
     *  Prefills title/description and locks every field the owner
     *  may NOT edit — the form's submit button then saves the edit
     *  instead of creating a proposal. */
    function _startOwnerEdit(taskId) {
        if (busy || !taskId) {
            return;
        }
        const request = requestsCache.find(
            (r) => r.status === 'approved' &&
                String(r.task_id) === String(taskId)
        );
        if (!request) {
            _formError('المهمة غير موجودة');
            return;
        }
        // The owner edit owns the form: drop any changes-requested
        // resubmit context while the edit is open.
        editRequestId = null;
        ownerEditTaskId = String(taskId);
        _applyEditMode();
        _applyOwnerEditMode();
        _formError('');
    }

    /** Leave owner-edit mode and restore the normal create form. */
    function _exitOwnerEdit() {
        ownerEditTaskId = null;
        _clearForm();
        _applyEditMode();
        _applyOwnerEditMode();
        _formError('');
    }

    /**
     * Save an owner edit.  The payload carries ONLY title and
     * description — the server rejects financial and
     * execution-semantics keys outright, and nothing else leaves
     * the client.  Refresh happens only after the server confirms.
     */
    async function _ownerEdit(taskId, btn) {
        if (busy || !taskId) {
            return;
        }
        const payload = {
            title: _field('taskreq-title').trim(),
            description: _field('taskreq-description').trim(),
        };
        if (!payload.title || !payload.description) {
            _formError('أدخل عنوان المهمة ووصفها');
            return;
        }

        _haptic();
        busy = true;
        _formError('');
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'جارٍ الحفظ…';
        }
        try {
            const response = await fetch(_ownerEditUrl(taskId), {
                method: 'POST',
                headers: _headers(),
                body: JSON.stringify(payload),
            });
            const data = await _parse(response);
            if (!response.ok || !data || data.ok !== true) {
                _formError(_messageFor(data));
                return;
            }
            // Server confirmed: leave edit mode, show its message,
            // then refresh the list from the server.
            ownerEditTaskId = null;
            _setSuccess(data.message || 'تم تعديل المهمة');
            await _reload();
        } catch (error) {
            _formError(_messageFor(null));
        } finally {
            busy = false;
            if (btn) {
                btn.disabled = false;
                // On success the render already restored the create-mode
                // label (owner-edit mode is over); only a FAILED save
                // goes back to the edit-mode label.
                btn.textContent = ownerEditTaskId
                    ? '💾 حفظ التعديلات'
                    : 'إرسال للمراجعة';
            }
        }
    }

    async function _ownerPause(taskId, btn) {
        if (busy || !taskId) {
            return;
        }
        const data = await _ownerPost(
            btn, _ownerPauseUrl(taskId), 'جارٍ الإيقاف…', '⏸️ إيقاف'
        );
        if (!data) {
            return; // error surfaced; state unchanged
        }
        _setSuccess(data.message || 'تم إيقاف المهمة');
        await _reload();
    }

    async function _ownerResume(taskId, btn) {
        if (busy || !taskId) {
            return;
        }
        const data = await _ownerPost(
            btn, _ownerResumeUrl(taskId), 'جارٍ الاستئناف…', '▶️ استئناف'
        );
        if (!data) {
            return; // error surfaced; state unchanged
        }
        _setSuccess(data.message || 'تم استئناف المهمة');
        await _reload();
    }

    async function _ownerArchive(taskId, btn) {
        if (busy || !taskId) {
            return;
        }
        // Archive hides the task from THIS user's view only — nothing
        // is ever deleted — so it gets a confirm step before the POST.
        if (typeof confirm === 'function' &&
            !confirm('هل تريد إخفاء هذه المهمة من عرضك؟ لن تُحذف المهمة أو مواردها.')) {
            return;
        }
        const data = await _ownerPost(
            btn, _ownerArchiveUrl(taskId), 'جارٍ الإخفاء…', '📁 إخفاء'
        );
        if (!data) {
            return; // error surfaced; state unchanged
        }
        _setSuccess(data.message || 'تم إخفاء المهمة من عرضك');
        await _reload();
    }

    
    /** Verified Telegram initData, using the existing auth infrastructure. */
    function _initData() {
        return (typeof TelegramApp !== 'undefined' && TelegramApp.getInitData)
            ? TelegramApp.getInitData()
            : '';
    }

    function _headers() {
        const headers = {};
        headers[INIT_DATA_HEADER] = _initData();
        headers['Content-Type'] = 'application/json';
        return headers;
    }

    /** Escape untrusted text before it enters innerHTML. */
    function _esc(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /** Existing project haptic helper pattern (same as navigation.js). */
    function _haptic() {
        if (window.Telegram?.WebApp?.HapticFeedback) {
            window.Telegram.WebApp.HapticFeedback.impactOccurred('light');
        }
    }

    function _messageFor(data) {
        if (data && typeof data.message === 'string' && data.message) {
            return data.message;
        }
        if (data && typeof data.error === 'string' && ERROR_MESSAGES[data.error]) {
            return ERROR_MESSAGES[data.error];
        }
        return ERROR_MESSAGES.network;
    }

    async function _parse(response) {
        try {
            return await response.json();
        } catch (error) {
            return null;
        }
    }

    function _statusLabel(status) {
        return STATUS_LABELS[status] || status;
    }

    /* ── Overlay shell ──────────────────────────────────────────── */

    function _buildShell() {
        const el = document.createElement('div');
        el.className = 'taskreq-overlay';
        el.setAttribute('data-testid', 'taskreq-overlay');
        return el;
    }

    /**
     * Open the «إضافة مهمة» dialog. The user's own requests load
     * fresh on every open, so a fresh admin decision is never stale.
     */
    async function open() {
        if (overlay) {
            return;
        }
        _haptic();
        overlay = _buildShell();
        document.body.appendChild(overlay);
        justSubmitted = false;
        editRequestId = null;
        ownerEditTaskId = null;
        statusMessage = '';
        _renderLoading();

        try {
            const response = await fetch(LIST_URL, { headers: _headers() });
            const data = await _parse(response);
            if (!response.ok || !data || data.ok !== true) {
                _renderMessage(_messageFor(data));
                return;
            }
            requestsCache = Array.isArray(data.requests) ? data.requests : [];
            const editable = requestsCache.find(
                (r) => r.status === 'changes_requested'
            );
            editRequestId = editable ? editable.request_id : null;
            _renderMain();
        } catch (error) {
            _renderMessage(_messageFor(null));
        }
    }

    function close() {
        if (overlay) {
            overlay.remove();
            overlay = null;
            busy = false;
            editRequestId = null;
            justSubmitted = false;
            ownerEditTaskId = null;
            statusMessage = '';
        }
    }

    /* ── Loading / plain message states ────────────────────────── */

    function _cardStart(title) {
        return `
            <div class="taskreq-card" role="dialog" aria-modal="true"
                 aria-label="إضافة مهمة">
                <div class="taskreq-head">
                    <h3 class="taskreq-title">${_esc(title)}</h3>
                    <button type="button" class="taskreq-close"
                            data-testid="taskreq-close"
                            aria-label="إغلاق">✕</button>
                </div>`;
    }

    function _renderLoading() {
        overlay.innerHTML = `${_cardStart('إضافة مهمة')}
            <div class="taskreq-status" data-testid="taskreq-loading">
                جارٍ التحميل…
            </div>
        </div>`;
        _wireClose();
    }

    function _renderMessage(message) {
        overlay.innerHTML = `${_cardStart('إضافة مهمة')}
            <div class="taskreq-error" data-testid="taskreq-error">
                ${_esc(message)}
            </div>
            <button type="button" class="taskreq-submit"
                    data-testid="taskreq-retry">إعادة المحاولة</button>
        </div>`;
        _wireClose();
        const retry = overlay.querySelector('[data-testid="taskreq-retry"]');
        if (retry) {
            retry.addEventListener('click', () => {
                _haptic();
                // Drop the failed shell first — open() is a no-op
                // while an overlay element already exists.
                if (overlay) {
                    overlay.remove();
                    overlay = null;
                }
                open();
            });
        }
    }

    function _wireClose() {
        const closeBtn = overlay.querySelector(
            '[data-testid="taskreq-close"]'
        );
        if (closeBtn) {
            closeBtn.addEventListener('click', close);
        }
    }

    /* ── Main view: status banner + own list + form ────────────── */

    function _renderMain() {
        const pendingChip = justSubmitted ? `
            <div class="taskreq-chip taskreq-chip-pending"
                 data-testid="taskreq-last-status">⏳ قيد المراجعة</div>
        ` : '';
        // statusMessage is always the SERVER's own message for the
        // last confirmed action — escaped at the injection point.
        const successBlock = statusMessage ? `
            <div class="taskreq-success" data-testid="taskreq-success">
                ${_esc(statusMessage)}
            </div>
            ${pendingChip}
        ` : '';

        overlay.innerHTML = `${_cardStart('إضافة مهمة')}
            ${successBlock}
            <div class="taskreq-list-wrap" data-testid="taskreq-list-wrap">
                <span class="taskreq-section-label">طلباتي</span>
                <div class="taskreq-list" data-testid="taskreq-list"></div>
            </div>
            <div class="taskreq-editnote" data-testid="taskreq-editnote"
                 hidden></div>
            <div class="taskreq-field">
                <label class="taskreq-label" for="taskreq-title">عنوان المهمة</label>
                <input class="taskreq-input" id="taskreq-title" type="text"
                       maxlength="200" autocomplete="off"
                       data-testid="taskreq-title" placeholder="مثال: متابعة حسابي على Instagram">
            </div>
            <div class="taskreq-field">
                <label class="taskreq-label" for="taskreq-description">وصف المهمة</label>
                <textarea class="taskreq-input taskreq-textarea" rows="3"
                          maxlength="1000" id="taskreq-description"
                          data-testid="taskreq-description"
                          placeholder="تعليمات التنفيذ للمستخدم"></textarea>
            </div>
            <div class="taskreq-field">
                <label class="taskreq-label" for="taskreq-provider">نوع المهمة (المنصة)</label>
                <select class="taskreq-select" id="taskreq-provider"
                        data-testid="taskreq-provider"></select>
            </div>
            <div class="taskreq-field">
                <label class="taskreq-label" for="taskreq-action">الإجراء</label>
                <select class="taskreq-select" id="taskreq-action"
                        data-testid="taskreq-action"></select>
            </div>
            <div class="taskreq-field">
                <label class="taskreq-label" for="taskreq-target">رابط/هدف المهمة (اختياري)</label>
                <input class="taskreq-input" id="taskreq-target" type="text"
                       maxlength="500" autocomplete="off"
                       data-testid="taskreq-target" placeholder="https://…">
            </div>
            <div class="taskreq-field">
                <label class="taskreq-label" for="taskreq-reward">المكافأة (USDT)</label>
                <input class="taskreq-input" id="taskreq-reward" type="text"
                       inputmode="decimal" autocomplete="off"
                       data-testid="taskreq-reward" placeholder="0.5">
            </div>
            <div class="taskreq-error" data-testid="taskreq-form-error"
                 hidden></div>
            <button type="button" class="taskreq-submit"
                    data-testid="taskreq-submit">إرسال للمراجعة</button>
            <button type="button" class="taskreq-secondary"
                    data-testid="taskreq-new" hidden>إرسال طلب جديد</button>
            <button type="button" class="taskreq-secondary"
                    data-testid="taskreq-owner-cancel" hidden>إلغاء التعديل</button>
        </div>`;
        _wireClose();
        _renderList();
        _fillProviderOptions();
        _wireProviderFilter();
        _applyEditMode();
        _applyOwnerEditMode();
        overlay.querySelector('[data-testid="taskreq-submit"]')
            .addEventListener('click', _submit);
        const newBtn = overlay.querySelector('[data-testid="taskreq-new"]');
        if (newBtn) {
            newBtn.addEventListener('click', () => {
                _haptic();
                editRequestId = null;
                ownerEditTaskId = null;
                _clearForm();
                _applyEditMode();
                _applyOwnerEditMode();
                _formError('');
            });
        }
        const cancelBtn = overlay.querySelector(
            '[data-testid="taskreq-owner-cancel"]'
        );
        if (cancelBtn) {
            cancelBtn.addEventListener('click', () => {
                _haptic();
                _exitOwnerEdit();
            });
        }
        _wireOwnerActions();
    }

    function _renderList() {
        const list = overlay.querySelector('[data-testid="taskreq-list"]');
        if (!list) {
            return;
        }
        if (!requestsCache.length) {
            list.innerHTML = `<span class="taskreq-empty"
                data-testid="taskreq-empty">لا توجد لديك طلبات مهام بعد.</span>`;
            return;
        }
        list.innerHTML = requestsCache.map((r) => {
            // Defense-in-depth: an unknown status is server data
            // just like title/reason, so it is escaped at the
            // injection point too. Known labels pass through _esc
            // unchanged — the rendered text never differs.
            const label = STATUS_LABELS[r.status] || r.status;
            const chipClass = 'taskreq-chip taskreq-chip-' + _esc(r.status);
            const reason = r.reason
                ? `<span class="taskreq-reason"
                         data-testid="taskreq-reason">${_esc(r.reason)}</span>`
                : '';
            const ownerActions =
                r.status === 'approved' &&
                r.task_id !== null && r.task_id !== undefined
                    ? _ownerActionsHtml(r) : '';
            return `<div class="taskreq-item"
                     data-testid="taskreq-item" data-status="${_esc(r.status)}">
                <span class="taskreq-item-title">${_esc(r.title)}</span>
                <span class="${chipClass}">${_esc(label)}</span>
                ${reason}
                ${ownerActions}
            </div>`;
        }).join('');
    }

    function _editableRequest() {
        return requestsCache.find(
            (r) => r.request_id === editRequestId
        ) || null;
    }

    function _fillProviderOptions() {
        const select = overlay.querySelector(
            '[data-testid="taskreq-provider"]'
        );
        if (!select) {
            return;
        }
        select.innerHTML = Object.keys(PROVIDER_LABELS).map((key) =>
            `<option value="${_esc(key)}">${_esc(PROVIDER_LABELS[key])}</option>`
        ).join('');
        _fillActionOptions();
    }

    function _fillActionOptions() {
        const provider = overlay.querySelector(
            '[data-testid="taskreq-provider"]'
        ).value;
        const actions = ACTIONS_BY_PROVIDER[provider] || [];
        const select = overlay.querySelector(
            '[data-testid="taskreq-action"]'
        );
        select.innerHTML = actions.map((key) =>
            `<option value="${_esc(key)}">${_esc(ACTION_LABELS[key] || key)}</option>`
        ).join('');
    }

    function _wireProviderFilter() {
        overlay.querySelector('[data-testid="taskreq-provider"]')
            .addEventListener('change', () => {
                _fillActionOptions();
            });
    }

    function _setField(testid, value) {
        const node = overlay.querySelector(`[data-testid="${testid}"]`);
        if (node) {
            node.value = value === null || value === undefined ? '' : value;
        }
    }

    function _clearForm() {
        _setField('taskreq-title', '');
        _setField('taskreq-description', '');
        _setField('taskreq-target', '');
        _setField('taskreq-reward', '');
        const provider = overlay.querySelector(
            '[data-testid="taskreq-provider"]'
        );
        if (provider && provider.options.length) {
            provider.selectedIndex = 0;
        }
        _fillActionOptions();
    }

    /** Prefill the form when the admin returned a request for changes. */
    function _applyEditMode() {
        const note = overlay.querySelector(
            '[data-testid="taskreq-editnote"]'
        );
        const newBtn = overlay.querySelector('[data-testid="taskreq-new"]');
        const request = _editableRequest();

        // If this is a pending request that the admin returned for changes,
        // the form edits that request (PATCH); otherwise we are creating a
        // fresh proposal (POST).
        if (!request || request.status !== 'changes_requested') {
            if (note) {
                note.hidden = true;
            }
            if (newBtn) {
                newBtn.hidden = true;
            }
            return;
        }

        _setField('taskreq-title', request.title);
        _setField('taskreq-description', request.description);
        _setField('taskreq-target', request.target_ref);
        _setField('taskreq-reward', request.reward_units !== null &&
            request.reward_units !== undefined && typeof WalletData !== 'undefined'
            ? WalletData.formatUsdt(request.reward_units)
            : String(request.reward));
        const provider = overlay.querySelector(
            '[data-testid="taskreq-provider"]'
        );
        if (provider) {
            provider.value = request.provider;
        }
        _fillActionOptions();
        const action = overlay.querySelector(
            '[data-testid="taskreq-action"]'
        );
        if (action) {
            action.value = request.action;
        }
        if (note) {
            const reason = request.reason
                ? ` ملاحظة الإدارة: ${request.reason}`
                : '';
            note.textContent =
                'هذا الطلب يحتاج إلى تعديل — عدّل البيانات ثم أعد الإرسال للمراجعة.' + reason;
            note.hidden = false;
        }
        if (newBtn) {
            newBtn.hidden = false;
        }
    }

    /**
     * Reflect owner-edit mode on the freshly rendered form: prefill
     * title/description from the cached request, lock every field the
     * owner may NOT edit (server rejects them anyway), and switch the
     * primary button to «حفظ التعديلات» with a visible cancel control.
     */
    function _applyOwnerEditMode() {
        const submitBtn = overlay.querySelector(
            '[data-testid="taskreq-submit"]'
        );
        const cancelBtn = overlay.querySelector(
            '[data-testid="taskreq-owner-cancel"]'
        );
        const newBtn = overlay.querySelector('[data-testid="taskreq-new"]');
        const note = overlay.querySelector(
            '[data-testid="taskreq-editnote"]'
        );

        if (!ownerEditTaskId) {
            if (cancelBtn) {
                cancelBtn.hidden = true;
            }
            if (submitBtn) {
                submitBtn.textContent = 'إرسال للمراجعة';
            }
            _setExecutionFieldsDisabled(false);
            return;
        }

        const request = requestsCache.find(
            (r) => r.status === 'approved' &&
                String(r.task_id) === String(ownerEditTaskId)
        );
        if (!request) {
            ownerEditTaskId = null;
            if (cancelBtn) {
                cancelBtn.hidden = true;
            }
            return;
        }

        _setField('taskreq-title', request.title);
        _setField('taskreq-description', request.description);
        // Platform / action / target link / reward are fixed at
        // approval time for the owner: locked, never editable.
        _setExecutionFieldsDisabled(true);
        if (note) {
            note.textContent =
                'تعديل مهمة منشورة — العنوان والوصف فقط. المنصة والإجراء والرابط والمكافأة ثابتة.';
            note.hidden = false;
        }
        if (newBtn) {
            newBtn.hidden = true;
        }
        if (submitBtn) {
            submitBtn.textContent = '💾 حفظ التعديلات';
        }
        if (cancelBtn) {
            cancelBtn.hidden = false;
        }
    }

    /** Lock/unlock the fields an owner edit may never change. */
    function _setExecutionFieldsDisabled(disabled) {
        const ids = [
            'taskreq-provider', 'taskreq-action',
            'taskreq-target', 'taskreq-reward',
        ];
        for (const id of ids) {
            const node = overlay.querySelector(`[data-testid="${id}"]`);
            if (node) {
                node.disabled = disabled;
            }
        }
    }

    /** Wire owner-management buttons after every render (ALL items). */
    function _wireOwnerActions() {
        _wireOwnerButtons('taskreq-owner-edit', (id, btn) => {
            if (_requireInitData()) {
                _startOwnerEdit(id);
            }
        });
        _wireOwnerButtons('taskreq-owner-pause', (id, btn) => {
            if (_requireInitData()) {
                _ownerPause(id, btn);
            }
        });
        _wireOwnerButtons('taskreq-owner-resume', (id, btn) => {
            if (_requireInitData()) {
                _ownerResume(id, btn);
            }
        });
        _wireOwnerButtons('taskreq-owner-archive', (id, btn) => {
            if (_requireInitData()) {
                _ownerArchive(id, btn);
            }
        });
    }

    function _wireOwnerButtons(testid, handler) {
        const buttons = overlay.querySelectorAll(
            `[data-testid="${testid}"]`
        ) || [];
        for (const btn of buttons) {
            btn.addEventListener('click', () => {
                handler(btn.getAttribute('data-published-task-id'), btn);
            });
        }
    }

    function _formError(message) {
        const box = overlay.querySelector(
            '[data-testid="taskreq-form-error"]'
        );
        if (!box) {
            return;
        }
        box.textContent = message || '';
        box.hidden = !message;
    }

    function _field(testid) {
        const node = overlay.querySelector(`[data-testid="${testid}"]`);
        return node ? node.value : '';
    }

    /**
     * Create (POST) or resubmit (PATCH) the proposal. The payload is
     * the full field set; the server validates everything again and
     * derives identity from initData only.
     */
    async function _submit() {
        if (busy) {
            return;
        }
        if (ownerEditTaskId) {
            // In owner-edit mode the SAME primary button saves the
            // published task instead of creating a proposal.
            await _ownerEdit(ownerEditTaskId, overlay.querySelector(
                '[data-testid="taskreq-submit"]'
            ));
            return;
        }
        const payload = {
            title: _field('taskreq-title').trim(),
            description: _field('taskreq-description').trim(),
            provider: _field('taskreq-provider'),
            action: _field('taskreq-action'),
            target_ref: _field('taskreq-target').trim(),
            reward: _field('taskreq-reward').trim()
        };

        // Quick feedback only — the server is authoritative.
        if (!payload.title || !payload.description || !payload.reward) {
            _formError('أدخل عنوان المهمة ووصفها والمكافأة');
            return;
        }

        _haptic();
        busy = true;
        _formError('');
        const submitBtn = overlay.querySelector(
            '[data-testid="taskreq-submit"]'
        );
        submitBtn.disabled = true;
        submitBtn.textContent = 'جارٍ الإرسال…';

        const url = editRequestId
            ? `${LIST_URL}/${editRequestId}`
            : LIST_URL;
        const method = editRequestId ? 'PATCH' : 'POST';

        try {
            const response = await fetch(url, {
                method: method,
                headers: _headers(),
                body: JSON.stringify(payload)
            });
            const data = await _parse(response);
            if (!response.ok || !data || data.ok !== true) {
                busy = false;
                submitBtn.disabled = false;
                submitBtn.textContent = 'إرسال للمراجعة';
                _formError(_messageFor(data));
                return;
            }
            busy = false;
            _setSuccess(data.message || 'تم إرسال المهمة للمراجعة من الإدارة.');
            justSubmitted = true;
            editRequestId = null;
            // Refresh the caller's own list from the server.
            await _reload();
        } catch (error) {
            busy = false;
            submitBtn.disabled = false;
            submitBtn.textContent = 'إرسال للمراجعة';
            _formError(_messageFor(null));
        }
    }

    async function _reload() {
        try {
            const response = await fetch(LIST_URL, { headers: _headers() });
            const data = await _parse(response);
            if (response.ok && data && data.ok === true) {
                requestsCache = Array.isArray(data.requests)
                    ? data.requests : [];
            }
        } catch (error) {
            // Keep the previous list — the submit already succeeded.
        }
        _renderMain();
    }

    return { open, close };
})();
