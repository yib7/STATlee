"""Checklist 3.12: a model provider that cannot be used gives the user an
actionable message, never a generic failure (release audit 2026-10-16).

Two cases: no API key configured, and a network error or timeout from the
SDK. Everything runs against fake clients, so no call leaves the machine.
"""
import types

import httpx
import pytest
from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv

from statlee import llm
from statlee.config import Config

NS = types.SimpleNamespace
PASS = '{"decision": "pass", "reason": ""}'
_REQ = httpx.Request('POST', 'https://provider.invalid/v1')


def _anthropic_with(error):
    svc = llm.LLMService(Config(env='testing', llm_provider='anthropic',
                                anthropic_api_key='k'))

    def fail(**_kw):
        raise error

    svc._backend._client = NS(messages=NS(create=fail, stream=fail))
    return svc


@pytest.mark.parametrize('provider, key', [('gemini', 'GEMINI_API_KEY'),
                                           ('openai', 'OPENAI_API_KEY')])
def test_missing_key_is_a_typed_not_configured_error(provider, key):
    svc = llm.LLMService(Config(env='testing', llm_provider=provider))
    with pytest.raises(llm.LLMNotConfiguredError) as exc:
        svc.generate('lite', 'hi')
    assert exc.value.reason == 'not_configured'
    assert key in exc.value.user_message
    assert '.env' in exc.value.user_message
    assert '—' not in exc.value.user_message
    with pytest.raises(llm.LLMNotConfiguredError):
        list(svc.stream('draft', 'hi'))


@pytest.mark.parametrize('error', [
    __import__('anthropic').APIConnectionError(request=_REQ),
    __import__('anthropic').APITimeoutError(request=_REQ),
    httpx.ConnectError('connection refused', request=_REQ),
    ConnectionResetError('reset by peer'),
    TimeoutError('timed out'),
])
def test_network_errors_become_a_typed_unreachable_error(error):
    svc = _anthropic_with(error)
    with pytest.raises(llm.LLMConnectionError) as exc:
        svc.generate('lite', 'hi')
    assert exc.value.reason == 'unreachable'
    assert exc.value.__cause__ is error
    with pytest.raises(llm.LLMConnectionError):
        list(svc.stream('draft', 'hi'))


def test_openai_timeout_is_unreachable():
    import openai
    svc = llm.LLMService(Config(env='testing', llm_provider='openai',
                                openai_api_key='k'))

    def fail(**_kw):
        raise openai.APITimeoutError(request=_REQ)

    svc._backend._client = NS(chat=NS(completions=NS(create=fail)))
    with pytest.raises(llm.LLMConnectionError):
        svc.generate('lite', 'hi')


def test_a_network_error_wrapped_by_the_sdk_is_still_recognized():
    try:
        try:
            raise ConnectionRefusedError('refused')
        except ConnectionRefusedError as inner:
            raise RuntimeError('request failed') from inner
    except RuntimeError as wrapped:
        error = wrapped
    with pytest.raises(llm.LLMConnectionError):
        _anthropic_with(error).generate('lite', 'hi')


def test_other_errors_are_not_reclassified():
    with pytest.raises(ValueError):
        _anthropic_with(ValueError('bad request')).generate('lite', 'hi')


# --- what the routes answer --------------------------------------------------

class _Backend:
    """Moderation passes; every other call fails with ``error``."""

    def __init__(self, error, fail_moderation=False):
        self.error = error
        self.fail_moderation = fail_moderation

    def generate(self, model, contents, *, temperature, json_mode):
        if json_mode and not self.fail_moderation:
            return llm.LLMResult(text=PASS, usage={'input': 1, 'output': 1})
        raise self.error

    def stream(self, model, contents, *, temperature, usage_out):
        raise self.error
        yield ''   # pragma: no cover


def _install(backend=None, **cfg):
    svc = llm.LLMService(Config(env='testing', **cfg))
    if backend is not None:
        svc._backend = backend
    llm.set_service(svc)
    return svc


def test_chat_without_a_key_names_the_missing_variable(client):
    upload_csv(client, SAMPLE_CSV)
    _install()
    resp = post_json(client, '/chat', {'filename': 'test.csv',
                                       'prompt': 'summarize'})
    assert resp.status_code == 503
    body = resp.get_json()
    assert body['reason'] == 'not_configured'
    assert 'GEMINI_API_KEY' in body['error']


def test_chat_with_the_provider_unreachable_says_so(client):
    upload_csv(client, SAMPLE_CSV)
    _install(_Backend(ConnectionRefusedError('refused'), fail_moderation=True),
             gemini_api_key='k')
    resp = post_json(client, '/chat', {'filename': 'test.csv',
                                       'prompt': 'summarize'})
    assert resp.status_code == 503
    body = resp.get_json()
    assert body['reason'] == 'unreachable'
    assert 'Traceback' not in body['error']


def test_a_stream_that_loses_the_network_ends_in_a_typed_sse_error(client):
    upload_csv(client, SAMPLE_CSV)
    _install(_Backend(httpx.ReadTimeout('read timed out', request=_REQ)),
             gemini_api_key='k')
    resp = post_json(client, '/chat', {'filename': 'test.csv',
                                       'prompt': 'summarize'})
    assert resp.status_code == 200
    error = next(e for e in sse_events(resp) if e.get('type') == 'error')
    assert error['reason'] == 'unreachable'
    assert error['message'] == llm.LLMConnectionError.default_message


def test_json_model_routes_answer_503_with_the_reason(app):
    from statlee.routes import llm_json_error
    with app.app_context():
        resp, status = llm_json_error(
            llm.LLMConnectionError('Gemini'), 'Could not do it.')
    assert status == 503
    assert resp.get_json()['reason'] == 'unreachable'
