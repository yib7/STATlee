"""Role-based LLM service with usage tracking (roadmap 3.3 / 3.4).

Every model call in the app goes through this module. Calls are addressed by
*role* — 'pro', 'pro_max', 'flash', 'lite', or 'draft' — and the role→model
mapping lives in config, so swapping a model is a config change, not a code
change. Code generation normally uses 'draft'; the "Pro mode" toggle routes it
to 'pro_max' (a bigger, stronger model) instead.

``LLMService`` owns role resolution, the deterministic-call cache, and usage
accounting; the per-provider wire protocol lives in a backend
(``GeminiBackend`` / ``AnthropicBackend`` / ``OpenAIBackend``) selected from
``config.llm_provider`` (default ``gemini``). Gemini stays the default; the
other two let a self-hoster bring their own Claude or OpenAI key.
"""
import hashlib
import logging
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger('statlee.llm')

_GEN_CACHE_MAX = 256


@dataclass
class LLMResult:
    text: str = ''
    usage: dict = field(default_factory=dict)  # {input, output, model}


@dataclass
class MediaPart:
    """Provider-neutral binary content (image or PDF) for multimodal prompts.

    Routes pass these inside a ``contents`` list instead of provider-specific
    objects; each backend translates them to its own SDK shape.
    """
    data: bytes
    mime_type: str   # e.g. 'image/png', 'application/pdf'


# ---------------------------------------------------------------------------
# Gemini backend — owns only the wire protocol.
# ---------------------------------------------------------------------------
class GeminiBackend:
    def __init__(self, config):
        self.config = config
        self._client = None

    def _client_(self):
        if self._client is None:
            from google import genai
            if not self.config.gemini_api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not configured on the server.")
            self._client = genai.Client(api_key=self.config.gemini_api_key)
        return self._client

    @staticmethod
    def _usage(response, model):
        meta = getattr(response, 'usage_metadata', None)
        return {
            'model': model,
            'input': getattr(meta, 'prompt_token_count', 0) or 0,
            'output': getattr(meta, 'candidates_token_count', 0) or 0,
        }

    @staticmethod
    def _to_contents(contents):
        """Translate any MediaPart in the list into a google-genai Part."""
        if isinstance(contents, str):
            return contents
        out = []
        for c in contents:
            if isinstance(c, MediaPart):
                from google.genai import types
                out.append(types.Part.from_bytes(
                    data=c.data, mime_type=c.mime_type))
            else:
                out.append(c)
        return out

    def generate(self, model, contents, *, temperature, json_mode):
        from google.genai import types
        kwargs = {'temperature': temperature}
        if json_mode:
            kwargs['response_mime_type'] = 'application/json'
        response = self._client_().models.generate_content(
            model=model, contents=self._to_contents(contents),
            config=types.GenerateContentConfig(**kwargs))
        usage = self._usage(response, model)
        logger.info("llm call provider=gemini model=%s in=%s out=%s",
                    model, usage['input'], usage['output'])
        return LLMResult(text=(response.text or '').strip(), usage=usage)

    def stream(self, model, contents, *, temperature, usage_out):
        from google.genai import types
        stream = self._client_().models.generate_content_stream(
            model=model, contents=self._to_contents(contents),
            config=types.GenerateContentConfig(temperature=temperature))
        last_chunk = None
        for chunk in stream:
            last_chunk = chunk
            delta = getattr(chunk, 'text', None)
            if delta:
                yield delta
        usage = (self._usage(last_chunk, model) if last_chunk
                 else {'model': model, 'input': 0, 'output': 0})
        if usage_out is not None:
            usage_out.update(usage)


