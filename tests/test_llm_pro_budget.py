"""Pro-model output budget (release audit 2026-10-16, B-14).

Not live-verified (that needs a paid call): on Anthropic the Pro-mode model
thinks on every call and thinking counts against max_tokens, so the report
draft is now streamed (the larger stream budget, no non-streaming timeout)
and the 'pro_max' model gets at least ``PRO_MAX_MIN_TOKENS``.
"""
import types

from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv

from statlee import llm
from statlee.config import Config

NS = types.SimpleNamespace


def _anthropic(model_pro_max, **cfg):
    svc = llm.LLMService(Config(env='testing', llm_provider='anthropic',
                                anthropic_api_key='k',
                                model_pro_max=model_pro_max, **cfg))
    calls = []

    class Manager:
        text_stream = ['ok']
        current_message_snapshot = None

        def __init__(self, kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return NS(stop_reason='end_turn',
                      usage=NS(input_tokens=1, output_tokens=1))

    def create(**k):
        calls.append(k)
        return NS(content=[NS(type='text', text='ok')], stop_reason='end_turn',
                  usage=NS(input_tokens=1, output_tokens=1))

    svc._backend._client = NS(messages=NS(stream=lambda **k: Manager(k),
                                          create=create))
    return svc, calls


def test_thinking_pro_model_gets_the_larger_budget():
    svc, calls = _anthropic('claude-fable-5-1')
    list(svc.stream('pro_max', 'p'))
    assert calls[-1]['max_tokens'] >= llm.AnthropicBackend.PRO_MAX_MIN_TOKENS
    svc.generate('pro_max', 'p')
    assert calls[-1]['max_tokens'] >= llm.AnthropicBackend.PRO_MAX_MIN_TOKENS


def test_other_roles_keep_the_configured_budget():
    svc, calls = _anthropic('claude-fable-5-1', anthropic_max_tokens=8192,
                            anthropic_stream_max_tokens=16000)
    list(svc.stream('draft', 'p'))
    assert calls[-1]['max_tokens'] == 16000
    svc.generate('lite', 'p')
    assert calls[-1]['max_tokens'] == 8192


def test_an_older_pinned_pro_model_keeps_the_configured_budget():
    # A pre-4.7 id may have a smaller output ceiling; never raise past config.
    svc, calls = _anthropic('claude-haiku-4-5', anthropic_stream_max_tokens=16000)
    list(svc.stream('pro_max', 'p'))
    assert calls[-1]['max_tokens'] == 16000


def test_report_draft_is_streamed_not_a_single_call(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    generated = []
    original = fake_llm.generate

    def spy(role, contents, **kw):
        generated.append(fake_llm._kind(fake_llm._flatten(contents)))
        return original(role, contents, **kw)
    fake_llm.generate = spy
    resp = post_json(client, '/generate_report',
                     {'output': 'coef = 1.2, p = 0.01',
                      'interpretation': 'A strong effect.'})
    events = sse_events(resp)
    assert any(e.get('type') == 'done' for e in events)
    assert 'report_draft' not in generated
    roles = {k: r for r, k, _t in fake_llm.calls}
    assert roles['report_draft'] == 'pro_max'
    done = next(e for e in events if e.get('type') == 'done')
    assert done['usage']                 # the streamed draft is still counted
