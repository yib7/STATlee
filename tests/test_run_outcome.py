"""/interpret picks auto-debug mode from the real run outcome (deep audit D1-2 /
D3-14): the sandbox return code and timeout flag persisted with the last run,
not a guess from the output text."""
from conftest import SAMPLE_CSV, csrf_token, post_json, sse_events, upload_csv

from statlee import storage

R_ERROR = ('[1] "loaded"\n--- Output/Warnings ---\n'
           'Error in lm(y ~ x) : object \'y\' not found\nExecution halted')


def _identity(client):
    csrf_token(client)
    with client.session_transaction() as sess:
        return f"anon_{sess['sid']}"


def _interpret(client):
    events = sse_events(post_json(client, '/interpret', {}))
    done = [e for e in events if e.get('type') == 'done']
    assert done, events
    return done[0]['debug']


def test_r_error_with_nonzero_returncode_is_debug(app, client, fake_llm):
    identity = _identity(client)
    with app.app_context():
        storage.save_run(R_ERROR, [], script='lm(y ~ x)', language='R',
                         returncode=1, timed_out=False, identity=identity)
    assert _interpret(client) is True
    assert any(c[1] == 'interpret_debug' for c in fake_llm.calls)


def test_timeout_is_debug(app, client):
    identity = _identity(client)
    with app.app_context():
        storage.save_run('Execution timed out after 60 seconds. Code took '
                         'too long to run.', [], script='while True: pass',
                         language='Python', returncode=-1, timed_out=True,
                         identity=identity)
    assert _interpret(client) is True


def test_successful_output_starting_with_error_is_not_debug(app, client):
    identity = _identity(client)
    with app.app_context():
        storage.save_run('Error rate: 4.2%\nAccuracy: 0.958', [],
                         script="print('Error rate: 4.2%')",
                         language='Python', returncode=0,
                         timed_out=False, identity=identity)
    assert _interpret(client) is False


def test_legacy_run_without_status_detects_r_error(app, client):
    """Run dirs written before the status file existed fall back to a text
    heuristic, which now also recognises R errors anywhere in the output."""
    identity = _identity(client)
    with app.app_context():
        storage.save_run(R_ERROR, [], identity=identity)
    assert _interpret(client) is True


def test_real_failed_run_switches_interpret_to_debug(client, fake_llm):
    """End to end: a script that prints normally and then exits non-zero (no
    traceback in the output) is still interpreted as a failure."""
    script = "print('loaded')\nraise SystemExit(3)"
    fake_llm.set('validation', script)
    upload_csv(client, SAMPLE_CSV)
    sse_events(post_json(client, '/chat', {'filename': 'test.csv',
                                           'prompt': 'go'}))
    run = post_json(client, '/run', {'filename': 'test.csv', 'code': script,
                                     'language': 'Python'})
    assert run.get_json()['returncode'] == 3
    assert _interpret(client) is True
