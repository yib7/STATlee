# Multi-Provider LLM Layer — Design (reference implementation)

**Date:** 2026-06-20
**Status:** Approved design → ready for implementation plan
**Scope:** STATlee (this repo) is the **reference implementation**. xenoblade vendors the
same backend interface + provider config into its (future) strategy layer. **scrape_data and
twitter_project are explicitly out of scope** — see Scope below.

---

## Goal

Make the app provider-agnostic across **Gemini, Anthropic (Claude), and OpenAI**, addressed by
**role/tier**, with **Gemini as the default** so the user never has to juggle keys. Each role maps
to its "most similar brother" on every provider (e.g. the `lite` tier → Gemini Flash-Lite ↔ Claude
Haiku ↔ OpenAI nano). Model IDs live in **one config surface** with a **live verification step** so
a deprecated/renamed model is caught before a run breaks.

The existing single-provider behaviour must be **unchanged** when only `GEMINI_API_KEY` is set.

---

## Decisions (locked with the user)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Default provider | **Gemini** (`LLM_PROVIDER` env, default `gemini`). User keeps using one key. |
| 2 | Addressing | By **role/tier** (`pro`/`flash`/`lite`/`draft`), never a raw model name — same as today. |
| 3 | Provider seam | Add `AnthropicBackend` + `OpenAIBackend` **alongside** the existing `GeminiBackend`, same interface. `LLMService` (routing, cache, usage, priority, streaming) stays provider-agnostic. |
| 4 | Model IDs | Live in config (per-provider role→model map), env-overridable; **`verify.py` checks them against each provider's models-list endpoint**. |
| 5 | SDKs | Official SDKs only: `google-genai`, `anthropic`, `openai`. **No OpenAI-compat shims** (the user's Gemini `AQ.` keys 401 on the compat endpoint, and Claude must use the native `anthropic` SDK). |
| 6 | Sharing | STATlee is the reference; xenoblade **vendors a copy** of `llm/backends/*` + `models` config. No cross-repo package yet (YAGNI). |

---

## Current architecture (what we're extending)

`statlee/llm.py` already has the right seams:

- `LLMService` — owns role resolution (`_resolve`), the `priority` escalation, a deterministic
  temperature-0 cache, usage accounting (`usage_snapshot`), and `generate()` / `stream()`.
- `GeminiBackend` — owns *only* the wire protocol: `generate(model, contents, *, temperature,
  json_mode) -> LLMResult` and `stream(model, contents, *, temperature, usage_out)`.
- `MediaPart` — a **provider-neutral** binary part (`data`, `mime_type`); each backend translates
  it to its own SDK shape. This is the key that makes multi-provider clean — routes already pass
  neutral content.
- Roles `pro`/`flash`/`lite`/`draft` resolve via `config.model_pro` / `model_flash` /
  `model_flash_lite`.

The seam is good: **we add a provider dimension under `LLMService`, not a parallel stack.**

---

## Architecture

### Component A — backend interface (extract, don't rewrite)

Promote the implicit `GeminiBackend` contract to an explicit base so every provider is
interchangeable:

```python
class LLMBackend(Protocol):
    def generate(self, model, contents, *, temperature, json_mode) -> LLMResult: ...
    def stream(self, model, contents, *, temperature, usage_out): ...  # yields text deltas
```

`LLMResult` and `MediaPart` are unchanged. `GeminiBackend` already satisfies this — no behaviour
change.

### Component B — new backends (same interface)

**`AnthropicBackend`** (official `anthropic` SDK):
- `system` + `user` → `messages=[{"role":"user",...}]` with the system text on the `system` param.
- **Do not send `temperature`/`top_p`/`top_k` to Opus 4.8 / Sonnet 4.6 — they return HTTP 400.**
  The backend drops sampling params for Claude; if reasoning depth is wanted, use
  `thinking={"type":"adaptive"}` + `output_config.effort`, not a token budget.
- `json_mode` → `output_config={"format":{"type":"json_schema",...}}` (or `messages.parse()`);
  return parsed text as `LLMResult.text`.
- `MediaPart` → image/document content blocks.
- Usage from `response.usage` (`input_tokens` / `output_tokens`). Stream via `client.messages.stream`
  + `get_final_message()`; large outputs stream.

**`OpenAIBackend`** (official `openai` SDK):
- `system`/`user` → chat messages. `json_mode` → `response_format` json_schema.
- `MediaPart` → image content parts. Usage from `response.usage`.

Both reuse the existing logging/usage shape so `usage_snapshot()` works unchanged.

### Component C — provider selection (config)

- `config.llm_provider` ← `LLM_PROVIDER` env, **default `gemini`**.
- `LLMService.__init__` instantiates the one backend for the active provider. If that provider's
  key is missing, **fail loud** with an actionable message (no silent fallback).
- Default path: `LLM_PROVIDER` unset + only `GEMINI_API_KEY` present → **identical to today.**

### Component D — per-provider role→model map (single source)

Config grows from three flat fields to a per-provider map (env-overridable, one place to edit):

| Role / tier | Gemini (default) | Anthropic | OpenAI |
|---|---|---|---|
| `pro` / `draft` | `gemini-3.5-flash` ¹ | `claude-opus-4-8` | `gpt-5.5` ² |
| `flash` | `gemini-3.5-flash` | `claude-sonnet-4-6` | `gpt-5.4` ² |
| `lite` | `gemini-3.1-flash-lite` | `claude-haiku-4-5` | `gpt-5.4-nano` ² |

¹ Gemini `pro` is mapped to `gemini-3.5-flash` today per the scrape_data key-strategy decision;
bump to `gemini-3.1-pro-preview` later via one env var. ² OpenAI IDs are the **correct families
as of 2026-06; the exact ID strings must be confirmed by `verify.py`** (see Component E). Claude IDs
are exact and authoritative.

### Component E — `verify.py` ("ensure latest models")

A small check (CLI + CI-runnable) that, for the configured provider(s), lists models via the
native SDK — Gemini `client.models.list()`, Anthropic `client.models.list()`, OpenAI
`client.models.list()` — and asserts every ID in the role→model map still resolves. **Warn (or fail
CI) on any miss.** This is the deprecation safety net; it keeps all IDs in one file to update.

---

## Default-behaviour guarantee

With `GEMINI_API_KEY` set and `LLM_PROVIDER` unset: the active backend is `GeminiBackend`, the role
map is the Gemini column, and `generate()`/`stream()`/cache/usage behave exactly as before. No route
changes; routes still call `service.generate(role, contents, ...)`.

---

## Error handling

| Situation | Behaviour |
|-----------|-----------|
| Selected provider's key missing | Fail at init with an actionable message. |
| Sampling params on Claude Opus 4.8/Sonnet 4.6 | Backend drops them (would otherwise 400). |
| Provider returns non-JSON in `json_mode` | Existing `_extract_json`-style tolerance per backend; raise on hard failure. |
| `verify.py` finds a missing model ID | Warn (CLI) / fail (CI) naming the role+provider+ID. |
| Large output | Stream; backends use the SDK streaming helper. |

---

## Testing

- **Fake backends** for each provider (stub `generate`/`stream`) injected via `set_service` —
  no network. Assert role→model resolution per provider, JSON handling, `MediaPart` translation
  shape, usage accounting, and the temperature-0 cache still work for every backend.
- **Provider selection:** `LLM_PROVIDER` unset → Gemini; set to `anthropic`/`openai` → that backend;
  missing key → fail-loud.
- **Default-unchanged regression:** with only `GEMINI_API_KEY`, existing `llm` tests pass byte-for-byte.
- **`verify.py`:** mock each SDK's `models.list()`; pass when all IDs present, fail naming the
  missing one.

---

## Verification (done = )

1. `pytest -q` green, no regressions.
2. `LLM_PROVIDER=gemini` (default) run is identical to today.
3. `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`: one analysis request routes through Claude;
   usage snapshot shows a `claude-*` model.
4. `python -m statlee.llm.verify` lists models and reports all-green for the active provider.

---

## Scope (explicit)

- **In:** STATlee (reference), xenoblade (vendors the backends + model map into its strategy layer).
- **Out — scrape_data:** deliberately **Gemini-only + `keypool.py`** for cost (3 free-tier keys →
  Vertex backstop). Adding per-token providers to its high-volume scorer would defeat "being cheap."
  See `scrape_data/.autopilot/DECISIONS.md` (2026-06-20). Do **not** wire this layer there.
- **Out — twitter_project:** no LLM (client-side, no backend); its plan is frontend-only.

## File manifest

**Create:** `statlee/llm/backends/anthropic.py`, `statlee/llm/backends/openai.py`,
`statlee/llm/verify.py`, tests for each.
**Modify:** `statlee/llm.py` (extract `LLMBackend` base; provider selection in `LLMService`),
`statlee/config.py` (per-provider role→model map, `llm_provider`), `.env.example`
(`LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`), `requirements.txt` (`anthropic`, `openai`).
