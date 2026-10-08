"""Browser-level checks for the workspace frontend (deep audit D4, 2026-10-08).

The app is served from a background thread on a free localhost port, built the
same way ``conftest.py`` builds it (testing ``Config`` with temp dirs,
``create_app(config)``, then ``llm.set_service(FakeLLMService())``), so every
model call hits the deterministic fake: $0 and no real provider. Chromium is
driven through Playwright.

The module skips cleanly when Playwright or its Chromium build is missing (CI
does not install them). Locally: ``pip install playwright`` and
``python -m playwright install chromium``.

Set ``STATLEE_SHOTS_DIR`` to also write review screenshots of the workspace
(light/dark, desktop/390px) into that directory.
"""
import json
import os
import re
import threading

import pytest

sync_api = pytest.importorskip('playwright.sync_api')

from conftest import FakeLLMService  # noqa: E402

from statlee import llm  # noqa: E402
from statlee.config import Config  # noqa: E402

CSV_A = "age,income,group\n25,30000,A\n40,55000,B\n33,42000,A\n31,39000,B\n"
CSV_B = "score,hours\n70,2\n85,5\n90,6\n"
CODEBOOK_A = json.dumps({'age': 'Continuous', 'income': 'Continuous', 'group': 'Nominal'})

# Installed before any page script runs: counts fetch() calls per path and lets
# a test slow one endpoint down, so "a second submit while the first is still
# in flight" is deterministic instead of a race against a fast fake.
FETCH_SPY = """
(() => {
    window.__fetchCalls = {};
    window.__fetchDelay = {};
    const realFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        const path = new URL(String(input && input.url || input), location.href).pathname;
        window.__fetchCalls[path] = (window.__fetchCalls[path] || 0) + 1;
        const delay = window.__fetchDelay[path];
        if (!delay) return realFetch(input, init);
        return new Promise(r => setTimeout(r, delay)).then(() => realFetch(input, init));
    };
})();
"""


# ---------------------------------------------------------------------------
# Server + browser fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def e2e_server(tmp_path_factory):
    from werkzeug.serving import make_server

    from statlee.app import create_app

    tmp = tmp_path_factory.mktemp('e2e')
    cfg = Config(
        env='testing',
        upload_root=str(tmp / 'uploads'),
        database_url='sqlite:///' + str(tmp / 'e2e.db').replace('\\', '/'),
        flask_secret_key='e2e-secret-key',
        rate_limit_enabled=False,
        csrf_enabled=True,
        accounts_enabled=True,
        require_login=False,
        file_ttl_seconds=7200,
    )
    cfg.validate()
    app = create_app(cfg)
    fake = FakeLLMService()
    llm.set_service(fake)            # same override the conftest app fixture makes
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {'url': f'http://127.0.0.1:{server.server_port}', 'fake': fake}
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope='module')
def browser():
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'playwright unavailable: {exc}')
    try:
        b = pw.chromium.launch()
    except Exception as exc:  # pragma: no cover - browser not installed
        pw.stop()
        pytest.skip(f'playwright chromium not installed: {exc}')
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def fake(e2e_server):
    f = e2e_server['fake']
    f._overrides.clear()
    llm.set_service(f)               # another module may have swapped it
    return f


def _new_page(browser, base_url, width=1440, height=900, scheme='light'):
    ctx = browser.new_context(viewport={'width': width, 'height': height},
                              color_scheme=scheme)

    def _local_only(route):
        # Hermetic: only the app under test. Google Fonts and anything else
        # external are aborted, so the test never depends on the network.
        if route.request.url.startswith(base_url):
            route.continue_()
        else:
            route.abort()

    ctx.route('**/*', _local_only)
    ctx.add_init_script(FETCH_SPY)
    page = ctx.new_page()
    page.on('dialog', lambda d: d.accept())   # window.confirm on dataset switch
    page.goto(base_url + '/')
    page.wait_for_function('() => window.CC && CC.editor')
    return page


@pytest.fixture
def page(browser, e2e_server, fake):
    p = _new_page(browser, e2e_server['url'])
    yield p
    p.context.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def upload(page, name, content):
    page.set_input_files('#fileInput', files=[{
        'name': name, 'mimeType': 'text/csv', 'buffer': content.encode('utf-8')}])


def wait_codebook_prompt(page):
    page.wait_for_selector('#codebookPromptOverlay:not(.hidden)')


def skip_codebook(page):
    wait_codebook_prompt(page)
    page.click('#codebookNoBtn')
    page.click('#surveySkipBtn')
    wait_generate_enabled(page)


def wait_generate_enabled(page):
    page.wait_for_function("() => !document.getElementById('generateBtn').disabled")


def generate(page, prompt='Describe the data'):
    page.fill('#promptInput', prompt)
    page.click('#generateBtn')
    page.wait_for_selector('#runSection:not(.hidden)')


def run_script(page):
    page.click('#runBtn')
    page.wait_for_function(
        "() => CC.state.lastRun && CC.state.latestInterpretation"
        " && !document.getElementById('runBtn').disabled")


def calls(page, path):
    return page.evaluate(f"() => window.__fetchCalls[{path!r}] || 0")


def body_has(page, cls):
    return page.evaluate(f"() => document.body.classList.contains({cls!r})")


# ---------------------------------------------------------------------------
# D4-1: Escape on the codebook prompt = skip, never a stuck workspace
# ---------------------------------------------------------------------------

