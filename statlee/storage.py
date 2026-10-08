"""Per-identity file storage with dataset version control.

Implements:
- 1.1  Per-session file isolation — every browser session (or logged-in user)
       gets its own namespaced directory; ``resolve_path`` keeps its
       realpath-containment check so no endpoint can reach another identity's
       files.
- 5.16 Dataset versioning — wrangle operations write ``stem__vN.csv`` files
       tracked by a JSON manifest with an active-version pointer (undo/redo).
- 7.3/7.4 The local backend namespaces by user id when logged in
       (``user_<id>/``) and by session id otherwise (``anon_<sid>/``); an S3
       backend can mirror the same key layout when STORAGE_BACKEND=s3.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import time
import unicodedata

from filelock import FileLock
from flask import current_app, session
from werkzeug.utils import secure_filename

from .identity import current_user_or_none

logger = logging.getLogger('statlee.storage')

MANIFEST_PREFIX = '.versions__'
META_PREFIX = '.meta__'
APPROVED_SCRIPT = '.approved_script.json'
LAST_RUN_DIR = '.last_run'

# P2-2: the approved-script store keeps the last few validated scripts keyed by
# content hash (not a single slot), so a /wrangle save cannot evict the /chat
# script and force a spurious re-moderation of already-approved code.
APPROVED_SCRIPT_MAX = 5

# P2-9: the version manifest is read-modify-written; a cross-process file lock
# on the manifest serializes concurrent wrangles/undos so no version entry is
# lost. Held only for the brief RMW; the timeout guards against a stale lock.
_MANIFEST_LOCK_TIMEOUT = 10


_WIN_RETRIES = 50


def _retry_on_windows_lock(fn, *args):
    """Run ``fn(*args)``, retrying briefly on PermissionError. On Windows a file
    that another thread has open cannot be replaced or opened mid-replace; on
    POSIX this never triggers."""
    for attempt in range(_WIN_RETRIES):
        try:
            return fn(*args)
        except PermissionError:
            if os.name != 'nt' or attempt == _WIN_RETRIES - 1:
                raise
            time.sleep(0.005 * (attempt + 1))
    return None  # pragma: no cover - loop always returns or raises


def _write_json_atomic(path, obj):
    """Write JSON via a temp file + ``os.replace`` (atomic on POSIX and
    Windows) so a crash mid-write can never leave a truncated/partial file
    at ``path`` — the previous, complete version stays intact until the
    new one is fully written.

    D1-4: the temp file is UNIQUE per write (``mkstemp`` in the same dir).
    A shared ``path + '.tmp'`` let two overlapping writers replace each
    other's half-written temp file, which put truncated JSON at ``path``."""
    import tempfile
    directory = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.',
                               suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f)
        _retry_on_windows_lock(os.replace, tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path):
    """Load JSON from ``path``; None when missing or corrupt."""
    def _load():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    if not os.path.exists(path):
        return None
    try:
        return _retry_on_windows_lock(_load)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Identity & roots
# ---------------------------------------------------------------------------

def current_identity():
    """Stable storage namespace for the requesting principal.

    Logged-in users get ``user_<id>`` (durable across sessions); anonymous
    sessions get ``anon_<sid>`` (cleaned up by TTL).
    """
    try:
        u = current_user_or_none()
        if u is not None:
            return f"user_{u.id}"
    except Exception:  # login manager not initialised (tests/scripts)
        pass
    sid = session.get('sid')
    if not sid:
        # before_request normally sets this; be defensive for direct calls.
        import secrets
        sid = secrets.token_hex(16)
        session['sid'] = sid
    return f"anon_{sid}"


def storage_root():
    return current_app.config['UPLOAD_FOLDER']


LAST_SEEN = '.last_seen'
_ACTIVITY_TOUCH_INTERVAL = 60   # seconds; bounds the touch to ~1 write/minute


def _touch_activity(root):
    """Record that this workspace was used (D1-3). Every storage access goes
    through ``identity_root``, so reads count as activity too; throttled so a
    burst of requests costs one ``stat`` each, not one write each."""
    marker = os.path.join(root, LAST_SEEN)
    try:
        if time.time() - os.stat(marker).st_mtime < _ACTIVITY_TOUCH_INTERVAL:
            return
    except OSError:
        pass
    try:
        with open(marker, 'a', encoding='utf-8'):
            pass
        os.utime(marker, None)
    except OSError:
        pass


