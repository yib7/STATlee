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
import contextlib
import copy
import hashlib
import logging
import re
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


class LLMOutputError(RuntimeError):
    """A provider returned no usable output. Subclasses carry a
    ``user_message`` that routes show the user verbatim (it never contains
    provider internals or secrets) and a short ``reason`` code for clients."""

    reason = 'output_error'
    default_message = 'The model returned no usable response. Please try again.'

    def __init__(self, provider='', model='', detail=None, user_message=None):
        self.provider = provider
        self.model = model
        self.detail = detail
        self.user_message = user_message or self.default_message
        self.usage = None   # billed usage, when the provider reported it
        super().__init__(
            f"{provider} model {model}: {self.reason}"
            + (f" ({detail})" if detail else ''))


class TruncatedGenerationError(LLMOutputError):
    """Raised when a provider stopped generating because it hit the output-token
    ceiling (P2-6). The partial text is unusable for code generation -- a script
    cut mid-line would flow into validation and /run as confusing syntax errors
    the auto-debugger then bills more tokens to explain -- so the backend fails
    loudly instead of returning silently-truncated output. /chat turns this into
    a clean SSE error like any other backend exception."""

    reason = 'truncated'
    default_message = ('The model stopped at its output-token limit, so the '
                       'response is incomplete. Try a shorter request or '
                       'narrow the scope of the analysis.')


class BlockedGenerationError(LLMOutputError):
    """The provider's safety system blocked the prompt or stopped the answer
    (Gemini SAFETY/RECITATION/PROHIBITED_CONTENT/... or a blocked prompt,
    Anthropic ``stop_reason='refusal'``, OpenAI ``content_filter`` or a
    ``refusal`` message) (D3-4). Any partial text is unusable."""

    reason = 'blocked'
    default_message = ("The AI provider's safety filter declined this request, "
                       'so no result was produced. Rephrase the request (or '
                       'rename sensitive-looking columns) and try again.')


class EmptyGenerationError(LLMOutputError):
    """The provider answered with no text at all (no candidates, an empty
    message, or a stream that produced no deltas) (D3-4)."""

    reason = 'empty'
    default_message = ('The model returned an empty response. Please try '
                       'again.')


class MalformedReplyError(LLMOutputError, ValueError):
    """A JSON-mode reply that does not parse, or parses to the wrong shape for
    the route (D3-9/D3-10). Routes turn it into a 502."""

    reason = 'malformed'
    default_message = ("The model's reply was not in the expected format. "
                       'Please try again.')


def _reason_name(reason):
    if reason is None:
        return ''
    return (getattr(reason, 'name', None) or str(reason)).strip().upper()


def _truncated_by_token_limit(reason):
    """True if a provider finish/stop reason means generation was cut off by the
    output-token ceiling. Normalizes across providers: OpenAI ``'length'``,
    Anthropic ``'max_tokens'``, Gemini ``MAX_TOKENS`` (an enum whose ``.name``
    is used, or a plain string). Any other/absent reason is treated as a normal
    completion."""
    return _reason_name(reason) in ('LENGTH', 'MAX_TOKENS')


# Finish/stop reasons that mean "a safety system stopped this" (D3-4):
# Gemini FinishReason members, Anthropic 'refusal', OpenAI 'content_filter'.
_BLOCKED_REASONS = frozenset({
    'SAFETY', 'RECITATION', 'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII',
    'LANGUAGE', 'IMAGE_SAFETY', 'IMAGE_PROHIBITED_CONTENT', 'IMAGE_RECITATION',
    'REFUSAL', 'CONTENT_FILTER',
})


def _check_truncation(reason, provider, model):
    """Raise ``TruncatedGenerationError`` when ``reason`` signals a token-ceiling
    cutoff; a no-op for a normal finish reason. One shared helper keeps the
    three backends consistent (P2-6)."""
    if _truncated_by_token_limit(reason):
        raise TruncatedGenerationError(provider, model)


def _check_finish(reason, provider, model):
    """Raise the typed error a terminal finish/stop reason calls for: a safety
    stop or refusal (D3-4), else a token-ceiling cutoff (P2-6)."""
    name = _reason_name(reason)
    if name in _BLOCKED_REASONS:
        raise BlockedGenerationError(provider, model, detail=name)
    _check_truncation(reason, provider, model)


