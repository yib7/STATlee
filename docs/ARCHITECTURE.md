# STATlee Architecture & Codebase Guide

A walkthrough of how STATlee is put together, for anyone reading the code for the
first time. It explains the request lifecycle, the analysis pipeline, the
security boundaries, and where each responsibility lives.

## Big picture

STATlee is a **Flask app-factory** (`app.py`) wiring together focused modules and
five route blueprints. The frontend is **vanilla JavaScript** (no build step)
organized under a single `CC` namespace, talking to the backend over JSON and
**Server-Sent Events** (SSE) for streamed generation. There is no SPA framework
and no client bundler; `static/js/*.js` files are served as-is.

```
Browser (static/js/*, templates/index.html)
   │  fetch JSON  +  SSE streams
   ▼
Flask app factory (app.py)  ── middleware: request-id, CSRF, auth gate, rate limit, ProxyFix
   │
   ├─ routes/misc.py      /  /welcome  /health  /metrics  /report_issue  /generate_report
   ├─ routes/auth.py      /login /register /logout /check_auth /history
   ├─ routes/datasets.py  /upload /data_page /classify_variables /suggest /wrangle /export …
   ├─ routes/analyze.py   /chat /run /interpret /method_prompt
   └─ routes/converse.py  /converse
        │
        ├─ llm.py        role-based LLM calls (+ usage, cache)
        ├─ prompts.py    every prompt string builder
        ├─ storage.py    per-identity files + dataset versions
        ├─ sandbox.py    isolated code execution
        ├─ datatools.py  ingestion + profiling
        ├─ billing.py    monetization seam (no-op today)
        ├─ models.py     SQLAlchemy: User / Dataset / AnalysisRun / IssueReport
        └─ config.py     validated, env-driven settings
```

## Configuration (`config.py`)

One `Config` dataclass is the single source of truth. `Config.from_env()` reads
every environment variable **once**, then `validate()` fails fast on hard
requirements (e.g. production must have the selected provider's API key +
`FLASK_SECRET_KEY`) and warns loudly on soft ones (e.g. running the weaker
`subprocess` sandbox in production). Routes never read `os.environ` directly;
they read
`current_app.config['STATLEE']`.

## Request lifecycle (`app.py`)

Every request passes through ordered middleware:

1. **`assign_request_context`**: mints an 8-char `request_id` (correlated into
   every log line via a logging filter), a per-browser `sid` (the file-isolation
   key), and a `csrf_token`.
2. **`csrf_protect`**: double-submit check on `POST/PUT/DELETE`: the
   `X-CSRF-Token` header must equal the session token.
3. **`require_auth`**: gate everything except `PUBLIC_ENDPOINTS` (the loader,
   landing, health, and auth handshake). Behavior depends on the auth *mode*.
4. The view runs; **`stamp_request_id`** adds `X-Request-ID` to the response.

`ProxyFix` is installed when `TRUST_PROXY_HOPS > 0` (default 1 in production) so
the real client IP is read from `X-Forwarded-For` behind Render's proxy.

## Auth modes (`routes/auth.py`)

Three coexisting modes preserve the anonymous-sandbox promise:

- **open**: no password, no login required; anyone can use the sandbox.
- **password**: a single legacy `PASSWORD` gates the whole UI.
- **accounts**: optional email/password accounts (Flask-Login);
  `REQUIRE_LOGIN=true` makes them mandatory.

`is_authorized()` is the single decision the app-level gate calls.

## Identity & storage (`storage.py`)

Each principal gets an isolated directory:

- logged-in users → `user_<id>/` (durable),
- anonymous sessions → `anon_<sid>/` (TTL-cleaned).