def identity_root(identity=None):
    identity = identity or current_identity()
    root = os.path.join(storage_root(), identity)
    os.makedirs(root, exist_ok=True)
    _touch_activity(root)
    return root


_VERSION_ARTIFACT_RE = re.compile(r'__v\d+\.')


def identity_usage(identity=None):
    """Return ``(dataset_count, total_bytes)`` for the caller's storage dir (P2-4).

    ``dataset_count`` counts distinct uploaded files at the top level (hidden
    sidecars and ``{stem}__vN.csv`` version artifacts excluded), so wrangling a
    dataset into many versions does not, by itself, consume the upload-count
    quota. ``total_bytes`` is the true on-disk size of everything the identity
    has stored (uploads, version artifacts, sidecars, last-run plots), so the
    byte cap reflects real disk pressure. A small over-count is acceptable: the
    quota check that uses this fails safe toward rejecting.
    """
    root = identity_root(identity)
    dataset_count = 0
    total_bytes = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            try:
                total_bytes += os.stat(os.path.join(dirpath, fn)).st_size
            except OSError:
                continue
            if (dirpath == root and not fn.startswith('.')
                    and not _VERSION_ARTIFACT_RE.search(fn)):
                dataset_count += 1
    return dataset_count, total_bytes


_EXT_RE = re.compile(r'\.[a-z0-9]{1,8}')
_UPLOAD_STEM_MAX = 100


def upload_name(raw_filename):
    """Map a client upload filename to ``(stored_name, ext)`` (D1-1).

    ``secure_filename`` drops every non-ASCII character, so ``数据.csv`` used to
    become ``csv`` (no extension, unparseable) and every Cyrillic PDF collapsed
    to ``pdf``. Here the extension is split off the RAW name first and always
    kept, so the caller validates the same extension it stores. Latin accents
    still transliterate (``café`` -> ``cafe``). When the stem loses any
    non-ASCII character, an 8-hex digest of the original stem is appended, so
    distinct non-Latin names never collide while the same name maps to the
    same stored file (a re-upload replaces it). An empty stem falls back to
    ``upload_<digest>``. ``ext`` is lowercase and '' when it is not a plain
    short ASCII extension.
    """
    base = (raw_filename or '').replace('\\', '/').rsplit('/', 1)[-1]
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if not _EXT_RE.fullmatch(ext):
        ext = ''
    decomposed = unicodedata.normalize('NFKD', stem)
    lossy = any(ord(c) > 127 and not unicodedata.combining(c)
                for c in decomposed)
    safe_stem = secure_filename(stem)[:_UPLOAD_STEM_MAX].strip('._')
    if lossy or not safe_stem:
        digest = hashlib.sha256(stem.encode('utf-8')).hexdigest()[:8]
        safe_stem = f'{safe_stem}_{digest}' if safe_stem else f'upload_{digest}'
    return safe_stem + ext, ext


INCOMING_PREFIX = '.incoming_'


def incoming_path(ext, identity=None):
    """A unique hidden staging path for an upload in progress. The route parses
    and validates the file here and only moves it to its real name on success,
    so a failed upload never leaves a stray file behind, and a failed re-upload
    never clobbers the dataset it would have replaced (D1-8)."""
    import secrets
    return os.path.join(identity_root(identity),
                        f'{INCOMING_PREFIX}{secrets.token_hex(8)}{ext}')


def resolve_path(filename, identity=None):
    """Map a client-supplied filename to a safe path inside the caller's
    own directory. Returns None for empty names or traversal attempts."""
    if not filename:
        return None
    safe_name = secure_filename(filename)
    if not safe_name:
        return None
    root = os.path.realpath(identity_root(identity))
    full_path = os.path.realpath(os.path.join(root, safe_name))
    if os.path.commonpath([full_path, root]) != root:
        return None
    return full_path


# ---------------------------------------------------------------------------
# Dataset version manifest (5.16)
# ---------------------------------------------------------------------------

def _manifest_path(filename, identity=None):
    stem = os.path.splitext(secure_filename(filename))[0]
    return os.path.join(identity_root(identity), f"{MANIFEST_PREFIX}{stem}.json")


