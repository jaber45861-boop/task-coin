/**
 * Wallet Data Source (MT-UI-03 — UI binding only)
 * ================================================
 * Single source of truth for the balance and transaction list shown by
 * BOTH the Home profile area and the Wallet page, so the two screens can
 * never contradict each other.
 *
 * Balance comes from GET /api/me (same auth pattern as profile.js).
 * All formatting is integer-unit arithmetic only (no floats, no
 * financial calculations):
 *     1 USDT = 100,000,000 wallet units   (integer)
 *     1 EGP  = 100 minor units            (integer)
 * The EGP figure is a display equivalent provided by the backend;
 * this module never converts currencies on its own.
 */
const WalletData = (() => {
    const USDT_UNITS_PER_USDT = 100000000; // integer wallet units per USDT
    const EGP_MINOR_PER_EGP = 100;         // integer minor units per EGP
    const EGP_DECIMALS = 2;
    const USDT_DECIMALS = 8;

    const ACCOUNT_URL = '/api/me';
    const INIT_DATA_HEADER = 'X-Telegram-Init-Data';

    /**
     * Build the auth header from verified Telegram initData — same
     * source the Profile page uses.
     */
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

    /**
     * Current wallet balance for the signed-in user, fetched from
     * GET /api/me.  While the request is in flight the balance is
     * `null` (UI renders an em-dash).  On any failure the balance
     * stays `null` — never an invented amount.
     *
     * @returns {Promise<{availableUnits: number|null, egpDisplayMinor: number|null}>}
     *   `availableUnits` is integer USDT units or null.
     *   `egpDisplayMinor` is integer EGP minor units or null.
     */
    async function getBalance() {
        let data = null;
        let ok = false;
        try {
            const response = await fetch(ACCOUNT_URL, { headers: _headers() });
            data = await response.json();
            ok = response.ok && !!data && data.ok === true;
        } catch (error) {
            ok = false;
        }
        if (!ok || !data || !data.wallet) {
            return {
                availableUnits: null,
                egpDisplayMinor: null
            };
        }

        const wallet = data.wallet;
        // The backend returns null when the user has no wallet row,
        // or {availableUnits: <int>} when a wallet exists (possibly
        // with a zero balance).  Both are passed through verbatim so
        // the UI can distinguish "no wallet" from "zero balance".
        if (wallet.availableUnits === null || wallet.availableUnits === undefined) {
            return {
                availableUnits: null,
                egpDisplayMinor: null
            };
        }

        // Integer USDT units → EGP display minor units from the
        // backend-provided display equivalent.  If the backend did
        // not send one, leave it null — never invent a conversion.
        const egpMinor = (data.wallet.egpDisplayMinor !== undefined)
            ? data.wallet.egpDisplayMinor
            : null;

        return {
            availableUnits: wallet.availableUnits,
            egpDisplayMinor: egpMinor
        };
    }

    /**
     * Transaction history for the signed-in user.
     *
     * @returns {Array} empty while no backend transaction API exists.
     *   A future row shape (provided by the backend, never invented here):
     *   { direction: 'withdraw'|'deposit', amountUnits: number,
     *     createdAt: 'YYYY-MM-DD HH:MM', status: 'completed'|... }
     */
    function getTransactions() {
        return [];
    }

    /**
     * Format integer wallet units as an 8-decimal USDT string.
     * Integer-exact for every safe-integer unit count; null → '—'.
     *
     * @param {number|null} units integer USDT units (may be negative)
     * @returns {string} an 8-decimal string (whole.frac8), or "—" while
     *   the balance is unknown
     */
    function formatUsdt(units) {
        if (units === null || units === undefined) {
            return '—';
        }
        const negative = units < 0;
        const abs = Math.abs(units);
        // Math.floor / % on safe integers are exact — no float rounding.
        const whole = Math.floor(abs / USDT_UNITS_PER_USDT);
        const frac = String(abs % USDT_UNITS_PER_USDT).padStart(USDT_DECIMALS, '0');
        return (negative ? '-' : '') + whole + '.' + frac;
    }

    /**
     * Format integer minor EGP units as a 2-decimal EGP display string.
     * Integer-exact for every safe-integer minor-unit count; null → '—'.
     *
     * @param {number|null} minorUnits integer EGP minor units
     * @returns {string} a 2-decimal string (whole.frac2), or "—" while
     *   the display equivalent is unknown
     */
    function formatEgpMinor(minorUnits) {
        if (minorUnits === null || minorUnits === undefined) {
            return '—';
        }
        const negative = minorUnits < 0;
        const abs = Math.abs(minorUnits);
        const whole = Math.floor(abs / EGP_MINOR_PER_EGP);
        const frac = String(abs % EGP_MINOR_PER_EGP).padStart(EGP_DECIMALS, '0');
        return (negative ? '-' : '') + whole + '.' + frac;
    }

    /**
     * Format integer minor units as a 2-decimal EGP display string.
     * The value arrives from the backend as a display equivalent — this
     * function only renders it; it never converts currencies.
     *
     * @param {number|null} minorUnits integer EGP minor units
     * @returns {string} a 2-decimal string (whole.frac2), or "—" while
     *   the display equivalent is unknown
     */
    function formatEgp(minorUnits) {
        if (minorUnits === null || minorUnits === undefined) {
            return '—';
        }
        const negative = minorUnits < 0;
        const abs = Math.abs(minorUnits);
        const whole = Math.floor(abs / EGP_MINOR_PER_EGP);
        const frac = String(abs % EGP_MINOR_PER_EGP).padStart(EGP_DECIMALS, '0');
        return (negative ? '-' : '') + whole + '.' + frac;
    }

    return {
        getBalance,
        getTransactions,
        formatUsdt,
        formatEgp,
        formatEgpMinor,
        USDT_UNITS_PER_USDT,
        EGP_MINOR_PER_EGP
    };
})();