@contextlib.contextmanager
def _billed(usage):
    """Attach the call's billed ``usage`` to any output error raised inside,
    so ``LLMService`` can still account for a blocked/empty/truncated call
    the provider charged for (D3-11)."""
    try:
        yield
    except LLMOutputError as exc:
        exc.usage = usage
        raise


def _usage_dict(model, input_tokens, output_tokens, thinking=0, cached=0):
    """Per-call usage. ``output`` is everything billed at the output rate
    (visible text plus thinking/reasoning tokens); ``thinking`` and ``cached``
    (prompt tokens served from the provider's cache, already inside ``input``)
    are only present when non-zero (D3-3)."""
    usage = {'model': model, 'input': int(input_tokens or 0),
             'output': int(output_tokens or 0)}
    if thinking:
        usage['thinking'] = int(thinking)
    if cached:
        usage['cached'] = int(cached)
    return usage


def _status_code(exc):
    code = getattr(exc, 'status_code', None)
    if code is None:
        code = getattr(getattr(exc, 'response', None), 'status_code', None)
    return code


def _rejects_temperature(exc):
    """True for a provider 400 saying the model does not accept a custom
    ``temperature`` (D3-12/D3-13). Anything else (context length, auth, 5xx)
    is not a temperature problem and must not trigger the no-temperature
    retry."""
    code = _status_code(exc)
    if code not in (None, 400):
        return False
    return 'temperature' in str(exc).lower()


class _Timeouts:
    """Per-model client timeout (D3-5): the Pro-mode model ('pro_max') gets the
    longer budget, every other model the default one."""

    def __init__(self, config):
        self.config = config

    def default(self):
        return float(getattr(self.config, 'llm_timeout_seconds', 120) or 120)

    def longest(self):
        return max(self.default(), float(
            getattr(self.config, 'llm_pro_timeout_seconds', 0) or 0))

    def for_model(self, model):
        if model and model == getattr(self.config, 'model_pro_max', None):
            return self.longest()
        return self.default()


