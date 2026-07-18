"""Storage layer: path containment (1.1), identity isolation, and dataset
version control with undo/redo (5.16)."""
import json
import os
import time

import pandas as pd
from flask import session

from statlee import storage


def _req(app, sid='session-a'):
    """A request context with a fixed session id (stable identity)."""
    ctx = app.test_request_context()
    ctx.push()
    session['sid'] = sid
    return ctx


def test_resolve_path_rejects_empty(app):
    ctx = _req(app)
    try:
        assert storage.resolve_path('') is None
        assert storage.resolve_path(None) is None
    finally:
        ctx.pop()


def test_resolve_path_contains_traversal(app):
    """secure_filename + realpath keep traversal attempts inside the root."""
    ctx = _req(app)
    try:
        root = os.path.realpath(storage.identity_root())
        evil = storage.resolve_path('../../../../etc/passwd')
        assert evil is not None
        assert os.path.commonpath([evil, root]) == root
        assert 'passwd' in os.path.basename(evil)
    finally:
        ctx.pop()


def test_identity_isolation(app):
    """Two sessions get distinct roots and cannot see each other's files."""
    ctx_a = _req(app, 'sid-a')
    try:
        path_a = storage.resolve_path('data.csv')
        with open(path_a, 'w') as f:
            f.write('a')
        root_a = storage.identity_root()
    finally:
        ctx_a.pop()

    ctx_b = _req(app, 'sid-b')
    try:
        root_b = storage.identity_root()
        path_b = storage.resolve_path('data.csv')
        assert root_a != root_b
        assert path_a != path_b
        assert not os.path.exists(path_b)   # B's namespace is empty
    finally:
        ctx_b.pop()


def test_version_control_undo_redo(app):
    ctx = _req(app)
    try:
        # Seed an original dataset and register it.
        path = storage.resolve_path('study.csv')
        df0 = pd.DataFrame({'x': [1, 2, 3]})
        df0.to_csv(path, index=False)
        storage.register_dataset('study.csv')

        # v2: a transformed frame.
        df1 = pd.DataFrame({'x': [1, 2]})
        storage.add_dataset_version('study.csv', df1, 'Dropped a row')
        log = storage.dataset_changelog('study.csv')
        assert log['active'] == 2
        assert log['can_undo'] is True
        assert log['can_redo'] is False

        # Undo -> active back to v1, redo available.
        storage.shift_dataset_version('study.csv', 'undo')
        log = storage.dataset_changelog('study.csv')
        assert log['active'] == 1
        assert log['can_redo'] is True
        active = pd.read_csv(storage.active_dataset_path('study.csv'))
        assert len(active) == 3

        # Redo -> v2 again.
        storage.shift_dataset_version('study.csv', 'redo')
        active = pd.read_csv(storage.active_dataset_path('study.csv'))
        assert len(active) == 2
    finally:
        ctx.pop()


def test_new_edit_truncates_redo_branch(app):
    ctx = _req(app)
    try:
        path = storage.resolve_path('t.csv')
        pd.DataFrame({'x': [1, 2, 3]}).to_csv(path, index=False)
        storage.register_dataset('t.csv')
        storage.add_dataset_version('t.csv', pd.DataFrame({'x': [1, 2]}), 'v2')
        storage.shift_dataset_version('t.csv', 'undo')   # back to v1
        # A new edit from v1 should discard the old v2 (redo branch).
        storage.add_dataset_version('t.csv', pd.DataFrame({'x': [9]}), 'v2-new')
        log = storage.dataset_changelog('t.csv')
        assert log['active'] == 2
        assert log['can_redo'] is False
        assert log['versions'][-1]['instruction'] == 'v2-new'
    finally:
        ctx.pop()


def test_approved_script_roundtrip(app):
    ctx = _req(app)
    try:
        assert storage.load_approved_script() is None
        storage.save_approved_script("print('hi')", 'Python')
        loaded = storage.load_approved_script()
        assert loaded['code'] == "print('hi')"
        assert loaded['language'] == 'Python'
    finally:
        ctx.pop()


