/**
 * Account Page Component
 * Renders the «حسابي» page with the caller's own data —
 * and only their own data:
 *
 *   • identity (name, username, photo, Telegram id) from the
 *     SAME source Home's welcome card reads (TelegramApp.getUser())
 *   • account figures (available balance, lifetime earnings,
 *     completed / in-progress tasks) from GET /api/me — the
 *     read-only account summary the backend serves to the
 *     verified initData user
 *
 * Nothing is invented: every figure ships as a neutral «—»
 * placeholder and is replaced only by a value the backend
 * confirmed; a failed, errored or malformed read keeps the
 * placeholder.  The backend defines no XP or progression
 * tier, so none is ever shown.
 */
const Profile = (() => {
    const ACCOUNT_URL = '/api/me';
    const INIT_DATA_HEADER = 'X-Telegram-Init-Data';

    /**
     * Build and return the full Account page DOM element.
     */
    function render() {
        const page = document.createElement('div');
        page.className = 'page page-profile';
        page.setAttribute('data-testid', 'profile-page');

        // Static skeleton only: every user-controlled value
        // is filled below through textContent/setAttribute,
        // never interpolated into markup.
        page.innerHTML = `
            <div class="page-header">
                <h2>حسابي</h2>
            </div>
            <div class="page-content">
                <section class="profile-section" data-testid="profile-identity">
                    <div class="welcome-card">
                        <div class="welcome-avatar" data-testid="profile-avatar"></div>
                        <div class="welcome-info">
                            <span class="welcome-name" data-testid="profile-name">—</span>
                            <span class="welcome-username" data-testid="profile-username" hidden></span>
                        </div>
                    </div>
                </section>
                <section class="profile-section" data-testid="profile-account">
                    <div class="profile-field">
                        <span class="profile-field-label">الرصيد المتاح</span>
                        <span class="profile-field-value profile-money" data-testid="profile-balance">—</span>
                    </div>
                    <div class="profile-field">
                        <span class="profile-field-label">إجمالي الأرباح</span>
                        <span class="profile-field-value profile-money" data-testid="profile-earnings">—</span>
                    </div>
                    <div class="profile-field">
                        <span class="profile-field-label">المهام المكتملة</span>
                        <span class="profile-field-value" data-testid="profile-completed">—</span>
                    </div>
                    <div class="profile-field">
                        <span class="profile-field-label">قيد التنفيذ</span>
                        <span class="profile-field-value" data-testid="profile-progress">—</span>
                    </div>
                </section>
                <section class="profile-section" data-testid="profile-details">
                    <div class="profile-field">
                        <span class="profile-field-label">المعرّف</span>
                        <span class="profile-field-value" data-testid="profile-id">—</span>
                    </div>
                </section>
                <button type="button" class="review-entry" data-goto="review" data-testid="review-entry" hidden>مراجعة الإثباتات</button>
            </div>
        `;

        _fillIdentity(page);
        _loadAccount(page);

        return page;
    }

    /* ── Identity (Telegram user) ────────────────────────── */

    /**
     * Fill the identity card from the Telegram user object —
     * the same extraction convention Home's welcome card uses.
     */
    function _fillIdentity(page) {
        const user = typeof TelegramApp !== 'undefined'
            ? TelegramApp.getUser()
            : null;

        const firstName = user?.first_name || null;
        const lastName = user?.last_name || null;
        const username = user?.username || null;
        const photoUrl = user?.photo_url || null;
        const userId = user?.id ?? null;

        // Avatar: the Telegram photo when it is served over
        // https, otherwise the neutral placeholder — the same
        // avatar pattern Home uses.
        const avatar = page.querySelector('[data-testid="profile-avatar"]');
        if (avatar) {
            if (photoUrl && photoUrl.startsWith('https://')) {
                const img = document.createElement('img');
                img.className = 'avatar-img';
                img.setAttribute('data-testid', 'profile-avatar-img');
                img.alt = '';
                img.src = photoUrl;
                avatar.appendChild(img);
            } else {
                avatar.innerHTML = '<span class="avatar-placeholder">👤</span>';
            }
        }

        // Display name: first_name plus last_name when Telegram
        // provides them; the neutral dash otherwise.
        const name = page.querySelector('[data-testid="profile-name"]');
        if (name) {
            name.textContent = [firstName, lastName]
                .filter(Boolean)
                .join(' ') || '—';
        }

        // Username: the row ships hidden and is revealed only
        // when Telegram actually provides a username.
        const usernameEl = page.querySelector('[data-testid="profile-username"]');
        if (usernameEl) {
            if (username) {
                usernameEl.textContent = `@${username}`;
                usernameEl.hidden = false;
            } else {
                usernameEl.hidden = true;
            }
        }

        // Telegram user id — the account's stable identifier.
        const id = page.querySelector('[data-testid="profile-id"]');
        if (id) {
            id.textContent = userId === null ? '—' : String(userId);
        }
    }

    /* ── Account figures (GET /api/me) ───────────────────── */

    /** Verified Telegram initData — same source the Tasks page uses. */
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

    async function _parse(response) {
        try {
            return await response.json();
        } catch (error) {
            return null;
        }
    }

    /**
     * Exact atomic USDT display for a units value — the shared
     * WalletData formatter (integer math only) plus the currency
     * label.  Returns null while there is no exact value to show,
     * so the row keeps its neutral placeholder.
     */
    function _formatUsdt(units) {
        if (typeof units !== 'number' ||
            !Number.isSafeInteger(units) || units < 0) {
            return null;
        }
        if (typeof WalletData === 'undefined' || !WalletData ||
            typeof WalletData.formatUsdt !== 'function') {
            return null;
        }
        const text = WalletData.formatUsdt(units);
        if (!text || text === '—') {
            return null;
        }
        return text + ' USDT';
    }

    /** A confirmed non-negative safe-integer count, or null. */
    function _count(value) {
        return (typeof value === 'number' &&
            Number.isSafeInteger(value) && value >= 0)
            ? String(value)
            : null;
    }

    /** Replace one placeholder — only with a confirmed value. */
    function _setText(page, testid, text) {
        const el = page.querySelector(`[data-testid="${testid}"]`);
        if (el && text !== null) {
            el.textContent = text;
        }
    }

    /**
     * Fetch the caller's own account summary.  Every figure is
     * replaced only when the backend confirmed the read AND the
     * value is an exact safe integer; anything else keeps the
     * «—» placeholder — an unknown figure is never guessed.
     */
    async function _loadAccount(page) {
        let data = null;
        let ok = false;
        try {
            const response = await fetch(ACCOUNT_URL, { headers: _headers() });
            data = await _parse(response);
            ok = response.ok && !!data && data.ok === true;
        } catch (error) {
            ok = false;
        }
        if (!ok) {
            return;
        }

        const wallet = data.wallet || {};
        const stats = data.stats || {};

        _setText(page, 'profile-balance',
            _formatUsdt(wallet.availableUnits));
        _setText(page, 'profile-earnings',
            _formatUsdt(stats.earnedUnits));
        _setText(page, 'profile-completed',
            _count(stats.completedTasks));
        _setText(page, 'profile-progress',
            _count(stats.inProgressTasks));
    }

    return { render };
})();
