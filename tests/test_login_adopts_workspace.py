"""Signing in keeps the work done anonymously (deep audit D1 latent L2).

The storage identity switches from ``anon_<sid>`` to ``user_<id>`` at login,
and nothing moved the files: "sign up to save my work" lost the upload
(/data_page answered 400 "Invalid filename" right after /register). The
anonymous workspace is now adopted into the account, without overwriting
anything the account already holds.
"""
import os

from conftest import SAMPLE_CSV, post_json, upload_csv

OTHER_CSV = "a,b\n1,2\n3,4\n"


def _rows(client, name):
    resp = post_json(client, '/data_page', {'filename': name})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()['data']


def test_register_keeps_the_anonymous_upload(client):
    upload_csv(client, SAMPLE_CSV)
    assert post_json(client, '/register', {
        'email': 'keep@example.com', 'password': 'password123'}).status_code == 201
    assert len(_rows(client, 'test.csv')) == 4


def test_login_keeps_history_and_versions(client, fake_llm):
    post_json(client, '/register', {'email': 'v@example.com',
                                    'password': 'password123'})
    post_json(client, '/logout', {})
    upload_csv(client, SAMPLE_CSV)
    post_json(client, '/wrangle', {'filename': 'test.csv',
                                   'instruction': 'drop missing rows'})
    assert post_json(client, '/login', {'email': 'v@example.com',
                                        'password': 'password123'}
                     ).status_code == 200
    assert len(_rows(client, 'test.csv')) == 2          # the wrangled version
    log = post_json(client, '/dataset_versions', {'filename': 'test.csv'})
    assert log.get_json()['changelog']['active'] == 2


def test_login_never_overwrites_the_accounts_files(app, client):
    post_json(client, '/register', {'email': 'own@example.com',
                                    'password': 'password123'})
    upload_csv(client, OTHER_CSV)                         # account's test.csv
    post_json(client, '/logout', {})

    anon = app.test_client()
    upload_csv(anon, SAMPLE_CSV)                          # anon test.csv
    upload_csv(anon, OTHER_CSV, filename='extra.csv')
    assert post_json(anon, '/login', {'email': 'own@example.com',
                                      'password': 'password123'}
                     ).status_code == 200
    assert len(_rows(anon, 'test.csv')) == 2              # account copy kept
    assert len(_rows(anon, 'extra.csv')) == 2             # new file adopted


def test_adopted_anonymous_directory_is_emptied(app, client):
    upload_csv(client, SAMPLE_CSV)
    with client.session_transaction() as sess:
        sid = sess['sid']
    post_json(client, '/register', {'email': 'gone@example.com',
                                    'password': 'password123'})
    anon_dir = os.path.join(app.config['UPLOAD_FOLDER'], f'anon_{sid}')
    leftovers = os.listdir(anon_dir) if os.path.isdir(anon_dir) else []
    assert 'test.csv' not in leftovers
