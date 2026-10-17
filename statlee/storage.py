"""Per-identity file storage with dataset version control.

Implements:
- Per-session file isolation: every browser session (or logged-in user)
  gets its own namespaced directory; ``resolve_path`` keeps its
  realpath-containment check so no endpoint can reach another identity's
  files.
- Dataset versioning: wrangle operations write ``stem__vN.csv`` files
  tracked by a JSON manifest with an active-version pointer (undo/redo).
- The local backend namespaces by user id when logged in
  (``user_<id>/``) and by session id otherwise (``anon_<sid>/``); an S3
  backend could mirror the same key layout.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import time
import unicodedata
import uuid

from filelock import FileLock
from flask import current_app, session
from werkzeug.utils import secure_filename

from .identity import current_user_or_none

logger = logging.getLogger('statlee.storage')

MANIFEST_PREFIX = '.versions__'
META_PREFIX = '.meta__'
APPROVED_SCRIPT = '.approved_script.json'
LAST_RUN_DIR = '.last_run'

# The approved-script store keeps the last few validated scripts keyed by
# content hash (not a single slot), so a /wrangle save cannot evict the /chat
# script and force a spurious re-moderation of already-approved code.
APPROVED_SCRIPT_MAX = 5

# The version manifest is read-modify-written; a cross-process file lock
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
    return None  # pragma: no cover (the loop always returns or raises)


def _write_json_atomic(path, obj):
    """Write JSON via a temp file + ``os.replace`` (atomic on POSIX and
    Windows) so a crash mid-write can never leave a truncated/partial file
    at ``path``: the previous, complete version stays intact until the
    new one is fully written.

    The temp file is UNIQUE per write (``mkstemp`` in the same dir).
    A shared ``path + '.tmp'`` would let two overlapping writers replace each
    other's half-written temp file and put truncated JSON at ``path``."""
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
    """Record that this workspace was used. Every storage access goes
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
_RESERVED_STEM_RE = re.compile(r'__v\d+$')


def is_reserved_name(filename):
    """True when ``filename``'s stem ends in a version suffix such as
    ``__v2``. Those names belong to dataset history files (``stem__vN.csv``),
    so neither an upload nor a request naming a dataset may use one: a
    client passing ``data__v2.csv`` would otherwise address a version
    file as if it were its own dataset and grow a second history on top of
    it."""
    stem = os.path.splitext(os.path.basename(str(filename or '')))[0]
    return bool(_RESERVED_STEM_RE.search(stem))


def identity_usage(identity=None):
    """Return ``(dataset_count, total_bytes)`` for the caller's storage dir.

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
    """Map a client upload filename to ``(stored_name, ext)``.

    ``secure_filename`` drops every non-ASCII character, so on its own it turns
    ``数据.csv`` into ``csv`` (no extension, unparseable) and every Cyrillic PDF
    into ``pdf``. Here the extension is split off the RAW name first and always
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
    never clobbers the dataset it would have replaced."""
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
# Dataset version manifest
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
    across workers AND threads, closing the lost-update window where two
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
    count toward the byte quota. Each registration gets a new
    ``upload_id``, so a wrangle that read the previous upload can tell."""
    with _manifest_lock(filename, identity):
        return _register_unlocked(filename, identity)


def _register_unlocked(filename, identity=None):
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
    rev = 0
    if isinstance(previous, dict) and isinstance(previous.get('rev'), int):
        rev = previous['rev']
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
        # The version token. A fresh id per upload plus a counter bumped
        # by every change, so neither a same-name re-upload nor an undo
        # followed by a new edit (same v number, same file name, different
        # bytes) can look like the version a running wrangle read.
        'upload_id': uuid.uuid4().hex,
        'rev': rev + 1,
    }
    _save_manifest(filename, manifest, identity)
    return manifest


def active_dataset_snapshot(filename, identity=None):
    """``(path, version)`` of the ACTIVE version from ONE manifest read, so
    the two always describe the same state (a /run records them before the
    sandbox starts; a wrangle landing mid-run must not pair the new version
    number with the old bytes). ``version`` is None for a dataset without a
    manifest; ``path`` falls back to the plain file."""
    manifest = _load_manifest(filename, identity)
    if manifest:
        active = next((v for v in manifest['versions']
                       if v['v'] == manifest['active']), None)
        if active:
            path = resolve_path(active['file'], identity)
            if path and os.path.exists(path):
                number = manifest.get('active')
                if isinstance(number, bool) or not isinstance(number, int):
                    number = None
                return path, number
    return resolve_path(filename, identity), None


def active_dataset_path(filename, identity=None):
    """Resolve the ACTIVE version of a dataset (falls back to the plain file)."""
    return active_dataset_snapshot(filename, identity)[0]


def _truncate_redo_branch(manifest):
    """Drop any versions after the active pointer: a new edit (or revert)
    after an Undo replaces the redo branch, like an editor's undo stack."""
    manifest['versions'] = [v for v in manifest['versions']
                            if v['v'] <= manifest['active']]