# ---------------------------------------------------------------------------
# Gemini backend — owns only the wire protocol.
# ---------------------------------------------------------------------------
class GeminiBackend:
    # Transient statuses retried with exponential backoff + jitter (D3-5).
    RETRY_STATUS_CODES = [408, 429, 500, 502, 503, 504]

    def __init__(self, config):
        self.config = config
        self._client = None
        self._timeouts = _Timeouts(config)

    def _client_(self):
        if self._client is None:
            from google import genai
            from google.genai import types
            if not self.config.gemini_api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not configured on the server.")
            retries = int(getattr(self.config, 'llm_max_retries', 2))
            self._client = genai.Client(
                api_key=self.config.gemini_api_key,
                http_options=types.HttpOptions(
                    timeout=int(self._timeouts.default() * 1000),   # ms
                    # google-genai sends the client timeout as X-Server-Timeout
                    # (a hard server-side deadline for the whole call, streams
                    # included) unless the header is already set. Pin it to
                    # the longest per-call budget so a long Pro draft is not
                    # killed by the default client timeout; stalls are still
                    # caught client-side by the read timeout.
                    headers={'X-Server-Timeout':
                             str(int(self._timeouts.longest()))},
                    # Retries wrap the request, before any stream delta is
                    # delivered; a stream that fails mid-way is not replayed.
                    retry_options=types.HttpRetryOptions(
                        attempts=retries + 1, initial_delay=1.0,
                        max_delay=8.0, exp_base=2.0, jitter=1.0,
                        http_status_codes=list(self.RETRY_STATUS_CODES))))
        return self._client

    def _config(self, model, **kwargs):
        from google.genai import types
        timeout = self._timeouts.for_model(model)
        if timeout != self._timeouts.default():
            kwargs['http_options'] = types.HttpOptions(
                timeout=int(timeout * 1000))
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _usage(response, model):
        meta = getattr(response, 'usage_metadata', None)
        candidates = getattr(meta, 'candidates_token_count', 0) or 0
        thoughts = getattr(meta, 'thoughts_token_count', 0) or 0
        return _usage_dict(
            model, getattr(meta, 'prompt_token_count', 0) or 0,
            candidates + thoughts, thinking=thoughts,
            cached=getattr(meta, 'cached_content_token_count', 0) or 0)

    @staticmethod
    def _finish_reason(response):
        """The terminal finish reason of the first candidate, or None."""
        cands = getattr(response, 'candidates', None) or []
        if cands:
            return getattr(cands[0], 'finish_reason', None)
        return None

    @staticmethod
    def _prompt_block(response):
        """The prompt-level block reason (``prompt_feedback.block_reason``),
        or None. A blocked prompt comes back with no candidates at all."""
        feedback = getattr(response, 'prompt_feedback', None)
        name = _reason_name(getattr(feedback, 'block_reason', None))
        if name and name != 'BLOCKED_REASON_UNSPECIFIED':
            return name
        return None

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
        kwargs = {'temperature': temperature}
        if json_mode:
            kwargs['response_mime_type'] = 'application/json'
        response = self._client_().models.generate_content(
            model=model, contents=self._to_contents(contents),
            config=self._config(model, **kwargs))
        usage = self._usage(response, model)
        logger.info("llm call provider=gemini model=%s in=%s out=%s",
                    model, usage['input'], usage['output'])
        with _billed(usage):
            block = self._prompt_block(response)
            if block:
                raise BlockedGenerationError('Gemini', model, detail=block)
            _check_finish(self._finish_reason(response), 'Gemini', model)
            text = (getattr(response, 'text', None) or '').strip()
            if not text:
                raise EmptyGenerationError('Gemini', model)
        return LLMResult(text=text, usage=usage)

    def stream(self, model, contents, *, temperature, usage_out):
        stream = self._client_().models.generate_content_stream(
            model=model, contents=self._to_contents(contents),
            config=self._config(model, temperature=temperature))
        # Make the usage contract explicit rather than relying on an implicit
        # "last chunk wins" assignment: seed a zero-usage baseline (so an empty
        # stream reports zero, not a stale/None value) and refresh it from every
        # chunk that carries usage_metadata. Gemini reports cumulative counts on
        # its chunks, so the latest refresh holds the totals so far, and
        # usage_out is updated as they arrive: a stream cut short (client
        # disconnect, safety stop) still accounts for what was billed (D3-11).
        if usage_out is not None:
            usage_out.update({'model': model, 'input': 0, 'output': 0})
        finish_reason = None
        block = None
        produced = False
        for chunk in stream:
            if getattr(chunk, 'usage_metadata', None) is not None:
                if usage_out is not None:
                    usage_out.update(self._usage(chunk, model))
            block = block or self._prompt_block(chunk)
            fr = self._finish_reason(chunk)
            if fr is not None:
                finish_reason = fr
            delta = getattr(chunk, 'text', None)
            if delta:
                produced = True
                yield delta
        # After the deltas: a safety stop or blocked prompt (D3-4) and a
        # token-ceiling cutoff (P2-6) fail loudly so /chat emits a clean error
        # instead of accepting a partial script.
        if block:
            raise BlockedGenerationError('Gemini', model, detail=block)
        _check_finish(finish_reason, 'Gemini', model)
        if not produced:
            raise EmptyGenerationError('Gemini', model)


