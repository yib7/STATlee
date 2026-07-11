"""Dataset lifecycle routes: upload, paging, codebook, suggestions,
wrangling/version control, export, reset.

Implements roadmap items 5.1 (formats), 5.6 (cache), 5.8 (paging cache +
row cap), 5.13 (survey→codebook), 5.16 (wrangle + undo/redo), 5.3 (export),
4.5 (suggestion reroll), 4.6 (reset).
"""
import io
import json
import logging
import os
import re
import textwrap
import threading
import time
import zipfile
from collections import OrderedDict

import pandas as pd
from flask import Blueprint, current_app, jsonify, request, send_file
from werkzeug.utils import secure_filename

from .. import datatools, llm, prompts, sandbox, storage
from ..extensions import limiter
from ..usage import usage_breakdown
from . import json_error, moderation_blocked

logger = logging.getLogger('statlee.datasets')

bp = Blueprint('datasets', __name__)


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


def _quota_error(incoming_bytes):
    """P2-4: return a 413 ``json_error`` if accepting ``incoming_bytes`` more
    would push the caller's identity dir past its file-count or byte quota,
    else ``None``. Uses a cheap directory scan BEFORE the new file is saved so
    a burst of uploads cannot fill the disk ahead of the 2h TTL cleanup."""
    cfg = _cfg()
    count, used = storage.identity_usage()
    if cfg.max_datasets_per_identity and count >= cfg.max_datasets_per_identity:
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
_cache_lock = threading.Lock()
ANALYSIS_CACHE_MAX = 128
FRAME_CACHE_MAX = 4


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


