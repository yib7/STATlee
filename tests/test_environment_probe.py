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


def test_python_probe_failure_is_stated_and_not_cached(app, monkeypatch):
    results = [_Result('boom C:\\secret\\path', returncode=1),
               _Result('Python 3.12.1\npandas 2.2.0\n')]
    monkeypatch.setattr(environment.sandbox, 'run_in_sandbox',
                        lambda code, language='Python', **k: results.pop(0))
    monkeypatch.setattr(environment.shutil, 'which', lambda name: None)
    with app.app_context():
        first = environment.capture(app.config['STATLEE'])
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