# ---------------------------------------------------------------------------
# Anthropic / Claude backend.
# ---------------------------------------------------------------------------
class AnthropicBackend:
    """Anthropic/Claude backend. Auth resolves from config: an explicit API key
    (deploy), else a bare client that picks up ANTHROPIC_AUTH_TOKEN or a local
    `claude`/`ant` OAuth login (local subscription)."""

    JSON_SYSTEM = ("Output only a single valid JSON value — no markdown, "
                   "no code fences, no prose.")

    # Sampling parameters (D3-13). Anthropic deprecated temperature/top_p/top_k
    # for Claude 4.7 and later models (a non-default value is a 400), and the
    # Python SDK 1.x dropped them from messages.create/stream (passing one is a
    # TypeError), so the temperature travels in ``extra_body`` and only for
    # models before 4.7. An unknown id that still rejects it is learned from
    # its first 400 and remembered.
    _VERSIONED_ID = re.compile(
        r'^claude-[a-z]+-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$')
    SAMPLING_DEPRECATED_FROM = (4, 7)

    def __init__(self, config):
        self.config = config
        self._client = None
        self._timeouts = _Timeouts(config)
        self._no_temperature = set()
        self._lock = threading.Lock()

    def _client_(self):
        if self._client is None:
            import anthropic
            common = {'timeout': self._timeouts.default(),
                      'max_retries': int(getattr(self.config,
                                                 'llm_max_retries', 2))}
            if self.config.anthropic_api_key:
                self._client = anthropic.Anthropic(
                    api_key=self.config.anthropic_api_key, **common)
            else:
                self._client = anthropic.Anthropic(
                    default_headers={'anthropic-beta': 'oauth-2025-04-20'},
                    **common)
        return self._client

    @classmethod
    def _sampling_deprecated(cls, model):
        """True for a Claude id at or after the version that rejects sampling
        parameters (claude-opus-4-7, claude-sonnet-5-5, claude-fable-5-1...)."""
        m = cls._VERSIONED_ID.match(str(model))
        if not m:
            return False
        version = (int(m.group(1)), int(m.group(2) or 0))
        return version >= cls.SAMPLING_DEPRECATED_FROM

    def _accepts_temperature(self, model):
        if self._sampling_deprecated(model):
            return False
        with self._lock:
            return model not in self._no_temperature

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
        cache_read = getattr(u, 'cache_read_input_tokens', 0) or 0
        cache_write = getattr(u, 'cache_creation_input_tokens', 0) or 0
        # input_tokens excludes cache reads/writes on Anthropic; fold them in
        # so 'input' means "all prompt tokens" on every provider.
        return _usage_dict(
            model,
            (getattr(u, 'input_tokens', 0) or 0) + cache_read + cache_write,
            getattr(u, 'output_tokens', 0) or 0, cached=cache_read)

    # B-14 (defensive, not live-verified: confirming it needs a paid call).
    # The Pro-mode model on Anthropic (claude-fable-5-1 by default) thinks on
    # every call, and thinking tokens count against max_tokens, so a long Pro
    # draft or report draft could stop at the configured budget and fail as
    # truncated. The 'pro_max' model gets at least this budget when it is a
    # current (4.7+) Claude id, all of which accept far larger outputs; the
    # cap is a ceiling, so it costs nothing unless the model uses it. OpenAI
    # streams already get 32000 for the same reason (D3-16).
    PRO_MAX_MIN_TOKENS = 32000

    def _max_tokens(self, model, base):
        if (model == getattr(self.config, 'model_pro_max', None)
                and self._sampling_deprecated(model)):
            return max(int(base), self.PRO_MAX_MIN_TOKENS)
        return base

    def _kwargs(self, model, contents, temperature, max_tokens):
        kwargs = {
            'model': model,
            'max_tokens': self._max_tokens(model, max_tokens),
            'messages': [{'role': 'user', 'content': self._content(contents)}],
            'timeout': self._timeouts.for_model(model),
        }
        if temperature is not None and self._accepts_temperature(model):
            kwargs['extra_body'] = {'temperature': temperature}
        return kwargs

    @staticmethod
    def _has_temperature(kwargs):
        return 'temperature' in (kwargs.get('extra_body') or {})

    def _call(self, fn, kwargs):
        """Call ``fn(**kwargs)``; on a 'temperature not supported' 400, retry
        once without it and remember the model (D3-13)."""
        try:
            return fn(**kwargs)
        except Exception as exc:
            if self._has_temperature(kwargs) and _rejects_temperature(exc):
                with self._lock:
                    self._no_temperature.add(kwargs['model'])
                kwargs = {k: v for k, v in kwargs.items() if k != 'extra_body'}
                return fn(**kwargs)
            raise

    def generate(self, model, contents, *, temperature, json_mode):
        kwargs = self._kwargs(model, contents, temperature,
                              self.config.anthropic_max_tokens)
        if json_mode:
            kwargs['system'] = self.JSON_SYSTEM
        message = self._call(self._client_().messages.create, kwargs)
        text = next((b.text for b in (getattr(message, 'content', None) or [])
                     if getattr(b, 'type', None) == 'text'), '')
        usage = self._usage(message, model)
        logger.info("llm call provider=anthropic model=%s in=%s out=%s",
                    model, usage['input'], usage['output'])
        with _billed(usage):
            _check_finish(getattr(message, 'stop_reason', None),
                          'Anthropic', model)
            text = (text or '').strip()
            if not text:
                raise EmptyGenerationError('Anthropic', model)
        return LLMResult(text=text, usage=usage)

    def _open_stream(self, kwargs):
        """Enter ``messages.stream`` and return ``(exit_stack, stream)``.

        ``messages.stream()`` only builds a manager; the HTTP request (and a
        'temperature not supported' 400) happens on ENTER, so the D3-13
        retry-without-temperature must wrap the enter, not the call (B-07).
        No delta has been yielded at that point, so a retry is invisible."""
        stack = contextlib.ExitStack()
        try:
            stream = stack.enter_context(
                self._client_().messages.stream(**kwargs))
        except Exception as exc:
            stack.close()
            if not (self._has_temperature(kwargs) and _rejects_temperature(exc)):
                raise
            with self._lock:
                self._no_temperature.add(kwargs['model'])
            kwargs = {k: v for k, v in kwargs.items() if k != 'extra_body'}
            stack = contextlib.ExitStack()
            stream = stack.enter_context(
                self._client_().messages.stream(**kwargs))
        return stack, stream

    def stream(self, model, contents, *, temperature, usage_out):
        kwargs = self._kwargs(model, contents, temperature,
                              self.config.anthropic_stream_max_tokens)
        stack, stream = self._open_stream(kwargs)
        produced = False
        final = None
        with stack:
            try:
                for delta in stream.text_stream:
                    if delta:
                        produced = True
                        yield delta
                final = stream.get_final_message()
            finally:
                # D3-11: account for a stream cut short (client disconnect or
                # an error) from the partial snapshot the SDK keeps.
                snapshot = final if final is not None else getattr(
                    stream, 'current_message_snapshot', None)
                if usage_out is not None and snapshot is not None:
                    usage_out.update(self._usage(snapshot, model))
        _check_finish(getattr(final, 'stop_reason', None), 'Anthropic', model)
        if not produced:
            raise EmptyGenerationError('Anthropic', model)