# ---------------------------------------------------------------------------
# Anthropic / Claude backend.
# ---------------------------------------------------------------------------
class AnthropicBackend:
    """Anthropic/Claude backend. Auth resolves from config: an explicit API key
    (deploy), else a bare client that picks up ANTHROPIC_AUTH_TOKEN or a local
    `claude`/`ant` OAuth login (local subscription)."""

    JSON_SYSTEM = ("Output only a single valid JSON value — no markdown, "
                   "no code fences, no prose.")

    def __init__(self, config):
        self.config = config
        self._client = None

    def _client_(self):
        if self._client is None:
            import anthropic
            if self.config.anthropic_api_key:
                self._client = anthropic.Anthropic(
                    api_key=self.config.anthropic_api_key)
            else:
                self._client = anthropic.Anthropic(
                    default_headers={'anthropic-beta': 'oauth-2025-04-20'})
        return self._client

    @staticmethod
    def _content(contents):
        """Build the Anthropic user-message ``content`` from a str or a list of
        str / MediaPart items (text, base64 image, or base64 PDF document)."""
        if isinstance(contents, str):
            return contents
        import base64
        blocks = []
        for c in contents:
            if isinstance(c, str):
                blocks.append({'type': 'text', 'text': c})
            elif isinstance(c, MediaPart):
                b64 = base64.b64encode(c.data).decode('ascii')
                block_type = ('document' if c.mime_type == 'application/pdf'
                              else 'image')
                blocks.append({
                    'type': block_type,
                    'source': {'type': 'base64',
                               'media_type': c.mime_type, 'data': b64},
                })
            else:
                raise RuntimeError(
                    "Unsupported content item for the Anthropic provider: "
                    f"{type(c).__name__}")
        return blocks

    @staticmethod
    def _usage(message, model):
        u = getattr(message, 'usage', None)
        return {
            'model': model,
            'input': getattr(u, 'input_tokens', 0) or 0,
            'output': getattr(u, 'output_tokens', 0) or 0,
        }

    def generate(self, model, contents, *, temperature, json_mode):
        # temperature is intentionally ignored — sampling params are rejected on
        # current Claude models; determinism is handled by LLMService's cache.
        kwargs = {
            'model': model,
            'max_tokens': self.config.anthropic_max_tokens,
            'messages': [{'role': 'user', 'content': self._content(contents)}],
        }
        if json_mode:
            kwargs['system'] = self.JSON_SYSTEM
        message = self._client_().messages.create(**kwargs)
        text = next((b.text for b in message.content
                     if getattr(b, 'type', None) == 'text'), '')
        usage = self._usage(message, model)
        logger.info("llm call provider=anthropic model=%s in=%s out=%s",
                    model, usage['input'], usage['output'])
        return LLMResult(text=(text or '').strip(), usage=usage)

    def stream(self, model, contents, *, temperature, usage_out):
        with self._client_().messages.stream(
                model=model,
                max_tokens=self.config.anthropic_stream_max_tokens,
                messages=[{'role': 'user',
                           'content': self._content(contents)}]) as stream:
            for delta in stream.text_stream:
                if delta:
                    yield delta
            final = stream.get_final_message()
        usage = self._usage(final, model)
        if usage_out is not None:
            usage_out.update(usage)


# ---------------------------------------------------------------------------
# OpenAI backend.
# ---------------------------------------------------------------------------
class OpenAIBackend:
    """OpenAI backend (Chat Completions). ``MediaPart`` maps to ``image_url``
    parts (images) and ``file`` parts (base64 PDFs). Some newer models (gpt-5,
    o-series) reject non-default sampling params, so a ``temperature`` error
    triggers one retry without it — determinism for cached temp-0 calls is
    preserved by ``LLMService``'s cache regardless."""

    def __init__(self, config):
        self.config = config
        self._client = None

    def _client_(self):
        if self._client is None:
            import openai
            if not self.config.openai_api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY is not configured on the server.")
            self._client = openai.OpenAI(api_key=self.config.openai_api_key)
        return self._client

    @staticmethod
    def _content(contents):
        """Build the Chat Completions message ``content`` from a str or a list
        of str / MediaPart items (text, image_url, or PDF file part)."""
        if isinstance(contents, str):
            return contents
        import base64
        parts = []
        for c in contents:
            if isinstance(c, str):
                parts.append({'type': 'text', 'text': c})
            elif isinstance(c, MediaPart):
                b64 = base64.b64encode(c.data).decode('ascii')
                data_uri = f'data:{c.mime_type};base64,{b64}'
                if c.mime_type == 'application/pdf':
                    parts.append({'type': 'file', 'file': {
                        'filename': 'document.pdf', 'file_data': data_uri}})
                else:
                    parts.append({'type': 'image_url',
                                  'image_url': {'url': data_uri}})
            else:
                raise RuntimeError(
                    "Unsupported content item for the OpenAI provider: "
                    f"{type(c).__name__}")
        return parts

    @staticmethod
    def _usage(response, model):
        u = getattr(response, 'usage', None)
        return {
            'model': model,
            'input': getattr(u, 'prompt_tokens', 0) or 0,
            'output': getattr(u, 'completion_tokens', 0) or 0,
        }

    def _create(self, *, model, contents, temperature, json_mode, stream):
        kwargs = {
            'model': model,
            'messages': [{'role': 'user', 'content': self._content(contents)}],
            'max_completion_tokens': self.config.openai_max_tokens,
        }
        if temperature is not None:
            kwargs['temperature'] = temperature
        if json_mode:
            kwargs['response_format'] = {'type': 'json_object'}
        if stream:
            kwargs['stream'] = True
            kwargs['stream_options'] = {'include_usage': True}
        try:
            return self._client_().chat.completions.create(**kwargs)
        except Exception as exc:
            # gpt-5 / o-series only accept the default temperature; drop it and
            # retry once rather than failing the call.
            if temperature is not None and 'temperature' in str(exc).lower():
                kwargs.pop('temperature', None)
                return self._client_().chat.completions.create(**kwargs)
            raise

    def generate(self, model, contents, *, temperature, json_mode):
        response = self._create(model=model, contents=contents,
                                temperature=temperature, json_mode=json_mode,
                                stream=False)
        text = (response.choices[0].message.content or ''
                if response.choices else '')
        usage = self._usage(response, model)
        logger.info("llm call provider=openai model=%s in=%s out=%s",
                    model, usage['input'], usage['output'])
        return LLMResult(text=text.strip(), usage=usage)

    def stream(self, model, contents, *, temperature, usage_out):
        events = self._create(model=model, contents=contents,
                              temperature=temperature, json_mode=False,
                              stream=True)
        usage = {'model': model, 'input': 0, 'output': 0}
        for chunk in events:
            if getattr(chunk, 'usage', None):
                usage = self._usage(chunk, model)
            if getattr(chunk, 'choices', None):
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        if usage_out is not None:
            usage_out.update(usage)


