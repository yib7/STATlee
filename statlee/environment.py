"""Analysis environment probe for the reproducibility bundle (SP4).

``environment.txt`` in the export says which interpreter and package versions
the analyses ran under. It is captured by fixed, server-authored probe scripts
that take the SAME path as any analysis script: ``codecheck.check_code`` first,
then ``sandbox.run_in_sandbox`` with the configured limits and sandbox mode. No
new execution path, no widened allowlist: the Python probe reads
``sys.version`` and each package's ``__version__`` (``importlib`` is refused by
codecheck, so ``importlib.metadata`` is not used), and every import is guarded
so a missing package reads "not installed". The R probe prints
``R.version.string`` and ``packageVersion()`` for every installed package.

The result is cached in process memory per boot and per sandbox mode (plus the
runner image in docker mode). A failed Python probe is not cached, so a later
export tries again; its environment.txt says the environment could not be
captured, and the export still succeeds. Raw sandbox output from a failed
probe is never written into the bundle (it can carry server paths).
"""
import logging
import shutil
import threading
from datetime import UTC, datetime

from . import codecheck, sandbox

logger = logging.getLogger('statlee.environment')

# The analysis stack on codecheck's import allowlist, in report order.
PYTHON_PACKAGES = (
    'pandas', 'numpy', 'scipy', 'statsmodels', 'matplotlib', 'seaborn',
    'sklearn', 'patsy', 'pingouin', 'lifelines', 'linearmodels',
    'factor_analyzer', 'semopy', 'scikit_posthocs', 'pyreadstat', 'openpyxl',
    'xlrd', 'tabulate',
)

# Probe output kept per section (the R list can be long).
SECTION_MAX = 64 * 1024

FAILED = 'The analysis environment could not be captured for this export'


def python_probe_source(packages=PYTHON_PACKAGES):
    """The Python probe for ``packages``: one guarded import per package."""
    lines = [
        '# STATlee environment probe (server-authored, read-only).',
        'import sys',
        'import warnings',
        '',
        "warnings.simplefilter('ignore')",
        "print('Python ' + ' '.join(sys.version.split()))",
        "print('Platform: ' + sys.platform)",
    ]
    for name in packages:
        lines += [
            'try:',
            f'    import {name} as _probe_pkg',
            f"    print('{name} ' + str(getattr(_probe_pkg, '__version__', "
            "'unknown')))",
            'except Exception:',
            f"    print('{name} not installed')",
        ]
    return '\n'.join(lines) + '\n'


PYTHON_PROBE = python_probe_source()

R_PROBE = '''# STATlee environment probe (server-authored, read-only).
cat(R.version.string, "\\n", sep = "")
pkgs <- sort(unique(rownames(installed.packages())))
for (p in pkgs) {
  v <- tryCatch(as.character(packageVersion(p)), error = function(e) "unknown")
  cat(p, " ", v, "\\n", sep = "")
}
'''

_cache = {}
_lock = threading.Lock()


def clear_cache():
    with _lock:
        _cache.clear()


def _cache_key(cfg):
    mode = getattr(cfg, 'sandbox_mode', 'subprocess')
    image = getattr(cfg, 'runner_image', '') if mode == 'docker' else ''
    return mode, image


def _probe(cfg, code, language):
    """Run one probe through codecheck + the sandbox. Returns its output, or
    None when it was blocked, failed or timed out."""
    blocked, reason = codecheck.check_code(code, language)
    if blocked:
        logger.error("Environment probe (%s) refused by codecheck: %s",
                     language, reason)
        return None
    try:
        result = sandbox.run_in_sandbox(
            code, language, timeout=cfg.exec_timeout,
            memory_mb=cfg.exec_memory_mb, output_limit=cfg.exec_output_limit,
            mode=cfg.sandbox_mode, runner_image=cfg.runner_image,
            work_root=cfg.sandbox_work_root)
    except Exception:
        logger.exception("Environment probe (%s) crashed", language)
        return None
    if not result.success:
        logger.warning("Environment probe (%s) failed (rc=%s, timed_out=%s)",
                       language, result.returncode, result.timed_out)
        return None
    return (result.output or '').strip()[:SECTION_MAX]


def _r_available(cfg):
    # docker mode runs R inside the runner image, so PATH here says nothing.
    return cfg.sandbox_mode == 'docker' or bool(shutil.which('Rscript'))


def _render(cfg, python_out, r_out, r_tried):
    when = datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')
    out = ['STATlee analysis environment',
           f'Captured {when} by running a fixed probe script in the analysis '
           f'sandbox ({cfg.sandbox_mode} mode), the same way your analyses '
           'run.', '', '[Python]', python_out, '', '[R]']
    if r_out:
        out.append(r_out)
    elif r_tried:
        out.append('R: not available in the sandbox (the probe did not '
                   'finish).')
    else:
        out.append('R: not available in the sandbox (Rscript is not '
                   'installed).')
    return '\n'.join(out) + '\n'


def _failed_text(cfg):
    return (f'{FAILED}: the probe script did not finish in the analysis '
            f'sandbox ({cfg.sandbox_mode} mode).\n'
            'The analysis files in this bundle are complete; install a recent '
            'Python with pandas, numpy, scipy, statsmodels and matplotlib to '
            'run them.\n')


def capture(cfg):
    """environment.txt text for the configured sandbox (cached per boot and
    per sandbox mode). Never raises."""
    key = _cache_key(cfg)
    with _lock:
        if key in _cache:
            return _cache[key]
        python_out = _probe(cfg, PYTHON_PROBE, 'Python')
        if not python_out:
            return _failed_text(cfg)
        r_tried = _r_available(cfg)
        r_out = _probe(cfg, R_PROBE, 'R') if r_tried else None
        text = _render(cfg, python_out, r_out, r_tried)
        _cache[key] = text
        return text
