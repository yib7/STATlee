"""Sign-in reports which anonymous datasets moved into the account (S5).

Adoption at sign-in (L2) skips a dataset whose name already exists in the
account, so the account copy wins. That used to be silent: the client could
only warn generically. /login and /register now return
``workspace: {moved: [...], skipped: [...]}`` with the stored dataset names.
"""
from conftest import SAMPLE_CSV, post_json, upload_csv

OTHER_CSV = "a,b\n1,2\n3,4\n"
ACCOUNT = {'email': 'own@example.com', 'password': 'password123'}


def test_register_reports_the_moved_datasets(client):
    upload_csv(client, SAMPLE_CSV)
    upload_csv(client, OTHER_CSV, filename='extra.csv')
    resp = post_json(client, '/register', {'email': 'new@example.com',
                                           'password': 'password123'})
    assert resp.status_code == 201
    workspace = resp.get_json()['workspace']
    assert sorted(workspace['moved']) == ['extra.csv', 'test.csv']
    assert workspace['skipped'] == []


def test_login_reports_a_name_collision_as_skipped(app, client):
    post_json(client, '/register', ACCOUNT)
    upload_csv(client, OTHER_CSV)                         # account's test.csv
    post_json(client, '/logout', {})

    anon = app.test_client()
    upload_csv(anon, SAMPLE_CSV)                          # anon test.csv
    upload_csv(anon, OTHER_CSV, filename='extra.csv')
    resp = post_json(anon, '/login', ACCOUNT)
    assert resp.status_code == 200
    workspace = resp.get_json()['workspace']
    assert workspace['skipped'] == ['test.csv']
    assert workspace['moved'] == ['extra.csv']


def test_login_with_nothing_to_adopt_reports_empty_lists(client):
    post_json(client, '/register', ACCOUNT)
    post_json(client, '/logout', {})
    workspace = post_json(client, '/login', ACCOUNT).get_json()['workspace']
    assert workspace == {'moved': [], 'skipped': []}
