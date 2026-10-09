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

    /* ── Reviewer entry visibility ──────────────────────────────── */

    /**
     * Make the Account reviewer entry unrenderable, inline.
     *
     * The `hidden` attribute alone is NOT enough: any author rule that
     * sets `display` on the entry outranks the user-agent `[hidden]`
     * rule, so a client still holding a stylesheet that predates
     * `.review-entry[hidden]` would render a "hidden" button.  An
     * inline `!important` declaration is the strongest author
     * declaration available, so the entry cannot be displayed by any
     * cached stylesheet, and it cannot flash before the authority
     * probe answers either.
     */
    function _lockReviewEntry(entry) {
        entry.hidden = true;
        entry.style.setProperty('display', 'none', 'important');
    }

    /**
     * Release the lock for a server-authorized reviewer.
     *
     * Only called on an explicit `true` verdict.  Clearing the inline
     * declaration hands the presentation back to the stylesheet, where
     * `.review-entry` lays the button out.
     */
    function _unlockReviewEntry(entry) {
        entry.hidden = false;
        entry.style.removeProperty('display');
    }

    /**
     * Handle navigation changes
     */
    function handleNavigation(page) {
        renderPage(page);
    }

    /**
     * Render a page from a dynamic component or template.
     * The 'home' page is built by the Home module, the 'wallet'
     * page by the Wallet module, the 'tasks' page by the Tasks
     * module (MT-TASK-03, real API data) and the 'profile'
     * page by the Profile module (the caller's Telegram
     * identity); other pages use HTML <template> cloning —
     * same routing pattern as before.
     */
    function renderPage(page) {
        // Clear current content
        contentEl.innerHTML = '';

        let pageEl;

        if (page === 'home' && typeof Home !== 'undefined') {
            pageEl = Home.render();
        } else if (page === 'wallet' && typeof Wallet !== 'undefined') {
            pageEl = Wallet.render();
        } else if (page === 'tasks' && typeof Tasks !== 'undefined') {
            pageEl = Tasks.render();
        } else if (page === 'review' && typeof Review !== 'undefined') {
            pageEl = Review.render();
        } else if (page === 'profile' && typeof Profile !== 'undefined') {
            pageEl = Profile.render();
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

        // Wire the existing SocialAccounts module (SA-YT-01) to the
        // Home account-linking section; the module owns the YouTube
        // connect handler, which is never duplicated here.
        if (page === 'home' && typeof SocialAccounts !== 'undefined') {
            const accountLinkingSection = pageEl.querySelector(
                '[data-testid="home-account-linking"]'
            );
            if (accountLinkingSection) {
                SocialAccounts.attach(accountLinkingSection);
            }
        }

        // In-page entries (e.g. the Account reviewer entry) reuse the
        // existing Navigation router — same pattern as the wallet
        // icon on Home; no second router is introduced.
        pageEl.querySelectorAll('[data-goto]').forEach((el) => {
            el.addEventListener('click', () => Navigation.navigateTo(el.dataset.goto));
        });

        // The Account reviewer entry is a reviewer-only surface.  It is
        // hidden HERE, with inline state, the moment the page is built
        // — BEFORE any request and unconditionally: a missing probe, a
        // stale review.js or a failed request can never leave it
        // visible.  Only an explicit `true` from the SERVER authority
        // probe (the same ok /claims verdict the review page renders
        // from) clears that inline state.  Identity rides the verified
        // initData header only; the client never decides.
        if (page === 'profile') {
            const entry = pageEl.querySelector('[data-testid="review-entry"]');
            if (entry) {
                _lockReviewEntry(entry);
                if (typeof Review !== 'undefined'
                    && typeof Review.probeAccess === 'function') {
                    Review.probeAccess().then((allowed) => {
                        if (allowed === true) {
                            _unlockReviewEntry(entry);
                        }
                    }, () => {
                        // Authority unproven — the entry stays locked.
                    });
                }
            }
        }

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
