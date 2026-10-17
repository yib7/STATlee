"""Dataset lifecycle routes: upload, paging, codebook, suggestions,
wrangling/version control, export, reset.

Implements roadmap items 5.1 (formats), 5.6 (cache), 5.8 (paging cache +
row cap), 5.13 (survey→codebook), 5.16 (wrangle + undo/redo), 5.3 (export),
4.5 (suggestion reroll), 4.6 (reset).
"""
import hashlib
import io
import json
import logging
import os
import tempfile
import textwrap
import threading
import time
import zipfile
from collections import OrderedDict

import numpy as np
import pandas as pd
from flask import Blueprint, current_app, jsonify, request, send_file
from werkzeug.utils import secure_filename

from .. import (
    bundle,
    codecheck,
    datatools,
    environment,
    llm,
    prompts,
    sandbox,
    storage,
)
from .. import codebook as codebook_store
from ..extensions import limiter
from ..usage import usage_breakdown
from . import (
    BACKGROUND_MAX,
    FILENAME_MAX,
    HEADER_FIELD_MAX,
    HEADERS_MAX_COUNT,
    MAPPING_KEY_MAX,
    MAPPING_VALUE_MAX,
    PREVIOUS_FIELD_MAX,
    PREVIOUS_MAX_COUNT,
    FieldError,
    clamp,
    clamp_history,
    clamp_mapping,
    clamp_str_list,
    dataset_filename_field,
    dict_field,
    json_error,
    json_object_reply,
    language_field,
    list_field,
    llm_json_error,
    model_for_role,
    moderation_gate,
    parse_json_reply,
    register_field_errors,
    request_json,
    str_field,
    string_mapping,
)

logger = logging.getLogger('statlee.datasets')

bp = Blueprint('datasets', __name__)
register_field_errors(bp)


def _filename(data):
    """The dataset a request names (L8: never a reserved version-file name)."""
    return dataset_filename_field(data)


def _cfg():
    return current_app.config['STATLEE']


def _incoming_size(file):
    """Bytes the uploaded ``file`` will occupy, read from its stream without
    consuming it. Best-effort: an unseekable stream reports 0 (the byte quota
    then only reflects already-stored data, which still fails safe on the next
    upload once the file has landed)."""
    try:
        stream = file.stream
        pos = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(pos)
        return size
    except (OSError, ValueError, AttributeError):
        return 0


def _quota_error(incoming_bytes, replacing=None):
    """P2-4: return a 413 ``json_error`` if accepting ``incoming_bytes`` more
    would push the caller's identity dir past its file-count or byte quota,
    else ``None``. Uses a cheap directory scan BEFORE the new file is saved so
    a burst of uploads cannot fill the disk ahead of the 2h TTL cleanup.

    ``replacing`` is the path the upload will overwrite, if it exists: the file
    count then does not grow and its bytes are freed, so a user at the cap can
    still refresh ``data.csv`` (D1-14)."""
    cfg = _cfg()
    count, used = storage.identity_usage()
    if replacing and os.path.exists(replacing):
        try:
            used -= os.stat(replacing).st_size
        except OSError:
            pass
    elif cfg.max_datasets_per_identity and count >= cfg.max_datasets_per_identity:
        return json_error(
            'Storage limit reached: at most '
            f'{cfg.max_datasets_per_identity} files per session. Remove data '
            'or use Reset before uploading more.', 413)
    if (cfg.max_bytes_per_identity
            and used + incoming_bytes > cfg.max_bytes_per_identity):
        mb = cfg.max_bytes_per_identity // (1024 * 1024)
        return json_error(
            f'Storage quota of {mb} MB exceeded for this session. Remove data '
            'or use Reset before uploading more.', 413)
    return None


# ---------------------------------------------------------------------------
# Small caches (5.6 classification/suggestion cache, 5.8 parsed-frame cache)
# ---------------------------------------------------------------------------
_analysis_cache = OrderedDict()   # (sha256, kind) -> result
_frame_cache = OrderedDict()      # (path, mtime) -> DataFrame
# (path, mtime, filters) -> filtered DataFrame. Lets /data_page filter the frame
# ONCE per filter set and then serve any page of it, instead of re-filtering the
# whole (up to data_page_row_cap) frame on every page request (P2-5).
_filtered_cache = OrderedDict()
_cache_lock = threading.Lock()
ANALYSIS_CACHE_MAX = 128
_MEASUREMENT_LEVELS = {'nominal': 'Nominal', 'ordinal': 'Ordinal',
                       'continuous': 'Continuous'}
SUGGESTIONS_MAX = 3
CLASSIFY_CHUNK = 80          # columns per classification call
CLASSIFY_MAX_CHUNKS = 10     # at most 800 columns classified per dataset
FRAME_CACHE_MAX = 4
FILTERED_CACHE_MAX = 8


def _normalize_suggestions(value):
    """A list of at most ``SUGGESTIONS_MAX`` non-empty strings from a
    ``/suggest`` reply: a bare array, ``{"suggestions": [...]}``, or an object
    holding exactly one list. Raises ``llm.MalformedReplyError`` when nothing
    usable is left (D3-9/D4-21)."""
    items = value
    if isinstance(value, dict):
        items = value.get('suggestions')
        if not isinstance(items, list):
            lists = [v for v in value.values() if isinstance(v, list)]
            items = lists[0] if len(lists) == 1 else None
    if not isinstance(items, list):
        raise llm.MalformedReplyError(detail='suggestions shape')
    out = [s.strip()[:PREVIOUS_FIELD_MAX * 2] for s in items
           if isinstance(s, str) and s.strip()]
    if not out:
        raise llm.MalformedReplyError(detail='no suggestions')
    return out[:SUGGESTIONS_MAX]


def _cache_get(cache, key):
    with _cache_lock:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    return None


def _cache_put(cache, key, value, max_size):
    with _cache_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > max_size:
            cache.popitem(last=False)


