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

        // The Wallet page is opened from the Home wallet icon — it is
        // NOT a bottom-navigation tab, so the Home tab stays active
        // while the Wallet page is open (approved design).
        const activeTab = page === 'wallet' ? 'home' : page;

        // Update active state
        const navItems = document.querySelectorAll('.nav-item');
        navItems.forEach(item => {
            item.classList.toggle('active', item.dataset.page === activeTab);
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
