"""Reproducibility bundle v2 (SP4): the pieces of the project export that
document what produced it.

``/export`` (routes/datasets.py) assembles the zip; this module owns the parts
that are not plain file copies:

* which workbook runs go in (``runs=all|starred|latest``, capped at
  ``MAX_RUNS_PER_IDENTITY``) and their ``outputs/run_NNNN/`` folders;
* ``provenance.json``: ``{generated_at, app_version, llm_provider, steps}``,
  one step per upload, cleaning step (the lineage of the shipped data), run and
  interpretation. Only what was recorded is written (unknown fields are
  omitted, never invented); ``prompt`` is omitted everywhere when the export
  is made with ``include_prompts=false``. Data values and system prompts are
  never part of it;
* ``README.md``: how to run the bundle, what is in it, how it was produced.

Every file copied into the zip is resolved under the caller's identity root;
anything outside it (or gone by the time it is read) is skipped.
"""
import functools
import hashlib
import json
import logging
import os
import re
import time
import tomllib
import zipfile
from datetime import UTC, datetime

from . import datatools, storage
from . import methods as methods_card

logger = logging.getLogger('statlee.bundle')

RUN_SELECTIONS = ('all', 'starred', 'latest')

_SHA_RE = re.compile(r'[0-9a-f]{64}')
_KIND_ORDER = {'upload': 0, 'wrangle': 1, 'run': 2, 'interpret': 3}
_PYPROJECT = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'pyproject.toml')