def clear_caches():
    with _cache_lock:
        _analysis_cache.clear()
        _frame_cache.clear()
        # Belt-and-suspenders: the mtime/path in each filtered key already
        # invalidates on any data change, but a wrangle/undo/revert/reset should
        # drop stale filtered frames immediately too.
        _filtered_cache.clear()


def _read_active(filename, nrows=None, row_cap=None):
    """Read the ACTIVE version of a dataset (5.16-aware).

    With ``row_cap`` (L5), at most ``row_cap + 1`` rows are parsed: a frame
    longer than ``row_cap`` comes back truncated (and uncached) so the caller
    can refuse it without first materializing the whole file."""
    path = storage.active_dataset_path(filename)
    if not path or not os.path.exists(path):
        return None, None
    if nrows is not None:
        return pd.read_csv(path, nrows=nrows), path
    mtime = os.stat(path).st_mtime
    cached = _cache_get(_frame_cache, (path, mtime))
    if cached is not None:
        return cached, path
    if row_cap:
        df = pd.read_csv(path, nrows=row_cap + 1)
        if len(df) > row_cap:
            return df, path
    else:
        df = pd.read_csv(path)
    _cache_put(_frame_cache, (path, mtime), df, FRAME_CACHE_MAX)
    return df, path


def read_version_frame(filename, version):
    """Cached full read of one version of a dataset (SP1: interpreting an
    older run reads the data it ran on). Returns ``(df, path)`` or
    ``(None, None)`` when that version's file is gone."""
    path = storage.dataset_version_path(filename, version)
    if not path:
        return None, None
    mtime = os.stat(path).st_mtime
    cached = _cache_get(_frame_cache, (path, mtime))
    if cached is not None:
        return cached, path
    df = pd.read_csv(path)
    _cache_put(_frame_cache, (path, mtime), df, FRAME_CACHE_MAX)
    return df, path


def read_active_frame(filename):
    """Public, cached full read of a dataset's active version for other
    blueprints. Returns ``(df, path)`` or ``(None, None)``."""
    return _read_active(filename)


def _json_records(df):
    """Rows as JSON-safe records (D1-9): NaN/NA and +/-inf become null. Flask
    would otherwise emit bare ``NaN``/``Infinity`` tokens, which the browser's
    ``JSON.parse`` rejects, breaking the data viewer."""
    df = df.replace([np.inf, -np.inf], np.nan)
    return df.astype(object).where(df.notna(), None).to_dict(orient='records')


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

