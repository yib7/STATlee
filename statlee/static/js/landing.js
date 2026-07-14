/* Landing-page scroll reveal.
 *
 * Lives in a static file rather than an inline <script> so the page can be
 * served under the app's strict Content-Security-Policy (script-src 'self',
 * no 'unsafe-inline'). See CSP_POLICY in statlee/app.py.
 *
 * Elements marked [data-reveal] start at opacity 0 in the stylesheet and get
 * the .revealed class once they scroll into view. Anything that cannot run the
 * observer is shown immediately, so the copy is never stuck invisible.
 */
(function () {
    'use strict';

    function reveal() {
        var els = document.querySelectorAll('[data-reveal]');
        if (!els.length) return;

        var reduce = window.matchMedia &&
            window.matchMedia('(prefers-reduced-motion: reduce)').matches;

        // Reduced-motion visitors, and browsers without IntersectionObserver,
        // get the whole page at once instead of a fade that never fires.
        if (reduce || !('IntersectionObserver' in window)) {
            Array.prototype.forEach.call(els, function (el) {
                el.classList.add('revealed');
            });
            return;
        }

        var list = Array.prototype.slice.call(els);
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (!entry.isIntersecting) return;
                // Stagger in groups of four so a row of cards ripples in
                // rather than snapping as one block.
                var idx = list.indexOf(entry.target);
                entry.target.style.transitionDelay = ((idx % 4) * 70) + 'ms';
                entry.target.classList.add('revealed');
                io.unobserve(entry.target);
            });
        }, { threshold: 0.12, rootMargin: '0px 0px -8% 0px' });

        list.forEach(function (el) { io.observe(el); });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', reveal);
    } else {
        reveal();
    }
})();