`resolve_path()` is the safety chokepoint: it runs `secure_filename`, joins
against the caller's own root, then does a `realpath` containment check, so no
filename (traversal, absolute path) can escape the caller's directory. Datasets
are **version-controlled**: each wrangle writes `stem__vN.csv` tracked by a JSON
manifest with an active-version pointer (undo/redo truncates the redo branch like
an editor's undo stack). The **approved-script** store (`.approved_script.json`)
backs the run-guard.

## The analysis pipeline (`routes/analyze.py`)

`POST /chat` is the heart of the app. It streams over SSE:

1. **Moderation** (`lite`, temp 0): synchronous; must 403 before any stream.
2. **Feature selection** (`flash`): on wide datasets, pick the minimal columns.
3. **Draft** (`draft`→pro): stream the first script, deltas shown live.
4. **Validation** (`lite`): a second pass that replaces the draft client-side.
5. **Save approved script**: the run-guard remembers exactly what was produced.

`POST /run` enforces the **run-guard** (roadmap 0.4): it only executes the
server-approved script. If the submitted code differs (the editor allows
hand-edits), it is **re-moderated** via `code_moderation` and rejected on
`BLOCK` before running. `POST /interpret` streams a plain-English write-up (or
switches to a debugging assistant when the run failed).

The in-app **Pro mode** toggle threads a `pro` flag from the UI into the analysis
path: when set, code generation runs on the larger `pro_max` model
(e.g. `gemini-3.1-pro`) instead of the standard code model, and the request still
passes through `billing.check_and_debit`.

## LLM service (`llm.py`)

All model access funnels through `LLMService`, addressed by **role**
(`pro`/`flash`/`lite`/`draft`, plus `pro_max` for Pro mode) mapped to a model id
in config. The backend is **pluggable**: `LLM_PROVIDER` selects Gemini (default),
Anthropic, or OpenAI behind one provider-neutral seam. Highlights:

- **Usage accounting** per model, exposed at `/metrics` and surfaced live in the
  UI (the `usageBadge`).
- **Pro-mode routing.** Code generation switches to the `pro_max` role when the
  user enables Pro mode; every other role is unchanged.
- **Deterministic cache.** `generate()` calls at `temperature == 0` (moderation,
  code-moderation) are LRU-cached on `(model, json_mode, sha256(content))`, so a
  hammered prompt can't re-bill the API.

The module exposes `init_service` / `get_service` / `set_service`; tests inject a
deterministic `FakeLLMService` via `set_service`.

## Sandbox (`sandbox.py`)

The real security boundary for executed code:

- **`_safe_env`**: an explicit allowlist environment; **no app secret** (LLM
  provider key, Flask secret, SMTP creds, DB URL) is ever present.
- **throwaway working dir**: contains only the one dataset, deleted after the run.
- **POSIX rlimits**: cap memory/CPU/file-size/processes (no-op on Windows dev).
- **`SANDBOX_MODE=docker`**: runs each execution in a network-less, non-root,
  read-only, resource-capped sibling container built from `runner.Dockerfile`.

All `plot*.png` files the run produces are collected and returned as base64.

## Monetization seam (`billing.py` + `models.py`)

There is **no real billing yet**. `User` carries `plan` (`'free'`) and `credits`
(`0`) columns, and `billing.check_and_debit(user, priority=, cost=)` is the
single chokepoint that a future paid tier will implement (plus a Stripe webhook
to top up `credits`). Today it always returns `(True, None)`. Pro mode
already calls it, so turning billing on is *implementing one function*, not a
refactor.

## Frontend (`static/js/`, `templates/index.html`)

| File | Responsibility |
|---|---|
| `api.js` | `CC` namespace: state, fetch/CSRF helpers, SSE parser, sanitized markdown, usage badge, toasts, console-error ring buffer. |
| `data.js` | Upload, data viewer paging, codebook UI. |
| `analyze.js` | Editor (CodeMirror), streamed generation, run + interpret, Pro mode toggle. |
| `converse.js` | Converse tab (mentor + guide mode). |
| `tools.js` / `ui.js` | Report builder, export, tabs, sidebar, theming. |

**Security note:** every piece of LLM/Markdown text reaches the DOM through
`CC.renderMarkdown` → `marked` → **DOMPurify**. That is the only path, which is
the XSS defense (roadmap 0.5).

`templates/landing.html` is the standalone marketing page at `/welcome`
(self-contained CSS, independent of the app shell).

## Testing (`tests/`)

`conftest.py` injects a `FakeLLMService` that maps each prompt to a canned
response by a marker phrase, and gives every test its own temp upload root and
SQLite file. The whole HTTP surface is exercised **offline**: no API key, no
network. Coverage spans uploads, codebook, wrangling/versioning, the run-guard,
converse, export, auth, CSRF, the rate-limit key function, the billing seam, and
Pro-mode routing. `ruff` lints; CI runs ruff + byte-compile + pytest on push.

## Where to start reading

- Want the request flow? `app.py` → `routes/analyze.py`.
- Want the safety model? `sandbox.py` + `storage.resolve_path` + the run-guard in
  `routes/analyze.py`, then [SECURITY_AUDIT.md](SECURITY_AUDIT.md).
- Want to swap a model or add a tier? `config.py` + `llm.py`.
