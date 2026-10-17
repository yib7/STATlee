"""Survey-native codebook (SP2): schema, validation, clamping and merge.

One codebook per dataset, stored in the dataset's meta sidecar under
``codebook`` as one layer per source::

    {"layers": {"user":   {column: {field: value}},
                "native": {...}, "pdf": {...}, "llm": {...}}}

Fields are ``type``, ``label``, ``value_labels``, ``missing_codes``,
``role`` and ``scale_group``. The merged view a client or a prompt sees takes
each field from the highest-precedence layer that has it: user > native
(.sav/.dta labels, value labels, user-missing values) > pdf (codebook or
survey extraction) > llm (classification). A seed only ever rewrites its own
layer, so a field the user edited is never overwritten by a later seed or a
re-classify. The merged entry::

    {"type": continuous|ordinal|nominal|binary|text|id|date|null,
     "label": str, "value_labels": {code: text}, "missing_codes": [code],
     "role": analyze|id|weight|ignore, "scale_group": str|null,
     "source": user|native|pdf|llm|default, "user_fields": [field],
     "field_sources": {field: source}}

``source`` is the highest layer contributing any field (``default`` when no
layer has the column yet); ``user_fields`` lists the user-set fields.

The stored layers keep every column ever described; the merged view is
always computed for a column list (the active version's columns by default),
so wrangle versions inherit the codebook, a column added by a transform gets
a default entry, a removed one is dropped from the view (and comes back on
undo).

Re-uploading identical content restores the codebook: every write mirrors the
non-native layers to an archive keyed by the dataset's original sha256
(``storage.save_codebook_archive``), and an upload restores from it.

All text here is user or model supplied: it is clamped on the way in and the
prompt helpers return data meant to sit inside ``prompts.untrusted`` blocks.
"""
import json
import logging
import math
import os
import re
from datetime import UTC, datetime

from .prompts import neutralize_markers

logger = logging.getLogger('statlee.codebook')

TYPES = ('continuous', 'ordinal', 'nominal', 'binary', 'text', 'id', 'date')
ROLES = ('analyze', 'id', 'weight', 'ignore')
SOURCES = ('user', 'native', 'pdf', 'llm')     # precedence, highest first
FIELDS = ('type', 'label', 'value_labels', 'missing_codes', 'role',
          'scale_group')
READ_ONLY_KEYS = ('source', 'user_fields', 'field_sources')
DEFAULT_SOURCE = 'default'

LABEL_MAX = 2000            # a PDF description can carry its coding scheme
CODE_MAX = 50
VALUE_TEXT_MAX = 200
VALUE_LABELS_MAX = 200
MISSING_CODES_MAX = 50
SCALE_GROUP_MAX = 80
IMPORT_ENTRIES_MAX = 10000
# Total size of one dataset's user, pdf and llm layers (as JSON). They are
# kept twice (meta sidecar + sha256 archive) and re-read on every /chat, so a
# write that would grow them past this is refused (413) instead of stored.
# The native layer does not count: it comes from the uploaded file, is
# bounded by the per-field clamps, and the user could not shrink it to make
# room for an edit (3A A1).
CODEBOOK_BYTES_MAX = 4 * 1024 * 1024
PROMPT_VALUE_LABELS_MAX = 40
SUMMARY_LINE_MAX = 200

EXPORT_FORMAT = 'statlee-codebook'
EXPORT_VERSION = 1

_TITLES = {'continuous': 'Continuous', 'ordinal': 'Ordinal',
           'nominal': 'Nominal', 'binary': 'Binary', 'text': 'Text',
           'id': 'ID', 'date': 'Date'}
_EXCLUDED_ROLES = ('id', 'ignore')


class CodebookError(ValueError):
    """An invalid codebook payload. The message is client-safe (a 400)."""


class CodebookTooLarge(CodebookError):
    """The codebook would grow past CODEBOOK_BYTES_MAX. Client-safe (a 413)."""


