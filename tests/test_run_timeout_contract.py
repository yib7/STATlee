"""A /run timeout is an executed failure, reported as such (L9, S5).

It used to be an HTTP 500 whose ``error`` was the sandbox's timeout message,
so the client had to recognise it by its text, and whatever the script had
printed before it was stopped was thrown away. It is now a 200 with
``timed_out: true``, ``success: false`` and the partial output.
"""
from conftest import SAMPLE_CSV, post_json, upload_csv

from statlee import sandbox

SLOW = ("print('started the model', flush=True)\n"
        "import time\n"
        "time.sleep(10)\n"
        "print('never printed')\n")


def test_sandbox_keeps_output_printed_before_a_timeout():
    res = sandbox.run_in_sandbox(SLOW, 'Python', timeout=2)
    assert res.timed_out is True
    assert 'started the model' in res.output
    assert 'never printed' not in res.output
    assert 'timed out after 2 seconds' in res.output


def test_run_timeout_is_a_200_with_the_flag_and_partial_output(app, client):
    app.config['STATLEE'].exec_timeout = 2
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/run', {'filename': 'test.csv', 'code': SLOW,
                                      'language': 'Python'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body['timed_out'] is True
    assert body['success'] is False
    assert body['returncode'] == -1
    assert body['language'] == 'Python'
    assert 'started the model' in body['output']
    assert 'timed out' in body['output'].lower()
    assert body['plots'] == []


def test_a_normal_run_reports_timed_out_false(client):
    upload_csv(client, SAMPLE_CSV)
    body = post_json(client, '/run', {'filename': 'test.csv',
                                      'code': "print('quick')",
                                      'language': 'Python'}).get_json()
    assert body['timed_out'] is False and body['success'] is True
