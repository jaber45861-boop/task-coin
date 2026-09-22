/**
 * Wallet Page Component (MT-UI-03)
 * Renders the المحفظة page/state opened from the Home wallet icon.
 *
 * Structure follows the approved design exactly:
 *   header (back ‹ / title / wallet icon)
 *   current-balance card (رصيدك الحالي)
 *   two large actions (السحب RED ↑ / الإيداع GREEN ↓) + captions
 *   transaction history card (سجل المعاملات) + عرض كل المعاملات
 *
 * UI-only task: no accounting, no deposits/withdrawals, no ledger
 * mutations, no exchange-rate fetching, no financial calculations.
 * Balance and history come from the shared WalletData binding, which
 * returns neutral "no data" values until a real backend read API exists.
 */

/**
 * Shared wallet icon markup (inline SVG — no generated image assets).
 * Reused by the Home circular wallet button and the Wallet header.
 */
const WalletIcon = `
    <svg class="wallet-icon-svg" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
        <circle cx="11" cy="8" r="4" fill="#ffd166"/>
        <circle cx="17" cy="6.5" r="3.2" fill="#ffc233"/>
        <rect x="4" y="10" width="24" height="16" rx="4" fill="#ff3d2e" stroke="#ff8a7a" stroke-width="1"/>
        <path d="M7 10 V8 a3 3 0 0 1 3-3 h12 a3 3 0 0 1 3 3 v2"
              fill="none" stroke="#ff8a7a" stroke-width="1.5"/>
        <rect x="18" y="15" width="11" height="7" rx="3.5" fill="#8e1a10"/>
        <circle cx="22" cy="18.5" r="1.8" fill="#ffe082"/>
    </svg>`;