def test_approved_script_store_is_hash_keyed(app):
    """P2-2: the approved-script store keeps scripts keyed by content hash, so a
    later save (e.g. a /wrangle transform) does NOT evict an earlier /chat
    script -- both stay recognized by membership, and genuinely-new code does
    not."""
    ctx = _req(app)
    try:
        storage.save_approved_script("print('chat')", 'Python')
        # A /wrangle save used to clobber the single slot; now it coexists.
        storage.save_approved_script("df = df.dropna()", 'Python')
        assert storage.is_approved_script("print('chat')")
        assert storage.is_approved_script("df = df.dropna()")
        # Genuinely-new/edited code is NOT approved (still gets re-moderated).
        assert not storage.is_approved_script("import os; os.system('x')")
        # load_approved_script keeps returning the most-recent for /export.
        assert storage.load_approved_script()['code'] == "df = df.dropna()"
    finally:
        ctx.pop()


def test_approved_script_store_evicts_oldest(app):
    """P2-2: only the last few scripts are retained (insertion-order eviction),
    so the store cannot grow without bound."""
    ctx = _req(app)
    try:
        for i in range(7):
            storage.save_approved_script(f"print({i})", 'Python')
        # Store cap is 5; the two oldest are evicted.
        assert not storage.is_approved_script("print(0)")
        assert not storage.is_approved_script("print(1)")
        for i in range(2, 7):
            assert storage.is_approved_script(f"print({i})")
    finally:
        ctx.pop()


def test_approved_script_reads_legacy_single_slot(app):
    """P2-2: an existing on-disk store in the OLD single-slot shape
    ({code, language}) is still honored after upgrade."""
    import json as _json
    ctx = _req(app)
    try:
        path = os.path.join(storage.identity_root(), storage.APPROVED_SCRIPT)
        with open(path, 'w', encoding='utf-8') as f:
            _json.dump({'code': "print('legacy')", 'language': 'Python'}, f)
        assert storage.is_approved_script("print('legacy')")
        assert storage.load_approved_script()['code'] == "print('legacy')"
    finally:
        ctx.pop()


def test_last_run_script_roundtrips_producing_script(app):
    """P2-1: save_last_run persists the script that produced the run in a
    sidecar so /export and /interpret can read exactly what ran, independent of
    the shared approved-script store (which a later /wrangle poisons)."""
    ctx = _req(app)
    try:
        # No run recorded yet -> None.
        assert storage.last_run_script() is None
        # A run WITHOUT a script (back-compat callers) leaves no sidecar.
        storage.save_last_run('some output', [])
        assert storage.last_run_script() is None
        # A run WITH a script round-trips code + language.
        storage.save_last_run('out', [], script='ANALYSIS', language='Python')
        assert storage.last_run_script() == {'code': 'ANALYSIS',
                                             'language': 'Python'}
        # A later run without a script wipes the sidecar (rundir is recreated).
        storage.save_last_run('newer output', [])
        assert storage.last_run_script() is None
    finally:
        ctx.pop()