class VersionConflict(Exception):
    """The active version moved while a transform derived from it was
    running: committing it would silently discard the other edit."""


def _active_entry(manifest):
    return next((v for v in manifest['versions']
                 if v['v'] == manifest['active']), None)


def _version_token(manifest):
    return f"{manifest.get('upload_id') or ''}:{int(manifest.get('rev') or 0)}"


def _bump(manifest):
    manifest['rev'] = int(manifest.get('rev') or 0) + 1


def dataset_version_token(filename, identity=None):
    """An opaque token naming the dataset's current state, or None for an
    unknown dataset. It changes on every upload, wrangle, undo, redo and
    revert; pass it back as ``base_token`` to ``add_dataset_version`` to
    commit only on top of the state that was read."""
    manifest = _load_manifest(filename, identity)
    return _version_token(manifest) if manifest else None


def active_version_number(filename, identity=None):
    """The active version number (1 = the original upload), or None for an
    unknown dataset. Recorded with each run (provenance)."""
    manifest = _load_manifest(filename, identity)
    active = manifest.get('active') if isinstance(manifest, dict) else None
    if isinstance(active, bool) or not isinstance(active, int):
        return None
    return active


def dataset_version_path(filename, version, identity=None):
    """Path of version ``version`` of a dataset while its file still exists,
    else None (an unknown version, or one dropped by a re-upload)."""
    manifest = _load_manifest(filename, identity)
    if not isinstance(manifest, dict):
        return None
    entry = next((v for v in manifest.get('versions', [])
                  if isinstance(v, dict) and v.get('v') == version), None)
    path = resolve_path(entry.get('file'), identity) if entry else None
    return path if path and os.path.exists(path) else None


def add_dataset_version(filename, new_df, instruction, summary=None,
                        identity=None, code=None, base_file=None,
                        base_token=None, role=None, model=None):
    """Persist a wrangled DataFrame as the next version and activate it.

    ``instruction`` is the user's own words; ``summary`` is the applied,
    past-tense description (used to render the cleaning history as a chat-style
    transcript). A new edit after an Undo truncates the redo branch.

    ``code`` is the transform that produced the frame, kept on the version
    entry so an export can replay the cleaning from the original upload.
    ``base_file`` is the version file the transform read; when another
    wrangle/undo/revert has moved the active version since, the commit is
    refused with ``VersionConflict`` instead of stacking a frame derived
    from the old version on top of the new one.
    ``base_token`` (from ``dataset_version_token``, read BEFORE the data) is
    the stronger check: file names repeat after a same-name re-upload or
    an undo plus a new edit, the token does not.
    ``role``/``model`` name the LLM role and configured model that wrote the
    transform (provenance); recorded only when given.
    """
    # Serialize the whole read-modify-write so a concurrent wrangle/undo
    # cannot compute the same next_v and silently drop this version.
    with _manifest_lock(filename, identity):
        manifest = (_load_manifest(filename, identity)
                    or _register_unlocked(filename, identity))
        if base_token is not None and _version_token(manifest) != base_token:
            raise VersionConflict(filename)
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
        if role:
            manifest['versions'][-1]['role'] = str(role)[:64]
        if model:
            manifest['versions'][-1]['model'] = str(model)[:64]
        manifest['active'] = next_v
        _bump(manifest)
        _save_manifest(filename, manifest, identity)
        return manifest


