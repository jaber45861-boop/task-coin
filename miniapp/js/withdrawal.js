/**
 * Withdrawal Form (MT-ADMIN-25)
 * =============================
 * Opens from the Wallet page السحب button and talks to the production
 * withdrawal API — no financial logic lives here or in wallet.js.
 *
 * Boundaries (mirrors tasks.js):
 * - identity comes only from the verified Telegram initData header
 *   (TelegramApp.getInitData); this file never supplies a user id
 * - GET  /api/withdrawal/methods → active payout methods (server-safe
 *   metadata only; the platform's destination is never returned)
 * - POST /api/withdrawal        → {payment_method_id, amount, user_
 *   destination}; the amount is sent as the RAW text the admin's user
 *   typed — this file never parses it into a number, never converts a
 *   currency and never computes a fee: the server owns every rule and
 *   replies with the authoritative result (including the pinned rate)
 * - server Arabic messages (with stable-code fallbacks) decide every
 *   error state; no request payload is ever logged
 */
const WithdrawalUI = (() => {
    const METHODS_URL = '/api/withdrawal/methods';
    const CREATE_URL = '/api/withdrawal';
    const INIT_DATA_HEADER = 'X-Telegram-Init-Data';

    // Arabic fallbacks keyed by the backend's stable error codes.
    // The server-provided Arabic `message` always wins when present.
    const ERROR_MESSAGES = {
        unauthenticated: 'افتح التطبيق من تيليجرام أولاً',
        invalid_request: 'الطلب غير صالح',
        invalid_amount: 'المبلغ غير صالح',
        unsupported_method: 'طريقة السحب غير مدعومة لهذه الوسيلة',
        payment_method_not_found: 'وسيلة الدفع غير موجودة',
        payment_method_unavailable: 'وسيلة الدفع غير متاحة حالياً',
        rate_unavailable: 'سعر الصرف غير متاح حالياً، حاول لاحقاً',
        withdrawal_settings_missing: 'إعدادات السحب غير مكتملة، تواصل مع الإدارة',
        cooldown: 'تم إنشاء طلب سحب خلال آخر 24 ساعة، حاول لاحقاً',
        pending_exists: 'لديك طلب سحب قيد المراجعة بالفعل',
        insufficient_balance: 'رصيدك لا يكفي لإتمام هذا السحب',
        below_minimum: 'المبلغ أقل من الحد الأدنى للسحب',
        server_error: 'حدث خطأ غير متوقع، حاول مرة أخرى',
        network: 'تعذر الاتصال بالخادم، حاول مرة أخرى'
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

    function _unitFor(method) {
        return method.category === 'cash' ? 'EGP' : (method.asset || 'USDT');
    }

    function _destinationLabel(method) {
        return method.category === 'cash'
            ? 'رقم فودافون كاش المستلم'
            : 'عنوان المحفظة (شبكة BEP-20)';
    }

    /* ── Overlay shell ──────────────────────────────────────────── */

    function _buildShell() {
        const el = document.createElement('div');
        el.className = 'withdrawal-overlay';
        el.setAttribute('data-testid', 'withdrawal-overlay');
        return el;
    }

    /**
     * Open the withdrawal form (called by the Wallet السحب button).
     * Methods are loaded fresh from the API on every open so an
     * deactivated payout method can never be offered.
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
                _renderMessage('لا توجد وسائل دفع متاحة حالياً');
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
                 aria-label="سحب الرصيد">
                <div class="withdrawal-head">
                    <h3 class="withdrawal-title">سحب الرصيد</h3>
                    <button type="button" class="withdrawal-close"
                            data-testid="withdrawal-close"
                            aria-label="إغلاق">✕</button>
                </div>
                <div class="withdrawal-status" data-testid="withdrawal-loading">
                    جارٍ التحميل…
                </div>
            </div>`;
        _wireClose();
    }

    function _renderMessage(message) {
        overlay.innerHTML = `
            <div class="withdrawal-card" role="dialog" aria-modal="true"
                 aria-label="سحب الرصيد">
                <div class="withdrawal-head">
                    <h3 class="withdrawal-title">سحب الرصيد</h3>
                    <button type="button" class="withdrawal-close"
                            data-testid="withdrawal-close"
                            aria-label="إغلاق">✕</button>
                </div>
                <div class="withdrawal-error" data-testid="withdrawal-error">
                    ${_esc(message)}
                </div>
            </div>`;
        _wireClose();
    }

    function _wireClose() {
        const closeBtn = overlay.querySelector(
            '[data-testid="withdrawal-close"]'
        );
        if (closeBtn) {
            closeBtn.addEventListener('click', close);
        }
    }

    /* ── Form ───────────────────────────────────────────────────── */

    function _renderForm() {
        const options = methodsCache.map((m) => {
            const unit = _unitFor(m);
            const network = m.network ? ` · ${m.network}` : '';
            const label = `${m.display_name} (${unit}${network})`;
            return `<option value="${_esc(m.id)}">${_esc(label)}</option>`;
        }).join('');

        const first = methodsCache[0];
        overlay.innerHTML = `
            <div class="withdrawal-card" role="dialog" aria-modal="true"
                 aria-label="سحب الرصيد">
                <div class="withdrawal-head">
                    <h3 class="withdrawal-title">سحب الرصيد</h3>
                    <button type="button" class="withdrawal-close"
                            data-testid="withdrawal-close"
                            aria-label="إغلاق">✕</button>
                </div>
                <label class="withdrawal-field">
                    <span class="withdrawal-label">وسيلة السحب</span>
                    <select class="withdrawal-select"
                            data-testid="withdrawal-method">
                        ${options}
                    </select>
                </label>
                <label class="withdrawal-field">
                    <span class="withdrawal-label">
                        المبلغ (<span data-testid="withdrawal-unit"
                            >${_esc(_unitFor(first))}</span>)
                    </span>
                    <input class="withdrawal-input" type="text"
                           inputmode="decimal" autocomplete="off"
                           data-testid="withdrawal-amount"
                           placeholder="0.00">
                </label>
                <label class="withdrawal-field">
                    <span class="withdrawal-label"
                          data-testid="withdrawal-destination-label"
                          >${_esc(_destinationLabel(first))}</span>
                    <input class="withdrawal-input" type="text"
                           autocomplete="off"
                           data-testid="withdrawal-destination"
                           placeholder="">
                </label>
                <div class="withdrawal-error" data-testid="withdrawal-form-error"
                     hidden></div>
                <button type="button" class="withdrawal-submit"
                        data-testid="withdrawal-submit">تأكيد السحب</button>
            </div>`;
        _wireClose();

        const select = overlay.querySelector(
            '[data-testid="withdrawal-method"]'
        );
        select.addEventListener('change', () => {
            const method = _selectedMethod();
            overlay.querySelector('[data-testid="withdrawal-unit"]')
                .textContent = _unitFor(method);
            overlay.querySelector(
                '[data-testid="withdrawal-destination-label"]'
            ).textContent = _destinationLabel(method);
        });

        overlay.querySelector('[data-testid="withdrawal-submit"]')
            .addEventListener('click', _submit);
    }

    function _selectedMethod() {
        const select = overlay.querySelector(
            '[data-testid="withdrawal-method"]'
        );
        const id = Number(select.value);
        return methodsCache.find((m) => m.id === id) || methodsCache[0];
    }

    function _formError(message) {
        const box = overlay.querySelector(
            '[data-testid="withdrawal-form-error"]'
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
            '[data-testid="withdrawal-amount"]'
        ).value.trim();
        const destination = overlay.querySelector(
            '[data-testid="withdrawal-destination"]'
        ).value.trim();

        if (!amount || !destination) {
            _formError('أدخل المبلغ وبيانات الاستلام');
            return;
        }

        busy = true;
        _formError('');
        const submitBtn = overlay.querySelector(
            '[data-testid="withdrawal-submit"]'
        );
        submitBtn.disabled = true;
        submitBtn.textContent = 'جارٍ الإرسال…';

        try {
            const response = await fetch(CREATE_URL, {
                method: 'POST',
                headers: _headers(),
                body: JSON.stringify({
                    payment_method_id: method.id,
                    amount: amount,
                    user_destination: destination
                })
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok || !data.ok) {
                busy = false;
                submitBtn.disabled = false;
                submitBtn.textContent = 'تأكيد السحب';
                _formError(_messageFor(data));
                return;
            }
            _renderSuccess(data.request, data.message);
        } catch (err) {
            busy = false;
            submitBtn.disabled = false;
            submitBtn.textContent = 'تأكيد السحب';
            _formError(_messageFor(null));
        }
    }

    /* ── Success ────────────────────────────────────────────────── */

    function _renderSuccess(request, message) {
        const unit = _esc(request.native_unit);
        overlay.innerHTML = `
            <div class="withdrawal-card" role="dialog" aria-modal="true"
                 aria-label="سحب الرصيد">
                <div class="withdrawal-success" data-testid="withdrawal-success">
                    ✅ ${_esc(message || 'تم إنشاء طلب السحب بنجاح')}
                </div>
                <dl class="withdrawal-receipt"
                    data-testid="withdrawal-receipt">
                    <div><dt>المبلغ</dt><dd dir="ltr">${_esc(request.amount)}
                        ${unit}</dd></div>
                    <div><dt>رسوم المعالجة</dt><dd dir="ltr">${_esc(request.fee)}
                        ${unit}</dd></div>
                    <div><dt>الحالة</dt><dd>${_esc(request.status)}</dd></div>
                    <div><dt>وسيلة السحب</dt><dd>${_esc(request.display_name)}
                    </dd></div>
                    <div><dt>رقم الطلب</dt><dd dir="ltr"
                        >${_esc(request.request_id)}</dd></div>
                </dl>
                <button type="button" class="withdrawal-submit"
                        data-testid="withdrawal-done">تم</button>
            </div>`;
        const done = overlay.querySelector(
            '[data-testid="withdrawal-done"]'
        );
        done.addEventListener('click', close);
    }

    return { open, close };
})();
