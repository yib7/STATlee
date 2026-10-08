"""Flask blueprints (roadmap 3.1) and small shared route helpers."""
import json
import logging
import re

from flask import Response, jsonify, stream_with_context


def json_error(message, status=400):
    """Generic client-safe error body (1.6 — never leak str(e))."""
    return jsonify({'error': message}), status


_MODERATION_FALLBACK = ('The request could not be verified as safe and '
                        'on-topic. Please rephrase and try again.')


def moderation_blocked(result_text):
    """Interpret a moderation verdict, **failing closed**.

    Moderation prompts now return a structured JSON verdict
    ``{"decision": "pass" | "block", "reason": "..."}``. This parses it and
    treats anything that is not an explicit ``pass`` — malformed JSON, a missing
    or unexpected ``decision``, an empty body — as *blocked*. That closes the
    prompt-injection path where suppressing a magic word made moderation
    fail open.

    Case handling (P1-6): the prompt schema (``prompts._VERDICT_SHAPE``)
    specifies lowercase ``"pass"``/``"block"``, but ``decision`` is compared
    case-INSENSITIVELY on purpose. This is deliberate slack, not a bug: it is
    the safe direction to differ — a model that returns ``"Pass"`` or ``"PASS"``
    is honored as a pass rather than fail-closed into blocking a legitimate
    request, while any non-pass token (however cased) still blocks. Do NOT
    tighten this to an exact-case match; that would let a stray capital letter
    reject valid analyses.

    Returns ``(blocked: bool, reason: str)``; ``reason`` is empty when allowed.
    """
    try:
        verdict = json.loads((result_text or '').strip())
    except (ValueError, TypeError):
        return True, _MODERATION_FALLBACK

    decision = None
    reason = ''
    if isinstance(verdict, dict):
        decision = str(verdict.get('decision', '')).strip().lower()
        reason = str(verdict.get('reason', '') or '').strip()
    elif isinstance(verdict, str):
        decision = verdict.strip().lower()

    if decision == 'pass':
        return False, ''
    return True, (reason or _MODERATION_FALLBACK)


# ---------------------------------------------------------------------------
# P1-7: server-side caps on client-supplied prompt material
# ---------------------------------------------------------------------------
# Every free-text field a client POSTs flows into an LLM prompt billed to the
# operator's key; before these caps the only bound was the global 16 MB
# request limit, so one request inside the normal rate limit could smuggle
# megabytes of attacker-chosen text (millions of input tokens) into a paid
# call. Sizing follows the /history save caps in routes/auth.py (10k/20k).
FREE_TEXT_MAX = 20000       # analysis output, code, context, chat messages
BACKGROUND_MAX = 5000       # short framing text (e.g. report background)
STYLE_FIELD_MAX = 100       # enum-ish UI values (length, tone, format, language)
HISTORY_MAX_TURNS = 50      # most-recent chat turns kept
HISTORY_FIELD_MAX = 2000    # chars kept per field within each turn
CODEBOOK_MAX = 100000       # serialized codebook budget (wide datasets)
PLOTS_MAX_COUNT = 3         # prompts only ever attach the first 3 plots
PLOT_B64_MAX = 2000000      # per-plot base64 payload cap (~1.5 MB decoded)


def clamp(text, limit):
    """Coerce to str and hard-truncate to ``limit`` characters."""
    if text is None:
        return ''
    if not isinstance(text, str):
        text = str(text)
    return text[:limit]


def clamp_history(turns, max_turns=HISTORY_MAX_TURNS,
                  field_max=HISTORY_FIELD_MAX):
    """Keep the most recent ``max_turns`` {role, text} turns, fields clamped.

    Non-list payloads and non-dict turns are dropped (the prompt formatters
    skip them anyway), so the result is always a bounded, well-shaped list.
    """
    if not isinstance(turns, list):
        return []
    clamped = []
    for turn in turns[-max_turns:]:
        if not isinstance(turn, dict):
            continue
        clamped.append({'role': clamp(turn.get('role') or 'user', 32),
                        'text': clamp(turn.get('text'), field_max)})
    return clamped


def clamp_codebook(codebook, limit=CODEBOOK_MAX):
    """Bound a {column: description} mapping to ~``limit`` serialized chars.

    Entries are kept in order until the budget is spent; everything after is
    dropped (the routes already degrade to headers-only context).
    """
    if not isinstance(codebook, dict):
        return {}
    kept, total = {}, 0
    for key, value in codebook.items():
        entry = len(str(key)) + len(json.dumps(value, default=str))
        if total + entry > limit:
            break
        kept[key] = value
        total += entry
    return kept


MAPPING_KEY_MAX = 200       # column-name keys in codebook / pdf_mapping
MAPPING_VALUE_MAX = 2000    # one column's classification / description
FILENAME_MAX = 255
HEADERS_MAX_COUNT = 2000    # survey-mode header list (wide survey files)
HEADER_FIELD_MAX = 200
PREVIOUS_MAX_COUNT = 10     # /suggest reroll: suggestions already shown
PREVIOUS_FIELD_MAX = 500


def clamp_mapping(mapping, limit=CODEBOOK_MAX, key_max=MAPPING_KEY_MAX,
                  value_max=MAPPING_VALUE_MAX):
    """Bound a {column: text} mapping per entry AND in total.

    ``clamp_codebook`` alone drops an oversized entry but lets a merely large
    value through whole; this clamps each key/value first (values become
    strings), then applies the total budget."""
    if not isinstance(mapping, dict):
        return {}
    per_entry = {clamp(k, key_max): clamp(v, value_max)
                 for k, v in mapping.items()}
    return clamp_codebook(per_entry, limit)