def revert_to_original(filename, identity=None):
    """Restore the dataset to its first-uploaded state as a NEW version.

    Implemented as a forward edit (copy v1's bytes into a fresh version) rather
    than a pointer jump, so reverting is itself undo-able: one Undo brings the
    pre-revert state back. Returns the manifest, or ``None`` for an unknown
    dataset or a missing original file.
    """
    # Same manifest read-modify-write, same lock.
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
        _bump(manifest)
        _save_manifest(filename, manifest, identity)
        return manifest


def shift_dataset_version(filename, direction, identity=None):
    """Move the active-version pointer (undo: -1, redo: +1)."""
    # The pointer move is a manifest read-modify-write too; lock it so a
    # concurrent add/undo cannot overwrite the other's result.
    with _manifest_lock(filename, identity):
        manifest = _load_manifest(filename, identity)
        if not manifest:
            return None
        versions = sorted(v['v'] for v in manifest['versions'])
        idx = versions.index(manifest['active'])
        new_idx = idx + (1 if direction == 'redo' else -1)
        if new_idx < 0 or new_idx >= len(versions):
            return manifest  # already at the edge: no-op
        manifest['active'] = versions[new_idx]
        _bump(manifest)
        _save_manifest(filename, manifest, identity)
        return manifest


def dataset_lineage(filename, identity=None):
    """What an export needs to reproduce the active version.

    Returns None for an unknown dataset, else a dict with ``original`` (path
    of the v1 file), ``active_v``/``latest_v``, and ``steps``: the transform
    entries that turn the original into the active version, in order. A revert
    restarts the lineage from the original; versions past the active pointer
    (an undone redo branch) are not part of it. ``replayable`` is False when a
    step predates code recording and so cannot be replayed.
    ``versions`` is every manifest entry in version order (provenance
    reads each step's base version from it)."""
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
        'versions': versions,
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
    data = _read_json(path)
    return data if isinstance(data, dict) else {}


class MetaUnreadable(OSError):
    """A meta sidecar exists but is not a readable JSON object."""


def load_dataset_meta_checked(filename, identity=None):
    """``load_dataset_meta`` for a read-modify-write: ``{}`` only when there
    is no sidecar; an existing one that does not parse raises
    ``MetaUnreadable`` instead of reading as empty (writing that back would
    drop the sha256, original name and labels)."""
    path = _meta_path(filename, identity)
    if not os.path.exists(path):
        return {}
    data = _read_json(path)
    if not isinstance(data, dict):
        raise MetaUnreadable(path)
    return data


def dataset_meta_lock(filename, identity=None):
    """Cross-process lock for a read-modify-write of one dataset's meta
    sidecar (the codebook lives there and is edited concurrently by
    PUT /codebook, classification and PDF extraction)."""
    return FileLock(_meta_path(filename, identity) + '.lock',
                    timeout=_MANIFEST_LOCK_TIMEOUT)


# Codebooks keyed by a dataset's original sha256, so re-uploading
# identical content restores the user's edits even after the meta sidecar of
# that name was replaced by another upload. Hidden files, so they never count
# as datasets; the oldest are pruned past CODEBOOK_ARCHIVE_MAX.
CODEBOOK_ARCHIVE_PREFIX = '.codebook__'
CODEBOOK_ARCHIVE_MAX = 50
_SHA256_RE = re.compile(r'[0-9a-f]{64}')


def _codebook_archive_path(sha256, identity=None):
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        return None
    return os.path.join(identity_root(identity),
                        f'{CODEBOOK_ARCHIVE_PREFIX}{sha256}.json')


