"""Browser checks for the SP2 survey-native codebook UI (2026-10-08).

Same harness as ``test_e2e_ui.py`` (live app on a free localhost port, every
model call answered by the deterministic ``FakeLLMService``, Chromium through
Playwright): $0, no real provider. Skips cleanly without Playwright.

Set ``STATLEE_SHOTS_DIR`` to also write review screenshots of the Codebook tab
(light/dark, 1440px/390px).
"""
import json
import os

import pytest

sync_api = pytest.importorskip('playwright.sync_api')

from test_e2e_ui import (  # noqa: E402,F401  (fixtures are used by name)
    CODEBOOK_A,
    CSV_A,
    _new_page,
    browser,
    e2e_server,
    fake,
    page,
    skip_codebook,
    upload,
)


def row(col):
    return f'#cbList > li.cb-row[data-col="{col}"]'


def open_codebook(page, fake, name, content=None):
    """Upload a dataset (classified by the fake as CODEBOOK_A), skip the
    document prompt, and open the Codebook tab's variable editor."""
    fake.set('classify', CODEBOOK_A)
    upload(page, name, content or CSV_A)
    skip_codebook(page)
    page.click('#tabPdf')
    page.wait_for_selector('#cbEditor:not(.hidden)')
    page.wait_for_selector(row('group') + ' select.cb-type')
    # The classification has reached the editor.
    page.wait_for_function(
        f"() => document.querySelector('{row('group')} select.cb-type').value === 'nominal'")


def server_codebook(page):
    return page.evaluate(
        "() => fetch('/codebook?filename=' + encodeURIComponent(CC.state.filename))"
        ".then(r => r.json())")


def expect_put(page, carrying=None):
    """The next PUT /codebook response (whose request body contains
    `carrying`, when given: leaving a field saves it at once, so one edit
    can be split over two saves)."""
    def _match(r):
        if not (r.url.split('?')[0].endswith('/codebook') and r.request.method == 'PUT'):
            return False
        return carrying is None or carrying in (r.request.post_data or '')
    return page.expect_response(_match)


def badge(page, col):
    return page.text_content(row(col) + ' .cb-badge').strip()


def put_bodies(page):
    bodies = []

    def _on_request(req):
        if req.url.split('?')[0].endswith('/codebook') and req.method == 'PUT':
            bodies.append(json.loads(req.post_data or '{}'))
    page.on('request', _on_request)
    return bodies


# ---------------------------------------------------------------------------
# Plan checkpoint: edit a column type, reload, the edit persists
# ---------------------------------------------------------------------------

def test_edit_type_persists_after_reload(page, fake):
    open_codebook(page, fake, 'cb-persist.csv')
    bodies = put_bodies(page)
    assert badge(page, 'group') == 'model'

    with expect_put(page) as resp:
        page.select_option(row('group') + ' select.cb-type', 'ordinal')
    assert resp.value.ok
    # Only the changed field of the changed column is sent.
    assert bodies[-1]['codebook'] == {'group': {'type': 'ordinal'}}
    page.wait_for_function(
        f"() => document.querySelector('{row('group')} .cb-badge').textContent.trim() === 'edited'")
    entry = server_codebook(page)['codebook']['group']
    assert entry['type'] == 'ordinal' and entry['user_fields'] == ['type']
    # The sidebar chips show the server's type, not the stale classification.
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#codebookList .codebook-chip'))"
        ".some(c => c.textContent.includes('group') && c.textContent.includes('Ord'))")

    page.reload()
    page.wait_for_function('() => window.CC && CC.editor')
    stored = page.evaluate(
        "() => fetch('/codebook?filename=cb-persist.csv').then(r => r.json())")
    assert stored['codebook']['group']['type'] == 'ordinal'

    # Uploading the same content again restores the edited codebook.
    fake.set('classify', CODEBOOK_A)
    upload(page, 'cb-persist.csv', CSV_A)
    skip_codebook(page)
    page.click('#tabPdf')
    page.wait_for_selector(row('group') + ' select.cb-type')
    page.wait_for_function(
        f"() => document.querySelector('{row('group')} select.cb-type').value === 'ordinal'")
    assert badge(page, 'group') == 'edited'
    assert page.input_value(row('age') + ' select.cb-type') == 'continuous', \
        'the re-classify still fills the fields the user never edited'


# ---------------------------------------------------------------------------
# Other fields
# ---------------------------------------------------------------------------

