"""LLM backend reliability (deep audit D3-3, D3-4, D3-5, D3-11, D3-12,
D3-13, D3-16).

Every test fakes the provider client (``SimpleNamespace`` stand-ins or a
monkeypatched SDK constructor), so nothing touches the network and no key is
needed.
"""
import types

import pytest

from statlee import llm
from statlee.config import Config
from statlee.usage import usage_breakdown

NS = types.SimpleNamespace


def _enum(name):
    return NS(name=name)


# ---------------------------------------------------------------------------
# Gemini fakes
# ---------------------------------------------------------------------------
def _gemini_svc(**cfg):
    return llm.LLMService(Config(env='testing', gemini_api_key='k', **cfg))


def _gemini_with(svc, *, response=None, chunks=None):
    captured = {}

    def gen(**k):
        captured.update(k)
        return response

    def stream(**k):
        captured.update(k)
        return iter(chunks or [])

    svc._backend._client = NS(models=NS(generate_content=gen,
                                        generate_content_stream=stream))
    return captured


def _meta(prompt=5, cand=8, thoughts=None, cached=None):
    return NS(prompt_token_count=prompt, candidates_token_count=cand,
              thoughts_token_count=thoughts, cached_content_token_count=cached)


# ---------------------------------------------------------------------------
# D3-4: safety blocks, refusals and empty output raise a typed error
# ---------------------------------------------------------------------------
def test_gemini_generate_safety_finish_raises_blocked():
    svc = _gemini_svc()
    _gemini_with(svc, response=NS(
        text='import pandas as pd\ndf = pd.read_',
        candidates=[NS(finish_reason=_enum('SAFETY'))],
        usage_metadata=_meta()))
    with pytest.raises(llm.BlockedGenerationError) as exc:
        svc.generate('draft', 'p', temperature=0.0)
    assert exc.value.user_message


def test_gemini_generate_blocked_prompt_raises_blocked():
    svc = _gemini_svc()
    _gemini_with(svc, response=NS(
        text=None, candidates=[],
        prompt_feedback=NS(block_reason=_enum('PROHIBITED_CONTENT')),
        usage_metadata=_meta(cand=0)))
    with pytest.raises(llm.BlockedGenerationError):
        svc.generate('lite', 'p', temperature=0.0)


def test_gemini_generate_no_candidates_raises_empty():
    svc = _gemini_svc()
    _gemini_with(svc, response=NS(text=None, candidates=[],
                                  prompt_feedback=None,
                                  usage_metadata=_meta(cand=0)))
    with pytest.raises(llm.EmptyGenerationError):
        svc.generate('lite', 'p', temperature=0.0)


def test_gemini_stream_safety_stop_raises_after_partial_text():
    svc = _gemini_svc()
    _gemini_with(svc, chunks=[
        NS(text='import pandas as pd\n', usage_metadata=None, candidates=None),
        NS(text='df = pd.read_', usage_metadata=_meta(),
           candidates=[NS(finish_reason=_enum('SAFETY'))]),
    ])
    usage = {}
    g = svc.stream('draft', 'p', usage_out=usage)
    got = [next(g), next(g)]
    with pytest.raises(llm.BlockedGenerationError):
        next(g)
    assert ''.join(got).startswith('import pandas')
    assert usage['input'] == 5     # billed partial stream still accounted


def test_gemini_stream_blocked_prompt_raises_blocked():
    svc = _gemini_svc()
    _gemini_with(svc, chunks=[NS(
        text=None, candidates=[], usage_metadata=_meta(cand=0),
        prompt_feedback=NS(block_reason=_enum('SAFETY')))])
    with pytest.raises(llm.BlockedGenerationError):
        list(svc.stream('draft', 'p', usage_out={}))


def test_anthropic_generate_refusal_raises_blocked():
    svc = llm.LLMService(Config(env='testing', llm_provider='anthropic',
                                anthropic_api_key='k'))
    svc._backend._client = NS(messages=NS(create=lambda **k: NS(
        content=[], stop_reason='refusal',
        usage=NS(input_tokens=5, output_tokens=0))))
    with pytest.raises(llm.BlockedGenerationError):
        svc.generate('lite', 'p', temperature=0.0)


def test_anthropic_stream_refusal_raises_blocked():
    svc = llm.LLMService(Config(env='testing', llm_provider='anthropic',
                                anthropic_api_key='k'))

    class FakeStream:
        text_stream = ['partial']

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return NS(stop_reason='refusal',
                      usage=NS(input_tokens=5, output_tokens=1))

    svc._backend._client = NS(messages=NS(stream=lambda **k: FakeStream()))
    with pytest.raises(llm.BlockedGenerationError):
        list(svc.stream('draft', 'p', usage_out={}))


