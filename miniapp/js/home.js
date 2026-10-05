/**
 * Home Page Component
 * Renders the الرئيسية dashboard with structural UI sections.
 * Sections use neutral/placeholder states or real backend data —
 * never mock data, never an invented number.  The balance cards
 * («المتاح» / «المكافآت») are filled from the SAME /api/tasks read
 * the Tasks page uses, through the task-stats.js module.
 */
const Home = (() => {

    // ── Feature flag: Account Linking section ─────────────────────
    // SHOW_ACCOUNT_LINKING controls whether the «ربط الحسابات»
    // section (YouTube card + «ربط YouTube» button) is rendered on
    // Home.  The section markup and its social.js linking
    // functionality are fully intact — this flag only decides
    // visibility.  When false the section element is never created,
    // so no empty gap or container is left behind.  Set it to `true`
    // to restore the section exactly as it is today.
    const SHOW_ACCOUNT_LINKING = false;

    // ── Feature flag: Daily Registration section ─────────────────
    // SHOW_DAILY_REGISTRATION controls whether the «التسجيل اليومي»
    // section (📅 daily check-in card) is rendered on Home.  The
    // section builder (_buildDailyCheckinSection), its markup and
    // its CSS are fully intact — this flag only decides visibility.
    // When false the section element is never created, so no empty
    // gap, placeholder or container is left behind.  Set it to
    // `true` to restore the section exactly as it is today.
    const SHOW_DAILY_REGISTRATION = false;

    // ── Feature flag: user task submission («إضافة مهمة ➕») ──────
    // SHOW_USER_TASK_SUBMISSION wires the «إضافة مهمة» CTA to the
    // TaskRequestUI dialog (miniapp/js/task-request.js): the user
    // proposes a task and it ALWAYS enters admin review first — the
    // Mini App can never publish a task by itself.  The section
    // itself stays unconditional; this flag only decides whether the
    // button opens the form or stays a disabled placeholder (set it
    // to `false` to hide the capability without touching markup).
    const SHOW_USER_TASK_SUBMISSION = true;

    /**
     * Build and return the full Home page DOM element.
     */
    function render() {
        const page = document.createElement('div');
        page.className = 'page page-home';
        page.setAttribute('data-testid', 'home-page');

        page.appendChild(_buildWelcomeSection());
        page.appendChild(_buildBalanceSection());
        if (SHOW_DAILY_REGISTRATION) {
            page.appendChild(_buildDailyCheckinSection());
        }
        page.appendChild(_buildOfficialGuideSection());
        page.appendChild(_buildAddTaskSection());
        if (SHOW_ACCOUNT_LINKING) {
            page.appendChild(_buildAccountLinkingSection());
        }
        page.appendChild(_buildHotTasksSection());

        return page;
    }

    /* ── Welcome / Profile Summary ────────────────────────────── */

    function _buildWelcomeSection() {
        const section = document.createElement('section');
        section.className = 'home-section home-welcome';
        section.setAttribute('data-testid', 'home-welcome');

        // Get Telegram user data if available
        const user = typeof TelegramApp !== 'undefined' ? TelegramApp.getUser() : null;

        // Wallet balance shown beside the profile (same shared data
        // source as the Wallet page — neutral placeholder while no
        // backend wallet API exists; never a hard-coded amount).
        const balance = typeof WalletData !== 'undefined'
            ? WalletData.getBalance()
            : { availableUnits: null, egpDisplayMinor: null };
        const usdtText = typeof WalletData !== 'undefined'
            ? WalletData.formatUsdt(balance.availableUnits)
            : '—';
        const egpText = typeof WalletData !== 'undefined'
            ? WalletData.formatEgp(balance.egpDisplayMinor)
            : '—';

        // Shared inline-SVG wallet icon (defined by wallet.js).
        const walletIcon = typeof WalletIcon !== 'undefined' ? WalletIcon : '👛';
        const firstName = user?.first_name || null;
        const username = user?.username || null;
        const photoUrl = user?.photo_url || null;

        // Build avatar: use photo if available, otherwise placeholder
        let avatarHtml;
        if (photoUrl) {
            avatarHtml = `<img class="avatar-img" src="${photoUrl}" alt="" data-testid="home-avatar-img">`;
        } else {
            avatarHtml = `<span class="avatar-placeholder">👤</span>`;
        }

        // Build name: use first_name if available, otherwise dash placeholder
        const displayName = firstName || '—';

        // Build username: only show if actually provided by Telegram
        let usernameHtml = '';
        if (username) {
            usernameHtml = `<span class="welcome-username" data-testid="home-username-handle">@${username}</span>`;
        }

        section.innerHTML = `
            <div class="welcome-card">
                <div class="welcome-avatar" data-testid="home-avatar">
                    ${avatarHtml}
                </div>
                <div class="welcome-info">
                    <span class="welcome-name" data-testid="home-username">${displayName}</span>
                    ${usernameHtml}
                    <span class="welcome-level" data-testid="home-level">المستوى: قريباً</span>
                    <div class="welcome-balance" data-testid="home-wallet-balance">
                        <span class="welcome-balance-usdt" data-testid="home-wallet-usdt">${usdtText} USDT</span>
                        <span class="welcome-balance-egp" data-testid="home-wallet-egp">≈ ${egpText} EGP</span>
                    </div>
                </div>
                <button type="button" class="wallet-button" data-testid="home-wallet-button">
                    <span class="wallet-button-icon" aria-hidden="true">${walletIcon}</span>
                    <span class="wallet-button-label">المحفظة</span>
                </button>
            </div>
        `;

        // The circular wallet button opens the Wallet page through the
        // existing navigation router (haptic feedback included there).
        const walletButton = section.querySelector('[data-testid="home-wallet-button"]');
        walletButton.addEventListener('click', () => {
            Navigation.navigateTo('wallet');
        });

        return section;
    }

    /* ── Balance / Reward Summary ─────────────────────────────── */

    function _buildBalanceSection() {
        const section = document.createElement('section');
        section.className = 'home-section home-balance';
        section.setAttribute('data-testid', 'home-balance');

        // The markup ships with the neutral «—» placeholder: that is
        // what stays visible until (and unless) the backend read
        // answers, so an unknown value is never rendered as a number.
        section.innerHTML = `
            <div class="balance-card">
                <div class="balance-row">
                    <div class="balance-item" data-testid="balance-reward">
                        <span class="balance-label">المكافآت</span>
                        <span class="balance-value balance-empty">—</span>
                    </div>
                    <div class="balance-item" data-testid="balance-available">
                        <span class="balance-label">المتاح</span>
                        <span class="balance-value balance-empty">—</span>
                    </div>
                </div>
            </div>
        `;

        // The two values come from the SAME backend response the
        // Tasks page renders (GET /api/tasks, owned by task-stats.js):
        // «المتاح» = the tasks the backend reports with status
        // "available", «المكافآت» = the exact sum of those same tasks'
        // rewards.  Home itself stays fetch-free; a failed or
        // unauthenticated read simply leaves the placeholders.
        if (typeof TaskStats !== 'undefined' && TaskStats &&
            typeof TaskStats.load === 'function') {
            TaskStats.load().then((stats) => {
                if (stats) {
                    _fillBalanceValues(section, stats);
                }
            });
        }
        return section;
    }

    /**
     * Fill the two balance cards from fetched stats.  Only backend-
     * confirmed values are written; anything unknown keeps the
     * original «—» placeholder and the balance-empty styling.
     */
    function _fillBalanceValues(section, stats) {
        const availableNode = section.querySelector(
            '[data-testid="balance-available"] .balance-value'
        );
        if (availableNode) {
            availableNode.textContent = String(stats.availableCount);
            availableNode.classList.remove('balance-empty');
        }

        const rewardNode = section.querySelector(
            '[data-testid="balance-reward"] .balance-value'
        );
        const rewardText = (typeof TaskStats !== 'undefined' && TaskStats &&
            typeof TaskStats.formatRewards === 'function')
            ? TaskStats.formatRewards(stats.rewardUnits)
            : null;
        if (rewardNode && rewardText) {
            rewardNode.textContent = rewardText;
            rewardNode.classList.remove('balance-empty');
        }
    }

    /* ── Daily Check-in ───────────────────────────────────────── */

    function _buildDailyCheckinSection() {
        const section = document.createElement('section');
        section.className = 'home-section home-checkin';
        section.setAttribute('data-testid', 'home-checkin');

        section.innerHTML = `
            <div class="section-card checkin-card">
                <div class="checkin-header">
                    <span class="section-icon">📅</span>
                    <span class="section-title">التسجيل اليومي</span>
                </div>
                <div class="checkin-body">
                    <span class="checkin-status" data-testid="checkin-status">قريباً</span>
                </div>
            </div>
        `;
        return section;
    }

    /* ── Official Guide ───────────────────────────────────────── */

    function _buildOfficialGuideSection() {
        const section = document.createElement('section');
        section.className = 'home-section home-guide';
        section.setAttribute('data-testid', 'home-guide');

        section.innerHTML = `
            <div class="section-card guide-card">
                <div class="guide-header">
                    <span class="section-icon">📖</span>
                    <span class="section-title">الدليل الرسمي</span>
                </div>
                <div class="guide-body">
                    <button type="button" class="guide-open"
                            data-testid="guide-open">📖 افتح الدليل</button>
                </div>
            </div>
        `;

        // Pressing the section (card or its button) opens the full
        // official guide in the existing overlay-dialog pattern —
        // guide.js owns the content, Home stays presentational.
        const guideCard = section.querySelector('.guide-card');
        if (guideCard) {
            guideCard.addEventListener('click', () => {
                if (typeof Guide !== 'undefined' && Guide &&
                    typeof Guide.open === 'function') {
                    Guide.open();
                }
            });
        }
        return section;
    }

    /* ── Add Task ─────────────────────────────────────────────── */

    function _buildAddTaskSection() {
        const section = document.createElement('section');
        section.className = 'home-section home-add-task';
        section.setAttribute('data-testid', 'home-add-task');

        section.innerHTML = `
            <div class="section-card add-task-card">
                <div class="add-task-header">
                    <span class="section-icon">➕</span>
                    <span class="section-title">إضافة مهمة</span>
                </div>
                <div class="add-task-body">
                    <button class="add-task-cta" data-testid="add-task-cta">➕ إضافة مهمة</button>
                </div>
            </div>
        `;

        // The CTA opens the task-proposal dialog (TaskRequestUI owns
        // every fetch/validation); with the flag off it degrades to
        // the original disabled placeholder.  Home itself stays
        // fetch-free and mutation-free.
        const cta = section.querySelector('[data-testid="add-task-cta"]');
        if (cta) {
            if (SHOW_USER_TASK_SUBMISSION) {
                cta.addEventListener('click', () => {
                    if (typeof TaskRequestUI !== 'undefined' &&
                        TaskRequestUI &&
                        typeof TaskRequestUI.open === 'function') {
                        TaskRequestUI.open();
                    }
                });
            } else {
                cta.disabled = true;
            }
        }
        return section;
    }

    /* ── Account Linking ──────────────────────────────────────── */

    function _buildAccountLinkingSection() {
        const section = document.createElement('section');
        section.className = 'home-section home-account-linking';
        section.setAttribute('data-testid', 'home-account-linking');

        section.innerHTML = `
            <div class="section-card account-linking-card">
                <div class="account-linking-header">
                    <span class="section-icon">🔗</span>
                    <span class="section-title">ربط الحسابات</span>
                </div>
                <!-- YouTube linking (SA-YT-01): OAuth connect + state -->
                <div class="account-linking-body">
                    <span class="account-linking-status">منصات أخرى قريباً</span>
                    <div class="social-account-row" data-testid="social-youtube-row">
                        <span class="social-account-provider" data-testid="social-youtube-provider">YouTube</span>
                        <span class="account-linking-status social-account-state" data-testid="social-youtube-state">غير مرتبط</span>
                        <button type="button" class="social-connect-btn" data-testid="social-youtube-connect">ربط YouTube</button>
                    </div>
                </div>
            </div>
        `;
        return section;
    }

    /* ── Hot Tasks ────────────────────────────────────────────── */

    function _buildHotTasksSection() {
        const section = document.createElement('section');
        section.className = 'home-section home-hot-tasks';
        section.setAttribute('data-testid', 'home-hot-tasks');

        section.innerHTML = `
            <div class="section-card hot-tasks-card">
                <div class="hot-tasks-header">
                    <span class="section-icon">🔥</span>
                    <span class="section-title">المهام الساخنة</span>
                </div>
                <div class="hot-tasks-body" data-testid="hot-tasks-list">
                    <span class="hot-tasks-empty">لا توجد مهام حالياً</span>
                </div>
            </div>
        `;
        return section;
    }

    return { render };
})();