# ---------------------------------------------------------------------------
# OpenAI backend.
# ---------------------------------------------------------------------------
class OpenAIBackend:
    """OpenAI backend (Chat Completions). ``MediaPart`` maps to ``image_url``
    parts (images) and ``file`` parts (base64 PDFs). Reasoning models (gpt-5.x,
    gpt-6.x, o-series) reject non-default sampling params, so a ``temperature`` 400
    triggers one retry without it, and the model is remembered so later calls
    skip the failing round-trip (D3-12). Determinism for cached temp-0 calls is
    preserved by ``LLMService``'s cache regardless."""

    def __init__(self, config):
        self.config = config
        self._client = None
        self._timeouts = _Timeouts(config)
        self._no_temperature = set()
        self._lock = threading.Lock()

    def _client_(self):
        if self._client is None:
            import openai
            if not self.config.openai_api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY is not configured on the server.")
            self._client = openai.OpenAI(
                api_key=self.config.openai_api_key,
                timeout=self._timeouts.default(),
                max_retries=int(getattr(self.config, 'llm_max_retries', 2)))
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
        # completion_tokens already includes reasoning tokens and prompt_tokens
        # already includes cached ones; the details are reported alongside
        # (D3-3).
        return _usage_dict(
            model, getattr(u, 'prompt_tokens', 0) or 0,
            getattr(u, 'completion_tokens', 0) or 0,
            thinking=getattr(getattr(u, 'completion_tokens_details', None),
                             'reasoning_tokens', 0) or 0,
            cached=getattr(getattr(u, 'prompt_tokens_details', None),
                           'cached_tokens', 0) or 0)

    def _create(self, *, model, contents, temperature, json_mode, stream):
        kwargs = {
            'model': model,
            'messages': [{'role': 'user', 'content': self._content(contents)}],
            # D3-16: on reasoning models this budget includes hidden reasoning
            # tokens, so streams (code drafts) get the larger cap.
            'max_completion_tokens': (
                self.config.openai_stream_max_tokens if stream
                else self.config.openai_max_tokens),
            'timeout': self._timeouts.for_model(model),
        }
        with self._lock:
            skip_temperature = model in self._no_temperature
        if temperature is not None and not skip_temperature:
            kwargs['temperature'] = temperature
        if json_mode:
            kwargs['response_format'] = {'type': 'json_object'}
        if stream:
            kwargs['stream'] = True
            kwargs['stream_options'] = {'include_usage': True}
        try:
            return self._client_().chat.completions.create(**kwargs)
        except Exception as exc:
            # Reasoning models only accept the default temperature: drop it,
            # remember the model, and retry once rather than failing the call.
            if 'temperature' in kwargs and _rejects_temperature(exc):
                with self._lock:
                    self._no_temperature.add(model)
                kwargs.pop('temperature', None)
                return self._client_().chat.completions.create(**kwargs)
            raise

    def generate(self, model, contents, *, temperature, json_mode):
        response = self._create(model=model, contents=contents,
                                temperature=temperature, json_mode=json_mode,
                                stream=False)
        choices = getattr(response, 'choices', None) or []
        choice = choices[0] if choices else None
        message = getattr(choice, 'message', None)
        usage = self._usage(response, model)
        logger.info("llm call provider=openai model=%s in=%s out=%s",
                    model, usage['input'], usage['output'])
        with _billed(usage):
            refusal = getattr(message, 'refusal', None)
            if refusal:
                raise BlockedGenerationError(
                    'OpenAI', model, detail='refusal',
                    user_message=('The model declined this request: '
                                  f'{str(refusal)[:300]}'))
            _check_finish(getattr(choice, 'finish_reason', None),
                          'OpenAI', model)
            text = (getattr(message, 'content', None) or '').strip()
            if not text:
                raise EmptyGenerationError('OpenAI', model)
        return LLMResult(text=text, usage=usage)

    def stream(self, model, contents, *, temperature, usage_out):
        events = self._create(model=model, contents=contents,
                              temperature=temperature, json_mode=False,
                              stream=True)
        finish_reason = None
        refused = False
        produced = False
        if usage_out is not None:
            usage_out.update({'model': model, 'input': 0, 'output': 0})
        for chunk in events:
            if getattr(chunk, 'usage', None) and usage_out is not None:
                usage_out.update(self._usage(chunk, model))
            if getattr(chunk, 'choices', None):
                choice = chunk.choices[0]
                fr = getattr(choice, 'finish_reason', None)
                if fr is not None:
                    finish_reason = fr
                delta = getattr(choice, 'delta', None)
                if getattr(delta, 'refusal', None):
                    refused = True
                text = getattr(delta, 'content', None)
                if text:
                    produced = True
                    yield text
        if refused:
            raise BlockedGenerationError('OpenAI', model, detail='refusal')
        _check_finish(finish_reason, 'OpenAI', model)
        if not produced:
            raise EmptyGenerationError('OpenAI', model)


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
        # NB (P2-4): 'pro' and 'draft' BOTH map to model_pro — the default,
        # cheaper code-gen tier. The real "Pro mode" upgrade is 'pro_max' ->
        # model_pro_max. So MODEL_PRO changes the default tier, NOT Pro mode.
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
                # D3-11: a cache hit costs nothing, so report zero usage
                # (flagged) instead of re-showing the original call's tokens.
                return LLMResult(text=cached.text, usage={
                    'model': model, 'input': 0, 'output': 0,
                    'cache_hit': True})
        try:
            result = self._backend.generate(
                model, contents, temperature=temperature, json_mode=json_mode)
        except LLMOutputError as exc:
            if exc.usage:
                self._record(model, exc.usage.get('input', 0),
                             exc.usage.get('output', 0))
            raise
        self._record(model, result.usage.get('input', 0),
                     result.usage.get('output', 0))
        if cache_key is not None:
            self._cache_put(cache_key, copy.deepcopy(result))
        return result

    # ------------------------------------------------------------------
    # Streaming generation
    # ------------------------------------------------------------------
    def stream(self, role, contents, *, temperature=0.2, usage_out=None):
        """Yield text deltas; fill ``usage_out`` (if given) when finished.

        Usage is recorded in a ``finally`` (D3-11), so a stream that ends in a
        truncation/safety error or is closed by a client disconnect
        (``GeneratorExit``) is still counted in ``/metrics`` and reported to
        the caller. The backends keep ``local`` current as usage arrives."""
        model = self._resolve(role)
        local = {}
        started = False
        try:
            for delta in self._backend.stream(
                    model, contents, temperature=temperature, usage_out=local):
                started = True
                yield delta
        finally:
            if started or local.get('input') or local.get('output'):
                self._record(model, local.get('input', 0),
                             local.get('output', 0))
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
