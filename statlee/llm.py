"""Role-based LLM service with usage tracking (roadmap 3.3 / 3.4).

Every model call in the app goes through this module. Calls are addressed by
*role* — 'pro', 'pro_max', 'flash', 'lite', or 'draft' — and the role→model
mapping lives in config, so swapping a model is a config change, not a code
change. Code generation normally uses 'draft'; the "Pro mode" toggle routes it
to 'pro_max' (a bigger, stronger model) instead.

``LLMService`` owns role resolution, the deterministic-call cache, and usage
accounting. The Gemini wire protocol lives in ``GeminiBackend``.
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
# Service — routing, cache, and usage accounting.
# ---------------------------------------------------------------------------
class LLMService:
    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self.usage_totals = defaultdict(lambda: {'calls': 0, 'input': 0, 'output': 0})
        self._gen_cache = OrderedDict()   # deterministic generate() results
        self._backend = GeminiBackend(config)

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
