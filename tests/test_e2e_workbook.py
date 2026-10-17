"""Browser checks for the SP1 session workbook UI (2026-10-08).

Same harness as ``test_e2e_ui.py`` (live app on a free localhost port, every
model call answered by the deterministic ``FakeLLMService``, Chromium through
Playwright): $0, no real provider. Skips cleanly without Playwright.

Set ``STATLEE_SHOTS_DIR`` to also write review screenshots of the run rail and
the report checklist (light/dark, 1440px/390px).
"""
import json
import os

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

PLOT_SCRIPT = (
    "import matplotlib\n"
    "matplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "plt.plot([1, 2, 3])\n"
    "plt.savefig('plot_1.png')\n"
    "print('plotted output')\n"
)


def _script(fake, code):
    fake.set('draft', code)
    fake.set('validation', code)


def two_runs(page, fake, name):
    """Upload, then generate and run twice with distinct scripts, outputs and
    interpretations. Returns the run ids, oldest first."""
    upload(page, name, CSV_A + '77,1,A\n')
    skip_codebook(page)
    _script(fake, "print('first run output')")
    fake.set('interpret', 'FIRST interpretation')
    generate(page, 'First question about age')
    run_script(page)
    _script(fake, "print('second run output')")
    fake.set('interpret', 'SECOND interpretation')
    generate(page, 'Second question about income')
    run_script(page)
    page.wait_for_function("() => document.querySelectorAll('#runList > li').length === 2")
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('#runList > li')).map(li => li.dataset.runId)")


def server_runs(page):
    return page.evaluate("() => fetch('/runs').then(r => r.json())")


def row(run_id):
    return f'#runList > li[data-run-id="{run_id}"]'


# ---------------------------------------------------------------------------
# The spec's e2e: two runs -> both listed -> star -> report with both selected
# ---------------------------------------------------------------------------

def test_two_runs_star_and_report_with_both_selected(page, fake):
    first, second = two_runs(page, fake, 'wb-report.csv')
    assert first != second

    # Numbered, named by the question, the live run marked current.
    assert page.inner_text(row(first) + ' .wb-run-num').strip() == '1'
    assert page.inner_text(row(second) + ' .wb-run-num').strip() == '2'
    assert 'First question about age' in page.inner_text(row(first))
    assert page.get_attribute(row(second) + ' .wb-run-open', 'aria-current') == 'true'
    assert page.get_attribute(row(first) + ' .wb-run-open', 'aria-current') is None

    page.click('#tabResults')
    star = row(first) + ' .wb-run-star'
    assert page.get_attribute(star, 'aria-pressed') == 'false'
    page.click(star)
    page.wait_for_selector(star + '[aria-pressed="true"]')
    starred = {r['run_id']: r['starred'] for r in server_runs(page)['runs']}
    assert starred == {first: True, second: False}

    page.click('#tabReport')
    boxes = page.locator('#reportRunList input[type="checkbox"]')
    assert boxes.count() == 2
    assert boxes.nth(0).is_checked(), 'a starred run is pre-checked'
    assert boxes.nth(1).is_checked(), 'the latest run is checked by default'

    bodies = _capture_bodies(page, '/generate_report')
    page.click('#reportGenerateBtn')
    page.wait_for_function(
        "() => document.getElementById('reportEditor').value.length > 0"
        " && !document.getElementById('reportGenerateBtn').disabled")
    assert bodies and bodies[-1]['run_ids'] == [first, second]


def test_report_checklist_defaults_to_latest_and_respects_unchecking(page, fake):
    first, second = two_runs(page, fake, 'wb-latest.csv')
    page.click('#tabReport')
    boxes = page.locator('#reportRunList input[type="checkbox"]')
    assert not boxes.nth(0).is_checked(), 'an older unstarred run is not pre-checked'
    assert boxes.nth(1).is_checked()

    boxes.nth(1).uncheck()
    page.wait_for_timeout(100)
    page.click('#reportGenerateBtn')
    page.wait_for_selector('.cc-toast.error')
    assert 'at least one run' in page.locator('.cc-toast.error').last.inner_text()


