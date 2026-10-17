/* Password-reset form handler. Extracted from an inline <script> so the page
 * can be served under a strict Content-Security-Policy (script-src 'self',
 * no 'unsafe-inline'). */
(function () {
    'use strict';
    var form = document.getElementById('reset-form');
    if (!form) return;
    form.addEventListener('submit', function (e) {
        e.preventDefault();
        var msg = document.getElementById('msg');
        var btn = form.querySelector('button');
        msg.textContent = '';
        msg.className = 'msg';
        btn.disabled = true;
        fetch('/reset_password', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': form.csrf_token.value
            },
            body: JSON.stringify({
                token: form.token.value,
                password: form.password.value
            })
        }).then(function (r) {
            // A proxy's 502/504 page is HTML: that is a server error with a
            // status to show, not a network failure (the .catch below).
            return r.json().catch(function () { return null; }).then(function (body) {
                return { ok: r.ok, status: r.status, body: body };
            });
        }).then(function (res) {
            if (res.ok) {
                msg.textContent = 'Password reset. Redirecting to sign in...';
                msg.className = 'msg ok';
                setTimeout(function () { window.location.href = '/'; }, 1500);
            } else {
                msg.textContent = (res.body && res.body.error)
                    || 'Something went wrong (HTTP ' + res.status + '). Please try again.';
                msg.className = 'msg err';
                btn.disabled = false;
            }
        }).catch(function () {
            msg.textContent = 'Network error. Please try again.';
            msg.className = 'msg err';
            btn.disabled = false;
        });
    });
})();
