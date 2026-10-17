"""SP1 session workbook: every run is kept under ``<identity>/runs/<run_id>/``
with an atomic ``runs/index.json``; the ``last_run_*`` API is a thin wrapper
over the latest run."""
import base64
import json
import os

import pytest

from statlee import storage

PNG = base64.b64encode(b'\x89PNG\r\n\x1a\nfake').decode('ascii')
META_KEYS = {'run_id', 'created_at', 'prompt', 'filename', 'dataset_version',
             'dataset_sha256', 'model', 'role', 'name', 'starred', 'methods'}


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield app


def _runs_dir(ident):
    return os.path.join(storage.identity_root(ident), storage.RUNS_DIR)


def test_save_run_allocates_sequential_ids_and_index(ctx):
    a = storage.save_run('out1', [], script='print(1)', language='Python',
                         identity='anon_wb1', returncode=0)
    b = storage.save_run('out2', [PNG], identity='anon_wb1', returncode=1)
    assert (a, b) == ('r0001', 'r0002')
    assert storage.latest_run_id(identity='anon_wb1') == 'r0002'
    with open(os.path.join(_runs_dir('anon_wb1'), 'index.json'),
              encoding='utf-8') as f:
        index = json.load(f)
    assert index['runs'] == ['r0001', 'r0002']
    assert index['latest'] == 'r0002'
    assert [r['run_id'] for r in storage.list_runs(identity='anon_wb1')] == [
        'r0001', 'r0002']


def test_run_files_and_meta_shape(ctx):
    rid = storage.save_run(
        'the output', [PNG, PNG], script='summary(df)', language='R',
        identity='anon_wb2', returncode=0, timed_out=False,
        prompt='Run OLS', filename='study.csv', dataset_version=3,
        dataset_sha256='a' * 64, model='gemini-x', role='draft')
    run_dir = os.path.join(_runs_dir('anon_wb2'), rid)
    names = set(os.listdir(run_dir))
    assert {'output.txt', 'script.json', 'status.json', 'meta.json',
            'plot_1.png', 'plot_2.png'} <= names
    with open(os.path.join(run_dir, 'meta.json'), encoding='utf-8') as f:
        meta = json.load(f)
    assert set(meta) >= META_KEYS
    assert meta['run_id'] == rid
    assert meta['prompt'] == 'Run OLS'
    assert meta['filename'] == 'study.csv'
    assert meta['dataset_version'] == 3
    assert meta['dataset_sha256'] == 'a' * 64
    assert meta['model'] == 'gemini-x'
    assert meta['role'] == 'draft'
    assert meta['name'] is None
    assert meta['starred'] is False
    assert meta['methods'] is None
    assert meta['created_at'].endswith('+00:00')
    with open(os.path.join(run_dir, 'script.json'), encoding='utf-8') as f:
        assert json.load(f) == {'code': 'summary(df)', 'language': 'R'}
    with open(os.path.join(run_dir, 'status.json'), encoding='utf-8') as f:
        assert json.load(f) == {'returncode': 0, 'timed_out': False}


def test_load_run_and_artifacts(ctx):
    rid = storage.save_run('OUT', [PNG], script='x=1', language='Python',
                           identity='anon_wb3', returncode=2, timed_out=True)
    storage.save_interpretation('INTERP', run_id=rid, identity='anon_wb3')
    run = storage.load_run(rid, identity='anon_wb3')
    assert run['run_id'] == rid
    assert run['output'] == 'OUT'
    assert run['script'] == {'code': 'x=1', 'language': 'Python'}
    assert run['status'] == {'returncode': 2, 'timed_out': True}
    assert run['interpretation'] == 'INTERP'
    assert len(run['plots']) == 1 and run['plots'][0].endswith('plot_1.png')
    assert run['meta']['run_id'] == rid
    output, plots = storage.run_artifacts(rid, identity='anon_wb3')
    assert output == 'OUT' and len(plots) == 1


