/* STATlee boot script - runs in <head> before the body renders.
 *
 * It exists so a strict Content-Security-Policy (script-src 'self', no
 * 'unsafe-inline') can be enforced: the page carries no executable inline
 * script and no inline onclick= handlers. Responsibilities:
 *   1) read the server-injected config from the CSP-safe JSON island
 *      (<script type="application/json">, which is data, not executable) into
 *      window.CC_BOOT for api.js / tools.js;
 *   2) apply the saved or system theme before first paint (no flash);
 *   3) wire the former inline onclick= handlers through one delegated listener.
 */
(function () {
    'use strict';

    var island = document.getElementById('cc-boot-data');
    if (island) {
        try {
            window.CC_BOOT = JSON.parse(island.textContent || '{}');
        } catch (e) {
            window.CC_BOOT = {};
        }
    } else {
        window.CC_BOOT = window.CC_BOOT || {};
    }

    // Theme before paint (FOUC guard). Kept resilient: a locked-down browser
    // with no localStorage/matchMedia just stays on the default light theme.
    try {
        var store = window.localStorage;
        var dark = store.getItem('theme') === 'dark' ||
            (!('theme' in store) &&
             window.matchMedia('(prefers-color-scheme: dark)').matches);
        document.documentElement.classList.toggle('dark', dark);
    } catch (e) { /* no-op */ }

    // Delegated replacement for inline onclick= handlers. The target functions
    // (switchTab, toggleSidebar, toggleTheme, toggleCodebook, changePage) are
    // globals defined by the app JS loaded at the end of <body>; resolving them
    // lazily at click time lets this listener attach from <head>.
    document.addEventListener('click', function (ev) {
        var el = ev.target.closest && ev.target.closest('[data-action]');
        if (!el) return;
        var fn = window[el.getAttribute('data-action')];
        if (typeof fn !== 'function') return;
        var arg = el.getAttribute('data-arg');
        if (arg !== null && arg !== '' && !isNaN(arg)) {
            arg = Number(arg);
        }
        fn(arg);
    });
})();