def test_escape_on_codebook_prompt_continues_the_pipeline(page):
    upload(page, 'a.csv', CSV_A)
    wait_codebook_prompt(page)
    page.keyboard.press('Escape')
    page.wait_for_selector('#codebookPromptOverlay', state='hidden')
    # The pipeline must run on its own: Generate re-enables, suggestions land.
    wait_generate_enabled(page)
    page.wait_for_selector('#suggestionsList button')
    state = page.get_attribute('#pipelineChecklist [data-step="codebook_link"]', 'data-state')
    assert state == 'skipped'


# ---------------------------------------------------------------------------
# D4-2: a failed upload keeps the current dataset usable
# ---------------------------------------------------------------------------

def test_failed_upload_keeps_previous_dataset(page, fake):
    fake.set('classify', CODEBOOK_A)
    # Distinct bytes: the server caches classification by content, and the
    # earlier tests' identical CSV_A was classified with the fake's empty {}.
    upload(page, 'a.csv', CSV_A + '29,36000,A\n')
    skip_codebook(page)
    page.wait_for_selector('#codebookContainer:not(.hidden)')
    page.wait_for_selector('#changelogPanel:not(.hidden)')
    before = page.evaluate('() => CC.state.filename')
    assert before

    upload(page, 'broken.exe', 'not a dataset')
    page.wait_for_selector('.cc-toast.error')
    page.wait_for_timeout(300)

    assert page.evaluate('() => CC.state.filename') == before
    assert page.is_visible('#codebookContainer'), 'codebook of the loaded dataset vanished'
    assert page.is_visible('#changelogPanel'), 'cleaning panel of the loaded dataset vanished'
    assert page.evaluate("() => !document.getElementById('generateBtn').disabled"), \
        'Generate stayed disabled after a failed upload'


def test_upload_network_error_is_reported(page):
    page.context.route('**/upload', lambda route: route.abort())
    upload(page, 'a.csv', CSV_A)
    page.wait_for_selector('.cc-toast.error')


# ---------------------------------------------------------------------------
# D4-3: a new dataset resets the analysis built on the previous one
# ---------------------------------------------------------------------------

def test_new_upload_resets_previous_analysis(page):
    upload(page, 'a.csv', CSV_A)
    skip_codebook(page)
    generate(page)
    run_script(page)
    page.click('#tabConverse')
    page.fill('#chatInput', 'What does this mean?')
    page.press('#chatInput', 'Enter')
    page.wait_for_function('() => CC.state.converseHistory.length === 2')

    upload(page, 'b.csv', CSV_B)
    skip_codebook(page)

    assert page.evaluate('() => CC.getCode()') == ''
    assert not page.is_visible('#runSection')
    assert page.evaluate('() => CC.state.lastRun') is None
    assert page.evaluate('() => CC.state.latestInterpretation') == ''
    assert page.evaluate('() => CC.state.report') == ''
    assert page.inner_text('#interpretationOutput').strip() == ''
    assert page.inner_text('#runOutput').strip() == ''
    assert page.evaluate("() => document.querySelectorAll('#chatLog > :not(#converseEmptyState)').length") == 0
    for flag in ('cc-no-code', 'cc-no-run', 'cc-no-chat'):
        assert body_has(page, flag), f'{flag} not restored after a dataset switch'


# ---------------------------------------------------------------------------
# D4-4: sanitized model markdown cannot drive the app
# ---------------------------------------------------------------------------

HOSTILE_MD = (
    '<div data-action="toggleTheme" data-arg="x" style="position:fixed;inset:0">x</div>'
    '<form action="/logout"><input name="q"><button>Go</button><textarea></textarea>'
    '<select><option>1</option></select></form>'
    '<iframe src="/"></iframe><object data="/"></object>'
    '<a id="generateBtn" name="CC" class="btn-primary" href="https://example.org/x">link</a>\n\n'
    '[md link](https://example.org/y)'
)


def test_render_markdown_strips_active_content(page):
    html = page.evaluate('(md) => CC.renderMarkdown(md)', HOSTILE_MD)
    lowered = html.lower()
    for bad in ('data-action', 'data-arg', 'style=', '<form', '<input', '<button',
                '<textarea', '<select', '<iframe', '<object', ' id=', ' name=',
                'class='):
        assert bad not in lowered, f'{bad!r} survived sanitization: {html}'
    links = page.evaluate(
        """(md) => { const d = document.createElement('div');
                     d.innerHTML = CC.renderMarkdown(md);
                     return [...d.querySelectorAll('a')].map(a => [a.target, a.rel]); }""",
        HOSTILE_MD)
    assert links and all(t == '_blank' and 'noopener' in r and 'noreferrer' in r
                         for t, r in links), links


def test_data_action_dispatch_is_allowlisted_and_ignores_model_output(page):
    page.evaluate("""() => {
        window.__hit = 0;
        window.evilGlobal = () => { window.__hit += 1; };
        const raw = document.createElement('button');
        raw.id = 'rawEvil'; raw.setAttribute('data-action', 'evilGlobal');
        raw.textContent = 'raw';
        document.body.appendChild(raw);
        // A real handler name, but inside rendered model output.
        const bubble = document.createElement('div');
        bubble.className = 'markdown-body';
        bubble.innerHTML = '<span id="inModel" data-action="toggleTheme">t</span>';
        document.body.appendChild(bubble);
    }""")
    dark_before = page.evaluate("() => document.documentElement.classList.contains('dark')")
    page.click('#rawEvil')
    page.click('#inModel')
    assert page.evaluate('() => window.__hit') == 0, 'non-allowlisted global was dispatched'
    assert page.evaluate("() => document.documentElement.classList.contains('dark')") == dark_before, \
        'a data-action inside model output was dispatched'
    # The legitimate chrome still works through the same listener.
    page.click('#tabData')
    assert page.is_visible('#contentData')


