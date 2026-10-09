"""Session workbook (SP1): every analysis run is kept, not just the last one.

Layout, per identity::

    <identity_root>/runs/
        index.json          {"runs": [ids in order], "latest": id, "next": n}
        .index.lock         cross-process lock for allocation, eviction, meta
        r0001/
            output.txt
            script.json     {code, language}
            status.json     {returncode, timed_out}
            plot_N.png
            interpretation.md
            meta.json       {run_id, created_at, prompt, filename,
                             dataset_version, dataset_sha256, model, role,
                             name, starred, methods}

A run is written into a hidden temp dir and renamed into place while the
index lock is held, then the index is replaced atomically, so a reader that
goes through the index never sees a half-written run. ``run_id`` inputs are
validated (``^r[0-9]{4,6}$``), must be listed in the caller's own index, and
are resolved under the caller's identity root only.

The ``storage.last_run_*`` functions are thin wrappers over the latest run.
A legacy ``.last_run/`` dir (one-run layout) is adopted as the first run on
first access and then removed.

This module is re-exported from ``statlee.storage``; it imports storage
lazily because storage imports it at module load.
"""
import base64
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import UTC, datetime

from filelock import FileLock
from flask import current_app

logger = logging.getLogger('statlee.runs')

RUNS_DIR = 'runs'
INDEX_FILE = 'index.json'
LOCK_FILE = '.index.lock'
OUTPUT_FILE = 'output.txt'
SCRIPT_FILE = 'script.json'
STATUS_FILE = 'status.json'
META_FILE = 'meta.json'
INTERPRETATION_FILE = 'interpretation.md'

RUNS_MAX = 50              # default for MAX_RUNS_PER_IDENTITY
# The workbook's share of MAX_BYTES_PER_IDENTITY (default 200 MB -> 50 MB of
# runs). Past it the oldest unstarred runs are evicted, as past the run cap,
# so stored plots never fill the quota an upload needs ("storage full" with
# no visible cause). Plots per run are capped in statlee/sandbox.py
# (PLOTS_MAX = 20, PLOTS_BYTES_MAX = 10 MB), and save_run applies the same
# caps to what it is given.
RUNS_BYTES_SHARE_PERCENT = 25
RUNS_BYTES_DEFAULT = 50 * 1024 * 1024   # when MAX_BYTES_PER_IDENTITY is 0/unset
RUN_NAME_MAX = 80
RUN_PROMPT_MAX = 4000      # the question kept with a run
RUN_FIELD_MAX = 64         # model / role
PROMPT_EXCERPT_MAX = 120

_RUN_ID_RE = re.compile(r'r[0-9]{4,6}')
_PLOT_RE = re.compile(r'plot_([0-9]+)\.png')
_TMP_PREFIX = '.tmp-'
_LOCK_TIMEOUT = 10
_UNSET = object()


def _st():
    from . import storage
    return storage


# ---------------------------------------------------------------------------
# ids, paths, index
# ---------------------------------------------------------------------------

def valid_run_id(run_id):
    """True for a well-formed run id such as ``r0001`` (4 to 6 ASCII digits)."""
    return isinstance(run_id, str) and bool(_RUN_ID_RE.fullmatch(run_id))


def _cap():
    try:
        value = int(current_app.config['STATLEE'].max_runs_per_identity)
    except (KeyError, AttributeError, TypeError, ValueError):
        value = RUNS_MAX
    return value if value >= 1 else RUNS_MAX


def byte_budget():
    """Bytes the workbook may hold: RUNS_BYTES_SHARE_PERCENT of the identity's
    MAX_BYTES_PER_IDENTITY, leaving the rest for uploads and versions."""
    try:
        total = int(current_app.config['STATLEE'].max_bytes_per_identity)
    except (KeyError, AttributeError, TypeError, ValueError):
        total = 0
    if total <= 0:
        return RUNS_BYTES_DEFAULT
    return total * RUNS_BYTES_SHARE_PERCENT // 100


