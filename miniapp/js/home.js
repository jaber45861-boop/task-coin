/**
 * Home Page Component
 * Renders the الرئيسية dashboard with structural UI sections.
 * All sections use neutral/placeholder states — no fake data.
 */
const Home = (() => {

    /**
     * Build and return the full Home page DOM element.
     */
    function render() {
        const page = document.createElement('div');
        page.className = 'page page-home';
        page.setAttribute('data-testid', 'home-page');

        page.appendChild(_buildWelcomeSection());
        page.appendChild(_buildBalanceSection());
        page.appendChild(_buildDailyCheckinSection());
        page.appendChild(_buildOfficialGuideSection());
        page.appendChild(_buildAddTaskSection());
        page.appendChild(_buildAccountLinkingSection());
        page.appendChild(_buildHotTasksSection());

        return page;
    }

    /* ── Welcome / Profile Summary ────────────────────────────── */

    function _buildWelcomeSection() {
        const section = document.createElement('section');
        section.className = 'home-section home-welcome';
        section.setAttribute('data-testid', 'home-welcome');

        section.innerHTML = `
            <div class="welcome-card">
                <div class="welcome-avatar" data-testid="home-avatar">
                    <span class="avatar-placeholder">👤</span>
                </div>
                <div class="welcome-info">
                    <span class="welcome-name" data-testid="home-username">—</span>
                    <span class="welcome-level" data-testid="home-level">المستوى: قريباً</span>
                </div>
            </div>
        `;
        return section;
    }

    /* ── Balance / Reward Summary ─────────────────────────────── */

    function _buildBalanceSection() {
        const section = document.createElement('section');
        section.className = 'home-section home-balance';
        section.setAttribute('data-testid', 'home-balance');

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
        return section;
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
                    <span class="guide-status">قريباً</span>
                </div>
            </div>
        `;
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
                    <button class="add-task-cta" data-testid="add-task-cta" disabled>➕ إضافة مهمة</button>
                </div>
            </div>
        `;
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
                <div class="account-linking-body">
                    <span class="account-linking-status">قريباً</span>
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
