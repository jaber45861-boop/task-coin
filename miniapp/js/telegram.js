/**
 * Telegram WebApp Integration Module
 * Handles initialization and theme synchronization
 */
const TelegramApp = (() => {
    let tg = null;
    let user = null;

    /**
     * Initialize the Telegram WebApp
     */
    function init() {
        if (typeof window.Telegram === 'undefined' || !window.Telegram.WebApp) {
            console.warn('Telegram WebApp SDK not available');
            return false;
        }

        tg = window.Telegram.WebApp;
        
        // Ready signal to Telegram
        tg.ready();
        
        // Expand to full height
        tg.expand();
        
        // Apply theme
        applyTheme();
        
        // Get user info
        user = tg.initDataUnsafe?.user || null;
        
        // Listen for theme changes
        tg.onEvent('themeChanged', applyTheme);
        
        return true;
    }

    /**
     * Apply Telegram theme to the page
     */
    function applyTheme() {
        if (!tg) return;

        const themeParams = tg.themeParams;
        const root = document.documentElement;

        // Set CSS variables from Telegram theme
        if (themeParams.bg_color) {
            root.style.setProperty('--bg-color', themeParams.bg_color);
        }
        if (themeParams.text_color) {
            root.style.setProperty('--text-color', themeParams.text_color);
        }
        if (themeParams.hint_color) {
            root.style.setProperty('--text-secondary', themeParams.hint_color);
        }
        if (themeParams.secondary_bg_color) {
            root.style.setProperty('--bg-secondary', themeParams.secondary_bg_color);
        }
        if (themeParams.button_color) {
            root.style.setProperty('--accent-color', themeParams.button_color);
        }
        if (themeParams.button_text_color) {
            // Use button text color for nav active state
            root.style.setProperty('--nav-active-color', themeParams.button_text_color);
        }

        // Detect dark mode
        const isDark = tg.colorScheme === 'dark';
        document.body.classList.toggle('dark', isDark);
    }

    /**
     * Get the current user info
     */
    function getUser() {
        return user;
    }

    /**
     * Get the WebApp instance
     */
    function getWebApp() {
        return tg;
    }

    /**
     * Get the init data for backend authentication
     */
    function getInitData() {
        return tg?.initData || '';
    }

    return {
        init,
        applyTheme,
        getUser,
        getWebApp,
        getInitData
    };
})();
