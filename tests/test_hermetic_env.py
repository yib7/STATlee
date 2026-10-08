"""The test suite and the package import must be hermetic (D5-14).

``load_dotenv()`` used to run at ``import statlee.app``, and python-dotenv
walks UP from that file looking for a ``.env``. So a developer's real ``.env``
(provider keys, LLM_PROVIDER, MODEL_*) leaked into the test process and the
module-level ``app = create_app()`` built a real, keyed LLM service. Importing
the package must now be side-effect free: no ``.env`` loading and no app built.
Only the entry point (``wsgi.py``) loads ``.env``, and never under testing.

Each probe runs in a fresh interpreter with ``load_dotenv`` replaced by a
recorder that loads nothing, so these tests can never read a real ``.env``.
"""
import os
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

_PROBE_PREFIX = """
import dotenv, dotenv.main
calls = []
def _record(*args, **kwargs):
    calls.append(1)
    return False
dotenv.load_dotenv = _record
dotenv.main.load_dotenv = _record
"""

_SCRUB = ('GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY',
          'LLM_PROVIDER', 'MODEL_PRO', 'MODEL_PRO_MAX', 'MODEL_FLASH',
          'MODEL_FLASH_LITE', 'APP_ENV', 'DATABASE_URL', 'UPLOAD_ROOT')


def _probe(code, tmp_path, **env_overrides):
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env['PYTHONPATH'] = REPO
    env['PYTHON_DOTENV_DISABLED'] = '1'   # belt and braces: never load a file
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, '-c', _PROBE_PREFIX + code],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


def test_importing_the_package_loads_no_dotenv_and_builds_no_app(tmp_path):
    out = _probe(
        "import statlee.app as m\n"
        "print('CALLS', len(calls))\n"
        "print('HAS_APP', hasattr(m, 'app'))\n",
        tmp_path, APP_ENV='testing')
    assert 'CALLS 0' in out
    assert 'HAS_APP False' in out


def test_wsgi_skips_dotenv_under_testing(tmp_path):
    out = _probe(
        "import wsgi\n"
        "print('CALLS', len(calls))\n",
        tmp_path, APP_ENV='testing')
    assert 'CALLS 0' in out


def test_wsgi_loads_dotenv_outside_testing(tmp_path):
    """The real entry point still reads ``.env`` for a normal dev/prod run."""
    db_url = 'sqlite:///' + str(tmp_path / 'probe.db').replace('\\', '/')
    out = _probe(
        "import wsgi\n"
        "print('CALLS', len(calls))\n",
        tmp_path, APP_ENV='development', DATABASE_URL=db_url,
        UPLOAD_ROOT=str(tmp_path / 'uploads'))
    assert 'CALLS 1' in out


def test_provider_env_is_scrubbed_inside_tests():
    """conftest strips provider keys / provider + model selection, so a value
    exported in the developer's shell cannot steer the suite."""
    for var in ('GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY',
                'LLM_PROVIDER', 'MODEL_PRO', 'MODEL_PRO_MAX', 'MODEL_FLASH',
                'MODEL_FLASH_LITE'):
        assert var not in os.environ, var
    assert os.environ.get('PYTHON_DOTENV_DISABLED') == '1'
