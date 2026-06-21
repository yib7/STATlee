"""Server-rendered markup assertions for the cycle-2 UI changes.

These are observable verification without a JS runtime: we render the real
index page through the app and assert the structural hooks the frontend relies
on are present (zoom controls, on-demand suggest button, report tab, history
dialog size, the injected per-model price map).
"""


def _index_html(client):
    resp = client.get('/')
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


# --- SP1: injected price map ------------------------------------------------
def test_index_injects_model_price_map(client):
    html = _index_html(client)
    assert 'CC_BOOT' in html
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
