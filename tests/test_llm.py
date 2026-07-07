"""LLM service: role→model resolution, the deterministic-call cache, and the
per-provider backends (Gemini / Anthropic / OpenAI). These exercise the real
``LLMService`` with the provider client faked out, so no network or API key is
needed."""
import types

import pytest

from statlee import llm
from statlee.config import Config


def _svc():
    return llm.LLMService(Config(env='testing'))


def test_roles_resolve_to_configured_models():
    svc = _svc()
    cfg = svc.config
    assert svc._resolve('lite') == cfg.model_flash_lite
    assert svc._resolve('flash') == cfg.model_flash
    assert svc._resolve('pro') == cfg.model_pro
    assert svc._resolve('draft') == cfg.model_pro       # default code generation
    # "Pro mode" routes code generation to the bigger pro_max model.
    assert svc._resolve('pro_max') == cfg.model_pro_max


def test_unknown_role_raises():
    with pytest.raises(ValueError):
        _svc()._resolve('nope')


def test_generate_caches_deterministic_calls(monkeypatch):
    svc = _svc()
    seen = []

    def fake_gen(model, contents, temperature, json_mode):
        seen.append(contents)
        return llm.LLMResult(text='ok', usage={'model': model, 'input': 1, 'output': 1})

    monkeypatch.setattr(svc._backend, 'generate', fake_gen)

    # temperature 0 → deterministic → cached on the second identical call.
    svc.generate('lite', 'same prompt', temperature=0.0)
    svc.generate('lite', 'same prompt', temperature=0.0)
    assert len(seen) == 1

    # Different content → cache miss.
    svc.generate('lite', 'other prompt', temperature=0.0)
    assert len(seen) == 2


def test_nonzero_temperature_is_not_cached(monkeypatch):
    svc = _svc()
    seen = []
    monkeypatch.setattr(
        svc._backend, 'generate',
        lambda *a, **k: (seen.append(1), llm.LLMResult(text='x'))[1])
    svc.generate('lite', 'p', temperature=0.5)
    svc.generate('lite', 'p', temperature=0.5)
    assert len(seen) == 2


def test_different_roles_use_separate_cache_entries(monkeypatch):
    """pro_max and draft resolve to different models, so an identical prompt
    under each role must hit the backend twice, not collide in the cache."""
    svc = _svc()
    models = []

    def fake_gen(model, contents, temperature, json_mode):
        models.append(model)
        return llm.LLMResult(text='ok', usage={'model': model})

    monkeypatch.setattr(svc._backend, 'generate', fake_gen)
    svc.generate('draft', 'p', temperature=0.0)
    svc.generate('pro_max', 'p', temperature=0.0)
    assert models == [svc.config.model_pro, svc.config.model_pro_max]


def test_service_uses_gemini_backend():
    assert isinstance(_svc()._backend, llm.GeminiBackend)


def test_gemini_translates_mediapart_to_part():
    pytest.importorskip('google.genai')
    from google.genai import types as gtypes
    out = llm.GeminiBackend._to_contents(
        ['caption', llm.MediaPart(data=b'\x89PNG', mime_type='image/png')])
    assert out[0] == 'caption'
    assert isinstance(out[1], gtypes.Part)


def test_gemini_client_has_http_timeout(monkeypatch):
    """A stalled Gemini connection must not hang a worker thread forever —
    the client is built with an explicit http_options timeout (P1-1)."""
    genai = pytest.importorskip('google.genai')
    captured = {}
    monkeypatch.setattr(
        genai, 'Client',
        lambda **k: captured.update(k) or types.SimpleNamespace(**k))

    backend = llm.GeminiBackend(Config(env='testing', gemini_api_key='dummy-key'))
    backend._client_()

    assert 'http_options' in captured
    http_options = captured['http_options']
    assert http_options.timeout == 120_000


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
def test_backend_selected_by_provider():
    assert isinstance(
        llm.LLMService(Config(env='testing'))._backend, llm.GeminiBackend)
    assert isinstance(
        llm.LLMService(Config(env='testing', llm_provider='anthropic'))._backend,
        llm.AnthropicBackend)
    assert isinstance(
        llm.LLMService(Config(env='testing', llm_provider='openai'))._backend,
        llm.OpenAIBackend)


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------
def _anthropic_svc_with_capture():
    cfg = Config(env='testing', llm_provider='anthropic', anthropic_api_key='k')
    svc = llm.LLMService(cfg)
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type='text', text='hi there')],
            usage=types.SimpleNamespace(input_tokens=11, output_tokens=4))

    svc._backend._client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=fake_create))
    return svc, captured