def _openai_svc(create):
    svc = llm.LLMService(Config(env='testing', llm_provider='openai',
                                openai_api_key='k'))
    svc._backend._client = NS(chat=NS(completions=NS(create=create)))
    return svc


def test_openai_generate_refusal_raises_blocked():
    svc = _openai_svc(lambda **k: NS(
        choices=[NS(message=NS(content=None, refusal="I can't help with that."),
                    finish_reason='stop')],
        usage=NS(prompt_tokens=5, completion_tokens=3)))
    with pytest.raises(llm.BlockedGenerationError) as exc:
        svc.generate('lite', 'p', temperature=0.0)
    assert "can't help" in exc.value.user_message


def test_openai_generate_content_filter_raises_blocked():
    svc = _openai_svc(lambda **k: NS(
        choices=[NS(message=NS(content='partial', refusal=None),
                    finish_reason='content_filter')],
        usage=NS(prompt_tokens=5, completion_tokens=3)))
    with pytest.raises(llm.BlockedGenerationError):
        svc.generate('lite', 'p', temperature=0.0)


def test_openai_stream_content_filter_raises_blocked():
    svc = _openai_svc(lambda **k: iter([
        NS(choices=[NS(delta=NS(content='partial', refusal=None),
                       finish_reason=None)], usage=None),
        NS(choices=[NS(delta=NS(content=None, refusal=None),
                       finish_reason='content_filter')], usage=None),
    ]))
    with pytest.raises(llm.BlockedGenerationError):
        list(svc.stream('draft', 'p', usage_out={}))


def test_openai_stream_refusal_delta_raises_blocked():
    svc = _openai_svc(lambda **k: iter([
        NS(choices=[NS(delta=NS(content=None, refusal='No.'),
                       finish_reason='stop')], usage=None),
    ]))
    with pytest.raises(llm.BlockedGenerationError):
        list(svc.stream('draft', 'p', usage_out={}))


def test_empty_stream_raises_empty_but_reports_zero_usage():
    svc = _gemini_svc()
    _gemini_with(svc, chunks=[])
    usage = {}
    with pytest.raises(llm.EmptyGenerationError):
        list(svc.stream('draft', 'p', usage_out=usage))
    assert usage == {'model': 'gemini-3.8-flash', 'input': 0, 'output': 0}


def test_output_errors_share_a_base_with_a_user_message():
    for cls in (llm.TruncatedGenerationError, llm.BlockedGenerationError,
                llm.EmptyGenerationError):
        assert issubclass(cls, llm.LLMOutputError)
    assert llm.EmptyGenerationError('Gemini', 'm').user_message


# ---------------------------------------------------------------------------
# D3-3: thinking tokens are billed output; cached prompt tokens are reported
# ---------------------------------------------------------------------------
def test_gemini_usage_includes_thinking_tokens():
    svc = _gemini_svc()
    _gemini_with(svc, response=NS(
        text='ok', candidates=[NS(finish_reason=_enum('STOP'))],
        usage_metadata=_meta(prompt=500, cand=100, thoughts=900, cached=200)))
    res = svc.generate('draft', 'p', temperature=0.3)
    assert res.usage['output'] == 1000
    assert res.usage['thinking'] == 900
    assert res.usage['cached'] == 200
    assert svc.usage_snapshot()['gemini-3.8-flash']['output'] == 1000


def test_openai_usage_reports_cached_prompt_tokens():
    svc = _openai_svc(lambda **k: NS(
        choices=[NS(message=NS(content='ok', refusal=None),
                    finish_reason='stop')],
        usage=NS(prompt_tokens=50, completion_tokens=5,
                 prompt_tokens_details=NS(cached_tokens=40),
                 completion_tokens_details=NS(reasoning_tokens=3))))
    res = svc.generate('lite', 'p', temperature=0.3)
    assert res.usage['input'] == 50 and res.usage['cached'] == 40
    assert res.usage['output'] == 5 and res.usage['thinking'] == 3


def test_usage_breakdown_sums_cached_and_thinking():
    b = usage_breakdown({'model': 'm', 'input': 10, 'output': 7,
                         'thinking': 4, 'cached': 2},
                        {'model': 'm', 'input': 1, 'output': 1})
    assert b['thinking'] == 4 and b['cached'] == 2
    assert b['by_model']['m']['thinking'] == 4


# ---------------------------------------------------------------------------
# D3-11: usage of a stream cut short is recorded; cache hits are free
# ---------------------------------------------------------------------------
def test_truncated_stream_is_still_recorded_in_metrics():
    svc = _gemini_svc()
    _gemini_with(svc, chunks=[
        NS(text='def f(): retur', usage_metadata=_meta(prompt=5, cand=8),
           candidates=[NS(finish_reason=_enum('MAX_TOKENS'))])])
    with pytest.raises(llm.TruncatedGenerationError):
        list(svc.stream('draft', 'p'))
    snap = svc.usage_snapshot()['gemini-3.8-flash']
    assert snap == {'calls': 1, 'input': 5, 'output': 8}


