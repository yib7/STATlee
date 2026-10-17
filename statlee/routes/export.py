"""Project export (5.3): the reproducibility bundle zip.

``POST /export`` packages the active dataset version, the original upload, the
replayable cleaning steps (cleaning.py), the analysis script, the codebook, the
selected workbook runs' outputs, environment.txt, provenance.json, README.md
and report.md. ``statlee/bundle.py`` builds the bundle's parts and
``statlee/environment.py`` captures environment.txt; this module picks what goes
in and streams the zip.
"""
import json
import os
import tempfile
import time
import zipfile

from flask import Blueprint, current_app, send_file
from werkzeug.utils import secure_filename

from .. import bundle, environment, storage
from .. import codebook as codebook_store
from ..extensions import limiter
from . import (
    FieldError,
    clamp,
    clamp_history,
    dataset_filename_field,
    language_field,
    register_field_errors,
    request_json,
    str_field,
)

bp = Blueprint('export', __name__)
register_field_errors(bp)


def _cfg():
    return current_app.config['STATLEE']


def _filename(data):
    """The dataset a request names (L8: never a reserved version-file name)."""
    return dataset_filename_field(data)


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
