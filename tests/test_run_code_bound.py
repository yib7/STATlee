"""/run refuses an oversized script before any gate (Phase 4, 4.4 / 4.5).

An edited script is re-moderated by a paid model call, and before this cap
the only bound on ``code`` was the 16 MB request limit: one request inside
the /run rate limit could put millions of input tokens on the operator's key.
"""
from conftest import SAMPLE_CSV, post_json, upload_csv

from statlee.routes import RUN_CODE_MAX


def _kinds(fake_llm):
    return [kind for _role, kind, _text in fake_llm.calls]


def test_oversized_script_is_413_before_moderation(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    code = "x = 1\n" + "# padding\n" * (RUN_CODE_MAX // 10 + 1)
    assert len(code) > RUN_CODE_MAX
    resp = post_json(client, '/run', {'filename': 'test.csv', 'code': code,
                                      'language': 'Python'})
    assert resp.status_code == 413
    assert str(RUN_CODE_MAX) in resp.get_json()['error'].replace(',', '')
    assert 'code_moderation' not in _kinds(fake_llm)


def test_script_at_the_limit_still_runs(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    head = "print('ok')\n"
    code = head + "#" * (RUN_CODE_MAX - len(head))
    assert len(code) == RUN_CODE_MAX
    resp = post_json(client, '/run', {'filename': 'test.csv', 'code': code,
                                      'language': 'Python'})
    assert resp.status_code == 200
    assert 'ok' in resp.get_json()['output']