@bp.route('/upload', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def upload_file():
    """Dataset upload — CSV/TSV/Excel/Stata/SPSS (5.1), normalized to CSV."""
    storage.cleanup_old_files(_cfg().file_ttl_seconds)

    if 'file' not in request.files:
        return json_error('No file part')
    file = request.files['file']
    if not file.filename:
        return json_error('No selected file')

    # D1-1: derive the extension and the stored name together, so the
    # extension that is validated is the one that is kept (non-Latin names
    # used to lose it to secure_filename).
    filename, ext = storage.upload_name(file.filename)
    if ext not in datatools.SUPPORTED_EXTENSIONS:
        return json_error(
            'Invalid file format. Supported: '
            + ', '.join(datatools.SUPPORTED_EXTENSIONS))

    # P2-3: version artifacts are named "{stem}__vN.csv" and share this flat
    # identity dir, so a file whose stem already ends in a __vN suffix could
    # later be overwritten by a wrangle of another dataset. Reserve that suffix
    # at upload time rather than silently corrupting the second file.
    if storage.is_reserved_name(filename):
        return json_error(
            'Filenames ending in a version suffix like "__v2" are reserved for '
            'dataset history. Please rename the file and upload again.')
    csv_name = os.path.splitext(filename)[0] + '.csv'
    target = storage.resolve_path(csv_name)
    if not target:
        return json_error('Invalid filename')

    quota = _quota_error(_incoming_size(file), replacing=target)
    if quota is not None:
        return quota

    # D1-8: parse in a hidden staging file and move it into place only on
    # success. No failure path can leave an unregistered file counting toward
    # the quota, and a failed re-upload leaves the existing dataset intact.
    staged = storage.incoming_path(ext)
    staged_csv = os.path.splitext(staged)[0] + '.csv'

    def _discard(status, message):
        for p in {staged, staged_csv}:
            try:
                os.remove(p)
            except OSError:
                pass
        return json_error(message, status)

    file.save(staged)
    cfg = _cfg()
    info = {}
    try:
        # P2-2: bound the parse so a decompression bomb can't OOM/pin the
        # worker. 0 disables either cap (0 * 1024 * 1024 == 0).
        csv_path, labels = datatools.normalize_to_csv(
            staged,
            max_cells=cfg.max_upload_cells,
            max_uncompressed_bytes=cfg.max_upload_uncompressed_mb * 1024 * 1024,
            info=info)
    except datatools.ParseLimitError as e:
        return _discard(413, str(e))
    except (datatools.MissingDependencyError, datatools.DatasetParseError) as e:
        return _discard(422, str(e))
    except Exception:
        logger.exception("Failed to parse uploaded dataset")
        return _discard(422, 'Could not read that file. Is it a valid dataset?')

    try:
        df = info.get('frame')
        if df is None:
            df = pd.read_csv(csv_path)
        profile = datatools.profile_dataframe(df)
    except Exception:
        logger.exception("Failed to profile dataset")
        return _discard(422, 'Could not read the dataset after conversion.')

    os.replace(csv_path, target)
    clear_caches()
    sha256 = datatools.file_sha256(target)
    storage.register_dataset(csv_name)
    # SP2: the meta sidecar carries the server codebook. Native .sav/.dta
    # metadata seeds its native layer; identical content restores the user,
    # pdf and llm layers of an earlier upload (keyed by sha256).
    restored = codebook_store.init_for_upload(csv_name, {
        'sha256': sha256,
        'original_name': file.filename,
        'labels': labels,
    }, native=info.get('native_codebook'))

    return jsonify({
        'message': 'File uploaded',
        'filename': csv_name,
        'original_name': file.filename,   # display name (D1-1)
        'profile': profile,
        'labels': labels,        # native .sav/.dta labels seed the codebook
        'warnings': info.get('warnings', []),   # e.g. skipped sheets (D1-13)
        'sha256': sha256,
        'changelog': storage.dataset_changelog(csv_name),  # seeds the cleaning panel
        'codebook_restored': restored,   # GET /codebook has the merged codebook
    }), 200


@bp.route('/upload_pdf', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def upload_pdf():
    """Optional PDF/TXT codebook or survey upload. Max page count enforced."""
    storage.cleanup_old_files(_cfg().file_ttl_seconds)
    max_pages = _cfg().pdf_max_pages

    if 'file' not in request.files:
        return json_error('No file part')
    file = request.files['file']
    if not file.filename:
        return json_error('No selected file')

    # D1-1: branch on the extension of the name we STORE, derived once from
    # the raw name. Non-Latin names used to sanitize to a bare 'pdf', which
    # skipped both the page cap and the TXT size check below.
    filename, ext = storage.upload_name(file.filename)
    if ext not in ('.pdf', '.txt'):
        return json_error('Invalid file format. Please upload a PDF or TXT.')
    filepath = storage.resolve_path(filename)
    if not filepath:
        return json_error('Invalid filename')

    incoming = _incoming_size(file)
    quota = _quota_error(incoming, replacing=filepath)
    if quota is not None:
        return quota

    # P2-5: a .txt is rendered to PDF page-by-page through fpdf (pure Python,
    # slow) and only THEN measured against pdf_max_pages, so a 16 MB text file
    # could hold a worker in that conversion before being rejected. Measure the
    # raw bytes first. The budget (4096 bytes per allowed page) sits well above
    # real text density (~3 KB per dense rendered page), so nothing short enough
    # to pass the page cap is rejected here -- it only short-circuits the
    # pathological oversized case before any conversion work happens.
    if ext == '.txt':
        max_txt_bytes = max_pages * 4096
        if incoming > max_txt_bytes:
            return json_error(
                f'Text file is too large ({incoming // 1024} KB). The '
                f'{max_pages}-page documentation limit allows roughly '
                f'{max_txt_bytes // 1024} KB of text. Please upload a shorter '
                'document.', 413)

    staged = storage.incoming_path(ext)
    file.save(staged)

    def _discard(status, message):
        try:
            os.remove(staged)
        except OSError:
            pass
        return json_error(message, status)

    if ext == '.pdf':
        from pypdf import PdfReader
        try:
            with open(staged, 'rb') as f:
                page_count = len(PdfReader(f).pages)
        except Exception:
            return _discard(422, 'Failed to read PDF.')
        if page_count > max_pages:
            return _discard(
                413, f'PDF is {page_count} pages, which exceeds the '
                     f'{max_pages}-page limit. Please upload a shorter document.')
    else:
        # D1-10: a TXT codebook is kept as UTF-8 text and inlined into the
        # extraction prompt. It used to be rendered to PDF in a Latin-1 core
        # font, which silently erased Cyrillic/CJK text and smart quotes. The
        # byte budget above stands in for the page cap.
        try:
            with open(staged, 'rb') as f:
                text, _enc = datatools.decode_text_bytes(f.read())
            with open(staged, 'w', encoding='utf-8', newline='') as f:
                f.write(text)
        except Exception:
            logger.exception("TXT codebook decode failed")
            return _discard(422, 'Failed to read the text file.')

    os.replace(staged, filepath)
    return jsonify({'message': 'Documentation uploaded', 'filename': filename,
                    'original_name': file.filename}), 200


@bp.route('/extract_pdf_codebook', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def extract_pdf_codebook():
    """Map variable labels from an uploaded PDF.

    mode='codebook' (default): formal codebook extraction.
    mode='survey' (5.13): infer a codebook from the questionnaire itself.
    """
    data = request_json()
    # A codebook document, not a dataset: no version files exist for it.
    filename = str_field(data, 'filename', FILENAME_MAX)
    mode = str_field(data, 'mode', 32, default='codebook')
    # D1-6: the survey header list is client text going into a paid prompt.
    headers = clamp_str_list(list_field(data, 'headers'),
                             HEADERS_MAX_COUNT, HEADER_FIELD_MAX)
    # SP2: the dataset whose server codebook the extraction seeds (pdf
    # layer). Optional: without it the mapping is only returned, as before.
    dataset = dataset_filename_field(data, 'dataset')
    if not filename:
        return json_error('Missing filename')
    filepath = storage.resolve_path(filename)
    if not filepath or not os.path.exists(filepath):
        return json_error('Invalid filename')
    dataset_columns = None
    if dataset:
        dataset_columns = codebook_store.dataset_columns(dataset)
        if dataset_columns is None:
            return json_error('Unknown dataset.', 404)
    doc_ext = os.path.splitext(filepath)[1].lower()
    if doc_ext not in ('.pdf', '.txt'):
        return json_error('Not a documentation file.')

    try:
        with open(filepath, 'rb') as f:
            doc_bytes = f.read()
        if mode == 'survey':
            prompt = prompts.survey_extract(headers)
        else:
            prompt = prompts.pdf_extract()

        if doc_ext == '.txt':
            # D1-10: TXT codebooks are sent as text, so non-Latin text survives.
            text, _enc = datatools.decode_text_bytes(doc_bytes)
            contents = [prompt, "ATTACHED DOCUMENT (plain text, from the "
                                "user's .txt upload):\n"
                        + prompts.untrusted('DOCUMENT', text)]
        else:
            contents = [llm.MediaPart(data=doc_bytes, mime_type='application/pdf'),
                        prompt]
        result = llm.get_service().generate(
            'flash', contents, temperature=0.1, json_mode=True)
        # D3-9/D3-10: tolerant parse; the mapping feeds every later prompt as
        # column descriptions, so values are normalised to strings.
        mapping = string_mapping(json_object_reply(result.text),
                                 key_max=MAPPING_KEY_MAX,
                                 value_max=MAPPING_VALUE_MAX)
        body = {'status': 'success', 'mapping': mapping,
                'usage': usage_breakdown(result.usage)}
        if dataset_columns is not None:
            body['codebook_updated'] = _seed_pdf_layer(
                dataset, dataset_columns, mapping)
        return jsonify(body)
    except Exception as exc:
        logger.exception("PDF extraction error")
        return llm_json_error(
            exc, 'Failed to extract definitions from the document.')


def _seed_pdf_layer(dataset, columns, mapping):
    """Write extracted descriptions into the dataset's codebook as pdf-layer
    labels (SP2), matching names case-insensitively like the prompt context
    always did. Returns how many columns were described."""
    by_lower = {}
    for column in columns:
        by_lower.setdefault(column.lower(), column)
    updates = {}
    for key, text in mapping.items():
        column = by_lower.get(str(key).strip().lower())
        if column is None:
            continue
        fields = codebook_store.clean_fields({'label': text}, lenient=True)
        if fields.get('label'):
            updates[column] = fields
    try:
        codebook_store.apply(dataset, 'pdf', updates)
    except (codebook_store.CodebookStorageError,
            codebook_store.CodebookTooLarge) as exc:
        # The mapping is still returned; only the server codebook misses it.
        logger.warning("PDF codebook descriptions for %d column(s) were not "
                       "stored: %s", len(updates), type(exc).__name__)
        return 0
    return len(updates)


def server_codebook_context(filename, df):
    """``(levels, descriptions)`` of the dataset's server codebook for
    ``datatools.build_column_context``, clamped like the client mapping used
    to be (SP2: the client's ``codebook``/``pdf_mapping`` are ignored)."""
    entries = codebook_store.merged(filename, [str(c) for c in df.columns])
    levels, descriptions = codebook_store.prompt_levels_and_descriptions(
        entries or {})
    return clamp_mapping(levels), clamp_mapping(descriptions)


def ignore_client_codebook(data, route):
    """The client's ``codebook``/``pdf_mapping`` fields stay accepted (old
    clients send them; a wrong JSON type is still a 400) but never reach a
    prompt: the server codebook is the only source (SP2)."""
    for key in ('codebook', 'pdf_mapping'):
        if dict_field(data, key):
            logger.debug("%s: ignoring the client-sent %r field", route, key)


# ---------------------------------------------------------------------------
# Data viewer
# ---------------------------------------------------------------------------

@bp.route('/data_page', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_data_page)
def data_page():
    data = request_json()
    filename = _filename(data)
    try:
        page = int(data.get('page', 1))
        per_page = max(1, min(int(data.get('per_page', 100)), 500))
    except (TypeError, ValueError):
        return json_error("'page' and 'per_page' must be integers.")
    filters = data.get('filters') or {}
    if not isinstance(filters, dict):
        return json_error("'filters' must be an object.")

    if not filename:
        return json_error('Missing filename')
    cap = _cfg().data_page_row_cap
    try:
        df, path = _read_active(filename, row_cap=cap)
    except Exception:
        logger.exception("data_page read failure")
        return json_error('Could not read dataset.', 500)
    if df is None:
        return json_error('Invalid filename')

    if cap and len(df) > cap:
        return json_error(
            f'This dataset has more than {cap:,} rows, the viewer limit. '
            'Analysis still works; the table view is disabled for files this '
            'large.', 413)

    try:
        # Reuse a previously-filtered frame for this exact (path, mtime, filters)
        # set (P2-5), so paging through many pages of one filter set filters the
        # frame ONCE instead of re-scanning up to data_page_row_cap rows per page.
        # The mtime/path in the key invalidate the entry on any data change.
        key = (path, os.stat(path).st_mtime,
               tuple(sorted((c, str(t)) for c, t in filters.items() if t)))
        filtered = _cache_get(_filtered_cache, key)
        if filtered is None:
            filtered = df
            for col, term in filters.items():
                if term and col in filtered.columns:
                    # regex=False (P1-3): filter terms are literal text, so users
                    # can type '(' or '$' and a hostile pattern like '(a+)+$'
                    # cannot pin a worker thread with catastrophic backtracking.
                    filtered = filtered[filtered[col].astype(str).str.contains(
                        str(term), case=False, na=False, regex=False)]
            _cache_put(_filtered_cache, key, filtered, FILTERED_CACHE_MAX)

        total_rows = len(filtered)
        total_pages = max(1, -(-total_rows // per_page)) if total_rows else 0
        page = max(1, min(page, total_pages) if total_pages else 1)
        start = (page - 1) * per_page
        df_page = filtered.iloc[start:start + per_page]
        return jsonify({
            'status': 'success',
            'data': _json_records(df_page),
            'total_rows': total_rows,
            'total_pages': total_pages,
            'current_page': page,
        })
    except Exception:
        logger.exception("data_page filter/pagination failure")
        return json_error('Could not page through the dataset.', 500)


# ---------------------------------------------------------------------------
# Intelligent codebook + suggestions (cached per content hash, 5.6)
# ---------------------------------------------------------------------------

def _seed_llm_layer(filename, levels):
    """Classification seeds the llm layer's ``type`` (SP2); the user's and
    native fields still win in the merged view. The response keeps the
    model's levels for older clients."""
    try:
        codebook_store.apply(filename, 'llm', {
            column: {'type': level.lower()}
            for column, level in levels.items()})
    except (codebook_store.CodebookStorageError,
            codebook_store.CodebookTooLarge) as exc:
        # The classification is still returned, but /suggest and /chat will
        # not see these levels in the server codebook.
        logger.warning("Classification levels for %d column(s) were not "
                       "stored in the codebook: %s", len(levels),
                       type(exc).__name__)


@bp.route('/classify_variables', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def classify_variables():
    data = request_json()
    filename = _filename(data)
    if not filename:
        return json_error('Missing filename')
    try:
        df, path = _read_active(filename)
    except Exception:
        logger.exception("classify read failure")
        return json_error('Could not read dataset.', 500)
    if df is None:
        return json_error('Invalid filename')

    sha = datatools.file_sha256(path)
    cached = _cache_get(_analysis_cache, (sha, 'classify'))
    if cached is not None:
        _seed_llm_layer(filename, cached)
        return jsonify({'status': 'success', 'codebook': cached, 'cached': True})

    try:
        # D1-5/D3-6: classify in chunks of CLASSIFY_CHUNK columns instead of
        # truncating at the first 80, so a wide survey's later columns get a
        # measurement level too (and can then be found by feature selection).
        # D3-7: the metadata describes the full frame.
        columns = list(df.columns)
        chunks = [columns[i:i + CLASSIFY_CHUNK]
                  for i in range(0, len(columns), CLASSIFY_CHUNK)]
        chunks = chunks[:CLASSIFY_MAX_CHUNKS] or [[]]
        codebook, usages = {}, []
        for chunk in chunks:
            preview = datatools.metadata_json(df[chunk],
                                              max_columns=CLASSIFY_CHUNK)
            result = llm.get_service().generate(
                'flash', prompts.classify(preview), temperature=0.1,
                json_mode=True)
            usages.append(result.usage)
            part = json_object_reply(result.text)
            wanted = set(map(str, chunk))
            # D3-9: only real columns, and only the three measurement levels
            # the prompts and the UI understand (case-normalised).
            for key, level in part.items():
                level = _MEASUREMENT_LEVELS.get(str(level).strip().lower()) \
                    if isinstance(level, str) else None
                if key in wanted and level:
                    codebook[key] = level
        _cache_put(_analysis_cache, (sha, 'classify'), codebook, ANALYSIS_CACHE_MAX)
        _seed_llm_layer(filename, codebook)
        return jsonify({'status': 'success', 'codebook': codebook,
                        'usage': usage_breakdown(*usages)})
    except Exception as exc:
        logger.exception("Classification error")
        return llm_json_error(exc, 'Variable classification failed.')


@bp.route('/suggest', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def suggest_analysis():
    data = request_json()
    filename = _filename(data)
    ignore_client_codebook(data, '/suggest')
    # D1-6: every field below reaches the paid suggest prompt.
    previous = clamp_str_list(list_field(data, 'previous'),    # 4.5 reroll
                              PREVIOUS_MAX_COUNT, PREVIOUS_FIELD_MAX)

    if not filename:
        return json_error('Missing filename')
    try:
        df, path = _read_active(filename)
    except Exception:
        logger.exception("suggest read failure")
        return json_error('Could not read dataset.', 500)
    if df is None:
        return json_error('Invalid filename')

    sha = datatools.file_sha256(path)
    # SP2: the codebook context is the server's; D1-11: the cache key covers
    # all of it (levels and descriptions), so an edit serves new suggestions.
    codebook, pdf_mapping = server_codebook_context(filename, df)
    context_hash = hashlib.sha256(json.dumps(
        [codebook, pdf_mapping], sort_keys=True, default=str).encode()
    ).hexdigest()
    cache_key = (sha, 'suggest', context_hash)
    if not previous:
        cached = _cache_get(_analysis_cache, cache_key)
        if cached is not None:
            return jsonify({'status': 'success', 'suggestions': cached,
                            'cached': True})

    try:
        context = datatools.build_column_context(df, codebook, pdf_mapping)
        result = llm.get_service().generate(
            'flash', prompts.suggest(context, previous),
            temperature=0.7, json_mode=True)
        # D3-9/D4-21: OpenAI JSON mode returns an object, other providers may
        # return a bare array; either way the client gets <= 3 strings, and a
        # reply with none is a 502 that is never cached.
        suggestions = _normalize_suggestions(parse_json_reply(result.text))
        if not previous:
            _cache_put(_analysis_cache, cache_key, suggestions, ANALYSIS_CACHE_MAX)
        return jsonify({'status': 'success', 'suggestions': suggestions,
                        'usage': usage_breakdown(result.usage)})
    except Exception as exc:
        logger.exception("Suggestion error")
        return llm_json_error(exc, 'Could not generate suggestions.')


# ---------------------------------------------------------------------------
# Conversational wrangling + version control (5.16)
# ---------------------------------------------------------------------------

WRANGLE_HARNESS = textwrap.dedent("""\
    import pandas as pd
    import numpy as np
    df = pd.read_csv({src!r})
    # --- generated transform ---
    {code}
    # ---------------------------
    if not isinstance(df, pd.DataFrame):
        raise TypeError('Transform must leave a DataFrame in `df`.')
    df.to_csv('__wrangled.csv', index=False)
    print(f"OK rows={{len(df)}} cols={{len(df.columns)}}")
    """)


@bp.route('/wrangle', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_run)
def wrangle():
    data = request_json()
    filename = _filename(data)
    # D1-6/D3-1: the instruction is billed twice (moderation + plan) and is
    # stored in the version manifest, so cap it like the /chat prompt.
    instruction = str_field(data, 'instruction', BACKGROUND_MAX).strip()
    if not filename or not instruction:
        return json_error('Missing filename or instruction')

    service = llm.get_service()
    cfg = _cfg()
    denied, mod = moderation_gate(service, prompts.moderation(instruction),
                                  log_message="Wrangle moderation failed")
    if denied is not None:
        return denied

    # S5: take the version token BEFORE reading the data. Any upload, wrangle,
    # undo or revert that lands after this point changes it, so the commit
    # below is refused (409) instead of stacking a frame built from data that
    # is no longer the active version.
    base_token = storage.dataset_version_token(filename)
    try:
        df, active_path = _read_active(filename)
    except Exception:
        logger.exception("wrangle read failure")
        return json_error('Could not read dataset.', 500)
    if df is None:
        return json_error('Invalid filename')

    try:
        result = service.generate(
            cfg.wrangle_role,
            prompts.wrangle(datatools.metadata_json(df), instruction),
            temperature=0.1, json_mode=True)
        plan = parse_json_reply(result.text)
    except Exception as exc:
        logger.exception("Wrangle plan generation failed")
        return llm_json_error(exc, 'Could not generate the transformation.')

    if (not isinstance(plan, dict)
            or not isinstance(plan.get('code') or '', str)
            or not isinstance(plan.get('error') or '', str)
            or not isinstance(plan.get('summary') or '', str)):
        logger.warning("Wrangle plan had an unexpected shape: %r", type(plan))
        return json_error('Could not generate the transformation.', 502)
    if plan.get('error') or not plan.get('code'):
        return json_error(plan.get('error')
                          or 'Could not translate that instruction.', 422)

    # Run-guard (0.4): the LLM-generated transform code is itself re-moderated
    # before execution, exactly as /run does for user-edited scripts. Moderating
    # only the user instruction (above) is not enough — a jailbroken or
    # misbehaving model can emit code doing network access, env exfiltration, or
    # shell execution regardless of a benign-looking instruction. Default-deny:
    # a malformed/ambiguous verdict blocks (see moderation_blocked()).
    denied, code_verdict = moderation_gate(
        service, prompts.code_moderation(plan['code'], 'Python'),
        denied='The generated transform was rejected by the safety check.',
        unavailable='Could not verify the generated transform.',
        log_message="Wrangle code-moderation failed")
    if denied is not None:
        return denied

    # Static pre-check (P1-1a): a non-LLM AST gate on the generated transform,
    # run AFTER the (bypassable) LLM code_moderation as defense-in-depth, before
    # the transform is approved and the harness executes it.
    static_blocked, static_reason = codecheck.check_code(plan['code'], 'Python')
    if static_blocked:
        return json_error(
            f'The generated transform was rejected by the safety check '
            f'(static analysis). {static_reason}', 403)

    # Record the moderated transform in the approved-script store so /wrangle
    # upholds the same "every executed LLM-generated script is moderated and
    # recorded" invariant as the /chat -> /run path (0.4 run-guard).
    storage.save_approved_script(plan['code'], 'Python', source='wrangle')

    dataset_basename = os.path.basename(active_path)
    script = WRANGLE_HARNESS.format(src=dataset_basename,
                                    code=plan['code'])
    run = sandbox.run_in_sandbox(
        script, 'Python', dataset_path=active_path,
        dataset_name=dataset_basename,
        timeout=cfg.exec_timeout, memory_mb=cfg.exec_memory_mb,
        output_limit=cfg.exec_output_limit,
        mode=cfg.sandbox_mode, runner_image=cfg.runner_image,
        work_root=cfg.sandbox_work_root, collect=('__wrangled.csv',))

    if not run.success or '__wrangled.csv' not in run.files:
        logger.warning("Wrangle execution failed: %s", run.output[:500])
        return json_error(
            'The transformation failed to execute. Try rephrasing the '
            'instruction. Details: ' + run.output[:300], 422)

    try:
        new_df = pd.read_csv(io.BytesIO(run.files['__wrangled.csv']))
        # D6: keep the transform on the version entry (export replays it).
        # L1: refuse to commit when a concurrent wrangle/undo moved the active
        # version while this one ran, rather than discarding that edit.
        storage.add_dataset_version(
            filename, new_df, instruction, summary=plan.get('summary'),
            code=plan['code'], base_file=dataset_basename,
            base_token=base_token, role=cfg.wrangle_role,
            model=model_for_role(cfg, cfg.wrangle_role))
        clear_caches()
        return jsonify({
            'status': 'success',
            'summary': plan.get('summary') or instruction,
            'profile': datatools.profile_dataframe(new_df),
            'changelog': storage.dataset_changelog(filename),
            'usage': usage_breakdown(mod.usage, result.usage,
                                     code_verdict.usage),
        })
    except storage.VersionConflict:
        return json_error(
            'The dataset changed while this transformation was running '
            '(another edit, undo or revert landed first). Reload the data '
            'and try again.', 409)
    except Exception:
        logger.exception("Failed to persist wrangled version")
        return json_error('Could not save the transformed dataset.', 500)


@bp.route('/dataset_versions', methods=['POST'])
def dataset_versions():
    data = request_json()
    filename = _filename(data)
    if not filename:
        return json_error('Missing filename')
    changelog = storage.dataset_changelog(filename)
    if changelog is None:
        return json_error('Unknown dataset.', 404)
    # S5: the active version's columns too, so a client recovering from a
    # /wrangle 409 learns the current headers without a /data_page probe.
    try:
        df, _ = _read_active(filename)
        profile = datatools.profile_dataframe(df) if df is not None else None
    except Exception:
        logger.exception("dataset_versions profile failure")
        profile = None
    return jsonify({'status': 'success', 'changelog': changelog,
                    'headers': profile['headers'] if profile else None,
                    'profile': profile})


@bp.route('/version_control', methods=['POST'])
def version_control():
    """Undo/redo by moving the active-version pointer (5.16)."""
    data = request_json()
    filename = _filename(data)
    direction = data.get('direction')
    if not filename or direction not in ('undo', 'redo'):
        return json_error("Missing filename or direction ('undo'|'redo')")
    manifest = storage.shift_dataset_version(filename, direction)
    if manifest is None:
        return json_error('Unknown dataset.', 404)
    clear_caches()
    try:
        df, _ = _read_active(filename)   # D3-7: profile the whole version
        profile = datatools.profile_dataframe(df) if df is not None else None
    except Exception:
        profile = None
    return jsonify({
        'status': 'success',
        'changelog': storage.dataset_changelog(filename),
        'profile': profile,
    })


@bp.route('/revert_dataset', methods=['POST'])
def revert_dataset():
    """Restore the dataset to its original upload as a new, undo-able version
    (4.6). Distinct from /reset, which wipes the whole workspace."""
    data = request_json()
    filename = _filename(data)
    if not filename:
        return json_error('Missing filename')
    manifest = storage.revert_to_original(filename)
    if manifest is None:
        return json_error('Unknown dataset.', 404)
    clear_caches()
    try:
        df, _ = _read_active(filename)   # D3-7: profile the whole version
        profile = datatools.profile_dataframe(df) if df is not None else None
    except Exception:
        profile = None
    return jsonify({
        'status': 'success',
        'changelog': storage.dataset_changelog(filename),
        'profile': profile,
    })


# ---------------------------------------------------------------------------
# Project export (5.3) and reset (4.6)
# ---------------------------------------------------------------------------

def _for_dataset(entry, dataset_name):
    """True when an approved-script entry was generated for ``dataset_name``
    (the stored name; None when no dataset is exported): its approval context
    names it. An entry without a context (an edited script approved at /run)
    names no dataset."""
    ctx = entry.get('context') if isinstance(entry, dict) else None
    name = ctx.get('filename') if isinstance(ctx, dict) else None
    name = secure_filename(name) if isinstance(name, str) and name else None
    return (name or None) == (dataset_name or None)


def _export_script(client_code, client_language, dataset_name, run=None):
    """Pick the analysis script to bundle when no included run kept one
    (D4-9). Only a script that belongs to the exported dataset is ever
    bundled: another dataset's script would not read this one.

    ``run`` is ``(run_id, record)`` of the latest run on this dataset that
    may ship (``bundle.eligible_runs``) or None. Returns ``(code, language,
    source, editor_differs)``. Order:

    1. the editor's code, when it was generated for this dataset in this
       session (its approval context names it);
    2. otherwise the NEWER of that run's script and the last script generated
       for this dataset, so a script generated after a run is not shadowed by
       the older run (and vice versa);
    3. with no dataset exported, the editor text as-is; with one, nothing
       (the README then says no analysis has been run on it yet).

    A /wrangle transform is never bundled as the analysis script: it is a
    fragment that expects a ``df`` in scope (P2-1); cleaning.py replays those.
    """
    if client_code:
        entry = storage.approved_entry(client_code, client_language)
        if entry and entry.get('source') != 'wrangle' \
                and _for_dataset(entry, dataset_name):
            return client_code, client_language, 'reviewed editor script', False

    candidates = []
    if run and (run[1].get('script') or {}).get('code'):
        candidates.append((storage.run_time(run[0]) or 0, run[1]['script'],
                           'last run'))
    generated = storage.load_approved_script(
        exclude_sources=('wrangle',),
        match=lambda entry: _for_dataset(entry, dataset_name))
    if generated:
        candidates.append((generated.get('ts') or 0, generated,
                           'last generated script'))
    if candidates:
        _, record, source = max(candidates, key=lambda c: c[0])
        language = record.get('language')
        if not isinstance(language, str):   # legacy store entry (pre-D1-12)
            language = 'Python'
        code = record['code']
        differs = bool(client_code) and client_code.strip() != code.strip()
        return code, language, source, differs

    if client_code and not dataset_name:
        entry = storage.approved_entry(client_code, client_language)
        if not (entry and entry.get('source') == 'wrangle'):
            return (client_code, client_language,
                    'editor (not run or reviewed in this session)', False)
    return '', client_language, None, False


def _comment(text):
    return ' '.join(str(text or '').split())[:300]


def _cleaning_script(lineage, data_name, include_prompts=True):
    """cleaning.py (D6): rebuild the analysed dataset from the original upload
    by replaying each recorded /wrangle transform in order, the same way the
    in-app harness ran them. Without ``include_prompts`` a step's comment
    never falls back to the instruction the user typed."""
    lines = [
        '"""Rebuilds the dataset the analysis used from the original upload by',
        'replaying the data-cleaning steps recorded in STATlee, in order.',
        '',
        f'Run from this folder:  python cleaning.py   (writes {data_name})',
        '"""',
        'import numpy as np  # noqa: F401  (available to every step, as in-app)',
        'import pandas as pd',
        '',
        f'df = pd.read_csv({("data/original_" + data_name)!r})',
        '',
    ]
    for n, step in enumerate(lineage['steps'], start=1):
        label = step.get('summary') or (
            step.get('instruction') if include_prompts else None)
        lines.append(f'# Step {n} (version {step["v"]}): '
                     f'{_comment(label or "cleaning step")}')
        lines.append(step['code'].rstrip())
        lines.append("if not isinstance(df, pd.DataFrame):")
        lines.append(f"    raise TypeError('Step {n} must leave a DataFrame in df.')")
        lines.append('')
    lines.append(f'df.to_csv({data_name!r}, index=False)')
    lines.append(f'print(f"Wrote {data_name}: {{len(df)}} rows, '
                 f'{{len(df.columns)}} columns")')
    return '\n'.join(lines) + '\n'


# The export zip stays in memory up to this size, then spools to disk.
EXPORT_SPOOL_MAX = 16 * 1024 * 1024


def _report_md(stem, history, last_output, interpretation, include_prompts):
    lines = [f'# Project: {stem}', '',
             f'_Exported from STATlee on {time.strftime("%Y-%m-%d %H:%M")}_', '']
    if history and include_prompts:
        lines.append('## Analysis requests')
        lines.extend(f"- {msg['text']}" for msg in history
                     if msg['role'] == 'user')
        lines.append('')
    if last_output:
        lines += ['## Terminal output', '```', last_output, '```', '']
    if interpretation:
        lines += ['## AI interpretation', interpretation, '']
    return '\n'.join(lines)


def _export_options(data):
    """SP4 export options: ``include_prompts`` (bool, default true) and
    ``runs`` (all|starred|latest, default all)."""
    include_prompts = data.get('include_prompts', True)
    if not isinstance(include_prompts, bool):
        raise FieldError("'include_prompts' must be true or false.")
    runs = data.get('runs', 'all')
    if not isinstance(runs, str) or runs not in bundle.RUN_SELECTIONS:
        raise FieldError("'runs' must be 'all', 'starred' or 'latest'.")
    return include_prompts, runs


@bp.route('/export', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_run)
def export_project():
    """The reproducibility bundle (SP4): it runs as unzipped and documents
    what produced it (statlee/bundle.py builds provenance.json and
    README.md; statlee/environment.py captures environment.txt). Rate
    limited like /run: an export can run the sandboxed environment probe."""
    data = request_json()
    filename = _filename(data)
    language = language_field(data)
    include_prompts, runs_selection = _export_options(data)
    # Not a prompt (only written into report.md), so the bounds are generous;
    # clamp_history also drops non-dict turns that used to 500 here.
    history = clamp_history(data.get('history'), max_turns=1000,
                            field_max=20_000)
    # The interpretation in report.md is always the server's copy of the
    # run it describes; a client-sent 'interpretation' is ignored.
    report_md = clamp(data.get('report'), 400_000)
    client_code = str_field(data, 'code')
    cfg = _cfg()

    root = storage.identity_root()
    staged = storage.resolve_path(filename) if filename else None
    dataset_name = os.path.basename(staged) if staged else None
    active = storage.active_dataset_path(filename) if filename else None
    lineage = None
    if active and os.path.exists(active) and staged:
        lineage = storage.dataset_lineage(filename)
    lineage_steps = (bundle.lineage_steps(lineage, include_prompts, root)
                     if lineage else [])
    # Only runs on this dataset's current content: their meta records the
    # stored name and a hash of one of the versions this bundle documents.
    dataset_shas = None
    if dataset_name:
        dataset_shas = bundle.lineage_shas(lineage_steps)
        if not lineage and active and os.path.exists(active):
            dataset_shas.add(bundle.file_sha(active, root))
    eligible, left_out = bundle.eligible_runs(dataset_name, dataset_shas)
    records = bundle.load_runs(bundle.select_run_ids(
        runs_selection, cfg.max_runs_per_identity, eligible))
    # README.md reports only the left-out runs this selection would ship.
    left_out = bundle.left_out_counts(
        runs_selection, cfg.max_runs_per_identity, eligible, left_out)

    # analysis.py|analysis.R is the latest run in the bundle, so its hash
    # matches a provenance step; with no such run, the latest approved
    # script as before (_export_script).
    script_run = bundle.latest_scripted_run(records)
    if script_run:
        run_id, record = script_run
        code = record['script']['code']
        language = ('R' if str(record['script'].get('language')).lower() == 'r'
                    else 'Python')
        script_source = f'script of run {run_id}'
        editor_differs = bool(client_code) and \
            client_code.strip() != code.strip()
        last_output = record.get('output') or ''
        interpretation = record.get('interpretation') or ''
        script_run_id = report_run_id = run_id
    else:
        # No included run kept a script. report.md and the script come only
        # from the latest run on THIS dataset (any selection), never another
        # dataset's.
        fallback = None
        if eligible:
            loaded = bundle.load_runs([eligible[-1][0]])
            fallback = loaded[0] if loaded else None
        code, language, script_source, editor_differs = _export_script(
            client_code, language, dataset_name, fallback)
        last_output = interpretation = ''
        report_run_id = None
        if fallback:
            report_run_id = fallback[0]
            last_output = fallback[1].get('output') or ''
            interpretation = fallback[1].get('interpretation') or ''
        script_run_id = None
    env_text = environment.capture(cfg)

    stem = os.path.splitext(secure_filename(filename or 'project'))[0] or 'project'
    script_name = None
    data_name = None
    has_codebook = has_original = has_cleaning = False
    # Spooled: small bundles stay in memory, big ones (many runs of plots)
    # roll over to a temp file. Closed by the response (or below on error).
    spool = tempfile.SpooledTemporaryFile(max_size=EXPORT_SPOOL_MAX)
    try:
        with zipfile.ZipFile(spool, 'w', zipfile.ZIP_DEFLATED) as zf:
            if active and os.path.exists(active) and staged:
                # D4-9: /run stages the active version under the dataset's
                # stored name (dataset_name in run_code), and the script
                # reads it from its working directory -- so it goes at the
                # zip root under that name, not as data/<name>__vN.csv.
                if bundle.write_file(zf, active, dataset_name, root):
                    data_name = dataset_name
                    original = (lineage or {}).get('original') or active
                    has_original = bundle.write_file(
                        zf, original, f'data/original_{data_name}', root)
                if (data_name and has_original and lineage
                        and lineage['steps'] and lineage['replayable']):
                    zf.writestr('cleaning.py', _cleaning_script(
                        lineage, data_name, include_prompts))
                    has_cleaning = True
                codebook_json = codebook_store.export_json(filename)
                if codebook_json:
                    zf.writestr('codebook.json', codebook_json)
                    has_codebook = True
            if code:
                script_name = ('analysis.py' if language.lower() == 'python'
                               else 'analysis.R')
                zf.writestr(script_name, code)

            run_dirs = bundle.write_run_outputs(zf, records, root)
            zf.writestr('environment.txt', env_text)
            zf.writestr('provenance.json', json.dumps(
                bundle.provenance(cfg, lineage, records, include_prompts,
                                  root, steps=lineage_steps),
                indent=2, ensure_ascii=False))
            zf.writestr('README.md', bundle.readme(
                stem, script_name=script_name, script_source=script_source,
                editor_differs=editor_differs, data_name=data_name,
                lineage=lineage, run_dirs=run_dirs,
                runs_selection=runs_selection,
                include_prompts=include_prompts,
                has_codebook=has_codebook, has_report=bool(report_md),
                provider=cfg.llm_provider,
                generated=time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime()),
                script_run=script_run_id, has_original=has_original,
                has_cleaning=has_cleaning, report_run=report_run_id,
                left_out=left_out))
            zf.writestr('report.md', _report_md(
                stem, history, last_output, interpretation, include_prompts))
            if report_md:
                zf.writestr('full_report.md', report_md)
        size = spool.tell()
        spool.seek(0)
        response = send_file(spool, mimetype='application/zip',
                             as_attachment=True,
                             download_name=f'Project_{stem}.zip')
    except BaseException:
        spool.close()
        raise
    response.content_length = size
    response.call_on_close(spool.close)
    return response


@bp.route('/reset', methods=['POST'])
def reset():
    """Clear everything stored for this session/user's workspace (4.6)."""
    storage.reset_identity()
    clear_caches()
    return jsonify({'status': 'success'})
