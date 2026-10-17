"""Unit + HTTP-level tests for the non-LLM static pre-check (P1-1a).

``statlee.codecheck.check_code`` is defense-in-depth ADDED to the LLM
moderation gates: a deterministic AST import-allowlist + targeted rules (Python)
/ regex denylist (R) run before
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
    # constant-argument getattr on a host module resolves the same primitive the
    # os.*/shutil.* attribute rule blocks -- a literal name must not launder it.
    "getattr(os, 'system')('id')",
    "getattr(os, 'popen')('x')",
    "getattr(shutil, 'rmtree')('/')",
    "import os as o\ngetattr(o, 'system')('id')",   # aliased owner + constant attr
    # plain assignment aliasing (`o = os`) must be tracked like `import os as o`,
    # including chains, so it can't dodge the attribute rules either.
    "o = os\no.system('id')",
    "s = shutil\ns.rmtree('/tmp')",
    "a = os\nb = a\nb.popen('id')",
    # --- deep-audit 2026-10-08 (D2-1..D2-4): allowlist-model regressions -------
    # D2-1: the importable `builtins` module / from-import / name rebinding must
    # not launder eval/exec past the bare-name rule.
    'import builtins\nbuiltins.exec("print(1)")',
    'from builtins import exec as run\nrun("x")',
    'f = exec\nf("x")',
    # D2-2: attribute chains reaching os/sys through another module.
    'import os\nos.path.os.system("id")',
    'import pandas as pd\npd.io.common.os.system("id")',
    'import os\nos.__dict__["system"]("id")',
    'import os\nvars(os)["system"]("id")',
    'import os\nos.sys.modules["builtins"]',
    # D2-3: low-level OS / loader modules outside the analysis allowlist.
    'import nt\nnt.system("id")',
    'import posix',
    'import _socket',
    'import runpy',
    # D2-4: library file I/O / deserialization primitives.
    'df.to_pickle("/tmp/x")',
    'pd.read_pickle("x.pkl")',
    'import pandas as pd\npd.read_csv("/app/.env")',
    'import numpy as np\nnp.load("x.npy", allow_pickle=True)',
    'from pathlib import Path\nPath("/etc/passwd").read_text()',
    # reflection helpers and frame / bound-method escapes.
    'import operator\nop = operator.attrgetter("system")',
    'from os import *\nsystem("id")',
    'print.__self__.exec("x")',
    'import sys\nsys._getframe().f_globals',
    'globals()["__builtins__"]',
    'try:\n    1/0\nexcept Exception as e:\n    e.__traceback__.tb_frame.f_globals',
    # host-module calls / remote targets / non-literal-but-prefixed paths.
    'import shutil\nshutil.copy("/app/.env", "x.txt")',
    'from shutil import copy\ncopy("/app/.env", "x.txt")',
    'import os\nos.chdir("/app")',
    'import glob\nglob.glob("/app/*")',
    'from glob import glob\nglob("/app/*")',
    'import shutil\nc = shutil.copy\nc("/app/.env", "x.txt")',
    'import pandas as pd\npd.read_csv("https://example.com/x.csv")',
    'import pandas as pd\npd.read_csv(f"/app/{name}")',
    'open("/app/" + name)',
    'import pandas as pd\npd.io.common.urlopen("http://x")',
    'from os import sys as s\ns.modules',
    'import typing\ntyping.get_type_hints(f)',
    'import numpy as np\nnp.load("x.npy", allow_pickle=flag)',
    "getattr(print, '__self__')",
    'from . import x',
    # --- release audit 2026-10-16 (B-04): a host module used as a VALUE is
    # refused, so no binding form can launder it past the member rules.
    "import os\n(o := os).system('id')",
    "import os\na, b = os, 1\na.system('id')",
    "import os\ndef f(o=os):\n    o.system('id')\nf()",
    "import os\nfor o in [os]:\n    o.system('id')",
    "import os\n{'k': os}['k'].system('id')",
    "import os\no: object = os\no.system('id')",
    "import os, contextlib\nwith contextlib.nullcontext(os) as o:\n    o.system('id')",
    "import os\n(lambda o: o.system('id'))(os)",
    "import os\nprint(os)",
    "import os\no = os\nprint(o)",
    'import pandas as pd\nm = pd.io.common.os\nm.system("id")',
    "from os import sys as s\nf(s)",
    'import sys\nmods = [sys][0]',
    # dangerous os calls named by the audit (B-03/B-04)
    'import os\nos.kill(os.getppid(), 9)',
    'import os\nos.killpg(0, 9)',
    "import os\nos.mkfifo('plot.png')",
    "import os\nos.mknod('x')",
    "import os\nos.symlink(p, 'plot.png')",
    "import os\nos.link(p, 'x')",
    "import os\nos.chmod('x', 0o777)",
    "import os\nos.chown('x', 0, 0)",
    "import os\nos.rename('a', 'b')",
    "import os\nos.replace('a', 'b')",
    "import os\nos.truncate('a', 0)",
    "from os import kill\nkill(1, 9)",
    "import os\ngetattr(os, 'kill')(1, 9)",
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
    "getattr(os, 'getcwd')()",              # constant getattr on a HARMLESS os member
    'o = os\nprint(o.getcwd())',            # assignment alias to a harmless member
    "x = df\nprint(x.eval('a + b'))",       # assigning a non-module name must not over-block
    'g = getattr\nprint(g(df, "mean"))',    # rebinding a builtin name is not a module alias
])
def test_python_allows_individual_safe_snippets(code):
    blocked, reason = check_code(code, 'Python')
    assert blocked is False, f'wrongly blocked {code!r}: {reason}'


# Realistic analysis idioms the allowlist model must keep passing (guards
# against over-blocking: every one of these is something the code-gen prompts
# legitimately produce).
SAFE_CORPUS = [
    "import pandas as pd\ndf.groupby('g')['v'].mean()",
    "import pandas as pd\nm = df.merge(other, on='id', how='left')",
    "import pandas as pd\np = df.pivot_table(index='a', columns='b', values='v')",
    'df.query("age > 30")',
    'df.eval("a + b")',
    'import statsmodels.formula.api as smf\nres = smf.ols("y ~ x", data=df).fit()',
    'from scipy import stats\nprint(stats.ttest_ind(a, b))',
    'from scipy import stats\nprint(stats.chi2_contingency(t))',
    ("import seaborn as sns\nimport matplotlib.pyplot as plt\n"
     "sns.histplot(df['age'])\nplt.savefig('plot.png', bbox_inches='tight')"),
    "df.to_csv('cleaned.csv')",
    "pd.read_csv('data.csv')",
    "import os\nos.path.join('a', 'b')",
    'import os\nprint(os.getcwd())',
    'import sys\nprint(sys.version)',
    "import warnings\nwarnings.filterwarnings('ignore')",
    'import statsmodels.api as sm',
    'from sklearn.linear_model import LogisticRegression',
    'import pingouin as pg',
    'df.code.value_counts()',
    'df.time.mean()',
    'df.signal.describe()',
    'print(x.__class__.__name__)',
    'print(type(x).__name__)',
    "n = 3\nprint(f'{n} rows, mean={df.v.mean():.2f}')",
    'sq = [i * i for i in range(10) if i % 2]',
    "getattr(df, 'mean')",
    "import os\ngetattr(os, 'getcwd')()",
    'from collections import Counter',
    "import numpy as np\nnp.save('arr.npy', a)",
    "from pathlib import Path\nPath('data.csv').exists()",
    "import os\nos.makedirs('out', exist_ok=True)",
    "import glob\nprint(glob.glob('*.csv'))",
    "from glob import glob\nprint(glob('*.csv'))",
    "import os\nj = os.path.join\nprint(j('/a', 'b'))",
    "import numpy as np\na = np.load('arr.npy')",
    "import numpy as np\na = np.load('arr.npy', allow_pickle=False)",
    "import pandas as pd\nd = pd.read_csv(f'{name}.csv')",
    "import pandas as pd\nd = pd.to_datetime(df['d'], format='%Y/%m/%d')",
    "rows = df.to_dict('records')",
    "import re\npat = re.compile(r'\\d+')",
    "import matplotlib\nmatplotlib.use('Agg')",
    "from typing import List, Optional",
    # B-04 over-block guards: module VALUE refusal must not touch these.
    "import glob\nfiles = sorted(glob.glob('*.csv'))",
    "import sys\nprint('warning', file=sys.stderr)",
    "import sys\nif sys.version_info >= (3, 8):\n    print('ok')",
    "import os\nexists = os.path.exists('data.csv')",
    "import os\nprint(hasattr(os, 'getcwd'))",
    "df = df.rename(columns={'a': 'b'})",
    "df['c'] = df['c'].str.replace('a', 'b')",
    "nt = df.copy()\nnt = nt.rename(columns={'a': 'b'})",   # variable named nt
    "print(df.os.value_counts())",                           # column named os
    "x = df.os",
    "import pandas as pd\nimport os\nos.makedirs('out', exist_ok=True)\n"
    "pd.DataFrame({'a': [1]}).to_csv(os.path.join('out', 'a.csv'))",
    "from pathlib import Path\nfiles = list(Path('.').glob('*.csv'))",
]


@pytest.mark.parametrize('code', SAFE_CORPUS)
def test_python_safe_corpus_not_blocked(code):
    blocked, reason = check_code(code, 'Python')
    assert blocked is False, f'wrongly blocked {code!r}: {reason}'


def test_python_disallowed_import_reason_names_module():
    blocked, reason = check_code('import nt', 'Python')
    assert blocked is True
    assert reason == 'imports a module outside the analysis allowlist: nt'


def test_python_relative_import_blocked():
    assert check_code('from . import x', 'Python')[0] is True


def test_allowed_modules_is_public_frozenset():
    from statlee.codecheck import ALLOWED_PYTHON_MODULES
    assert isinstance(ALLOWED_PYTHON_MODULES, frozenset)
    assert {'pandas', 'numpy', 'scipy', 'os', 'sys'} <= ALLOWED_PYTHON_MODULES
    assert not {'builtins', 'nt', 'posix', 'subprocess', 'socket'} & ALLOWED_PYTHON_MODULES


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
    # D2-6: indirection + network libraries.
    'do.call("system", list("id"))',
    'get("system")("id")',
    'eval(parse(text = "1"))',
    'library(httr)',
    'do.call(paste0("sys", "tem"), list("id"))',
    'httr::GET("http://x")',
    'res <- .Call("C_fn", x)',
])
def test_r_blocks_dangerous(code):
    blocked, reason = check_code(code, 'R')
    assert blocked is True
    assert reason


@pytest.mark.parametrize('code', [
    'summary(lm(y ~ x, data=df))',
    'library(dplyr)',
    'library(ggplot2)',
    'library(MASS)',
    'all <- do.call(rbind, lst)',
    'all <- do.call("cbind", lst)',
    'f <- function(x) match.call()',
])
def test_r_allows_regression(code):
    blocked, reason = check_code(code, 'R')
    assert blocked is False, f'wrongly blocked R {code!r}: {reason}'
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
