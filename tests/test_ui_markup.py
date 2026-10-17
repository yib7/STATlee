"""Server-rendered markup assertions for the cycle-2 UI changes.

These are observable verification without a JS runtime: we render the real
index page through the app and assert the structural hooks the frontend relies
on are present (zoom controls, on-demand suggest button, report tab, history
dialog size, the injected per-model price map).
"""
import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, 'statlee', 'static', 'js')


def _index_html(client):
    resp = client.get('/')
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _visible(html):
    """`html` minus its comments: what a user can actually read. Comments are
    served to the browser but never rendered, so copy assertions must not trip
    over a comment that discusses the very wording it explains."""
    return re.sub(r'<!--.*?-->', '', html, flags=re.S)


def _js_sources():
    """Every workspace JS file, as one blob plus a path->text map."""
    out = {}
    for path in sorted(glob.glob(os.path.join(JS_DIR, '*.js'))):
        with open(path, encoding='utf-8') as f:
            out[os.path.basename(path)] = f.read()
    return out


# --- CSP contract: every data-action must resolve to a real global ----------
def test_every_data_action_resolves_to_a_global(client):
    """boot.js dispatches clicks by looking up window[data-action]. A typo, or
    a renamed handler, yields a button that silently does nothing: the exact
    failure a strict CSP (no inline onclick) makes easy to introduce. Assert
    both sides of the contract instead.
    """
    sources = _js_sources()
    blob = '\n'.join(sources.values())
    haystack = _index_html(client) + '\n' + blob

    actions = set(re.findall(r'data-action="([A-Za-z_$][\w$]*)"', haystack))
    assert actions, 'no data-action attributes found: the contract moved'

    globals_defined = set(re.findall(r'window\.([A-Za-z_$][\w$]*)\s*=', blob))
    missing = sorted(actions - globals_defined)
    assert not missing, (
        'data-action targets with no window.<fn> definition (dead buttons): '
        + ', '.join(missing))


# --- SP1: injected price map ------------------------------------------------
def test_index_injects_model_price_map(client):
    html = _index_html(client)
    # P2-14: the price map now ships as a CSP-safe JSON data island that boot.js
    # reads into window.CC_BOOT (no executable inline script).
    assert 'id="cc-boot-data"' in html
    assert 'prices' in html
    assert 'gemini-3.8-flash' in html
    assert 'gemini-3.5-flash-lite' in html
    assert 'gemini-4-argon' in html          # Pro mode model is priced for the cost display
    # Retired or superseded models must not linger in the injected price map.
    assert 'gemini-3-flash-preview' not in html
    assert 'gemini-3.1-flash-lite-preview' not in html
    assert 'gemini-3.1-pro-preview' not in html
    assert '"gemini-3.5-flash"' not in html


# --- SP3: data-viewer zoom controls -----------------------------------------
def test_data_viewer_has_zoom_controls(client):
    html = _index_html(client)
    assert 'id="dataZoomIn"' in html
    assert 'id="dataZoomOut"' in html
    assert 'id="dataZoomReset"' in html


# --- SP4: on-demand suggestion button ---------------------------------------
def test_on_demand_suggest_button_present(client):
    html = _index_html(client)
    assert 'id="suggestNowBtn"' in html


# --- SP6: larger analysis-history dialog ------------------------------------
def test_history_dialog_is_larger(client):
    html = _index_html(client)
    i = html.find('id="historyModal"')
    assert i != -1
    panel = html[i:i + 500]                # the history dialog's own panel div
    assert 'max-w-3xl' in panel
    assert 'max-h-[85vh]' in panel


# --- SP7: report promoted to a tab, modal + sidebar button removed ----------
def test_report_is_a_tab_not_a_modal(client):
    html = _index_html(client)
    assert 'id="tabReport"' in html
    assert 'id="contentReport"' in html
    assert 'id="reportModal"' not in html
    assert 'id="reportBtn"' not in html
    # still reachable from the split-pane selector
    assert 'value="report"' in html


# --- report format selector: formal report vs traditional essay -------------
def test_report_has_format_selector(client):
    html = _index_html(client)
    assert 'id="reportFormat"' in html
    assert 'value="essay"' in html


# --- SP8: compact codebook grid ---------------------------------------------
def test_codebook_list_uses_compact_grid(client):
    html = _index_html(client)
    i = html.find('id="codebookList"')
    assert i != -1
    assert 'codebook-grid' in html[i:i + 120]


# =============================================================================
# Run lifecycle (cycle 2 SP2): generating / executing / failed
# =============================================================================