# ---------------------------------------------------------------------------
# D4-5: a failed generation never destroys the user's script
# ---------------------------------------------------------------------------

def test_refused_generation_keeps_hand_edits(page, fake):
    upload(page, 'a.csv', CSV_A)
    skip_codebook(page)
    page.evaluate("() => CC.setCode('my_edits = 1  # hand written')")
    fake.block('off-topic request')
    page.fill('#promptInput', 'please do something')
    page.click('#generateBtn')
    page.wait_for_selector('.cc-toast.error')
    wait_generate_enabled(page)
    assert page.evaluate('() => CC.getCode()') == 'my_edits = 1  # hand written'


def test_stream_error_restores_previous_script(page, fake):
    upload(page, 'a.csv', CSV_A)
    skip_codebook(page)
    page.evaluate("() => CC.setCode('keep_me = 2')")
    fake.block_code('network access')
    page.fill('#promptInput', 'regress income on age')
    page.click('#generateBtn')
    page.wait_for_selector('.cc-toast.error')
    wait_generate_enabled(page)
    assert page.evaluate('() => CC.getCode()') == 'keep_me = 2'
    assert not page.is_visible('#runSection')


def test_stream_without_terminal_event_is_an_error(page):
    upload(page, 'a.csv', CSV_A)
    skip_codebook(page)
    page.evaluate("() => CC.setCode('orig = 3')")
    # D4-7: a proxy cut mid-stream: deltas, then EOF with no done/error.
    page.context.route('**/chat', lambda route: route.fulfill(
        status=200, content_type='text/event-stream',
        body='data: {"type": "phase", "phase": "drafting"}\n\n'
             'data: {"type": "delta", "text": "partial = "}\n\n'))
    page.fill('#promptInput', 'anything')
    page.click('#generateBtn')
    page.wait_for_selector('.cc-toast.error')
    wait_generate_enabled(page)
    assert page.evaluate('() => CC.getCode()') == 'orig = 3'
    state = page.get_attribute('#pipelineChecklist [data-step="generate"]', 'data-state')
    assert state == 'failed'


# ---------------------------------------------------------------------------
# D4-6: no second submit while the first is in flight
# ---------------------------------------------------------------------------

def test_converse_ignores_send_while_streaming(page):
    page.evaluate("() => { window.__fetchDelay['/converse'] = 1200; }")
    page.click('#tabConverse')
    page.fill('#chatInput', 'first question')
    page.press('#chatInput', 'Enter')
    page.fill('#chatInput', 'second question')
    page.press('#chatInput', 'Enter')
    page.click('#chatSend', force=True)      # force: it is (rightly) disabled once fixed
    page.wait_for_function('() => CC.state.converseHistory.length === 2')
    page.wait_for_timeout(300)
    assert calls(page, '/converse') == 1
    # The unsent text is kept, not swallowed.
    assert page.input_value('#chatInput') == 'second question'


def test_wrangle_ignores_enter_while_applying(page):
    upload(page, 'a.csv', CSV_A)
    skip_codebook(page)
    page.evaluate("() => { window.__fetchDelay['/wrangle'] = 1200; }")
    page.fill('#wrangleInput', 'drop missing rows')
    page.press('#wrangleInput', 'Enter')
    page.fill('#wrangleInput', 'and also this')
    page.press('#wrangleInput', 'Enter')
    page.wait_for_selector('.cc-toast.success')
    page.wait_for_timeout(300)
    assert calls(page, '/wrangle') == 1
    assert page.evaluate("() => document.querySelectorAll('#changelogList .pending').length") == 0


# ---------------------------------------------------------------------------
# D4-8: Fix with AI is only offered for a real traceback
# ---------------------------------------------------------------------------

def test_run_guard_refusal_offers_retry_but_not_fix(page, fake):
    upload(page, 'a.csv', CSV_A)
    skip_codebook(page)
    generate(page)
    page.evaluate("() => CC.setCode(CC.getCode() + '\\nprint(1)')")   # an edit: re-moderated
    fake.block_code('not allowed')
    page.click('#runBtn')
    page.wait_for_selector('#pipelineChecklist [data-step="execute"][data-state="failed"]')
    assert page.locator('#pipelineChecklist .step-retry').count() == 1
    assert page.locator('#pipelineChecklist .step-fix').count() == 0


# ---------------------------------------------------------------------------
# D4-12: account flows
# ---------------------------------------------------------------------------

def _open_account(page):
    page.click('#accountBtn')
    page.wait_for_selector('#accountModal:not(.hidden)')