class CodebookStorageError(RuntimeError):
    """The dataset's meta sidecar exists but could not be read, so a codebook
    write is refused rather than replacing it with a sidecar that has lost
    its sha256, original name and labels."""


# ---------------------------------------------------------------------------
# Field validation and clamping
# ---------------------------------------------------------------------------

def _code(value):
    """A value-label or missing-value code as a string (``1.0`` -> ``'1'``)."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CodebookError('Codes must be text or numbers.')
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodebookError('Codes must be finite numbers.')
        if value.is_integer():
            value = int(value)
    return str(value).strip()[:CODE_MAX]


def _text(value, limit, name):
    if not isinstance(value, str):
        raise CodebookError(f"'{name}' must be text.")
    return value.strip()[:limit]


def _enum(value, allowed, name):
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        raise CodebookError(f"'{name}' must be one of: {', '.join(allowed)}.")
    return value.strip().lower()


def _value_labels(value):
    if not isinstance(value, dict):
        raise CodebookError("'value_labels' must be an object of code: text.")
    out = {}
    for key, text in value.items():
        if isinstance(text, bool) or not isinstance(text, (str, int, float)):
            raise CodebookError("Each value label must be text.")
        code = _code(key) if not isinstance(key, str) else key.strip()[:CODE_MAX]
        if not code:
            continue
        out[code] = str(text).strip()[:VALUE_TEXT_MAX]
        if len(out) >= VALUE_LABELS_MAX:
            break
    return out


def _missing_codes(value):
    if not isinstance(value, list):
        raise CodebookError("'missing_codes' must be a list.")
    out = []
    for item in value:
        code = _code(item)
        if code and code not in out:
            out.append(code)
            if len(out) >= MISSING_CODES_MAX:
                break
    return out


def _scale_group(value):
    if value is None:
        return None
    group = _text(value, SCALE_GROUP_MAX, 'scale_group')
    return group or None


_VALIDATORS = {
    'type': lambda v: _enum(v, TYPES, 'type'),
    'label': lambda v: _text(v, LABEL_MAX, 'label'),
    'value_labels': _value_labels,
    'missing_codes': _missing_codes,
    'role': lambda v: _enum(v, ROLES, 'role'),
    'scale_group': _scale_group,
}


def clean_fields(fields, *, lenient=False):
    """Validate and clamp one column's fields.

    Strict (``PUT /codebook``): an unknown key or a null other than
    ``scale_group`` is a ``CodebookError``. Lenient (import, stored layers):
    unknown keys and nulls are skipped. Read-only keys of a merged entry
    (``source``, ``user_fields``, ``field_sources``) are always ignored, so a
    client may send an entry back as it received it."""
    if not isinstance(fields, dict):
        raise CodebookError('Each codebook entry must be an object.')
    out = {}
    for key, value in fields.items():
        if key in READ_ONLY_KEYS:
            continue
        if key not in _VALIDATORS:
            if lenient:
                continue
            raise CodebookError(f"Unknown codebook field '{str(key)[:40]}'.")
        if value is None and key != 'scale_group':
            if lenient:
                continue
            raise CodebookError(f"'{key}' cannot be null.")
        if value is None and lenient:
            continue
        out[key] = _VALIDATORS[key](value)
    return out


def _clean_stored(fields):
    """Stored layer content, defensively re-validated per field (a corrupt
    or hand-edited sidecar loses the bad field, never the whole codebook)."""
    if not isinstance(fields, dict):
        return None
    out = {}
    for key, value in fields.items():
        if key not in _VALIDATORS or value is None and key != 'scale_group':
            continue
        try:
            out[key] = _VALIDATORS[key](value)
        except CodebookError:
            continue
    return out


# ---------------------------------------------------------------------------
# Layers and merge (pure)
# ---------------------------------------------------------------------------

def empty_layers():
    return {source: {} for source in SOURCES}


def default_entry():
    return {'type': None, 'label': '', 'value_labels': {}, 'missing_codes': [],
            'role': 'analyze', 'scale_group': None, 'source': DEFAULT_SOURCE,
            'user_fields': [], 'field_sources': {}}


def has_entries(layers):
    """True when any layer describes any column."""
    return any(any(cols.values()) for cols in (layers or {}).values()
               if isinstance(cols, dict))


def apply_to_layers(layers, source, updates):
    """Merge ``{column: fields}`` into ``source``'s layer (fields already
    cleaned). Only that layer changes, so precedence is never bypassed."""
    if source not in SOURCES:
        raise ValueError(f'unknown codebook source {source!r}')
    layer = layers.setdefault(source, {})
    for column, fields in updates.items():
        if fields:
            layer.setdefault(str(column), {}).update(fields)
    return layers


def merge(layers, columns):
    """The merged entry of every column in ``columns``, in that order."""
    out = {}
    for column in map(str, columns):
        entry = default_entry()
        contributing = []
        for field in FIELDS:
            for source in SOURCES:
                fields = (layers.get(source) or {}).get(column)
                if isinstance(fields, dict) and field in fields:
                    entry[field] = fields[field]
                    entry['field_sources'][field] = source
                    if source not in contributing:
                        contributing.append(source)
                    break
        if contributing:
            entry['source'] = min(contributing, key=SOURCES.index)
        entry['user_fields'] = [f for f in FIELDS
                                if entry['field_sources'].get(f) == 'user']
        out[column] = entry
    return out


def layers_from_meta(meta):
    """The stored layers of a meta sidecar, cleaned. A sidecar written before
    the server codebook existed only has ``labels`` (native .sav/.dta labels);
    they become the native layer."""
    meta = meta if isinstance(meta, dict) else {}
    stored = meta.get('codebook')
    layers = empty_layers()
    if isinstance(stored, dict) and isinstance(stored.get('layers'), dict):
        for source in SOURCES:
            cols = stored['layers'].get(source)
            if not isinstance(cols, dict):
                continue
            for column, fields in cols.items():
                cleaned = _clean_stored(fields)
                if cleaned:
                    layers[source][str(column)] = cleaned
        return layers
    labels = meta.get('labels')
    if isinstance(labels, dict):
        for column, label in labels.items():
            if isinstance(label, str) and label.strip():
                layers['native'][str(column)] = {
                    'label': label.strip()[:LABEL_MAX]}
    return layers


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

def parse_update(codebook, columns):
    """``PUT /codebook``'s ``codebook``: ``{column: {field: value}}`` for
    columns of the dataset, validated strictly."""
    if not isinstance(codebook, dict) or not codebook:
        raise CodebookError("'codebook' must be a non-empty object of "
                            "column: fields.")
    known = set(map(str, columns))
    updates = {}
    for column, fields in codebook.items():
        if column not in known:
            raise CodebookError(
                f"Unknown column '{str(column)[:80]}' for this dataset.")
        updates[column] = clean_fields(fields)
    return updates


def parse_import(doc, columns):
    """An imported codebook: an export document (``{"format": ...,
    "codebook": {...}}``) or a bare ``{column: entry}`` map. Columns this
    dataset does not have are ignored and returned; fields are validated
    leniently (nulls and unknown keys skipped, a bad enum is still an
    error). Returns ``(updates, ignored_columns)``."""
    if not isinstance(doc, dict):
        raise CodebookError('The codebook file must be a JSON object.')
    entries = doc.get('codebook') if isinstance(doc.get('codebook'), dict) \
        else doc
    if 'codebook' in doc and not isinstance(doc.get('codebook'), dict):
        raise CodebookError("'codebook' must be an object.")
    if not entries:
        raise CodebookError('The codebook file has no entries.')
    if len(entries) > IMPORT_ENTRIES_MAX:
        raise CodebookError(
            f'The codebook file has more than {IMPORT_ENTRIES_MAX} entries.')
    known = set(map(str, columns))
    updates, ignored = {}, []
    for column, fields in entries.items():
        if column not in known:
            ignored.append(str(column)[:200])
            continue
        try:
            cleaned = clean_fields(fields, lenient=True)
        except CodebookError as exc:
            raise CodebookError(f"Column '{str(column)[:80]}': {exc}") from exc
        cleaned = _imported_fields(fields, cleaned)
        if cleaned:
            updates[column] = cleaned
    return updates, ignored


_DEFAULT_VALUES = {'type': None, 'label': '', 'value_labels': {},
                   'missing_codes': [], 'role': 'analyze', 'scale_group': None}


def _imported_fields(raw, cleaned):
    """The fields of one imported entry that become user fields.

    An exported entry (it lists ``user_fields``) carries every field, most of
    them defaults or values from the source dataset's own seeds; only the
    fields the user edited there are imported, so an empty default never
    outranks the target's native labels or missing codes. A hand-written
    entry without ``user_fields`` imports every field that differs from the
    default; an entry marked ``source: "default"`` imports nothing."""
    if raw.get('source') == DEFAULT_SOURCE:
        return {}
    listed = raw.get('user_fields')
    if isinstance(listed, list):
        return {k: v for k, v in cleaned.items() if k in listed}
    return {k: v for k, v in cleaned.items() if v != _DEFAULT_VALUES[k]}


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def _is_default(entry):
    return entry.get('source') == DEFAULT_SOURCE


_WS_RE = re.compile(r'\s+')


def prompt_text(value):
    """Codebook text as it may appear in a prompt: one line (whitespace runs,
    newlines included, collapse to a single space, so a label cannot forge a
    ``- column:`` line) and no ``<<<``/``>>>`` runs (so it cannot close or
    open an untrusted-data block). Storage keeps the original text."""
    text = _WS_RE.sub(' ', str(value)).strip()
    return neutralize_markers(text)


def _prompt_entry(entry):
    """A merged entry with every text field passed through ``prompt_text``."""
    out = dict(entry)
    out['label'] = prompt_text(entry.get('label') or '')
    out['value_labels'] = {prompt_text(k): prompt_text(v) for k, v in
                           (entry.get('value_labels') or {}).items()}
    out['missing_codes'] = [prompt_text(c)
                            for c in entry.get('missing_codes') or []]
    group = entry.get('scale_group')
    out['scale_group'] = prompt_text(group) if group else None
    return out


def _capped_labels(value_labels):
    items = list((value_labels or {}).items())
    out = dict(items[:PROMPT_VALUE_LABELS_MAX])
    if len(items) > PROMPT_VALUE_LABELS_MAX:
        out['...'] = f'{len(items) - PROMPT_VALUE_LABELS_MAX} more'
    return out


def prompt_dict(entries):
    """``{column: compact entry}`` for the draft prompt: only fields that say
    something (no defaults), value labels capped. Default entries are left
    out entirely."""
    out = {}
    for column, entry in entries.items():
        if _is_default(entry):
            continue
        entry = _prompt_entry(entry)
        compact = {}
        if entry.get('type'):
            compact['type'] = entry['type']
        if entry.get('label'):
            compact['label'] = entry['label']
        if entry.get('value_labels'):
            compact['value_labels'] = _capped_labels(entry['value_labels'])
        if entry.get('missing_codes'):
            compact['missing_codes'] = list(entry['missing_codes'])
        if entry.get('role') and entry['role'] != 'analyze':
            compact['role'] = entry['role']
        if entry.get('scale_group'):
            compact['scale_group'] = entry['scale_group']
        if compact:
            out[column] = compact
    return out


def role_summary(compact):
    """Weight column(s), columns excluded from modelling, and scale groups,
    from a ``prompt_dict``."""
    weight, exclude, groups = [], [], {}
    for column, entry in compact.items():
        if not isinstance(entry, dict):
            continue
        column = prompt_text(column)
        role = entry.get('role')
        if role == 'weight':
            weight.append(column)
        elif role in _EXCLUDED_ROLES:
            exclude.append(column)
        group = entry.get('scale_group')
        if group:
            groups.setdefault(prompt_text(group), []).append(column)
    return {'weight': weight, 'exclude': exclude, 'scale_groups': groups}


def describe(entry):
    """One line of codebook text for a column (label, value labels, missing
    codes, role, scale group). Empty for a default entry."""
    if _is_default(entry):
        return ''
    entry = _prompt_entry(entry)
    parts = []
    if entry.get('label'):
        parts.append(entry['label'])
    if entry.get('value_labels'):
        parts.append('values: ' + ', '.join(
            f'{code}={text}'
            for code, text in _capped_labels(entry['value_labels']).items()))
    if entry.get('missing_codes'):
        parts.append('missing codes (treat as missing): '
                     + ', '.join(entry['missing_codes']))
    role = entry.get('role')
    if role in _EXCLUDED_ROLES:
        parts.append(f'role: {role} (exclude from modelling)')
    elif role == 'weight':
        parts.append('role: survey weight')
    if entry.get('scale_group'):
        parts.append(f"scale group: {entry['scale_group']}")
    return '; '.join(parts)


def summary_line(entry):
    """A short ``Type; description`` line (feature selection's column list)."""
    if _is_default(entry):
        return ''
    head = _TITLES.get(entry.get('type'), '')
    text = '; '.join(p for p in (head, describe(entry)) if p)
    return text[:SUMMARY_LINE_MAX]


def prompt_levels_and_descriptions(entries):
    """``(levels, descriptions)`` for ``datatools.build_column_context``:
    ``{column: 'Nominal'}`` and ``{column: describe(entry)}``, skipping empty
    ones."""
    levels, descriptions = {}, {}
    for column, entry in entries.items():
        title = _TITLES.get(entry.get('type'))
        if title:
            levels[column] = title
        text = describe(entry)
        if text:
            descriptions[column] = text
    return levels, descriptions


# ---------------------------------------------------------------------------
# Storage-backed API (per dataset, under the caller's identity root)
# ---------------------------------------------------------------------------

def _st():
    from . import storage
    return storage


def dataset_columns(filename, version=None, identity=None):
    """Column names of the dataset's active version (or ``version``); None
    when the dataset is unknown or its file is gone."""
    import pandas as pd
    st = _st()
    if st.active_version_number(filename, identity) is None:
        return None
    if version is None:
        path = st.active_dataset_path(filename, identity)
    else:
        path = st.dataset_version_path(filename, version, identity)
    if not path or not os.path.isfile(path):
        return None
    try:
        return [str(c) for c in pd.read_csv(path, nrows=0).columns]
    except Exception:
        logger.warning("Could not read the dataset header for the codebook")
        return None


def load_layers(filename, identity=None):
    return layers_from_meta(_st().load_dataset_meta(filename, identity))


def has_codebook(filename, identity=None):
    """True when the dataset has any server codebook content."""
    return has_entries(load_layers(filename, identity))


def merged(filename, columns=None, identity=None):
    """The merged codebook of a dataset for ``columns`` (default: the active
    version's). None for an unknown dataset."""
    if columns is None:
        columns = dataset_columns(filename, identity=identity)
        if columns is None:
            return None
    return merge(load_layers(filename, identity), columns)


def _archive(meta, layers, identity):
    sha = meta.get('sha256') if isinstance(meta, dict) else None
    if not isinstance(sha, str):
        return
    keep = {s: layers.get(s, {}) for s in SOURCES if s != 'native'}
    if has_entries(keep):
        _st().save_codebook_archive(sha, {'layers': keep}, identity)


def _capped_size(layers):
    """Bytes of the layers ``CODEBOOK_BYTES_MAX`` applies to (not native)."""
    editable = {s: layers.get(s, {}) for s in SOURCES if s != 'native'}
    return len(json.dumps(editable, ensure_ascii=False).encode('utf-8'))


def apply(filename, source, updates, identity=None):
    """Merge cleaned ``{column: fields}`` into ``source``'s layer of the
    dataset's codebook, under the meta sidecar lock (atomic write), and mirror
    it to the sha256 archive. Returns the stored layers. Raises
    ``CodebookTooLarge`` (nothing written) when the write would grow it past
    ``CODEBOOK_BYTES_MAX``."""
    if source not in SOURCES:
        raise ValueError(f'unknown codebook source {source!r}')
    st = _st()
    with st.dataset_meta_lock(filename, identity):
        try:
            meta = st.load_dataset_meta_checked(filename, identity)
        except st.MetaUnreadable as exc:
            logger.warning("Refusing a codebook write over an unreadable "
                           "meta sidecar")
            raise CodebookStorageError(str(filename)) from exc
        layers = layers_from_meta(meta)
        if updates:
            before = _capped_size(layers)
            apply_to_layers(layers, source, updates)
            size = _capped_size(layers)
            # Only growth past the cap is refused: a codebook stored before
            # the cap existed can still be edited down.
            if size > CODEBOOK_BYTES_MAX and size > before:
                raise CodebookTooLarge(
                    'The codebook is too large to save: it would hold '
                    f'{size // 1024} KB, over the {CODEBOOK_BYTES_MAX // 1024} '
                    'KB limit. Shorten labels or value labels and try again.')
            meta['codebook'] = {'layers': layers}
            st.save_dataset_meta(filename, meta, identity)
            _archive(meta, layers, identity)
    return layers


def init_for_upload(filename, meta, native=None, identity=None):
    """Write a fresh upload's meta sidecar with its codebook: the user, pdf
    and llm layers come back when identical content (same sha256) was
    described before -- the previous sidecar of this name, else the archive
    -- and the native layer is the upload's own .sav/.dta metadata. Returns
    True when an earlier codebook was restored."""
    st = _st()
    sha = meta.get('sha256')
    with st.dataset_meta_lock(filename, identity):
        previous = st.load_dataset_meta(filename, identity) or {}
        restored = empty_layers()
        if isinstance(sha, str) and previous.get('sha256') == sha \
                and isinstance(previous.get('codebook'), dict):
            restored = layers_from_meta(previous)
        if not has_entries({s: restored[s] for s in SOURCES if s != 'native'}) \
                and isinstance(sha, str):
            archived = st.load_codebook_archive(sha, identity)
            if isinstance(archived, dict):
                restored = layers_from_meta({'codebook': archived})
        layers = empty_layers()
        for source in SOURCES:
            if source != 'native':
                layers[source] = restored.get(source, {})
        native_updates = {}
        for column, fields in (native or {}).items():
            cleaned = clean_fields(fields, lenient=True)
            if cleaned:
                native_updates[str(column)] = cleaned
        apply_to_layers(layers, 'native', native_updates)
        new_meta = dict(meta)
        new_meta['codebook'] = {'layers': layers}
        st.save_dataset_meta(filename, new_meta, identity)
        _archive(new_meta, layers, identity)
    return has_entries({s: layers[s] for s in SOURCES if s != 'native'})


def export_document(filename, identity=None):
    """The ``codebook.json`` document for a dataset (the export download and
    the reproducibility bundle): plain JSON, None for an unknown dataset."""
    columns = dataset_columns(filename, identity=identity)
    if columns is None:
        return None
    st = _st()
    meta = st.load_dataset_meta(filename, identity) or {}
    return {
        'format': EXPORT_FORMAT,
        'version': EXPORT_VERSION,
        'dataset': os.path.basename(st.resolve_path(filename, identity) or '')
        or filename,
        'original_name': meta.get('original_name')
        if isinstance(meta.get('original_name'), str) else None,
        'sha256': meta.get('sha256') if isinstance(meta.get('sha256'), str)
        else None,
        'dataset_version': st.active_version_number(filename, identity),
        'exported_at': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'codebook': merge(load_layers(filename, identity), columns),
    }


def export_json(filename, identity=None):
    """``export_document`` serialised (None for an unknown dataset)."""
    doc = export_document(filename, identity)
    return None if doc is None else json.dumps(doc, indent=2,
                                               ensure_ascii=False)
