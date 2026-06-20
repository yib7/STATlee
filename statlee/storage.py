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
import json
import logging
import os
import shutil
import time

from flask import current_app, session
from werkzeug.utils import secure_filename

logger = logging.getLogger('statlee.storage')

MANIFEST_PREFIX = '.versions__'
META_PREFIX = '.meta__'
APPROVED_SCRIPT = '.approved_script.json'
LAST_RUN_DIR = '.last_run'


# ---------------------------------------------------------------------------
# Identity & roots
# ---------------------------------------------------------------------------

def current_identity():
    """Stable storage namespace for the requesting principal.

    Logged-in users get ``user_<id>`` (durable across sessions); anonymous
    sessions get ``anon_<sid>`` (cleaned up by TTL).
    """
    try:
        from flask_login import current_user
        if current_user and getattr(current_user, 'is_authenticated', False):
            return f"user_{current_user.id}"
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
        return None


def _save_manifest(filename, manifest, identity=None):
    with open(_manifest_path(filename, identity), 'w', encoding='utf-8') as f:
        json.dump(manifest, f)


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
    manifest = _load_manifest(filename, identity) or register_dataset(filename, identity)
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
    manifest = _load_manifest(filename, identity)
    if not manifest:
        return None
    versions = sorted(v['v'] for v in manifest['versions'])
    idx = versions.index(manifest['active'])
    new_idx = idx + (1 if direction == 'redo' else -1)
    if new_idx < 0 or new_idx >= len(versions):
        return manifest  # already at the edge — no-op
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
    with open(_meta_path(filename, identity), 'w', encoding='utf-8') as f:
        json.dump(meta, f)


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

def save_approved_script(code, language, identity=None):
    path = os.path.join(identity_root(identity), APPROVED_SCRIPT)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'code': code, 'language': language}, f)


def load_approved_script(identity=None):
    path = os.path.join(identity_root(identity), APPROVED_SCRIPT)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_last_run(output, plots_b64, identity=None):
    """Keep the latest run's artifacts server-side so /export can bundle them."""
    import base64
    run_dir = os.path.join(identity_root(identity), LAST_RUN_DIR)
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, 'output.txt'), 'w', encoding='utf-8') as f:
        f.write(output or '')
    for i, b64 in enumerate(plots_b64 or [], start=1):
        try:
            with open(os.path.join(run_dir, f'plot_{i}.png'), 'wb') as f:
                f.write(base64.b64decode(b64))
        except Exception:
            logger.warning("Could not persist plot %d for export", i)


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
            if not os.listdir(identity_dir):
                os.rmdir(identity_dir)
        except OSError:
            pass
