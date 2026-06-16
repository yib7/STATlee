"""LLM service: priority role escalation and the deterministic-call cache
(workstream B). These exercise the real ``LLMService`` with the Gemini call
monkeypatched out, so no network or API key is needed."""
from statlee import llm
from statlee.config import Config


def _svc():
    return llm.LLMService(Config(env='testing'))


def test_priority_escalates_roles_one_tier():
    svc = _svc()
    cfg = svc.config
    # Without priority: roles map to their configured tiers.
    assert svc._resolve('lite') == cfg.model_flash_lite
    assert svc._resolve('flash') == cfg.model_flash
    # With priority: each role steps up toward the strongest model.
    assert svc._resolve('lite', priority=True) == cfg.model_flash
    assert svc._resolve('flash', priority=True) == cfg.model_pro
    # 'pro' is already the top tier — escalation is a no-op.
    assert svc._resolve('pro', priority=True) == cfg.model_pro


def test_generate_caches_deterministic_calls(monkeypatch):
    svc = _svc()
    seen = []

    def fake_gen(model, contents, temperature, json_mode):
        seen.append(contents)
        return llm.LLMResult(text='ok', usage={'model': model, 'input': 1, 'output': 1})

    monkeypatch.setattr(svc, '_generate_gemini', fake_gen)

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
        svc, '_generate_gemini',
        lambda *a, **k: (seen.append(1), llm.LLMResult(text='x'))[1])
    svc.generate('lite', 'p', temperature=0.5)
    svc.generate('lite', 'p', temperature=0.5)
    assert len(seen) == 2


def test_priority_call_uses_separate_cache_entry(monkeypatch):
    """A priority call resolves to a different model, so it must not collide
    with the non-priority cache entry for the same prompt."""
    svc = _svc()
    models = []

    def fake_gen(model, contents, temperature, json_mode):
        models.append(model)
        return llm.LLMResult(text='ok', usage={'model': model})

    monkeypatch.setattr(svc, '_generate_gemini', fake_gen)
    svc.generate('lite', 'p', temperature=0.0)
    svc.generate('lite', 'p', temperature=0.0, priority=True)
    assert models == [svc.config.model_flash_lite, svc.config.model_flash]