def test_client_disconnect_mid_stream_is_recorded():
    svc = _gemini_svc()
    _gemini_with(svc, chunks=[
        NS(text='a', usage_metadata=_meta(prompt=5, cand=1), candidates=None),
        NS(text='b', usage_metadata=_meta(prompt=5, cand=2), candidates=None),
    ])
    g = svc.stream('draft', 'p')
    assert next(g) == 'a'
    g.close()                     # GeneratorExit, as on a client disconnect
    snap = svc.usage_snapshot()['gemini-3.8-flash']
    assert snap['calls'] == 1 and snap['input'] == 5


def test_cache_hit_is_reported_as_free():
    svc = _gemini_svc()
    calls = []

    def fake_gen(model, contents, *, temperature, json_mode):
        calls.append(1)
        return llm.LLMResult(text='{"decision":"pass"}',
                             usage={'model': model, 'input': 600, 'output': 2})

    svc._backend.generate = fake_gen
    first = svc.generate('lite', 'same', temperature=0.0)
    second = svc.generate('lite', 'same', temperature=0.0)
    assert len(calls) == 1
    assert first.usage['input'] == 600
    assert second.usage['input'] == 0 and second.usage['cache_hit'] is True
    b = usage_breakdown(first.usage, second.usage)
    assert b['calls'] == 1 and b['input'] == 600 and b['cache_hits'] == 1


# ---------------------------------------------------------------------------
# D3-5: bounded retries with backoff on Gemini; explicit timeouts everywhere
# ---------------------------------------------------------------------------
def test_gemini_client_retries_transient_statuses(monkeypatch):
    genai = pytest.importorskip('google.genai')
    captured = {}
    monkeypatch.setattr(genai, 'Client',
                        lambda **k: captured.update(k) or NS(**k))
    cfg = Config(env='testing', gemini_api_key='k')
    llm.GeminiBackend(cfg)._client_()
    opts = captured['http_options']
    assert opts.retry_options is not None
    assert opts.retry_options.attempts == cfg.llm_max_retries + 1 == 3
    assert {429, 500, 503, 504} <= set(opts.retry_options.http_status_codes)
    # The client timeout is not the server deadline: the deadline header is
    # pinned to the longest per-call budget, so a long Pro draft is not cut.
    assert int(opts.headers['X-Server-Timeout']) >= cfg.llm_pro_timeout_seconds
    assert opts.timeout == cfg.llm_timeout_seconds * 1000


def test_gemini_pro_max_calls_get_the_long_timeout():
    pytest.importorskip('google.genai')
    svc = _gemini_svc()
    cap = _gemini_with(svc, response=NS(
        text='ok', candidates=[NS(finish_reason=_enum('STOP'))],
        usage_metadata=_meta()))
    svc.generate('pro_max', 'p', temperature=0.3)
    assert cap['config'].http_options.timeout == \
        svc.config.llm_pro_timeout_seconds * 1000
    svc.generate('lite', 'p', temperature=0.3)
    assert cap['config'].http_options is None or \
        cap['config'].http_options.timeout == svc.config.llm_timeout_seconds * 1000


def test_anthropic_and_openai_clients_get_explicit_timeout_and_retries(
        monkeypatch):
    anthropic = pytest.importorskip('anthropic')
    openai = pytest.importorskip('openai')
    cap_a, cap_o = {}, {}
    monkeypatch.setattr(anthropic, 'Anthropic',
                        lambda **k: cap_a.update(k) or NS(**k))
    monkeypatch.setattr(openai, 'OpenAI',
                        lambda **k: cap_o.update(k) or NS(**k))
    cfg = Config(env='testing', llm_provider='anthropic',
                 anthropic_api_key='a', openai_api_key='o')
    llm.AnthropicBackend(cfg)._client_()
    llm.OpenAIBackend(cfg)._client_()
    for cap in (cap_a, cap_o):
        assert cap['max_retries'] == cfg.llm_max_retries
        assert cap['timeout'] == float(cfg.llm_timeout_seconds)
    # worst case per call stays well under the SDK default of 600 s x 3
    assert cfg.llm_timeout_seconds * (cfg.llm_max_retries + 1) <= 600


