# CodeCaster — Implementation Plan & Roadmap

> **Status:** Draft for review. This is a prioritized backlog, not a committed schedule.
> Items are grouped into tiers; within a tier they're roughly ordered by value-to-effort.
> Each item lists **why**, a **concrete approach** (with file paths), **acceptance criteria**,
> an **effort** estimate (S ≈ <½ day, M ≈ 1–2 days, L ≈ 3+ days), and **risk**.

**Goal:** Take CodeCaster from a polished prototype to a safe, production-ready,
maintainable platform — then grow its analytical feature set.

**Tech stack (current):** Python 3.12 / Flask, pandas + statsmodels, Google GenAI SDK
(multi-model), subprocess execution, Docker, vanilla JS + Tailwind (CDN), SSE streaming.

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
stronger and also enables the "editable code" feature (4.4) to round-trip through the
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

---

## Tier 1 — Multi-tenant correctness & production readiness

### 1.1 Per-session file isolation

**Why:** `UPLOAD_FOLDER` is one shared dir and files are keyed by `secure_filename`. Two
users uploading `survey.csv` collide, and any client can read any dataset by guessing its
name via `/data_page` / `/extract_pdf_codebook`. On a public deploy this is a real
data-leak between users.

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

## Tier 3 — Architecture & maintainability

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

---

## Tier 4 — Feature backlog (concrete)

> Pull from these once Tier 0–1 land. Each is independent.

### 4.1 More input formats (high value for social scientists)

**Why:** Social-science data is often Excel, Stata (`.dta`), or SPSS (`.sav`), not just CSV.

**Approach:** Extend `/upload` to accept `.xlsx/.xls` (`pandas.read_excel`, needs
`openpyxl`), `.tsv`, and — via `pyreadstat` — `.dta`/`.sav` (which also carry native
variable/value labels that can seed the codebook, reducing LLM calls). Normalize everything
to a parquet/CSV internally. Update the dropzone `accept` and copy.

**Acceptance:** Uploading a labeled `.sav` populates the codebook from embedded labels and
analysis runs end-to-end. **Effort:** M. **Risk:** Medium (parser deps).

### 4.2 Multiple plots per run

**Why:** Code is forced to a single `plot.png`; multi-panel analyses lose figures.

**Approach:** Instruct the model to save `plot_1.png`, `plot_2.png`, … (or any `*.png` in the
run dir). `sandbox.py` collects all PNGs; `/run` returns a `plots[]` array; the frontend
renders a gallery. Pairs naturally with 0.2's per-run dir.

**Acceptance:** A script producing 3 figures shows all 3. **Effort:** M. **Risk:** Low.

### 4.3 Export / downloadable report

**Why:** Researchers need artifacts: the script, the results, and a shareable writeup.

**Approach:** "Download" menu → (a) raw script, (b) plots, (c) a combined **HTML/PDF report**
(prompt + code + terminal output + plots + interpretation). Server route renders a Jinja
template; PDF via `weasyprint` or headless Chromium. Reuse the `reportlab`/`fpdf2` choice
from 2.2.

**Acceptance:** One click yields a self-contained report matching the on-screen results.
**Effort:** M–L. **Risk:** Low–Medium (PDF rendering deps).

### 4.4 Editable code before running

**Why:** The generated script is a read-only `<pre>`. Power users want to tweak before
executing.

**Approach:** Swap the `<pre>` for CodeMirror 6 (Python/R modes). On Run, send the editor
contents. **Must** land together with 0.4 (run-guard) so editing doesn't reopen arbitrary
execution — e.g. server stores the generated baseline, accepts edits, but still runs them
through the sandbox + (optionally) re-moderation.

**Acceptance:** User edits a line, runs, sees the effect; sandbox guarantees from Tier 0 hold.
**Effort:** M. **Risk:** Medium (security coupling).

### 4.5 Stream the draft generation

**Why:** Only the validation pass streams; the Pro **draft** call is synchronous, so the user
stares at "Generating…" during the slowest step.

