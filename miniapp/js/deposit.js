/**
 * Deposit Panel (MT-ADMIN-28)
 * ===========================
 * Opens from the Wallet page الإيداع button and talks to the production
 * deposit API — no financial logic lives here or in wallet.js.
 *
 * Boundaries (mirrors withdrawal.js):
 * - identity comes only from the verified Telegram initData header
 *   (TelegramApp.getInitData); this file never supplies a user id
 * - GET  /api/deposit/methods → methods EXPLICITLY configured as
 *   active deposit methods (server-safe metadata; the platform deposit
 *   destination is intentionally included — it is where the user SENDS
 *   funds).  Methods are loaded FRESH on every open, so a deactivated
 *   method can never be offered.
 * - POST /api/deposit        → {payment_method_id, amount}; the amount
 *   is sent as the RAW text the user typed — this file never parses it
 *   into a number, never converts a currency and never computes a fee:
 *   the server owns every rule (exact integer units, minimum deposit)
 *   and replies with the authoritative result.
 * - a created deposit request is ALWAYS pending/unverified: this UI
 *   never claims funds were received — there is no
 *   blockchain/confirmation UI here by design.
 * - POST /api/deposit/proof → upload ONE payment screenshot for
 *   MANUAL admin review (MT-ADMIN-31).  The image is EVIDENCE only:
 *   the only success claim this flow may show is
 *   “تم إرسال إثبات الدفع للمراجعة” — never that funds arrived or
 *   were added.  Identity still comes only from initData; no user id,
 *   amount, decision or verification fact is ever sent from here.
 * - server Arabic messages (with stable-code fallbacks) decide every
 *   error state; no request payload is ever logged.
 */