def test_anthropic_pro_max_call_passes_long_per_request_timeout():
    cfg = Config(env='testing', llm_provider='anthropic', anthropic_api_key='k',
                 model_pro_max='claude-big', model_pro='claude-mid')
    svc = llm.LLMService(cfg)
    cap = {}
    svc._backend._client = NS(messages=NS(create=lambda **k: cap.update(k) or NS(
        content=[NS(type='text', text='ok')], stop_reason='end_turn',
        usage=NS(input_tokens=1, output_tokens=1))))
    svc.generate('pro_max', 'p', temperature=0.3)
    assert cap['timeout'] == float(cfg.llm_pro_timeout_seconds)
    svc.generate('draft', 'p', temperature=0.3)
    assert cap['timeout'] == float(cfg.llm_timeout_seconds)


# ---------------------------------------------------------------------------
# D3-12 / D3-13: temperature handling per model
# ---------------------------------------------------------------------------
def test_openai_temperature_rejection_is_memoized_per_model():
    calls = []

    class BadRequest(Exception):
        status_code = 400

    def create(**k):
        calls.append(k)
        if 'temperature' in k:
            raise BadRequest("Unsupported value: 'temperature' does not "
                             "support 0.2 with this model.")
        return NS(choices=[NS(message=NS(content='ok', refusal=None),
                              finish_reason='stop')],
                  usage=NS(prompt_tokens=1, completion_tokens=1))

    svc = _openai_svc(create)
    svc.generate('lite', 'a', temperature=0.2)
    svc.generate('lite', 'b', temperature=0.2)
    assert len(calls) == 3          # one failed probe, then never again


def test_openai_unrelated_bad_request_is_not_retried():
    class BadRequest(Exception):
        status_code = 400

    def create(**k):
        raise BadRequest('context_length_exceeded')

    svc = _openai_svc(create)
    with pytest.raises(BadRequest):
        svc.generate('lite', 'a', temperature=0.2)


def _anthropic_capture(model_lite='claude-haiku-4-5', fail_temperature=False):
    cfg = Config(env='testing', llm_provider='anthropic', anthropic_api_key='k',
                 model_flash_lite=model_lite)
    svc = llm.LLMService(cfg)
    calls = []

    class BadRequest(Exception):
        status_code = 400

    def create(**k):
        calls.append(k)
        if fail_temperature and 'temperature' in (k.get('extra_body') or {}):
            raise BadRequest('temperature is not supported for this model')
        return NS(content=[NS(type='text', text='ok')], stop_reason='end_turn',
                  usage=NS(input_tokens=1, output_tokens=1))

    svc._backend._client = NS(messages=NS(create=create))
    return svc, calls


def test_anthropic_sends_temperature_to_models_that_accept_it():
    # Claude before 4.7 still takes a temperature; SDK 1.x removed the kwarg,
    # so it travels in extra_body.
    svc, calls = _anthropic_capture('claude-haiku-4-5')
    svc.generate('lite', 'p', temperature=0.0)
    assert calls[-1]['extra_body'] == {'temperature': 0.0}
    assert 'temperature' not in calls[-1]


@pytest.mark.parametrize('model', [
    'claude-opus-4-7', 'claude-opus-4-8', 'claude-opus-5-5',
    'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-1'])
def test_anthropic_omits_temperature_for_known_rejecting_models(model):
    # Anthropic deprecated sampling params for Claude 4.7 and later (400 on a
    # non-default value).
    svc, calls = _anthropic_capture(model)
    svc.generate('lite', 'p', temperature=0.0)
    assert 'temperature' not in calls[-1]
    assert 'extra_body' not in calls[-1]


@pytest.mark.parametrize('model', [
    'claude-haiku-4-5', 'claude-sonnet-4-6', 'claude-opus-4-5-20251101',
    'claude-opus-4-20250514'])
def test_anthropic_pre_4_7_models_accept_temperature(model):
    assert not llm.AnthropicBackend._sampling_deprecated(model)


def test_anthropic_temperature_rejection_retries_once_and_memoizes():
    svc, calls = _anthropic_capture('claude-future-1', fail_temperature=True)
    assert svc.generate('lite', 'a', temperature=0.2).text == 'ok'
    svc.generate('lite', 'b', temperature=0.2)
    assert len(calls) == 3
    assert 'extra_body' not in calls[-1]


# ---------------------------------------------------------------------------
# D3-16: reasoning tokens must not truncate OpenAI scripts
# ---------------------------------------------------------------------------
def test_openai_stream_uses_the_larger_stream_budget():
    cap = {}

    def create(**k):
        cap.update(k)
        return iter([NS(choices=[NS(delta=NS(content='x', refusal=None),
                                    finish_reason='stop')], usage=None)])

    svc = _openai_svc(create)
    list(svc.stream('draft', 'p'))
    assert cap['max_completion_tokens'] == svc.config.openai_stream_max_tokens
    assert svc.config.openai_stream_max_tokens >= 32000
    assert svc.config.openai_max_tokens >= 16000
