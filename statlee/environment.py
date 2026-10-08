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
runner image in docker mode). A failure (or an R probe that did not finish) is
cached for ``FAILURE_TTL`` seconds, so a broken sandbox is not re-probed on
every export; environment.txt then says the environment could not be captured,
and the export still succeeds. The probes never run under the module lock: one
caller runs them, concurrent callers wait once on its result. Raw sandbox output from a failed
probe is never written into the bundle (it can carry server paths).
"""
import logging
import shutil
import threading
import time
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

# A failed (or partly failed) capture is kept this long, so a broken or hung
# sandbox is probed at most once per window instead of once per export.
FAILURE_TTL = 300

# key -> (text, expires_at or None for "until restart")
_cache = {}
# key -> threading.Event set when the in-flight capture for that key finishes
_inflight = {}
# Guards _cache and _inflight only; never held while a probe runs.
_lock = threading.Lock()


def _now():
    return time.monotonic()


def _wait_timeout(cfg):
    """How long a concurrent caller waits for the in-flight capture: both
    probes at their full timeout, plus slack."""
    return 2 * max(int(getattr(cfg, 'exec_timeout', 60) or 60), 1) + 30


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


def _cached(key):
    """The cached text for ``key`` while it is fresh (call under _lock)."""
    hit = _cache.get(key)
    if hit and (hit[1] is None or hit[1] > _now()):
        return hit[0]
    return None


def _run_probes(cfg):
    """``(text, ttl)``: ttl None caches until restart, FAILURE_TTL when the
    Python probe or an attempted R probe did not finish."""
    python_out = _probe(cfg, PYTHON_PROBE, 'Python')
    if not python_out:
        return _failed_text(cfg), FAILURE_TTL
    r_tried = _r_available(cfg)
    r_out = _probe(cfg, R_PROBE, 'R') if r_tried else None
    ttl = FAILURE_TTL if (r_tried and not r_out) else None
    return _render(cfg, python_out, r_out, r_tried), ttl


def capture(cfg):
    """environment.txt text for the configured sandbox. Never raises.

    One caller per cache key runs the probes, outside the module lock; callers
    arriving meanwhile wait once on its result (or get the failure text when
    it outlasts ``_wait_timeout``). A full success is cached until restart, a
    failure or an R probe that did not finish for ``FAILURE_TTL`` seconds."""
    key = _cache_key(cfg)
    with _lock:
        text = _cached(key)
        if text is not None:
            return text
        event = _inflight.get(key)
        runner = event is None
        if runner:
            event = _inflight[key] = threading.Event()
    if not runner:
        if event.wait(_wait_timeout(cfg)):
            with _lock:
                text = _cached(key)
            if text is not None:
                return text
        return _failed_text(cfg)

    text, ttl = _failed_text(cfg), FAILURE_TTL
    try:
        text, ttl = _run_probes(cfg)
    except Exception:
        logger.exception("Environment capture crashed")
    finally:
        with _lock:
            _cache[key] = (text, None if ttl is None else _now() + ttl)
            _inflight.pop(key, None)
        event.set()
    return text