def test_register_202_shows_check_email_and_does_not_reload(page):
    page.context.route('**/register', lambda route: route.fulfill(
        status=202, content_type='application/json',
        body=json.dumps({'status': 'verification_required',
                         'message': 'Account created. Check your email to confirm it.'})))
    page.evaluate('() => { window.__noReload = 1; }')
    _open_account(page)
    page.fill('#accEmail', 'new@example.edu')
    page.fill('#accPassword', 'longenough1')
    page.click('#accRegisterBtn')
    page.wait_for_selector('#accError:not(.hidden)')
    assert 'Check your email' in page.inner_text('#accError')
    page.wait_for_timeout(500)
    assert page.evaluate('() => window.__noReload') == 1, 'page reloaded and hid the message'


def test_forgot_password_posts_reset_request(page):
    _open_account(page)
    page.fill('#accEmail', 'someone@example.edu')
    page.click('#accForgotBtn')
    page.wait_for_selector('#accError:not(.hidden)')
    assert 'reset link' in page.inner_text('#accError')
    assert calls(page, '/request_password_reset') == 1


def test_enter_in_password_field_signs_in(page):
    _open_account(page)
    page.fill('#accEmail', 'nobody@example.edu')
    page.fill('#accPassword', 'wrong-password')
    page.press('#accPassword', 'Enter')
    page.wait_for_selector('#accError:not(.hidden)')
    assert 'Invalid email or password' in page.inner_text('#accError')
    assert calls(page, '/login') == 1


# ---------------------------------------------------------------------------
# D4-13 / D4-14: focus is visible, trapped in dialogs and restored on close
# ---------------------------------------------------------------------------

def test_tab_focus_ring_is_visible(page):
    page.focus('#tabData')
    page.keyboard.press('ArrowLeft')         # roving focus between tabs
    page.keyboard.press('ArrowRight')
    # The tabs carry Tailwind's transition-all, which also eases the outline
    # in; read the settled value, not a mid-transition one.
    page.wait_for_timeout(600)
    style = page.evaluate("""() => { const s = getComputedStyle(document.activeElement);
                                     return [document.activeElement.id, s.outlineStyle,
                                             s.outlineWidth, s.outlineColor]; }""")
    assert style[0] == 'tabData'
    # Tailwind's focus:outline-none leaves a 2px *transparent* outline, so the
    # colour is the real test of visibility.
    assert style[1] != 'none' and style[2] != '0px', style
    assert style[3] == 'rgb(91, 83, 230)', style       # light --indigo


def test_tabs_expose_selected_state(page):
    page.click('#tabResults')
    assert page.get_attribute('#tabResults', 'aria-selected') == 'true'
    assert page.get_attribute('#tabGenerate', 'aria-selected') == 'false'
    assert page.get_attribute('#contentResults', 'role') == 'tabpanel'


def test_dialog_traps_and_restores_focus(page):
    page.focus('#catalogBtn')
    page.keyboard.press('Enter')
    page.wait_for_selector('#catalogModal:not(.hidden)')
    for _ in range(40):
        page.keyboard.press('Tab')
        inside = page.evaluate(
            "() => document.getElementById('catalogModal').contains(document.activeElement)")
        assert inside, 'focus escaped the open dialog'
    page.keyboard.press('Escape')
    page.wait_for_selector('#catalogModal', state='hidden')
    assert page.evaluate('() => document.activeElement.id') == 'catalogBtn'


# ---------------------------------------------------------------------------
# D4-18: re-picking the same file uploads again
# ---------------------------------------------------------------------------

def test_file_input_is_cleared_after_upload(page):
    upload(page, 'a.csv', CSV_A)
    skip_codebook(page)
    assert page.input_value('#fileInput') == ''


# ---------------------------------------------------------------------------
# D4-16: copy is honest about provider and sandbox
# ---------------------------------------------------------------------------

def test_disclosure_and_provider_copy(page):
    html = page.content()
    assert 'Execute Script Locally' not in html
    assert 'What is sent to' in html
    footer = page.text_content('footer')      # not inner_text: the footer is CSS-uppercased
    assert 'Google Gemini' in footer          # the configured (default) provider, derived from config


# ---------------------------------------------------------------------------
# D4-17: no horizontal page scroll on a phone-width screen
# ---------------------------------------------------------------------------

def test_narrow_viewport_has_no_horizontal_scroll(browser, e2e_server, fake):
    p = _new_page(browser, e2e_server['url'], width=390, height=844)
    try:
        width = p.evaluate('() => document.documentElement.scrollWidth')
        assert width <= 390, f'page is {width}px wide at a 390px viewport'
        box = p.locator('#rightContent').bounding_box()
        assert box and box['width'] >= 340, f'workspace squeezed to {box}'
        # A loaded dataset reveals the codebook and data-cleaning panels,
        # whose toolbars must wrap rather than widen the page.
        upload(p, 'narrow.csv', CSV_A + '61,72000,A\n')
        skip_codebook(p)
        p.wait_for_selector('#changelogPanel:not(.hidden)')
        width = p.evaluate('() => document.documentElement.scrollWidth')
        assert width <= 390, f'page is {width}px wide at 390px once a dataset is loaded'
    finally:
        p.context.close()


# ---------------------------------------------------------------------------
# Review screenshots (opt-in)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get('STATLEE_SHOTS_DIR'),
                    reason='set STATLEE_SHOTS_DIR to write review screenshots')