def save_codebook_archive(sha256, payload, identity=None):
    path = _codebook_archive_path(sha256, identity)
    if path is None:
        return
    _write_json_atomic(path, payload)
    root = os.path.dirname(path)
    try:
        others = [os.path.join(root, n) for n in os.listdir(root)
                  if n.startswith(CODEBOOK_ARCHIVE_PREFIX)
                  and n.endswith('.json')
                  and os.path.join(root, n) != path]
        stamps = {p: os.stat(p).st_mtime_ns for p in others}
        # Recency is the file mtime. A coarse filesystem clock can give
        # archives written in quick succession the same stamp, so the one
        # just written is stamped strictly newer than every other archive;
        # otherwise the prune below could delete it. The 1 ms step survives
        # filesystems that round stamps (NTFS keeps 100 ns).
        newest = max([time.time_ns()]
                     + [t + 1_000_000 for t in stamps.values()])
        os.utime(path, ns=(newest, newest))
        others.sort(key=stamps.__getitem__, reverse=True)
        for old in others[CODEBOOK_ARCHIVE_MAX - 1:]:
            os.remove(old)
    except OSError:
        logger.warning("Could not prune the codebook archive", exc_info=True)


def load_codebook_archive(sha256, identity=None):
    path = _codebook_archive_path(sha256, identity)
    return _read_json(path) if path else None


# ---------------------------------------------------------------------------
# Approved-script store (run-guard) and last-run artifacts (export)
# ---------------------------------------------------------------------------

def _lang_key(language):
    return str(language or 'Python').strip().lower()