# ---------------------------------------------------------------------------
# Viewing a saved run is read-only until "Load script"
# ---------------------------------------------------------------------------

def test_view_older_run_is_read_only_until_load_script(page, fake):
    first, second = two_runs(page, fake, 'wb-view.csv')
    page.click('#tabResults')
    assert not page.is_visible('#runViewBar')
    editor_before = page.evaluate('() => CC.getCode()')
    assert 'second run output' in editor_before

    page.click(row(first) + ' .wb-run-open')
    page.wait_for_selector('#runViewBar:not(.hidden)')
    assert 'first run output' in page.inner_text('#runOutput')
    assert 'FIRST interpretation' in page.inner_text('#interpretationOutput')
    assert 'First question about age' in page.inner_text('#runViewQuestion')
    assert page.get_attribute(row(first) + ' .wb-run-open', 'aria-current') == 'true'
    assert page.evaluate('() => CC.getCode()') == editor_before, 'viewing changed the editor'
    assert page.is_visible('#runBackBtn')
    assert not page.is_visible('#runInheritNote')

    # Converse grounds on the run being viewed.
    bodies = _capture_bodies(page, '/converse')
    page.click('#tabConverse')
    page.fill('#chatInput', 'What does this mean?')
    page.press('#chatInput', 'Enter')
    page.wait_for_function('() => CC.state.converseHistory.length === 2')
    assert bodies[-1]['run_id'] == first
    assert 'first run output' in bodies[-1]['code']

    page.click('#tabResults')
    page.click('#runLoadScriptBtn')
    page.wait_for_function("() => CC.getCode().includes('first run output')")
    assert page.get_attribute('#tabGenerate', 'aria-selected') == 'true'

    # Back to latest shows the newest run again.
    page.click('#tabResults')
    page.click('#runBackBtn')
    page.wait_for_function(
        "() => document.getElementById('runOutput').innerText.includes('second run output')")
    assert 'SECOND interpretation' in page.inner_text('#interpretationOutput')


def test_a_new_run_leaves_the_saved_run_view(page, fake):
    first, second = two_runs(page, fake, 'wb-leave.csv')
    page.click('#tabResults')
    page.click(row(first) + ' .wb-run-open')
    page.wait_for_selector('#runViewBar:not(.hidden)')
    page.click('#tabGenerate')
    run_script(page)
    page.wait_for_function("() => document.querySelectorAll('#runList > li').length === 3")
    assert not page.is_visible('#runViewBar')
    third = page.evaluate("() => document.querySelector('#runList > li:last-child').dataset.runId")
    assert page.get_attribute(row(third) + ' .wb-run-open', 'aria-current') == 'true'


def test_interpret_an_older_run_sends_its_run_id(page, fake):
    upload(page, 'wb-interp.csv', CSV_A + '78,1,A\n')
    skip_codebook(page)
    _script(fake, "print('uninterpreted output')")
    generate(page, 'Question one')
    # The first run's interpretation never reaches the server.
    page.route('**/interpret', lambda route: route.abort())
    page.click('#runBtn')
    page.wait_for_function(
        "() => CC.state.lastRun && !document.getElementById('runBtn').disabled"
        " && document.getElementById('interpretationOutput').innerText.includes('Network error')")
    page.unroute('**/interpret')
    _script(fake, "print('second output')")
    generate(page, 'Question two')
    run_script(page)
    page.wait_for_function("() => document.querySelectorAll('#runList > li').length === 2")
    first = page.evaluate("() => document.querySelector('#runList > li').dataset.runId")

    page.click(row(first) + ' .wb-run-open')
    page.wait_for_selector('#runInterpretBtn:not(.hidden)')
    fake.set('interpret', 'LATE interpretation')
    bodies = _capture_bodies(page, '/interpret')
    page.click('#runInterpretBtn')
    page.wait_for_function(
        "() => document.getElementById('interpretationOutput').innerText.includes('LATE interpretation')")
    assert bodies[-1]['run_id'] == first
    assert not page.is_visible('#runInterpretBtn')
    stored = page.evaluate(f"() => fetch('/runs/{first}').then(r => r.json())")
    assert 'LATE interpretation' in stored['interpretation']
    # The live run's interpretation is untouched.
    assert 'LATE' not in page.evaluate('() => CC.state.latestInterpretation')


