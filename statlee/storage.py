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


def _write_json_atomic(path, obj):
    """Write JSON via a temp file + ``os.replace`` (atomic on POSIX and
    Windows) so a crash mid-write can never leave a truncated/partial file
    at ``path`` — the previous, complete version stays intact until the
    new one is fully written."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f)
    os.replace(tmp, path)


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


def identity_root(identity=None):
    identity = identity or current_identity()
    root = os.path.join(storage_root(), identity)
    os.makedirs(root, exist_ok=True)
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
    """Initialise the version manifest for a freshly uploaded dataset."""
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


def add_dataset_version(filename, new_df, instruction, summary=None, identity=None):
    """Persist a wrangled DataFrame as the next version and activate it.

    ``instruction`` is the user's own words; ``summary`` is the applied,
    past-tense description (used to render the cleaning history as a chat-style
    transcript). A new edit after an Undo truncates the redo branch.
    """
    # P2-9: serialize the whole read-modify-write so a concurrent wrangle/undo
    # cannot compute the same next_v and silently drop this version.
    with _manifest_lock(filename, identity):
        manifest = (_load_manifest(filename, identity)
                    or register_dataset(filename, identity))
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

def _hash_code(code):
    return hashlib.sha256((code or '').encode('utf-8')).hexdigest()


def _load_approved_store(identity=None):
    """Return the approved-script store as ``{sha256(code): {code, language}}``
    in insertion order (oldest first). Tolerates a missing/corrupt file and
    transparently upgrades an OLD single-slot file (``{code, language}``)."""
    path = os.path.join(identity_root(identity), APPROVED_SCRIPT)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Back-compat: an old single-slot file {code, language} -> one entry.
    if isinstance(data.get('code'), str):
        return {_hash_code(data['code']):
                {'code': data['code'], 'language': data.get('language')}}
    # New shape {sha: {code, language}}; drop any malformed entries.
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and isinstance(v.get('code'), str)}


def save_approved_script(code, language, identity=None):
    """Record a validated script keyed by its content hash (P2-2).

    Keeps the most recent ``APPROVED_SCRIPT_MAX`` scripts; re-saving a known
    script refreshes its recency. This is what lets a /wrangle transform and the
    earlier /chat script coexist, so re-running the unchanged chat code still
    matches by membership and skips re-moderation.
    """
    store = _load_approved_store(identity)
    sha = _hash_code(code)
    store.pop(sha, None)                      # re-insert -> becomes most-recent
    store[sha] = {'code': code, 'language': language}
    while len(store) > APPROVED_SCRIPT_MAX:   # evict oldest by insertion order
        del store[next(iter(store))]
    path = os.path.join(identity_root(identity), APPROVED_SCRIPT)
    _write_json_atomic(path, store)


def is_approved_script(code, identity=None):
    """True if ``code`` exactly matches a previously approved script (P2-2)."""
    return _hash_code(code) in _load_approved_store(identity)


def load_approved_script(identity=None):
    """Return the MOST-RECENTLY approved script ``{code, language}`` or None.

    Retained for /export (bundles the latest script) and for the /run guard's
    "has anything been generated yet?" check. Membership tests use
    ``is_approved_script`` instead of equality against this single entry.
    """
    store = _load_approved_store(identity)
    if not store:
        return None
    return store[next(reversed(store))]


def save_last_run(output, plots_b64, script=None, language=None, identity=None):
    """Keep the latest run's artifacts server-side so /export can bundle them.

    When ``script`` is provided, the exact script that PRODUCED this run is
    persisted alongside the output as a small ``script.json`` sidecar. /export
    and the /interpret grounded-debug path read THAT (via ``last_run_script``)
    so a later /wrangle — which saves its own transform into the shared
    approved-script store and thereby becomes "most recent" — cannot substitute
    the transform snippet for the analysis script that actually ran (P2-1/P2-7).
    ``save_last_run`` wipes and recreates the run dir on every call, so omitting
    ``script`` leaves no stale sidecar behind.
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
    for i, b64 in enumerate(plots_b64 or [], start=1):
        try:
            with open(os.path.join(run_dir, f'plot_{i}.png'), 'wb') as f:
                f.write(base64.b64decode(b64))
        except Exception:
            logger.warning("Could not persist plot %d for export", i)


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

def reset_identity(identity=None):
    """Delete everything the caller has stored (4.6 Reset)."""
    root = identity_root(identity)
    shutil.rmtree(root, ignore_errors=True)


def cleanup_old_files(ttl_seconds=7200):
    """Prune files older than the TTL inside every ANONYMOUS identity dir,
    then remove empty dirs. Logged-in users' files are kept (their data is
    account-scoped; bucket lifecycle rules own that in 7.3)."""
    now = time.time()
    root = storage_root()
    if not os.path.isdir(root):
        return
    for entry in os.listdir(root):
        identity_dir = os.path.join(root, entry)
        if not os.path.isdir(identity_dir) or not entry.startswith('anon_'):
            continue
        for dirpath, _dirnames, filenames in os.walk(identity_dir, topdown=False):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    if os.stat(fp).st_mtime < now - ttl_seconds:
                        os.remove(fp)
                        logger.info("Cleaned up old file: %s", fp)
                except OSError:
                    pass
            try:
                if dirpath != identity_dir and not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass
        try:
            if not os.listdir(identity_dir):
                os.rmdir(identity_dir)
        except OSError:
            pass