@functools.lru_cache(maxsize=1)
def app_version():
    """The app version from pyproject.toml (the source of truth in a checkout
    and in the Docker image), else the installed distribution, else
    'unknown'."""
    try:
        with open(_PYPROJECT, 'rb') as f:
            version = tomllib.load(f)['project']['version']
        if isinstance(version, str) and version:
            return version
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        pass
    try:
        from importlib.metadata import version as dist_version
        return dist_version('statlee')
    except Exception:
        return 'unknown'


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _ts_of(value):
    """Epoch seconds of an ISO-8601 string, or None."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _sha_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def within(path, root):
    """True when ``path`` (symlinks resolved) is inside ``root``."""
    if not path:
        return False
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(root)
        return os.path.commonpath([real, base]) == base and real != base
    except (OSError, ValueError):
        return False


def file_sha(path, root):
    if not within(path, root):
        return None
    try:
        return datatools.file_sha256(path)
    except OSError:
        return None


# Already-compressed formats are stored as-is (deflating a PNG costs CPU and
# saves nothing).
_STORED_SUFFIXES = ('.png',)


def write_file(zf, path, arcname, root):
    """Copy ``path`` into the zip when it is inside ``root`` and still
    readable. Returns True when written. ``zf.write`` copies the file in
    chunks; where the archive itself lives (a spooled temp file) is up to the
    caller."""
    if not within(path, root):
        logger.warning("Export skipped a path outside the identity root")
        return False
    compress = (zipfile.ZIP_STORED if arcname.lower().endswith(_STORED_SUFFIXES)
                else None)
    try:
        zf.write(path, arcname=arcname, compress_type=compress)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def select_run_ids(selection, cap, dataset_name):
    """The caller's run ids for ``selection``, oldest first, at most ``cap``
    (the newest are kept). Only runs on the exported dataset count: a run's
    meta ``filename`` is the stored dataset name (``dataset_name``, the
    basename the sandbox staged); with no dataset, only runs that recorded
    none."""
    metas = []
    for run_id in storage.run_ids():
        meta = storage.run_meta(run_id)
        if meta is None:
            continue
        if (meta.get('filename') or None) != (dataset_name or None):
            continue
        metas.append((run_id, meta))
    if selection == 'latest':
        metas = metas[-1:]
    elif selection == 'starred':
        metas = [(r, m) for r, m in metas if m.get('starred')]
    cap = cap if isinstance(cap, int) and cap >= 1 else storage.RUNS_MAX
    return [r for r, _m in metas][-cap:]


def load_runs(run_ids):
    """``[(run_id, record)]`` for the runs still on disk (a run evicted or
    deleted since it was listed is skipped)."""
    out = []
    for run_id in run_ids:
        record = storage.load_run(run_id)
        if record:
            out.append((run_id, record))
    return out


def run_dir_name(run_id):
    return 'run_' + run_id[1:]


def latest_scripted_run(records):
    """``(run_id, record)`` of the newest included run that kept its script,
    or None. Its script is the bundle's analysis.py|analysis.R, so that file's
    hash matches a provenance step."""
    for run_id, record in reversed(records):
        script = record.get('script')
        if script and isinstance(script.get('code'), str) and script['code']:
            return run_id, record
    return None


def _script_name(language):
    return 'analysis.R' if str(language or '').lower() == 'r' else 'analysis.py'


def write_run_outputs(zf, records, root):
    """``outputs/run_NNNN/`` per run: output.txt, plot_N.png, methods.json,
    interpretation.md (when there is one) and the run's own script. Returns
    the folder names written."""
    written = []
    for run_id, record in records:
        base = f'outputs/{run_dir_name(run_id)}/'
        output = record.get('output') or ''
        zf.writestr(base + 'output.txt', output)
        for path in record.get('plots') or []:
            write_file(zf, path, base + os.path.basename(path), root)
        card = methods_card.for_run(record.get('meta') or {}, output)
        zf.writestr(base + 'methods.json', json.dumps(card, indent=2,
                                                      ensure_ascii=False))
        interpretation = record.get('interpretation') or ''
        if interpretation.strip():
            zf.writestr(base + 'interpretation.md', interpretation)
        script = record.get('script')
        if script and script.get('code'):
            zf.writestr(base + _script_name(script.get('language')),
                        script['code'])
        written.append(run_dir_name(run_id))
    return written


# ---------------------------------------------------------------------------
# provenance.json
# ---------------------------------------------------------------------------

def _str(value, limit=4000):
    return value[:limit] if isinstance(value, str) and value else None


def _lineage_steps(lineage, include_prompts, root):
    """Upload + the cleaning steps that produced the shipped data."""
    versions = lineage.get('versions') or []
    if not versions:
        return []
    paths = {}
    for entry in versions:
        path = storage.resolve_path(entry.get('file')) if entry.get('file') \
            else None
        paths[entry.get('v')] = path if path and os.path.exists(path) else None
    shas = {}

    def sha(v):
        if v not in shas:
            shas[v] = file_sha(paths.get(v), root) if paths.get(v) else None
        return shas[v]

    out = []
    first = versions[0]
    upload = {'kind': 'upload'}
    if isinstance(first.get('ts'), (int, float)):
        upload['ts'] = iso(first['ts'])
    if sha(first.get('v')):
        upload['dataset_sha256_after'] = sha(first.get('v'))
    out.append((first.get('ts') or 0, upload))

    order = [e.get('v') for e in versions]
    for entry in lineage.get('steps') or []:
        step = {'kind': 'wrangle'}
        if isinstance(entry.get('ts'), (int, float)):
            step['ts'] = iso(entry['ts'])
        if include_prompts and _str(entry.get('instruction')):
            step['prompt'] = _str(entry.get('instruction'))
        for key in ('model', 'role'):
            if _str(entry.get(key), 64):
                step[key] = _str(entry.get(key), 64)
        idx = order.index(entry.get('v')) if entry.get('v') in order else 0
        if idx > 0 and sha(order[idx - 1]):
            step['dataset_sha256_before'] = sha(order[idx - 1])
        if sha(entry.get('v')):
            step['dataset_sha256_after'] = sha(entry.get('v'))
        if isinstance(entry.get('code'), str) and entry['code'].strip():
            step['script_sha256'] = _sha_text(entry['code'])
        out.append((entry.get('ts') or 0, step))
    return out


def _run_steps(run_id, record, include_prompts):
    meta = record.get('meta') or {}
    created = _ts_of(meta.get('created_at'))
    step = {'kind': 'run'}
    if created is not None:
        step['ts'] = iso(created)
    if include_prompts and _str(meta.get('prompt')):
        step['prompt'] = _str(meta.get('prompt'))
        step['prompt_inherited'] = bool(meta.get('prompt_inherited'))
    for key in ('model', 'role'):
        if _str(meta.get(key), 64):
            step[key] = _str(meta.get(key), 64)
    if isinstance(meta.get('dataset_sha256'), str) and \
            _SHA_RE.fullmatch(meta['dataset_sha256']):
        step['dataset_sha256_before'] = meta['dataset_sha256']
    script = record.get('script')
    if script and isinstance(script.get('code'), str):
        step['script_sha256'] = _sha_text(script['code'])
    step['run_id'] = run_id
    out = [(created or 0, step)]

    if (record.get('interpretation') or '').strip():
        interp = {'kind': 'interpret'}
        written = storage.interpretation_time(run_id)
        if written is not None:
            interp['ts'] = iso(written)
        interp['run_id'] = run_id
        out.append((written if written is not None else (created or 0),
                    interp))
    return out


def provenance(cfg, lineage, records, include_prompts, root):
    """The provenance.json document."""
    steps = []
    if lineage:
        steps += _lineage_steps(lineage, include_prompts, root)
    for run_id, record in records:
        steps += _run_steps(run_id, record, include_prompts)
    steps.sort(key=lambda item: (item[0], _KIND_ORDER[item[1]['kind']]))
    return {
        'generated_at': iso(time.time()),
        'app_version': app_version(),
        'llm_provider': getattr(cfg, 'llm_provider', None) or 'unknown',
        'steps': [step for _ts, step in steps],
    }


# ---------------------------------------------------------------------------
# README.md
# ---------------------------------------------------------------------------

_RUNS_LABEL = {'all': 'all runs in the workbook',
               'starred': 'starred runs only',
               'latest': 'the latest run only'}


def readme(stem, *, script_name, script_source, editor_differs, data_name,
           lineage, run_dirs, runs_selection, include_prompts, has_codebook,
           has_report, provider, generated, script_run=None,
           has_original=True, has_cleaning=False):
    """README.md for the bundle (plain Markdown, no em dashes). Only files
    that were actually written are listed."""
    replay = bool(has_cleaning)
    out = [f'# STATlee project export: {stem}', '',
           f'Exported {generated} with STATlee {app_version()} '
           f'(LLM provider: {provider}).', '', '## How to run', '']
    if script_name and data_name:
        runner = (f'python {script_name}' if script_name.endswith('.py')
                  else f'Rscript {script_name}')
        steps = ['Unzip this archive and open a terminal inside the '
                 'unzipped folder.',
                 'Install the packages the script uses. environment.txt '
                 'lists the versions STATlee ran it with.']
        if replay:
            steps.append(f'Optional: `python cleaning.py` rebuilds {data_name} '
                         f'from data/original_{data_name} by replaying your '
                         'cleaning steps. The folder already holds the result, '
                         'so you can skip this.')
        steps.append(f'Run: `{runner}`')
        out += [f'{n}. {text}' for n, text in enumerate(steps, start=1)]
        out += ['', f'The script reads {data_name}, which sits next to it in '
                'this folder.']
    elif script_name:
        out.append(f'Run {script_name} from this folder.')
    else:
        out.append('No analysis script was generated or run in this session, '
                   'so none is included.')

    out += ['', '## Contents', '']
    if script_name and script_run:
        out.append(f'- {script_name}: the script of run {script_run}, the '
                   'latest run in this bundle (its outputs are in '
                   f'outputs/{run_dir_name(script_run)}/).')
    elif script_name:
        out.append(f'- {script_name}: the analysis script ({script_source}).')
    if data_name:
        if lineage and lineage['active_v'] != 1:
            out.append(f'- {data_name}: the dataset version the analysis used '
                       f'(version {lineage["active_v"]}).')
        else:
            out.append(f'- {data_name}: the dataset the analysis used.')
        if has_original:
            out.append(f'- data/original_{data_name}: the dataset as first '
                       'uploaded (after conversion to CSV).')
        if lineage and lineage['steps']:
            if replay:
                out.append(f'- cleaning.py: replays the {len(lineage["steps"])} '
                           f'cleaning step(s) that turn data/original_{data_name} '
                           f'into {data_name}. Run: `python cleaning.py`')
            elif not lineage['replayable']:
                out.append('- cleaning.py is not included: some cleaning steps '
                           'were made before STATlee recorded their code.')
    if has_codebook:
        out.append('- codebook.json: variable types, labels, value labels, '
                   'missing codes and roles. STATlee can import it again.')
    out.append('- environment.txt: the Python (and R) version and package '
               'versions of the sandbox the analyses ran in.')
    out.append('- provenance.json: every step that produced these files '
               '(upload, cleaning steps, runs, interpretations) with times, '
               'models and SHA-256 hashes of the data and scripts.')
    if run_dirs:
        out.append(f'- outputs/: one folder per run ({_RUNS_LABEL[runs_selection]}, '
                   f'{len(run_dirs)} in total), each with output.txt, plots, '
                   'methods.json (the methods and assumptions card), '
                   'interpretation.md when there is one, and the script that '
                   'run executed:')
        out += [f'  - outputs/{name}/' for name in run_dirs]
    else:
        out.append(f'- outputs/: no runs included ({_RUNS_LABEL[runs_selection]} '
                   'was selected and there are none).')
    which = (f'run {script_run}' if script_run else 'the latest run')
    if include_prompts:
        out.append(f'- report.md: your requests, the terminal output and the '
                   f'interpretation of {which}.')
    else:
        out.append(f'- report.md: the terminal output and the interpretation '
                   f'of {which}.')
    if has_report:
        out.append('- full_report.md: the written report.')

    out += ['', '## How this was produced', '',
            f'- STATlee version: {app_version()}',
            f'- LLM provider: {provider}',
            f'- Exported: {generated}',
            f'- Runs included: {_RUNS_LABEL[runs_selection]}']
    if include_prompts:
        out.append('- Prompts: the questions and cleaning instructions you '
                   'typed are included in provenance.json and report.md.')
    else:
        out.append('- Prompts: left out at export. The questions and cleaning '
                   'instructions you typed are not in this bundle; text '
                   'written by the model (interpretations, reports, script '
                   'comments) may still restate them.')
    if editor_differs and script_run:
        out += ['', '## Note', '',
                'The code in your editor at export time differs from the '
                f'script of run {script_run}, so it is not bundled; '
                f'{script_name} holds that run\'s script instead.']
    elif editor_differs:
        out += ['', '## Note', '',
                'The code in your editor at export time was not reviewed or '
                f'run in this session, so it is not bundled; {script_name} '
                f'holds the {script_source} instead.']
    return '\n'.join(out) + '\n'
