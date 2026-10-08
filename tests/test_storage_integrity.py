"""Storage integrity under concurrency and approval scoping (deep audit D1-4,
D1-7) plus the persisted run outcome (D1-2)."""
import json
import os
import threading
from collections import Counter

from flask import session

from statlee import storage


def _req(app, sid='integrity-sid'):
    ctx = app.test_request_context()
    ctx.push()
    session['sid'] = sid
    return ctx


# ---------------------------------------------------------------------------
# D1-4: atomic writes and the approved-store read-modify-write
# ---------------------------------------------------------------------------

def test_write_json_atomic_survives_concurrent_writers(app):
    ctx = _req(app)
    try:
        path = os.path.join(storage.identity_root(), 'shared.json')
        storage._write_json_atomic(path, {'n': -1})
        errors = Counter()
        bad_reads = Counter()
        stop = threading.Event()

        def writer(k):
            for i in range(60):
                try:
                    storage._write_json_atomic(path, {'n': i, 'k': k,
                                                      'pad': 'x' * 4000})
                except Exception as e:   # noqa: BLE001 - counted, asserted below
                    errors[type(e).__name__] += 1

        def reader():
            while not stop.is_set():
                try:
                    with open(path, encoding='utf-8') as f:
                        json.load(f)
                except json.JSONDecodeError:
                    bad_reads['truncated'] += 1
                except OSError:
                    pass   # Windows: a file mid-replace can refuse an open

        rt = threading.Thread(target=reader)
        rt.start()
        ws = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
        for t in ws:
            t.start()
        for t in ws:
            t.join()
        stop.set()
        rt.join()

        assert not errors, errors
        assert not bad_reads, bad_reads
        with open(path, encoding='utf-8') as f:
            assert 'n' in json.load(f)
        leftovers = [f for f in os.listdir(storage.identity_root())
                     if f.endswith('.tmp')]
        assert leftovers == []
    finally:
        ctx.pop()


def test_concurrent_approved_saves_lose_nothing(app):
    ctx = _req(app)
    try:
        identity = storage.current_identity()
    finally:
        ctx.pop()

    lost = 0
    for trial in range(15):
        codes = [f"print('t{trial}-{i}')" for i in range(3)]
        barrier = threading.Barrier(len(codes))
        errors = []

        def save(code, barrier=barrier, errors=errors):
            with app.app_context():
                barrier.wait()
                try:
                    storage.save_approved_script(code, 'Python',
                                                 identity=identity)
                except Exception as e:   # noqa: BLE001
                    errors.append(e)

        ts = [threading.Thread(target=save, args=(c,)) for c in codes]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors, errors
        with app.app_context():
            missing = [c for c in codes
                       if not storage.is_approved_script(c, 'Python',
                                                         identity=identity)]
        lost += bool(missing)
    assert lost == 0, f'{lost}/15 trials lost an approved entry'


# ---------------------------------------------------------------------------
# D1-7: approval is scoped to the language it was moderated for
# ---------------------------------------------------------------------------

def test_approval_does_not_transfer_across_languages(app):
    ctx = _req(app)
    try:
        storage.save_approved_script("print('x')", 'Python')
        assert storage.is_approved_script("print('x')", 'Python')
        assert storage.is_approved_script("print('x')", 'python')
        assert not storage.is_approved_script("print('x')", 'R')
    finally:
        ctx.pop()


def test_run_as_other_language_never_runs_unreviewed(client, fake_llm):
    """D1-7: a script reviewed as Python must never run under Rscript without
    an R review. Since D4-29 the unchanged script runs under the language it
    WAS approved for (the dropdown switch is stale UI state), so it runs as
    Python, and never as unreviewed R."""
    from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv
    fake_llm.set('validation', 'x <- 1')
    upload_csv(client, SAMPLE_CSV)
    sse_events(post_json(client, '/chat', {'filename': 'test.csv',
                                           'prompt': 'go',
                                           'language': 'Python'}))
    fake_llm.block_code('not reviewed for R')
    resp = post_json(client, '/run', {'filename': 'test.csv',
                                      'code': 'x <- 1', 'language': 'R'})
    assert resp.get_json()['language'] == 'Python'
    from statlee import storage
    with client.session_transaction() as sess:
        ident = f"anon_{sess['sid']}"
    with client.application.app_context():
        record = storage.last_run_script(identity=ident)
    assert record['language'] == 'Python'


# ---------------------------------------------------------------------------
# D1-2: the run outcome is persisted with the artifacts
# ---------------------------------------------------------------------------

def test_last_run_status_roundtrip(app):
    ctx = _req(app)
    try:
        assert storage.last_run_status() is None
        storage.save_last_run('out', [], script='x', language='R',
                              returncode=1, timed_out=False)
        assert storage.last_run_status() == {'returncode': 1, 'timed_out': False}
        storage.save_last_run('Execution timed out', [], returncode=-1,
                              timed_out=True)
        assert storage.last_run_status() == {'returncode': -1, 'timed_out': True}
    finally:
        ctx.pop()