def test_generation_header_has_phase_model_chip_and_timer(client):
    html = _index_html(client)
    assert 'id="genHeader"' in html          # phase header strip
    assert 'id="genPhase"' in html           # spinner + "Drafting script" label
    assert 'id="genModelChip"' in html       # which model is generating
    assert 'id="genModelName"' in html
    assert 'id="genProBadge"' in html        # PRO badge when Pro mode is on
    assert 'id="genElapsed"' in html         # elapsed timer


def test_index_injects_code_generation_model_ids(client):
    """The model chip names a real model, so the server must ship the ids by
    role. Display only, exactly like the price map."""
    html = _index_html(client)
    i = html.find('id="cc-boot-data"')
    assert i != -1
    island = html[i:i + 900]
    assert 'models' in island
    assert 'pro_max' in island               # the Pro-mode code-gen upgrade
    assert 'draft' in island                 # the default code-gen model


def test_streaming_editor_chrome(client):
    html = _index_html(client)
    assert 'id="streamState"' in html        # "streaming" pip in the title bar
    assert 'streaming' in html
    assert 'id="autoScrollNote"' in html
    assert 'Auto-scrolling with output' in html


def test_execution_header_has_sandbox_badge_and_live_timer(client):
    html = _index_html(client)
    assert 'id="execHeader"' in html
    assert 'SANDBOX' in html
    assert 'Executing in sandbox' in html
    assert 'id="execElapsedVal"' in html     # the live m:ss value
    assert 'id="execProgress"' in html       # running progress bar
    assert 'id="termState"' in html          # running / failed - exit N


def test_ai_debug_frame_offers_a_fix_path(client):
    """A failed run must never be a dead end: name the problem, then offer the
    two ways forward."""
    html = _index_html(client)
    assert 'id="debugFrame"' in html
    assert 'STATlee spotted the problem' in html
    assert 'id="debugSummary"' in html
    assert 'id="debugFixBtn"' in html
    assert 'Fix with AI' in html
    assert 'id="debugEditBtn"' in html
    assert 'Edit script' in html


def test_lifecycle_chrome_uses_no_inline_event_handlers(client):
    """CSP is script-src 'self' (app.py): an inline onclick= would be dead
    markup. The lifecycle buttons must go through the data-action contract or
    an addEventListener in analyze.js."""
    html = _index_html(client)
    assert 'onclick=' not in html.lower()


def test_lifecycle_styles_are_hand_written_not_purged_utilities():
    """tailwind.css is a PURGED build and there is no local Tailwind toolchain,
    so a utility class that isn't already in the build silently does nothing.
    The lifecycle chrome therefore styles itself from app.css. Assert the
    classes the markup depends on really exist somewhere, rather than trusting
    that they do.
    """
    css_dir = os.path.join(ROOT, 'statlee', 'static', 'css')
    with open(os.path.join(css_dir, 'app.css'), encoding='utf-8') as f:
        app_css = f.read()
    with open(os.path.join(css_dir, 'tailwind.css'), encoding='utf-8') as f:
        built = f.read()

    for cls in ('lc-header', 'lc-model', 'lc-pro', 'lc-elapsed', 'lc-badge',
                'lc-term-state', 'lc-autoscroll', 'lc-progress', 'lc-debug',
                'lc-culprit', 'lc-trace-err', 'lc-trace-dim', 'lc-term-icon',
                'step-actions', 'step-retry', 'step-fix'):
        assert '.' + cls in app_css, f'{cls} has no rule in app.css'

    # Anything that can be toggled with Tailwind's `hidden` needs an explicit
    # override: app.css loads after tailwind.css, so at equal specificity its
    # own display rule would beat `.hidden { display: none }`.
    for cls in ('lc-header', 'lc-model', 'lc-pro', 'lc-badge', 'lc-glyph',
                'lc-term-state', 'lc-autoscroll', 'lc-progress', 'lc-debug'):
        assert f'.{cls}.hidden' in app_css, (
            f'.{cls} sets display but has no .hidden override: '
            'toggling it from JS would not hide it')

    # Guard the trap directly: this utility is NOT in the purged build, so it
    # must not be reintroduced into the markup as a way to colour failures.
    assert '.text-red-400{' not in built


# =============================================================================
# Workspace empty states (cycle 2 SP3)
# =============================================================================

def test_empty_state_flags_ship_set_on_the_body(client):
    """The empty states are CSS-driven off four body flags. If the template stops
    shipping them set, every empty state silently vanishes on first paint."""
    html = _index_html(client)
    i = html.find('<body')
    body_tag = html[i:html.find('>', i)]
    for flag in ('cc-no-data', 'cc-no-code', 'cc-no-run', 'cc-no-chat'):
        assert flag in body_tag, f'{flag} missing from <body>'