def test_missing_codes_label_role_and_scale_group_persist(page, fake):
    open_codebook(page, fake, 'cb-fields.csv')
    bodies = put_bodies(page)

    with expect_put(page):
        page.fill(row('age') + ' input.cb-missing', '-9, 98 to 99')
    assert bodies[-1]['codebook'] == {'age': {'missing_codes': ['-9', '98 to 99']}}

    with expect_put(page):
        page.fill(row('age') + ' input.cb-label', 'Age in years')
    with expect_put(page):
        page.select_option(row('income') + ' select.cb-role', 'weight')
    with expect_put(page):
        page.fill(row('income') + ' input.cb-scale', 'econ')

    cb = server_codebook(page)['codebook']
    assert cb['age']['missing_codes'] == ['-9', '98 to 99']
    assert cb['age']['label'] == 'Age in years'
    assert sorted(cb['age']['user_fields']) == ['label', 'missing_codes']
    assert cb['income']['role'] == 'weight'
    assert cb['income']['scale_group'] == 'econ'

    # Clearing the scale group sends null ("no group").
    with expect_put(page):
        page.fill(row('income') + ' input.cb-scale', '')
    assert bodies[-1]['codebook'] == {'income': {'scale_group': None}}
    assert server_codebook(page)['codebook']['income']['scale_group'] is None


def test_value_labels_editor_expands_and_saves(page, fake):
    open_codebook(page, fake, 'cb-values.csv')
    bodies = put_bodies(page)
    toggle = row('group') + ' button.cb-values-toggle'
    assert page.get_attribute(toggle, 'aria-expanded') == 'false'
    page.click(toggle)
    assert page.get_attribute(toggle, 'aria-expanded') == 'true'
    panel_id = page.get_attribute(toggle, 'aria-controls')
    page.wait_for_selector(f'#{panel_id}:not(.hidden)')

    page.click(f'#{panel_id} button.cb-vl-add')
    page.fill(f'#{panel_id} .cb-vl-row:last-child input.cb-vl-code', 'A')
    with expect_put(page, carrying='Group A'):
        page.fill(f'#{panel_id} .cb-vl-row:last-child input.cb-vl-text', 'Group A')
    assert bodies[-1]['codebook'] == {'group': {'value_labels': {'A': 'Group A'}}}
    assert server_codebook(page)['codebook']['group']['value_labels'] == {'A': 'Group A'}
    assert '1' in page.inner_text(toggle)

    with expect_put(page):
        page.click(f'#{panel_id} .cb-vl-row:last-child button.cb-vl-remove')
    assert server_codebook(page)['codebook']['group']['value_labels'] == {}


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------

def test_export_downloads_the_codebook_json(page, fake):
    open_codebook(page, fake, 'cb-export.csv')
    with expect_put(page):
        page.fill(row('age') + ' input.cb-label', 'Exported age')
    with page.expect_download() as dl:
        page.click('#cbExportBtn')
    download = dl.value
    assert download.suggested_filename == 'cb-export_codebook.json'
    with open(download.path(), encoding='utf-8') as f:
        doc = json.load(f)
    assert doc['format'] == 'statlee-codebook'
    assert list(doc['codebook']) == ['age', 'income', 'group']
    assert doc['codebook']['age']['label'] == 'Exported age'


def test_import_updates_the_rows(page, fake):
    open_codebook(page, fake, 'cb-import.csv')
    doc = {'format': 'statlee-codebook', 'version': 1, 'codebook': {
        'age': {'label': 'Imported age', 'type': 'ordinal', 'user_fields': ['label', 'type']},
        'nope': {'label': 'not in this dataset'},
    }}
    with page.expect_response(lambda r: r.url.endswith('/codebook/import')) as resp:
        page.set_input_files('#cbImportInput', files=[{
            'name': 'other_codebook.json', 'mimeType': 'application/json',
            'buffer': json.dumps(doc).encode('utf-8')}])
    assert resp.value.ok
    page.wait_for_function(
        f"() => document.querySelector('{row('age')} input.cb-label').value === 'Imported age'")
    assert page.input_value(row('age') + ' select.cb-type') == 'ordinal'
    assert badge(page, 'age') == 'edited'
    toast = page.locator('.cc-toast.success').last.inner_text()
    assert '1 variable' in toast and 'nope' in toast, toast