def _hash_code(code, language='Python'):
    """Approval key: the script AND the interpreter it was reviewed for.
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
    """Record a validated script keyed by (language, content hash).

    Keeps the most recent ``APPROVED_SCRIPT_MAX`` scripts; re-saving a known
    script refreshes its recency. This is what lets a /wrangle transform and the
    earlier /chat script coexist, so re-running the unchanged chat code still
    matches by membership and skips re-moderation. ``source`` ('chat', 'run' or
    'wrangle') lets /export skip wrangle transforms.

    The read-modify-write runs under a file lock, so two concurrent saves
    cannot drop each other's entry.

    ``context``: what the script was generated for, as a small JSON-able
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
    ``language``."""
    return _hash_code(code, language) in _load_approved_store(identity)


def approved_entry(code, language='Python', identity=None):
    """The store entry ``{code, language, source, ts}`` approving ``code`` for
    ``language``, or None."""
    return _load_approved_store(identity).get(_hash_code(code, language))


def approved_language(code, language='Python', identity=None):
    """The language to run ``code`` under: ``language`` when the
    script is approved for it, otherwise the language it WAS approved for
    (the user switched the dropdown after generating), otherwise ``language``
    (unknown code, which the run-guard then reviews for that language)."""
    store = _load_approved_store(identity)
    if _hash_code(code, language) in store:
        return language
    for other in ('Python', 'R'):
        if _hash_code(code, other) in store:
            return other
    return language


def load_approved_script(identity=None, exclude_sources=(), match=None):
    """Return the MOST-RECENTLY approved script ``{code, language, ...}`` or
    None, skipping entries whose ``source`` is in ``exclude_sources`` and,
    with ``match``, entries for which ``match(entry)`` is false (/export only
    takes a script generated for the exported dataset).

    Membership tests use ``is_approved_script`` instead of equality against
    this single entry.
    """
    store = _load_approved_store(identity)
    for entry in reversed(list(store.values())):
        if entry.get('source') in exclude_sources:
            continue
        if match is None or match(entry):
            return entry
    return None


# Session workbook: the run store lives in ``runs.py`` and is
# re-exported at the bottom of this module.


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
    """Move an anonymous workspace into an account's at sign-in.

    Datasets move as whole groups (file, version files, manifest, metadata
    sidecar); a group is skipped when ANY of its names already exists in the
    account, so nothing the account holds is ever overwritten. The last run,
    the approved-script store and each codebook archive (``.codebook__<sha>``)
    move only when the account has none. Anything
    skipped stays behind and expires with the anonymous workspace.

    Returns ``{'moved': [...], 'skipped': [...]}``: the stored names of the
    datasets that moved and of those left behind because the account already
    had that name, so the client can say so instead of a generic
    warning."""
    report = {'moved': [], 'skipped': []}
    src = os.path.join(storage_root(), from_identity)
    if from_identity == to_identity or not os.path.isdir(src):
        return report
    dst = identity_root(to_identity)
    groups = {}
    singles = []
    for name in os.listdir(src):
        if name in (APPROVED_SCRIPT, LAST_RUN_DIR, RUNS_DIR):
            singles.append(name)
            continue
        if name.startswith(CODEBOOK_ARCHIVE_PREFIX) and name.endswith('.json'):
            # Codebook archives are keyed by content hash: each moves on its
            # own unless the account already holds one for that content.
            singles.append(name)
            continue
        stem = _adoption_group(name)
        if stem is not None and os.path.isfile(os.path.join(src, name)):
            groups.setdefault(stem, []).append(name)
    for _stem, names in sorted(groups.items()):
        # The dataset's own file (not a sidecar or a version file) names it.
        datasets = [n for n in names if not n.startswith('.')
                    and not _VERSION_FILE_RE.match(n)]
        if any(os.path.exists(os.path.join(dst, n)) for n in names):
            report['skipped'].extend(datasets)
            continue
        for n in names:
            os.replace(os.path.join(src, n), os.path.join(dst, n))
        report['moved'].extend(datasets)
    for name in singles:
        target = os.path.join(dst, name)
        if name == RUNS_DIR and os.path.isdir(target) and not run_ids(to_identity):
            # Reading the workbook creates an empty runs/ dir; an account with
            # no runs takes the anonymous workbook whole.
            shutil.rmtree(target, ignore_errors=True)
        if not os.path.exists(target):
            os.replace(os.path.join(src, name), target)
    return report


def reset_identity(identity=None):
    """Delete everything the caller has stored (Reset)."""
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
    account-scoped).

    Idleness is the session's last activity (``.last_seen``, touched by every
    storage access, or any newer file), not each file's own age: reads never
    refresh a file's mtime, so per-file expiry would delete an active
    session's original upload two hours in while the manifest and later
    versions survived (revert would 404, undo would point at a deleted file).
    An expired workspace is removed in one ``rmtree`` so no half-deleted
    history can remain."""
    now = time.time()
    root = storage_root()
    if not os.path.isdir(root):
        return
    for entry in os.listdir(root):
        identity_dir = os.path.join(root, entry)
        if not os.path.isdir(identity_dir) or not entry.startswith('anon_'):
            continue
        # A fresh marker settles it with one stat; only a workspace whose
        # marker is missing or old is walked file by file.
        try:
            seen = os.stat(os.path.join(identity_dir, LAST_SEEN)).st_mtime
        except OSError:
            seen = None
        if seen is not None and now - seen <= ttl_seconds:
            continue
        last = _last_activity(identity_dir)
        if last is None or now - last > ttl_seconds:
            shutil.rmtree(identity_dir, ignore_errors=True)
            logger.info("Expired idle anonymous workspace: %s", entry)


# Session workbook re-exports. Imported last: runs.py imports this
# module lazily, so the import order is safe either way.
from .runs import (  # noqa: E402, F401 (re-exported API)
    RUN_NAME_MAX,
    RUNS_DIR,
    RUNS_MAX,
    interpretation_time,
    latest_meta_for_script,
    latest_run_id,
    list_runs,
    load_run,
    record_run,
    run_artifacts,
    run_ids,
    run_interpretation,
    run_meta,
    run_script,
    run_status,
    run_summary,
    run_time,
    save_interpretation,
    save_run,
    update_run_meta,
    valid_run_id,
)