def test_each_body_flag_is_cleared_by_the_js():
    """A flag that is never removed is an empty state that never lifts. Assert
    the other half of the contract: something clears each one."""
    blob = '\n'.join(_js_sources().values())
    for flag in ('cc-no-data', 'cc-no-code', 'cc-no-run', 'cc-no-chat'):
        assert f"classList.remove('{flag}')" in blob or \
               f"classList.toggle('{flag}'" in blob, \
               f'nothing ever clears {flag}: that empty state would be permanent'


def test_dropzone_hero_is_complete(client):
    html = _index_html(client)
    assert 'Drop your dataset here' in html
    assert 'Browse files' in html
    # Extensions the file input actually accepts.
    assert '.dta' in html and '.sav' in html
    assert 'es-privacy' in html


def test_upload_privacy_copy_does_not_overclaim(client):
    """Uploads ARE written to disk and reaped on a TTL (config.file_ttl_seconds,
    2h default), so absolute 'never stored' phrasing is false. SP1 made the same
    correction on the landing page; keep the workspace consistent."""
    visible = _visible(_index_html(client)).lower()
    assert 'never stored' not in visible
    assert 'data not stored' not in visible
    assert 'auto-deleted' in visible


def test_pipeline_spine_is_visible_before_any_upload(client):
    """The spine renders from first paint against an 'awaiting data' status, so
    the run is legible before committing a file. It must NOT ship hidden."""
    html = _index_html(client)
    i = html.find('id="pipelineChecklist"')
    assert i != -1
    checklist = html[i:i + 160]
    assert 'hidden' not in checklist, 'the spine is hidden again: empty state lost'
    assert 'id="pipelineStatus"' in html
    assert 'awaiting data' in html


def test_composer_is_disabled_until_a_dataset_is_loaded(client):
    html = _index_html(client)
    i = html.find('id="generateBtn"')
    assert i != -1
    assert 'disabled' in html[i:i + 120], 'Generate ships enabled with no dataset'
    assert 'Upload a dataset to enable' in html


def test_classify_failure_still_releases_the_composer():
    """Generation only needs CC.state.filename; the codebook is optional context
    that merges empty. Re-enabling ONLY on the success path stranded the user
    behind a permanently disabled Generate button when /classify_variables
    failed. The release must not depend on success (only on the reply still
    belonging to the loaded dataset, 3A C-16)."""
    data_js = _js_sources()['data.js']
    i = data_js.find('async function fetchCodebook')
    assert i != -1
    end = data_js.find('\n    }\n', i)
    assert end != -1
    body = data_js[i:end]
    assert 'finally' in body and 'releaseComposer()' in body, \
        'fetchCodebook must release the composer on the failure path too'


def test_each_tab_has_an_empty_state(client):
    html = _index_html(client)
    assert 'id="codeEmptyState"' in html
    assert 'Your generated script appears here' in html
    assert 'id="dataEmptyState"' in html
    assert 'Preview your data here' in html
    assert 'Results land here after a run' in html
    assert 'id="converseEmptyState"' in html
    assert 'Ask STATlee about your analysis' in html
    assert 'No codebook attached' in html
    assert 'How STATlee classifies' in html


def test_codebook_classification_table_matches_the_real_levels(client):
    """The three levels prompts.classify() sorts every column into."""
    html = _index_html(client)
    i = html.find('How STATlee classifies')
    table = html[i:i + 1400]
    for level in ('continuous', 'nominal', 'ordinal'):
        assert level in table, f'{level} missing from the classification table'


def test_report_lock_does_not_claim_a_successful_run_is_required(client):
    """tools.js gates generateReport on CC.state.lastRun.output, which a FAILED
    run also sets. 'after your first successful run' would be false."""
    html = _index_html(client)
    assert 'unlocks after your first run' in html
    # Comments are served but not shown; this is about the visible copy (and the
    # comments here explain precisely why the claim was rejected).
    assert 'successful run' not in _visible(html)


def test_sample_chips_fill_the_composer_and_never_auto_send():
    """Every send is a billable model call, so a chip must not fire one. Assert
    askSample only touches the input value."""
    converse = _js_sources()['converse.js']
    i = converse.find('CC.askSample = function')
    assert i != -1
    body = converse[i:converse.find('};', i)]
    assert 'chatInput.value = question' in body
    assert 'sendMessage' not in body, 'a sample chip must not auto-send (it costs money)'
    assert 'postStream' not in body and 'CC.post(' not in body


def test_empty_state_styles_are_hand_written_not_purged_utilities():
    """Same trap as the lifecycle chrome: tailwind.css is a purged build with no
    local toolchain, so any utility not already in it silently does nothing."""
    css_dir = os.path.join(ROOT, 'statlee', 'static', 'css')
    with open(os.path.join(css_dir, 'app.css'), encoding='utf-8') as f:
        app_css = f.read()

    for cls in ('es-veil', 'es-skel', 'es-skels', 'es-hero', 'es-notice',
                'es-icon', 'es-title', 'es-sub', 'es-chip', 'es-ask',
                'es-staged', 'es-browse', 'es-privacy', 'es-hint',
                'es-class-table', 'es-class-row', 'es-legend-label'):
        assert '.' + cls in app_css, f'{cls} has no rule in app.css'

    # The skeleton fill token the empty states are built on, in both themes.
    assert '--skel:' in app_css.replace(' ', '')
    assert app_css.count('--skel:') >= 2, '--skel must be defined for light AND dark'