def test_inherited_question_is_flagged(page, fake):
    upload(page, 'wb-inherit.csv', CSV_A + '79,1,A\n')
    skip_codebook(page)
    _script(fake, "print('generated')")
    generate(page, 'Original question')
    run_script(page)
    # An edited script runs under the last generation's question.
    page.evaluate("() => CC.setCode(\"print('edited by hand')\")")
    page.click('#tabGenerate')
    run_script(page)
    page.wait_for_function("() => document.querySelectorAll('#runList > li').length === 2")
    edited = page.evaluate("() => document.querySelector('#runList > li:last-child').dataset.runId")
    first = page.evaluate("() => document.querySelector('#runList > li').dataset.runId")
    # Make the edited run an older one, then open each from the rail.
    _script(fake, "print('third')")
    generate(page, 'Third question')
    run_script(page)
    page.wait_for_function("() => document.querySelectorAll('#runList > li').length === 3")
    page.click(row(first) + ' .wb-run-open')
    page.wait_for_selector('#runViewBar:not(.hidden)')
    assert not page.is_visible('#runInheritNote'), 'a generated run is not flagged'
    page.click(row(edited) + ' .wb-run-open')
    page.wait_for_function("() => document.getElementById('runOutput').innerText.includes('edited by hand')")
    page.wait_for_selector('#runViewBar:not(.hidden)')
    assert page.is_visible('#runInheritNote')
    note = page.inner_text('#runInheritNote')
    assert 'carried over' in note and '\u2014' not in note


# ---------------------------------------------------------------------------
# Rename inline
# ---------------------------------------------------------------------------

def test_rename_inline_saves_and_escape_cancels(page, fake):
    first, second = two_runs(page, fake, 'wb-rename.csv')
    page.click('#tabResults')
    page.click(row(first) + ' .wb-run-rename')
    field = row(first) + ' .wb-run-name-input'
    page.wait_for_selector(field)
    page.fill(field, 'Baseline <b>model</b>')
    page.press(field, 'Enter')
    page.wait_for_function(
        f"() => document.querySelector('{row(first)} .wb-run-name').textContent === 'Baseline <b>model</b>'")
    assert page.locator(row(first) + ' b').count() == 0, 'a run name was rendered as HTML'
    names = {r['run_id']: r['name'] for r in server_runs(page)['runs']}
    assert names[first] == 'Baseline <b>model</b>'
    # Focus comes back to the rename button.
    assert page.evaluate(
        f"() => document.activeElement === document.querySelector('{row(first)} .wb-run-rename')")
    # The report checklist uses the name too.
    assert 'Baseline <b>model</b>' in page.inner_text('#reportRunList')

    page.click(row(second) + ' .wb-run-rename')
    page.fill(row(second) + ' .wb-run-name-input', 'Never saved')
    page.press(row(second) + ' .wb-run-name-input', 'Escape')
    page.wait_for_selector(row(second) + ' .wb-run-name-input', state='detached')
    assert 'Never saved' not in page.inner_text(row(second))
    assert server_runs(page)['runs'][1]['name'] is None


