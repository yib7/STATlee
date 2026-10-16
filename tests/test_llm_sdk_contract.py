"""The keyword arguments each backend builds must bind to the INSTALLED provider
SDK's method signatures. The other LLM tests swap the SDK client for a fake,
so a pinned-SDK bump that renames or drops a parameter (anthropic 1.0 removed
``temperature`` from ``messages.create``) would otherwise pass the suite and
fail only on a live call. Nothing here opens a connection."""
import inspect

import pytest

from statlee import llm
from statlee.config import Config


def _bind(method, kwargs, *, unbound=False):
    """Bind ``kwargs`` to ``method``'s real signature (raises TypeError on an
    unknown or missing parameter). ``unbound`` methods get a placeholder self."""
    sig = inspect.signature(method)
    if unbound:
        sig.bind(None, **kwargs)
    else:
        sig.bind(**kwargs)


@pytest.mark.parametrize('model', ['claude-haiku-4-5', 'claude-opus-5-5'])
@pytest.mark.parametrize('json_mode', [False, True])
def test_anthropic_kwargs_bind_to_installed_sdk(model, json_mode):
    anthropic = pytest.importorskip('anthropic')
    backend = llm.AnthropicBackend(
        Config(env='testing', llm_provider='anthropic', anthropic_api_key='k'))
    kwargs = backend._kwargs(model, 'hello', 0.2, 1024)
    if json_mode:
        kwargs['system'] = backend.JSON_SYSTEM
    messages = anthropic.resources.messages.Messages
    _bind(messages.create, kwargs, unbound=True)
    _bind(messages.stream, kwargs, unbound=True)


@pytest.mark.parametrize('stream', [False, True])
def test_openai_kwargs_bind_to_installed_sdk(stream):
    openai = pytest.importorskip('openai')
    cfg = Config(env='testing', llm_provider='openai', openai_api_key='k')
    backend = llm.OpenAIBackend(cfg)
    captured = {}

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    captured.update(kw)
                    return None

    backend._client = _Client()
    backend._create(model=cfg.model_flash_lite, contents='hi', temperature=0.2,
                    json_mode=not stream, stream=stream)
    completions = openai.resources.chat.completions.Completions
    _bind(completions.create, captured, unbound=True)


def test_anthropic_client_constructor_kwargs_bind():
    anthropic = pytest.importorskip('anthropic')
    _bind(anthropic.Anthropic, {'api_key': 'k', 'timeout': 120.0,
                                'max_retries': 2})
    _bind(anthropic.Anthropic, {'default_headers': {'anthropic-beta': 'x'},
                                'timeout': 120.0, 'max_retries': 2})


def test_bind_rejects_a_removed_parameter():
    # Guards the guard: a kwarg the SDK no longer takes must fail to bind.
    anthropic = pytest.importorskip('anthropic')
    with pytest.raises(TypeError):
        _bind(anthropic.resources.messages.Messages.create,
              {'model': 'm', 'max_tokens': 1, 'messages': [],
               'no_such_parameter': 0.2}, unbound=True)


def test_openai_client_constructor_kwargs_bind():
    openai = pytest.importorskip('openai')
    _bind(openai.OpenAI, {'api_key': 'k', 'timeout': 120.0, 'max_retries': 2})