def test_add_dataset_version_concurrent_no_lost_update(app):
    """P2-9: two concurrent add_dataset_version calls on the same dataset must
    yield two DISTINCT versions -- the per-manifest lock prevents the second
    save from silently discarding the first's entry (lost update)."""
    import threading

    ident = 'anon_race'
    with app.app_context():
        path = storage.resolve_path('t.csv', identity=ident)
        pd.DataFrame({'x': [1, 2, 3]}).to_csv(path, index=False)
        storage.register_dataset('t.csv', identity=ident)  # v1

    barrier = threading.Barrier(2)
    errors = []

    def worker(n):
        try:
            with app.app_context():
                barrier.wait(timeout=5)  # maximize interleaving overlap
                storage.add_dataset_version(
                    't.csv', pd.DataFrame({'x': [n]}), f'edit-{n}',
                    identity=ident)
        except Exception as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (10, 20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    with app.app_context():
        log = storage.dataset_changelog('t.csv', identity=ident)
    versions = sorted(v['v'] for v in log['versions'])
    # v1 original + two distinct new versions; without the lock one is lost.
    assert versions == [1, 2, 3]


def test_reset_identity_clears_workspace(app):
    ctx = _req(app)
    try:
        path = storage.resolve_path('keep.csv')
        with open(path, 'w') as f:
            f.write('x')
        assert os.path.exists(path)
        storage.reset_identity()
        assert not os.path.exists(path)
    finally:
        ctx.pop()


# ---------------------------------------------------------------------------
# P1-3 — atomic JSON writes / corrupt-manifest tracing
# ---------------------------------------------------------------------------

def test_load_manifest_logs_warning_on_corrupt_json(app, caplog):
    """A truncated/invalid manifest must not fail silently: `_load_manifest`
    should return None AND leave a trace in the logs (the caller falls back
    to re-registering a fresh v1 manifest, silently losing history)."""
    ctx = _req(app)
    try:
        path = storage._manifest_path('study.csv')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{not valid json')

        with caplog.at_level('WARNING', logger='statlee.storage'):
            result = storage._load_manifest('study.csv')

        assert result is None
        assert any('study.csv' in r.message or path in r.message
                    or 'Corrupt' in r.message or 'corrupt' in r.message
                    for r in caplog.records)
    finally:
        ctx.pop()


def test_write_json_atomic_leaves_original_intact_on_failed_write(app, monkeypatch):
    """If json.dump raises partway through a save, the previously-written
    destination file must be untouched (no truncation, no history loss)."""
    ctx = _req(app)
    try:
        path = storage.resolve_path('atomic.json')
        original = {'versions': [{'v': 1}], 'active': 1}
        storage._write_json_atomic(path, original)
        with open(path, encoding='utf-8') as f:
            assert json.load(f) == original

        def boom(*_a, **_kw):
            raise RuntimeError('simulated crash mid-write')

        monkeypatch.setattr(json, 'dump', boom)
        try:
            storage._write_json_atomic(path, {'versions': [], 'active': 999})
        except RuntimeError:
            pass

        # The real destination must still hold the ORIGINAL, complete content.
        with open(path, encoding='utf-8') as f:
            assert json.load(f) == original
    finally:
        ctx.pop()


def test_save_manifest_survives_simulated_crash(app, monkeypatch):
    """End-to-end: a manifest saved via _save_manifest survives a simulated
    failed re-save (the add_dataset_version silent-reset scenario)."""
    ctx = _req(app)
    try:
        path = storage.resolve_path('crash.csv')
        pd.DataFrame({'x': [1, 2, 3]}).to_csv(path, index=False)
        manifest = storage.register_dataset('crash.csv')
        assert manifest['active'] == 1

        def boom(*_a, **_kw):
            raise RuntimeError('simulated crash mid-write')

        monkeypatch.setattr(json, 'dump', boom)
        try:
            storage._save_manifest('crash.csv', {'versions': [], 'active': 999})
        except RuntimeError:
            pass

        monkeypatch.undo()
        reloaded = storage._load_manifest('crash.csv')
        assert reloaded is not None
        assert reloaded['active'] == 1
        assert len(reloaded['versions']) == 1
    finally:
        ctx.pop()


# ---------------------------------------------------------------------------
# P1-6 — TTL cleanup prunes empty subdirs
# ---------------------------------------------------------------------------

def test_cleanup_old_files_prunes_empty_subdirs(app):
    """A leftover empty .last_run/ subdir (after its file ages out) must not
    strand the identity dir forever — the whole anon_ dir should be removed
    once every file and subdir underneath it is gone."""
    ctx = _req(app, 'anon-cleanup-sid')
    try:
        identity = storage.current_identity()
        assert identity.startswith('anon_')
        root = storage.identity_root()

        run_dir = os.path.join(root, storage.LAST_RUN_DIR)
        os.makedirs(run_dir, exist_ok=True)
        stale_file = os.path.join(run_dir, 'output.txt')
        with open(stale_file, 'w', encoding='utf-8') as f:
            f.write('old run output')

        old_time = time.time() - 999999
        os.utime(stale_file, (old_time, old_time))

        storage.cleanup_old_files(ttl_seconds=1)
    finally:
        ctx.pop()

    assert not os.path.isdir(run_dir)
    assert not os.path.isdir(root)


def test_cleanup_old_files_removes_empty_subdir_even_without_stale_files(app):
    """An already-empty subdir (e.g. a stray .last_run/ with no files) should
    be pruned so it doesn't keep the identity dir non-empty forever."""
    ctx = _req(app, 'anon-empty-subdir-sid')
    try:
        root = storage.identity_root()
        empty_dir = os.path.join(root, storage.LAST_RUN_DIR)
        os.makedirs(empty_dir, exist_ok=True)

        storage.cleanup_old_files(ttl_seconds=7200)
    finally:
        ctx.pop()

    assert not os.path.isdir(empty_dir)
    assert not os.path.isdir(root)
