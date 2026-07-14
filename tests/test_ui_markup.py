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
    assert 'gemini-3.5-flash' in html
    assert 'gemini-3.1-flash-lite' in html
    assert 'gemini-3.1-pro' in html          # Pro mode model is priced for the cost display
    # Retired models must not linger in the injected price map.
    assert 'gemini-3-flash-preview' not in html
    assert 'gemini-3.1-flash-lite-preview' not in html


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
    failed. The release must be unconditional."""
    data_js = _js_sources()['data.js']
    i = data_js.find('async function fetchCodebook')
    assert i != -1
    body = data_js[i:i + 1200]
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
