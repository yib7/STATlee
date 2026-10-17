"""Run output keeps one line break per line on every platform.

A script run on a Windows host prints "\\r\\n" line endings. The run store
wrote output.txt in text mode, which turned each "\\r\\n" into "\\r\\r\\n" on
disk, so a saved run (GET /runs/<id>) and the export's
outputs/run_NNNN/output.txt showed a blank line after every line.
"""
from conftest import SAMPLE_CSV, csrf_token, post_json, upload_csv

from statlee import sandbox, storage


def _identity(client):
    csrf_token(client)
    with client.session_transaction() as sess:
        return f"anon_{sess['sid']}"


def test_combined_output_uses_plain_newlines():
    out = sandbox._combine_output('a\r\nb\r\n', 'warn\r\n', 10_000)
    assert '\r' not in out
    assert out == 'a\nb\n\n--- Output/Warnings ---\nwarn'


def test_stored_output_reads_back_with_the_same_line_count(app, client):
    ident = _identity(client)
    with app.app_context():
        rid = storage.save_run('line1\r\nline2\r\nline3', [], script="print('x')",
                               language='Python', identity=ident, returncode=0)
    body = client.get(f'/runs/{rid}').get_json()
    assert body['output'] == 'line1\nline2\nline3'


def test_saved_run_output_matches_what_the_script_printed(client):
    upload_csv(client, SAMPLE_CSV)
    code = "print('alpha')\nprint('beta')\nprint('gamma')"
    live = post_json(client, '/run', {'filename': 'test.csv', 'code': code,
                                      'language': 'Python'})
    assert live.status_code == 200
    data = live.get_json()
    assert data['output'] == 'alpha\nbeta\ngamma'
    saved = client.get(f"/runs/{data['run_id']}").get_json()
    assert saved['output'] == 'alpha\nbeta\ngamma'
