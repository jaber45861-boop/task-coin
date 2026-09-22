/**
 * Header Component
 * Generic header action bus.  The old top action buttons were removed
 * in favour of the Wallet page (MT-UI-03); the component keeps its
 * action wiring so future header actions have one home.
 */
const Header = (() => {
    let onActionCallback = null;

    /**
     * Initialize the header component
     */
    function init(callback) {
        onActionCallback = callback;
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