def _dir_bytes(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.stat(os.path.join(dirpath, fn)).st_size
            except OSError:
                continue
    return total


def _runs_root(identity=None):
    path = os.path.join(_st().identity_root(identity), RUNS_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def _lock(root):
    return FileLock(os.path.join(root, LOCK_FILE), timeout=_LOCK_TIMEOUT)


def _load_index(root):
    """The index, self-healing: entries whose dir is gone are dropped, and a
    missing or corrupt file is rebuilt from the run dirs (in id order)."""
    data = _st()._read_json(os.path.join(root, INDEX_FILE))
    if (isinstance(data, dict) and isinstance(data.get('runs'), list)
            and all(valid_run_id(r) for r in data['runs'])):
        runs = list(dict.fromkeys(data['runs']))
        nxt = data.get('next') if isinstance(data.get('next'), int) else 0
    else:
        try:
            names = os.listdir(root)
        except OSError:
            names = []
        runs = sorted((n for n in names if valid_run_id(n)),
                      key=lambda n: int(n[1:]))
        nxt = 0
    runs = [r for r in runs if os.path.isdir(os.path.join(root, r))]
    nxt = max([nxt, 1] + [int(r[1:]) + 1 for r in runs])
    return {'runs': runs, 'latest': runs[-1] if runs else None, 'next': nxt}


def _save_index(root, index):
    index['latest'] = index['runs'][-1] if index['runs'] else None
    _st()._write_json_atomic(os.path.join(root, INDEX_FILE), {
        'runs': index['runs'], 'latest': index['latest'],
        'next': index['next']})


def _allocate(index):
    run_id = f"r{index['next']:04d}"
    index['next'] += 1
    return run_id


def _new_tmp_dir(root):
    path = os.path.join(root, f'{_TMP_PREFIX}{uuid.uuid4().hex}')
    os.makedirs(path)
    return path


def _sweep(root, index):
    """Under the lock: remove temp dirs left by a crashed writer and run dirs
    no longer in the index (an eviction whose delete failed)."""
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if name.startswith(_TMP_PREFIX) or (
                valid_run_id(name) and name not in index['runs']):
            shutil.rmtree(path, ignore_errors=True)


def _run_dir(run_id, identity=None):
    """The caller's own dir for ``run_id``, or None for a malformed, unknown
    or foreign id."""
    if not valid_run_id(run_id):
        return None
    _ensure_adopted(identity)
    root = os.path.realpath(_runs_root(identity))
    path = os.path.realpath(os.path.join(root, run_id))
    if os.path.dirname(path) != root or not os.path.isdir(path):
        return None
    if run_id not in _load_index(root)['runs']:
        return None
    return path


# ---------------------------------------------------------------------------
# file helpers
# ---------------------------------------------------------------------------

def _now_iso(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts,
                                  UTC).isoformat()


def _iso_to_ts(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _clamp_str(value, limit):
    if value is None:
        return None
    value = str(value)
    return value[:limit] if value else None


def _write_text_atomic(path, text):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.',
                               suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text or '')
        _st()._retry_on_windows_lock(os.replace, tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_text(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


def _plot_paths(run_dir):
    try:
        names = os.listdir(run_dir)
    except OSError:
        return []
    plots = [(int(m.group(1)), n) for n in names
             if (m := _PLOT_RE.fullmatch(n))]
    return [os.path.join(run_dir, n) for _i, n in sorted(plots)]


def _read_meta(run_dir):
    data = _st()._read_json(os.path.join(run_dir, META_FILE))
    return data if isinstance(data, dict) else {}


def _read_script(run_dir):
    data = _st()._read_json(os.path.join(run_dir, SCRIPT_FILE))
    if not isinstance(data, dict) or not isinstance(data.get('code'), str):
        return None
    return {'code': data['code'], 'language': data.get('language') or 'Python'}


def _read_status(run_dir):
    data = _st()._read_json(os.path.join(run_dir, STATUS_FILE))
    if not isinstance(data, dict) or not isinstance(data.get('returncode'), int):
        return None
    return {'returncode': data['returncode'],
            'timed_out': bool(data.get('timed_out'))}


def _new_meta(run_id, created_ts=None, prompt=None, filename=None,
              dataset_version=None, dataset_sha256=None, model=None,
              role=None, methods=None, prompt_inherited=False):
    if isinstance(dataset_version, bool) or not isinstance(dataset_version, int):
        dataset_version = None
    return {
        'run_id': run_id,
        'created_at': _now_iso(created_ts),
        'prompt': _clamp_str(prompt, RUN_PROMPT_MAX),
        # True when ``prompt`` is the latest generation's question borrowed
        # for an edited or pasted script, not the question that produced it.
        'prompt_inherited': bool(prompt_inherited),
        'filename': _clamp_str(filename, 255),
        'dataset_version': dataset_version,
        'dataset_sha256': _clamp_str(dataset_sha256, 64),
        'model': _clamp_str(model, RUN_FIELD_MAX),
        'role': _clamp_str(role, RUN_FIELD_MAX),
        'name': None,
        'starred': False,
        # SP3: the parsed Methods card (statlee.methods.parse) /run passes
        # in. Null for runs saved before SP3; readers parse those lazily
        # from output.txt (statlee.methods.for_run).
        'methods': methods,
    }


# ---------------------------------------------------------------------------
# legacy adoption
# ---------------------------------------------------------------------------

def _ensure_adopted(identity=None):
    """Adopt a legacy ``.last_run/`` dir as a run (the first one), once."""
    st = _st()
    legacy = os.path.join(st.identity_root(identity), st.LAST_RUN_DIR)
    if not os.path.isdir(legacy):
        return
    root = _runs_root(identity)
    with _lock(root):
        if not os.path.isdir(legacy):
            return
        out_path = os.path.join(legacy, OUTPUT_FILE)
        if not os.path.exists(out_path):
            shutil.rmtree(legacy, ignore_errors=True)
            return
        try:
            created = os.path.getmtime(out_path)
        except OSError:
            created = None
        index = _load_index(root)
        _sweep(root, index)
        run_id = _allocate(index)
        tmp = os.path.join(root, f'{_TMP_PREFIX}{uuid.uuid4().hex}')
        # A rename, not a copy: if it fails nothing moved, and once it worked
        # the legacy dir is gone, so a run is never adopted twice.
        os.rename(legacy, tmp)
        try:
            st._write_json_atomic(os.path.join(tmp, META_FILE),
                                  _new_meta(run_id, created_ts=created))
            os.rename(tmp, os.path.join(root, run_id))
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        # "The first run": an older run goes before any already listed.
        index['runs'].insert(0, run_id)
        _save_index(root, index)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def save_run(output, plots_b64, script=None, language=None, identity=None,
             returncode=None, timed_out=False, prompt=None, filename=None,
             dataset_version=None, dataset_sha256=None, model=None, role=None,
             methods=None, prompt_inherited=False):
    """Persist one run's artifacts as a new run and return its ``run_id``.

    ``script`` is the exact script that produced the run (P2-1/P2-7: /export
    and /interpret read it, never the shared approved store's "most recent").
    ``returncode``/``timed_out`` are the real sandbox outcome (D1-2).
    ``prompt``, ``filename``, ``dataset_version``, ``dataset_sha256``,
    ``model`` and ``role`` record what produced it, for provenance;
    ``prompt_inherited`` marks a prompt borrowed from another generation.

    Allocation, write, index update and eviction all happen under the index
    lock. Over ``MAX_RUNS_PER_IDENTITY`` runs or ``byte_budget()`` bytes the
    oldest unstarred run is evicted (the oldest run when all are starred);
    the new run is never the victim.
    """
    st = _st()
    _ensure_adopted(identity)
    root = _runs_root(identity)
    doomed = []
    with _lock(root):
        index = _load_index(root)
        _sweep(root, index)
        run_id = _allocate(index)
        tmp = _new_tmp_dir(root)
        try:
            with open(os.path.join(tmp, OUTPUT_FILE), 'w',
                      encoding='utf-8') as f:
                f.write(output or '')
            if script is not None:
                st._write_json_atomic(
                    os.path.join(tmp, SCRIPT_FILE),
                    {'code': script, 'language': language or 'Python'})
            if returncode is not None:
                st._write_json_atomic(
                    os.path.join(tmp, STATUS_FILE),
                    {'returncode': int(returncode),
                     'timed_out': bool(timed_out)})
            _write_plots(tmp, plots_b64, run_id)
            st._write_json_atomic(os.path.join(tmp, META_FILE), _new_meta(
                run_id, prompt=prompt, filename=filename,
                dataset_version=dataset_version,
                dataset_sha256=dataset_sha256, model=model, role=role,
                methods=methods, prompt_inherited=prompt_inherited))
            os.rename(tmp, os.path.join(root, run_id))
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        index['runs'].append(run_id)
        doomed = _evictions(root, index)
        _save_index(root, index)
        # The index no longer lists them; a failed delete is swept later.
        for victim in doomed:
            shutil.rmtree(os.path.join(root, victim), ignore_errors=True)
    return run_id


def _write_plots(run_dir, plots_b64, run_id):
    """Persist at most PLOTS_MAX plots and PLOTS_BYTES_MAX bytes of them (the
    sandbox already caps what a run returns; this bounds any other caller)."""
    from .sandbox import PLOTS_BYTES_MAX, PLOTS_MAX
    written = 0
    for i, b64 in enumerate((plots_b64 or [])[:PLOTS_MAX], start=1):
        try:
            data = base64.b64decode(b64)
            if written + len(data) > PLOTS_BYTES_MAX:
                logger.warning("Run %s: plot byte cap reached at plot %d",
                               run_id, i)
                break
            with open(os.path.join(run_dir, f'plot_{i}.png'), 'wb') as f:
                f.write(data)
            written += len(data)
        except Exception:
            logger.warning("Could not persist plot %d of run %s", i, run_id)


def _evictions(root, index):
    """Runs to drop so the workbook holds at most ``_cap()`` runs and
    ``byte_budget()`` bytes: the oldest unstarred run first (the oldest run
    when all are starred); never the run just saved."""
    cap = _cap()
    budget = byte_budget()
    sizes = {r: _dir_bytes(os.path.join(root, r)) for r in index['runs']}
    doomed = []
    while len(index['runs']) > 1 and (
            len(index['runs']) > cap
            or sum(sizes[r] for r in index['runs']) > budget):
        candidates = index['runs'][:-1]       # never the run just saved
        victim = next(
            (r for r in candidates
             if not _read_meta(os.path.join(root, r)).get('starred')),
            candidates[0])
        index['runs'].remove(victim)
        doomed.append(victim)
    return doomed


def run_ids(identity=None):
    """The caller's run ids, oldest first."""
    _ensure_adopted(identity)
    return list(_load_index(_runs_root(identity))['runs'])


def latest_meta_for_script(code, language, identity=None):
    """Meta of the most recent run that executed exactly ``code`` under
    ``language``, or None. Lets a re-run keep the provenance of the run it
    repeats after its approval left the (small) approved-script store."""
    _ensure_adopted(identity)
    root = _runs_root(identity)
    language = language or 'Python'
    for run_id in reversed(_load_index(root)['runs']):
        run_dir = os.path.join(root, run_id)
        script = _read_script(run_dir)
        if script and script['code'] == code and script['language'] == language:
            return _read_meta(run_dir)
    return None


def latest_run_id(identity=None):
    _ensure_adopted(identity)
    return _load_index(_runs_root(identity))['latest']


def _summary(run_dir, run_id):
    meta = _read_meta(run_dir)
    status = _read_status(run_dir) or {}
    prompt = meta.get('prompt') if isinstance(meta.get('prompt'), str) else ''
    script = _read_script(run_dir)
    return {
        'run_id': run_id,
        'name': meta.get('name') if isinstance(meta.get('name'), str) else None,
        'created_at': meta.get('created_at'),
        'prompt_excerpt': prompt[:PROMPT_EXCERPT_MAX],
        'starred': bool(meta.get('starred')),
        'has_plots': bool(_plot_paths(run_dir)),
        'returncode': status.get('returncode'),
        'timed_out': bool(status.get('timed_out')),
        'filename': meta.get('filename'),
        'model': meta.get('model'),
        'language': (script or {}).get('language'),
    }


def list_runs(identity=None):
    """Summaries of the caller's runs, oldest first."""
    _ensure_adopted(identity)
    root = _runs_root(identity)
    return [_summary(os.path.join(root, r), r)
            for r in _load_index(root)['runs']]


def run_summary(run_id, identity=None):
    run_dir = _run_dir(run_id, identity)
    return _summary(run_dir, run_id) if run_dir else None


def load_run(run_id, identity=None):
    """Everything stored for one run, or None for a malformed/unknown id:
    ``{run_id, meta, output, script, status, interpretation, plots}`` where
    ``plots`` are file paths."""
    run_dir = _run_dir(run_id, identity)
    if not run_dir:
        return None
    return {
        'run_id': run_id,
        'meta': _read_meta(run_dir),
        'output': _read_text(os.path.join(run_dir, OUTPUT_FILE)),
        'script': _read_script(run_dir),
        'status': _read_status(run_dir),
        'interpretation': _read_text(os.path.join(run_dir,
                                                  INTERPRETATION_FILE)),
        'plots': _plot_paths(run_dir),
    }


def run_artifacts(run_id, identity=None):
    """``(output, plot_paths)`` of one run; ``('', [])`` when unknown."""
    run_dir = _run_dir(run_id, identity)
    if not run_dir:
        return '', []
    return _read_text(os.path.join(run_dir, OUTPUT_FILE)), _plot_paths(run_dir)


def run_script(run_id, identity=None):
    run_dir = _run_dir(run_id, identity)
    return _read_script(run_dir) if run_dir else None


def run_status(run_id, identity=None):
    run_dir = _run_dir(run_id, identity)
    return _read_status(run_dir) if run_dir else None


def run_interpretation(run_id, identity=None):
    run_dir = _run_dir(run_id, identity)
    return _read_text(os.path.join(run_dir, INTERPRETATION_FILE)) \
        if run_dir else ''


def run_meta(run_id, identity=None):
    run_dir = _run_dir(run_id, identity)
    return _read_meta(run_dir) if run_dir else None


def run_time(run_id, identity=None):
    """Epoch seconds the run was recorded, or None."""
    run_dir = _run_dir(run_id, identity)
    if not run_dir:
        return None
    ts = _iso_to_ts(_read_meta(run_dir).get('created_at'))
    if ts is None:
        try:
            ts = os.path.getmtime(os.path.join(run_dir, OUTPUT_FILE))
        except OSError:
            return None
    return ts


def interpretation_time(run_id, identity=None):
    """Epoch seconds the run's interpretation was last written, or None when
    it has none (SP4 provenance)."""
    run_dir = _run_dir(run_id, identity)
    if not run_dir:
        return None
    path = os.path.join(run_dir, INTERPRETATION_FILE)
    try:
        if not os.path.getsize(path):
            return None
        return os.path.getmtime(path)
    except OSError:
        return None


def save_interpretation(text, run_id=None, identity=None):
    """Store the interpretation of ``run_id`` (default: the latest run) next
    to its output, so /converse and /generate_report ground on the server's
    copy (D3-2). Returns the run id written, or None when there is no such
    run (a no-op)."""
    if run_id is None:
        run_id = latest_run_id(identity)
    run_dir = _run_dir(run_id, identity) if run_id else None
    if not run_dir:
        return None
    _write_text_atomic(os.path.join(run_dir, INTERPRETATION_FILE), text)
    return run_id


def update_run_meta(run_id, name=_UNSET, starred=_UNSET, identity=None):
    """Rename and/or (un)star a run. ``name=None`` or '' clears the name.
    Returns the updated meta, or None for a malformed/unknown id."""
    run_dir = _run_dir(run_id, identity)
    if not run_dir:
        return None
    with _lock(os.path.dirname(run_dir)):
        if not os.path.isdir(run_dir):
            return None
        meta = _read_meta(run_dir) or _new_meta(run_id)
        if name is not _UNSET:
            name = (str(name).strip() if name is not None else '')
            meta['name'] = name[:RUN_NAME_MAX] or None
        if starred is not _UNSET:
            meta['starred'] = bool(starred)
        _st()._write_json_atomic(os.path.join(run_dir, META_FILE), meta)
        return meta