**Approach:** Use `generate_content_stream` for the draft and emit `delta` events; the
existing `streamSSE` client helper already handles incremental code. Keep the validation pass
as a second streamed phase (label phases in the UI).

**Acceptance:** Code visibly streams during drafting; final validated code still replaces it.
**Effort:** M. **Risk:** Low.

### 4.6 Cache classification & suggestions per dataset

**Why:** Re-uploading or revisiting a dataset re-runs `/classify_variables` and `/suggest`
(paid, slow) even when the data is identical.

**Approach:** Hash the uploaded file (sha256) → cache codebook + suggestions (in-memory LRU
now; Redis when scaled). On upload, short-circuit on cache hit. Invalidate by hash.

**Acceptance:** Second upload of the same file returns codebook/suggestions without new LLM
calls. **Effort:** S–M. **Risk:** Low.

### 4.7 Analysis history / save & resume

**Why:** Everything is ephemeral (2-hour temp cleanup). Researchers want to revisit prior runs.

**Approach:** Persist a per-session history (prompt, code, output, plots, interpretation) —
start with `localStorage` on the client (no backend storage needed, fits "data not stored"),
later optionally a lightweight DB (SQLite) for logged-in users. Add a history panel/tab.

**Acceptance:** Past analyses are listed and reloadable within a session. **Effort:** M.
**Risk:** Low (client-only first).

### 4.8 Large-CSV handling

**Why:** `/data_page` does `pd.read_csv(filepath)` (whole file) on every page/filter request;
big files are slow and memory-heavy.

**Approach:** Cache the parsed DataFrame per session/hash; or for very large files use
chunked/lazy reads (pyarrow/`polars`) and push filtering down. Add a row-count cap with a
clear UI message for oversized data.

**Acceptance:** Paging/filtering a 200 MB CSV stays responsive and bounded in memory.
**Effort:** M. **Risk:** Medium.

### 4.9 Accessibility & i18n pass

**Why:** Icon-only buttons, custom bullets, and gradient text can fail contrast/screen-reader
checks; copy is English-only.

**Approach:** Add `aria-label`s to icon buttons, ensure focus-visible states, verify color
contrast in both themes, make the resizer keyboard-operable. Extract UI strings for future
localization.

**Acceptance:** Passes an automated a11y audit (axe) with no critical issues; keyboard-only
navigation works. **Effort:** M. **Risk:** Low.

### 4.10 In-UI model selection & cost display

**Why:** Model routing is hardcoded; users/admins may want to trade speed vs. depth, or see
cost.

**Approach:** Expose the `MODEL_*` constants via config (1.3) and an optional admin UI control;
show per-analysis token usage (from 3.3) in the Results tab.

**Acceptance:** Switching the configured model changes which model serves requests; usage is
visible. **Effort:** S–M. **Risk:** Low.

---

## Suggested sequencing

1. **Tier 0** (0.1 → 0.5) — close the execution-safety gaps. 0.1/0.5 are quick; 0.3 is the big one.
2. **Tier 1** (1.1 isolation, 1.2 server, 1.3 config, then 1.4–1.6) — make it safe to run publicly.
3. **Tier 2** (tests first, then deps, then CI) — lock in the above with regression coverage.
4. **Tier 3** modularization — now that tests exist, refactor safely.
5. **Tier 4** — pull features by value; 4.1 (formats), 4.3 (report), 4.5 (draft streaming),
   and 4.6 (caching) are the highest user-visible wins.

## Open questions for you

- **Sandbox approach (0.3):** are you willing to run sibling containers / mount the Docker
  socket, or prefer a managed sandbox service? This drives the infra design.
- **Deploy target:** staying on Render? That constrains 0.3 (no Docker socket on standard
  Render web services — may need a separate worker service or a provider that allows it).
- **"Data not stored" promise:** 4.7 history and 4.6 caching touch this — client-only by
  default keeps the promise; confirm before any server-side persistence.
