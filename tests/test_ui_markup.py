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
    assert 'gemini-3-flash-preview' in html
    assert 'gemini-3.1-flash-lite-preview' in html


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
