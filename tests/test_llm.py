"""LLM service: role→model resolution and the deterministic-call cache. These
exercise the real ``LLMService`` with the Gemini call monkeypatched out, so no
network or API key is needed."""
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
