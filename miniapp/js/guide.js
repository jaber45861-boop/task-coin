/**
 * Official Guide (Mini App «الدليل الرسمي 📖»)
 * ============================================
 * Overlay dialog opened from the Home «الدليل الرسمي» section: it
 * shows the complete official guide for Market Task.
 *
 * Boundaries (mirrors task-request.js / wallet.js):
 * - purely presentational: no fetch, no API, no auth, no database —
 *   the guide text is a fixed, trusted string shipped with the app
 * - no feature flag is involved (SHOW_DAILY_REGISTRATION and the
 *   other Home flags are untouched)
 * - reuses the existing overlay-dialog mechanics: fixed backdrop +
 *   scrollable card + close button + the shared Telegram haptic helper
 * - the document inherits `dir="rtl"` from index.html, so the whole
 *   guide renders right-to-left on mobile without extra plumbing
 */
const Guide = (() => {
    let overlay = null;

    /**
     * The official guide, verbatim. Kept as one static markup block:
     * it never contains user/server data, so nothing here is escaped
     * at runtime — and nothing here is ever shortened.
     */
    const GUIDE_DOC = `
        <p class="guide-intro">
            مرحبًا بك في Market Task 👋<br>
            هنا يمكنك متابعة المهام وإضافة مهامك وإدارة طلباتك بسهولة من خلال الـMini App.
        </p>

        <section class="guide-block" data-testid="guide-block-home">
            <h4 class="guide-h">🏠 1. الرئيسية</h4>
            <p>من الصفحة الرئيسية ستجد أهم المعلومات الخاصة بحسابك، بالإضافة إلى:</p>
            <ul class="guide-list">
                <li>👤 معلومات حسابك ومستواك.</li>
                <li>💰 المحفظة والرصيد.</li>
                <li>➕ إضافة مهمة.</li>
                <li>🔥 المهام الساخنة والمتاحة.</li>
            </ul>
        </section>

        <section class="guide-block" data-testid="guide-block-add-task">
            <h4 class="guide-h">➕ 2. إضافة مهمة</h4>
            <p>اضغط على «إضافة مهمة» لفتح نموذج المهمة.</p>
            <p>أدخل بيانات المهمة المطلوبة بشكل واضح، ثم أرسل الطلب.</p>
            <p>بعد الإرسال يتم تسجيل طلب المهمة ومراجعته وفق نظام المنصة.</p>
        </section>

        <section class="guide-block" data-testid="guide-block-tasks">
            <h4 class="guide-h">📋 3. متابعة المهام</h4>
            <p>من قسم «المهام» يمكنك متابعة المهام المتاحة والمهام التي يمكنك تنفيذها.</p>
            <p>اقرأ تفاصيل المهمة وشروطها جيدًا قبل البدء.</p>
        </section>

        <section class="guide-block" data-testid="guide-block-hot">
            <h4 class="guide-h">🔥 4. المهام الساخنة</h4>
            <p>هذا القسم يعرض المهام التي يتم إبرازها للمستخدمين.</p>
            <p>تابع القسم باستمرار لمعرفة المهام المتاحة لك.</p>
        </section>

        <section class="guide-block" data-testid="guide-block-wallet">
            <h4 class="guide-h">👛 5. المحفظة</h4>
            <p>المحفظة مخصصة لعرض بيانات رصيدك والمعاملات المرتبطة بحسابك.</p>
            <p class="guide-note" data-testid="guide-note">«ملاحظة: ظهور بعض بيانات الرصيد أو المكافآت يعتمد على الأنظمة والخصائص المفعّلة في المنصة.»</p>
        </section>

        <section class="guide-block" data-testid="guide-block-requests">
            <h4 class="guide-h">📝 6. طلبات المهام</h4>
            <p>عند إرسال مهمة جديدة، قد يمر الطلب بعدة حالات أثناء المراجعة:</p>
            <ul class="guide-list">
                <li>🟡 قيد المراجعة.</li>
                <li>🟢 تمت الموافقة عليه.</li>
                <li>🔴 تم رفضه.</li>
                <li>🔄 يحتاج إلى تعديلات وإعادة إرسال.</li>
            </ul>
            <p>إذا طُلب منك تعديل الطلب، قم بتحديث البيانات المطلوبة ثم أعد إرساله.</p>
        </section>

        <section class="guide-block" data-testid="guide-block-security">
            <h4 class="guide-h">🔐 7. الأمان وتسجيل الدخول</h4>
            <p>استخدم الـMini App من خلال Telegram فقط، ولا تشارك بيانات حسابك أو أي رموز وصول مع أي شخص.</p>
            <p>إذا ظهرت رسالة «تعذر الاتصال بالخادم»، تأكد من فتح التطبيق من داخل Telegram ثم اضغط «إعادة المحاولة».</p>
        </section>

        <section class="guide-block" data-testid="guide-block-tips">
            <h4 class="guide-h">💡 نصائح مهمة</h4>
            <ul class="guide-list">
                <li>اقرأ تفاصيل المهمة قبل إرسالها.</li>
                <li>تأكد من صحة البيانات قبل الإرسال.</li>
                <li>لا ترسل معلومات حساسة داخل وصف المهمة.</li>
                <li>تابع حالة طلباتك من قسم المهام.</li>
                <li>في حالة وجود مشكلة، أعد فتح التطبيق وحاول مرة أخرى.</li>
            </ul>
        </section>

        <section class="guide-block guide-start" data-testid="guide-block-start">
            <h4 class="guide-h">🚀 ابدأ الآن</h4>
            <p>ارجع إلى الرئيسية واضغط:</p>
            <p><span class="guide-cta-line" data-testid="guide-cta-line">➕ إضافة مهمة</span></p>
            <p>لبدء إنشاء أول طلب لك.</p>
        </section>
    `;

    /** Existing project haptic helper pattern (same as navigation.js). */
    function _haptic() {
        if (window.Telegram?.WebApp?.HapticFeedback) {
            window.Telegram.WebApp.HapticFeedback.impactOccurred('light');
        }
    }

    /**
     * Open the official guide dialog. Idempotent: a second open()
     * while the dialog is visible is a no-op.
     */
    function open() {
        if (overlay) {
            return;
        }
        _haptic();

        overlay = document.createElement('div');
        overlay.className = 'guide-overlay';
        overlay.setAttribute('data-testid', 'guide-overlay');
        overlay.innerHTML = `
            <div class="guide-dialog" role="dialog" aria-modal="true"
                 aria-label="الدليل الرسمي">
                <div class="guide-dialog-head">
                    <h3 class="guide-dialog-title">📖 الدليل الرسمي — Market Task</h3>
                    <button type="button" class="guide-close"
                            data-testid="guide-close"
                            aria-label="إغلاق">✕</button>
                </div>
                <div class="guide-doc" data-testid="guide-doc">
                    ${GUIDE_DOC}
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        overlay.querySelector('[data-testid="guide-close"]')
            .addEventListener('click', close);
        // Backdrop tap closes; the card itself never bubbles out.
        overlay.addEventListener('click', (event) => {
            if (event.target === overlay) {
                close();
            }
        });
        document.addEventListener('keydown', _onKeydown);
    }

    /** Escape closes the dialog (same behaviour as any modal). */
    function _onKeydown(event) {
        if (event.key === 'Escape') {
            close();
        }
    }

    function close() {
        if (!overlay) {
            return;
        }
        document.removeEventListener('keydown', _onKeydown);
        overlay.remove();
        overlay = null;
    }

    return { open, close };
})();