def _make_backend(config):
    provider = (getattr(config, 'llm_provider', 'gemini') or 'gemini').lower()
    if provider == 'anthropic':
        return AnthropicBackend(config)
    if provider == 'openai':
        return OpenAIBackend(config)
    return GeminiBackend(config)


# ---------------------------------------------------------------------------
# Service — routing, cache, and usage accounting.
# ---------------------------------------------------------------------------
class LLMService:
    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self.usage_totals = defaultdict(lambda: {'calls': 0, 'input': 0, 'output': 0})
        self._gen_cache = OrderedDict()   # deterministic generate() results
        self._backend = _make_backend(config)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def _resolve(self, role):
        cfg = self.config
        mapping = {
            'pro': cfg.model_pro,
            'pro_max': cfg.model_pro_max,   # "Pro mode" code-gen upgrade
            'flash': cfg.model_flash,
            'lite': cfg.model_flash_lite,
            'draft': cfg.model_pro,
        }
        if role not in mapping:
            raise ValueError(f"Unknown LLM role: {role!r}")
        return mapping[role]

    # ------------------------------------------------------------------
    # Usage accounting
    # ------------------------------------------------------------------
    def _record(self, model, input_tokens, output_tokens):
        with self._lock:
            entry = self.usage_totals[model]
            entry['calls'] += 1
            entry['input'] += input_tokens or 0
            entry['output'] += output_tokens or 0

    def usage_snapshot(self):
        with self._lock:
            return {model: dict(v) for model, v in self.usage_totals.items()}

    # ------------------------------------------------------------------
    # Deterministic-call cache (workstream B)
    # ------------------------------------------------------------------
    @staticmethod
    def _cache_key(model, contents, json_mode):
        flat = contents if isinstance(contents, str) else "\n".join(
            str(c) for c in contents)
        digest = hashlib.sha256(flat.encode('utf-8', 'ignore')).hexdigest()
        return (model, json_mode, digest)

    def _cache_get(self, key):
        with self._lock:
            if key in self._gen_cache:
                self._gen_cache.move_to_end(key)
                return self._gen_cache[key]
        return None

    def _cache_put(self, key, value):
        with self._lock:
            self._gen_cache[key] = value
            self._gen_cache.move_to_end(key)
            while len(self._gen_cache) > _GEN_CACHE_MAX:
                self._gen_cache.popitem(last=False)

    # ------------------------------------------------------------------
    # Synchronous generation
    # ------------------------------------------------------------------
    def generate(self, role, contents, *, temperature=0.2, json_mode=False):
        model = self._resolve(role)
        # Only temperature-0 calls are deterministic and therefore cacheable
        # (e.g. moderation / code-moderation), so a hammered prompt can't
        # re-bill the API. Higher-temperature calls always hit the model.
        cache_key = None
        if temperature == 0.0 and isinstance(contents, (str, list, tuple)):
            cache_key = self._cache_key(model, contents, json_mode)
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
        result = self._backend.generate(
            model, contents, temperature=temperature, json_mode=json_mode)
        self._record(model, result.usage.get('input', 0),
                     result.usage.get('output', 0))
        if cache_key is not None:
            self._cache_put(cache_key, result)
        return result

    # ------------------------------------------------------------------
    # Streaming generation
    # ------------------------------------------------------------------
    def stream(self, role, contents, *, temperature=0.2, usage_out=None):
        """Yield text deltas; fill ``usage_out`` (if given) when finished."""
        model = self._resolve(role)
        local = {}
        yield from self._backend.stream(
            model, contents, temperature=temperature, usage_out=local)
        self._record(model, local.get('input', 0), local.get('output', 0))
        if usage_out is not None:
            usage_out.update(local)


# ---------------------------------------------------------------------------
# Module-level service (swappable for tests)
# ---------------------------------------------------------------------------
_service = None


def init_service(config):
    global _service
    _service = LLMService(config)
    return _service


def get_service():
    if _service is None:
        raise RuntimeError("LLM service not initialised — call init_service().")
    return _service


def set_service(service):
    """Test hook: inject a fake service."""
    global _service
    _service = service