def test_import_rejects_a_bad_file_with_the_servers_message(page, fake):
    open_codebook(page, fake, 'cb-badimport.csv')
    page.set_input_files('#cbImportInput', files=[{
        'name': 'bad.json', 'mimeType': 'application/json',
        'buffer': json.dumps({'codebook': {'age': {'type': 'banana'}}}).encode('utf-8')}])
    page.wait_for_selector('.cc-toast.error')
    assert "'type' must be one of" in page.locator('.cc-toast.error').last.inner_text()
    page.set_input_files('#cbImportInput', files=[{
        'name': 'broken.json', 'mimeType': 'application/json', 'buffer': b'{not json'}])
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.cc-toast.error'))"
        ".some(t => t.textContent.includes('not valid JSON'))")


# ---------------------------------------------------------------------------
# Errors and rendering safety
# ---------------------------------------------------------------------------

def test_failed_save_reverts_the_field_and_shows_the_message(page, fake):
    open_codebook(page, fake, 'cb-fail.csv')

    def _reject(route):
        if route.request.method == 'PUT':
            route.fulfill(status=400, content_type='application/json',
                          body=json.dumps({'error': "Unknown column 'group' for this dataset."}))
        else:
            route.continue_()
    page.route('**/codebook', _reject)
    page.select_option(row('group') + ' select.cb-type', 'binary')
    page.wait_for_selector('.cc-toast.error')
    assert "Unknown column 'group'" in page.locator('.cc-toast.error').last.inner_text()
    page.wait_for_function(
        f"() => document.querySelector('{row('group')} select.cb-type').value === 'nominal'")
    page.unroute('**/codebook')

    def _broken(route):
        if route.request.method == 'PUT':
            route.fulfill(status=500, content_type='application/json', body=json.dumps(
                {'error': "The dataset's metadata could not be read, so the codebook was not saved."}))
        else:
            route.continue_()
    page.route('**/codebook', _broken)
    page.fill(row('age') + ' input.cb-label', 'never saved')
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.cc-toast.error'))"
        ".some(t => t.textContent.includes('metadata could not be read'))")
    page.wait_for_function(f"() => document.querySelector('{row('age')} input.cb-label').value === ''")
    assert server_codebook(page)['codebook']['age']['label'] == ''


def test_codebook_text_renders_literally(page, fake):
    csv = '<i>q1</i>,group\n1,A\n2,B\n'
    fake.set('classify', json.dumps({'<i>q1</i>': 'Ordinal', 'group': 'Nominal'}))
    upload(page, 'cb-xss.csv', csv)
    skip_codebook(page)
    evil = '<img src=x onerror="window.__xss=1">'
    page.evaluate(
        """(evil) => CC.put('/codebook', {filename: CC.state.filename, codebook: {
            '<i>q1</i>': {label: evil, value_labels: {'1': '<b>bold</b>'}, scale_group: evil}}})""",
        evil)
    page.click('#tabPdf')
    page.evaluate('() => CC.codebookEditor.refresh()')
    first = '#cbList > li.cb-row:first-child'
    page.wait_for_function(
        f"() => document.querySelector('{first} input.cb-label')"
        f" && document.querySelector('{first} input.cb-label').value.startsWith('<img')")
    assert page.inner_text(first + ' .cb-name').strip() == '<i>q1</i>'
    assert page.input_value(first + ' input.cb-label') == evil
    page.click(first + ' button.cb-values-toggle')
    assert page.input_value(first + ' input.cb-vl-text') == '<b>bold</b>'
    assert page.locator('#cbList img, #cbList i, #cbList b').count() == 0
    assert page.evaluate('() => window.__xss') is None
    # Filtering by a label still works (and treats it as text).
    page.fill('#cbFilter', 'group')
    page.wait_for_function(f"() => document.querySelector('{first}').classList.contains('hidden')")
    assert page.is_visible(row('group'))


def test_wrangle_adds_a_default_row(page, fake):
    open_codebook(page, fake, 'cb-wrangle.csv')
    fake.set('wrangle', json.dumps({'code': "df['age2'] = df['age'] * 2",
                                    'summary': 'Added age2', 'error': None}))
    fake.set('classify', CODEBOOK_A)        # the re-classify does not know age2
    page.fill('#wrangleInput', 'add age squared-ish')
    page.click('#wrangleApplyBtn')
    page.wait_for_selector(row('age2') + ' select.cb-type')
    assert page.input_value(row('age2') + ' select.cb-type') == ''
    assert badge(page, 'age2') == 'not set'