def test_clearing_a_run_name_restores_the_question_label(page, fake):
    first, _second = two_runs(page, fake, 'wb-clear.csv')
    page.click('#tabResults')
    field = row(first) + ' .wb-run-name-input'
    page.click(row(first) + ' .wb-run-rename')
    page.fill(field, 'Named for now')
    page.press(field, 'Enter')
    page.wait_for_function(
        f"() => document.querySelector('{row(first)} .wb-run-name').textContent === 'Named for now'")

    bodies = []
    page.on('request', lambda req: bodies.append(json.loads(req.post_data or '{}'))
            if req.method == 'PATCH' and req.url.endswith('/runs/' + first) else None)
    page.click(row(first) + ' .wb-run-rename')
    page.fill(field, '   ')
    page.press(field, 'Enter')
    page.wait_for_function(
        f"() => document.querySelector('{row(first)} .wb-run-name').textContent"
        " === 'First question about age'")
    assert bodies[-1] == {'name': None}
    names = {r['run_id']: r['name'] for r in server_runs(page)['runs']}
    assert names[first] is None


def test_star_failure_reverts_the_star_and_toasts(page, fake):
    first, _second = two_runs(page, fake, 'wb-star-fail.csv')
    page.click('#tabResults')

    def _patch_fails(route):
        if route.request.method == 'PATCH':
            route.fulfill(status=500, content_type='application/json',
                          body=json.dumps({'error': 'Internal server error.'}))
        else:
            route.continue_()

    page.route('**/runs/' + first, _patch_fails)
    star = row(first) + ' .wb-run-star'
    assert page.get_attribute(star, 'aria-pressed') == 'false'
    page.click(star)
    page.wait_for_selector('.cc-toast.error')
    page.wait_for_selector(star + '[aria-pressed="false"]')
    assert {r['run_id']: r['starred'] for r in server_runs(page)['runs']}[first] is False
    # Not checked by default in the report list either.
    page.click('#tabReport')
    assert not page.locator('#reportRunList input[type="checkbox"]').nth(0).is_checked()


def test_load_script_of_an_r_run_switches_language_and_runs_as_r(page, fake):
    first, _second = two_runs(page, fake, 'wb-r.csv')
    page.click('#tabResults')
    r_code = 'summary(df)\nprint("from R")'

    def _as_r_run(route):
        if route.request.method != 'GET':
            route.continue_()
            return
        run = route.fetch().json()
        run.update(language='R', code=r_code)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(run))

    page.route('**/runs/' + first, _as_r_run)
    page.click(row(first) + ' .wb-run-open')
    page.wait_for_selector('#runViewBar:not(.hidden)')
    page.click('#runLoadScriptBtn')            # the replace confirm is accepted
    page.wait_for_function("() => CC.getCode().includes('from R')")
    assert page.input_value('#languageSelect') == 'R'
    assert page.evaluate('() => CC.state.generated.language') == 'R'

    # Run sends it as R. The sandbox's R is not needed: the reply is canned.
    bodies = _capture_bodies(page, '/run')
    page.route('**/run', lambda route: route.fulfill(
        status=200, content_type='application/json',
        body=json.dumps({'output': 'from R', 'plots': [], 'success': True,
                         'returncode': 0, 'language': 'R'})))
    page.click('#runBtn')
    page.wait_for_function("() => document.getElementById('runOutput').innerText.includes('from R')")
    assert bodies[-1]['language'] == 'R'
    assert bodies[-1]['code'] == r_code


def _with_evicted(evicted):
    """Route handler: the real /run reply, with ``evicted`` replaced."""
    def handler(route):
        reply = route.fetch().json()
        reply['evicted'] = evicted
        route.fulfill(status=200, content_type='application/json',
                      body=json.dumps(reply))
    return handler


EVICTION_TOAST = ".cc-toast.info:has-text('removed to make room')"


def _eviction_toast(page):
    """The eviction toast's text: waits for that toast by its words, so an
    unrelated info toast cannot be read in its place."""
    page.wait_for_selector(EVICTION_TOAST)
    return page.locator(EVICTION_TOAST).last.inner_text()


