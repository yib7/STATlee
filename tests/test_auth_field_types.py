"""Auth routes answer a wrong JSON type with a 400 the UI can show, never a
500 (release audit 2026-10-16, B-13)."""
import pytest
from conftest import csrf_token, post_json


@pytest.mark.parametrize('path, payload', [
    ('/login', {'email': 123}),
    ('/login', {'email': 'a@b.co', 'password': 5}),
    ('/login', {'password': ['x']}),
    ('/register', {'email': 'a@b.co', 'password': 12345678}),
    ('/register', {'email': ['x'], 'password': 'longenough1'}),
    ('/request_password_reset', {'email': 5}),
    ('/reset_password', {'token': 7, 'password': 'longenough1'}),
    ('/reset_password', {'token': 'abc', 'password': 12345678}),
])
def test_wrong_field_type_is_a_400(client, path, payload):
    resp = post_json(client, path, payload)
    assert resp.status_code == 400
    assert 'must be a string' in resp.get_json()['error']


@pytest.mark.parametrize('path', ['/login', '/register',
                                  '/request_password_reset', '/reset_password'])
def test_a_json_array_body_is_not_a_500(client, path):
    resp = post_json(client, path, ['a@b.co', 'longenough1'])
    assert resp.status_code < 500


def test_reset_password_form_post_still_reads_the_form(client):
    """The reset page posts a form, not JSON: an unknown token is the normal
    invalid-link 400, not a type error."""
    token = csrf_token(client)
    resp = client.post('/reset_password',
                       data={'token': 'nope', 'password': 'longenough1'},
                       headers={'X-CSRF-Token': token})
    assert resp.status_code == 400
    assert 'invalid or has expired' in resp.get_json()['error']


def _signed_in(client):
    resp = post_json(client, '/register',
                     {'email': 'h@x.com', 'password': 'longenough1'})
    assert resp.status_code == 201


@pytest.mark.parametrize('payload', [
    {'prompt': 5}, {'code': ['x']}, {'output': {'a': 1}},
    {'interpretation': 3.5}, {'dataset_name': 9}, {'language': True},
])
def test_history_save_rejects_wrong_types(client, payload):
    _signed_in(client)
    resp = post_json(client, '/history', payload)
    assert resp.status_code == 400
    assert 'must be a string' in resp.get_json()['error']


def test_history_save_array_body_saves_an_empty_entry(client):
    _signed_in(client)
    resp = post_json(client, '/history', ['not', 'an', 'object'])
    assert resp.status_code == 200
    assert resp.get_json()['saved'] is True


def test_history_save_still_clamps_and_defaults(client):
    _signed_in(client)
    resp = post_json(client, '/history', {'prompt': 'p' * 20000,
                                          'language': None})
    assert resp.status_code == 200
    rows = client.get('/history').get_json()['runs']
    assert rows[0]['language'] == 'Python'
    assert len(rows[0]['prompt']) == 10000


def test_master_password_with_non_ascii_is_a_401_not_a_500(tmp_path, fake_llm):
    """secrets.compare_digest raises TypeError on non-ASCII str; the legacy
    password check compares UTF-8 bytes instead."""
    from statlee import llm
    from statlee.app import create_app
    from statlee.config import Config
    cfg = Config(env='testing', upload_root=str(tmp_path / 'u'),
                 database_url='sqlite:///' + str(tmp_path / 'a.db').replace(
                     chr(92), '/'),
                 flask_secret_key='k', rate_limit_enabled=False,
                 app_password='caf\u00e9-secret')
    cfg.validate()
    app = create_app(cfg)
    llm.set_service(fake_llm)
    c = app.test_client()
    assert post_json(c, '/login', {'password': 'na\u00efve'}).status_code == 401
    assert post_json(c, '/login',
                     {'password': 'caf\u00e9-secret'}).status_code == 200
