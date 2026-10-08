"""Browser checks for the SP4 export dialog (2026-10-08).

Same harness as ``test_e2e_ui.py`` (live app on a free localhost port, every
model call answered by the deterministic ``FakeLLMService``, Chromium through
Playwright): $0, no real provider. The real sandbox runs the scripts, and the
downloaded zip is opened to check the options reached the server.

Set ``STATLEE_SHOTS_DIR`` to also write review screenshots of the dialog
(light/dark, 1440px/390px). Skips cleanly without Playwright.
"""
import io
import json
import os
import zipfile

import pytest

sync_api = pytest.importorskip('playwright.sync_api')

from test_e2e_ui import (  # noqa: E402,F401  (fixtures are used by name)
    CSV_A,
    _capture_bodies,
    _new_page,
    browser,
    e2e_server,
    fake,
    generate,
    page,
    run_script,
    skip_codebook,
    upload,
)

SCRIPT = "print('Mean income: 42000')\n"


def _start(page, fake, name, runs=2):
    upload(page, name, CSV_A + '61,1,A\n')
    skip_codebook(page)
    fake.set('draft', SCRIPT)
    fake.set('validation', SCRIPT)
    for n in range(runs):
        generate(page, f'Question number {n}')
        run_script(page)
    page.wait_for_function(
        f"() => document.querySelectorAll('#runList > li').length === {runs}")


def _download(page):
    page.click('#exportBtn')
    page.wait_for_selector('#exportModal:not(.hidden)')
    with page.expect_download() as info:
        page.click('#exportDownloadBtn')
    path = info.value.path()
    with open(path, 'rb') as f:
        return zipfile.ZipFile(io.BytesIO(f.read()))


def _run_dirs(zf):
    return sorted({n.split('/')[1] for n in zf.namelist()
                   if n.startswith('outputs/')})


def test_export_dialog_defaults_send_all_runs_with_prompts(page, fake):
    bodies = _capture_bodies(page, '/export')
    _start(page, fake, 'ex-default.csv')
    zf = _download(page)
    assert bodies[-1]['include_prompts'] is True
    assert bodies[-1]['runs'] == 'all'
    assert _run_dirs(zf) == ['run_0001', 'run_0002']
    prov = json.loads(zf.read('provenance.json'))
    assert any(s.get('prompt') == 'Question number 0' for s in prov['steps'])
    for name in ('README.md', 'analysis.py', 'environment.txt',
                 'codebook.json'):
        assert name in zf.namelist(), name
    page.wait_for_selector('#exportModal.hidden', state='attached')
    assert 'README.md' in page.inner_text('.cc-toast.success')


def test_export_dialog_options_reach_the_bundle(page, fake):
    bodies = _capture_bodies(page, '/export')
    _start(page, fake, 'ex-options.csv')
    page.click('#exportBtn')
    page.wait_for_selector('#exportModal:not(.hidden)')
    page.uncheck('#exportIncludePrompts')
    page.select_option('#exportRuns', 'latest')
    with page.expect_download() as info:
        page.click('#exportDownloadBtn')
    with open(info.value.path(), 'rb') as f:
        zf = zipfile.ZipFile(io.BytesIO(f.read()))
    assert bodies[-1]['include_prompts'] is False
    assert bodies[-1]['runs'] == 'latest'
    assert _run_dirs(zf) == ['run_0002']
    prov = json.loads(zf.read('provenance.json'))
    assert all('prompt' not in s for s in prov['steps'])


def test_starred_option_exports_only_starred_runs(page, fake):
    _start(page, fake, 'ex-starred.csv')
    first = page.evaluate(
        "() => document.querySelector('#runList > li').dataset.runId")
    star = f'#runList > li[data-run-id="{first}"] .wb-run-star'
    page.click(star)
    page.wait_for_selector(star + '[aria-pressed="true"]')
    page.click('#exportBtn')
    page.wait_for_selector('#exportModal:not(.hidden)')
    page.select_option('#exportRuns', 'starred')
    with page.expect_download() as info:
        page.click('#exportDownloadBtn')
    with open(info.value.path(), 'rb') as f:
        zf = zipfile.ZipFile(io.BytesIO(f.read()))
    assert _run_dirs(zf) == ['run_' + first[1:]]


def test_export_dialog_is_keyboard_reachable_and_closes_on_escape(page, fake):
    _start(page, fake, 'ex-keys.csv', runs=1)
    page.click('#exportBtn')
    page.wait_for_selector('#exportModal:not(.hidden)')
    # The dialog moves focus to its first control (not the close button).
    page.wait_for_function(
        "() => document.getElementById('exportModal').contains(document.activeElement)")
    assert page.evaluate('() => document.activeElement.id') == 'exportRuns'
    page.keyboard.press('Escape')
    page.wait_for_selector('#exportModal.hidden', state='attached')
    assert page.evaluate('() => document.activeElement.id') == 'exportBtn'


def test_export_dialog_fits_a_phone_screen(browser, e2e_server, fake):
    p = _new_page(browser, e2e_server['url'], width=390, height=844)
    try:
        _start(p, fake, 'ex-narrow.csv', runs=1)
        p.click('#exportBtn')
        p.wait_for_selector('#exportModal:not(.hidden)')
        width = p.evaluate('() => document.documentElement.scrollWidth')
        assert width <= 390, f'page is {width}px wide at 390px with the dialog'
        box = p.locator('#exportDownloadBtn').bounding_box()
        assert box and box['x'] >= 0 and box['x'] + box['width'] <= 390
    finally:
        p.context.close()


# ---------------------------------------------------------------------------
# Review screenshots (opt-in)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get('STATLEE_SHOTS_DIR'),
                    reason='set STATLEE_SHOTS_DIR to write review screenshots')
@pytest.mark.parametrize('scheme', ['light', 'dark'])
@pytest.mark.parametrize('width,height', [(1440, 900), (390, 844)])
def test_export_screenshots(browser, e2e_server, fake, scheme, width, height):
    out = os.environ['STATLEE_SHOTS_DIR']
    os.makedirs(out, exist_ok=True)
    p = _new_page(browser, e2e_server['url'], width=width, height=height,
                  scheme=scheme)
    try:
        if scheme == 'dark':
            p.evaluate("() => { if (!document.documentElement.classList.contains('dark')) CC.toggleTheme(); }")
        _start(p, fake, f'ex-shot-{scheme}-{width}.csv', runs=1)
        p.click('#exportBtn')
        p.wait_for_selector('#exportModal:not(.hidden)')
        p.wait_for_timeout(400)   # fade-in animation
        tag = f'{scheme}-{width}'
        p.screenshot(path=os.path.join(out, f'export-dialog-{tag}.png'))
        p.uncheck('#exportIncludePrompts')
        p.select_option('#exportRuns', 'starred')
        p.locator('#exportModal > div').screenshot(
            path=os.path.join(out, f'export-options-{tag}.png'))
    finally:
        p.context.close()