def test_anthropic_generate_maps_text_and_usage():
    svc, captured = _anthropic_svc_with_capture()
    res = svc.generate('lite', 'hello', temperature=0.0)
    assert res.text == 'hi there'
    assert res.usage['input'] == 11 and res.usage['output'] == 4
    assert svc.usage_snapshot()[svc._resolve('lite')]['output'] == 4
    assert 'temperature' not in captured   # Claude rejects sampling params


def test_anthropic_json_mode_sets_system():
    svc, captured = _anthropic_svc_with_capture()
    svc.generate('lite', 'give me json', temperature=0.2, json_mode=True)
    assert 'system' in captured and 'JSON' in captured['system']


def test_anthropic_builds_image_block():
    svc, captured = _anthropic_svc_with_capture()
    svc.generate('flash', ['look at this', llm.MediaPart(
        data=b'\x89PNG', mime_type='image/png')], temperature=0.3)
    content = captured['messages'][0]['content']
    assert content[0] == {'type': 'text', 'text': 'look at this'}
    assert content[1]['type'] == 'image'
    assert content[1]['source']['media_type'] == 'image/png'
    assert content[1]['source']['type'] == 'base64' and content[1]['source']['data']


def test_anthropic_builds_pdf_document_block():
    svc, captured = _anthropic_svc_with_capture()
    svc.generate('flash', [llm.MediaPart(
        data=b'%PDF-1.4', mime_type='application/pdf'), 'extract it'],
        temperature=0.1)
    content = captured['messages'][0]['content']
    assert content[0]['type'] == 'document'
    assert content[0]['source']['media_type'] == 'application/pdf'
    assert content[1] == {'type': 'text', 'text': 'extract it'}


def test_anthropic_rejects_unknown_content_type():
    svc, _ = _anthropic_svc_with_capture()
    with pytest.raises(RuntimeError, match='Unsupported content item'):
        svc.generate('lite', ['describe this', object()], temperature=0.2)


def test_anthropic_stream_yields_and_records_usage():
    cfg = Config(env='testing', llm_provider='anthropic', anthropic_api_key='k')
    svc = llm.LLMService(cfg)

    class FakeStream:
        text_stream = ['He', 'llo']

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return types.SimpleNamespace(
                usage=types.SimpleNamespace(input_tokens=7, output_tokens=3))

    svc._backend._client = types.SimpleNamespace(
        messages=types.SimpleNamespace(stream=lambda **k: FakeStream()))

    usage = {}
    out = list(svc.stream('draft', 'go', usage_out=usage))
    assert ''.join(out) == 'Hello'
    assert usage['input'] == 7 and usage['output'] == 3


def test_gemini_stream_yields_and_records_usage():
    """Gemini stream fills usage_out from the chunks that carry usage_metadata,
    accumulating deltas along the way (P1-5: explicit usage contract)."""
    svc = llm.LLMService(Config(env='testing', gemini_api_key='k'))

    chunks = [
        types.SimpleNamespace(text='He', usage_metadata=None),
        types.SimpleNamespace(
            text='llo',
            usage_metadata=types.SimpleNamespace(
                prompt_token_count=9, candidates_token_count=4)),
    ]
    svc._backend._client = types.SimpleNamespace(
        models=types.SimpleNamespace(
            generate_content_stream=lambda **k: iter(chunks)))

    usage = {}
    out = list(svc.stream('draft', 'go', usage_out=usage))
    assert ''.join(out) == 'Hello'
    assert usage['input'] == 9 and usage['output'] == 4


def test_gemini_empty_stream_reports_zero_usage():
    """An empty Gemini stream must report a clean zero usage, not None or stale."""
    svc = llm.LLMService(Config(env='testing', gemini_api_key='k'))
    svc._backend._client = types.SimpleNamespace(
        models=types.SimpleNamespace(
            generate_content_stream=lambda **k: iter([])))

    usage = {}
    out = list(svc.stream('draft', 'go', usage_out=usage))
    assert out == []
    assert usage == {'model': 'gemini-3.5-flash', 'input': 0, 'output': 0}


