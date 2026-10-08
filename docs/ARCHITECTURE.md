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
        ├─ billing.py    monetization seam (off by default; real when enabled)
        ├─ models.py     SQLAlchemy: User / AnalysisRun / IssueReport
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

`/health` is exempt from the default rate limit, never mints a session, and
runs a `SELECT 1`, so it answers `503` when the database is unreachable.

`ProxyFix` is installed when `TRUST_PROXY_HOPS > 0` so the real client IP is
read from `X-Forwarded-For` behind a proxy. It defaults to 0 everywhere, since
trusting that header on a directly exposed app lets clients spoof it to dodge
per-IP rate limits; a deploy behind Render's single proxy sets
`TRUST_PROXY_HOPS=1`. The effective value is logged at startup.

## Auth modes (`routes/auth.py`)

Three coexisting modes preserve the anonymous-sandbox promise:

- **open**: no password, no login required; anyone can use the sandbox.
- **password**: a single legacy `PASSWORD` gates the whole UI.
- **accounts**: optional email/password accounts (Flask-Login);
  `REQUIRE_LOGIN=true` makes them mandatory.

`is_authorized()` is the single decision the app-level gate calls.

## Identity & storage (`storage.py`)

Each principal gets an isolated directory:

- logged-in users → `user_<id>/` (durable, as long as the storage root is),
- anonymous sessions → `anon_<sid>/` (TTL-cleaned).

The storage root is `UPLOAD_ROOT`, or `instance/uploads` when unset (testing
uses a throwaway temp dir). In a container, keep it on a persistent volume (the
Dockerfile sets `UPLOAD_ROOT=/app/instance/uploads` and compose mounts a named
volume at `/app/instance`), or logged-in users' files go with the container.

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
5. **Code moderation** (`lite`, temp 0): the run-guard re-moderates the final
   script itself; a `BLOCK` (or a moderation failure) ends the stream with an
   error and nothing is approved.
6. **Static check** (`codecheck.check_code`): the deterministic AST/regex
   denylist runs as the last gate; a hit also ends the stream unapproved.
7. **Save approved script**: only a script that passed both gates is remembered,
   so `/run` executes exactly what was produced and checked.

`POST /run` enforces the **run-guard** (roadmap 0.4): it only executes the
server-approved script. If the submitted code differs (the editor allows
hand-edits), it is **re-moderated** via `code_moderation` and rejected on
`BLOCK` before running. `POST /interpret` streams a plain-English write-up (or
switches to a debugging assistant when the run failed).

The in-app **Pro mode** toggle threads a `pro` flag from the UI into the analysis
path: when set, code generation runs on the larger `pro_max` model
(e.g. `gemini-3.1-pro-preview`) instead of the standard code model, and the request still
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

## Sandbox (`sandbox.py` + `codecheck.py`)

Execution safety is layered; the strength of the boundary depends on the mode:

- **`codecheck.check_code` (non-LLM static pre-check)**: before any sandbox run,
  every generated/edited script passes a deterministic AST denylist (Python) /
  regex denylist (R) that rejects network access, process/shell spawning,
  `os`/env exfiltration, dynamic-exec builtins, reflective sandbox escapes, and
  file opens outside the run directory. It encodes the same prohibitions
  `prompts.code_moderation` describes to the model, so the gate is not solely
  LLM-dependent. Wired into `/chat`, `/run`, and `/wrangle`; fails open only on a
  `SyntaxError` (an unparseable script cannot execute).
- **run-guard (LLM `code_moderation`)**: re-moderates the model's own generated
  script and any hand-edited script before execution (default-deny).
- **`_safe_env`**: an explicit allowlist environment; **no app secret** (LLM
  provider key, Flask secret, SMTP creds, DB URL) is ever present.
- **throwaway working dir**: contains only the one dataset, deleted after the run.
- **POSIX rlimits**: cap memory/CPU/file-size/processes (no-op on Windows dev).

In the default **`SANDBOX_MODE=subprocess`** the pre-check + run-guard are the
boundary: the environment is scrubbed of secrets, but the process shares the
host network and filesystem, so run it only where the host holds nothing
sensitive. **`SANDBOX_MODE=docker`** makes the boundary kernel-enforced:

- **`SANDBOX_MODE=docker`**: runs each execution in a network-less, non-root,
  read-only, resource-capped sibling container built from `runner.Dockerfile`
  (whose analysis-library pins come from `requirements-runner.txt` and must
  match `requirements.txt`; a test enforces it). The shipped app image has **no
  `docker` CLI**, so docker mode needs the app on a host (or a derived image)
  with the CLI and daemon access; from the stock image every run fails with
  "Interpreter not available". The two real-Docker integration tests in
  `tests/test_sandbox.py` skip unless the runner image is built; the CI
  `sandbox-docker` job builds it and runs them.
  When the app itself is containerized with the host docker socket mounted,
  `docker run -v` is resolved by the host daemon against the host filesystem, so
  set `SANDBOX_WORK_ROOT` to a directory bind-mounted at the SAME absolute path
  into the app container (e.g. `/srv/statlee-runs`); run dirs are created under
  it and, on the POSIX docker path, widened to world-accessible so the pinned
  `--user 1000:1000` runner can read `/work`.
- **process-group kill**: in subprocess mode on POSIX the child runs in a new
  session, so a timeout kills the whole process group (`os.killpg`), not just
  the direct child. On Windows the direct child is killed.

All `plot*.png` files the run produces are collected and returned as base64.

## Monetization seam (`billing.py` + `models.py`)

Billing is **off by default and real when enabled**. `User` carries `plan`
(`'free'`) and `credits` columns, and `billing.check_and_debit(user, priority=,
cost=)` is the single chokepoint every priced request passes through. With
`BILLING_ENABLED=false` (the default) it never touches credits; it only enforces
the global monthly ceiling on priority calls against the operator's key
(`MONTHLY_PRIORITY_CALL_CEILING`, which applies with billing on or off). With
billing on, it also does an atomic conditional credit debit (no double-spend or
negative balance under concurrency), refunds the credit if the request then
fails, and tops a free user up to `MONTHLY_FREE_CREDITS` once per calendar month. A
`flask grant-credits <email> <n>` CLI grants credits manually. What is *not*
built is a payment processor: wiring Stripe Checkout + a webhook to sell credits
is the remaining step, and it is deliberately left as a human action because it
is the point where real money and secrets enter.

## Database schema & migrations (`migrations/`, `app.py`)

The schema is versioned with Alembic (Flask-Migrate). At boot `create_app`
brings the database up to date:

- a fresh DB is migrated to head;
- an unstamped DB (tables but no `alembic_version`) whose schema already
  matches the models is stamped at head (nothing to replay);
- an unstamped DB that is behind the models is treated as a legacy
  `create_all()` DB: stamped at the baseline revision `171777e71dff`, then
  upgraded. If that upgrade fails, the stamp is removed again, so the DB is
  never left claiming a revision it did not reach;
- an already-migrated DB just upgrades to head.

After upgrading, the boot compares the live schema with the models
(`alembic.autogenerate.compare_metadata`). A missing table or column (a DB
stamped at head but lacking, say, `users.email_verified`) is logged as an
actionable error, and in production the boot refuses to start rather than
serving 500s. `flask --app wsgi statlee-doctor` prints the same report.

Each DB URI is handled once per process. The boot upgrade is skipped for
`flask ... db <cmd>` and `flask ... statlee-doctor` (and whenever
`STATLEE_SKIP_BOOT_MIGRATE=1`), so the Alembic CLI stays usable as a repair
tool: `db current` reports the real revision and `db stamp head` runs even on a
DB the boot upgrade cannot handle. The testing env keeps plain `create_all()`
for fast throwaway DBs; `test_models_match_migration_head` and the CI
`flask db check` step fail when the models and the migration head drift. Add a
schema change with `flask --app wsgi db migrate -m "..."`, review the generated
file under `migrations/versions/`, and the boot upgrade applies it on the next
deploy.

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
network. The suite is hermetic: importing the `statlee` package never loads
`.env` (only `wsgi.py` does, and never under `APP_ENV=testing`), and `conftest`
sets `PYTHON_DOTENV_DISABLED=1` and scrubs provider keys plus `LLM_PROVIDER` /
`MODEL_*`, so a developer's `.env` or shell cannot steer results. Coverage spans uploads, codebook, wrangling/versioning, the run-guard,
converse, export, auth, CSRF, the rate-limit key function, the billing seam, and
Pro-mode routing. `ruff` lints; CI runs ruff + byte-compile + pytest on push.

## Where to start reading

- Want the request flow? `app.py` → `routes/analyze.py`.
- Want the safety model? `sandbox.py` + `storage.resolve_path` + the run-guard in
  `routes/analyze.py`.
- Want to swap a model or add a tier? `config.py` + `llm.py`.