def test_plots_sort_numerically(ctx):
    rid = storage.save_run('o', [PNG] * 11, identity='anon_wbsort')
    _out, plots = storage.run_artifacts(rid, identity='anon_wbsort')
    assert [os.path.basename(p) for p in plots][:3] == [
        'plot_1.png', 'plot_2.png', 'plot_3.png']
    assert os.path.basename(plots[-1]) == 'plot_11.png'


def test_save_interpretation_targets_run_or_latest(ctx):
    assert storage.save_interpretation('nothing', identity='anon_wb4') is None
    r1 = storage.save_run('one', [], identity='anon_wb4')
    r2 = storage.save_run('two', [], identity='anon_wb4')
    assert storage.save_interpretation('LATEST', identity='anon_wb4') == r2
    assert storage.save_interpretation('FIRST', run_id=r1,
                                       identity='anon_wb4') == r1
    assert storage.load_run(r1, identity='anon_wb4')['interpretation'] == 'FIRST'
    assert storage.load_run(r2, identity='anon_wb4')['interpretation'] == 'LATEST'
    assert storage.save_interpretation('x', run_id='r9999',
                                       identity='anon_wb4') is None


def test_update_run_meta(ctx):
    rid = storage.save_run('o', [], identity='anon_wb5')
    meta = storage.update_run_meta(rid, name='Main model', starred=True,
                                   identity='anon_wb5')
    assert meta['name'] == 'Main model' and meta['starred'] is True
    meta = storage.update_run_meta(rid, starred=False, identity='anon_wb5')
    assert meta['name'] == 'Main model' and meta['starred'] is False
    assert storage.update_run_meta('r0404', name='x',
                                   identity='anon_wb5') is None


@pytest.mark.parametrize('bad', [
    '', None, 'r1', 'r123', 'r1234567', '../r0001', 'r0001/..', 'R0001',
    'r0001 ', '..', 'r00\x001', 'x0001', 'r0001.json', '/etc/passwd', 12])
def test_run_id_validation_rejects_bad_ids(ctx, bad):
    storage.save_run('o', [], identity='anon_wb6')
    assert not storage.valid_run_id(bad)
    assert storage.load_run(bad, identity='anon_wb6') is None
    assert storage.run_artifacts(bad, identity='anon_wb6') == ('', [])
    assert storage.update_run_meta(bad, name='x', identity='anon_wb6') is None


def test_valid_run_id_accepts_four_to_six_digits():
    for good in ('r0001', 'r12345', 'r999999'):
        assert storage.valid_run_id(good)


def test_runs_are_isolated_per_identity(ctx):
    rid = storage.save_run('SECRET', [], identity='anon_owner')
    assert storage.load_run(rid, identity='anon_other') is None
    assert storage.list_runs(identity='anon_other') == []


def test_cap_evicts_oldest_unstarred(ctx):
    ctx.config['STATLEE'].max_runs_per_identity = 3
    ident = 'anon_wbcap'
    r1 = storage.save_run('1', [], identity=ident)
    storage.update_run_meta(r1, starred=True, identity=ident)
    for i in range(2, 6):
        storage.save_run(str(i), [], identity=ident)
    ids = [r['run_id'] for r in storage.list_runs(identity=ident)]
    assert ids == ['r0001', 'r0004', 'r0005']
    assert not os.path.isdir(os.path.join(_runs_dir(ident), 'r0002'))
    assert not os.path.isdir(os.path.join(_runs_dir(ident), 'r0003'))


def test_cap_evicts_oldest_when_all_starred(ctx):
    ctx.config['STATLEE'].max_runs_per_identity = 2
    ident = 'anon_wbstar'
    for i in range(2):
        rid = storage.save_run(str(i), [], identity=ident)
        storage.update_run_meta(rid, starred=True, identity=ident)
    storage.save_run('new', [], identity=ident)
    ids = [r['run_id'] for r in storage.list_runs(identity=ident)]
    assert ids == ['r0002', 'r0003']


def test_ids_are_never_reused_after_eviction(ctx):
    ctx.config['STATLEE'].max_runs_per_identity = 1
    ident = 'anon_wbreuse'
    storage.save_run('a', [], identity=ident)
    storage.save_run('b', [], identity=ident)
    assert storage.save_run('c', [], identity=ident) == 'r0003'