def _load_manifest(filename, identity=None):
    path = _manifest_path(filename, identity)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "Corrupt/unreadable manifest %s; returning None "
            "(history may be re-initialized)", path)
        return None


def _save_manifest(filename, manifest, identity=None):
    _write_json_atomic(_manifest_path(filename, identity), manifest)


def _manifest_lock(filename, identity=None):
    """A cross-process lock guarding one dataset's manifest read-modify-write.

    The deployed worker model is multi-process (gunicorn ``--workers`` with
    ``--threads`` each), so an in-process ``threading.Lock`` would only serialize
    within a single worker. A ``filelock`` on ``<manifest>.lock`` serializes
    across workers AND threads, closing the lost-update window in P2-9 where two
    concurrent wrangles both compute the same ``next_v`` and the second save
    discards the first's entry. Each caller builds a fresh lock and never nests
    another manifest lock inside it, so there is no re-entrancy or deadlock.
    """
    return FileLock(_manifest_path(filename, identity) + '.lock',
                    timeout=_MANIFEST_LOCK_TIMEOUT)


def register_dataset(filename, identity=None):
    """Initialise the version manifest for a freshly uploaded dataset.

    A re-upload under the same name starts a fresh history; the previous
    history's version files are deleted so they neither linger on disk nor
    count toward the byte quota (D1-14)."""
    previous = _load_manifest(filename, identity)
    if previous:
        keep = secure_filename(filename)
        for v in previous.get('versions', []):
            old = v.get('file') if isinstance(v, dict) else None
            if old and secure_filename(old) != keep:
                path = resolve_path(old, identity)
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        logger.warning("Could not remove old version %s", path)
    manifest = {
        'filename': secure_filename(filename),
        'versions': [{
            'v': 1,
            'file': secure_filename(filename),
            'instruction': 'Original upload',
            'summary': None,
            'ts': time.time(),
        }],
        'active': 1,
    }
    _save_manifest(filename, manifest, identity)
    return manifest


def active_dataset_path(filename, identity=None):
    """Resolve the ACTIVE version of a dataset (falls back to the plain file)."""
    manifest = _load_manifest(filename, identity)
    if manifest:
        active = next((v for v in manifest['versions']
                       if v['v'] == manifest['active']), None)
        if active:
            path = resolve_path(active['file'], identity)
            if path and os.path.exists(path):
                return path
    return resolve_path(filename, identity)


def _truncate_redo_branch(manifest):
    """Drop any versions after the active pointer — a new edit (or revert)
    after an Undo replaces the redo branch, like an editor's undo stack."""
    manifest['versions'] = [v for v in manifest['versions']
                            if v['v'] <= manifest['active']]


class VersionConflict(Exception):
    """The active version moved while a transform derived from it was running
    (L1): committing it would silently discard the other edit."""


def _active_entry(manifest):
    return next((v for v in manifest['versions']
                 if v['v'] == manifest['active']), None)


def add_dataset_version(filename, new_df, instruction, summary=None,
                        identity=None, code=None, base_file=None):
    """Persist a wrangled DataFrame as the next version and activate it.

    ``instruction`` is the user's own words; ``summary`` is the applied,
    past-tense description (used to render the cleaning history as a chat-style
    transcript). A new edit after an Undo truncates the redo branch.

    ``code`` is the transform that produced the frame (D6), kept on the version
    entry so an export can replay the cleaning from the original upload.
    ``base_file`` is the version file the transform read; when another
    wrangle/undo/revert has moved the active version since, the commit is
    refused with ``VersionConflict`` (L1) instead of stacking a frame derived
    from the old version on top of the new one.
    """
    # P2-9: serialize the whole read-modify-write so a concurrent wrangle/undo
    # cannot compute the same next_v and silently drop this version.
    with _manifest_lock(filename, identity):
        manifest = (_load_manifest(filename, identity)
                    or register_dataset(filename, identity))
        if base_file is not None:
            active = _active_entry(manifest)
            if active is None or active['file'] != base_file:
                raise VersionConflict(filename)
        _truncate_redo_branch(manifest)
        next_v = manifest['versions'][-1]['v'] + 1
        stem = os.path.splitext(manifest['filename'])[0]
        version_file = f"{stem}__v{next_v}.csv"
        version_path = resolve_path(version_file, identity)
        new_df.to_csv(version_path, index=False)
        manifest['versions'].append({
            'v': next_v,
            'file': version_file,
            'instruction': instruction,
            'summary': summary,
            'code': code,
            'op': 'transform',
            'ts': time.time(),
        })
        manifest['active'] = next_v
        _save_manifest(filename, manifest, identity)
        return manifest


