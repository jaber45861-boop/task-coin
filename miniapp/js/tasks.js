/**
 * Tasks Page (MT-TASK-03)
 * -----------------------
 * Renders the production Tasks page from the real backend API
 * (/api/tasks) — no hardcoded task dataset anywhere in this file.
 *
 * Boundaries:
 * - identity comes only from the verified Telegram initData header
 *   (TelegramApp.getInitData) — this page never supplies a user id
 * - the server decides every task status; this file only displays it
 *   (available / started / completed) and never re-completes a task
 * - reward is display-only metadata read from the API response; it is
 *   never sent back to the server and no funds controls live here
 * - the channel join link (join_url) comes from the server response;
 *   no channel URL is hardcoded in JavaScript and clicking "join"
 *   never marks a task complete — only the verify call can do that
 * - manual tasks (MT-TASK-17) expose the task's own validated
 *   external destination (`task_url`) and submit the bounded
 *   text/URL proof_ref — opening that link and sending the proof are
 *   two separate, explicit user actions
 *   through the existing submit endpoint; only the server response
 *   (awaiting_decision / status) decides the card's next state
 * - business rules stay in the backend; this file only maps the
 *   server's Arabic messages (with safe fallbacks) onto the UI
 */
const Tasks = (() => {
    const LIST_URL = '/api/tasks';
    const INIT_DATA_HEADER = 'X-Telegram-Init-Data';

    // Arabic fallbacks keyed by the backend's stable error codes.
    // The server-provided Arabic `message` always wins when present.
    const ERROR_MESSAGES = {
        unauthenticated: 'افتح التطبيق من تيليجرام أولاً',
        task_not_found: 'المهمة غير موجودة',
        task_inactive: 'المهمة غير متاحة حالياً',
        task_not_available: 'هذه المهمة غير متاحة لك',
        task_already_started: 'بدأت هذه المهمة مسبقاً',
        task_already_completed: 'لقد أكملت هذه المهمة مسبقاً',
        task_not_started: 'يجب بدء المهمة أولاً',
        invalid_task_state: 'لا يمكن تنفيذ هذا الإجراء الآن',
        invalid_request: 'الطلب غير صالح',
        invalid_submission: 'بيانات الإرسال غير صالحة',
        invalid_proof: 'الإثبات غير صالح، أرسل رابطاً أو نصاً واضحاً',
        verification_failed: 'لم يتم تأكيد الإنجاز، تأكد من اشتراكك ثم أعد المحاولة',
        verification_error: 'تعذر التحقق حالياً، حاول مرة أخرى لاحقاً',
        submission_in_progress: 'جارٍ التحقق من محاولة سابقة، حاول بعد قليل',
        server_error: 'حدث خطأ غير متوقع، حاول مرة أخرى',
        network: 'تعذر الاتصال بالخادم، حاول مرة أخرى'
    };

    // Statuses the backend supports — including owner-managed states.
    const STATUS_LABELS = {
        available: 'متاحة',
        started: 'بدأت',
        completed: 'مكتملة',
        paused: 'متوقفة مؤقتاً',
        archived: 'مؤرشفة'
    };

    // Status CSS class suffixes for paused / archived cards.
    const STATUS_CLASSES = {
        available: 'available',
        started: 'started',
        completed: 'completed',
        paused: 'paused',
        archived: 'archived'
    };

    const TYPE_LABELS = {
        channel_subscription: 'اشتراك في قناة',
        deterministic: 'مهمة تحقق',
        manual: 'مهمة يدوية',
        referral_task: 'مهمة إحالة',
        telegram_channel: 'انضمام عبر تيليجرام'
    };

    let pageEl = null;
    let tasksCache = [];
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

    /**
     * Idempotency key for one submission attempt (MT-TASK-04).
     * Fresh per user action; the backend dedupes an identical
     * retried request by this header — it carries no identity or
     * business data.
     */
    function _idempotencyKey() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        return 'k' + Date.now().toString(36)
            + Math.random().toString(36).slice(2, 12);
    }

    /**
     * JSON body for a manual proof: exactly the one field the
     * existing submit endpoint reads (proof_ref), escaped for JSON.
     * Nothing else — no identity, no status, no reward — is ever
     * serialized by this page.
     */
    function _proofBody(value) {
        const escaped = String(value)
            .replace(/\\/g, '\\\\')
            .replace(/"/g, '\\"')
            .replace(/\n/g, '\\n')
            .replace(/\r/g, '\\r')
            .replace(/\t/g, '\\t');
        return '{"proof_ref":"' + escaped + '"}';
    }

    /**
     * True only for a well-formed absolute https URL with a non-empty
     * host.  The value comes from the server, which already validates
     * it — this is a defensive second check so a bad value can never
     * become a clickable href.  http:, javascript: and data: are all
     * refused by this check.
     */
    function _isSafeHttpsUrl(value) {
        if (typeof value !== 'string' || !value) {
            return false;
        }
        const trimmed = value.trim();
        if (!trimmed.toLowerCase().startsWith('https://')) {
            return false;
        }
        try {
            const parsed = new URL(trimmed);
            return parsed.protocol === 'https:' && parsed.hostname.length > 0;
        } catch (error) {
            return false;
        }
    }

    function _hasOwn(object, key) {
        return Object.prototype.hasOwnProperty.call(object, key);
    }

    /** Drop a chosen screenshot and reset every related control. */
    function _clearImage(preview, removeBtn, statusEl, fileInput) {
        if (preview) {
            preview.removeAttribute('src');
            preview.hidden = true;
        }
        if (removeBtn) {
            removeBtn.hidden = true;
        }
        if (statusEl) {
            statusEl.textContent = '';
            statusEl.hidden = true;
        }
        if (fileInput) {
            fileInput.value = '';   // allow re-picking the SAME file
        }
    }

    /**
     * Upload the chosen screenshot and return its server reference.
     *
     * The image is validated and stored SERVER-SIDE (magic bytes,
     * bounded size and dimensions) under a server-generated key.  This
     * never submits the proof: it only stages the evidence.  Returns
     * null on any failure so the caller shows an error and the
     * existing text/URL proof path is left untouched.
     */
    async function _uploadScreenshot(task, fileInput, statusEl) {
        if (!fileInput || !fileInput.files || !fileInput.files[0]) {
            return null;
        }
        const payload = new FormData();
        payload.append('file', fileInput.files[0]);
        statusEl.textContent = 'جارٍ رفع الصورة…';
        statusEl.hidden = false;
        try {
            const response = await fetch(`/api/tasks/${task.id}/proof-image`, {
                method: 'POST',
                headers: _headers(),
                body: payload
            });
            const data = await response.json().catch(() => null);
            if (!response.ok || !data || data.ok !== true || !data.proof_ref) {
                statusEl.textContent = (data && data.message) || 'تعذر رفع الصورة';
                return null;
            }
            statusEl.textContent = data.message || 'تم تجهيز الصورة';
            return data.proof_ref;
        } catch (error) {
            statusEl.textContent = 'تعذر الاتصال بالخادم';
            return null;
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

    /* ── Page shell ─────────────────────────────────────────────── */

    function render() {
        const page = document.createElement('div');
        page.className = 'page page-tasks';
        page.setAttribute('data-testid', 'tasks-page');

        page.innerHTML = `
            <div class="page-header">
                <h2>المهام</h2>
            </div>
            <div class="page-content">
                <div class="tasks-state tasks-loading" data-testid="tasks-loading">جارٍ تحميل المهام…</div>
                <div class="tasks-state tasks-empty" data-testid="tasks-empty" hidden>لا توجد مهام متاحة حالياً</div>
                <div class="tasks-state tasks-error" data-testid="tasks-error" hidden>
                    <span class="tasks-error-text" data-testid="tasks-error-text"></span>
                    <button type="button" class="tasks-retry-btn" data-testid="tasks-retry">إعادة المحاولة</button>
                </div>
                <div class="tasks-notice" data-testid="tasks-notice" hidden></div>
                <div class="tasks-list" data-testid="tasks-list" hidden></div>
            </div>
        `;

        const retry = page.querySelector('[data-testid="tasks-retry"]');
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
        _show(_node('tasks-loading'), true);
        _show(_node('tasks-empty'), false);
        _show(_node('tasks-error'), false);
        _show(_node('tasks-notice'), false);
        _show(_node('tasks-list'), false);

        let data = null;
        let ok = false;
        try {
            const response = await fetch(LIST_URL, { headers: _headers() });
            data = await _parse(response);
            ok = response.ok && data && data.ok === true;
        } catch (error) {
            ok = false;
        }

        _show(_node('tasks-loading'), false);

        if (!ok) {
            _showError(_messageFor(data));
            return;
        }

        tasksCache = Array.isArray(data.tasks) ? data.tasks : [];
        _renderTasks(tasksCache);
    }

    function _showError(message) {
        const text = _node('tasks-error-text');
        if (text) {
            text.textContent = message;
        }
        _show(_node('tasks-error'), true);
    }

    function _showNotice(message, kind) {
        const notice = _node('tasks-notice');
        if (!notice) {
            return;
        }
        notice.textContent = message;
        notice.classList.remove('tasks-notice-error', 'tasks-notice-success');
        notice.classList.add(kind === 'success' ? 'tasks-notice-success' : 'tasks-notice-error');
        _show(notice, true);
    }

    function _hideNotice() {
        _show(_node('tasks-notice'), false);
    }

    /* ── Task list rendering ────────────────────────────────────── */

    function _renderTasks(tasks) {
        const list = _node('tasks-list');
        if (!list) {
            return;
        }
        list.innerHTML = '';
        for (const task of tasks) {
            list.appendChild(_buildCard(task));
        }
        _show(_node('tasks-empty'), tasks.length === 0);
        _show(_node('tasks-list'), tasks.length > 0);
        _show(_node('tasks-error'), false);
    }

    function _buildCard(task) {
        const card = document.createElement('article');
        card.className = 'task-card';
        card.setAttribute('data-testid', 'task-card');
        card.setAttribute('data-status', task.status);
        card.setAttribute('data-type', task.type);

        let statusKey = task.status;
        if (task.archived_at) {
            statusKey = 'archived';
        } else if (task.paused) {
            statusKey = 'paused';
        }
        const statusLabel = STATUS_LABELS[statusKey] || STATUS_LABELS.available;
        const statusClass = STATUS_CLASSES[statusKey] || 'available';
        const typeLabel = TYPE_LABELS[task.type] || task.type;

        card.innerHTML = `
            <div class="task-card-top">
                <span class="task-title" data-testid="task-title"></span>
                <span class="task-status task-status-${statusClass}" data-testid="task-status"></span>
            </div>
            <p class="task-description" data-testid="task-description"></p>
            <div class="task-meta">
                <span class="task-type-badge" data-testid="task-type"></span>
                <span class="task-reward" data-testid="task-reward"></span>
            </div>
            <div class="task-actions" data-testid="task-actions"></div>
        `;

        // API strings are injected as text — never as markup.
        card.querySelector('[data-testid="task-title"]').textContent = task.title || '';
        card.querySelector('[data-testid="task-status"]').textContent = statusLabel;
        card.querySelector('[data-testid="task-description"]').textContent = task.description || '';
        card.querySelector('[data-testid="task-type"]').textContent = typeLabel;
        // Reward is read-only metadata rendered exactly as the server sent it.
        // Sub-cent tasks carry the exact atomic field (integer USDT
        // units): the display derives from it with integer math only —
        // never JavaScript floating-point — through the existing
        // WalletData formatter; the whole-USDT `reward` field remains
        // the fallback for rows without an atomic value.
        const hasExactUnits = task.reward_units !== null
            && task.reward_units !== undefined
            && typeof WalletData !== 'undefined';
        const rewardText = hasExactUnits
            ? WalletData.formatUsdt(task.reward_units)
            : String(task.reward);
        card.querySelector('[data-testid="task-reward"]').textContent =
            'المكافأة: ' + rewardText;

        _buildActions(card.querySelector('[data-testid="task-actions"]'), task);
        return card;
    }

    function _buildActions(actions, task) {
        if (!actions) {
            return;
        }

        // A referral claim or manual proof is already awaiting its
        // server-side decision, so no submit control is offered while
        // the server keeps reporting awaiting_decision.
        if (task.awaiting_decision === true) {
            const waiting = document.createElement('span');
            waiting.className = 'task-waiting-label';
            waiting.setAttribute('data-testid', 'task-awaiting');
            // Referral keeps the buyer wording; manual proofs show
            // the clearer "under review" wording.
            waiting.textContent = task.type === 'manual'
                ? 'المهمة قيد المراجعة'
                : 'بانتظار موافقة العميل';
            actions.appendChild(waiting);
            return;
        }

        if (task.status === 'available') {
            const startBtn = document.createElement('button');
            startBtn.type = 'button';
            startBtn.className = 'task-action-btn';
            startBtn.setAttribute('data-testid', 'task-start');
            startBtn.textContent = 'ابدأ المهمة';
            startBtn.addEventListener('click', () => startTask(task, startBtn));
            actions.appendChild(startBtn);
            return;
        }

        if (task.status === 'started') {
            // Server-provided public join destination only (channel tasks).
            if (typeof task.join_url === 'string' && task.join_url.startsWith('https://')) {
                const joinLink = document.createElement('a');
                joinLink.className = 'task-action-btn task-join-btn';
                joinLink.setAttribute('data-testid', 'task-join');
                joinLink.href = task.join_url;
                joinLink.target = '_blank';
                joinLink.rel = 'noopener';
                joinLink.textContent = 'انضم للقناة';
                actions.appendChild(joinLink);
            }

            if (task.type === 'manual') {
                // The task's own external destination, when the server
                // sent one.  A SEPARATE control: it opens the link and
                // never submits anything — the proof stays an explicit
                // later step by the user.  Same secure link pattern the
                // channel join link already uses.
                if (_isSafeHttpsUrl(task.task_url)) {
                    const openLink = document.createElement('a');
                    openLink.className = 'task-action-btn task-open-btn';
                    openLink.setAttribute('data-testid', 'task-open-url');
                    openLink.href = task.task_url;
                    openLink.target = '_blank';
                    openLink.rel = 'noopener';
                    openLink.textContent = 'فتح رابط المهمة';
                    actions.appendChild(openLink);
                } else if (_hasOwn(task, 'task_url')) {
                    // The server DID advertise a destination but it
                    // failed validation — say so instead of silently
                    // offering nothing.
                    _showNotice('رابط المهمة غير صالح، تواصل مع الإدارة', 'error');
                }

                // Manual proof (MT-TASK-17): one bounded text/URL
                // reference posted to the existing submit endpoint
                // as proof_ref — the server validates and reviews it.
                const proofInput = document.createElement('input');
                proofInput.type = 'text';
                proofInput.className = 'task-proof-input';
                proofInput.setAttribute('data-testid', 'task-proof-input');
                proofInput.maxLength = 500;
                proofInput.placeholder = 'رابط الإثبات أو نصاً واضحاً';

                const proofBtn = document.createElement('button');
                proofBtn.type = 'button';
                proofBtn.className = 'task-action-btn';
                proofBtn.setAttribute('data-testid', 'task-proof-submit');
                proofBtn.textContent = 'إرسال الإثبات';
                proofBtn.addEventListener('click', () =>
                    submitProof(task, proofInput, proofBtn, fileInput, uploadStatus));

                // ── Screenshot evidence (MT-ADMIN-34) ──────────────
                // A SEPARATE, explicit upload control: the image is
                // validated and stored server-side, and the returned
                // reference lands in the text field above so the
                // EXISTING submit path carries it.  Nothing is sent
                // until the user presses «إرسال الإثبات».
                const fileInput = document.createElement('input');
                fileInput.type = 'file';
                fileInput.accept = 'image/png,image/jpeg,image/gif';
                fileInput.className = 'task-proof-file';
                fileInput.setAttribute('data-testid', 'task-proof-file');
                // Only set capture on devices that expose it — desktop
                // browsers must keep the normal gallery picker.
                if ('capture' in fileInput) {
                    fileInput.capture = 'environment';
                }

                const preview = document.createElement('img');
                preview.className = 'task-proof-preview';
                preview.setAttribute('data-testid', 'task-proof-preview');
                preview.alt = '';
                preview.hidden = true;

                const removeBtn = document.createElement('button');
                removeBtn.type = 'button';
                removeBtn.className = 'task-proof-remove';
                removeBtn.setAttribute('data-testid', 'task-proof-remove');
                removeBtn.textContent = '✕ إزالة الصورة';
                removeBtn.hidden = true;

                const uploadStatus = document.createElement('span');
                uploadStatus.className = 'task-proof-upload-status';
                uploadStatus.setAttribute('data-testid', 'task-proof-upload-status');
                uploadStatus.hidden = true;

                // Replace/remove before sending: choosing a new file
                // clears the previous selection entirely.
                fileInput.addEventListener('change', () => {
                    const file = fileInput.files && fileInput.files[0];
                    if (!file) {
                        _clearImage(preview, removeBtn, uploadStatus, fileInput);
                        return;
                    }
                    // Local preview only — the bytes are not sent yet.
                    const reader = new FileReader();
                    reader.onload = () => {
                        preview.src = String(reader.result);
                        preview.hidden = false;
                        removeBtn.hidden = false;
                        uploadStatus.textContent = 'تم اختيار الصورة، اضغط إرسال الإثبات';
                        uploadStatus.hidden = false;
                    };
                    reader.onerror = () => {
                        _clearImage(preview, removeBtn, uploadStatus, fileInput);
                        _showNotice('تعذر قراءة الصورة المختارة', 'error');
                    };
                    reader.readAsDataURL(file);
                });
                removeBtn.addEventListener('click', () => {
                    _clearImage(preview, removeBtn, uploadStatus, fileInput);
                });

                actions.appendChild(proofInput);
                actions.appendChild(proofBtn);
                actions.appendChild(fileInput);
                actions.appendChild(preview);
                actions.appendChild(removeBtn);
                actions.appendChild(uploadStatus);
                return;
            }

            const submitBtn = document.createElement('button');
            submitBtn.type = 'button';
            submitBtn.className = 'task-action-btn';
            submitBtn.setAttribute('data-testid', 'task-submit');
            submitBtn.textContent = 'تحقق وإتمام';
            submitBtn.addEventListener('click', () => submitTask(task, submitBtn));
            actions.appendChild(submitBtn);
            return;
        }

        // completed — terminal, no further action is offered.
        const done = document.createElement('span');
        done.className = 'task-done-label';
        done.setAttribute('data-testid', 'task-completed-label');
        done.textContent = 'تم الإنجاز ✓';
        actions.appendChild(done);
    }

    function _replaceTask(updated) {
        const index = tasksCache.findIndex((t) => t.id === updated.id);
        if (index >= 0) {
            tasksCache[index] = updated;
        }
        _renderTasks(tasksCache);
    }

    /* ── Actions ────────────────────────────────────────────────── */

    async function startTask(task, button) {
        if (busy) {
            return;
        }
        busy = true;
        _haptic();
        _hideNotice();
        if (button) {
            button.disabled = true;
        }

        let data = null;
        let ok = false;
        try {
            const response = await fetch(`/api/tasks/${task.id}/start`, {
                method: 'POST',
                headers: _headers()
            });
            data = await _parse(response);
            ok = response.ok && data && data.ok === true;
        } catch (error) {
            ok = false;
        }

        busy = false;

        if (ok) {
            // Only the status changes.  NO link is opened here and NO
            // proof is submitted: the task's external destination and
            // the proof are both explicit, separate user actions after
            // the card re-renders in its `started` state.
            _replaceTask(Object.assign({}, task, { status: 'started' }));
            _showNotice(data.message || 'تم بدء المهمة', 'success');
            // Tell the user the next step, so an empty-looking card is
            // never confusing.  Wording depends only on whether the
            // server advertised a usable destination.
            if (_hasOwn(task, 'task_url') && !_isSafeHttpsUrl(task.task_url)) {
                _showNotice('رابط المهمة غير صالح، تواصل مع الإدارة', 'error');
            } else if (_isSafeHttpsUrl(task.task_url)) {
                _showNotice('افتح رابط المهمة، ثم أرسل الإثبات', 'success');
            } else {
                _showNotice('أرسل الإثبات بعد تنفيذ المهمة', 'success');
            }
            return;
        }

        if (button) {
            button.disabled = false;
        }
        _showNotice(_messageFor(data), 'error');
    }

    async function submitTask(task, button) {
        if (busy) {
            return;
        }
        busy = true;
        _haptic();
        _hideNotice();
        if (button) {
            button.disabled = true;
        }

        let data = null;
        let ok = false;
        try {
            const headers = _headers();
            headers['Idempotency-Key'] = _idempotencyKey();
            // No payload: the server-side task definition is authoritative
            // and verification data is checked by the backend pipeline.
            const response = await fetch(`/api/tasks/${task.id}/submit`, {
                method: 'POST',
                headers: headers
            });
            data = await _parse(response);
            ok = response.ok && data && data.ok === true;
        } catch (error) {
            ok = false;
        }

        busy = false;

        if (ok) {
            if (data.awaiting_decision === true) {
                // Referral claim accepted — the task stays started
                // until the buyer's decision arrives server-side.
                _replaceTask(Object.assign({}, task, {
                    status: 'started',
                    awaiting_decision: true
                }));
            } else {
                _replaceTask(Object.assign({}, task, { status: 'completed' }));
            }
            _showNotice(data.message || 'تم إنجاز المهمة بنجاح', 'success');
            return;
        }

        if (button) {
            button.disabled = false;
        }
        _showNotice(_messageFor(data), 'error');
    }

    /**
     * Manual proof submission (MT-TASK-17): posts the bounded
     * proof_ref to the existing submit endpoint. Every outcome —
     * validation, waiting state, review decision — comes from the
     * server response; this function only renders it.
     */
    async function submitProof(task, input, button, fileInput, statusEl) {
        if (busy) {
            return;
        }
        busy = true;
        _haptic();
        _hideNotice();
        if (button) {
            button.disabled = true;
        }

        // Stage the chosen screenshot first (server-side validation and
        // storage).  A failure aborts the submission: the user fixes
        // the image or falls back to a text/URL proof — but a broken
        // upload must never silently submit an empty proof.
        if (fileInput && fileInput.files && fileInput.files[0]) {
            const reference = await _uploadScreenshot(task, fileInput, statusEl);
            if (reference) {
                input.value = reference;   // carried by the EXISTING path
            } else {
                busy = false;
                if (button) {
                    button.disabled = false;
                }
                _showNotice('تعذر رفع الصورة، أعد المحاولة أو أرسل رابطاً', 'error');
                return;
            }
        }

        let data = null;
        let ok = false;
        try {
            const headers = _headers();
            headers['Content-Type'] = 'application/json';
            headers['Idempotency-Key'] = _idempotencyKey();
            const response = await fetch(`/api/tasks/${task.id}/submit`, {
                method: 'POST',
                headers: headers,
                body: _proofBody(input.value)
            });
            data = await _parse(response);
            ok = response.ok && data && data.ok === true;
        } catch (error) {
            ok = false;
        }

        busy = false;

        if (ok) {
            // The server decides: awaiting its review keeps the card
            // in the waiting state, its approval completes the task.
            _replaceTask(Object.assign({}, task, {
                status: data.status || 'started',
                awaiting_decision: data.awaiting_decision === true
            }));
            _showNotice(data.message || 'تم استلام الإثبات', 'success');
            return;
        }

        // Failure (invalid proof, a decision against this attempt,
        // network): the server message is shown and the input stays
        // editable, so a new proof can be submitted right away.
        if (button) {
            button.disabled = false;
        }
        _showNotice(_messageFor(data), 'error');
    }

    return {
        render,
        load,
        startTask,
        submitTask
    };
})();
