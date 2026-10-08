"""SP1 workbook link on account history: POST /history records the run id
(and the run's dataset version and model, read server-side), and GET
/history says whether the linked run can still be restored in full."""
from conftest import post_json

from statlee import storage


def _register(client):
    post_json(client, '/register', {'email': 'wb@x.com',
                                    'password': 'longenough1'})


def test_history_records_run_link_from_server_meta(app, client):
    _register(client)
    with app.app_context():
        from statlee.extensions import db
        from statlee.models import User
        uid = db.session.execute(db.select(User.id)).scalar_one()
        rid = storage.save_run('o', [], identity=f'user_{uid}',
                               dataset_version=4, model='gem-x', role='draft')
    saved = post_json(client, '/history', {
        'prompt': 'p', 'run_id': rid, 'model': 'CLIENT_MODEL',
        'dataset_version': 99})
    assert saved.get_json()['saved'] is True
    row = client.get('/history').get_json()['runs'][0]
    assert row['run_id'] == rid
    assert row['dataset_version'] == 4
    assert row['model'] == 'gem-x'
    assert row['run_available'] is True


def test_history_ignores_unknown_run_id(client):
    _register(client)
    post_json(client, '/history', {'prompt': 'p', 'run_id': 'r0404'})
    post_json(client, '/history', {'prompt': 'q', 'run_id': '../etc'})
    rows = client.get('/history').get_json()['runs']
    assert all(r['run_id'] is None and r['run_available'] is False
               for r in rows)


def test_history_run_unavailable_after_eviction(app, client):
    _register(client)
    app.config['STATLEE'].max_runs_per_identity = 1
    with app.app_context():
        from statlee.extensions import db
        from statlee.models import User
        uid = db.session.execute(db.select(User.id)).scalar_one()
        rid = storage.save_run('o', [], identity=f'user_{uid}')
    post_json(client, '/history', {'prompt': 'p', 'run_id': rid})
    with app.app_context():
        storage.save_run('newer', [], identity=f'user_{uid}')
    row = client.get('/history').get_json()['runs'][0]
    assert row['run_id'] == rid and row['run_available'] is False
