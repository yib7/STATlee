"""Unit + HTTP-level tests for the non-LLM static pre-check (P1-1a).

``statlee.codecheck.check_code`` is defense-in-depth ADDED to the LLM
moderation gates: a deterministic AST (Python) / regex (R) denylist run before
every sandbox execution. These tests exercise the checker directly (fast, no
HTTP) plus a couple of route-level regressions proving the static gate rejects
code even when the LLM gate passes.
"""
import pytest

from statlee.codecheck import check_code

# ---------------------------------------------------------------------------
# Python — blocked constructs
# ---------------------------------------------------------------------------

BLOCKED_PY = [
    'import socket',
    'import urllib.request',
    'import requests',
    'import subprocess',
    'subprocess.run(["id"])',
    "os.system('id')",
    "os.popen('x')",
    "os.environ['SECRET']",
    "eval('1')",
    "exec('x')",
    "__import__('os')",
    "open('/etc/passwd')",
    "open('/app/.env')",
    "open('../secrets')",
    'getattr(o, name_var)',
    '().__class__.__bases__[0].__subclasses__()',
    "getattr(__builtins__, 'eval')('1')",   # reflective bare-name escape
    '__builtins__',
    # aliased / from-imported host-module primitives (os/sys/shutil are not on
    # the import denylist, so these must be caught by alias resolution + the
    # from-import member check, not the import rule).
    "import os as o\no.system('id')",
    "import os as _o\n_o.popen('id')",
    "from os import system\nsystem('id')",
    "from os import popen\npopen('id')",
    "from os import environ\nprint(environ)",
    "from os import remove\nremove('x')",
    'from sys import modules',
    "from shutil import rmtree\nrmtree('/')",
    "from shutil import move\nmove('a', 'b')",
]


@pytest.mark.parametrize('code', BLOCKED_PY)
def test_python_blocks_dangerous(code):
    blocked, reason = check_code(code, 'Python')
    assert blocked is True, f'expected BLOCK for: {code!r}'
    assert reason, 'a blocked result must carry a non-empty reason'


# ---------------------------------------------------------------------------
# Python — allowed realistic analysis
# ---------------------------------------------------------------------------

CLEAN_ANALYSIS = """\
import pandas as pd
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import os

df = pd.read_csv('data.csv')
with open('data.csv') as fh:
    fh.read()
os.path.join('a', 'b')
print(df.describe())
print(getattr(df, 'mean'))
plt.savefig('plot.png')
"""


def test_python_allows_realistic_pandas_analysis():
    blocked, reason = check_code(CLEAN_ANALYSIS, 'Python')
    assert blocked is False, f'realistic analysis wrongly blocked: {reason}'
    assert reason == ''


@pytest.mark.parametrize('code', [
    'import os',
    "os.path.join('a', 'b')",
    "getattr(df, 'mean')",
    "open('data.csv')",
    "open('plot.png', 'w')",
    'import sys',
    "df.eval('a + b')",          # legit pandas method must not be blocked
    'import os as o\nprint(o.getcwd())',      # aliased-but-harmless os member
    "from os.path import join\nprint(join('a', 'b'))",  # os.path re-export, safe
    "from os import getcwd\nprint(getcwd())",           # harmless os member
    'import numpy as np\nprint(np.mean([1, 2]))',       # alias map must not over-block
])
def test_python_allows_individual_safe_snippets(code):
    blocked, reason = check_code(code, 'Python')
    assert blocked is False, f'wrongly blocked {code!r}: {reason}'


def test_python_syntax_error_fails_open():
    # A script that cannot parse cannot execute anything harmful -> fail open.
    assert check_code('def (', 'Python') == (False, '')


def test_language_is_case_insensitive():
    assert check_code('import requests', 'python')[0] is True
    assert check_code('import requests', 'PYTHON')[0] is True


# ---------------------------------------------------------------------------
# R — textual denylist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('code', [
    "system('id')",
    'download.file(u, d)',
])
def test_r_blocks_dangerous(code):
    blocked, reason = check_code(code, 'R')
    assert blocked is True
    assert reason


def test_r_allows_regression():
    blocked, reason = check_code('summary(lm(y ~ x, data=df))', 'R')
    assert blocked is False
    assert reason == ''


# ---------------------------------------------------------------------------
# HTTP-level regression: the STATIC gate rejects code the LLM gate passes
# ---------------------------------------------------------------------------

from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv  # noqa: E402


def _generate_script(client, prompt='go'):
    """Run /chat and drain the SSE stream so the approved script persists."""
    resp = post_json(client, '/chat', {'filename': 'test.csv', 'prompt': prompt})
    sse_events(resp)
    return resp


def test_run_static_gate_blocks_os_system_even_when_llm_passes(client, fake_llm):
    """The edited-/run path re-moderates via the LLM (default PASS here), but the
    non-LLM static gate must independently reject a script that shells out."""
    fake_llm.set('validation', "print('original')")
    upload_csv(client, SAMPLE_CSV)
    _generate_script(client)

    # LLM code_moderation defaults to PASS; only the static AST gate blocks this.
    resp = post_json(client, '/run',
                     {'filename': 'test.csv',
                      'code': "import pandas as pd\nimport os\nos.system('id')",
                      'language': 'Python'})
    assert resp.status_code == 403
    assert 'static analysis' in resp.get_json()['error'].lower()


def test_run_static_gate_blocks_network_import_even_when_llm_passes(client, fake_llm):
    fake_llm.set('validation', "print('original')")
    upload_csv(client, SAMPLE_CSV)
    _generate_script(client)

    resp = post_json(client, '/run',
                     {'filename': 'test.csv',
                      'code': "import requests\nrequests.get('http://x')",
                      'language': 'Python'})
    assert resp.status_code == 403
    assert 'static analysis' in resp.get_json()['error'].lower()


def test_chat_static_gate_rejects_generated_network_code(client, fake_llm):
    """/chat: the LLM code_moderation passes (default) but the generated script
    imports requests -> the static gate refuses to approve it and streams an
    error instead of a runnable 'done'."""
    fake_llm.set('validation', "import requests\nrequests.get('http://x')")
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/chat',
                     {'filename': 'test.csv', 'prompt': 'summarize the data'})
    events = sse_events(resp)
    assert not any(e.get('type') == 'done' for e in events)
    errors = [e for e in events if e.get('type') == 'error']
    assert errors and 'static analysis' in errors[0]['message'].lower()
    # Never approved -> a follow-up /run refuses it.
    run = post_json(client, '/run',
                    {'filename': 'test.csv',
                     'code': "import requests\nrequests.get('http://x')",
                     'language': 'Python'})
    assert run.status_code == 403


def test_wrangle_static_gate_rejects_generated_network_code(client, fake_llm):
    """/wrangle: a transform that the LLM passes but that shells out is rejected
    by the static gate (403) before the harness executes."""
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('wrangle', '{"code": "import os\\nos.system(\'id\')", '
                            '"summary": "x", "error": null}')
    resp = post_json(client, '/wrangle',
                     {'filename': 'test.csv', 'instruction': 'clean it'})
    assert resp.status_code == 403
    assert 'static analysis' in resp.get_json()['error'].lower()