def clamp_str_list(items, max_count, field_max):
    """Keep at most ``max_count`` scalar entries, each clamped to ``field_max``
    characters. Non-scalar entries are dropped."""
    if not isinstance(items, list):
        return []
    kept = []
    for item in items:
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            kept.append(clamp(item, field_max))
            if len(kept) >= max_count:
                break
    return kept


# ---------------------------------------------------------------------------
# D1-12: request-shape validation. A wrong JSON type is a 400, not a 500 from
# deep inside a route.
# ---------------------------------------------------------------------------

class FieldError(ValueError):
    """A request field has the wrong JSON type. Blueprints render it as 400
    through ``register_field_errors``."""


def register_field_errors(bp):
    bp.register_error_handler(FieldError, lambda e: json_error(str(e), 400))


def request_json():
    """The JSON body as a dict; a missing, malformed or non-object body (a JSON
    array or scalar) is treated as an empty object."""
    from flask import request
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def str_field(data, key, limit=None, default=''):
    """A string field. Missing/null -> ``default``; any other non-string is a
    ``FieldError``. Clamped to ``limit`` characters when given."""
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise FieldError(f"'{key}' must be a string.")
    return value[:limit] if limit else value


def dict_field(data, key):
    """An object field. Falsy (null, '', [], {}) -> ``{}``; any other non-dict
    is a ``FieldError``."""
    value = data.get(key)
    if not value:
        return {}
    if not isinstance(value, dict):
        raise FieldError(f"'{key}' must be an object.")
    return value


def list_field(data, key):
    """A list field. Falsy -> ``[]``; any other non-list is a ``FieldError``."""
    value = data.get(key)
    if not value:
        return []
    if not isinstance(value, list):
        raise FieldError(f"'{key}' must be a list.")
    return value


LANGUAGES = {'python': 'Python', 'r': 'R'}


def language_field(data, key='language', default='Python'):
    """The analysis language, canonicalised to ``'Python'`` or ``'R'``.

    Missing/empty -> ``default``. Anything else is a ``FieldError``: the value
    selects the interpreter, the static check and the approval key, so it is an
    enum, not free text (D1-7/D1-12)."""
    value = data.get(key)
    if value is None or value == '':
        return default
    if not isinstance(value, str) or value.strip().lower() not in LANGUAGES:
        raise FieldError(f"'{key}' must be 'Python' or 'R'.")
    return LANGUAGES[value.strip().lower()]


def clamp_plots(plots, max_count=PLOTS_MAX_COUNT, max_chars=PLOT_B64_MAX):
    """Cap plot attachments: at most ``max_count`` base64 strings, each within
    ``max_chars``. Oversized or non-string entries are dropped, not truncated,
    because a truncated base64 image is garbage anyway."""
    if not isinstance(plots, list):
        return []
    kept = []
    for plot in plots:
        if not isinstance(plot, str) or len(plot) > max_chars:
            continue
        kept.append(plot)
        if len(kept) >= max_count:
            break
    return kept


class DebitGuard:
    """Give a debited credit back unless the streamed request delivered its
    result (D1-15).

    Error branches inside a stream refunded through ``except Exception``, but a
    client disconnect closes the generator with ``GeneratorExit`` (a
    BaseException), and a generator closed before its first step runs none of
    its body. Usage: call ``delivered()`` right before yielding the final
    ``done`` event, call ``refund()`` in the generator's ``finally``, and wrap
    the response with ``attach()`` so a never-started stream refunds on close.
    Refunds happen at most once.
    """

    def __init__(self, user, *, priority, config, debited=True):
        import threading

        from flask import current_app
        self._app = current_app._get_current_object()
        self._user = user
        self._user_id = getattr(user, 'id', None)
        self._priority = priority
        self._config = config
        self._lock = threading.Lock()
        self.settled = not debited

    def delivered(self):
        with self._lock:
            self.settled = True

    def _take(self):
        with self._lock:
            if self.settled:
                return False
            self.settled = True
            return True

    def refund(self):
        """Refund now, inside the request/app context (the generator's finally)."""
        if not self._take():
            return
        from .. import billing
        try:
            billing.refund(self._user, priority=self._priority,
                           config=self._config)
        except Exception:
            logging.getLogger('statlee.billing').exception(
                "Failed to refund credit for an undelivered stream")

    def _refund_detached(self):
        """``call_on_close`` hook: runs after the request context is gone, so
        it opens an app context and re-loads the user."""
        if not self._take():
            return
        from .. import billing
        try:
            with self._app.app_context():
                user = None
                if self._user_id is not None:
                    from ..extensions import db
                    from ..models import User
                    user = db.session.get(User, self._user_id)
                billing.refund(user, priority=self._priority,
                               config=self._config)
        except Exception:
            logging.getLogger('statlee.billing').exception(
                "Failed to refund credit after a client disconnect")

    def attach(self, response):
        response.call_on_close(self._refund_detached)
        return response


def sse_stream(generator):
    """Wrap a generator function in a standard Server-Sent Events response."""
    return Response(
        stream_with_context(generator()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def sse_event(payload):
    return f"data: {json.dumps(payload)}\n\n"


def strip_code_fences(text):
    """Defensive fence-stripping for models that ignore the no-markdown rule."""
    final_code = (text or '').strip()
    # Broadened fence tag (P2-6): match any language label (```py, ```python,
    # ```r, ```javascript, or a bare ```), not just python/r, so a mis-tagged
    # fence is stripped cleanly instead of falling through to the line-strip.
    match = re.search(r'```[a-zA-Z]*\n(.*?)```', final_code,
                      re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    if final_code.startswith('```'):
        return '\n'.join(
            line for line in final_code.split('\n')
            if not line.strip().startswith('```'))
    return final_code