@pytest.mark.parametrize('scheme', ['light', 'dark'])
@pytest.mark.parametrize('width,height', [(1440, 900), (390, 844)])
def test_workspace_screenshots(browser, e2e_server, fake, scheme, width, height):
    out = os.environ['STATLEE_SHOTS_DIR']
    os.makedirs(out, exist_ok=True)
    fake.set('classify', CODEBOOK_A)
    p = _new_page(browser, e2e_server['url'], width=width, height=height, scheme=scheme)
    try:
        if scheme == 'dark':
            p.evaluate("() => { if (!document.documentElement.classList.contains('dark')) CC.toggleTheme(); }")
        tag = f'{scheme}-{width}'
        p.screenshot(path=os.path.join(out, f'workspace-empty-{tag}.png'), full_page=True)
        upload(p, 'survey.csv', CSV_A + '52,61000,B\n')   # uncached: real codebook chips
        skip_codebook(p)
        generate(p)
        run_script(p)
        p.screenshot(path=os.path.join(out, f'workspace-results-{tag}.png'), full_page=True)
    finally:
        p.context.close()


# ---------------------------------------------------------------------------
# S4: the frontend against the S1-B backend contract (FIX-S1B.md, 17 changes)
# ---------------------------------------------------------------------------

def _capture_bodies(page, path):
    """Record the JSON body of every request to `path`."""
    bodies = []

    def _on_request(req):
        if req.url.endswith(path) and req.method == 'POST':
            try:
                bodies.append(json.loads(req.post_data or '{}'))
            except ValueError:
                pass
    page.on('request', _on_request)
    return bodies


def test_non_ascii_upload_shows_original_name_and_uses_stored_name(page):
    # Contract 3: a non-Latin name is stored as upload_<8hex>.csv; the UI must
    # show the user's name and send the stored one on every later call.
    pages = _capture_bodies(page, '/data_page')
    classify = _capture_bodies(page, '/classify_variables')
    suggest = _capture_bodies(page, '/suggest')
    upload(page, 'Опрос.csv', CSV_A + '45,50000,B\n')
    skip_codebook(page)
    page.wait_for_selector('#suggestionsList button')
    stored = page.evaluate('() => CC.state.filename')
    assert re.fullmatch(r'upload_[0-9a-f]{8}\.csv', stored), stored
    assert page.evaluate('() => CC.state.originalName') == 'Опрос.csv'
    info = page.locator('#datasetInfo')
    assert info.is_visible()
    assert 'Опрос.csv' in info.inner_text()
    note = page.inner_text('#pipelineChecklist [data-step="load"] .step-note')
    assert 'Опрос.csv' in note
    sent = [b.get('filename') for b in pages + classify + suggest]
    assert sent and all(f == stored for f in sent), sent


