"""SP4 environment probe: a fixed, server-authored script that reports the
analysis environment. It goes through codecheck and the sandbox like any
analysis script (no new exec path), and its result is cached in process
memory per boot and per sandbox mode."""
import shutil

import pytest

from statlee import codecheck, environment, sandbox


@pytest.fixture(autouse=True)
def _fresh_cache():
    environment.clear_cache()
    yield
    environment.clear_cache()


class _Result:
    def __init__(self, output='', returncode=0, timed_out=False):
        self.output = output
        self.returncode = returncode
        self.timed_out = timed_out
        self.plots = []
        self.files = {}

    @property
    def success(self):
        return self.returncode == 0 and not self.timed_out


def test_python_probe_passes_codecheck_unchanged():
    assert codecheck.check_code(environment.PYTHON_PROBE, 'Python') == \
        (False, '')


def test_r_probe_passes_the_r_static_scan():
    assert codecheck.check_code(environment.R_PROBE, 'R') == (False, '')


def test_probe_never_uses_importlib():
    assert 'importlib' not in environment.PYTHON_PROBE
    assert '__import__' not in environment.PYTHON_PROBE


def test_probed_packages_are_all_on_the_import_allowlist():
    assert environment.PYTHON_PACKAGES
    for name in environment.PYTHON_PACKAGES:
        assert name in codecheck.ALLOWED_PYTHON_MODULES, name
        assert f'import {name} as' in environment.PYTHON_PROBE


def test_real_python_probe_reports_versions(app):
    import numpy
    import pandas
    with app.app_context():
        text = environment.capture(app.config['STATLEE'])
    assert 'Python 3.' in text
    assert f'pandas {pandas.__version__}' in text
    assert f'numpy {numpy.__version__}' in text
    assert 'could not be captured' not in text


def test_a_missing_package_reads_not_installed():
    # The guard around each import: a package that is not importable prints
    # "not installed" instead of failing the probe.
    source = environment.python_probe_source(('pandas', 'statlee_no_such_pkg'))
    result = sandbox.run_in_sandbox(source, 'Python', timeout=60)
    assert result.success, result.output
    assert 'statlee_no_such_pkg not installed' in result.output
    assert 'pandas ' in result.output


@pytest.mark.skipif(not shutil.which('Rscript'), reason='Rscript not on PATH')
def test_real_r_probe_reports_the_r_version(app):
    with app.app_context():
        text = environment.capture(app.config['STATLEE'])
    assert 'R version 4.' in text
    assert 'stats ' in text


def test_probe_runs_through_codecheck_and_the_sandbox(app, monkeypatch):
    calls = []
    checked = []
    real_check = codecheck.check_code

    def fake_run(code, language='Python', **kwargs):
        calls.append((language, kwargs.get('mode')))
        if language == 'Python':
            return _Result('Python 3.12.1\nPlatform: linux\npandas 2.2.0\n')
        return _Result('R version 4.4.0 (2024-04-24)\nstats 4.4.0\n')

    def spy_check(code, language='Python'):
        checked.append(language)
        return real_check(code, language)

    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox', fake_run)
    monkeypatch.setattr(environment.codecheck, 'check_code', spy_check)
    monkeypatch.setattr(environment.shutil, 'which', lambda name: 'Rscript')
    with app.app_context():
        text = environment.capture(app.config['STATLEE'])
        again = environment.capture(app.config['STATLEE'])
    assert text == again
    assert [c[0] for c in calls] == ['Python', 'R']        # once per boot
    assert checked == ['Python', 'R']
    assert 'pandas 2.2.0' in text and 'R version 4.4.0' in text


def test_cache_is_per_sandbox_mode(app, monkeypatch):
    calls = []

    def fake_run(code, language='Python', **kwargs):
        calls.append(kwargs.get('mode'))
        return _Result(f"Python 3.12.1 mode={kwargs.get('mode')}\n")

    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox', fake_run)
    monkeypatch.setattr(environment.shutil, 'which', lambda name: None)
    cfg = app.config['STATLEE']
    with app.app_context():
        environment.capture(cfg)
        monkeypatch.setattr(cfg, 'sandbox_mode', 'docker')
        docker_text = environment.capture(cfg)
        environment.capture(cfg)
    assert calls[0] == 'subprocess'
    assert 'docker' in calls[1:]
    assert 'mode=docker' in docker_text
    # R in docker mode is attempted in the runner image, not looked up on PATH.
    assert calls.count('subprocess') == 1


def test_r_missing_is_reported_not_a_failure(app, monkeypatch):
    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox',
                        lambda code, language='Python', **k:
                        _Result('Python 3.12.1\npandas 2.2.0\n'))
    monkeypatch.setattr(environment.shutil, 'which', lambda name: None)
    with app.app_context():
        text = environment.capture(app.config['STATLEE'])
    assert 'pandas 2.2.0' in text
    assert 'R: not available' in text
    assert 'could not be captured' not in text