def test_a_run_that_evicts_a_starred_run_says_so(page, fake):
    """3A A3: /run reports the runs the cap pushed out; a starred one is
    named, so the user learns starring did not keep it."""
    first, _second = two_runs(page, fake, 'wb-evict.csv')
    page.route('**/run', _with_evicted(
        [{'run_id': first, 'name': 'Baseline model', 'starred': True}]))
    page.click('#tabGenerate')
    run_script(page)
    text = _eviction_toast(page)
    assert 'starred run "Baseline model"' in text, text
    assert 'workbook is full' in text, text


def test_a_run_that_evicts_unstarred_runs_counts_them(page, fake):
    first, second = two_runs(page, fake, 'wb-evict2.csv')
    page.route('**/run', _with_evicted(
        [{'run_id': first, 'name': None, 'starred': False},
         {'run_id': second, 'name': None, 'starred': False}]))
    page.click('#tabGenerate')
    run_script(page)
    text = _eviction_toast(page)
    assert '2 older runs were removed' in text, text
    assert 'starred' not in text, text


def test_a_run_with_nothing_evicted_shows_no_toast(page, fake):
    two_runs(page, fake, 'wb-evict3.csv')
    assert page.locator(EVICTION_TOAST).count() == 0


def test_star_tooltip_does_not_promise_the_run_is_kept(page, fake):
    first, _second = two_runs(page, fake, 'wb-star-tip.csv')
    page.click('#tabResults')
    star = row(first) + ' .wb-run-star'
    page.click(star)
    page.wait_for_selector(star + '[aria-pressed="true"]')
    tip = page.get_attribute(star, 'title')
    assert 'report' in tip, tip
    assert 'not' in tip and 'full' in tip, tip


# ---------------------------------------------------------------------------
# History restore brings back output + plots while the run exists
# ---------------------------------------------------------------------------

def test_history_restore_brings_back_output_and_plots(page, fake):
    upload(page, 'wb-hist.csv', CSV_A + '80,1,A\n')
    skip_codebook(page)
    _script(fake, PLOT_SCRIPT)
    generate(page, 'Plot it')
    run_script(page)
    page.wait_for_selector('#plotOutput img')
    run_id = page.evaluate('() => CC.state.lastRun.run_id')
    assert run_id
    entry = page.evaluate("() => JSON.parse(localStorage.getItem('cc_history'))[0]")
    assert entry['run_id'] == run_id
    # Make the browser copy stale, as a trimmed local entry would be.
    page.evaluate("""() => {
        const list = JSON.parse(localStorage.getItem('cc_history'));
        list[0].output = 'stale local copy';
        list.push({prompt: 'gone', code: "print('gone')", language: 'Python',
                   dataset_name: list[0].dataset_name, output: 'text only output',
                   interpretation: 'old words', run_id: 'r0999', ts: Date.now() - 5000});
        localStorage.setItem('cc_history', JSON.stringify(list));
    }""")
    page.evaluate('() => CC.resetAnalysisState()')
    assert page.locator('#plotOutput img').count() == 0

    page.click('#historyBtn')
    page.wait_for_selector('#historyList button')
    page.locator('#historyList button', has_text='Plot it').click()
    page.wait_for_function(
        "() => document.getElementById('runOutput').innerText.includes('plotted output')")
    assert page.locator('#plotOutput img').count() == 1
    assert 'stale local copy' not in page.inner_text('#runOutput')
    assert page.evaluate('() => CC.state.lastRun.plots.length') == 1
    page.wait_for_selector(row(run_id) + ' .wb-run-open[aria-current="true"]', state='attached')
    assert not page.is_visible('#runViewBar')

    # A run that no longer exists restores text only.
    page.click('#historyBtn')
    page.wait_for_selector('#historyList button')
    page.locator('#historyList button', has_text='gone').click()
    page.wait_for_function(
        "() => document.getElementById('runOutput').innerText.includes('text only output')")
    page.wait_for_timeout(300)
    assert page.locator('#plotOutput img').count() == 0


