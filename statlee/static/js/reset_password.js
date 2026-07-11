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
            return r.json().then(function (body) { return { ok: r.ok, body: body }; });
        }).then(function (res) {
            if (res.ok) {
                msg.textContent = 'Password reset. Redirecting to sign in...';
                msg.className = 'msg ok';
                setTimeout(function () { window.location.href = '/'; }, 1500);
            } else {
                msg.textContent = (res.body && res.body.error) || 'Something went wrong.';
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