# =============================================================================
# Deep audit D4 (2026-10-08): frontend hardening, a11y, honest copy
# =============================================================================

def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def test_data_action_dispatch_uses_an_explicit_allowlist(client):
    """D4-4: boot.js used to call window[<any data-action>]. Sanitized model
    output must never be able to reach an arbitrary global, so dispatch goes
    through a fixed allowlist, and every data-action the app ships is on it."""
    boot = _read('statlee', 'static', 'js', 'boot.js')
    m = re.search(r'ACTIONS\s*=\s*\[([^\]]*)\]', boot)
    assert m, 'boot.js has no ACTIONS allowlist'
    allowed = set(re.findall(r"'([A-Za-z_$][\w$]*)'", m.group(1)))
    assert 'open' not in allowed and 'fetch' not in allowed
    haystack = _index_html(client) + '\n' + '\n'.join(_js_sources().values())
    used = set(re.findall(r'data-action="([A-Za-z_$][\w$]*)"', haystack))
    missing = sorted(used - allowed)
    assert not missing, 'data-action targets missing from the boot.js allowlist: ' + ', '.join(missing)
    assert 'window[el.getAttribute' not in boot, 'unrestricted window[...] dispatch is back'


def test_markdown_sanitizer_is_hardened():
    """D4-4: DOMPurify's defaults keep data-* attributes, style and form
    controls. The renderer must turn those off and open links in a new tab."""
    api = _read('statlee', 'static', 'js', 'api.js')
    assert 'ALLOW_DATA_ATTR: false' in api
    for token in ("'style'", "'form'", "'button'", "'input'", "'iframe'", "'object'"):
        assert token in api, f'{token} not forbidden in the sanitizer config'
    assert 'noopener noreferrer' in api and "'_blank'" in api


def test_buttons_do_not_suppress_the_focus_ring(client):
    """D4-13: Tailwind's focus:outline-none (0,2,0) beat app.css's focus ring
    (0,1,1) on every tab and sidebar toggle."""
    html = _index_html(client)
    for tag in re.findall(r'<button[^>]*>', html):
        assert 'focus:outline-none' not in tag, tag[:120]


def test_tabs_and_live_regions_are_marked_up(client):
    """D4-14: real tab semantics and a log role on the conversation."""
    html = _index_html(client)
    for name in ('Generate', 'Data', 'Results', 'Converse', 'Report', 'Pdf'):
        i = html.find(f'id="tab{name}"')
        tag = html[i:html.find('>', i)]
        assert 'aria-selected=' in tag and f'aria-controls="content{name}"' in tag, name
        j = html.find(f'id="content{name}"')
        panel = html[j:html.find('>', j)]
        assert 'role="tabpanel"' in panel, name
    i = html.find('id="chatLog"')
    assert 'role="log"' in html[i:html.find('>', i)]
    assert 'id="srAnnounce"' in html


