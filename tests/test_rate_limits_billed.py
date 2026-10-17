"""Rate limits on the billed routes (checklist 3.13 gap from the release
audit 2026-10-16): /run and /chat each spend money per call, so their
dedicated limits must actually answer 429, with a message the toast can show
(no em dash, C-15). Also the tighter /report_issue limit (B-16)."""
import pytest
from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv

from statlee import llm
from statlee.app import create_app
from statlee.config import Config


@pytest.fixture
def limited_app(tmp_path, fake_llm, reset_limiter_state):
    cfg = Config(
        env='testing', upload_root=str(tmp_path / 'u'),
        database_url='sqlite:///' + str(tmp_path / 'r.db').replace('\\', '/'),
        flask_secret_key='k', rate_limit_enabled=True,
        rate_limit_run='2 per minute', rate_limit_chat='2 per minute')
    cfg.validate()
    app = create_app(cfg)
    llm.set_service(fake_llm)
    return app


def _assert_429(resp):
    assert resp.status_code == 429
    message = resp.get_json()['error']
    assert message.startswith('Too many requests.')
    assert '—' not in message          # no em dash in user-facing copy


def test_run_is_rate_limited(limited_app):
    client = limited_app.test_client()
    codes = []
    for _ in range(3):
        resp = post_json(client, '/run', {'code': "print('x')",
                                          'language': 'Python'})
        codes.append(resp.status_code)
    assert codes[:2] == [200, 200]
    _assert_429(resp)


def test_chat_is_rate_limited(limited_app, fake_llm):
    client = limited_app.test_client()
    upload_csv(client, SAMPLE_CSV)
    for _ in range(2):
        ok = post_json(client, '/chat', {'filename': 'test.csv',
                                         'prompt': 'summarize'})
        assert ok.status_code == 200
        # Drain each stream before the next request (its request context).
        assert any(e.get('type') == 'done' for e in sse_events(ok))
    _assert_429(post_json(client, '/chat', {'filename': 'test.csv',
                                            'prompt': 'summarize'}))
    # The limited request never reached a model.
    assert sum(1 for c in fake_llm.calls if c[1] == 'draft') == 2


def test_report_issue_has_its_own_tight_limit(limited_app):
    from statlee.routes import misc
    client = limited_app.test_client()
    codes = [post_json(client, '/report_issue',
                       {'description': f'issue {i}'}).status_code
             for i in range(misc.REPORT_ISSUE_RATE_COUNT + 1)]
    assert codes[:-1] == [200] * misc.REPORT_ISSUE_RATE_COUNT
    assert codes[-1] == 429
