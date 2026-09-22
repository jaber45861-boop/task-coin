/**
 * Task Coin Mini App
 * Main application entry point
 */
const App = (() => {
    const contentEl = document.getElementById('app-content');
    let currentPage = 'home';

    /**
     * Initialize the application
     */
    function init() {
        // Initialize Telegram WebApp
        const telegramReady = TelegramApp.init();
        
        if (!telegramReady) {
            console.warn('Running outside Telegram environment');
        }

        // Initialize header
        Header.init(handleHeaderAction);

        // Initialize navigation
        Navigation.init(handleNavigation);

        // Render initial page
        renderPage('home');
    }

    /**
     * Handle header button actions
     *
     * The old header action bar was removed in favour of the Wallet
     * page (MT-UI-03); Header.init remains wired as the
     * generic header action bus.
     */
    function handleHeaderAction(action) {
        console.log('Header action:', action);
    }

    /**
     * Handle navigation changes
     */
    function handleNavigation(page) {
        renderPage(page);
    }

    /**
     * Render a page from a dynamic component or template.
     * The 'home' page is built by the Home module and the 'wallet'
     * page by the Wallet module; other pages use HTML <template>
     * cloning — same routing pattern as before.
     */
    function renderPage(page) {
        // Clear current content
        contentEl.innerHTML = '';

        let pageEl;

        if (page === 'home' && typeof Home !== 'undefined') {
            pageEl = Home.render();
        } else if (page === 'wallet' && typeof Wallet !== 'undefined') {
            pageEl = Wallet.render();
        } else {
            const template = document.getElementById(`page-${page}`);

            if (!template) {
                console.error(`Page template not found: ${page}`);
                return;
            }

            const fragment = template.content.cloneNode(true);
            pageEl = fragment.querySelector('.page');
            if (!pageEl) {
                pageEl = fragment;
            }
        }

        contentEl.appendChild(pageEl);

        // Add enter animation
        pageEl.classList.add('page-enter');

        currentPage = page;

        // Presentational only: let CSS scope page-specific chrome
        // (header action buttons are shown on Home only).
        document.body.dataset.page = page;
    }

    /**
     * Get the current page
     */
    function getCurrentPage() {
        return currentPage;
    }

    return {
        init,
        getCurrentPage,
        renderPage
    };
})();

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    App.init();
});