# ---------------------------------------------------------------------------
# Layout: the rail never widens a phone screen
# ---------------------------------------------------------------------------

def test_run_rail_fits_a_narrow_viewport(browser, e2e_server, fake):
    p = _new_page(browser, e2e_server['url'], width=390, height=844)
    try:
        two_runs(p, fake, 'wb-narrow.csv')
        p.evaluate("() => CC.switchTab('results')")
        width = p.evaluate('() => document.documentElement.scrollWidth')
        assert width <= 390, f'page is {width}px wide at 390px with a run rail'
        p.evaluate("() => CC.switchTab('report')")
        width = p.evaluate('() => document.documentElement.scrollWidth')
        assert width <= 390, f'page is {width}px wide at 390px with the report checklist'
    finally:
        p.context.close()


# ---------------------------------------------------------------------------
# Review screenshots (opt-in)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get('STATLEE_SHOTS_DIR'),
                    reason='set STATLEE_SHOTS_DIR to write review screenshots')
@pytest.mark.parametrize('scheme', ['light', 'dark'])
@pytest.mark.parametrize('width,height', [(1440, 900), (390, 844)])
def test_workbook_screenshots(browser, e2e_server, fake, scheme, width, height):
    out = os.environ['STATLEE_SHOTS_DIR']
    os.makedirs(out, exist_ok=True)
    p = _new_page(browser, e2e_server['url'], width=width, height=height, scheme=scheme)
    try:
        if scheme == 'dark':
            p.evaluate("() => { if (!document.documentElement.classList.contains('dark')) CC.toggleTheme(); }")
        first, _second = two_runs(p, fake, f'wb-shot-{scheme}-{width}.csv')
        p.evaluate("() => CC.switchTab('results')")
        p.click(row(first) + ' .wb-run-star')
        p.wait_for_selector(row(first) + ' .wb-run-star[aria-pressed="true"]')
        tag = f'{scheme}-{width}'
        p.screenshot(path=os.path.join(out, f'workbook-rail-{tag}.png'), full_page=True)
        p.click(row(first) + ' .wb-run-open')
        p.wait_for_selector('#runViewBar:not(.hidden)')
        p.screenshot(path=os.path.join(out, f'workbook-view-{tag}.png'), full_page=True)
        p.click(row(first) + ' .wb-run-rename')
        p.screenshot(path=os.path.join(out, f'workbook-rename-{tag}.png'), full_page=True)
        p.keyboard.press('Escape')
        p.evaluate("() => CC.switchTab('report')")
        p.screenshot(path=os.path.join(out, f'workbook-report-{tag}.png'), full_page=True)
    finally:
        p.context.close()


def test_runs_endpoint_is_quiet_on_a_fresh_page(page):
    # A fresh browser has an empty workbook: no rail, no checklist, no errors.
    page.wait_for_function("() => (window.__fetchCalls['/runs'] || 0) >= 1")
    assert not page.is_visible('#runRail')
    assert page.locator('#reportRunList input').count() == 0
    assert json.loads(json.dumps(server_runs(page))) == {'runs': [], 'latest': None}


def test_refresh_leaves_the_view_of_a_run_that_is_gone(page, fake):
    """Final review item 8: when the viewed run leaves the workbook
    (eviction, sign-in), refresh() ends the read-only view."""
    first, second = two_runs(page, fake, 'wb-gone.csv')
    page.click('#tabResults')
    page.click(row(first) + ' .wb-run-open')
    page.wait_for_selector('#runViewBar:not(.hidden)')
    listing = server_runs(page)
    listing['runs'] = [r for r in listing['runs'] if r['run_id'] != first]
    page.route('**/runs', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(listing)))
    page.evaluate('() => CC.workbook.refresh()')
    page.wait_for_selector('#runViewBar', state='hidden')
    assert page.evaluate('() => CC.workbook.viewedRun()') is None
    assert page.evaluate('() => CC.workbook.activeRunId()') != first
