# CodeCaster — Implementation Plan & Roadmap

> **Status:** Implemented. The backlog below has been built out in this branch
> (see [Implementation status](#implementation-status-2026-06-12)). Items are grouped
> into tiers; each lists **why**, a **concrete approach** (with file paths),
> **acceptance criteria**, an **effort** estimate (S ≈ <½ day, M ≈ 1–2 days, L ≈ 3+ days),
> and **risk**. The descriptions are kept as the design record; the status section maps
> each tier to the modules that now implement it.
>
> **Revision (2026-06-12):** merged the second feature-ideas batch (extended formats, zip
> export, data wrangling, report builder, split panes, SaaS platform, UX fixes, etc.) into
> this document, then implemented it. Duplicates between the two lists were collapsed into
> single items, and requested features that already existed were moved to the
> ["Already covered" ledger](#already-covered--no-action-needed) at the end.

**Goal:** Take CodeCaster from a polished prototype to a safe, production-ready,
maintainable platform — then grow its analytical feature set, and eventually evolve it
into a multi-user product.

**Tech stack (current):** Python 3.11+ / Flask (app factory + blueprints), pandas +
statsmodels, provider-agnostic LLM service (Google GenAI multi-model, optional Anthropic
Claude for drafting), subprocess **or** Docker sandbox execution, Flask-SQLAlchemy /
Flask-Login / Flask-Limiter, vanilla JS (modular `CC` namespace) + Tailwind + CodeMirror,
SSE streaming. Tested with pytest (fake-LLM client) and linted with ruff in CI.

---

## Implementation status (2026-06-12)

The original ~880-line monolithic `app.py` was decomposed (3.1) and the full roadmap was
implemented. Status by tier:

| Tier | Scope | Status | Primary modules |
|---|---|---|---|
| 0 | Execution safety | ✅ Done | `sandbox.py` (env scrub, per-run dir, rlimits, Docker mode), `routes/analyze.py` (run-guard), `routes/converse.py` (0.6 moderation), `static/js/api.js` (DOMPurify) |
| 1 | Multi-tenant correctness & production | ✅ Done | `storage.py` (per-identity isolation), `app.py` (CSRF, cookies, rate limits, generic errors, request-id logs), `Dockerfile` (gunicorn) |
| 2 | Tests, deps, CI | ✅ Done | `tests/` (83 tests, fake LLM), `pyproject.toml`, `.github/workflows/ci.yml`, `.pre-commit-config.yaml`, pinned `requirements*.txt` |
| 3 | Architecture, cost & observability | ✅ Done | module split, `config.py`, `llm.py` (role routing + usage), `/metrics`, UI usage badge |
| 4 | UX quick wins | ✅ Done | footer fix, single-pass converse, loading states, pipeline checklist, suggestion reroll, reset, logo |
| 5 | Analytical & data features | ✅ Done | `datatools.py` (formats/metadata), multi-plot, `/export`, CodeMirror editor, streamed draft, caches, history, auto-debug, refine mode, survey→codebook, method picker, guide mode, `/wrangle` + versioning, report builder |
| 6 | Workspace & support | ✅ Done | split-pane scaffolding + pop-out codebook (`static/js/ui.js`), `/report_issue` (6.3) |
| 7 | Multi-user SaaS | ✅ Done (S3 is a stub) | `models.py`, `routes/auth.py` (accounts), per-user persistence; anonymous sandbox mode preserved. `STORAGE_BACKEND=s3` is wired in config but the S3 backend itself is left as a deployment task. |

**Verification:** `ruff check .` is clean; `pytest -q` → 83 passed; the app boots in
`testing` and `development` modes and serves `/`, `/health`, `/metrics`, and `/check_auth`.

**Known follow-ups (not blocking):** the actual S3 object-store backend (7.3) is a
config-level stub; true CodeMirror split-pane editing in pane B is scaffolded but the
secondary pane currently mirrors content rather than hosting an independent editor.

---

## 0. Already done in the cleanup pass (context)

These shipped in the hardening branch and are **not** part of this backlog — listed so
you know the current baseline:

- Path-traversal fix: all filename-based reads go through `resolve_dataset_path()`.
- XSS escaping for the Data Viewer, Codebook, Suggestions, and Converse bubbles.
- `FLASK_SECRET_KEY` fallback now warns loudly (multi-worker session breakage).
- Centralized Gemini model IDs (`MODEL_PRO` / `MODEL_FLASH` / `MODEL_FLASH_LITE`).
- `sse_stream()` helper replaced 3× duplicated SSE boilerplate.
- `print()` → `logging`; `.gitignore`, README, GEMINI.md, docker-compose, `.env.example` cleanup.

**Not yet addressed (this document):** real execution isolation, multi-tenant file
isolation, production server config, dependency migration, tests/CI, modularization,
and the feature backlog.

---

## Tier 0 — Execution safety (do first)

> The single most important area. `/run` executes model- or client-supplied code.
> Today that code runs **in the web container, as root, with `GEMINI_API_KEY` in the
> environment, with full network access, and no resource limits**. The moderation step
> only gates `/chat` (generation) — a client can POST arbitrary code straight to `/run`
> and bypass it entirely. Treat the README's "sandboxed" claim as aspirational until
> this tier is done.

### 0.1 Scrub secrets & lock down the subprocess environment (quick win)

**Why:** Generated code can currently read `os.environ['GEMINI_API_KEY']` and exfiltrate
it (the live deploy's key). Even before full container isolation, the subprocess should
get a minimal, secret-free environment.

**Approach** — in `app.py` `run_code()`, build an explicit env instead of inheriting:

```python
SAFE_EXEC_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "HOME": app.config['UPLOAD_FOLDER'],
    "MPLBACKEND": "Agg",          # force headless matplotlib
    "OPENBLAS_NUM_THREADS": "2",  # cap BLAS thread fan-out
    "LANG": "C.UTF-8",
}
result = subprocess.run(
    cmd, capture_output=True, text=True,
    cwd=run_dir, timeout=60, env=SAFE_EXEC_ENV,
)
```

**Acceptance:** A generated script that prints `os.environ.get('GEMINI_API_KEY')` outputs
`None`. Plots still render (matplotlib uses Agg).

**Effort:** S. **Risk:** Low — confirm R scripts still find `Rscript` on `PATH`.

### 0.2 Per-run working directory + resource limits

**Why:** All runs currently share `UPLOAD_FOLDER` as `cwd` and a single hardcoded
`plot.png`. Concurrent runs race (user A can be served user B's plot, or have theirs
deleted mid-run). There's also no memory/CPU/output cap, so generated code can OOM or
fill the disk.

**Approach:**
- Create `sandbox.py` exposing `run_in_sandbox(code, language, dataset_path) -> RunResult`.
- Per call: `run_dir = tempfile.mkdtemp()`, copy/symlink only the one dataset into it,
  write the script there, run with `cwd=run_dir`, collect `run_dir/plot*.png`, then
  `shutil.rmtree(run_dir)` in a `finally`.
- On POSIX, apply limits via a `preexec_fn` using `resource.setrlimit` (RLIMIT_AS for
  memory ~2 GB, RLIMIT_CPU, RLIMIT_FSIZE, RLIMIT_NPROC). Document that this is Linux/Docker
  only (no-op on the Windows dev host).
- Truncate captured stdout/stderr to a max (e.g. 256 KB) before returning.

**Acceptance:** Two simultaneous `/run` requests never see each other's files; a script
allocating 8 GB is killed with a clean error, not a server OOM; `run_dir` is always removed.

**Effort:** M. **Risk:** Medium — `setrlimit` tuning; ensure cleanup on timeout/exception.

### 0.3 True container isolation for execution (the real sandbox)

**Why:** 0.1/0.2 reduce blast radius but the code still runs in the app process. Real
isolation means a separate, throwaway, network-less, non-root container per execution.

**Approach (pick one, document trade-offs in an ADR under `docs/adr/`):**
- **A — Sibling containers via Docker API (recommended):** App calls the Docker socket to
  `docker run --rm --network none --read-only --user 1000:1000 --memory 2g --cpus 1
  --pids-limit 128 --cap-drop ALL --security-opt no-new-privileges
  -v <run_dir>:/work:rw -w /work codecaster-runner <python|Rscript> script`.
  Requires mounting `/var/run/docker.sock` (itself a privilege — document it) or a
  rootless Docker / Sysbox setup. Ship a minimal `runner.Dockerfile` with just
  Python+pandas+statsmodels+matplotlib and the R stack.
- **B — gVisor (`runsc`)** as the runtime for option A, for kernel-level isolation.
- **C — Managed sandbox** (e.g. E2B / Modal) if you'd rather not run the infra.

**Acceptance:** Generated code cannot reach the network (`requests.get` fails), cannot
read the app source or other users' uploads, runs as non-root, and is killed at the
memory/CPU/pids caps. App container no longer runs untrusted code in-process.

**Effort:** L. **Risk:** High — infra/permissions; needs a staging environment to validate.

### 0.4 Moderate (or refuse) raw `/run` payloads

**Why:** Moderation lives only on `/chat`. `/run` accepts arbitrary `code` from the
client, so the safety filter is trivially bypassed.

**Approach:** Either (a) only execute code the server generated this session — store the
last validated code server-side (keyed by session) and have `/run` ignore client-sent
code in favor of it; or (b) re-run moderation on the submitted code. Option (a) is
stronger and also enables the "editable code" feature (5.4) to round-trip through the
server. Combine with rate limiting (1.4).

**Acceptance:** Posting hand-crafted code that wasn't generated/approved this session is
rejected (or re-moderated). Normal generate→run flow is unchanged.

**Effort:** M. **Risk:** Medium — interacts with the editable-code feature; design together.

### 0.5 Sanitize rendered interpretation HTML

**Why:** `/interpret` and `/converse` text is rendered with `marked.parse()`, which passes
raw HTML through. A model that emits `<img onerror=...>` becomes stored/cached XSS. This
is the one injection sink intentionally left from the cleanup pass.

**Approach:** Add DOMPurify (pinned CDN with SRI, or vendored under `static/vendor/`) and
wrap every `marked.parse(x)` as `DOMPurify.sanitize(marked.parse(x))` in
`templates/index.html`. A tiny `renderMarkdown()` helper centralizes it.

**Acceptance:** Markdown still renders (headers, bold, lists); an interpretation containing
`<img src=x onerror=alert(1)>` does not execute.

**Effort:** S. **Risk:** Low.

### 0.6 Converse guardrails & moderation

**Why:** The `/chat` pipeline has a moderation gate (`app.py:522`), but `/converse` has
**none** — and no persona constraint either. A user can steer the Converse tab into
generating arbitrary code or off-topic/malicious content, bypassing the Generate-tab
safety pipeline entirely. The two features should stay isolated: Converse explains,
Generate codes.

**Approach:**
- **Backend (`app.py` `/converse`):** Prepend the same baseline moderation check used by
  `/chat` (`MODEL_FLASH_LITE`, temperature 0.0) before the answer call.
- Update the system prompt: the persona is strictly limited to explaining concepts,
  interpreting statistics, and discussing the methodology of the current analysis. Add a
  hard constraint: *"You must completely refuse any request to write, modify, or output
  executable code snippets. Redirect the user to the 'Generate Code' tab for coding
  tasks."* (Explaining existing code remains allowed; producing new/modified code is not.)
- Design together with **4.2** (single-pass Converse) so the moderation gate and persona
  rules land in the consolidated call rather than being added to a pipeline that's about
  to change.

**Acceptance:** Asking Converse to "write me a script that scrapes Twitter" is refused
with a redirect to the Generate tab; "what does this p-value mean?" works normally;
prompts that `/chat` would block are blocked here too.

**Effort:** S. **Risk:** Low — prompt-only; verify refusals don't over-trigger on
legitimate "explain this code" questions.

---

## Tier 1 — Multi-tenant correctness & production readiness

### 1.1 Per-session file isolation

**Why:** `UPLOAD_FOLDER` is one shared dir and files are keyed by `secure_filename`. Two
users uploading `survey.csv` collide, and any client can read any dataset by guessing its
name via `/data_page` / `/extract_pdf_codebook`. On a public deploy this is a real
data-leak between users. *(This is also the session-scoped stepping stone toward the
full per-user isolation in Tier 7.)*

**Approach:**
- Give each browser session a random id: in a `@app.before_request`, set
  `session['sid'] = session.get('sid') or secrets.token_hex(16)`.
- Namespace storage: `user_dir = os.path.join(UPLOAD_FOLDER, session['sid'])`.
- Extend `resolve_dataset_path(filename)` to resolve **within `user_dir`** and to keep its
  realpath-containment check. No endpoint can then reach another session's files.
- Update `cleanup_old_files()` to walk per-session subdirs and remove empty ones.

**Acceptance:** Two sessions uploading the same filename keep independent data; a request
with another session's filename returns "Invalid filename". Cleanup still prunes >2h files.

**Effort:** M. **Risk:** Medium — touches every file-reading endpoint; cover with tests (2.1).

### 1.2 Production WSGI server + correct port binding

**Why:** `Dockerfile` runs `flask run` (dev server: single-threaded, not for production)
and **ignores `$PORT`**, while the `__main__` block reads `$PORT`. On hosts that inject a
port, the two disagree. `gunicorn` is in `requirements.txt` but never used.

**Approach:**
- Change the Dockerfile `CMD` to gunicorn honoring `$PORT`, with threaded workers (needed
  for SSE long-lived responses):
  ```dockerfile
  CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --threads 8 \
       --timeout 120 --graceful-timeout 30 app:app"]
  ```
- With ≥2 workers, `FLASK_SECRET_KEY` becomes mandatory — fail fast at startup in prod if
  unset (see 1.3). Add a Docker `HEALTHCHECK` hitting `/health`.
- Verify SSE streaming still flushes under gunicorn (threads worker class; keep
  `X-Accel-Buffering: no`).

**Acceptance:** Container binds `$PORT`; `/chat` and `/interpret` still stream
incrementally; health check passes; sessions survive across workers.

**Effort:** M. **Risk:** Medium — **changes the live deploy's start command; test on staging first.**

### 1.3 Centralized, validated configuration

**Why:** Env access is scattered; some missing values fail late or silently.

**Approach:** Add `config.py` with a `Config` dataclass that reads/validates env once
(`GEMINI_API_KEY` required; in prod `FLASK_SECRET_KEY` required; parse `PORT`, `PASSWORD`,
feature-selection threshold, upload limit, exec timeout). `app.py` imports `Config`. Surface
an `APP_ENV={development,production}` flag to drive prod-only strictness and cookie flags (1.5).

**Acceptance:** Missing required prod vars abort startup with a clear message; tunables
(timeout, threshold, max upload) come from config, not literals.

**Effort:** S–M. **Risk:** Low.

### 1.4 Rate limiting & request guards

**Why:** Each `/chat`, `/suggest`, `/interpret`, `/run` triggers paid LLM calls and/or code
execution. Nothing throttles abuse.

**Approach:** Add `Flask-Limiter` (memory backend for single-instance; Redis when scaled).
Suggested limits: `/run` 10/min/session, `/chat` 20/min, global generous default. Return
`429` with a friendly message; surface it in the frontend status line.

**Acceptance:** Exceeding the limit returns 429 without touching Gemini/subprocess; normal
use is unaffected.

**Effort:** S. **Risk:** Low.

### 1.5 Session cookie & CSRF hardening

**Why:** Session cookies lack explicit flags, and authenticated POST endpoints have no CSRF
protection — a logged-in user visiting a hostile page could be made to run code.

**Approach:** Set `SESSION_COOKIE_HTTPONLY=True`, `SESSION_COOKIE_SAMESITE='Lax'`, and
`SESSION_COOKIE_SECURE=True` in production. Add `Flask-WTF` CSRF (or a custom double-submit
token) to state-changing routes; the frontend sends the token with each `fetch`. SameSite=Lax
already blocks most cross-site POSTs; CSRF tokens are defense-in-depth.

**Acceptance:** Cookies carry the flags; a cross-origin POST without a token is rejected;
same-origin app flows work.

**Effort:** M. **Risk:** Medium — must thread tokens through all `fetch` calls.

### 1.6 Don't leak `str(e)` to clients

**Why:** Several handlers return `jsonify({'error': str(e)})`, leaking internals (paths,
stack details) to the client.

**Approach:** Return generic messages (e.g. "Could not read dataset.") to the client; log
full detail server-side via `logger.exception`. Add a global error handler for 500s.

**Acceptance:** Client error bodies are generic; full tracebacks appear only in logs.

**Effort:** S. **Risk:** Low.

---

## Tier 2 — Dependencies, tests, CI

### 2.1 Test suite (pytest)

**Why:** Zero tests today. The fixes above (path traversal, per-session isolation, run
guard) need regression coverage, and the Gemini calls must be mockable.

**Approach:** Add `tests/` with `pytest` + `pytest-flask`. Patch the module-level `client`
with a fake that returns canned `.text` / streams, so no network/keys are needed. Priority
cases:
- `resolve_dataset_path`: rejects `../`, absolute paths, empty; accepts normal names;
  (after 1.1) rejects other sessions' files.
- `/upload`: rejects non-CSV, enforces size, returns a correct profile.
- `/upload_pdf`: rejects >50-page PDFs; converts TXT→PDF.
- `/data_page`: pagination math + filter behavior; bad filename → 400.
- `/run`: secret-free env (0.1); timeout path; plot round-trips as base64.
- `streamSSE` payload parsing (a small JS/Vitest test, or assert server SSE framing).

**Acceptance:** `pytest -q` green locally and in CI with no API key set.

**Effort:** L. **Risk:** Low — mostly additive; design the genai fake once and reuse.

### 2.2 Migrate deprecated / unpinned dependencies

**Why:** `PyPDF2` is EOL (superseded by `pypdf`); `fpdf` (1.x) is unmaintained (`fpdf2` is
the live fork); nothing is version-pinned, so builds aren't reproducible.

**Approach:**
- `PyPDF2` → `pypdf` (`from pypdf import PdfReader`; API is compatible for `len(reader.pages)`).
- `fpdf` → `fpdf2`, or replace TXT→PDF conversion with `reportlab`. Re-test the page-count
  enforcement either way.
- Pin all of `requirements.txt` to known-good versions; consider `pip-tools`
  (`requirements.in` → compiled `requirements.txt`) or `uv` for a lockfile.

**Acceptance:** `pip install -r requirements.txt` resolves deterministically; PDF/TXT upload
paths pass their tests (2.1).

**Effort:** M. **Risk:** Medium — exercise upload paths after swapping.

### 2.3 CI pipeline

**Why:** No automated checks; regressions can ship silently.

**Approach:** `.github/workflows/ci.yml`: matrix on Python 3.12; steps = install,
`ruff check` (add `ruff` config), `python -m py_compile app.py`, `pytest`. Optionally build
the Docker image. Add `pre-commit` (ruff, end-of-file-fixer, trailing-whitespace) in
`.pre-commit-config.yaml`.

**Acceptance:** PRs run lint + compile + tests; red blocks merge.

**Effort:** S–M. **Risk:** Low.

---

## Tier 3 — Architecture, cost & observability

### 3.1 Modularize the backend

**Why:** `app.py` is ~880 lines mixing config, prompts, model routing, file I/O, execution,
and routes. It's coherent but at the edge of comfortably-holdable size, and prompts are
buried in route bodies.

**Approach (incremental, behind tests from 2.1):**
- `config.py` (1.3), `sandbox.py` (0.2/0.3).
- `gemini_service.py`: every model call + a `prompts/` package (one module/file per prompt:
  moderation, feature-selection, draft, validation, classify, suggest, interpret, converse).
  Routes call `gemini_service.classify(df)` etc. and stop embedding prompt text.
- Flask **blueprints**: `routes/auth.py`, `routes/datasets.py` (upload, data_page, codebook),
  `routes/analyze.py` (chat, run, interpret), `routes/converse.py`. `app.py` becomes a thin
  factory (`create_app()`).

**Acceptance:** Behavior unchanged (tests green); each module has one responsibility; prompts
are editable without touching route logic.

**Effort:** L. **Risk:** Medium — do it in small, test-backed steps, not one big bang.

### 3.2 Extract & harden the frontend

**Why:** `templates/index.html` is ~1,225 lines of inline HTML+CSS+JS. The Tailwind **CDN
build prints a console warning that it's not for production**, and CDN scripts (`marked`,
Tailwind) are unpinned with no SRI.

**Approach:**
- Split JS into `static/js/` modules (auth, upload, table, codebook, analyze, converse,
  sse) and CSS into `static/css/app.css`; load via `url_for('static', ...)`.
- Pin CDN versions + add SRI hashes, or vendor them under `static/vendor/`.
- Move Tailwind to a real build (CLI/PostCSS) producing a purged `static/css/tailwind.css`,
  removing the production warning and shrinking payload.
- Optional: a tiny `package.json` + build step; keep it framework-free to preserve the
  current simplicity.

**Acceptance:** UI identical; no Tailwind production warning; external scripts pinned/integrity-checked; JS lives in reviewable files.

**Effort:** L. **Risk:** Medium — careful not to regress the SSE/streaming UI behavior.

### 3.3 Observability

**Why:** No request correlation, no LLM token/cost visibility — hard to debug or budget.

**Approach:** Add a request-id (`X-Request-ID`) into log context; log Gemini `usage_metadata`
(input/output tokens) per call with the model id; expose a `/metrics` (Prometheus) or simple
structured JSON logs. Track per-route latency.

**Acceptance:** Each request's logs are correlated by id; token usage per model is queryable.

**Effort:** M. **Risk:** Low.

### 3.4 Model routing & cost optimization

**Why:** A full upload→suggest→generate→run→interpret→converse workflow makes **11 LLM
calls**, several arguably over-provisioned, and two endpoints make two sequential calls
each (answer + format), doubling latency and cost. Model routing is hardcoded and there
is no cost visibility. For a product like this, keeping cost low and speed high — without
sacrificing quality — is integral.

**Approach:**
- **Audit the call sites** (current state, `app.py`):

  | Call | Line | Model |
  |---|---|---|
  | PDF codebook extraction | `:288` | FLASH |
  | Variable classification | `:388` | FLASH |
  | Suggestions | `:477` | FLASH |
  | Moderation gate | `:522` | FLASH_LITE |
  | Feature selection (wide data) | `:597` | FLASH |
  | Code draft | `:657` | **PRO** |
  | Code validation | `:686` | FLASH_LITE |
  | Interpretation (deep) | `:814` | FLASH |
  | Interpretation (format) | `:842` | FLASH_LITE |
  | Converse (deep) | `:894` | **PRO** |
  | Converse (format) | `:922` | FLASH_LITE |

- **Collapse the two-pass pipelines:** fold the formatting rules into the main prompt for
  `/interpret` and `/converse` and drop the second FLASH_LITE pass (see 4.2 for the
  Converse half). Saves 2 calls and one full round-trip of latency per use.
- **Downshift where quality holds:** candidates — Converse deep answer PRO→FLASH
  (conversational explanation rarely needs the Pro model); evaluate whether the
  validation pass can merge into the draft via a stronger draft prompt. Decide with a
  small fixed evaluation prompt-suite run before/after each change.
- **Evaluate the Claude API for the code-drafting stage** (`:657`) — Claude models are
  particularly strong at code generation. Route every model call through the centralized
  config (1.3) so provider/model is a config swap, then A/B the drafting stage
  (quality / latency / cost) before committing.
- **Surface usage:** log `usage_metadata` per call (3.3) and show a per-analysis
  token/cost summary in the Results tab; optionally expose an admin model-selection
  control. *(Absorbs the original "In-UI model selection & cost display" item.)*

**Acceptance:** The standard end-to-end flow makes ≤9 LLM calls with no quality regression
on the evaluation suite; per-analysis cost is visible; switching the configured drafting
model/provider requires no code change.

**Effort:** M. **Risk:** Medium — quality regressions from downshifting; gate every change
on the evaluation suite.

---

## Tier 4 — UX quick wins & fixes

> Small, independent frontend-leaning items (all S effort). They can interleave with any
> tier — good fillers between the heavier infrastructure work.

### 4.1 Fix the Converse input bar being cut off by the footer

**Why:** The "Sandbox Environment • Data not stored" footer is `fixed bottom-0 … z-50`
(`templates/index.html:363`) and overlays the Converse tab's "Ask a follow-up question…"
input row (`:336–341`) at common viewport sizes, leaving it partially hidden or
unclickable.

**Approach:** Frontend only — add bottom padding equal to the footer height to the app
shell (or to the Converse input container), or make the footer static/in-flow. Verify the
`#chatLog` scroll area still fills correctly in both themes and that other tabs aren't
similarly clipped.

**Acceptance:** Input and send button fully visible and clickable at 1366×768 and
smaller; footer remains visible elsewhere.

**Effort:** S. **Risk:** Low.

### 4.2 Converse single-pass output restructure

**Why:** `/converse` answers with `MODEL_PRO` (`app.py:894`), then re-formats the answer
with a second FLASH_LITE call (`:922`). The second pass adds latency and cost and is the
prime suspect for the reported "weird output structure" — a rewrite step can mangle the
original answer.

**Approach:** **Backend:** delete the format pass; move its rules (simple Markdown, inline
code for variable/function names, multi-line code blocks only for corrections, no
over-structuring with headers) into the main call's system prompt; stream the main call
directly with `generate_content_stream`. Land together with **0.6** so the moderation
gate and no-code persona go into this same consolidated call. **Frontend:** unchanged
(same SSE contract).

**Acceptance:** Converse responses stream from a single model call with clean structure;
no formatting regressions on bold/inline-code rendering; one fewer LLM call per message
(counts toward 3.4's target).

**Effort:** S. **Risk:** Low–Medium — one prompt now does reasoning *and* formatting;
spot-check a handful of typical questions.

### 4.3 Modernized loading states

**Why:** Waits are communicated by text-only pulsing lines (e.g. "Compiling Script…",
the profiling message at `templates/index.html:780`). Functional, but dated-looking and
easy to miss.

**Approach:** Frontend — build one small reusable spinner (Tailwind `animate-spin` SVG)
and a skeleton-block style; attach them to every network wait: upload/profiling status
(add the spinner *beside* the existing "Please wait, profiling dataset variables. This
might take a sec.." text, which already ships), codebook classification, suggestions
fetch, `/chat` generation, `/interpret`. Remove the remaining plain-text-only loading
injections from the DOM-manipulation functions.

**Acceptance:** Every network wait shows an animated indicator (spinner or skeleton);
no text-only loading state remains.

**Effort:** S. **Risk:** Low.

### 4.4 Persistent pipeline status checklist

**Why:** The pipeline already emits per-step messages — "Data loaded successfully."
(`index.html:764`), "Codebook RAG linked successfully." (`:878`), the profiling notice
(`:780`) — but each **replaces** the previous one in a single status line, so users lose
track of which steps ran and which are still pending.

**Approach:** Frontend — replace the single `#uploadStatus` line with a persistent step
list rendered when an upload starts:

1. Data loaded
2. Codebook linked *(optional step — shows "skipped" if declined)*
3. Variables classified
4. Suggestions ready

Each step has states: pending (slate), active (amber + spinner from 4.3), done
(emerald ✓), failed (red ✕). Steps transition amber→green as `runPostUploadPipeline`
progresses and are **never removed** during the session, so the whole pipeline stays
legible.

**Acceptance:** After a full upload flow, all steps show green; skipping the PDF marks
that step "skipped" without hiding it; a classification failure shows a red step while
earlier green steps remain.

**Effort:** S. **Risk:** Low.

### 4.5 Suggestion reroll button

**Why:** Suggestions generate once per upload; if the user dislikes them there's no way
to get a fresh set. *(The other half of this request — suggestions must use exact column
names — already ships: `app.py:454` forces "EXACT labels".)*

**Approach:** **Frontend:** add a refresh/reroll icon button next to the "Suggested
Analysis" header that re-triggers the `/suggest` fetch and re-populates the list (with
the 4.3 spinner while loading). **Backend:** accept an optional `previous_suggestions`
array in the `/suggest` payload and instruct the model to propose *different* analyses
than those listed.

**Acceptance:** Clicking reroll yields 3 new, non-repeating, still-column-grounded
suggestions.

**Effort:** S. **Risk:** Low.

### 4.6 Reset / New Analysis button

**Why:** There is no way to start over short of a hard refresh — and even that leaves
server-side files behind. Users want a clean-slate "new chat" affordance.

**Approach:** **Frontend:** a "New Analysis" button (header or sidebar) with a confirm
step; clears all client state (`chatHistory`, `converseHistory`, `currentCode`,
`currentCodebook`, `pdfMapping`, `currentFilename`, `latestInterpretation`, data table,
pipeline checklist) and returns every tab to its initial state. **Backend:** optional
`POST /reset` that deletes the current session's uploaded files (exact once 1.1's
per-session dirs exist).

**Acceptance:** After reset, the UI matches a fresh page load; the previous dataset is no
longer referenced anywhere; a new upload then works end-to-end.

**Effort:** S. **Risk:** Low.

### 4.7 Custom branding / logo

**Why:** The header uses a generic inline gradient-square SVG; a custom logo exists and
should replace it.

**Approach:** Create `static/images/`, add the logo asset (needs the file from the
owner); replace the inline SVG in the premium header with
`<img src="{{ url_for('static', filename='images/logo.png') }}" alt="CodeCaster">` sized
to match the surrounding typography (~`w-10 h-10`). Check both themes (transparent
background or per-theme variants).

**Acceptance:** Logo renders crisply in light and dark mode at header size with no layout
shift.

**Effort:** S. **Risk:** Low.

---

## Tier 5 — Analytical & data features

> Pull from these once Tiers 0–1 land. Each is independent unless a dependency is noted.

### 5.1 More input formats (high value for social scientists)

**Why:** Social-science data is often Excel, Stata (`.dta`), or SPSS (`.sav`), not just CSV.

**Approach:** Extend `/upload` to accept `.xlsx/.xls` (`pandas.read_excel`, needs
`openpyxl`), `.tsv`, and — via `pyreadstat` — `.dta`/`.sav` (which also carry native
variable/value labels that can seed the codebook, reducing LLM calls). Normalize everything
to a parquet/CSV internally. Validate file extensions securely and fail gracefully with a
clear message when an optional parser dependency (`openpyxl`, `pyreadstat`) is missing.
Update the dropzone `accept` attribute and UI copy to list the supported formats.

**Acceptance:** Uploading a labeled `.sav` populates the codebook from embedded labels and
analysis runs end-to-end; an `.xlsx` upload on a host missing `openpyxl` returns a clear
error, not a 500. **Effort:** M. **Risk:** Medium (parser deps).

### 5.2 Multiple plots per run

**Why:** Code is forced to a single `plot.png`; multi-panel analyses lose figures.

**Approach:** Instruct the model to save `plot_1.png`, `plot_2.png`, … (or any `*.png` in the
run dir). `sandbox.py` collects all PNGs; `/run` returns a `plots[]` array; the frontend
renders a gallery. Pairs naturally with 0.2's per-run dir.

**Acceptance:** A script producing 3 figures shows all 3. **Effort:** M. **Risk:** Low.

### 5.3 Project export: zip archive + report

**Why:** Researchers need portable artifacts of the whole workflow: the data, the script,
the results, and a shareable writeup — in one download.

**Approach:**
- **Backend:** new `/export` endpoint using `zipfile` (or `shutil.make_archive`) that
  builds `Project_<dataset-name>.zip` containing: the active data file, the current
  `script.py`/`script.R`, all generated plots, and a cleanly formatted `report.md`
  compiled from the conversation history, terminal output, and `/interpret` text.
- Also offer a combined **HTML/PDF report** variant (prompt + code + output + plots +
  interpretation) rendered from a Jinja template; PDF via `weasyprint` or headless
  Chromium — reuse the `reportlab`/`fpdf2` decision from 2.2.
- **Frontend:** a "Download Project" button (premium header or footer) doing a blob fetch
  and client-side save; a small menu can offer "script only / plots only / full zip /
  PDF report".
- When 5.17 (AI report builder) exists, its generated report becomes the `report.md`.

**Acceptance:** One click yields a self-contained archive whose contents match the
on-screen state; the PDF/HTML report matches the on-screen results.
**Effort:** M–L. **Risk:** Low–Medium (PDF rendering deps).

### 5.4 Editable code before running (interactive code editor)

**Why:** The generated script is a read-only `<pre><code id="codeOutput">`. Power users
want to tweak the code before executing — a collaborative coding environment instead of
a take-it-or-leave-it snippet.

**Approach:** Swap the `<pre>` for **CodeMirror 6** (Python/R modes; Monaco is the
heavier alternative if richer editing is ever needed). Syntax highlighting comes free.
On Run, send the editor contents as the `/run` payload. **Must** land together with 0.4
(run-guard) so editing doesn't reopen arbitrary execution — e.g. server stores the
generated baseline, accepts edits, but still runs them through the sandbox +
(optionally) re-moderation.

**Acceptance:** User edits a line, runs, sees the effect; sandbox guarantees from Tier 0 hold.
**Effort:** M. **Risk:** Medium (security coupling).

### 5.5 Stream the draft generation

**Why:** Only the validation pass streams; the Pro **draft** call is synchronous, so the user
stares at "Generating…" during the slowest step.

**Approach:** Use `generate_content_stream` for the draft and emit `delta` events; the
existing `streamSSE` client helper already handles incremental code. Keep the validation pass
as a second streamed phase (label phases in the UI).

**Acceptance:** Code visibly streams during drafting; final validated code still replaces it.
**Effort:** M. **Risk:** Low.

### 5.6 Cache classification & suggestions per dataset

**Why:** Re-uploading or revisiting a dataset re-runs `/classify_variables` and `/suggest`
(paid, slow) even when the data is identical.

**Approach:** Hash the uploaded file (sha256) → cache codebook + suggestions (in-memory LRU
now; Redis when scaled). On upload, short-circuit on cache hit. Invalidate by hash.

**Acceptance:** Second upload of the same file returns codebook/suggestions without new LLM
calls. **Effort:** S–M. **Risk:** Low.

### 5.7 Analysis history / save & resume

**Why:** Everything is ephemeral (2-hour temp cleanup). Researchers want to revisit prior runs.

**Approach:** Persist a per-session history (prompt, code, output, plots, interpretation) —
start with `localStorage` on the client (no backend storage needed, fits "data not stored").
Add a history panel/tab. *(Long-term this is absorbed by Tier 7's database-backed project
history; build the client-only version in a way that can later hydrate from the server.)*

**Acceptance:** Past analyses are listed and reloadable within a session. **Effort:** M.
**Risk:** Low (client-only first).

### 5.8 Large-data handling & upload guards

**Why:** `/data_page` does `pd.read_csv(filepath)` (whole file) on every page/filter request;
big files are slow and memory-heavy. *(Type/size guards — CSV-only accept, 16 MB cap,
50-page PDF cap — already exist; what's missing is graceful handling of big-but-valid
files.)*

**Approach:** Cache the parsed DataFrame per session/hash; or for very large files use
chunked/lazy reads (pyarrow/`polars`) and push filtering down. Add a row-count cap with a
clear UI message for oversized data, so nothing crashes the dyno.

**Acceptance:** Paging/filtering a 200 MB CSV stays responsive and bounded in memory; an
over-cap file is rejected with a friendly explanation, not a crash.
**Effort:** M. **Risk:** Medium.

### 5.9 Accessibility & i18n pass

**Why:** Icon-only buttons, custom bullets, and gradient text can fail contrast/screen-reader
checks; copy is English-only.

**Approach:** Add `aria-label`s to icon buttons, ensure focus-visible states, verify color
contrast in both themes, make the resizer keyboard-operable. Extract UI strings for future
localization.

**Acceptance:** Passes an automated a11y audit (axe) with no critical issues; keyboard-only
navigation works. **Effort:** M. **Risk:** Low.

### 5.10 Metadata-driven prompt context

**Why:** Prompts currently describe columns with dtype + 3 sample values
(`app.py:371`, `:431`) — a good baseline that already avoids raw-row dumps — but they
lack the distributional and missingness information that drives correct method choice
(skew, missing-value rates, cardinality).

**Approach:** Build a `summarize_dataframe(df)` helper returning, per column: dtype,
codebook classification, `nunique()`, missing count/% (`df.isna().sum()`), and for
numeric columns min/median/max from `df.describe()`. Reuse it in `/classify_variables`,
`/suggest`, and the `/chat` context block. Keep the token budget bounded (compact JSON,
truncate very wide frames — coordinate with the existing ≥15-column feature-selection
stage).

**Acceptance:** Prompts contain the structural metadata; suggestion/codebook quality is
unchanged or better on a quick comparison; still no raw row data in any prompt.
**Effort:** S–M. **Risk:** Low.

### 5.11 Context-aware error resolution (auto-debugging)

**Why:** When a script fails, the Results tab just shows the raw traceback. For the
target audience (social scientists, not programmers) that's a dead end — the
interpretation step should become a debugging assistant when execution fails.

**Approach:**
- **Backend:** `/interpret` accepts the generated `code` and the run's exit status as
  additional payload (prefer the real `returncode` from `/run` over scanning output for
  "Traceback"/"Error", which can false-positive). On failure, switch the prompt: act as
  a debugging assistant — cross-reference the failed output with the provided code,
  explain in plain English what went wrong, and provide a corrected code snippet.
- **Frontend:** the `runBtn` handler's `/interpret` fetch includes `currentCode` and the
  exit status; `interpretationOutput` renders returned Markdown code blocks with
  syntax highlighting — through the `renderMarkdown()` sanitizer from 0.5.

**Acceptance:** A run failing with a `NameError` produces a plain-English diagnosis plus
a corrected snippet; successful runs keep the normal statistical interpretation.
**Effort:** M. **Risk:** Low–Medium.

### 5.12 Iterative code refinement

**Why:** Conversation history already flows to `/chat`, so follow-ups *can* influence the
next generation — but nothing instructs the model to *modify the existing script* (or
includes the user's manual edits from 5.4), so it tends to regenerate from scratch.
Users who already like the script want surgical changes: "turn this into 3 graphs
instead of 1", "add a linear regression to this".

**Approach:** **Backend:** `/chat` accepts an optional `current_code` field; when
present, the draft prompt switches to: "Modify the following existing script per the
user's request; preserve everything unrelated." **Frontend:** after the first
generation, automatically include the current editor contents (from 5.4) with follow-up
prompts — optionally with an explicit "Refine current script" toggle so users can still
force a from-scratch rewrite.

**Acceptance:** A follow-up "add a regression line" returns the same script with the
addition, retaining prior structure and any manual edits.
**Effort:** M. **Risk:** Low.

### 5.13 Survey→codebook generation

**Why:** Many users lack a formal codebook but *do* have the survey instrument the data
came from. The LLM can derive a codebook (variable names, labels, scales) from the
questionnaire itself. *(The first half of this flow — "want to attach a codebook?
yes/no" — already ships as the `#codebookPromptOverlay` modal.)*

**Approach:**
- **Frontend:** extend the existing modal into a two-step decision tree: "Attach a
  codebook?" → **Yes** → PDF/TXT upload (existing path) / **No** → "Do you have the
  survey questionnaire instead? Upload it to generate a codebook."
- **Backend:** a survey mode for the extraction endpoint (parameter on
  `/extract_pdf_codebook` or a sibling `/generate_codebook_from_survey`) whose prompt
  infers variable→description/scale mappings from survey questions, then reuses the
  existing case-insensitive matching against CSV headers (same contract as `pdfMapping`).
- Show match coverage ("matched 14 of 22 questions to columns") so users can judge quality.

**Acceptance:** Uploading a questionnaire yields descriptions attached to matching columns
in the codebook UI; unmatched questions are reported, not silently dropped.
**Effort:** M. **Risk:** Medium — question→column matching is fuzzy.

### 5.14 Analysis catalog / method picker

**Why:** Users don't know what tests and plots exist, or which fit their data. A
browsable catalog bridges that knowledge gap and turns a method choice into a
ready-to-edit prompt.

**Approach:** **Frontend:** an "Analysis Catalog" panel — a curated static list grouped
by family (comparison tests, regression, correlation, categorical tests,
visualizations) with one-line descriptions. **Backend:** clicking an entry calls a
`/method_prompt` endpoint with the chosen method + the dataset metadata summary (5.10);
the model returns a tailored, column-grounded prompt — *"OLS regression? That will best
be done as `income ~ education + age` because…"* — which is inserted into
`#promptInput` for the user to edit and send.

**Acceptance:** Picking "OLS regression" yields a ready prompt naming real columns whose
measurement levels suit the method.
**Effort:** M. **Risk:** Low.

### 5.15 Guided analysis & hypothesis coach

**Why:** Novice researchers struggle to turn a hunch into a well-formed analysis request
— the "gap in knowledge creating good prompts". They need a conversational on-ramp:
examine the data, reason about "if X then Y" hypotheses, and end up with a rigorous
prompt.

**Approach:** A guided mode (a "Guide me" toggle inside Converse, or a dedicated panel):
the model asks about the user's hypothesis, inspects the dataset metadata summary
(5.10) and codebook, recommends candidate variables and methods with if-this-then-that
scaffolding, and finishes by drafting a concrete analysis prompt the user can send to
Generate. Respects the 0.6 guardrails — it guides and drafts *prompts*, never code.

**Acceptance:** Starting from "I think income affects voting", the coach elicits
specifics and outputs a concrete, column-grounded analysis prompt.
**Effort:** M. **Risk:** Low–Medium (prompt design; keep it focused).

### 5.16 Conversational data wrangling & version control

**Why:** The environment is read-only today. Researchers need cleaning and reshaping —
"delete column X", "filter for values…", "recode labels…" — without round-tripping
through Excel, plus undo/redo and an audit trail of what was changed.

**Approach:**
- **Backend:** a `/wrangle` endpoint specialized in executing pandas (or dplyr)
  transformation commands: the LLM translates the instruction into a transform, which
  runs **inside the sandbox** (0.2) against the active dataset version — wrangle code is
  untrusted code like any other. Save each result as `data_v{n+1}.csv` in the session
  dir (1.1) and keep a per-session version manifest (`{version, instruction,
  timestamp}`). Return the updated dataset profile on success.
- **Versioning semantics:** Undo/Redo move an active-version pointer along the manifest;
  a new edit after Undo truncates the redo branch. All downstream endpoints (`/chat`,
  `/suggest`, `/data_page`, `/classify_variables`) target the **active version**.
- **Frontend:** a "Data Cleaning" panel or modal for wrangle commands; a chronological
  **Changelog sidebar** listing applied transformations; Undo/Redo buttons mapped to the
  versioned files; the Data Viewer and codebook refresh on version change.

**Acceptance:** "Delete column X" produces v2 with X gone and a changelog entry; Undo
returns to v1 and subsequent analyses use v1; Redo restores v2; two sessions' version
chains never interact.
**Effort:** L. **Risk:** Medium–High — depends on 0.2 (sandbox) and 1.1 (isolation);
wrangle code must pass the same moderation as `/run`.

### 5.17 AI-assisted report builder

**Why:** Bridge the gap between raw statistical output and a finalized academic or
professional report — with the user's background context, at a requested length and
tone, and grounded in the actual numbers.

**Approach:**
- **Backend:** a `/generate_report` endpoint whose prompt strictly synthesizes: the
  terminal outputs and interpretations from this session, user-provided background
  context, requested length, and requested tone (academic / executive summary). Hard
  constraint: cite the specific data points produced by the code; never invent
  findings.
- **Frontend:** a "Report Builder" interface with input fields for Context/Background,
  Desired Length, and Writing Style; the generated report populates an **editable
  Markdown area** where the user can tweak manually or request targeted AI revisions
  ("expand on this paragraph" re-prompts with the selected text only).
- Feeds 5.3: the final report becomes the `report.md` / PDF in the project export.

**Acceptance:** The generated report references only statistics that appear in the run
outputs (spot-check the numbers); a targeted revision changes only the selected section.
**Effort:** M–L. **Risk:** Medium — hallucination control; keep the cite-the-data
constraint hard.

---

## Tier 6 — Workspace & support

### 6.1 Split-pane workspace

**Why:** Tabs are mutually exclusive — `switchTab` shows exactly one of Source Code /
Data / Results / Converse / PDF at a time — so users flip back and forth constantly
(e.g. discussing results in Converse while wanting the code visible). *(The existing
draggable divider only resizes the left sidebar; it is not this feature.)*

**Approach:** Frontend — refactor the right panel into a split layout (CSS Grid or
Split.js): default single pane; a "split" control adds a second pane with its own
content selector so any two views can sit side-by-side (e.g. Converse + Source Code).
Tab-content rendering must stop assuming a single container (give each pane its own
mount, or move DOM nodes between panes). Best done **after 3.2** (frontend
modularization) so the tab logic is in reviewable modules first.

**Acceptance:** User views Converse beside Source Code with both fully functional (chat
streams while code stays visible); single-pane mode behaves exactly as today.
**Effort:** L. **Risk:** Medium — significant DOM/JS refactor of the tab system.

### 6.2 Pop-out codebook

**Why:** The Intelligent Codebook lives in the left sidebar; consulting it while reading
code or results means scrolling away from what you're doing.

**Approach:** Frontend — a "pop out" button on `#codebookContainer` that re-renders the
codebook into a floating, draggable, pinnable panel (`position: fixed`, drag handle,
close/re-dock button). Alternatively — and more cheaply once 6.1 exists — expose the
codebook as selectable pane content in the split workspace.

**Acceptance:** The codebook stays visible while switching tabs; re-dock restores the
sidebar placement.
**Effort:** S–M. **Risk:** Low.

### 6.3 In-app issue reporting

**Why:** No feedback loop exists. Useful bug reports need context (active code, terminal
output, console errors) that users won't gather by hand.

**Approach:**
- **Frontend:** a "Support / Report Issue" button (sidebar or footer) opening a modal
  with a description field; on submit, auto-attach the active code snippet, the last
  terminal output, recent JS console errors (a small `window.onerror` ring buffer), and
  browser/UA info.
- **Backend:** a `/report_issue` endpoint that logs the structured report server-side
  and/or emails the administrator (SMTP settings via 1.3 config). **Never** include the
  dataset itself by default — privacy.

**Acceptance:** Submitting a report stores/emails a payload containing the description +
diagnostics and shows the user a confirmation with a reference id.
**Effort:** M. **Risk:** Low — mind PII in attachments.

---

## Tier 7 — Multi-user SaaS platform

> The biggest architectural shift: from a single-session sandbox to a multi-user product
> with accounts, persistence, and cloud storage. Do this **last** — it builds on 1.1
> (isolation), 1.3 (config), 2.1 (tests), and 3.1 (modular backend), and it forces a
> product decision about the "Data not stored" promise (see Open questions).

### 7.1 Real user authentication

**Why:** The single master `APP_PASSWORD` is a mockup-grade gate. Real users need their
own accounts.

**Approach:** Pick in an ADR: self-hosted **Flask-Login + SQLAlchemy** user model
(email + password hashing, password reset), or a hosted provider (**Firebase / Supabase
/ Auth0**) if you'd rather not own credential storage. Build registration, login, and a
dashboard view listing past projects. Scope every route to `current_user`; remove the
`APP_PASSWORD` overlay logic.

**Acceptance:** Two users each see only their own projects; unauthenticated API access
returns 401/redirects to login.
**Effort:** L. **Risk:** Medium.

### 7.2 Database-backed persistence (PostgreSQL)

**Why:** Everything currently dies with the server (Render free-tier sleep wipes temp
storage). Users expect their past chats, scripts, and datasets to be there when they log
back in.

**Approach:** PostgreSQL via **SQLAlchemy** (SQLite locally for dev) with Alembic
migrations. Models: `User`, `Project`, `Dataset` (metadata + storage key), `ChatMessage`
(both chat and converse histories), `AnalysisRun` (prompt, code, output, plots,
interpretation), `ReportDraft`. Wire the 5.7 history UI to hydrate from the DB for
logged-in users.

**Acceptance:** Log out, restart the server, log back in: prior chats, scripts, and
datasets reload.
**Effort:** L. **Risk:** Medium.

### 7.3 Cloud object storage

**Why:** The server's temp dir is ephemeral and single-host; uploaded data must live
somewhere durable and scalable.

**Approach:** An S3 / Google Cloud Storage bucket; uploads stream to keys like
`user_<id>/<project>/<filename>`; reads via presigned URLs or a thin proxy; the sandbox
(0.2/0.3) fetches the dataset into the per-run dir at execution time. Bucket lifecycle
rules replace `cleanup_old_files()` for anonymous/expired data.

**Acceptance:** Files survive restarts and redeploys; storage keys are never derivable
across users.
**Effort:** M–L. **Risk:** Medium.

### 7.4 User-isolated workspaces

**Why:** The hard guarantee: Alice's and Bob's files, histories, and runs never touch.

**Approach:** Every artifact keyed by user id — storage prefixes (7.3) plus DB foreign
keys (7.2) — with authorization checks on every data route. Builds directly on 1.1's
containment pattern (session-scoped → user-scoped). Extend the 2.1 test suite with
cross-user access attempts.

**Acceptance:** All cross-user access attempts return 403/404 in tests; no shared-path
fallbacks remain.
**Effort:** M (mostly falls out of 7.1–7.3 done right). **Risk:** Medium.

---

## Already covered — no action needed

Requested features that **already exist** in the codebase, recorded here so they aren't
re-proposed. (Where a request was half-covered, the new half's item is linked.)

| Request | Status in code |
|---|---|
| "Create codebooks from CSV files" | Ships as the Intelligent Codebook: `/classify_variables` classifies every variable (Nominal/Ordinal/Continuous). Survey-based generation is new → **5.13** |
| Codebook upload as an if/else flow ("wanna upload a codebook? yes/no") | Ships as the `#codebookPromptOverlay` modal ("Attach a Codebook?" Yes/Skip). The survey branch is new → **5.13** |
| "Please wait, profiling dataset variables. This might take a sec.." loading message | Exists verbatim (`templates/index.html:780`); only the spinner icon is missing → **4.3** |
| Suggested analyses must use exact column names | Enforced: `app.py:454` ("EXACT labels") and `:586` (feature selection, exact match) |
| Data limits on CSV and PDF/TXT to prevent crashes | `accept=".csv"` / `accept=".pdf,.txt"`, 16 MB upload cap, 50-page PDF cap all ship. Big-but-valid file handling → **5.8** |
| Pipeline messages ("Data loaded successfully", "Codebook RAG linked successfully") | Messages exist (`index.html:764`, `:878`) but replace each other; the persistent checklist is new → **4.4** |
| Moderation/safety gate on code generation | `/chat` has it (`app.py:522`); `/converse` does not → **0.6** |
| Metadata (not raw rows) in prompts | No `df.to_string()` anywhere; prompts already use dtype + 3 sample values (`app.py:371`, `:431`). Richer stats → **5.10** |
| Follow-up prompts refining an analysis | Conversation history is already sent to `/chat`; the explicit modify-don't-regenerate mode is new → **5.12** |
| Side-by-side resizing | A draggable sidebar resizer exists — but tab-level split panes do not → **6.1** |

---

## Suggested sequencing

1. **Tier 0** (0.1 → 0.6) — close the execution-safety gaps. 0.1/0.5/0.6 are quick;
   0.3 is the big one. Land 0.6 together with 4.2.
2. **Tier 4 quick wins** — interleave anywhere from day one; each is < ½ day and
   user-visible (4.1 input-bar fix and 4.4 pipeline checklist first).
3. **Tier 1** (1.1 isolation, 1.2 server, 1.3 config, then 1.4–1.6) — make it safe to
   run publicly.
4. **Tier 2** (tests first, then deps, then CI) — lock in the above with regression
   coverage.
5. **Tier 3** — modularize behind tests; then run the 3.4 cost pass (it's much easier
   once prompts/models live in one service module).
6. **Tier 5** — pull by value: 5.1 (formats), 5.3 (export), 5.5 (draft streaming),
   5.6 (caching) are the highest user-visible wins; 5.4 must pair with 0.4;
   5.16/5.17 only after the sandbox (0.2/0.3) and isolation (1.1) exist.
7. **Tier 6** — 6.3 anytime; 6.1/6.2 ideally after 3.2.
8. **Tier 7** — last, and only after the "data not stored" product decision below.

## Open questions for you

- **Sandbox approach (0.3):** are you willing to run sibling containers / mount the Docker
  socket, or prefer a managed sandbox service? This drives the infra design.
- **Deploy target:** staying on Render? That constrains 0.3 (no Docker socket on standard
  Render web services — may need a separate worker service or a provider that allows it)
  and makes Tier 7's Postgres/storage choices concrete (Render Postgres? S3? Supabase?).
- **"Data not stored" promise:** 5.7 history and 5.6 caching touch this — client-only by
  default keeps the promise. **Tier 7 breaks it outright** for logged-in users; decide
  whether to keep an anonymous "sandbox mode" alongside accounts, and update the footer
  copy accordingly.
- **Claude API for code drafting (3.4):** is there an Anthropic API key/budget for the
  A/B evaluation, and which stage(s) are in scope — drafting only, or validation too?
- **Auth provider (7.1):** self-hosted (Flask-Login) vs. hosted (Firebase / Supabase /
  Auth0)? Owning credentials means owning resets, lockouts, and breach risk.
- **Logo asset (4.7):** need the final logo file (ideally SVG or transparent PNG, with a
  dark-mode variant if the design needs one).
