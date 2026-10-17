"""Browser checks for the front-end guards added in the 2026-10-16 release
audit (3A area C): stale background replies, double clicks on billed
buttons, the Copy button's label, and edits flushed on exit.

Same harness as ``test_e2e_ui.py`` (live app on a free localhost port, every
model call answered by the deterministic ``FakeLLMService``, Chromium through
Playwright): $0, no real provider. Skips cleanly without Playwright.
"""

import pytest

sync_api = pytest.importorskip('playwright.sync_api')

from test_e2e_ui import (  # noqa: E402,F401  (fixtures are used by name)
    CODEBOOK_A,
    CSV_A,
    CSV_B,
    browser,
    calls,
    e2e_server,
    fake,
    generate,
    page,
    skip_codebook,
    upload,
    wait_codebook_prompt,
)

# ---------------------------------------------------------------------------
# C-16: a background re-classify of dataset A must not land on dataset B
# ---------------------------------------------------------------------------

def test_stale_reclassify_does_not_touch_the_next_dataset(page, fake):
    fake.set('classify', CODEBOOK_A)
    upload(page, 'stale-a.csv', CSV_A + '81,1,A\n')
    skip_codebook(page)
    # The wrangle's background re-classify of A is held back in the page...
    page.evaluate("() => { window.__fetchDelay['/classify_variables'] = 1500; }")
    page.fill('#wrangleInput', 'drop missing rows')
    page.press('#wrangleInput', 'Enter')
    page.wait_for_selector('.cc-toast.success')
    page.wait_for_function('() => (window.__fetchCalls["/classify_variables"] || 0) >= 2')
    # ...while B is uploaded and its codebook question is still open.
    with page.expect_response('**/classify_variables') as stale:
        upload(page, 'stale-b.csv', CSV_B)
        wait_codebook_prompt(page)
    assert stale.value.ok
    page.wait_for_timeout(200)   # let the page apply (or drop) that reply
    assert page.evaluate('() => CC.state.filename') == 'stale-b.csv'
    assert page.evaluate('() => CC.state.codebook') == {}, \
        "dataset A's classification was written into B's session"
    assert page.evaluate("() => document.getElementById('generateBtn').disabled"), \
        'a stale reply re-enabled Generate before B was profiled'
    assert not page.is_visible('#codebookContainer')


# ---------------------------------------------------------------------------
# C-18: a double click on a billed button is one request, not two
# ---------------------------------------------------------------------------

def test_double_click_on_a_catalog_method_drafts_once(page):
    upload(page, 'dbl-m.csv', CSV_A + '82,1,A\n')
    skip_codebook(page)
    page.click('#catalogBtn')
    page.wait_for_selector('#catalogModal:not(.hidden)')
    page.evaluate("() => { window.__fetchDelay['/method_prompt'] = 600; }")
    method = page.locator('#catalogList button', has_text='Pearson correlation')
    label = method.text_content()
    method.dblclick()
    page.wait_for_selector('.cc-toast.success')
    assert calls(page, '/method_prompt') == 1
    # The modal closed on success; the button reads its method again, not
    # the spinner a second click used to capture as its "original" label.
    assert method.text_content() == label
    assert not method.is_disabled()


def test_double_click_on_reroll_asks_once(page):
    upload(page, 'dbl-r.csv', CSV_A + '83,1,A\n')
    skip_codebook(page)
    page.wait_for_selector('#suggestionsList button')
    before = calls(page, '/suggest')
    page.evaluate("() => { window.__fetchDelay['/suggest'] = 600; }")
    page.dblclick('#rerollBtn')
    page.wait_for_function("() => !document.getElementById('rerollBtn').disabled"
                           " && document.querySelector('#suggestionsList button')")
    assert calls(page, '/suggest') == before + 1


# ---------------------------------------------------------------------------
# C-19: a second Copy click within 2 s must not leave "Copied!" for good
# ---------------------------------------------------------------------------

def test_copy_button_label_comes_back_after_two_quick_clicks(page):
    upload(page, 'copy.csv', CSV_A + '84,1,A\n')
    skip_codebook(page)
    generate(page)
    page.click('#tabGenerate')
    page.wait_for_selector('#copyBtn', state='visible')
    label = page.text_content('#copyBtn').strip()
    assert 'Copied' not in label
    page.evaluate("() => { navigator.clipboard.writeText = () => Promise.resolve(); }")
    page.clock.install()
    page.click('#copyBtn')
    page.wait_for_function("() => document.getElementById('copyBtn').textContent.includes('Copied!')")
    page.clock.run_for(500)
    page.click('#copyBtn')
    page.clock.run_for(5000)
    assert page.text_content('#copyBtn').strip() == label


# ---------------------------------------------------------------------------
# C-20: a proxy's HTML error page on reset is a server error, not "network"
# ---------------------------------------------------------------------------

def test_reset_password_non_json_error_names_the_status(page, e2e_server, monkeypatch):
    from statlee.routes import auth as auth_mod

    # The live server runs in this process: let the GET render the form for
    # a made-up token instead of creating a user and a real reset token.
    monkeypatch.setattr(auth_mod, '_reset_user_for_token',
                        lambda token: object() if token == 'e2e-reset-tok' else None)

    def _proxy_error(route):
        if route.request.method == 'POST':
            route.fulfill(status=502, content_type='text/html',
                          body='<html><body>502 Bad Gateway</body></html>')
        else:
            route.continue_()

    page.context.route('**/reset_password', _proxy_error)
    page.goto(e2e_server['url'] + '/reset_password?token=e2e-reset-tok')
    page.fill('#password', 'a-new-pass-123')
    page.click('#reset-form button')
    page.wait_for_selector('#msg.err')
    text = page.text_content('#msg')
    assert 'Network error' not in text, text
    assert '502' in text, text
    assert not page.is_disabled('#reset-form button')
