/**
 * Task Stats — data source for the Home «المتاح» / «المكافآت» cards
 * ==================================================================
 * Reads the SAME endpoint the Tasks page reads: GET /api/tasks with
 * the verified Telegram initData header (same URL, same header, same
 * `ok === true` contract as miniapp/js/tasks.js), then derives the two
 * Home summary numbers from that ONE response, so the Home cards and
 * the task list can never disagree:
 *
 *   • availableCount — how many tasks the backend reports with
 *     status "available".  The backend alone decides availability;
 *     this module never re-decides it and never filters on anything
 *     else (started / completed rows are excluded by the server's
 *     own status field).
 *   • rewardUnits — the exact atomic (1e-8 USDT) sum of the rewards
 *     of THOSE SAME tasks.
 *
 * Rules (mirrors the Tasks page contract):
 * - identity comes only from TelegramApp.getInitData() — nothing is
 *   hard-coded here and nothing is ever sent back to the server
 * - no invented numbers: a failed / unauthenticated / malformed read
 *   resolves to `null`, so the caller keeps its neutral «—»
 *   placeholder instead of showing a guessed value
 * - reward values are never modified: `reward_units` (the backend's
 *   exact atomic accounting field) is used when present; only rows
 *   without it fall back to the whole-USDT `reward` field converted
 *   with integer math — never floating point
 */
const TaskStats = (() => {
    const LIST_URL = '/api/tasks';
    const INIT_DATA_HEADER = 'X-Telegram-Init-Data';

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

    /** Integer USDT units per USDT, from the shared WalletData source. */
    function _unitsPerUsdt() {
        const scale = (typeof WalletData !== 'undefined' && WalletData)
            ? WalletData.USDT_UNITS_PER_USDT
            : null;
        return (typeof scale === 'number' &&
            Number.isSafeInteger(scale) && scale > 0)
            ? scale
            : null;
    }

    /**
     * Exact atomic reward of one task row — integer math only.
     * `reward_units` is the backend's atomic accounting field; a row
     * without it is converted from the whole-USDT `reward` field.
     * Returns null when neither value is an exact safe integer, so
     * the caller reports the sum as unknown instead of guessing.
     */
    function _rewardUnitsOf(task) {
        const units = task.reward_units;
        if (typeof units === 'number' && Number.isSafeInteger(units)) {
            return units;
        }
        const scale = _unitsPerUsdt();
        const reward = task.reward;
        if (scale !== null &&
            typeof reward === 'number' && Number.isSafeInteger(reward)) {
            const converted = reward * scale;
            return Number.isSafeInteger(converted) ? converted : null;
        }
        return null;
    }

    /**
     * Fetch the Home summary from the backend.
     *
     * @returns {Promise<{availableCount: number,
     *                    rewardUnits: number|null}|null>}
     *   `null` when the backend did not confirm the read (network,
     *   auth or server error) — the UI then keeps its placeholder.
     *   `rewardUnits` is `null` while at least one available task's
     *   reward cannot be resolved exactly (a partial sum would be a
     *   wrong number); `availableCount` is always the exact count of
     *   tasks the backend reported as available.
     */
    async function load() {
        let data = null;
        let ok = false;
        try {
            const response = await fetch(LIST_URL, { headers: _headers() });
            data = await _parse(response);
            ok = response.ok && data && data.ok === true;
        } catch (error) {
            ok = false;
        }
        if (!ok || !Array.isArray(data.tasks)) {
            return null;
        }

        let availableCount = 0;
        let sum = 0;
        let sumExact = true;
        for (const task of data.tasks) {
            if (!task || task.status !== 'available') {
                // The backend did not mark this task available for
                // this user — it counts for neither number.
                continue;
            }
            availableCount += 1;
            const units = _rewardUnitsOf(task);
            if (units === null || !Number.isSafeInteger(sum + units)) {
                sumExact = false;
                continue;
            }
            sum += units;
        }
        return {
            availableCount: availableCount,
            rewardUnits: sumExact ? sum : null
        };
    }

    /**
     * Display string for the reward sum: the shared WalletData
     * formatter (exact integer math) plus the unchanged USDT currency
     * label. Returns null while there is no exact value to show, so
     * the card keeps its neutral placeholder.
     *
     * @param {number|null} units exact atomic USDT units
     * @returns {string|null} e.g. "3.50000000 USDT", or null
     */
    function formatRewards(units) {
        if (units === null || units === undefined ||
            typeof units !== 'number') {
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

    return { load, formatRewards };
})();