def test_corrupt_index_is_rebuilt_from_run_dirs(ctx):
    ident = 'anon_wbcorrupt'
    storage.save_run('a', [], identity=ident)
    storage.save_run('b', [], identity=ident)
    with open(os.path.join(_runs_dir(ident), 'index.json'), 'w',
              encoding='utf-8') as f:
        f.write('{not json')
    assert storage.latest_run_id(identity=ident) == 'r0002'
    assert storage.save_run('c', [], identity=ident) == 'r0003'


def test_legacy_last_run_dir_is_adopted_then_removed(ctx):
    ident = 'anon_wblegacy'
    legacy = os.path.join(storage.identity_root(ident), storage.LAST_RUN_DIR)
    os.makedirs(legacy)
    with open(os.path.join(legacy, 'output.txt'), 'w', encoding='utf-8') as f:
        f.write('LEGACY OUT')
    with open(os.path.join(legacy, 'script.json'), 'w', encoding='utf-8') as f:
        json.dump({'code': 'old()', 'language': 'R'}, f)
    with open(os.path.join(legacy, 'status.json'), 'w', encoding='utf-8') as f:
        json.dump({'returncode': 0, 'timed_out': False}, f)
    with open(os.path.join(legacy, 'interpretation.md'), 'w',
              encoding='utf-8') as f:
        f.write('LEGACY INTERP')
    with open(os.path.join(legacy, 'plot_1.png'), 'wb') as f:
        f.write(b'png')

    runs = storage.list_runs(identity=ident)
    assert [r['run_id'] for r in runs] == ['r0001']
    run = storage.load_run('r0001', identity=ident)
    assert run['output'] == 'LEGACY OUT'
    assert run['script'] == {'code': 'old()', 'language': 'R'}
    assert run['status'] == {'returncode': 0, 'timed_out': False}
    assert run['interpretation'] == 'LEGACY INTERP'
    assert len(run['plots']) == 1
    assert run['meta']['methods'] is None
    assert not os.path.exists(legacy)
    # The wrappers see it as the latest run too.
    assert storage.last_run_script(identity=ident) == {'code': 'old()',
                                                       'language': 'R'}


def test_last_run_wrappers_follow_latest_run(ctx):
    ident = 'anon_wbwrap'
    assert storage.last_run_artifacts(identity=ident) == ('', [])
    assert storage.last_run_time(identity=ident) is None
    storage.save_last_run('first', [], script='a', language='Python',
                          identity=ident, returncode=0)
    storage.save_last_interpretation('I1', identity=ident)
    storage.save_last_run('second', [PNG], identity=ident, returncode=1)
    out, plots = storage.last_run_artifacts(identity=ident)
    assert out == 'second' and len(plots) == 1
    assert storage.last_run_script(identity=ident) is None
    assert storage.last_run_status(identity=ident) == {'returncode': 1,
                                                       'timed_out': False}
    assert storage.last_interpretation(identity=ident) == ''
    assert isinstance(storage.last_run_time(identity=ident), float)
    # The older run keeps its interpretation.
    assert storage.load_run('r0001', identity=ident)['interpretation'] == 'I1'


def test_adopt_workspace_moves_runs(ctx):
    storage.save_run('ANON RUN', [], identity='anon_wbadopt')
    storage.identity_root('user_77')
    storage.adopt_workspace('anon_wbadopt', 'user_77')
    assert storage.load_run('r0001', identity='user_77')['output'] == 'ANON RUN'


def test_concurrent_saves_get_unique_ids(ctx):
    import threading
    ident = 'anon_wbconc'
    ids, errors = [], []

    def worker(i):
        try:
            with ctx.app_context():
                ids.append(storage.save_run(f'out {i}', [], identity=ident))
        except Exception as exc:   # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(ids) == [f'r{i:04d}' for i in range(1, 9)]
    listed = [r['run_id'] for r in storage.list_runs(identity=ident)]
    assert sorted(listed) == sorted(ids)


