/**
 * Social Account Linking (SA-YT-01)
 * ---------------------------------
 * Starts the Google OAuth flow for the authenticated Telegram Mini App
 * user and renders the linked-account state inside the existing Home
 * "ربط الحسابات" section.
 *
 * Boundaries:
 * - identity comes only from the verified Telegram initData header
 *   (TelegramApp.getInitData) — the page never supplies a user id
 * - the browser is navigated to the authorize URL produced by the
 *   backend; no OAuth secret, code or token ever reaches this page
 * - no rewards, tasks, wallet or balance logic lives here
 */
const SocialAccounts = (() => {
    const INIT_DATA_HEADER = 'X-Telegram-Init-Data';
    const CONNECT_URL = '/api/social/youtube/connect';
    const ACCOUNTS_URL = '/api/social/accounts';

    // Fixed messages for the one-shot #youtube=<outcome> return marker.
    const OUTCOME_MESSAGES = {
        linked: null, // success: refresh() renders the channel name
        denied: 'لم تكتمل خطوات الربط',
        invalid_state: 'انتهت صلاحية الجلسة، أعد المحاولة',
        invalid_request: 'طلب الربط غير صالح',
        oauth_failed: 'تعذر إتمام الربط',
        config_error: 'الربط غير متاح حالياً',
        channel_taken: 'هذه القناة مرتبطة بحساب آخر',
        storage_failed: 'تعذر حفظ الرابط'
    };

    let sectionEl = null;
    let linkedAccount = null;
    let pendingMessage = null;

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

    function _button() {
        return sectionEl
            ? sectionEl.querySelector('[data-testid="social-youtube-connect"]')
            : null;
    }

    function _stateEl() {
        return sectionEl
            ? sectionEl.querySelector('[data-testid="social-youtube-state"]')
            : null;
    }

    /**
     * Wire the section: connect button, one-shot return marker, and the
     * current linked state from the backend.
     */
    function attach(section) {
        sectionEl = section;
        const button = _button();
        if (button) {
            button.addEventListener('click', connectYouTube);
        }
        _consumeRedirectResult();
        refresh();
    }

    /** Navigate the browser into the Google OAuth flow. */
    async function connectYouTube() {
        _haptic();
        const button = _button();
        if (button) {
            button.disabled = true;
        }
        try {
            const response = await fetch(CONNECT_URL, {
                headers: _headers(),
                redirect: 'manual'
            });
            const data = await response.json();
            if (response.ok && data.authorize_url) {
                window.location.href = data.authorize_url;
                return;
            }
            pendingMessage = 'تعذر الربط، حاول لاحقاً';
            _renderState();
        } catch (error) {
            pendingMessage = 'تعذر الربط، حاول لاحقاً';
            _renderState();
        } finally {
            if (button) {
                button.disabled = false;
            }
        }
    }

    /** Load the caller's linked accounts and re-render the row. */
    async function refresh() {
        let accounts = [];
        try {
            const response = await fetch(ACCOUNTS_URL, {
                headers: _headers()
            });
            if (!response.ok) {
                return;
            }
            const data = await response.json();
            accounts = Array.isArray(data.accounts) ? data.accounts : [];
        } catch (error) {
            return;
        }
        linkedAccount = accounts.find((account) => account.provider === 'youtube') || null;
        if (linkedAccount) {
            pendingMessage = null;
        }
        _renderState();
    }

    function _renderState() {
        const stateEl = _stateEl();
        if (!stateEl) {
            return;
        }
        const button = _button();
        if (linkedAccount) {
            // YouTube + connected channel name/handle (never a token).
            stateEl.textContent = linkedAccount.display_name
                || linkedAccount.username
                || 'YouTube';
            stateEl.classList.add('is-linked');
            if (button) {
                button.hidden = true;
            }
            return;
        }
        stateEl.classList.remove('is-linked');
        stateEl.textContent = pendingMessage || 'غير مرتبط';
        if (button) {
            button.hidden = false;
        }
    }

    /**
     * Read the one-shot #youtube=<outcome> marker left by the OAuth
     * callback, then clear it so it can never replay.
     */
    function _consumeRedirectResult() {
        const match = /^#youtube=([a-z_]+)$/.exec(window.location.hash || '');
        if (!match) {
            return;
        }
        const outcome = match[1];
        if (window.history && window.history.replaceState) {
            window.history.replaceState(
                null,
                '',
                window.location.pathname + window.location.search
            );
        }
        if (outcome === 'linked') {
            pendingMessage = null;
            _haptic();
            return;
        }
        pendingMessage = OUTCOME_MESSAGES[outcome] || 'تعذر إتمام الربط';
    }

    return {
        attach,
        connectYouTube,
        refresh
    };
})();
