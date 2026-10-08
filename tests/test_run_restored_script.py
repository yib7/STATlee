"""A script restored into a fresh session (deep audit D4-10).

A user who reopens the app with a script from a previous session (restored
editor, or pasted from an export) has no approved-script store yet. /run used
to refuse with a hard 403 "Generate code before running". It now runs that
code through the full edited-script guard instead: LLM code moderation
(default-deny, failing closed) and then the static check.
"""
from conftest import SAMPLE_CSV, post_json, upload_csv


def _run(client, code="print('restored ok')", language='Python'):
    return post_json(client, '/run', {'filename': 'test.csv', 'code': code,
                                      'language': language})


def _moderation_calls(fake_llm):
    return [c for c in fake_llm.calls if c[1] == 'code_moderation']


def test_restored_script_runs_after_moderation(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    resp = _run(client)
    assert resp.status_code == 200, resp.get_json()
    assert 'restored ok' in resp.get_json()['output']
    assert len(_moderation_calls(fake_llm)) == 1


def test_restored_script_is_recorded_as_approved(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    assert _run(client).status_code == 200
    # A second run of the same code needs no second moderation call.
    assert _run(client).status_code == 200
    assert len(_moderation_calls(fake_llm)) == 1


def test_restored_script_blocked_by_moderation(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.block_code('network access')
    resp = _run(client)
    assert resp.status_code == 403
    assert 'network access' in resp.get_json()['error']


def test_restored_script_moderation_outage_fails_closed(client, fake_llm,
                                                        monkeypatch):
    upload_csv(client, SAMPLE_CSV)
    real = fake_llm.generate

    def flaky(role, contents, **kw):
        if 'strict security reviewer' in fake_llm._flatten(contents):
            raise RuntimeError('provider down')
        return real(role, contents, **kw)

    monkeypatch.setattr(fake_llm, 'generate', flaky)
    resp = _run(client)
    assert resp.status_code == 503


def test_restored_script_still_gets_static_check(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    # Moderation passes (fake default), but the AST gate must still refuse.
    resp = _run(client, code="import os\nos.system('echo hi')")
    assert resp.status_code == 403
    assert 'static analysis' in resp.get_json()['error']


def test_restored_unparseable_verdict_is_denied(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    fake_llm.set('code_moderation', 'not json at all')
    resp = _run(client)
    assert resp.status_code == 403
