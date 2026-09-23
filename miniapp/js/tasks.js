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
        verification_failed: 'لم يتم تأكيد الإنجاز، تأكد من اشتراكك ثم أعد المحاولة',
        verification_error: 'تعذر التحقق حالياً، حاول مرة أخرى لاحقاً',
        submission_in_progress: 'جارٍ التحقق من محاولة سابقة، حاول بعد قليل',
        server_error: 'حدث خطأ غير متوقع، حاول مرة أخرى',
        network: 'تعذر الاتصال بالخادم، حاول مرة أخرى'
    };

    // Exactly the three statuses the backend supports.
    const STATUS_LABELS = {
        available: 'متاحة',
        started: 'بدأت',
        completed: 'مكتملة'
    };

    const TYPE_LABELS = {
        channel_subscription: 'اشتراك في قناة',
        deterministic: 'مهمة تحقق'
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

        const statusKey = STATUS_LABELS[task.status] ? task.status : 'available';
        const statusLabel = STATUS_LABELS[statusKey];
        const typeLabel = TYPE_LABELS[task.type] || task.type;

        card.innerHTML = `
            <div class="task-card-top">
                <span class="task-title" data-testid="task-title"></span>
                <span class="task-status task-status-${statusKey}" data-testid="task-status"></span>
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
        card.querySelector('[data-testid="task-reward"]').textContent =
            'المكافأة: ' + String(task.reward);

        _buildActions(card.querySelector('[data-testid="task-actions"]'), task);
        return card;
    }

    function _buildActions(actions, task) {
        if (!actions) {
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
            _replaceTask(Object.assign({}, task, { status: 'started' }));
            _showNotice(data.message || 'تم بدء المهمة', 'success');
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
            _replaceTask(Object.assign({}, task, { status: 'completed' }));
            _showNotice(data.message || 'تم إنجاز المهمة بنجاح', 'success');
            return;
        }

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