def revert_to_original(filename, identity=None):
    """Restore the dataset to its first-uploaded state as a NEW version (4.6).

    Implemented as a forward edit (copy v1's bytes into a fresh version) rather
    than a pointer jump, so reverting is itself undo-able — one Undo brings the
    pre-revert state back. Returns the manifest, or ``None`` for an unknown
    dataset or a missing original file.
    """
    # P2-9: same manifest read-modify-write, same lock.
    with _manifest_lock(filename, identity):
        manifest = _load_manifest(filename, identity)
        if not manifest:
            return None
        original = min(manifest['versions'], key=lambda v: v['v'])
        orig_path = resolve_path(original['file'], identity)
        if not orig_path or not os.path.exists(orig_path):
            return None
        _truncate_redo_branch(manifest)
        next_v = manifest['versions'][-1]['v'] + 1
        stem = os.path.splitext(manifest['filename'])[0]
        version_file = f"{stem}__v{next_v}.csv"
        version_path = resolve_path(version_file, identity)
        shutil.copyfile(orig_path, version_path)
        manifest['versions'].append({
            'v': next_v,
            'file': version_file,
            'instruction': 'Reverted to original upload',
            'summary': None,
            'code': None,
            'op': 'revert',
            'ts': time.time(),
        })
        manifest['active'] = next_v
        _save_manifest(filename, manifest, identity)
        return manifest


def shift_dataset_version(filename, direction, identity=None):
    """Move the active-version pointer (undo: -1, redo: +1)."""
    # P2-9: the pointer move is a manifest read-modify-write too; lock it so a
    # concurrent add/undo cannot overwrite the other's result.
    with _manifest_lock(filename, identity):
        manifest = _load_manifest(filename, identity)
        if not manifest:
            return None
        versions = sorted(v['v'] for v in manifest['versions'])
        idx = versions.index(manifest['active'])
        new_idx = idx + (1 if direction == 'redo' else -1)
        if new_idx < 0 or new_idx >= len(versions):
            return manifest  # already at the edge -- no-op
        manifest['active'] = versions[new_idx]
        _save_manifest(filename, manifest, identity)
        return manifest


def dataset_lineage(filename, identity=None):
    """What an export needs to reproduce the active version (D6).

    Returns None for an unknown dataset, else a dict with ``original`` (path
    of the v1 file), ``active_v``/``latest_v``, and ``steps``: the transform
    entries that turn the original into the active version, in order. A revert
    restarts the lineage from the original; versions past the active pointer
    (an undone redo branch) are not part of it. ``replayable`` is False when a
    step predates code recording (D6) and so cannot be replayed."""
    manifest = _load_manifest(filename, identity)
    if not manifest or not manifest.get('versions'):
        return None
    versions = sorted(manifest['versions'], key=lambda v: v['v'])
    original = resolve_path(versions[0]['file'], identity)
    steps = []
    for v in versions[1:]:
        if v['v'] > manifest['active']:
            break
        if v.get('op') == 'revert' or v.get('instruction') == \
                'Reverted to original upload':
            steps = []
        else:
            steps.append(v)
    return {
        'original': original,
        'active_v': manifest['active'],
        'latest_v': versions[-1]['v'],
        'steps': steps,
        'replayable': all(isinstance(s.get('code'), str) and s['code'].strip()
                          for s in steps),
    }


def dataset_changelog(filename, identity=None):
    manifest = _load_manifest(filename, identity)
    if not manifest:
        return None
    return {
        'active': manifest['active'],
        'versions': [
            {'v': v['v'], 'instruction': v['instruction'],
             'summary': v.get('summary'), 'ts': v['ts']}
            for v in manifest['versions']
        ],
        'can_undo': manifest['active'] > min(v['v'] for v in manifest['versions']),
        'can_redo': manifest['active'] < max(v['v'] for v in manifest['versions']),
    }


