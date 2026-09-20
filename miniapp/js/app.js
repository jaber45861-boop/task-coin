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
     */
    function handleHeaderAction(action) {
        // Placeholder for future charge/withdraw functionality
        console.log('Header action:', action);
        
        // For now, just show a placeholder
        if (action === 'charge') {
            showPlaceholder('الشحن');
        } else if (action === 'withdraw') {
            showPlaceholder('السحب');
        }
    }

    /**
     * Handle navigation changes
     */
    function handleNavigation(page) {
        renderPage(page);
    }

    /**
     * Render a page from template
     */
    function renderPage(page) {
        const template = document.getElementById(`page-${page}`);
        
        if (!template) {
            console.error(`Page template not found: ${page}`);
            return;
        }

        // Clear current content
        contentEl.innerHTML = '';

        // Clone and append template content
        const pageContent = template.content.cloneNode(true);
        contentEl.appendChild(pageContent);

        // Add enter animation
        const pageEl = contentEl.querySelector('.page');
        if (pageEl) {
            pageEl.classList.add('page-enter');
        }

        currentPage = page;
    }

    /**
     * Show a placeholder message (for header actions)
     */
    function showPlaceholder(action) {
        const template = document.getElementById('page-home');
        const placeholder = template.content.cloneNode(true);
        
        const pageEl = placeholder.querySelector('.page');
        if (pageEl) {
            pageEl.querySelector('h2').textContent = action;
            pageEl.querySelector('.placeholder-text').textContent = 
                `${action} - قريباً`;
        }

        contentEl.innerHTML = '';
        contentEl.appendChild(placeholder);
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
