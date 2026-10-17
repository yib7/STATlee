"""Browser checks for the front-end guards added in the 2026-10-16 release
audit (3A area C): stale background replies, double clicks on billed
buttons, the Copy button's label, and edits flushed on exit.

Same harness as ``test_e2e_ui.py`` (live app on a free localhost port, every
model call answered by the deterministic ``FakeLLMService``, Chromium through
Playwright): $0, no real provider. Skips cleanly without Playwright.
"""
import json

import pytest

sync_api = pytest.importorskip('playwright.sync_api')

from test_e2e_ui import (  # noqa: E402,F401  (fixtures are used by name)
    CODEBOOK_A,
    CSV_A,
    CSV_B,
    _capture_bodies,
    _new_page,
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
