/**
 * Header Component
 * Handles the top action buttons (الشحن/السحب)
 */
const Header = (() => {
    let onActionCallback = null;

    /**
     * Initialize the header component
     */
    function init(callback) {
        onActionCallback = callback;
        
        const chargeBtn = document.getElementById('btn-charge');
        const withdrawBtn = document.getElementById('btn-withdraw');

        if (chargeBtn) {
            chargeBtn.addEventListener('click', () => handleAction('charge'));
        }

        if (withdrawBtn) {
            withdrawBtn.addEventListener('click', () => handleAction('withdraw'));
        }
    }

    /**
     * Handle header button actions
     */
    function handleAction(action) {
        // Haptic feedback if available
        if (window.Telegram?.WebApp?.HapticFeedback) {
            window.Telegram.WebApp.HapticFeedback.impactOccurred('light');
        }

        if (onActionCallback) {
            onActionCallback(action);
        }
    }

    /**
     * Update header state (for future use)
     */
    function updateState(state) {
        // Placeholder for future state updates
        // Could show loading states, disable buttons, etc.
    }

    return {
        init,
        handleAction,
        updateState
    };
})();