def test_python_probe_failure_is_stated_and_retried_after_the_ttl(
        app, monkeypatch):
    results = [_Result('boom C:\\secret\\path', returncode=1),
               _Result('Python 3.12.1\npandas 2.2.0\n')]
    now = [50.0]
    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox',
                        lambda code, language='Python', **k: results.pop(0))
    monkeypatch.setattr(environment.shutil, 'which', lambda name: None)
    monkeypatch.setattr(environment, '_now', lambda: now[0])
    with app.app_context():
        first = environment.capture(app.config['STATLEE'])
        now[0] += environment.FAILURE_TTL + 1
        second = environment.capture(app.config['STATLEE'])
    assert 'could not be captured' in first
    assert 'secret' not in first
    assert 'pandas 2.2.0' in second


def test_a_blocked_probe_is_never_executed(app, monkeypatch):
    ran = []
    monkeypatch.setattr(environment.codecheck, 'check_code',
                        lambda code, language='Python': (True, 'nope'))
    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox',
                        lambda *a, **k: ran.append(1))
    with app.app_context():
        text = environment.capture(app.config['STATLEE'])
    assert ran == []
    assert 'could not be captured' in text


def test_an_exception_in_the_sandbox_is_a_failure_not_a_crash(app, monkeypatch):
    def boom(*a, **k):
        raise OSError('disk full')
    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox', boom)
    with app.app_context():
        text = environment.capture(app.config['STATLEE'])
    assert 'could not be captured' in text


# ---------------------------------------------------------------------------
# Fix round 1: a failing or hung probe must not block or re-run per export
# ---------------------------------------------------------------------------

def test_a_failed_probe_is_cached_for_the_failure_ttl(app, monkeypatch):
    calls = []
    now = [1000.0]

    def failing(code, language='Python', **k):
        calls.append(language)
        return _Result('boom', returncode=1)
    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox', failing)
    monkeypatch.setattr(environment.shutil, 'which', lambda name: None)
    monkeypatch.setattr(environment, '_now', lambda: now[0])
    cfg = app.config['STATLEE']
    with app.app_context():
        for _ in range(5):
            assert 'could not be captured' in environment.capture(cfg)
        assert calls == ['Python']                      # once within the TTL
        now[0] += environment.FAILURE_TTL + 1
        environment.capture(cfg)
    assert calls == ['Python', 'Python']                # retried after it


def test_a_transient_r_failure_expires_instead_of_sticking(app, monkeypatch):
    calls = []
    now = [1000.0]

    def run(code, language='Python', **k):
        calls.append(language)
        if language == 'R':
            return _Result('', returncode=-1, timed_out=True)
        return _Result('Python 3.12.1\npandas 2.2.0\n')
    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox', run)
    monkeypatch.setattr(environment.shutil, 'which', lambda name: 'Rscript')
    monkeypatch.setattr(environment, '_now', lambda: now[0])
    cfg = app.config['STATLEE']
    with app.app_context():
        text = environment.capture(cfg)
        environment.capture(cfg)
        assert calls == ['Python', 'R']
        assert 'pandas 2.2.0' in text and 'did not finish' in text
        now[0] += environment.FAILURE_TTL + 1
        environment.capture(cfg)
    assert calls == ['Python', 'R', 'Python', 'R']


def test_concurrent_first_exports_run_the_probe_once(app, monkeypatch):
    import threading
    import time
    calls = []
    started = threading.Event()

    def slow(code, language='Python', **k):
        calls.append(language)
        started.set()
        time.sleep(0.5)
        return _Result('Python 3.12.1\npandas 2.2.0\n')
    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox', slow)
    monkeypatch.setattr(environment.shutil, 'which', lambda name: None)
    cfg = app.config['STATLEE']
    results = []

    def worker():
        results.append(environment.capture(cfg))
    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert calls == ['Python']
    assert len(results) == 6
    assert all('pandas 2.2.0' in r for r in results)


def test_the_lock_is_not_held_while_the_probe_runs(app, monkeypatch):
    seen = []

    def run(code, language='Python', **k):
        # Another caller must be able to take the module lock meanwhile.
        got = environment._lock.acquire(timeout=1)
        seen.append(got)
        if got:
            environment._lock.release()
        return _Result('Python 3.12.1\n')
    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox', run)
    monkeypatch.setattr(environment.shutil, 'which', lambda name: None)
    environment.capture(app.config['STATLEE'])
    assert seen == [True]


def test_a_waiter_that_times_out_gets_the_failure_text(app, monkeypatch):
    import threading
    release = threading.Event()
    started = threading.Event()

    def hung(code, language='Python', **k):
        # Reached only after the runner registered itself in _inflight.
        started.set()
        release.wait(5)
        return _Result('Python 3.12.1\n')
    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox', hung)
    monkeypatch.setattr(environment.shutil, 'which', lambda name: None)
    monkeypatch.setattr(environment, '_wait_timeout', lambda cfg: 0.2)
    cfg = app.config['STATLEE']
    runner = threading.Thread(target=environment.capture, args=(cfg,))
    runner.start()
    try:
        assert started.wait(5)
        assert 'could not be captured' in environment.capture(cfg)
    finally:
        release.set()
        runner.join(5)
