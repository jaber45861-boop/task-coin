/**
 * Bottom Navigation Component
 * Handles tab switching between الرئيسية/المهام/حسابي
 */
const Navigation = (() => {
    let currentPage = 'home';
    let onNavigateCallback = null;

    /**
     * Initialize the navigation component
     */
    function init(callback) {
        onNavigateCallback = callback;
        
        const navItems = document.querySelectorAll('.nav-item');
        
        navItems.forEach(item => {
            item.addEventListener('click', () => {
                const page = item.dataset.page;
                if (page && page !== currentPage) {
                    navigateTo(page);
                }
            });
        });
    }

    /**
     * Navigate to a specific page
     */
    function navigateTo(page) {
        // Haptic feedback if available
        if (window.Telegram?.WebApp?.HapticFeedback) {
            window.Telegram.WebApp.HapticFeedback.impactOccurred('light');
        }

        // Update active state
        const navItems = document.querySelectorAll('.nav-item');
        navItems.forEach(item => {
            item.classList.toggle('active', item.dataset.page === page);
        });

        currentPage = page;

        // Notify callback
        if (onNavigateCallback) {
            onNavigateCallback(page);
        }
    }

    /**
     * Get the current active page
     */
    function getCurrentPage() {
        return currentPage;
    }

    return {
        init,
        navigateTo,
        getCurrentPage
    };
})();