def _read_active(filename, nrows=None):
    """Read the ACTIVE version of a dataset (5.16-aware)."""
    path = storage.active_dataset_path(filename)
    if not path or not os.path.exists(path):
        return None, None
    if nrows is not None:
        return pd.read_csv(path, nrows=nrows), path
    mtime = os.stat(path).st_mtime
    cached = _cache_get(_frame_cache, (path, mtime))
    if cached is not None:
        return cached, path
    df = pd.read_csv(path)
    _cache_put(_frame_cache, (path, mtime), df, FRAME_CACHE_MAX)
    return df, path


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

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in datatools.SUPPORTED_EXTENSIONS:
        return json_error(
            'Invalid file format. Supported: '
            + ', '.join(datatools.SUPPORTED_EXTENSIONS))

    filename = secure_filename(file.filename)
    # P2-3: version artifacts are named "{stem}__vN.csv" and share this flat
    # identity dir, so a file whose stem already ends in a __vN suffix could
    # later be overwritten by a wrangle of another dataset. Reserve that suffix
    # at upload time rather than silently corrupting the second file.
    if re.search(r'__v\d+$', os.path.splitext(filename)[0]):
        return json_error(
            'Filenames ending in a version suffix like "__v2" are reserved for '
            'dataset history. Please rename the file and upload again.')
    filepath = storage.resolve_path(filename)
    if not filepath:
        return json_error('Invalid filename')

    quota = _quota_error(_incoming_size(file))
    if quota is not None:
        return quota
    file.save(filepath)

    try:
        csv_path, labels = datatools.normalize_to_csv(filepath)
    except datatools.MissingDependencyError as e:
        try:
            os.remove(filepath)
        except OSError:
            pass
        return json_error(str(e), 422)
    except Exception:
        logger.exception("Failed to parse uploaded dataset")
        try:
            os.remove(filepath)
        except OSError:
            pass
        return json_error('Could not read that file. Is it a valid dataset?', 422)

    csv_name = os.path.basename(csv_path)
    try:
        df = pd.read_csv(csv_path, nrows=100)
        profile = datatools.profile_dataframe(df)
    except Exception:
        logger.exception("Failed to profile dataset")
        return json_error('Could not read the dataset after conversion.', 500)

    sha256 = datatools.file_sha256(csv_path)
    storage.register_dataset(csv_name)
    storage.save_dataset_meta(csv_name, {
        'sha256': sha256,
        'original_name': file.filename,
        'labels': labels,
    })

    return jsonify({
        'message': 'File uploaded',
        'filename': csv_name,
        'profile': profile,
        'labels': labels,        # native .sav/.dta labels seed the codebook
        'sha256': sha256,
        'changelog': storage.dataset_changelog(csv_name),  # seeds the cleaning panel
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

    lower = file.filename.lower()
    if not (lower.endswith('.pdf') or lower.endswith('.txt')):
        return json_error('Invalid file format. Please upload a PDF or TXT.')

    filename = secure_filename(file.filename)
    filepath = storage.resolve_path(filename)
    if not filepath:
        return json_error('Invalid filename')

    incoming = _incoming_size(file)
    quota = _quota_error(incoming)
    if quota is not None:
        return quota

    # P2-5: a .txt is rendered to PDF page-by-page through fpdf (pure Python,
    # slow) and only THEN measured against pdf_max_pages, so a 16 MB text file
    # could hold a worker in that conversion before being rejected. Measure the
    # raw bytes first. The budget (4096 bytes per allowed page) sits well above
    # real text density (~3 KB per dense rendered page), so nothing short enough
    # to pass the page cap is rejected here -- it only short-circuits the
    # pathological oversized case before any conversion work happens.
    if filename.lower().endswith('.txt'):
        max_txt_bytes = max_pages * 4096
        if incoming > max_txt_bytes:
            return json_error(
                f'Text file is too large ({incoming // 1024} KB). The '
                f'{max_pages}-page documentation limit allows roughly '
                f'{max_txt_bytes // 1024} KB of text. Please upload a shorter '
                'document.')

    file.save(filepath)

    from pypdf import PdfReader

    if filename.lower().endswith('.pdf'):
        try:
            with open(filepath, 'rb') as f:
                page_count = len(PdfReader(f).pages)
        except Exception:
            os.remove(filepath)
            return json_error('Failed to read PDF.', 422)
        if page_count > max_pages:
            os.remove(filepath)
            return json_error(
                f'PDF is {page_count} pages, which exceeds the {max_pages}-page '
                'limit. Please upload a shorter document.')

    if filename.lower().endswith('.txt'):
        pdf_filename = filename.rsplit('.', 1)[0] + '.pdf'
        pdf_filepath = storage.resolve_path(pdf_filename)
        try:
            from fpdf import FPDF
            pdf = FPDF()
            pdf.set_auto_page_break(auto=True, margin=15)
            pdf.add_page()
            pdf.set_font('Helvetica', size=11)
            with open(filepath, encoding='utf-8', errors='replace') as f:
                for line in f:
                    clean = line.encode('latin-1', 'ignore').decode('latin-1')
                    pdf.multi_cell(0, 6, text=clean)
            pdf.output(pdf_filepath)
            os.remove(filepath)
            with open(pdf_filepath, 'rb') as f:
                page_count = len(PdfReader(f).pages)
            if page_count > max_pages:
                os.remove(pdf_filepath)
                return json_error(
                    f'TXT file converts to {page_count} pages, which exceeds '
                    f'the {max_pages}-page limit. Please upload a shorter document.')
            filename = pdf_filename
        except Exception:
            logger.exception("TXT→PDF conversion failed")
            return json_error('Failed to convert TXT to PDF.', 500)

    return jsonify({'message': 'Documentation uploaded', 'filename': filename}), 200


@bp.route('/extract_pdf_codebook', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def extract_pdf_codebook():
    """Map variable labels from an uploaded PDF.

    mode='codebook' (default): formal codebook extraction.
    mode='survey' (5.13): infer a codebook from the questionnaire itself.
    """
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
    mode = data.get('mode', 'codebook')
    if not filename:
        return json_error('Missing filename')
    filepath = storage.resolve_path(filename)
    if not filepath or not os.path.exists(filepath):
        return json_error('Invalid filename')

    try:
        with open(filepath, 'rb') as f:
            pdf_bytes = f.read()
        if mode == 'survey':
            prompt = prompts.survey_extract(data.get('headers') or [])
        else:
            prompt = prompts.pdf_extract()

        result = llm.get_service().generate(
            'flash',
            [llm.MediaPart(data=pdf_bytes, mime_type='application/pdf'), prompt],
            temperature=0.1, json_mode=True)
        mapping = json.loads(result.text)
        if not isinstance(mapping, dict):
            raise ValueError('mapping is not an object')
        return jsonify({'status': 'success', 'mapping': mapping,
                        'usage': usage_breakdown(result.usage)})
    except Exception:
        logger.exception("PDF extraction error")
        return json_error('Failed to extract definitions from the document.', 500)


# ---------------------------------------------------------------------------
# Data viewer
# ---------------------------------------------------------------------------

@bp.route('/data_page', methods=['POST'])
def data_page():
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
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
    try:
        df, path = _read_active(filename)
    except Exception:
        logger.exception("data_page read failure")
        return json_error('Could not read dataset.', 500)
    if df is None:
        return json_error('Invalid filename')

    if len(df) > _cfg().data_page_row_cap:
        return json_error(
            f'This dataset has {len(df):,} rows, beyond the viewer limit of '
            f'{_cfg().data_page_row_cap:,}. Analysis still works; the table '
            'view is disabled for files this large.', 413)

    try:
        for col, term in filters.items():
            if term and col in df.columns:
                # regex=False (P1-3): filter terms are literal text, so users
                # can type '(' or '$' and a hostile pattern like '(a+)+$'
                # cannot pin a worker thread with catastrophic backtracking.
                df = df[df[col].astype(str).str.contains(
                    str(term), case=False, na=False, regex=False)]

        total_rows = len(df)
        total_pages = max(1, -(-total_rows // per_page)) if total_rows else 0
        page = max(1, min(page, total_pages) if total_pages else 1)
        start = (page - 1) * per_page
        df_page = df.iloc[start:start + per_page].fillna('')
        return jsonify({
            'status': 'success',
            'data': df_page.to_dict(orient='records'),
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

@bp.route('/classify_variables', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def classify_variables():
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
    if not filename:
        return json_error('Missing filename')
    try:
        df, path = _read_active(filename, nrows=100)
    except Exception:
        logger.exception("classify read failure")
        return json_error('Could not read dataset.', 500)
    if df is None:
        return json_error('Invalid filename')

    sha = datatools.file_sha256(path)
    cached = _cache_get(_analysis_cache, (sha, 'classify'))
    if cached is not None:
        return jsonify({'status': 'success', 'codebook': cached, 'cached': True})

    try:
        preview = datatools.metadata_json(df)
        result = llm.get_service().generate(
            'flash', prompts.classify(preview), temperature=0.1, json_mode=True)
        codebook = json.loads(result.text)
        _cache_put(_analysis_cache, (sha, 'classify'), codebook, ANALYSIS_CACHE_MAX)
        return jsonify({'status': 'success', 'codebook': codebook,
                        'usage': usage_breakdown(result.usage)})
    except Exception:
        logger.exception("Classification error")
        return json_error('Variable classification failed.', 500)


@bp.route('/suggest', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def suggest_analysis():
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
    codebook = data.get('codebook', {}) or {}
    pdf_mapping = data.get('pdf_mapping', {}) or {}
    previous = data.get('previous') or []   # 4.5 reroll: avoid repeats

    if not filename:
        return json_error('Missing filename')
    try:
        df, path = _read_active(filename, nrows=100)
    except Exception:
        logger.exception("suggest read failure")
        return json_error('Could not read dataset.', 500)
    if df is None:
        return json_error('Invalid filename')

    sha = datatools.file_sha256(path)
    cache_key = (sha, 'suggest', json.dumps(sorted(codebook.items()))[:512])
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
        suggestions = json.loads(result.text)
        if not previous:
            _cache_put(_analysis_cache, cache_key, suggestions, ANALYSIS_CACHE_MAX)
        return jsonify({'status': 'success', 'suggestions': suggestions,
                        'usage': usage_breakdown(result.usage)})
    except Exception:
        logger.exception("Suggestion error")
        return json_error('Could not generate suggestions.', 500)


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
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
    instruction = (data.get('instruction') or '').strip()
    if not filename or not instruction:
        return json_error('Missing filename or instruction')

    service = llm.get_service()
    cfg = _cfg()
    try:
        mod = service.generate('lite', prompts.moderation(instruction),
                               temperature=0.0, json_mode=True)
    except Exception:
        logger.exception("Wrangle moderation failed")
        return json_error('Moderation service failed.', 503)
    blocked, reason = moderation_blocked(mod.text)
    if blocked:
        return json_error(f'Request denied. {reason}', 403)

    try:
        df, active_path = _read_active(filename, nrows=100)
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
        plan = json.loads(result.text)
    except Exception:
        logger.exception("Wrangle plan generation failed")
        return json_error('Could not generate the transformation.', 500)

    if plan.get('error') or not plan.get('code'):
        return json_error(plan.get('error')
                          or 'Could not translate that instruction.', 422)

    # Run-guard (0.4): the LLM-generated transform code is itself re-moderated
    # before execution, exactly as /run does for user-edited scripts. Moderating
    # only the user instruction (above) is not enough — a jailbroken or
    # misbehaving model can emit code doing network access, env exfiltration, or
    # shell execution regardless of a benign-looking instruction. Default-deny:
    # a malformed/ambiguous verdict blocks (see moderation_blocked()).
    try:
        code_verdict = service.generate(
            'lite', prompts.code_moderation(plan['code'], 'Python'),
            temperature=0.0, json_mode=True)
    except Exception:
        logger.exception("Wrangle code-moderation failed")
        return json_error('Could not verify the generated transform.', 503)
    blocked, reason = moderation_blocked(code_verdict.text)
    if blocked:
        return json_error(
            f'The generated transform was rejected by the safety check. {reason}',
            403)

    # Record the moderated transform in the approved-script store so /wrangle
    # upholds the same "every executed LLM-generated script is moderated and
    # recorded" invariant as the /chat -> /run path (0.4 run-guard).
    storage.save_approved_script(plan['code'], 'Python')

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
        storage.add_dataset_version(
            filename, new_df, instruction, summary=plan.get('summary'))
        clear_caches()
        return jsonify({
            'status': 'success',
            'summary': plan.get('summary') or instruction,
            'profile': datatools.profile_dataframe(new_df),
            'changelog': storage.dataset_changelog(filename),
            'usage': usage_breakdown(mod.usage, result.usage,
                                     code_verdict.usage),
        })
    except Exception:
        logger.exception("Failed to persist wrangled version")
        return json_error('Could not save the transformed dataset.', 500)


@bp.route('/dataset_versions', methods=['POST'])
def dataset_versions():
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
    if not filename:
        return json_error('Missing filename')
    changelog = storage.dataset_changelog(filename)
    if changelog is None:
        return json_error('Unknown dataset.', 404)
    return jsonify({'status': 'success', 'changelog': changelog})


@bp.route('/version_control', methods=['POST'])
def version_control():
    """Undo/redo by moving the active-version pointer (5.16)."""
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
    direction = data.get('direction')
    if not filename or direction not in ('undo', 'redo'):
        return json_error("Missing filename or direction ('undo'|'redo')")
    manifest = storage.shift_dataset_version(filename, direction)
    if manifest is None:
        return json_error('Unknown dataset.', 404)
    clear_caches()
    try:
        df, _ = _read_active(filename, nrows=100)
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
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
    if not filename:
        return json_error('Missing filename')
    manifest = storage.revert_to_original(filename)
    if manifest is None:
        return json_error('Unknown dataset.', 404)
    clear_caches()
    try:
        df, _ = _read_active(filename, nrows=100)
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

@bp.route('/export', methods=['POST'])
def export_project():
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
    language = data.get('language', 'Python')
    history = data.get('history') or []
    interpretation = data.get('interpretation') or ''
    report_md = data.get('report') or ''

    approved = storage.load_approved_script()
    code = (approved or {}).get('code') or data.get('code') or ''
    last_output, plot_paths = storage.last_run_artifacts()

    buf = io.BytesIO()
    stem = os.path.splitext(secure_filename(filename or 'project'))[0] or 'project'
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        if filename:
            active = storage.active_dataset_path(filename)
            if active and os.path.exists(active):
                zf.write(active, arcname=f'data/{os.path.basename(active)}')
        if code:
            ext = '.py' if language.lower() == 'python' else '.R'
            zf.writestr(f'script{ext}', code)
        for p in plot_paths:
            zf.write(p, arcname=f'plots/{os.path.basename(p)}')

        lines = [f'# Project: {stem}', '',
                 f'_Exported from STATlee on {time.strftime("%Y-%m-%d %H:%M")}_', '']
        if history:
            lines.append('## Analysis requests')
            lines.extend(f"- {msg.get('text', '')}" for msg in history
                         if isinstance(msg, dict) and (msg.get('role') or '') == 'user')
            lines.append('')
        if last_output:
            lines += ['## Terminal output', '```', last_output, '```', '']
        if interpretation:
            lines += ['## AI interpretation', interpretation, '']
        zf.writestr('report.md', '\n'.join(lines))

        if report_md:
            zf.writestr('full_report.md', report_md)

    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f'Project_{stem}.zip')


@bp.route('/reset', methods=['POST'])
def reset():
    """Clear everything stored for this session/user's workspace (4.6)."""
    storage.reset_identity()
    clear_caches()
    return jsonify({'status': 'success'})