def test_max_runs_per_identity_setting(monkeypatch):
    from statlee.config import Config
    monkeypatch.setenv('MAX_RUNS_PER_IDENTITY', '7')
    assert Config.from_env().max_runs_per_identity == 7
    cfg = Config(env='testing', max_runs_per_identity=0)
    cfg.validate()
    assert cfg.max_runs_per_identity == 50


def test_adopt_workspace_replaces_an_empty_account_workbook(ctx):
    storage.save_run('ANON RUN', [], identity='anon_wbadopt2')
    assert storage.list_runs(identity='user_78') == []   # creates runs/
    storage.adopt_workspace('anon_wbadopt2', 'user_78')
    assert [r['run_id'] for r in storage.list_runs(identity='user_78')] == [
        'r0001']


def test_adopt_workspace_keeps_an_account_workbook(ctx):
    storage.save_run('ANON RUN', [], identity='anon_wbadopt3')
    storage.save_run('ACCOUNT RUN', [], identity='user_79')
    storage.adopt_workspace('anon_wbadopt3', 'user_79')
    assert storage.load_run('r0001', identity='user_79')['output'] == \
        'ACCOUNT RUN'


def test_save_run_caps_plots_per_run(ctx):
    """Final review item 4a: the store keeps at most PLOTS_MAX plots and
    PLOTS_BYTES_MAX plot bytes per run, whatever the caller passes."""
    from statlee import sandbox
    rid = storage.save_run('o', [PNG] * (sandbox.PLOTS_MAX + 5),
                           identity='anon_plotcap')
    assert len(storage.load_run(rid, identity='anon_plotcap')['plots']) == \
        sandbox.PLOTS_MAX


def test_eviction_keeps_the_workbook_under_its_byte_budget(ctx):
    """Final review item 4a: runs count toward MAX_BYTES_PER_IDENTITY, so the
    workbook evicts the oldest unstarred runs past its share of it."""
    from statlee import runs
    big = base64.b64encode(b'x' * 4000).decode('ascii')
    ctx.config['STATLEE'].max_bytes_per_identity = 40_000
    assert runs.byte_budget() == 40_000 * runs.RUNS_BYTES_SHARE_PERCENT // 100
    ident = 'anon_wbbytes'
    ids = [storage.save_run('o', [big], identity=ident) for _ in range(5)]
    kept = storage.run_ids(identity=ident)
    assert kept == ids[-2:]
    assert ctx.config['STATLEE'].max_runs_per_identity > len(ids)


def test_byte_eviction_never_drops_the_new_run(ctx):
    big = base64.b64encode(b'x' * 4000).decode('ascii')
    ctx.config['STATLEE'].max_bytes_per_identity = 4_000
    ident = 'anon_wbbytes2'
    storage.save_run('o', [big], identity=ident)
    new = storage.save_run('o', [big], identity=ident)
    assert storage.run_ids(identity=ident) == [new]


def test_locked_legacy_dir_is_left_in_place_and_adopted_later(
        ctx, monkeypatch):
    """Final review item 6: a locked file in a legacy .last_run dir (Windows)
    must not break every workbook read; adoption is retried later."""
    from statlee import runs
    ident = 'anon_wblocked'
    legacy = os.path.join(storage.identity_root(ident), storage.LAST_RUN_DIR)
    os.makedirs(legacy)
    with open(os.path.join(legacy, 'output.txt'), 'w', encoding='utf-8') as f:
        f.write('LOCKED LEGACY')
    real_rename = os.rename

    def locked(src, dst):
        if os.path.basename(src) == storage.LAST_RUN_DIR:
            raise PermissionError(32, 'file in use')
        return real_rename(src, dst)
    monkeypatch.setattr(runs.os, 'rename', locked)
    assert storage.list_runs(identity=ident) == []
    assert storage.latest_run_id(identity=ident) is None
    assert os.path.isdir(legacy)
    monkeypatch.setattr(runs.os, 'rename', real_rename)
    # The retry waits out the backoff (3A A4).
    monkeypatch.setattr(runs, '_monotonic',
                        lambda: 10 ** 9 + runs.ADOPT_RETRY_SECONDS)
    assert [r['run_id'] for r in storage.list_runs(identity=ident)] == [
        'r0001']
    assert storage.load_run('r0001', identity=ident)['output'] == \
        'LOCKED LEGACY'
    assert not os.path.exists(legacy)


