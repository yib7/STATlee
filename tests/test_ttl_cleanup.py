"""Anonymous TTL cleanup expires whole idle workspaces, keyed on the session's
last activity (deep audit D1-3 / D5-16).

The old per-file sweep removed an active session's original upload and
metadata two hours after upload (reads never refresh a file's mtime) while
keeping the manifest and later versions, so revert 404'd and undo pointed at
a deleted file.
"""
import os
import time

from conftest import SAMPLE_CSV, csrf_token, post_json, upload_csv

from statlee import storage

OLD = time.time() - 10 * 3600


def _root(app, client):
    csrf_token(client)
    with client.session_transaction() as sess:
        identity = f"anon_{sess['sid']}"
    return os.path.join(app.config['UPLOAD_FOLDER'], identity)


def _age_files(root, when=OLD, skip=()):
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn not in skip:
                os.utime(os.path.join(dirpath, fn), (when, when))


def _cleanup(app, ttl=7200):
    with app.app_context():
        storage.cleanup_old_files(ttl)


def test_active_session_keeps_original_upload_and_history(app, client):
    upload_csv(client, SAMPLE_CSV)
    post_json(client, '/wrangle', {'filename': 'test.csv',
                                   'instruction': 'drop missing rows'})
    root = _root(app, client)
    # The original upload and its meta are old; the session itself is active.
    for fn in ('test.csv', '.meta__test.json'):
        os.utime(os.path.join(root, fn), (OLD, OLD))

    _cleanup(app)

    assert os.path.exists(os.path.join(root, 'test.csv'))
    revert = post_json(client, '/revert_dataset', {'filename': 'test.csv'})
    assert revert.status_code == 200, revert.get_json()
    undo = post_json(client, '/version_control',
                     {'filename': 'test.csv', 'direction': 'undo'})
    assert undo.get_json()['profile'] is not None


def test_reading_refreshes_activity(app, client):
    upload_csv(client, SAMPLE_CSV)
    root = _root(app, client)
    _age_files(root)                       # everything, marker included, is old
    assert post_json(client, '/data_page',
                     {'filename': 'test.csv'}).status_code == 200
    _cleanup(app)
    assert os.path.exists(os.path.join(root, 'test.csv'))


def test_idle_session_is_removed_as_a_whole(app, client):
    upload_csv(client, SAMPLE_CSV)
    post_json(client, '/wrangle', {'filename': 'test.csv',
                                   'instruction': 'drop missing rows'})
    root = _root(app, client)
    _age_files(root)
    _cleanup(app)
    assert not os.path.exists(root)


def test_idle_check_ignores_a_single_fresh_file_only_if_marker_old(app, client):
    """Activity is the newest of the marker and any file, so a workspace with
    one recently written file is not expired even if the marker is old."""
    upload_csv(client, SAMPLE_CSV)
    root = _root(app, client)
    _age_files(root, skip=('test.csv',))
    _cleanup(app)
    assert os.path.exists(os.path.join(root, '.meta__test.json'))


def test_logged_in_workspace_is_not_expired(app, client):
    post_json(client, '/register', {'email': 'ttl@example.com',
                                    'password': 'longenough1'})
    upload_csv(client, SAMPLE_CSV)
    user_dirs = [d for d in os.listdir(app.config['UPLOAD_FOLDER'])
                 if d.startswith('user_')]
    assert user_dirs
    root = os.path.join(app.config['UPLOAD_FOLDER'], user_dirs[0])
    _age_files(root)
    _cleanup(app)
    assert os.path.exists(os.path.join(root, 'test.csv'))