const Wallet = (() => {

    /**
     * Build and return the full Wallet page DOM element.
     */
    function render() {
        const page = document.createElement('div');
        page.className = 'page page-wallet';
        page.setAttribute('data-testid', 'wallet-page');

        page.appendChild(_buildHeader());
        page.appendChild(_buildBalanceCard());
        page.appendChild(_buildActions());
        page.appendChild(_buildHistory());

        return page;
    }

    /* ── Shared bits ───────────────────────────────────────────── */

    /** Existing project haptic helper pattern (same as navigation.js). */
    function _haptic() {
        if (window.Telegram?.WebApp?.HapticFeedback) {
            window.Telegram.WebApp.HapticFeedback.impactOccurred('light');
        }
    }

    /* ── Wallet header: back ‹ | title | wallet icon ───────────── */

    function _buildHeader() {
        const header = document.createElement('header');
        header.className = 'wallet-header';
        header.setAttribute('data-testid', 'wallet-header');

        header.innerHTML = `
            <button type="button" class="wallet-back" data-testid="wallet-back"
                    aria-label="الرجوع إلى الرئيسية">
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                    <path d="M15 5 L8 12 L15 19" fill="none" stroke="currentColor"
                          stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            </button>
            <h2 class="wallet-title" data-testid="wallet-title">المحفظة</h2>
            <span class="wallet-header-icon" aria-hidden="true">${WalletIcon}</span>
        `;

        const backBtn = header.querySelector('[data-testid="wallet-back"]');
        backBtn.addEventListener('click', () => {
            _haptic();
            // Real navigation state via the existing router — returns Home.
            Navigation.navigateTo('home');
        });

        return header;
    }

    /* ── Current balance card (رصيدك الحالي) ───────────────────── */

    function _buildBalanceCard() {
        const section = document.createElement('section');
        section.className = 'wallet-balance-card';
        section.setAttribute('data-testid', 'wallet-balance-card');

        const balance = WalletData.getBalance();
        const usdt = WalletData.formatUsdt(balance.availableUnits);
        const egp = WalletData.formatEgp(balance.egpDisplayMinor);

        section.innerHTML = `
            <span class="wallet-balance-title">رصيدك الحالي</span>
            <span class="wallet-balance-amount" data-testid="wallet-balance-amount">
                <span class="wallet-balance-value">${usdt}</span>
                <span class="wallet-balance-currency">USDT</span>
            </span>
            <span class="wallet-balance-egp" data-testid="wallet-balance-egp">≈ ${egp} EGP</span>
        `;
        return section;
    }

    /* ── Withdraw / Deposit actions (inside the Wallet page) ───── */

    function _buildActions() {
        const section = document.createElement('section');
        section.className = 'wallet-actions';
        section.setAttribute('data-testid', 'wallet-actions');

        // RTL grid: first child renders on the right (الإيداع GREEN),
        // second child renders on the left (السحب RED) — per the design.
        section.innerHTML = `
            <div class="wallet-action-col">
                <button type="button" class="wallet-action wallet-action-deposit"
                        data-testid="wallet-deposit" data-action="deposit">
                    <span class="btn-arrow" aria-hidden="true">⬇</span>
                    <span class="wallet-action-label">الإيداع</span>
                </button>
                <span class="wallet-action-caption">شحن رصيدك</span>
            </div>
            <div class="wallet-action-col">
                <button type="button" class="wallet-action wallet-action-withdraw"
                        data-testid="wallet-withdraw" data-action="withdraw">
                    <span class="btn-arrow" aria-hidden="true">⬆</span>
                    <span class="wallet-action-label">السحب</span>
                </button>
                <span class="wallet-action-caption">تحويل إلى محفظتك</span>
            </div>
        `;

        // UI-only in MT-UI-03: haptic acknowledgement, no financial logic.
        section.querySelectorAll('.wallet-action').forEach((btn) => {
            btn.addEventListener('click', _haptic);
        });

        return section;
    }

    /* ── Transaction history (سجل المعاملات) ───────────────────── */

    function _buildHistory() {
        const section = document.createElement('section');
        section.className = 'wallet-history';
        section.setAttribute('data-testid', 'wallet-history');

        section.innerHTML = `
            <div class="wallet-history-header">
                <span class="section-icon">🕘</span>
                <span class="section-title">سجل المعاملات</span>
            </div>
            <div class="wallet-history-list" data-testid="wallet-history-list"></div>
            <button type="button" class="wallet-history-more"
                    data-testid="wallet-history-more">
                <span class="wallet-history-more-icon" aria-hidden="true">
                    <svg viewBox="0 0 24 24" focusable="false">
                        <rect x="3" y="4" width="18" height="16" rx="3"
                              fill="none" stroke="currentColor" stroke-width="2"/>
                        <path d="M7 9h10M7 13h10M7 17h6" fill="none"
                              stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                    </svg>
                </span>
                <span class="wallet-history-more-label">عرض كل المعاملات</span>
                <span class="wallet-history-chevron" aria-hidden="true">
                    <svg viewBox="0 0 24 24" focusable="false">
                        <path d="M14 6 L8 12 L14 18" fill="none" stroke="currentColor"
                              stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                </span>
            </button>
        `;

        const list = section.querySelector('[data-testid="wallet-history-list"]');
        const transactions = WalletData.getTransactions();

        if (transactions.length === 0) {
            // Correct future-ready empty state — never fake history.
            const empty = document.createElement('span');
            empty.className = 'wallet-history-empty';
            empty.setAttribute('data-testid', 'wallet-history-empty');
            empty.textContent = 'لا توجد معاملات بعد';
            list.appendChild(empty);
        } else {
            transactions.forEach((tx) => list.appendChild(_renderRow(tx)));
        }

        // UI-only in MT-UI-03: no transactions screen exists yet.
        section.querySelector('[data-testid="wallet-history-more"]')
            .addEventListener('click', _haptic);

        return section;
    }

    /**
     * Render one backend-provided transaction row (future-ready).
     * All formatting goes through WalletData — no local math, no floats.
     */
    function _renderRow(tx) {
        const isWithdraw = tx.direction === 'withdraw';
        const sign = isWithdraw ? '-' : '+';
        const amount = sign + WalletData.formatUsdt(Math.abs(tx.amountUnits)) + ' USDT';

        const row = document.createElement('div');
        row.className = 'wallet-tx-row';
        row.setAttribute('data-testid', 'wallet-tx-row');

        const typeLabel = isWithdraw ? 'سحب' : 'إيداع';
        const dirClass = isWithdraw ? 'withdraw' : 'deposit';

        row.innerHTML = `
            <span class="wallet-tx-icon wallet-tx-icon--${dirClass}" aria-hidden="true">
                <span class="wallet-tx-glyph" aria-hidden="true">${isWithdraw ? '⬆' : '⬇'}</span>
            </span>
            <span class="wallet-tx-info">
                <span class="wallet-tx-type" dir="auto">${typeLabel}</span>
                <span class="wallet-tx-date" dir="auto">${tx.createdAt}</span>
            </span>
            <span class="wallet-tx-figures">
                <span class="wallet-tx-amount wallet-tx-amount--${dirClass}"
                      dir="ltr">${amount}</span>
                <span class="wallet-tx-status">مكتمل</span>
            </span>
            <span class="wallet-tx-chevron" aria-hidden="true">
                <svg viewBox="0 0 24 24" focusable="false">
                    <path d="M9 5 L16 12 L9 19" fill="none" stroke="currentColor"
                          stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            </span>
        `;
        return row;
    }

    return { render };
})();
