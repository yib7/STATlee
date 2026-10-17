"""Every script the sandbox executes passed the static check verbatim
(Phase 4, 4.2).

The sandbox has three callers: /run, /wrangle and the environment probe that
/export starts. /wrangle used to check only the model's transform and then
execute it embedded in a harness, so the gate's guarantee rested on reasoning
about how the two texts combine. The contract here is exact: the text handed
to ``run_in_sandbox`` is a text ``codecheck.check_code`` saw, in the same
language.
"""
import pytest
from conftest import SAMPLE_CSV, post_json, upload_csv

from statlee import codecheck, environment, sandbox


@pytest.fixture
def gate_log(monkeypatch):
    checked, executed = [], []
    real_check = codecheck.check_code
    real_run = sandbox.run_in_sandbox

    def check(code, language='Python'):
        checked.append((code, str(language).lower()))
        return real_check(code, language)

    def run(code, language='Python', *args, **kwargs):
        executed.append((code, str(language).lower()))
        return real_run(code, language, *args, **kwargs)

    monkeypatch.setattr(codecheck, 'check_code', check)
    monkeypatch.setattr(sandbox, 'run_in_sandbox', run)
    environment.clear_cache()
    yield checked, executed
    environment.clear_cache()


def _assert_every_execution_was_checked(checked, executed):
    assert executed, 'nothing reached the sandbox'
    for item in executed:
        assert item in checked, (
            'the sandbox ran a script the static check never saw:\n'
            + item[0][:400])


def test_run_executes_only_checked_text(client, gate_log):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/run', {'filename': 'test.csv',
                                      'code': "print('hello')",
                                      'language': 'Python'})
    assert resp.status_code == 200
    _assert_every_execution_was_checked(*gate_log)


def test_wrangle_executes_only_checked_text(client, gate_log, fake_llm):
    fake_llm.set('wrangle', '{"code": "df = df.dropna()", '
                            '"summary": "Dropped missing rows", "error": null}')
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': 'drop missing rows'})
    assert resp.status_code == 200, resp.get_json()
    _assert_every_execution_was_checked(*gate_log)


def test_wrangle_refuses_a_transform_that_only_fails_inside_the_harness(
        client, gate_log, fake_llm, monkeypatch):
    """A harness-level refusal stops the run like a transform-level one."""
    from statlee.routes import datasets
    fake_llm.set('wrangle', '{"code": "df = df.dropna()", '
                            '"summary": "s", "error": null}')
    monkeypatch.setattr(
        datasets, 'WRANGLE_HARNESS',
        datasets.WRANGLE_HARNESS.replace("import numpy as np",
                                         "import numpy as np\nimport socket"))
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/wrangle', {'filename': 'test.csv',
                                          'instruction': 'drop missing rows'})
    assert resp.status_code == 403
    assert 'static analysis' in resp.get_json()['error']
    _checked, executed = gate_log
    assert executed == []


def test_export_probe_executes_only_checked_text(client, gate_log):
    upload_csv(client, SAMPLE_CSV)
    resp = post_json(client, '/export', {'filename': 'test.csv'})
    assert resp.status_code == 200
    _assert_every_execution_was_checked(*gate_log)