def _hex_lum(h):
    h = h.lstrip('#')
    vals = [int(h[k:k + 2], 16) / 255 for k in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in vals]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a, b):
    hi, lo = sorted((_hex_lum(a), _hex_lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(block):
    return dict(re.findall(r'--([\w-]+):\s*(#[0-9a-fA-F]{6})', block))


def test_text_tokens_meet_wcag_aa():
    """D4-15: secondary text, status colours and the amber 'Original' button
    must reach 4.5:1 on the surfaces they sit on, in both themes."""
    css = _read('statlee', 'static', 'css', 'app.css')
    light = _tokens(css[css.find(':root {'):css.find('}', css.find(':root {'))])
    dark = _tokens(css[css.find('.dark {'):css.find('}', css.find('.dark {'))])
    for fg in ('text-3', 'pipe-done', 'pipe-failed', 'c-nom', 'indigo'):
        for bg in ('s0', 's1', 's2'):
            ratio = _contrast(light[fg], light[bg])
            assert ratio >= 4.5, f'light --{fg} on --{bg} is {ratio:.2f}:1'
    for fg in ('text-3', 'pipe-done', 'pipe-failed', 'c-nom'):
        ratio = _contrast(dark[fg], dark['s1'])
        assert ratio >= 4.5, f'dark --{fg} on --s1 is {ratio:.2f}:1'
    for bg in ('code-bg', 'code-chrome'):
        ratio = _contrast(light['code-muted'], light[bg])
        assert ratio >= 4.5, f'--code-muted on --{bg} is {ratio:.2f}:1'


def test_copy_does_not_overclaim_isolation_or_storage(client):
    """D4-16: the default subprocess sandbox does not isolate the network, the
    script runs on the server, and anonymous uploads ARE stored for a while."""
    visible = _visible(_index_html(client))
    assert 'Execute Script Locally' not in visible
    assert 'Nothing is stored without an account' not in visible
    assert 'no network' not in visible.lower()
    assert 'What is sent to' in visible           # the data-handling disclosure
    js = '\n'.join(_js_sources().values())
    assert 'no network' not in js and 'local sandbox' not in js
    assert 'Gemini list prices' not in js


def test_landing_network_claims_depend_on_sandbox_mode(app, client):
    landing = _visible(client.get('/welcome').get_data(as_text=True)).lower()
    assert 'network-less' not in landing and 'no network access' not in landing
    app.config['STATLEE'].sandbox_mode = 'docker'
    try:
        docker_landing = _visible(client.get('/welcome').get_data(as_text=True)).lower()
    finally:
        app.config['STATLEE'].sandbox_mode = 'subprocess'
    assert 'no network access' in docker_landing


def test_provider_and_model_copy_come_from_config(app, client):
    """D4-24: footer, Pro tooltip and page cap are rendered from config, not
    hard-coded to Gemini / 50 pages."""
    cfg = app.config['STATLEE']
    old = (cfg.llm_provider, cfg.model_pro, cfg.model_pro_max, cfg.pdf_max_pages)
    cfg.llm_provider, cfg.model_pro, cfg.model_pro_max, cfg.pdf_max_pages = (
        'openai', 'gpt-test-mini', 'gpt-test-max', 12)
    try:
        html = _visible(_index_html(client))
    finally:
        cfg.llm_provider, cfg.model_pro, cfg.model_pro_max, cfg.pdf_max_pages = old
    footer = html[html.find('<footer'):html.find('</footer>')]
    assert 'OpenAI' in footer and 'Gemini' not in footer
    i = html.find('for="proToggle"')
    assert 'gpt-test-max' in html[i:i + 400] and 'gemini' not in html[i:i + 400].lower()
    assert 'Max 12 pages' in html


def test_account_form_has_submit_and_password_reset(client):
    """D4-12: Enter submits (a real form) and there is a reset entry point."""
    html = _index_html(client)
    assert 'id="accForm"' in html
    assert 'id="accForgotBtn"' in html
    tools = _js_sources()['tools.js']
    assert '/request_password_reset' in tools
    assert "status === 'success'" in tools      # reload only on a real sign-in


# --- S4: frontend reconciled with the S1-B backend contract -----------------
def test_s4_dataset_info_and_codebook_text_hooks(client):
    """The stored name can differ from the picked file (D1-1), so the sidebar
    names the dataset the user picked and lists upload warnings; a TXT
    codebook has a text view instead of the PDF frame."""
    html = _index_html(client)
    for hook in ('id="datasetInfo"', 'id="datasetName"', 'id="datasetWarnings"',
                 'id="codebookTextView"', 'id="interpretationTitle"'):
        assert hook in html, hook


def test_s4_export_copy_describes_the_runnable_zip(client):
    """Contract 15: data at the root, data/original_<name>, README.txt and
    cleaning.py. The old tooltip promised a data/ folder that is gone."""
    html = _visible(_index_html(client))
    i = html.find('id="exportBtn"')
    tag = html[i - 200:i + 400]
    assert 'README' in tag and 'original' in tag and 'cleaning' in tag
    assert 'data + script + plots + report as a zip' not in tag


def test_s4_no_stale_run_or_login_contract_in_js():
    """Contract 11: the "Generate code before running" 403 is gone, and
    contract 16: sign-in keeps the workspace, so login no longer reloads."""
    sources = _js_sources()
    blob = '\n'.join(sources.values())
    assert 'Generate code before running' not in blob
    assert 'only runs scripts it generated' not in blob
    tools = sources['tools.js']
    start = tools.find('function doLogin')
    body = tools[start:tools.find('function doRegister')]
    assert 'location.reload' not in body, 'login still throws the workspace away'


def test_s4_credit_balance_is_read_centrally():
    """Upcoming field: `credits_remaining` is picked up for every JSON reply
    and every SSE event, not route by route."""
    api = _js_sources()['api.js']
    assert 'credits_remaining' in api
    assert api.count('CC.noteCredits(') >= 3     # post, postForm, streamSSE


# =============================================================================
# SP1 session workbook (2026-10-08): run rail, read-only view, report checklist
# =============================================================================

def _section(html, start_id, end_id):
    i = html.find(f'id="{start_id}"')
    j = html.find(f'id="{end_id}"', i)
    assert i != -1 and j != -1, (start_id, end_id)
    return html[i:j]


def test_results_tab_has_a_numbered_run_rail(client):
    results = _section(_index_html(client), 'contentResults', 'contentConverse')
    assert 'id="runRail"' in results
    assert re.search(r'<ol[^>]*id="runList"', results), 'the run rail is a numbered list'
    # Viewing a saved run: read-only, with the one action that touches the editor.
    for hook in ('runViewBar', 'runViewTitle', 'runViewQuestion', 'runInheritNote',
                 'runLoadScriptBtn', 'runInterpretBtn', 'runBackBtn'):
        assert f'id="{hook}"' in results, hook
    assert 'Load script' in _visible(results)
    assert 'read-only' in _visible(results)


def test_report_builder_has_a_run_checklist(client):
    report = _section(_index_html(client), 'contentReport', 'paneDivider')
    m = re.search(r'<fieldset[^>]*id="reportRuns"[^>]*>(.*?)</fieldset>', report, re.S)
    assert m, 'the report run checklist is a fieldset'
    assert '<legend' in m.group(1)
    assert 'id="reportRunList"' in m.group(1)
    assert 'Grounded in your latest run.' not in report


def test_workbook_script_is_loaded_after_analyze(client):
    html = _index_html(client)
    a = html.find('js/analyze.js')
    w = html.find('js/workbook.js')
    t = html.find('js/tools.js')
    assert w != -1, 'workbook.js is not loaded'
    assert a < w < t


def test_workbook_js_renders_run_data_as_text_only():
    """Run names, questions, file names and outputs are user/model text: the
    workbook module builds its DOM with textContent, never innerHTML."""
    js = _js_sources()['workbook.js']
    assert 'innerHTML' not in js
    assert 'insertAdjacentHTML' not in js
    assert 'textContent' in js


def test_patch_requests_carry_the_csrf_header():
    """PATCH /runs/<id> is CSRF-protected: it must go through CC.headers()."""
    api = _js_sources()['api.js']
    m = re.search(r'CC\.send\s*=\s*async function[^{]*\{(.*?)\n    \};', api, re.S)
    assert m, 'api.js has no shared CC.send'
    assert 'CC.headers()' in m.group(1)
    assert re.search(r"CC\.patch\s*=.*CC\.send\('PATCH'", api)
    assert "CC.send('PATCH'" in api
    js = _js_sources()['workbook.js']
    assert 'CC.patch(' in js
    assert "method: 'PATCH'" not in js, 'PATCH bypasses the shared CSRF path'


def test_workbook_styles_are_hand_written_not_purged_utilities():
    css = _read('statlee', 'static', 'css', 'app.css')
    for cls in ('wb-rail', 'wb-run-list', 'wb-run', 'wb-run-open', 'wb-run-num',
                'wb-run-name', 'wb-run-meta', 'wb-icon-btn', 'wb-run-star',
                'wb-run-name-input', 'wb-viewbar', 'wb-note', 'wb-btn',
                'wb-checklist', 'wb-check'):
        assert '.' + cls in css, f'{cls} has no rule in app.css'
    for cls in ('wb-rail', 'wb-viewbar', 'wb-note', 'wb-btn', 'wb-checklist'):
        assert f'.{cls}.hidden' in css, f'.{cls} needs a .hidden override'


def test_workbook_copy_has_no_em_dashes(client):
    html = _index_html(client)
    for start, end in (('runRail', 'execHeader'), ('reportRuns', 'reportGenerateBtn')):
        assert '\u2014' not in _visible(_section(html, start, end))
    js = _js_sources()['workbook.js']
    strings = re.findall(r"'([^'\n]*)'", js)
    assert not [s for s in strings if '\u2014' in s]


# =============================================================================
# SP2 survey-native codebook (2026-10-08): the editable Codebook tab
# =============================================================================

def test_codebook_tab_has_an_editor_and_keeps_the_document_viewer(client):
    tab = _section(_index_html(client), 'contentPdf', 'contentReport')
    for hook in ('cbEditor', 'cbList', 'cbSummary', 'cbFilter', 'cbImportBtn',
                 'cbExportBtn', 'cbImportInput', 'cbEmpty', 'cbViewVariables',
                 'cbViewDocument', 'cbDocument'):
        assert f'id="{hook}"' in tab, hook
    # The existing PDF/TXT viewer stays, inside the Document view.
    doc = tab[tab.find('id="cbDocument"'):]
    for hook in ('codebookEmptyState', 'pdfViewerFrame', 'codebookTextView',
                 'codebookTabUploadBtn', 'codebookTabSurveyBtn'):
        assert f'id="{hook}"' in doc, hook
    assert 'id="codebookReplaceBtn"' in tab
    # The two views are a pressed-button switch, not two unlabelled divs.
    for view, target in (('cbViewVariables', 'cbEditor'), ('cbViewDocument', 'cbDocument')):
        i = tab.find(f'id="{view}"')
        tag = tab[tab.rfind('<', 0, i):tab.find('>', i)]
        assert 'aria-pressed=' in tag and f'aria-controls="{target}"' in tag, view
    m = re.search(r'<input[^>]*id="cbImportInput"[^>]*>', tab)
    assert m and 'accept=".json,application/json"' in m.group(0)
    assert re.search(r'<ol[^>]*id="cbList"', tab), 'variables are an ordered list'


def test_codebook_controls_are_labelled(client):
    tab = _section(_index_html(client), 'contentPdf', 'contentReport')
    m = re.search(r'<input[^>]*id="cbFilter"[^>]*>', tab)
    assert m and ('aria-label=' in m.group(0) or 'for="cbFilter"' in tab)
    js = _js_sources()['codebook.js']
    # Every per-row control gets an accessible name that names its column.
    for field in ('Type', 'Label', 'Role', 'Scale group', 'Missing codes'):
        assert f"'{field} for '" in js, field
    assert "setAttribute('aria-expanded'" in js
    assert "setAttribute('aria-controls'" in js


def test_codebook_import_copy_says_only_edits_come_across(client):
    tab = _visible(_section(_index_html(client), 'contentPdf', 'contentReport'))
    i = tab.find('id="cbEditor"')
    editor = tab[i:tab.find('id="cbDocument"')]
    assert 'only' in editor and 'edit' in editor
    assert re.search(r'export[^<]*only[^<]*edit', editor, re.I | re.S), \
        'the import hint must say an export brings over only hand edits'


def test_codebook_script_is_loaded_after_workbook(client):
    html = _index_html(client)
    w = html.find('js/workbook.js')
    c = html.find('js/codebook.js')
    t = html.find('js/tools.js')
    assert c != -1, 'codebook.js is not loaded'
    assert w < c < t


def test_codebook_js_renders_codebook_text_as_text_only():
    """Labels, value labels and column names are user/model/file text: the
    codebook module builds its DOM with createElement + textContent only."""
    js = _js_sources()['codebook.js']
    assert 'innerHTML' not in js
    assert 'insertAdjacentHTML' not in js
    assert 'outerHTML' not in js
    assert 'textContent' in js


def test_codebook_writes_carry_the_csrf_header():
    api = _js_sources()['api.js']
    assert re.search(r"CC\.put\s*=.*CC\.send\('PUT'", api)
    js = _js_sources()['codebook.js']
    assert 'CC.put(' in js and 'CC.post(' in js
    assert "method: 'PUT'" not in js or 'CC.headers()' in js


def test_codebook_reads_the_server_and_refreshes_on_dataset_changes():
    js = _js_sources()
    assert "'/codebook?filename='" in js['codebook.js']
    assert "'/codebook/import'" in js['codebook.js']
    assert "'/codebook/export?filename='" in js['codebook.js']
    # Classification, PDF extraction, wrangles, and a new dataset refresh it.
    data = js['data.js']
    assert data.count('CC.codebookEditor') >= 4, 'data.js must refresh the codebook editor'


def test_codebook_styles_are_hand_written_not_purged_utilities():
    css = _read('statlee', 'static', 'css', 'app.css')
    for cls in ('cb-editor', 'cb-toolbar', 'cb-list', 'cb-row', 'cb-badge',
                'cb-field', 'cb-values', 'cb-switch', 'cb-empty', 'cb-document'):
        assert '.' + cls in css, f'{cls} has no rule in app.css'
    for cls in ('cb-editor', 'cb-values', 'cb-empty', 'cb-document', 'cb-row'):
        assert f'.{cls}.hidden' in css, f'.{cls} needs a .hidden override'


def test_codebook_copy_has_no_em_dashes(client):
    tab = _visible(_section(_index_html(client), 'contentPdf', 'contentReport'))
    assert '—' not in tab
    js = _js_sources()['codebook.js']
    strings = re.findall(r"'([^'\n]*)'", js)
    assert not [s for s in strings if '—' in s]


# =============================================================================
# SP3 methods and assumptions card (2026-10-08)
# =============================================================================

def test_results_tab_has_a_methods_card_above_the_terminal(client):
    html = _index_html(client)
    results = _section(html, 'contentResults', 'contentConverse')
    card = results.find('id="methodsCard"')
    terminal = results.find('id="runOutput"')
    assert card != -1, 'no methods card in Results'
    assert card < terminal, 'the methods card sits above the terminal'
    section = _section(html, 'methodsCard', 'runOutput')
    assert 'Methods and assumptions' in _visible(section)
    assert 'aria-labelledby="methodsCardTitle"' in section
    assert 'id="methodsCardBody"' in section
    assert '—' not in _visible(section)


def test_methods_script_is_loaded_before_tools(client):
    html = _index_html(client)
    m = html.find('js/methods.js')
    t = html.find('js/tools.js')
    assert m != -1, 'methods.js is not loaded'
    assert m < t


def test_methods_js_renders_parsed_values_as_text_only():
    """The card's values come from a model-written script's output: built
    with createElement/textContent, never innerHTML."""
    js = _js_sources()['methods.js']
    assert 'innerHTML' not in js
    assert 'insertAdjacentHTML' not in js
    assert 'textContent' in js and 'createElement' in js
    # chips carry a text label, not color alone
    for label in ("'PASS'", "'WARN'", "'FAIL'"):
        assert label in js, label
    assert 'Assumptions not reported' in js
    strings = re.findall(r"'([^'\n]*)'", js)
    assert not [s for s in strings if '—' in s]


def test_methods_card_is_wired_into_runs_and_saved_runs():
    js = _js_sources()
    analyze = js['analyze.js']
    assert analyze.count('CC.renderMethods(') >= 3     # live run, timeout, saved run
    assert 'CC.clearMethods(' in analyze
    assert 'CC.clearMethods(' in js['tools.js']        # text-only history restore


def test_methods_card_styles_are_hand_written():
    css = _read('statlee', 'static', 'css', 'app.css')
    for cls in ('mc-card', 'mc-head', 'mc-method', 'mc-checks', 'mc-check',
                'mc-chip', 'mc-effects', 'mc-empty'):
        assert '.' + cls in css, f'{cls} has no rule in app.css'
    assert '.mc-card.hidden' in css
    for state in ('pass', 'warn', 'fail', 'unknown'):
        assert f'.mc-chip.{state}' in css, state


def test_methods_card_copy_is_plain_language():
    js = _js_sources()['methods.js']
    strings = ' '.join(re.findall(r"'([^'\n]*)'", js))
    assert 'methods block' not in strings, 'internal jargon in user-facing copy'
    assert 'did not print a methods summary' in strings


# --- SP4: export dialog with the bundle options ------------------------------
def _export_modal(html):
    start = html.find('id="exportModal"')
    assert start != -1, 'no export dialog'
    return html[start:html.find('<!--', start)]


def test_export_button_opens_a_dialog_with_the_two_options(client):
    html = _visible(_index_html(client))
    modal = _export_modal(html)
    assert 'role="dialog"' in html[html.find('id="exportModal"') - 120:
                                   html.find('id="exportModal"') + 200]
    # include_prompts: a labelled checkbox, on by default.
    assert re.search(r'<input type="checkbox" id="exportIncludePrompts"[^>]*checked',
                     modal)
    assert 'for="exportIncludePrompts"' in modal or \
        re.search(r'<label[^>]*>\s*<input type="checkbox" id="exportIncludePrompts"', modal)
    # runs: all (default) | starred | latest.
    select = modal[modal.find('id="exportRuns"'):]
    select = select[:select.find('</select>')]
    assert re.findall(r'<option value="(\w+)"', select) == ['all', 'starred', 'latest']
    assert re.search(r'<option value="all" selected', select)
    assert 'for="exportRuns"' in modal
    assert 'id="exportDownloadBtn"' in modal


def test_export_dialog_copy_is_plain_and_has_no_em_dashes(client):
    modal = _export_modal(_visible(_index_html(client)))
    text = re.sub(r'<[^>]+>', ' ', modal)
    assert '—' not in text and '–' not in text
    for words in ('Include my prompts', 'Runs to include', 'All runs',
                  'Starred runs', 'Latest run only'):
        assert words in text, words


def test_export_js_sends_the_options_and_names_readme_md():
    tools = _js_sources()['tools.js']
    body = tools[tools.find('async function exportProject'):
                 tools.find('// AI report builder')]
    assert 'include_prompts' in body and "getElementById('exportIncludePrompts')" in body
    assert re.search(r'\bruns\s*[,:]', body) and "getElementById('exportRuns')" in body
    assert 'README.md' in body and 'README.txt' not in body
    assert 'innerHTML' not in body
    assert "CC.modal.open('exportModal')" in tools
    assert "getElementById('exportDownloadBtn')" in tools
    for js in _js_sources().values():
        assert 'README.txt' not in js


def test_export_dialog_does_not_overpromise_prompt_removal(client):
    modal = _export_modal(_visible(_index_html(client)))
    text = ' '.join(re.sub(r'<[^>]+>', ' ', modal).split())
    assert 'may still restate' in text
    assert 'Turn off to share the bundle without them.' not in text
