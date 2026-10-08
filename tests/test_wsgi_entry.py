"""`python wsgi.py` binds loopback by default (D5-8).

The documented local run is open-auth in SANDBOX_MODE=subprocess, so binding
0.0.0.0 exposed /chat and /run (code execution billed to the developer's key)
to everyone on the same network. LAN exposure is now an explicit opt-in.
"""
import os
import subprocess
import sys

from statlee.config import Config

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

_RUN_WSGI_MAIN = """
import runpy
import flask
flask.Flask.run = lambda self, **kw: print('RUN', kw.get('host'), kw.get('port'))
runpy.run_path('wsgi.py', run_name='__main__')
"""


def _run_main(tmp_path, **extra_env):
    env = {k: v for k, v in os.environ.items()
           if k not in ('BIND_HOST', 'PORT', 'UPLOAD_ROOT')}
    env.update({'PYTHONPATH': REPO, 'APP_ENV': 'testing',
                'PYTHON_DOTENV_DISABLED': '1',
                'UPLOAD_ROOT': str(tmp_path / 'uploads')})
    env.update(extra_env)
    proc = subprocess.run([sys.executable, '-c', _RUN_WSGI_MAIN], cwd=REPO,
                          env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return [ln for ln in proc.stdout.splitlines() if ln.startswith('RUN ')][-1]


def test_bind_host_defaults_to_loopback():
    assert Config.bind_host == '127.0.0.1'


def test_bind_host_from_env(monkeypatch):
    monkeypatch.setenv('BIND_HOST', '0.0.0.0')
    assert Config.from_env().bind_host == '0.0.0.0'


def test_dev_server_binds_loopback_by_default(tmp_path):
    assert _run_main(tmp_path) == 'RUN 127.0.0.1 5000'


def test_dev_server_lan_binding_is_opt_in(tmp_path):
    assert _run_main(tmp_path, BIND_HOST='0.0.0.0', PORT='5055') == 'RUN 0.0.0.0 5055'