def test_wide_dataset_renders_once_and_refreshes_in_place(page, fake):
    cols = [f'v{i}' for i in range(300)]
    csv = '\n'.join([','.join(cols), ','.join('1' for _ in cols), ','.join('2' for _ in cols)]) + '\n'
    fake.set('classify', json.dumps(dict.fromkeys(cols, 'Continuous')))
    upload(page, 'cb-wide.csv', csv)
    skip_codebook(page)
    page.click('#tabPdf')
    page.wait_for_function("() => document.querySelectorAll('#cbList > li.cb-row').length === 300")
    page.wait_for_function(
        f"() => document.querySelector('{row('v299')} select.cb-type').value === 'continuous'")
    # Rows are kept across a refresh: no full re-render, and a field being
    # typed in keeps its text.
    page.evaluate(f"() => {{ document.querySelector('{row('v150')}').__kept = true; }}")
    page.click(row('v150') + ' input.cb-label')
    page.keyboard.type('half typed')
    page.evaluate('() => CC.codebookEditor.refresh()')
    page.wait_for_timeout(200)
    assert page.evaluate(f"() => document.querySelector('{row('v150')}').__kept === true")
    assert page.input_value(row('v150') + ' input.cb-label') == 'half typed'
    bodies = put_bodies(page)
    with expect_put(page, carrying='half typed'):
        page.keyboard.press('Tab')
    assert bodies[-1]['codebook'] == {'v150': {'label': 'half typed'}}
    assert '300 variables' in page.text_content('#cbSummary')


def test_no_dataset_shows_the_editor_empty_state(page):
    page.click('#tabPdf')
    assert page.is_visible('#cbEmpty')
    assert page.locator('#cbList > li').count() == 0
    page.click('#cbViewDocument')
    assert page.is_visible('#codebookEmptyState')
    assert page.get_attribute('#cbViewDocument', 'aria-pressed') == 'true'
    assert not page.is_visible('#cbEditor')


def test_narrow_codebook_has_no_horizontal_scroll(browser, e2e_server, fake):
    p = _new_page(browser, e2e_server['url'], width=390, height=844)
    try:
        open_codebook(p, fake, 'cb-narrow.csv')
        p.click(row('group') + ' button.cb-values-toggle')
        width = p.evaluate('() => document.documentElement.scrollWidth')
        assert width <= 390, f'page is {width}px wide at 390px on the Codebook tab'
    finally:
        p.context.close()


# ---------------------------------------------------------------------------
# Review screenshots (opt-in)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get('STATLEE_SHOTS_DIR'),
                    reason='set STATLEE_SHOTS_DIR to write review screenshots')
@pytest.mark.parametrize('scheme', ['light', 'dark'])
@pytest.mark.parametrize('width,height', [(1440, 900), (390, 844)])
def test_codebook_screenshots(browser, e2e_server, fake, scheme, width, height):
    out = os.environ['STATLEE_SHOTS_DIR']
    os.makedirs(out, exist_ok=True)
    p = _new_page(browser, e2e_server['url'], width=width, height=height, scheme=scheme)
    try:
        if scheme == 'dark':
            p.evaluate("() => { if (!document.documentElement.classList.contains('dark')) CC.toggleTheme(); }")
        tag = f'{scheme}-{width}'
        p.click('#tabPdf')
        p.screenshot(path=os.path.join(out, f'codebook-empty-{tag}.png'), full_page=True)
        open_codebook(p, fake, f'cb-shot-{tag}.csv')
        with expect_put(p):
            p.fill(row('age') + ' input.cb-label', 'Age of respondent in years')
        with expect_put(p):
            p.fill(row('age') + ' input.cb-missing', '-9, 98 to 99')
        p.click(row('group') + ' button.cb-values-toggle')
        p.click(row('group') + ' button.cb-vl-add')
        p.fill(row('group') + ' .cb-vl-row:last-child input.cb-vl-code', 'A')
        with expect_put(p, carrying='Treatment'):
            p.fill(row('group') + ' .cb-vl-row:last-child input.cb-vl-text', 'Treatment arm A')
        p.focus(row('income') + ' select.cb-role')
        p.screenshot(path=os.path.join(out, f'codebook-editor-{tag}.png'), full_page=True)
    finally:
        p.context.close()
