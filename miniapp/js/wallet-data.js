/**
 * Wallet Data Source (MT-UI-03 — UI binding only)
 * ================================================
 * Single source of truth for the balance and transaction list shown by
 * BOTH the Home profile area and the Wallet page, so the two screens can
 * never contradict each other.
 *
 * The Mini App backend does not expose a wallet API yet, so every read
 * returns a neutral "no data" value — never fake money, never fake
 * history.  When a real read API arrives, only this module changes.
 *
 * All formatting is integer-unit arithmetic only (no floats, no
 * financial calculations):
 *     1 USDT = 100,000,000 wallet units   (integer)
 *     1 EGP  = 100 minor units            (integer)
 * The EGP figure is a display equivalent provided by the backend later;
 * this module never converts currencies on its own.
 */
const WalletData = (() => {
    const USDT_UNITS_PER_USDT = 100000000; // integer wallet units per USDT
    const EGP_MINOR_PER_EGP = 100;         // integer minor units per EGP
    const EGP_DECIMALS = 2;
    const USDT_DECIMALS = 8;

    /**
     * Current wallet balance for the signed-in user.
     *
     * @returns {{availableUnits: number|null, egpDisplayMinor: number|null}}
     *   `null` everywhere while no backend wallet API exists — the UI
     *   renders an em-dash placeholder instead of an invented amount.
     */
    function getBalance() {
        return {
            availableUnits: null,      // integer USDT units, null = unknown
            egpDisplayMinor: null      // integer EGP minor units, null = unknown
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
        USDT_UNITS_PER_USDT,
        EGP_MINOR_PER_EGP
    };
})();