# ---------------------------------------------------------------------------
# Dataset metadata sidecar (sha256 hash for the 5.6 cache, native labels…)
# ---------------------------------------------------------------------------

def _meta_path(filename, identity=None):
    stem = os.path.splitext(secure_filename(filename))[0]
    return os.path.join(identity_root(identity), f"{META_PREFIX}{stem}.json")


def save_dataset_meta(filename, meta, identity=None):
    _write_json_atomic(_meta_path(filename, identity), meta)


def load_dataset_meta(filename, identity=None):
    path = _meta_path(filename, identity)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Approved-script store (0.4 run-guard) and last-run artifacts (5.3 export)
# ---------------------------------------------------------------------------

def _lang_key(language):
    return str(language or 'Python').strip().lower()


def _hash_code(code, language='Python'):
    """Approval key: the script AND the interpreter it was reviewed for (D1-7).
    A Python-moderated script must not run under Rscript without review."""
    material = f"{_lang_key(language)}\0{code or ''}"
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _approved_path(identity=None):
    return os.path.join(identity_root(identity), APPROVED_SCRIPT)


def _load_approved_store(identity=None):
    """Return the approved-script store as ``{key: entry}`` in insertion order
    (oldest first), where ``entry`` is ``{code, language, source?, ts?}`` and
    ``key`` is recomputed from (language, code). Tolerates a missing/corrupt
    file and transparently upgrades an OLD single-slot file
    (``{code, language}``) and stores keyed by the old code-only hash."""
    data = _read_json(_approved_path(identity))
    if not isinstance(data, dict):
        return {}
    # Back-compat: an old single-slot file {code, language} -> one entry.
    if isinstance(data.get('code'), str):
        data = {'legacy': data}
    store = {}
    for v in data.values():
        if isinstance(v, dict) and isinstance(v.get('code'), str):
            if not isinstance(v.get('language'), str):
                v = dict(v, language='Python')
            store[_hash_code(v['code'], v['language'])] = v
    return store


def save_approved_script(code, language, identity=None, source='chat',
                         context=None):
    """Record a validated script keyed by (language, content hash) (P2-2,
    D1-7).

    Keeps the most recent ``APPROVED_SCRIPT_MAX`` scripts; re-saving a known
    script refreshes its recency. This is what lets a /wrangle transform and the
    earlier /chat script coexist, so re-running the unchanged chat code still
    matches by membership and skips re-moderation. ``source`` ('chat', 'run' or
    'wrangle') lets /export skip wrangle transforms.

    The read-modify-write runs under a file lock (D1-4): two concurrent saves
    used to drop each other's entry.

    ``context`` (D6): what the script was generated for, as a small JSON-able
    dict (``prompt``, ``filename``, ``columns``, ``codebook``). /interpret
    reads it back from this server-side record, so its grounding cannot be
    spoofed by the client. Re-saving a known script without a context keeps
    the one already stored.
    """
    with FileLock(_approved_path(identity) + '.lock',
                  timeout=_MANIFEST_LOCK_TIMEOUT):
        store = _load_approved_store(identity)
        sha = _hash_code(code, language)
        previous = store.pop(sha, None)           # re-insert -> most-recent
        entry = {'code': code, 'language': language, 'source': source,
                 'ts': time.time()}
        if context is None and isinstance(previous, dict):
            context = previous.get('context')
        if context:
            entry['context'] = context
        store[sha] = entry
        while len(store) > APPROVED_SCRIPT_MAX:   # evict oldest by insertion
            del store[next(iter(store))]
        _write_json_atomic(_approved_path(identity), store)


def is_approved_script(code, language='Python', identity=None):
    """True if ``code`` exactly matches a script previously approved for
    ``language`` (P2-2, D1-7)."""
    return _hash_code(code, language) in _load_approved_store(identity)


def approved_entry(code, language='Python', identity=None):
    """The store entry ``{code, language, source, ts}`` approving ``code`` for
    ``language``, or None."""
    return _load_approved_store(identity).get(_hash_code(code, language))


