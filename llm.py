"""Provider-agnostic LLM service with usage tracking (roadmap 3.3 / 3.4).

Every model call in the app goes through this module. Calls are addressed by
*role* — 'pro', 'flash', 'lite', or 'draft' — and the role→(provider, model)
mapping lives in config, so swapping a model (or moving code drafting to the
Claude API) is a config change, not a code change.

Token usage is recorded per model and exposed for /metrics and the per-
analysis cost display.
"""
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger('codecaster.llm')


@dataclass
class LLMResult:
    text: str = ''
    usage: dict = field(default_factory=dict)  # {input, output, model}


class LLMService:
    def __init__(self, config):
        self.config = config
        self._gemini = None
        self._anthropic = None
        self._lock = threading.Lock()
        self.usage_totals = defaultdict(lambda: {'calls': 0, 'input': 0, 'output': 0})

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def _resolve(self, role):
        cfg = self.config
        if role == 'draft' and cfg.draft_provider == 'anthropic':
            return 'anthropic', cfg.anthropic_model
        mapping = {
            'pro': cfg.model_pro,
            'flash': cfg.model_flash,
            'lite': cfg.model_flash_lite,
            'draft': cfg.model_pro,
        }
        if role not in mapping:
            raise ValueError(f"Unknown LLM role: {role!r}")
        return 'gemini', mapping[role]

    def _gemini_client(self):
        if self._gemini is None:
            from google import genai
            if not self.config.gemini_api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not configured on the server.")
            self._gemini = genai.Client(api_key=self.config.gemini_api_key)
        return self._gemini

    def _anthropic_client(self):
        if self._anthropic is None:
            import anthropic
            self._anthropic = anthropic.Anthropic(
                api_key=self.config.anthropic_api_key)
        return self._anthropic

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

    @staticmethod
    def _gemini_usage(response, model):
        meta = getattr(response, 'usage_metadata', None)
        usage = {
            'model': model,
            'input': getattr(meta, 'prompt_token_count', 0) or 0,
            'output': getattr(meta, 'candidates_token_count', 0) or 0,
        }
        return usage

    # ------------------------------------------------------------------
    # Synchronous generation
    # ------------------------------------------------------------------
    def generate(self, role, contents, *, temperature=0.2, json_mode=False):
        provider, model = self._resolve(role)
        if provider == 'anthropic':
            return self._generate_anthropic(model, contents, temperature)
        return self._generate_gemini(model, contents, temperature, json_mode)

    def _generate_gemini(self, model, contents, temperature, json_mode):
        from google.genai import types
        kwargs = {'temperature': temperature}
        if json_mode:
            kwargs['response_mime_type'] = 'application/json'
        response = self._gemini_client().models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(**kwargs))
        usage = self._gemini_usage(response, model)
        self._record(model, usage['input'], usage['output'])
        logger.info("llm call model=%s in=%s out=%s",
                    model, usage['input'], usage['output'])
        return LLMResult(text=(response.text or '').strip(), usage=usage)

    def _generate_anthropic(self, model, contents, temperature):
        if not isinstance(contents, str):
            contents = "\n".join(str(c) for c in contents)
        response = self._anthropic_client().messages.create(
            model=model, max_tokens=8192, temperature=temperature,
            messages=[{'role': 'user', 'content': contents}])
        text = ''.join(
            block.text for block in response.content
            if getattr(block, 'type', '') == 'text')
        u = getattr(response, 'usage', None)
        usage = {
            'model': model,
            'input': getattr(u, 'input_tokens', 0) or 0,
            'output': getattr(u, 'output_tokens', 0) or 0,
        }
        self._record(model, usage['input'], usage['output'])
        logger.info("llm call model=%s in=%s out=%s",
                    model, usage['input'], usage['output'])
        return LLMResult(text=text.strip(), usage=usage)

    # ------------------------------------------------------------------
    # Streaming generation
    # ------------------------------------------------------------------
    def stream(self, role, contents, *, temperature=0.2, usage_out=None):
        """Yield text deltas; fill ``usage_out`` (if given) when finished."""
        provider, model = self._resolve(role)
        if provider == 'anthropic':
            yield from self._stream_anthropic(model, contents, temperature, usage_out)
        else:
            yield from self._stream_gemini(model, contents, temperature, usage_out)

    def _stream_gemini(self, model, contents, temperature, usage_out):
        from google.genai import types
        stream = self._gemini_client().models.generate_content_stream(
            model=model, contents=contents,
            config=types.GenerateContentConfig(temperature=temperature))
        last_chunk = None
        for chunk in stream:
            last_chunk = chunk
            delta = getattr(chunk, 'text', None)
            if delta:
                yield delta
        usage = self._gemini_usage(last_chunk, model) if last_chunk else {
            'model': model, 'input': 0, 'output': 0}
        self._record(model, usage['input'], usage['output'])
        if usage_out is not None:
            usage_out.update(usage)

    def _stream_anthropic(self, model, contents, temperature, usage_out):
        if not isinstance(contents, str):
            contents = "\n".join(str(c) for c in contents)
        with self._anthropic_client().messages.stream(
                model=model, max_tokens=8192, temperature=temperature,
                messages=[{'role': 'user', 'content': contents}]) as s:
            yield from s.text_stream
            final = s.get_final_message()
        u = getattr(final, 'usage', None)
        usage = {
            'model': model,
            'input': getattr(u, 'input_tokens', 0) or 0,
            'output': getattr(u, 'output_tokens', 0) or 0,
        }
        self._record(model, usage['input'], usage['output'])
        if usage_out is not None:
            usage_out.update(usage)


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
