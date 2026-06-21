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
