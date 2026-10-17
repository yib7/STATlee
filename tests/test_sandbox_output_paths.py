"""Run output names no host path (Phase 4, 4.9).

A failed script's traceback is expected output, but it used to carry the
host's absolute paths: the throwaway run dir under the system temp dir, the
interpreter's site-packages (on a dev machine, the user's home folder), all
of which the UI shows, the workbook stores and /export ships. The sandbox now
rewrites them to neutral labels while keeping the traceback readable.
"""
import os
import site
import sys
import tempfile

import pytest

from statlee import sandbox

FAILING = ("import pandas as pd\n"
           "print('before')\n"
           "pd.read_csv('missing.csv')\n")


def _host_paths():
    paths = {tempfile.gettempdir(), sys.prefix, os.path.expanduser('~')}
    paths.update(p for p in site.getsitepackages() if os.path.isdir(p))
    user_site = site.getusersitepackages()
    if os.path.isdir(user_site):
        paths.add(user_site)
    return {p for p in paths if p and len(p) > 3}


@pytest.fixture(scope='module')
def failed_run():
    return sandbox.run_in_sandbox(FAILING, 'Python', timeout=60)


def test_failed_run_keeps_a_readable_traceback(failed_run):
    out = failed_run.output
    assert failed_run.returncode != 0
    assert 'before' in out
    assert 'Traceback (most recent call last)' in out
    assert 'File "script.py", line 3' in out
    assert 'FileNotFoundError' in out
    assert 'pandas' in out


def test_failed_run_names_no_host_path(failed_run):
    out = failed_run.output
    lowered = out.lower()
    for path in _host_paths():
        for variant in (path, path.replace('\\', '/')):
            assert variant.lower() not in lowered, (
                f'host path {variant!r} leaked into run output:\n{out}')
    assert 'ccrun_' not in out


def test_redaction_leaves_ordinary_text_alone():
    text = "coef 1.25, p = 0.03\nsaved plot_1.png\n"
    assert sandbox.redact_host_paths(text, '/tmp/ccrun_abc') == text


def test_redaction_of_the_run_dir_and_docker_workdir():
    out = sandbox.redact_host_paths(
        'File "/tmp/ccrun_abc/script.py", line 2\n'
        'File "/work/script.py", line 2\n', '/tmp/ccrun_abc')
    assert out == 'File "script.py", line 2\nFile "script.py", line 2\n'