def load_approved_script(identity=None, exclude_sources=()):
    """Return the MOST-RECENTLY approved script ``{code, language, ...}`` or
    None, skipping entries whose ``source`` is in ``exclude_sources``.

    Retained for /export (bundles the latest script). Membership tests use
    ``is_approved_script`` instead of equality against this single entry.
    """
    store = _load_approved_store(identity)
    for entry in reversed(list(store.values())):
        if entry.get('source') not in exclude_sources:
            return entry
    return None


def save_last_run(output, plots_b64, script=None, language=None, identity=None,
                  returncode=None, timed_out=False):
    """Keep the latest run's artifacts server-side so /export can bundle them.

    When ``script`` is provided, the exact script that PRODUCED this run is
    persisted alongside the output as a small ``script.json`` sidecar. /export
    and the /interpret grounded-debug path read THAT (via ``last_run_script``)
    so a later /wrangle — which saves its own transform into the shared
    approved-script store and thereby becomes "most recent" — cannot substitute
    the transform snippet for the analysis script that actually ran (P2-1/P2-7).
    ``save_last_run`` wipes and recreates the run dir on every call, so omitting
    ``script`` leaves no stale sidecar behind.

    ``returncode``/``timed_out`` (D1-2) record the real sandbox outcome in a
    ``status.json`` sidecar so /interpret can choose debug mode from it rather
    than from the output text (R errors and timeouts used to be missed).
    """
    import base64
    run_dir = os.path.join(identity_root(identity), LAST_RUN_DIR)
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, 'output.txt'), 'w', encoding='utf-8') as f:
        f.write(output or '')
    if script is not None:
        _write_json_atomic(
            os.path.join(run_dir, 'script.json'),
            {'code': script, 'language': language or 'Python'})
    if returncode is not None:
        _write_json_atomic(
            os.path.join(run_dir, 'status.json'),
            {'returncode': int(returncode), 'timed_out': bool(timed_out)})
    for i, b64 in enumerate(plots_b64 or [], start=1):
        try:
            with open(os.path.join(run_dir, f'plot_{i}.png'), 'wb') as f:
                f.write(base64.b64decode(b64))
        except Exception:
            logger.warning("Could not persist plot %d for export", i)


INTERPRETATION_FILE = 'interpretation.md'


def save_last_interpretation(text, identity=None):
    """Keep the interpretation of the last run next to its output (D3-2), so
    /converse and /generate_report can ground on the server's copy instead of
    a client-supplied one. A new run wipes the run dir, so a stale
    interpretation never outlives the run it describes. No-op without a run."""
    run_dir = os.path.join(identity_root(identity), LAST_RUN_DIR)
    if not os.path.isdir(run_dir):
        return
    path = os.path.join(run_dir, INTERPRETATION_FILE)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text or '')
    _retry_on_windows_lock(os.replace, tmp, path)


def last_interpretation(identity=None):
    """The stored interpretation of the last run, or ''."""
    path = os.path.join(identity_root(identity), LAST_RUN_DIR,
                        INTERPRETATION_FILE)
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


def last_run_script(identity=None):
    """Return the script that produced the last run as ``{code, language}``.

    Reads the ``script.json`` sidecar written by ``save_last_run``. Returns
    None when no run has recorded a script (or the sidecar is missing/corrupt),
    tolerating a bad file the same way the other loaders do."""
    run_dir = os.path.join(identity_root(identity), LAST_RUN_DIR)
    path = os.path.join(run_dir, 'script.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('code'), str):
        return None
    return {'code': data['code'], 'language': data.get('language') or 'Python'}


def last_run_status(identity=None):
    """``{'returncode': int, 'timed_out': bool}`` for the last run, or None for
    a run dir written before the outcome was recorded (D1-2)."""
    path = os.path.join(identity_root(identity), LAST_RUN_DIR, 'status.json')
    data = _read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get('returncode'), int):
        return None
    return {'returncode': data['returncode'],
            'timed_out': bool(data.get('timed_out'))}