const DepositUI = (() => {
    const METHODS_URL = '/api/deposit/methods';
    const CREATE_URL = '/api/deposit';
    const PROOF_URL = '/api/deposit/proof';
    const INIT_DATA_HEADER = 'X-Telegram-Init-Data';

    // Arabic fallbacks keyed by the backend's stable error codes.
    // The server-provided Arabic `message` always wins when present.
    const ERROR_MESSAGES = {
        unauthenticated: 'افتح التطبيق من تيليجرام أولاً',
        invalid_request: 'الطلب غير صالح',
        invalid_amount: 'المبلغ غير صالح',
        below_minimum: 'المبلغ أقل من الحد الأدنى للإيداع',
        payment_method_not_found: 'وسيلة الإيداع غير موجودة',
        payment_method_unavailable: 'وسيلة الدفع غير متاحة حالياً',
        deposit_method_unavailable: 'هذه الوسيلة غير متاحة للإيداع حالياً',
        deposit_settings_missing: 'إعدادات الإيداع غير مكتملة، تواصل مع الإدارة',
        request_not_found: 'طلب الإيداع غير موجود',
        request_forbidden: 'لا يمكنك إرسال إثبات لهذا الطلب',
        request_processed: 'تمت معالجة طلب الإيداع بالفعل',
        proof_pending_review: 'إثبات الدفع قيد المراجعة بالفعل',
        invalid_upload: 'صورة الإثبات غير صالحة',
        unsupported_image_type: 'الملف ليس صورة مدعومة (PNG أو JPG أو GIF)',
        file_too_large: 'حجم الصورة كبير جداً',
        server_error: 'حدث خطأ غير متوقع، حاول مرة أخرى',
        network: 'تعذر الاتصال بالخادم، حاول مرة أخرى'
    };

    // ONLY the states this phase can produce.  There is deliberately
    // no terminal-success label: no response from this flow can ever mean
    // the funds were received.
    const STATUS_LABELS = {
        pending: '⏳ قيد التحقق',
        rejected: '❌ مرفوض'
    };

    let overlay = null;
    let methodsCache = null;
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

    /** Existing project haptic helper pattern (same as wallet.js). */
    function _haptic() {
        if (window.Telegram?.WebApp?.HapticFeedback) {
            window.Telegram.WebApp.HapticFeedback.impactOccurred('light');
        }
    }

    function _composition(method) {
        return method.network
            ? `${method.asset} · ${method.network}`
            : method.asset;
    }

    /* ── Overlay shell ──────────────────────────────────────────── */

    function _buildShell() {
        const el = document.createElement('div');
        el.className = 'withdrawal-overlay';
        el.setAttribute('data-testid', 'deposit-overlay');
        return el;
    }

    /**
     * Open the deposit panel (called by the Wallet الإيداع button).
     * Methods are loaded fresh from the API on every open so an
     * inactive (or not-deposit-enabled) method can never be offered.
     */
    async function open() {
        if (overlay) {
            return;
        }
        overlay = _buildShell();
        document.body.appendChild(overlay);
        _renderLoading();

        try {
            const response = await fetch(METHODS_URL, { headers: _headers() });
            const data = await response.json().catch(() => ({}));
            if (!response.ok || !data.ok) {
                _renderMessage(_messageFor(data));
                return;
            }
            methodsCache = Array.isArray(data.methods) ? data.methods : [];
            if (methodsCache.length === 0) {
                _renderMessage('لا توجد وسائل إيداع متاحة حالياً');
                return;
            }
            _renderForm();
        } catch (err) {
            _renderMessage(_messageFor(null));
        }
    }

    function close() {
        if (overlay) {
            overlay.remove();
            overlay = null;
            methodsCache = null;
            busy = false;
        }
    }

    function _messageFor(data) {
        if (data && data.message) {
            return data.message;
        }
        if (data && data.error && ERROR_MESSAGES[data.error]) {
            return ERROR_MESSAGES[data.error];
        }
        return ERROR_MESSAGES.network;
    }

    function _renderLoading() {
        overlay.innerHTML = `
            <div class="withdrawal-card" role="dialog" aria-modal="true"
                 aria-label="إيداع الرصيد">
                <div class="withdrawal-head">
                    <h3 class="withdrawal-title">إيداع الرصيد</h3>
                    <button type="button" class="withdrawal-close"
                            data-testid="deposit-close"
                            aria-label="إغلاق">✕</button>
                </div>
                <div class="withdrawal-status" data-testid="deposit-loading">
                    جارٍ التحميل…
                </div>
            </div>`;
        _wireClose();
    }

    function _renderMessage(message) {
        overlay.innerHTML = `
            <div class="withdrawal-card" role="dialog" aria-modal="true"
                 aria-label="إيداع الرصيد">
                <div class="withdrawal-head">
                    <h3 class="withdrawal-title">إيداع الرصيد</h3>
                    <button type="button" class="withdrawal-close"
                            data-testid="deposit-close"
                            aria-label="إغلاق">✕</button>
                </div>
                <div class="withdrawal-error" data-testid="deposit-error">
                    ${_esc(message)}
                </div>
            </div>`;
        _wireClose();
    }

    function _wireClose() {
        const closeBtn = overlay.querySelector(
            '[data-testid="deposit-close"]'
        );
        if (closeBtn) {
            closeBtn.addEventListener('click', close);
        }
    }

    /* ── Form ───────────────────────────────────────────────────── */

    function _methodOptions() {
        return methodsCache.map((m) => {
            const label = `${m.display_name} (${_composition(m)})`;
            return `<option value="${_esc(m.id)}">${_esc(label)}</option>`;
        }).join('');
    }

    function _renderMethodDetails(method) {
        const instructions = method.instructions
            ? `<div class="withdrawal-field" data-testid="deposit-instructions"
                    dir="auto">${_esc(method.instructions)}</div>`
            : '';
        return `
            <div class="withdrawal-status" data-testid="deposit-asset"
                 dir="auto">${_esc(_composition(method))} — ${_esc(method.provider)}</div>
            <label class="withdrawal-field">
                <span class="withdrawal-label">عنوان الإيداع (المنصة)</span>
                <input class="withdrawal-input" type="text" readonly
                       data-testid="deposit-destination"
                       dir="ltr" value="${_esc(method.destination)}">
            </label>
            <button type="button" class="withdrawal-submit"
                    data-testid="deposit-copy">📋 نسخ العنوان</button>
            ${instructions}`;
    }

    function _renderForm() {
        const first = methodsCache[0];
        overlay.innerHTML = `
            <div class="withdrawal-card" role="dialog" aria-modal="true"
                 aria-label="إيداع الرصيد">
                <div class="withdrawal-head">
                    <h3 class="withdrawal-title">إيداع الرصيد</h3>
                    <button type="button" class="withdrawal-close"
                            data-testid="deposit-close"
                            aria-label="إغلاق">✕</button>
                </div>
                <label class="withdrawal-field">
                    <span class="withdrawal-label">وسيلة الإيداع</span>
                    <select class="withdrawal-select"
                            data-testid="deposit-method">
                        ${_methodOptions()}
                    </select>
                </label>
                <div data-testid="deposit-details">
                    ${_renderMethodDetails(first)}
                </div>
                <label class="withdrawal-field">
                    <span class="withdrawal-label">
                        المبلغ (<span data-testid="deposit-unit"
                            >${_esc(first.asset)}</span>)
                    </span>
                    <input class="withdrawal-input" type="text"
                           inputmode="decimal" autocomplete="off"
                           data-testid="deposit-amount"
                           placeholder="0.00">
                </label>
                <div class="withdrawal-error" data-testid="deposit-form-error"
                     hidden></div>
                <button type="button" class="withdrawal-submit"
                        data-testid="deposit-submit">إنشاء طلب الإيداع</button>
            </div>`;
        _wireClose();

        const select = overlay.querySelector(
            '[data-testid="deposit-method"]'
        );
        select.addEventListener('change', () => {
            const method = _selectedMethod();
            overlay.querySelector('[data-testid="deposit-details"]')
                .innerHTML = _renderMethodDetails(method);
            overlay.querySelector('[data-testid="deposit-unit"]')
                .textContent = method.asset;
            _wireDetails(method);
        });

        _wireDetails(first);
        overlay.querySelector('[data-testid="deposit-submit"]')
            .addEventListener('click', _submit);
    }

    /** Wire the copy action + keep the destination read-only. */
    function _wireDetails(method) {
        const copyBtn = overlay.querySelector(
            '[data-testid="deposit-copy"]'
        );
        if (copyBtn) {
            copyBtn.addEventListener('click', async () => {
                _haptic();
                const original = copyBtn.textContent;
                try {
                    await navigator.clipboard.writeText(method.destination);
                    copyBtn.textContent = '✅ تم النسخ';
                } catch (err) {
                    // Fallback: select the readonly field for manual copy.
                    const field = overlay.querySelector(
                        '[data-testid="deposit-destination"]'
                    );
                    if (field) {
                        field.select();
                    }
                    copyBtn.textContent = 'انسخ يدوياً';
                }
                setTimeout(() => { copyBtn.textContent = original; }, 1500);
            });
        }
    }

    function _selectedMethod() {
        const select = overlay.querySelector(
            '[data-testid="deposit-method"]'
        );
        const id = Number(select.value);
        return methodsCache.find((m) => m.id === id) || methodsCache[0];
    }

    function _formError(message) {
        const box = overlay.querySelector(
            '[data-testid="deposit-form-error"]'
        );
        box.textContent = message;
        box.hidden = !message;
    }

    async function _submit() {
        if (busy) {
            return;
        }
        const method = _selectedMethod();
        // RAW text only — never parsed into a number here; the server
        // validates the exact decimal and owns every financial rule.
        const amount = overlay.querySelector(
            '[data-testid="deposit-amount"]'
        ).value.trim();

        if (!amount) {
            _formError('أدخل المبلغ');
            return;
        }

        busy = true;
        _formError('');
        const submitBtn = overlay.querySelector(
            '[data-testid="deposit-submit"]'
        );
        submitBtn.disabled = true;
        submitBtn.textContent = 'جارٍ الإرسال…';

        try {
            const response = await fetch(CREATE_URL, {
                method: 'POST',
                headers: _headers(),
                body: JSON.stringify({
                    payment_method_id: method.id,
                    amount: amount
                })
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok || !data.ok) {
                busy = false;
                submitBtn.disabled = false;
                submitBtn.textContent = 'إنشاء طلب الإيداع';
                _formError(_messageFor(data));
                return;
            }
            _renderCreated(data.request, data.message);
        } catch (err) {
            busy = false;
            submitBtn.disabled = false;
            submitBtn.textContent = 'إنشاء طلب الإيداع';
            _formError(_messageFor(null));
        }
    }

    /* ── Created (ALWAYS pending — funds not yet verified) ─────── */

    function _renderCreated(request, message) {
        const statusLabel = STATUS_LABELS[request.status]
            || '⏳ قيد التحقق';
        overlay.innerHTML = `
            <div class="withdrawal-card" role="dialog" aria-modal="true"
                 aria-label="إيداع الرصيد">
                <div class="withdrawal-success" data-testid="deposit-success">
                    ✅ ${_esc(message || 'تم إنشاء طلب الإيداع — بانتظار التحقق')}
                </div>
                <dl class="withdrawal-receipt"
                    data-testid="deposit-receipt">
                    <div><dt>رقم الطلب</dt><dd dir="ltr"
                        data-testid="deposit-request-id"
                        >${_esc(request.request_id)}</dd></div>
                    <div><dt>الحالة</dt><dd data-testid="deposit-status"
                        >${_esc(statusLabel)}</dd></div>
                    <div><dt>وسيلة الإيداع</dt><dd
                        >${_esc(request.display_name)}</dd></div>
                    <div><dt>المبلغ</dt><dd dir="ltr"
                        >${_esc(request.amount)} ${_esc(request.asset)}</dd></div>
                    <div><dt>تاريخ الإنشاء</dt><dd dir="ltr"
                        data-testid="deposit-created-at"
                        >${_esc(request.created_at)}</dd></div>
                </dl>
                <div class="withdrawal-field"
                     data-testid="deposit-proof-section">
                    <span class="withdrawal-label">إثبات الدفع
                        (صورة الإيصال)</span>
                    <input type="file"
                           accept="image/png,image/jpeg,image/gif"
                           data-testid="deposit-proof-file">
                    <div class="withdrawal-error"
                         data-testid="deposit-proof-error"
                         hidden></div>
                    <button type="button" class="withdrawal-submit"
                            data-testid="deposit-proof-submit"
                            >📤 إرسال إثبات الدفع</button>
                </div>
                <button type="button" class="withdrawal-submit"
                        data-testid="deposit-done">تم</button>
            </div>`;
        const done = overlay.querySelector(
            '[data-testid="deposit-done"]'
        );
        done.addEventListener('click', close);
        overlay.querySelector('[data-testid="deposit-proof-submit"]')
            .addEventListener('click', () => _uploadProof(request));
    }

    /* ── Manual proof upload (EVIDENCE ONLY — MT-ADMIN-31) ─────── */

    function _proofError(message) {
        const box = overlay.querySelector(
            '[data-testid="deposit-proof-error"]'
        );
        if (box) {
            box.textContent = message;
            box.hidden = !message;
        }
    }

    /**
     * Upload ONE payment screenshot for manual admin review.
     * Sends only the server-issued request id plus the image — no
     * user id, no amount, no decision.  The server replies with the
     * ONLY permitted confirmation (review pending).
     */
    async function _uploadProof(request) {
        if (busy) {
            return;
        }
        const input = overlay.querySelector(
            '[data-testid="deposit-proof-file"]'
        );
        const file = input && input.files ? input.files[0] : null;
        if (!file) {
            _proofError('اختر صورة إثبات الدفع أولاً');
            return;
        }

        busy = true;
        _proofError('');
        const button = overlay.querySelector(
            '[data-testid="deposit-proof-submit"]'
        );
        button.disabled = true;
        button.textContent = 'جارٍ الإرسال…';

        try {
            const form = new FormData();
            form.append('request_id', request.request_id);
            form.append('file', file);
            // initData header ONLY — never a JSON content type (the
            // browser must set the multipart boundary), never a
            // client-supplied user id.
            const headers = {};
            headers[INIT_DATA_HEADER] = _initData();
            const response = await fetch(PROOF_URL, {
                method: 'POST',
                headers: headers,
                body: form
            });
            const data = await response.json().catch(() => ({}));
            busy = false;
            if (!response.ok || !data.ok) {
                button.disabled = false;
                button.textContent = '📤 إرسال إثبات الدفع';
                _proofError(_messageFor(data));
                return;
            }
            _renderProofSent(data.message);
        } catch (err) {
            busy = false;
            if (button) {
                button.disabled = false;
                button.textContent = '📤 إرسال إثبات الدفع';
            }
            _proofError(_messageFor(null));
        }
    }

    /** The ONLY success state of an upload: review pending. */
    function _renderProofSent(message) {
        const section = overlay.querySelector(
            '[data-testid="deposit-proof-section"]'
        );
        if (section) {
            section.innerHTML =
                `<div class="withdrawal-success" ` +
                `data-testid="deposit-proof-sent">` +
                `${_esc(message || 'تم إرسال إثبات الدفع للمراجعة')}` +
                `</div>`;
        }
    }

    return { open, close };
})();