def test_anthropic_client_auth_modes(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    captured = {}
    monkeypatch.setattr(
        anthropic, 'Anthropic',
        lambda **k: captured.update(k) or types.SimpleNamespace(**k))

    api_backend = llm.AnthropicBackend(
        Config(env='testing', llm_provider='anthropic', anthropic_api_key='sekret'))
    api_backend._client_()
    assert captured.get('api_key') == 'sekret'

    captured.clear()
    sub_backend = llm.AnthropicBackend(Config(env='testing', llm_provider='anthropic'))
    sub_backend._client_()
    assert 'api_key' not in captured
    assert captured['default_headers']['anthropic-beta'] == 'oauth-2025-04-20'


# ---------------------------------------------------------------------------
# OpenAI backend
# ---------------------------------------------------------------------------
def _openai_svc_with_capture(text='hi there', in_tok=9, out_tok=5):
    cfg = Config(env='testing', llm_provider='openai', openai_api_key='k')
    svc = llm.LLMService(cfg)
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content=text))],
            usage=types.SimpleNamespace(
                prompt_tokens=in_tok, completion_tokens=out_tok))

    svc._backend._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=fake_create)))
    return svc, captured


def test_openai_generate_maps_text_and_usage():
    svc, captured = _openai_svc_with_capture()
    res = svc.generate('lite', 'hello', temperature=0.0)
    assert res.text == 'hi there'
    assert res.usage['input'] == 9 and res.usage['output'] == 5
    assert svc.usage_snapshot()[svc._resolve('lite')]['output'] == 5
    assert captured['max_completion_tokens'] == svc.config.openai_max_tokens
    assert captured['temperature'] == 0.0


def test_openai_json_mode_sets_response_format():
    svc, captured = _openai_svc_with_capture()
    svc.generate('lite', 'json pls', temperature=0.2, json_mode=True)
    assert captured['response_format'] == {'type': 'json_object'}


def test_openai_builds_image_and_pdf_parts():
    img = llm.OpenAIBackend._content(
        ['look', llm.MediaPart(data=b'\x89PNG', mime_type='image/png')])
    assert img[0] == {'type': 'text', 'text': 'look'}
    assert img[1]['type'] == 'image_url'
    assert img[1]['image_url']['url'].startswith('data:image/png;base64,')
    pdf = llm.OpenAIBackend._content(
        [llm.MediaPart(data=b'%PDF', mime_type='application/pdf')])
    assert pdf[0]['type'] == 'file'
    assert pdf[0]['file']['file_data'].startswith('data:application/pdf;base64,')


def test_openai_rejects_unknown_content_type():
    svc, _ = _openai_svc_with_capture()
    with pytest.raises(RuntimeError, match='Unsupported content item'):
        svc.generate('lite', ['describe', object()], temperature=0.2)


def test_openai_stream_yields_and_records_usage():
    cfg = Config(env='testing', llm_provider='openai', openai_api_key='k')
    svc = llm.LLMService(cfg)

    def fake_create(**kwargs):
        return iter([
            types.SimpleNamespace(choices=[types.SimpleNamespace(
                delta=types.SimpleNamespace(content='He'))], usage=None),
            types.SimpleNamespace(choices=[types.SimpleNamespace(
                delta=types.SimpleNamespace(content='llo'))], usage=None),
            types.SimpleNamespace(choices=[], usage=types.SimpleNamespace(
                prompt_tokens=6, completion_tokens=2)),
        ])

    svc._backend._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=fake_create)))

    usage = {}
    out = list(svc.stream('draft', 'go', usage_out=usage))
    assert ''.join(out) == 'Hello'
    assert usage['input'] == 6 and usage['output'] == 2


def test_openai_drops_temperature_on_sampling_error():
    """gpt-5/o-series reject non-default temperature; the backend retries once
    without it rather than failing the call."""
    cfg = Config(env='testing', llm_provider='openai', openai_api_key='k')
    svc = llm.LLMService(cfg)
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if 'temperature' in kwargs:
            raise RuntimeError(
                "Unsupported value: 'temperature' does not support 0.2")
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content='ok'))],
            usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    svc._backend._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=fake_create)))

    res = svc.generate('lite', 'hi', temperature=0.2)
    assert res.text == 'ok'
    assert len(calls) == 2 and 'temperature' not in calls[1]