def last_run_time(identity=None):
    """Epoch seconds of the last run, or None."""
    path = os.path.join(identity_root(identity), LAST_RUN_DIR, 'output.txt')
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def last_run_artifacts(identity=None):
    run_dir = os.path.join(identity_root(identity), LAST_RUN_DIR)
    if not os.path.isdir(run_dir):
        return '', []
    output = ''
    out_path = os.path.join(run_dir, 'output.txt')
    if os.path.exists(out_path):
        with open(out_path, encoding='utf-8') as f:
            output = f.read()
    plots = sorted(
        os.path.join(run_dir, p) for p in os.listdir(run_dir)
        if p.startswith('plot_') and p.endswith('.png'))
    return output, plots


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

_VERSION_FILE_RE = re.compile(r'^(?P<stem>.+)__v\d+\.[^.]+$')


def _adoption_group(name):
    """The dataset stem a workspace entry belongs to, or None for entries that
    are not adopted per dataset (locks, markers, staging files, stores)."""
    if name.endswith('.lock') or name == LAST_SEEN \
            or name.startswith(INCOMING_PREFIX):
        return None
    for prefix in (MANIFEST_PREFIX, META_PREFIX):
        if name.startswith(prefix) and name.endswith('.json'):
            return name[len(prefix):-len('.json')]
    if name.startswith('.'):
        return None
    m = _VERSION_FILE_RE.match(name)
    return m.group('stem') if m else os.path.splitext(name)[0]


def adopt_workspace(from_identity, to_identity):
    """Move an anonymous workspace into an account's at sign-in (L2).

    Datasets move as whole groups (file, version files, manifest, metadata
    sidecar); a group is skipped when ANY of its names already exists in the
    account, so nothing the account holds is ever overwritten. The last run and
    the approved-script store move only when the account has none. Anything
    skipped stays behind and expires with the anonymous workspace. Returns the
    number of entries moved."""
    src = os.path.join(storage_root(), from_identity)
    if from_identity == to_identity or not os.path.isdir(src):
        return 0
    dst = identity_root(to_identity)
    groups = {}
    singles = []
    for name in os.listdir(src):
        if name in (APPROVED_SCRIPT, LAST_RUN_DIR):
            singles.append(name)
            continue
        stem = _adoption_group(name)
        if stem is not None and os.path.isfile(os.path.join(src, name)):
            groups.setdefault(stem, []).append(name)
    moved = 0
    for names in groups.values():
        if any(os.path.exists(os.path.join(dst, n)) for n in names):
            continue
        for n in names:
            os.replace(os.path.join(src, n), os.path.join(dst, n))
            moved += 1
    for name in singles:
        if not os.path.exists(os.path.join(dst, name)):
            os.replace(os.path.join(src, name), os.path.join(dst, name))
            moved += 1
    return moved


def reset_identity(identity=None):
    """Delete everything the caller has stored (4.6 Reset)."""
    root = identity_root(identity)
    shutil.rmtree(root, ignore_errors=True)


def _last_activity(identity_dir):
    """Newest mtime of the activity marker and every file in the workspace
    (the files cover dirs written before the marker existed). None when the
    workspace holds no files at all."""
    newest = None
    for dirpath, _dirnames, filenames in os.walk(identity_dir):
        for fn in filenames:
            try:
                mtime = os.stat(os.path.join(dirpath, fn)).st_mtime
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
    return newest


def cleanup_old_files(ttl_seconds=7200):
    """Expire every ANONYMOUS workspace that has been idle longer than the TTL,
    as a whole. Logged-in users' files are kept (their data is
    account-scoped; bucket lifecycle rules own that in 7.3).

    D1-3/D5-16: this used to delete files one by one by their own age. Reads
    never refresh a file's mtime, so an active session lost its original upload
    and metadata two hours after upload while the manifest and later versions
    survived (revert 404'd, undo pointed at a deleted file). Idleness is now
    the session's last activity (``.last_seen``, touched by every storage
    access, or any newer file), and an expired workspace is removed in one
    ``rmtree`` so no half-deleted history can remain."""
    now = time.time()
    root = storage_root()
    if not os.path.isdir(root):
        return
    for entry in os.listdir(root):
        identity_dir = os.path.join(root, entry)
        if not os.path.isdir(identity_dir) or not entry.startswith('anon_'):
            continue
        last = _last_activity(identity_dir)
        if last is None or now - last > ttl_seconds:
            shutil.rmtree(identity_dir, ignore_errors=True)
            logger.info("Expired idle anonymous workspace: %s", entry)
