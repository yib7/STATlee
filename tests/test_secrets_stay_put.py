"""Configured secrets never reach a client, a run or the export (Phase 4, 4.1).

Every secret the app holds is set to a sentinel, a full session is driven
through the routes (upload, run, wrangle, workbook, export), and no response
body, header or zip member may contain any sentinel. The LLM key goes only to
the provider SDK, the SMTP password only to SMTP (test_smtp_tls.py), and the
sandbox child gets no secret (test_sandbox.py).
"""
import io
import zipfile

from conftest import SAMPLE_CSV, FakeLLMService, post_json, upload_csv

from statlee import llm
from statlee.app import create_app
from statlee.config import Config

SENTINELS = {
    'gemini_api_key': 'SENTINEL-gemini-7f3a',
    'anthropic_api_key': 'SENTINEL-anthropic-91c2',
    'openai_api_key': 'SENTINEL-openai-55d0',
    'flask_secret_key': 'SENTINEL-flask-secret-0b8e-long-enough',
    'smtp_password': 'SENTINEL-smtp-c4d1',
    'metrics_token': 'SENTINEL-metrics-2e6f',
}


def test_no_secret_in_any_response_or_the_export(tmp_path):
    cfg = Config(env='testing', upload_root=str(tmp_path / 'u'),
                 database_url='sqlite:///'
                 + str(tmp_path / 's.db').replace('\\', '/'),
                 rate_limit_enabled=False, **SENTINELS)
    cfg.validate()
    app = create_app(cfg)
    fake = FakeLLMService()
    llm.set_service(fake)
    client = app.test_client()

    bodies = []

    def keep(resp):
        bodies.append(resp.get_data())
        bodies.append(str(resp.headers).encode())
        return resp

    keep(upload_csv(client, SAMPLE_CSV))
    keep(post_json(client, '/run', {
        'filename': 'test.csv', 'language': 'Python',
        'code': "import pandas as pd\nprint(pd.read_csv('test.csv').shape)"}))
    keep(post_json(client, '/wrangle', {'filename': 'test.csv',
                                        'instruction': 'drop missing rows'}))
    keep(client.get('/runs'))
    keep(client.get('/check_auth'))
    keep(client.get('/health'))
    keep(client.get('/metrics'))
    export = keep(post_json(client, '/export', {'filename': 'test.csv'}))
    assert export.status_code == 200
    with zipfile.ZipFile(io.BytesIO(export.get_data())) as zf:
        assert zf.namelist()
        bodies.extend(zf.read(name) for name in zf.namelist())

    for name, value in SENTINELS.items():
        for blob in bodies:
            assert value.encode() not in blob, f'{name} leaked'


def test_unhandled_error_is_a_generic_500_with_no_detail(app, client,
                                                         monkeypatch):
    """Phase 4, 4.9: the 500 body names no exception, path or value."""
    from statlee import storage

    def boom(*_a, **_k):
        raise RuntimeError(r'SENTINEL-internal C:\srv\statlee\secret.db')

    monkeypatch.setattr(storage, 'list_runs', boom)
    resp = client.get('/runs')
    assert resp.status_code == 500
    assert resp.get_json() == {'error': 'Internal server error.'}
    body = resp.get_data(as_text=True)
    assert 'SENTINEL' not in body and 'Traceback' not in body


def test_malformed_bodies_get_plain_400s(client):
    from conftest import csrf_token
    token = csrf_token(client)
    headers = {'X-CSRF-Token': token, 'Content-Type': 'application/json'}
    # (/export with no dataset is a valid, dataset-less bundle, so not here.)
    for path in ('/run', '/chat', '/data_page', '/wrangle'):
        resp = client.post(path, data='{"code": ', headers=headers)
        assert resp.status_code == 400, path
        body = resp.get_data(as_text=True)
        assert 'Traceback' not in body and 'File "' not in body, path
    resp = client.post('/upload', data=b'--x\r\nbroken',
                       headers={'X-CSRF-Token': token,
                                'Content-Type': 'multipart/form-data; boundary=x'})
    assert resp.status_code == 400
    assert 'Traceback' not in resp.get_data(as_text=True)