def test_upload_warnings_are_surfaced(page):
    # Contract 3: `warnings` (e.g. "only the largest sheet was read").
    import io

    import pandas as pd
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as xw:
        pd.DataFrame({'note': ['readme']}).to_excel(xw, sheet_name='Notes', index=False)
        pd.DataFrame({'x': [1, 2, 3], 'y': [4, 5, 6]}).to_excel(xw, sheet_name='Data', index=False)
    page.set_input_files('#fileInput', files=[{
        'name': 'book.xlsx',
        'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'buffer': buf.getvalue()}])
    wait_codebook_prompt(page)
    warn = page.locator('#datasetWarnings')
    assert warn.is_visible()
    assert '2 sheets' in warn.inner_text()


def test_empty_csv_upload_shows_the_422_reason(page):
    # Contract 4: an empty / ragged CSV is a 422 with a specific message.
    statuses = []
    page.on('response', lambda r: statuses.append(r.status) if r.url.endswith('/upload') else None)
    upload(page, 'empty.csv', '')
    page.wait_for_selector('.cc-toast.error')
    text = page.inner_text('.cc-toast.error')
    assert 422 in statuses
    assert 'empty.csv' in text and 'empty' in text.lower(), text
    assert page.get_attribute('#pipelineChecklist [data-step="load"]', 'data-state') == 'failed'
    assert page.evaluate('() => CC.state.filename') == ''


def test_reupload_same_name_says_history_was_reset(page):
    # Contract 5: re-uploading under the same name replaces it and resets v1.
    upload(page, 'same.csv', CSV_A + '50,1,A\n')
    skip_codebook(page)
    page.fill('#wrangleInput', 'drop missing rows')
    page.press('#wrangleInput', 'Enter')
    page.wait_for_selector('#changelogList .wrangle-turn:not(.pending)')
    upload(page, 'same.csv', CSV_A + '51,2,B\n')
    page.wait_for_selector('.cc-toast.info >> text=history')
    skip_codebook(page)
    assert page.locator('#changelogList .wrangle-turn').count() == 0


def test_null_cells_render_a_missing_marker(page):
    # Contract 8: /data_page sends null for missing / NaN / inf cells.
    upload(page, 'gaps.csv', 'a,b\n1,\n,2\n3,4\n')
    skip_codebook(page)
    page.click('#tabData')
    page.wait_for_selector('#dataTableBody td')
    assert page.locator('#dataTableBody td.cell-missing').count() == 2
    assert 'null' not in page.inner_text('#dataTableBody')


def test_txt_codebook_previews_as_text_not_in_the_pdf_frame(page):
    # Contract 6: a .txt codebook stays .txt; it is shown as text.
    upload(page, 'cb.csv', CSV_A + '62,1,A\n')
    wait_codebook_prompt(page)
    page.set_input_files('#pdfInput', files=[{
        'name': 'codebook.txt', 'mimeType': 'text/plain',
        'buffer': 'age: Age in years\nincome: Возраст <b>bold</b>\n'.encode()}])
    page.wait_for_selector('#codebookTextView:not(.hidden)')
    assert 'Возраст <b>bold</b>' in page.inner_text('#codebookTextView')
    assert not page.is_visible('#pdfViewerFrame')
    src = page.evaluate("() => document.getElementById('pdfViewerFrame').getAttribute('src')")
    assert src in ('', 'about:blank', None), src


def test_suggest_previous_is_capped_at_ten(page):
    # Contract 9: `previous` is limited to 10 items.
    bodies = _capture_bodies(page, '/suggest')
    upload(page, 'sg.csv', CSV_A + '63,1,A\n')
    skip_codebook(page)
    page.wait_for_selector('#suggestionsList button')
    page.evaluate(
        "() => { CC.state.seenSuggestions = Array.from({length: 15}, (_, i) => 'idea ' + i); }")
    page.click('#rerollBtn')
    page.wait_for_function('() => (window.__fetchCalls["/suggest"] || 0) >= 2')
    page.wait_for_selector('#suggestionsList button')
    prev = bodies[-1]['previous']
    assert 0 < len(prev) <= 10, prev
    assert 'Suggestion 1' in prev            # the ideas on screen are always excluded


def test_wrangle_502_malformed_plan_is_explained(page, fake):
    # Contract 10: a malformed model plan is a 502.
    upload(page, 'w502.csv', CSV_A + '64,1,A\n')
    skip_codebook(page)
    fake.set('wrangle', json.dumps(['not', 'a', 'plan']))
    page.fill('#wrangleInput', 'drop missing rows')
    page.press('#wrangleInput', 'Enter')
    page.wait_for_selector('.cc-toast.error')
    text = page.inner_text('.cc-toast.error')
    assert 'rephras' in text.lower(), text
    assert page.input_value('#wrangleInput') == 'drop missing rows'


def test_wrangle_409_reloads_the_changelog_and_tells_the_user(page):
    # Contract 10: another tab's edit landed first -> 409; reload, then say so.
    upload(page, 'w409.csv', CSV_A + '65,1,A\n')
    skip_codebook(page)
    # Another tab applies a cleaning step this page does not know about.
    page.evaluate("""async () => {
        await CC.post('/wrangle', {filename: CC.state.filename, instruction: 'other tab edit'});
    }""")
    assert page.locator('#changelogList .wrangle-turn').count() == 0
    page.context.route('**/wrangle', lambda route: route.fulfill(
        status=409, content_type='application/json',
        body=json.dumps({'error': 'The dataset changed while this transformation was '
                                  'running (another edit, undo or revert landed first). '
                                  'Reload the data and try again.'})))
    before = calls(page, '/data_page')
    page.fill('#wrangleInput', 'my edit')
    page.press('#wrangleInput', 'Enter')
    page.wait_for_selector('.cc-toast.error')
    page.wait_for_function('() => (window.__fetchCalls["/dataset_versions"] || 0) >= 1')
    page.wait_for_selector('#changelogList .wrangle-turn')
    assert 'Dropped rows with missing values' in page.inner_text('#changelogList')
    page.wait_for_function(f'() => (window.__fetchCalls["/data_page"] || 0) > {before}')
    assert 'reloaded' in page.inner_text('.cc-toast.error').lower()
    assert page.input_value('#wrangleInput') == 'my edit'


def test_restored_history_runs_in_a_fresh_session(page):
    # Contract 11: a script restored from history into a new session is
    # re-checked and runs (no more "Generate code before running" 403).
    upload(page, 'hist.csv', CSV_A + '66,1,A\n')
    skip_codebook(page)
    stored = page.evaluate('() => CC.state.filename')
    page.evaluate("""(name) => localStorage.setItem('cc_history', JSON.stringify([{
        prompt: 'old prompt', code: "print('restored run')", language: 'Python',
        dataset_name: name, output: 'old output', interpretation: 'old interpretation',
        ts: Date.now() - 1000}]))""", stored)
    page.click('#historyBtn')
    page.wait_for_selector('#historyList button')
    page.click('#historyList button')
    toast = page.locator('.cc-toast.info').last.inner_text()
    assert 'safety re-check' in toast, toast
    assert 'only runs scripts it generated' not in toast
    page.click('#runBtn')
    page.wait_for_function(
        "() => CC.state.lastRun && !CC.state.lastRun.restored"
        " && !document.getElementById('runBtn').disabled")
    assert page.evaluate('() => CC.state.lastRun.success') is True
    assert 'restored run' in page.inner_text('#runOutput')
    assert page.get_attribute('#pipelineChecklist [data-step="execute"]', 'data-state') == 'done'


def test_restored_script_recheck_outage_is_explained(page):
    # Contract 11: 503 when moderation is unavailable for an unknown script.
    upload(page, 'hist503.csv', CSV_A + '67,1,A\n')
    skip_codebook(page)
    page.evaluate("() => CC.setCode(\"print('pasted')\")")
    page.locator('#runSection').evaluate("el => el.classList.remove('hidden')")
    page.context.route('**/run', lambda route: route.fulfill(
        status=503, content_type='application/json',
        body=json.dumps({'error': 'Could not verify the edited script. Please try again.'})))
    page.click('#runBtn')
    page.wait_for_selector('#pipelineChecklist [data-step="execute"][data-state="failed"]')
    text = page.inner_text('#runOutput')
    assert 'safety re-check' in text and 'unchanged' in text, text
    assert page.locator('#pipelineChecklist .step-retry').count() == 1
    assert page.locator('#pipelineChecklist .step-fix').count() == 0


def test_failed_run_interpretation_is_labelled_a_diagnosis(page, fake):
    # Contract 13: /interpret's `debug` follows the real exit code.
    upload(page, 'fail.csv', CSV_A + '68,1,A\n')
    skip_codebook(page)
    fake.set('draft', "raise ValueError('boom')")
    fake.set('validation', "raise ValueError('boom')")
    generate(page)
    run_script(page)
    assert page.inner_text('#interpretationTitle').strip().lower() == 'diagnosis'
    entry = page.evaluate("() => JSON.parse(localStorage.getItem('cc_history'))[0]")
    assert entry['success'] is False


def test_run_timeout_is_an_executed_failure(page):
    # L9: a /run timeout is a 500 carrying the sandbox's timeout message. The
    # script DID run and the outcome is persisted, so it is interpreted.
    upload(page, 'slow.csv', CSV_A + '69,1,A\n')
    skip_codebook(page)
    generate(page)
    page.context.route('**/run', lambda route: route.fulfill(
        status=500, content_type='application/json',
        body=json.dumps({'error': 'Execution timed out after 30 seconds. '
                                  'Simplify the analysis or reduce the data size.'})))
    page.click('#runBtn')
    page.wait_for_selector('#pipelineChecklist [data-step="execute"][data-state="failed"]')
    assert 'timed out' in page.inner_text('#pipelineChecklist [data-step="execute"] .step-note')
    page.wait_for_function('() => (window.__fetchCalls["/interpret"] || 0) >= 1')
    assert page.evaluate('() => !CC.state.lastRun.notExecuted') is True


def test_export_sends_the_generated_language_and_describes_the_zip(page):
    # Contracts 12 + 15: export under the language the script was approved in,
    # and describe the new zip layout.
    bodies = _capture_bodies(page, '/export')
    upload(page, 'exp.csv', CSV_A + '70,1,A\n')
    skip_codebook(page)
    generate(page)
    page.select_option('#languageSelect', 'R')
    with page.expect_download():
        page.click('#exportBtn')
    assert bodies[-1]['language'] == 'Python'
    page.wait_for_selector('.cc-toast.success')
    assert 'README.txt' in page.inner_text('.cc-toast.success')
    title = page.get_attribute('#exportBtn', 'title')
    assert 'README' in title and 'original' in title


def test_sign_in_keeps_the_loaded_dataset(page):
    # Contract 16: the server adopts the anonymous workspace at sign-in, so the
    # page keeps its dataset instead of reloading into an empty workspace.
    upload(page, 'keep.csv', CSV_A + '71,1,A\n')
    skip_codebook(page)
    stored = page.evaluate('() => CC.state.filename')
    page.evaluate('() => { window.__noReload = 1; }')
    before = calls(page, '/data_page')
    page.click('#accountBtn')
    page.wait_for_selector('#accountModal:not(.hidden)')
    page.fill('#accEmail', f'keeper-{os.getpid()}@example.test')
    page.fill('#accPassword', 'e2e-test-pass-1')
    page.click('#accRegisterBtn')
    page.wait_for_function('() => CC.state.user && CC.state.user.email')
    page.wait_for_selector('#accountModal', state='hidden')
    assert page.evaluate('() => window.__noReload') == 1, 'the page reloaded and lost the workspace'
    assert page.evaluate('() => CC.state.filename') == stored
    page.wait_for_function(f'() => (window.__fetchCalls["/data_page"] || 0) > {before}')
    page.wait_for_timeout(300)
    assert page.locator('.cc-toast.error').count() == 0
    assert page.locator('#dataTableBody td').count() > 0


def test_credits_remaining_refreshes_the_balance(page):
    # Upcoming LLM-layer field: credit-spending replies may carry
    # `credits_remaining`; when present it is the balance to show.
    page.evaluate("""() => { CC_BOOT.billing = true;
        CC.state.user = {email: 'x@example.test', credits: 50}; }""")
    page.context.route('**/method_prompt', lambda route: route.fulfill(
        status=200, content_type='application/json',
        body=json.dumps({'status': 'success', 'prompt': 'p', 'rationale': 'r',
                         'credits_remaining': 41})))
    page.evaluate("() => CC.post('/method_prompt', {})")
    page.wait_for_function("() => CC.state.user.credits === 41")
    assert page.text_content('#accountCredits').strip() == '41 credits'
    page.context.route('**/converse', lambda route: route.fulfill(
        status=200, content_type='text/event-stream',
        body='data: {"type": "delta", "text": "hi"}\n\n'
             'data: {"type": "done", "credits_remaining": 40}\n\n'))
    page.click('#tabConverse')
    page.fill('#chatInput', 'question')
    page.press('#chatInput', 'Enter')
    page.wait_for_function("() => CC.state.user.credits === 40")
    assert page.text_content('#accountCredits').strip() == '40 credits'


# ---------------------------------------------------------------------------
# S5: backend contract follow-ups (FIX-S4.md "backend follow-ups")
# ---------------------------------------------------------------------------


def test_run_timeout_flag_is_an_executed_failure(page):
    # S5: a timeout is a 200 with timed_out and the partial output.
    upload(page, 'slow2.csv', CSV_A + '76,1,A\n')
    skip_codebook(page)
    generate(page)
    page.context.route('**/run', lambda route: route.fulfill(
        status=200, content_type='application/json',
        body=json.dumps({'output': 'fitted 3 of 10 models\n\nExecution timed out after 30 '
                                   'seconds. Code took too long to run.',
                         'plots': [], 'plot': None, 'success': False, 'returncode': -1,
                         'timed_out': True, 'language': 'Python'})))
    page.click('#runBtn')
    page.wait_for_selector('#pipelineChecklist [data-step="execute"][data-state="failed"]')
    assert 'timed out' in page.inner_text('#pipelineChecklist [data-step="execute"] .step-note')
    assert 'fitted 3 of 10 models' in page.inner_text('#runOutput')
    page.wait_for_function('() => (window.__fetchCalls["/interpret"] || 0) >= 1')
    assert page.evaluate('() => CC.state.lastRun.timedOut') is True
    assert page.locator('#pipelineChecklist .step-fix').count() == 0


def test_wrangle_409_takes_headers_from_dataset_versions(page):
    # /dataset_versions now returns the active version's headers, in frame
    # order, so the 409 recovery needs no one-row /data_page probe.
    probes = _capture_bodies(page, '/data_page')
    upload(page, 'w409h.csv', CSV_A + '72,1,A\n')
    skip_codebook(page)
    page.evaluate("""async () => {
        await CC.post('/wrangle', {filename: CC.state.filename, instruction: 'other tab edit'});
    }""")
    page.context.route('**/wrangle', lambda route: route.fulfill(
        status=409, content_type='application/json',
        body=json.dumps({'error': 'The dataset changed while this transformation was '
                                  'running. Reload the data and try again.'})))
    page.fill('#wrangleInput', 'my edit')
    page.press('#wrangleInput', 'Enter')
    page.wait_for_selector('.cc-toast.error')
    page.wait_for_function('() => (window.__fetchCalls["/dataset_versions"] || 0) >= 1')
    page.wait_for_selector('#changelogList .wrangle-turn')
    page.wait_for_timeout(300)
    assert not any(b.get('per_page') == 1 for b in probes), probes


def test_wrangle_409_header_order_follows_the_server(page, fake):
    upload(page, 'w409o.csv', CSV_A + '73,1,A\n')
    skip_codebook(page)
    fake.set('wrangle', json.dumps({'code': "df.insert(0, 'aaa', 1)",
                                    'summary': 'Added aaa first', 'error': None}))
    page.evaluate("""async () => {
        await CC.post('/wrangle', {filename: CC.state.filename, instruction: 'other tab edit'});
    }""")
    page.context.route('**/wrangle', lambda route: route.fulfill(
        status=409, content_type='application/json',
        body=json.dumps({'error': 'The dataset changed. Reload the data and try again.'})))
    page.fill('#wrangleInput', 'my edit')
    page.press('#wrangleInput', 'Enter')
    page.wait_for_selector('.cc-toast.error')
    page.wait_for_function('() => (window.__fetchCalls["/dataset_versions"] || 0) >= 1')
    page.wait_for_timeout(500)
    assert page.evaluate('() => CC.state.headers') == ['aaa', 'age', 'income', 'group']


def test_sign_in_names_a_dataset_the_account_copy_kept(browser, e2e_server, fake):
    # The server now says which anonymous datasets moved and which were
    # skipped because the account already had that name.
    email = f'collide-{os.getpid()}@example.test'
    owner = _new_page(browser, e2e_server['url'])
    try:
        owner.evaluate("""async (email) => {
            await CC.post('/register', {email, password: 'e2e-test-pass-1'});
        }""", email)
        upload(owner, 'coll.csv', CSV_B)               # the account's coll.csv
        owner.wait_for_function("() => CC.state.filename === 'coll.csv'")
    finally:
        owner.context.close()
    anon = _new_page(browser, e2e_server['url'])
    try:
        upload(anon, 'coll.csv', CSV_A + '74,1,A\n')    # an anonymous coll.csv
        skip_codebook(anon)
        _open_account(anon)
        anon.fill('#accEmail', email)
        anon.fill('#accPassword', 'e2e-test-pass-1')
        anon.click('#accLoginBtn')
        anon.wait_for_function('() => CC.state.user && CC.state.user.email')
        anon.wait_for_selector('.cc-toast >> text=not moved')
        text = anon.inner_text('#toastArea')
        assert 'coll.csv' in text and 'account' in text, text
        # The table now shows the account's copy, with its own columns.
        anon.wait_for_function("() => CC.state.headers[0] === 'score'")
    finally:
        anon.context.close()


def test_sign_in_without_collisions_says_the_dataset_moved(page):
    upload(page, 'moved.csv', CSV_A + '75,1,A\n')
    skip_codebook(page)
    _open_account(page)
    page.fill('#accEmail', f'mover-{os.getpid()}@example.test')
    page.fill('#accPassword', 'e2e-test-pass-1')
    page.click('#accRegisterBtn')
    page.wait_for_selector('.cc-toast.success')
    text = page.inner_text('.cc-toast.success')
    assert 'moved into your account' in text, text
    assert 'account copy is kept' not in text, text