def test_evictions_are_reported_and_logged_with_the_starred_flag(
        ctx, caplog):
    """3A A3: a starred run pushed out by the cap is no longer evicted
    silently: record_run reports it and the log says it was starred."""
    ctx.config['STATLEE'].max_runs_per_identity = 2
    ident = 'anon_wbevictlog'
    for i in range(2):
        rid = storage.save_run(str(i), [], identity=ident)
        storage.update_run_meta(rid, starred=True, name=f'Model {i}',
                                identity=ident)
    with caplog.at_level('INFO', logger='statlee.runs'):
        new, evicted = storage.record_run('new', [], identity=ident)
    assert new == 'r0003'
    assert evicted == [{'run_id': 'r0001', 'name': 'Model 0',
                        'starred': True}]
    lines = [r.getMessage() for r in caplog.records
             if r.name == 'statlee.runs']
    assert any('starred run r0001' in line and 'run cap' in line
               for line in lines), lines
    # Nothing evicted: an empty report.
    assert storage.record_run('x', [], identity='anon_wbevict2')[1] == []


def test_byte_budget_evictions_are_reported(ctx):
    big = base64.b64encode(b'x' * 4000).decode('ascii')
    ctx.config['STATLEE'].max_bytes_per_identity = 4_000
    ident = 'anon_wbbytes3'
    first = storage.save_run('o', [big], identity=ident)
    _new, evicted = storage.record_run('o', [big], identity=ident)
    assert [e['run_id'] for e in evicted] == [first]
    assert evicted[0]['starred'] is False


def test_a_locked_legacy_dir_backs_off_instead_of_flooding_the_log(
        ctx, monkeypatch, caplog):
    """3A A4: a legacy dir that cannot be renamed is retried at most every
    ADOPT_RETRY_SECONDS, without the index lock or a traceback per read."""
    from statlee import runs
    ident = 'anon_wbbackoff'
    legacy = os.path.join(storage.identity_root(ident), storage.LAST_RUN_DIR)
    os.makedirs(legacy)
    with open(os.path.join(legacy, 'output.txt'), 'w', encoding='utf-8') as f:
        f.write('LOCKED LEGACY')
    real_rename = os.rename
    attempts = []

    def locked(src, dst):
        if os.path.basename(src) == storage.LAST_RUN_DIR:
            attempts.append(src)
            raise PermissionError(32, 'file in use')
        return real_rename(src, dst)
    clock = [1000.0]
    monkeypatch.setattr(runs, '_monotonic', lambda: clock[0])
    monkeypatch.setattr(runs.os, 'rename', locked)
    locks = []
    real_lock = runs._lock
    monkeypatch.setattr(runs, '_lock',
                        lambda root: locks.append(root) or real_lock(root))
    with caplog.at_level('DEBUG', logger='statlee.runs'):
        storage.save_run('new', [], identity=ident)
        for _ in range(10):
            storage.list_runs(identity=ident)
            storage.run_meta('r0001', identity=ident)
        assert len(attempts) == 1
        # Only save_run took the lock after the first failed attempt.
        assert len(locks) == 2
        clock[0] += runs.ADOPT_RETRY_SECONDS + 1
        storage.list_runs(identity=ident)
        assert len(attempts) == 2
    warnings = [r for r in caplog.records
                if r.name == 'statlee.runs' and r.levelname == 'WARNING']
    assert len(warnings) == 1 and warnings[0].exc_info
    debug = [r for r in caplog.records
             if r.name == 'statlee.runs' and r.levelname == 'DEBUG']
    assert len(debug) == 1 and not debug[0].exc_info
    # Once the file is released the next retry adopts it.
    monkeypatch.setattr(runs.os, 'rename', real_rename)
    clock[0] += runs.ADOPT_RETRY_SECONDS + 1
    assert storage.run_ids(identity=ident) == ['r0002', 'r0001']
    assert not os.path.exists(legacy)
    assert legacy not in runs._adopt_failures
