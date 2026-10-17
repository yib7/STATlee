"""Checklist 3.12: a misconfigured boot says what to fix, not a traceback.

``python wsgi.py`` and ``python -m flask --app wsgi`` stop with one line that
names the missing variables. Under gunicorn the error is re-raised, so its
"App failed to load" handling still applies. No ``.env`` is read
(PYTHON_DOTENV_DISABLED for wsgi.py, FLASK_SKIP_DOTENV for the Flask CLI) and
no provider key is set, so nothing here can reach a model.
"""
import os
import subprocess
import sys

import pytest

from statlee.config import Config, ConfigError

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

_KEYS = ('GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY',
         'FLASK_SECRET_KEY', 'LLM_PROVIDER')

_NO_SERVER = """
import runpy, sys
import flask
flask.Flask.run = lambda self, **kw: print('RUN', kw.get('host'))
sys.argv = ['wsgi.py']
runpy.run_path('wsgi.py', run_name='__main__')
"""


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in _KEYS and not k.startswith('EXEC_')}
    # PYTHON_DOTENV_DISABLED stops wsgi.py's load_dotenv; the Flask CLI reads
    # .env through dotenv_values, which ignores it, so it needs its own off
    # switch. Without it a developer's real .env leaks into these runs.
    env.update({'PYTHONPATH': REPO, 'PYTHON_DOTENV_DISABLED': '1',
                'FLASK_SKIP_DOTENV': '1',
                'UPLOAD_ROOT': str(tmp_path / 'uploads'),
                'DATABASE_URL': 'sqlite://'})
    env.update(extra)
    return env


def _run(args, env):
    return subprocess.run([sys.executable, *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=180)


def test_config_error_is_still_a_value_error():
    assert issubclass(ConfigError, ValueError)
    with pytest.raises(ConfigError):
        Config(env='production').validate()


def test_production_without_secrets_exits_with_a_named_message(tmp_path):
    proc = _run(['-c', _NO_SERVER], _env(tmp_path, APP_ENV='production'))
    assert proc.returncode == 1
    assert 'Traceback' not in proc.stderr
    assert 'GEMINI_API_KEY' in proc.stderr
    assert 'FLASK_SECRET_KEY' in proc.stderr
    assert 'RUN' not in proc.stdout


def test_flask_cli_without_secrets_exits_with_a_named_message(tmp_path):
    proc = _run(['-m', 'flask', '--app', 'wsgi', 'routes'],
                _env(tmp_path, APP_ENV='production'))
    assert proc.returncode != 0
    assert 'Traceback' not in proc.stderr
    assert 'GEMINI_API_KEY' in proc.stderr


def test_under_gunicorn_the_error_is_re_raised(tmp_path):
    """gunicorn reports an app that fails to import; a SystemExit from the
    import would bypass that, so the exception propagates there."""
    code = ("import sys, types; sys.modules['gunicorn'] = types.ModuleType("
            "'gunicorn'); import wsgi")
    proc = _run(['-c', code], _env(tmp_path, APP_ENV='production'))
    assert proc.returncode == 1
    assert 'ConfigError' in proc.stderr
    assert 'GEMINI_API_KEY' in proc.stderr


def test_development_without_a_key_boots_and_says_why_calls_fail(tmp_path):
    proc = _run(['-c', _NO_SERVER],
                _env(tmp_path, APP_ENV='development', EXEC_TIMEOUT='abc'))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert 'RUN 127.0.0.1' in proc.stdout
    assert 'GEMINI_API_KEY is not set' in proc.stderr
    assert "EXEC_TIMEOUT='abc'" in proc.stderr
    assert 'Traceback' not in proc.stderr
