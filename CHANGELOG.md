# Changelog

All notable changes to STATlee are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.6.0] - 2026-10-08

A session workbook, an editable codebook, a methods and assumptions card, and a
reproducibility bundle that records what produced what.

### Added
- **Session workbook.** Every run is kept under `runs/<run_id>/` (output, plots,
  script, status, interpretation, provenance), not just the last one. The
  Results tab has a numbered run rail: star a run, rename it, open an older one
  read-only with its own write-up, load its script back into the editor, or
  interpret it. The report builder takes a checklist of runs and grounds on each
  one's stored output. `MAX_RUNS_PER_IDENTITY` (default 50) caps the store and
  evicts the oldest unstarred run first. New routes: `GET /runs`,
  `GET /runs/<run_id>`, `PATCH /runs/<run_id>`. `/interpret` and `/converse`
  accept `run_id`, `/generate_report` accepts `run_ids`, and account history
  rows link to their run.
- **Editable codebook.** The Codebook tab is now a per-variable editor (type,
  label, value labels, missing codes, role, scale group) with a filter and JSON
  import and export. It is stored on the server in layers merged as
  user > file > PDF > model, so a re-classification or a new PDF never
  overwrites your edits. `.sav` and `.dta` labels, value labels and user-missing
  codes seed it at upload, and a codebook comes back when the same file is
  uploaded again. Prompts use the server codebook: missing codes are treated as
  missing, ID and ignored columns are not modelled, and a survey weight column
  is named. New routes: `GET`/`PUT /codebook`, `POST /codebook/import`,
  `GET /codebook/export`.
- **Methods and assumptions card.** Scripts print a `METHOD`, `ASSUMPTIONS`,
  `EFFECT` block. It is parsed leniently, stored with the run, shown above the
  terminal, and cited by the interpretation and the report. A script that prints
  none gets a card that says assumptions were not reported.
- **Reproducibility bundle v2.** The export adds `provenance.json` (upload,
  cleaning, run and interpretation steps with hashes, models, roles and
  timestamps), `codebook.json`, `environment.txt` (Python and R versions and the
  analysis package versions, captured through the sandbox) and a per-run
  `outputs/run_NNNN/` folder. The export dialog chooses all, starred or the
  latest run, and whether your prompts are included.

- **Sample dataset on first run.** With no dataset loaded, the upload area
  links to the synthetic sample survey (`GET /sample_survey.csv`, served from
  `docs/examples/`), so a new install has data to try.

### Changed
- In the export, `script.py` is now `analysis.py` (or `analysis.R`, the script of
  the latest included run), `README.txt` is `README.md`, and plots live in each
  run's folder instead of a top-level `plots/`.
- `/export` shares the `/run` rate limit, since it can start the environment
  probe. The zip is built in a spooled temporary file instead of in memory.
- Cleaning steps record the model and role that produced them.
- The CSRF check also covers `PATCH`.
- An export only ever bundles runs and a script that belong to the exported
  dataset's current content. Editor code that was never generated, reviewed or
  run for that dataset is no longer bundled; with nothing on record the README
  says no analysis has been run on the dataset yet.
- A run keeps at most 20 plots and 10 MB of plots (the output says how many were
  not kept), and the workbook also evicts its oldest unstarred runs once it
  holds more than a quarter of `MAX_BYTES_PER_IDENTITY`, so stored runs always
  leave room for uploads.
- A codebook write that would grow the stored codebook past 4 MB is refused with
  a clear message (413) instead of stored.
- Text placed inside an untrusted-data block of a prompt can no longer close or
  forge a block with its own `<<<` or `>>>` markers.
- Default models moved to current stable snapshots. Gemini: `gemini-3.8-flash`
  for code, `gemini-4-argon` for Pro mode (a GA id, replacing
  `gemini-3.1-pro-preview`), `gemini-3.5-flash-lite` for the light roles
  (`gemini-3.5-flash` now routes to 3.6 and `gemini-3.1-flash-lite` shuts down
  May 2027). Anthropic: `claude-opus-5-5`, `claude-fable-5-1` (Pro mode),
  `claude-sonnet-5-5`, `claude-haiku-5-5`. OpenAI: `gpt-6.1-sol`, `gpt-6-astra`
  (Pro mode), `gpt-6-luna` (`gpt-5.4-nano` is deprecated). The cost display
  prices each one.
- Dependencies bumped to current stable: google-genai 2.31.0, anthropic 1.15.0,
  openai 3.32.0, pandas 3.0.6, statsmodels 0.15.0, matplotlib 3.11.2,
  filelock 4.2.0, gunicorn 26.2.0, fpdf2 2.8.9, pypdf 6.21.0, alembic 1.20.0,
  redis 8.1.0, pyreadstat 1.3.6, python-dotenv 1.2.4.
- Repository layout: the pins moved to `requirements/` (`base.txt`, `dev.txt`,
  `postgres.txt`, `runner.txt`), the Dockerfiles to `docker/`
  (`app.Dockerfile`, `runner.Dockerfile`; build from the repo root with
  `-f docker/...`), migrations into `statlee/migrations/`, and the security
  policy into `.github/SECURITY.md`. `docker compose up --build` is unchanged.
- Dependencies are locked: `requirements/app.lock`, `dev.lock` and
  `runner.lock` pin and hash every transitive package (compiled from the
  `.txt` pins with `uv pip compile --universal`), and the Docker images, CI and
  the README install from them. Install locally with
  `pip install -r requirements/dev.lock` (or `app.lock` for the app alone).
  CI's pip-audit now covers every locked package, transitive ones included.
- The README setup is two numbered recipes: Quickstart (Docker Compose) and
  Development (a local virtual environment), and CI runs both as written.

### Fixed
- The Anthropic provider works with SDK 1.x, which removed `temperature` from
  `messages.create`: the temperature is sent only to models before Claude 4.7
  (later ones reject it) and travels in the request body.
- Long terminal output scrolls inside the terminal box instead of being clipped
  in the fixed-height Results column.
- Exporting a dataset with no run of its own no longer ships another dataset's
  script, terminal output or interpretation, and `report.md` never uses an
  interpretation sent by the browser.
- Runs on earlier content of a re-uploaded same-name dataset are left out of its
  export (the README counts them) instead of shipping with hashes that match
  nothing in `provenance.json`.
- A run records the dataset version and hash it actually read, even when a
  cleaning step lands while it runs.
- A file locked in an old single-run folder (Windows) no longer breaks the
  workbook; the old run is adopted on a later visit.
- Signing in now carries codebook archives into the account, so re-uploading the
  same file there restores your codebook edits.
- The run rail ends the read-only view of a run that left the workbook.
- Accessibility and layout pass (axe-core 4.10.2, zero violations on every
  workspace screen, the landing page and the reset page, light and dark):
  primary buttons, the dark theme's muted text, the light theme's violet tag,
  the terminal's status words and the editor's line numbers reach 4.5:1
  contrast; the workspace has `main` and sidebar landmarks, headings no longer
  skip levels, the sidebar resizer reports its position, the terminal output
  and the script editor are reachable by keyboard. An empty bordered box no
  longer shows under "Loaded <file>" after every upload, the docked cleaning
  panel's buttons no longer run past its edge, and the pipeline header gets
  the spacing its markup asked for (the rebuilt Tailwind file now holds every
  class the app shell uses and none taken from comments).

### Security
- Outgoing email verifies the SMTP server's certificate before logging in;
  `starttls()` without a context had accepted any certificate.
- Fonts are served by the app itself instead of Google Fonts, so a page view
  no longer sends the visitor's IP address to a third party. The README says
  what does leave the server.
- An email address over 254 characters is refused before the format check,
  which backtracked for seconds to minutes on a long run of dots.
- `/run` refuses a script over 200,000 characters (`413`) before the paid
  moderation call.
- `/wrangle` runs the static check on the full script it executes, not only on
  the model's transform.
- Run output no longer names host paths: tracebacks read `File "script.py"`
  and install, temp and home dirs become labels such as `<site-packages>`.
- Responses send `Referrer-Policy: same-origin`, production HTTPS responses
  send HSTS, and the remember-me cookie is `SameSite=Lax`.

### Known limitations
- Run plots count toward the per-identity storage cap (bounded by the workbook's
  share of it, above).
- Signing in does not merge an anonymous workbook into an account that already
  has runs; the account's runs win.
- With prompts excluded from an export, text written by the model
  (interpretations, reports, script comments) may still restate the question.
- `environment.txt` is captured once per boot and sandbox mode; a failed capture
  is retried after 5 minutes.
- The methods block depends on the model following the format; otherwise the
  card reports assumptions as not reported.
- Runs saved before this release did not record a dataset name and are left out
  of dataset exports.

### Internal
- New settings: `MAX_RUNS_PER_IDENTITY` (in `.env.example`). One migration adds
  nullable `run_id`, `dataset_version` and `model` columns to `analysis_runs`.
- Test suite grew from 884 to 1240 passing, including Playwright end-to-end
  tests for the run rail, codebook editor, methods card and export dialog.

## [1.5.0] - 2026-10-08

A whole-project audit (backend, security, LLM layer, frontend, infrastructure,
product) and the fixes that came out of it. The static pre-check is rebuilt as
an allowlist, the export bundle now runs as unzipped, and many failure paths
that used to return 500s, hang, or lose work are handled.

### Security
- Rebuilt the Python side of `codecheck.py` as an import allowlist (the
  analysis stack plus a small set of stdlib modules) instead of a denylist. The
  denylist missed `builtins.exec`, attribute chains such as `os.path.os.system`
  and `pd.io.common.os.system`, low-level modules (`nt`, `posix`, `_socket`,
  `runpy`), `from os import *`, `__dict__`/`vars()`/`__self__`/frame-object
  lookups, `operator.attrgetter`, and pickle or absolute-path reads and writes
  through pandas and numpy. All of these are now refused, and a corpus of
  realistic analysis scripts is tested to still pass. The R denylist also covers
  `do.call` and `get()` indirection, `eval(parse())`, and network packages.
- Login does the same password-hash work for unknown emails, closing a timing
  oracle for account enumeration.
- Password-reset and email-verification links are built from `PUBLIC_BASE_URL`
  instead of the request's Host header, and their tokens are never logged
  outside development.
- Model output rendered as Markdown is sanitized more strictly (no `style`,
  `data-*`, forms or buttons), so it can't trigger in-app actions or overlay the
  page. Links open in a new tab.
- `/converse` and `/generate_report` ground on the server's stored run and
  interpretation rather than client copies; any client text that is used is
  length-capped and moderated.
- `/metrics` requires `METRICS_TOKEN` and returns 404 in production without one.
- Added a `.dockerignore` so `.env`, local databases and `.git` never enter the
  app image, and the container runs as a non-root user.
- Bumped `pypdf` to 6.19.0 and `Werkzeug` to 3.1.9 (nine advisories). CI now
  runs `pip-audit`.

### Fixed
- Non-Latin filenames (e.g. `数据.csv`) upload correctly, keep their PDF page
  cap, and no longer collide with each other.
- cp1252/Latin-1 CSVs decode, and empty or ragged CSVs return a clear 422
  instead of a 500 or a dataset that breaks later.
- The data viewer no longer breaks on infinite values; NaN and infinity are sent
  as null.
- Wrong-typed JSON fields return 400 instead of 500 on every route.
- Free-text inputs that reach a paid prompt are length-capped.
- Anonymous workspaces expire as a whole after inactivity rather than file by
  file, so an active session no longer loses its original upload.
- JSON sidecar writes are atomic per write, and script approvals survive
  concurrent saves.
- A script approved as Python can't be run as R without its own review, and an
  unchanged script runs in the language it was approved in.
- `/interpret` switches to debugging mode from the real exit code (R errors and
  timeouts included). A run timeout returns 200 with `timed_out`.
- `/wrangle` checks a version token and returns 409 when the dataset changed
  underneath it, including a re-upload during an edit.
- Provider safety blocks, refusals, and empty or truncated replies are detected
  on Gemini, Anthropic and OpenAI and explained to the user. `/chat` never
  approves an empty script, and the credit is refunded.
- Gemini calls retry 429/5xx errors with backoff; all providers have explicit
  timeouts.
- JSON replies are parsed tolerantly (code fences, leading prose) and checked
  for shape; `/suggest` always returns up to three strings.
- Gemini thinking tokens are counted in usage and cost, and streams cut short
  still record their usage.
- The monthly priority-call ceiling applies even with billing off, as
  documented.
- The Pro credit is refunded when the client disconnects mid-stream.
- A database whose tables are already at the current schema but lack an Alembic
  stamp is stamped instead of crash-looping at boot. A new
  `flask --app wsgi statlee-doctor` command reports schema drift.
- Docker Compose passes the documented environment through to the app.
- Frontend: closing the codebook prompt with Escape no longer leaves the
  workspace stuck; a failed upload keeps the current dataset; a new upload
  clears the previous dataset's script, results, chat and report; errors no
  longer overwrite the editor; double submits are guarded; streams that end
  without a final event show an error; signing in keeps the workspace.

### Changed
- Exports run as unzipped: the data sits next to the script under the name the
  script reads, with the original upload, a `README.txt`, and a `cleaning.py`
  that replays every recorded cleaning step from the original file.
- The dataset profile uses the full file (true N, missing counts, dtypes);
  sample values stay sampled. Code generation can reach columns past the 80th
  on wide datasets.
- SPSS/Stata value labels feed the codebook, and Excel uploads warn when only
  the first sheet is read.
- Prompts: the draft prompt lists the packages that are actually installed and
  asks for assumption checks, effect sizes with confidence intervals, and the N
  used; the review pass sees the request and columns, never adds a global
  `dropna()`, and runs on the Pro tier in Pro mode; the interpreter gets the
  question, the columns and the true N; dataset and PDF text is marked as data.
- Uploads default to a stable `instance/uploads` folder instead of a new temp
  folder per process.
- `python wsgi.py` binds to 127.0.0.1 unless `BIND_HOST` is set.
- `TRUST_PROXY_HOPS` defaults to 0. Deployments behind a proxy must set it.
- `STORAGE_BACKEND=s3` is rejected at startup until it is implemented.

### Added
- A "Forgot password?" flow in the account dialog and a visible "check your
  email" step when verification is on.
- A short disclosure of what is sent to the model provider, and a notice before
  an anonymous upload expires.
- Keyboard focus styles, focus management in dialogs, live regions for streamed
  answers, WCAG AA contrast in the light theme, and a stacked layout for narrow
  screens.
- New settings: `PUBLIC_BASE_URL`, `BIND_HOST`, `METRICS_TOKEN`,
  `LLM_TIMEOUT_SECONDS`, `LLM_PRO_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`,
  `OPENAI_STREAM_MAX_TOKENS` (all in `.env.example`).

### Internal
- Test suite grew from 411 to 884 passing (plus 9 skip-marked: opt-in
  screenshots and the real-Docker sandbox tests). New Playwright end-to-end UI
  tests run against a fake-LLM server and skip when Playwright isn't installed.
- Tests no longer read the developer's `.env`, and importing the package no
  longer builds an app.
- CI adds jobs for the real-Docker sandbox, the app image, migrations on
  PostgreSQL, and a models-versus-migrations check.

## [1.4.1] - 2026-08-26

Two security fixes from a re-audit: a vulnerable PDF-parser dependency and two
gaps in the non-LLM static pre-check. No breaking changes, no change to how
legitimate analysis scripts run.

### Security
- Closed two evasions of the `codecheck.py` static pre-check that reached
  `os`/`shutil` primitives past the attribute rules: `getattr(os, 'system')`
  with a literal attribute name, and plain assignment aliasing (`o = os; o.system(...)`,
  chains included). The gate now tracks `name = <module>` rebindings and resolves
  constant `getattr`/`setattr`/`delattr` owners, so every route to a host-module
  primitive lands on the same rule that already blocked `os.system`. Harmless
  forms (`getattr(os, 'getcwd')`, a DataFrame named after a module, aliased
  numpy) still pass.
- Bumped `pypdf` 6.13.3 to 6.16.2. The pinned version had six 2026 advisories
  (PYSEC-2026-3610/3611/3612/3613/3655/3656) in the parser that reads untrusted
  PDF uploads; the bump clears them.

### Internal
- Test suite grew from 400 to 411 passing (plus 4 skip-marked tests: the 2
  POSIX-only ones run on CI's Linux runners, but the 2 real-Docker ones skipped
  everywhere, CI included, because no job built the runner image); the README
  badge reflects the new count.

## [1.4.0] - 2026-07-20

Sandbox hardening (a new non-LLM static pre-check), a decompression-bomb upload
fix, and correctness/perf fixes from a full code audit. No breaking changes.

### Security
- Added a non-LLM static pre-check (`statlee/codecheck.py`) that runs before
  every sandbox execution on all three code paths (`/chat`, `/run`, `/wrangle`).
  It is a deterministic AST denylist (Python) / regex denylist (R) that rejects
  network and process imports, `os`/environment/file-exfiltration primitives,
  dynamic code execution (`eval`/`exec`/`compile`/`__import__`), the classic
  `__subclasses__`/`__globals__` sandbox-escape idioms, and file opens outside
  the run directory. It is defense in depth added to the LLM moderation gates,
  so in the default `subprocess` mode the safety boundary is no longer solely
  LLM-dependent. A blocked script is refused before execution and its credit
  (when billing is on) is refunded.
- The static pre-check resolves module aliases (`import os as o; o.system(...)`)
  and blocks direct from-imports of `os`/`sys`/`shutil` primitives
  (`from os import system`), closing two one-line evasions of the attribute
  rules.
- Docker Compose no longer bind-mounts a readable `.env` secret into the
  untrusted-execution container, and the subprocess-isolation warning now fires
  in development too, not only in production.

### Fixed
- `/export` and `/interpret` now use the script that produced the last run
  instead of the most-recently-approved script. A `/wrangle` performed after an
  analysis no longer makes the project export bundle the one-line wrangle
  transform as `script.py`, or feed it to the auto-debugger, in place of the
  analysis script.
- Bounded upload parsing against a decompression bomb. Excel/Stata/SPSS uploads
  are size/row/cell-capped before full materialization (zip central-directory
  metadata for `.xlsx`, reader metadata for `.sav`/`.dta`, with a post-read cell
  backstop) and rejected with a 413, so a small compressed file can no longer
  expand to gigabytes and pin a worker.

### Changed
- `/data_page` caches the filtered frame per filter set and gets an explicit
  tighter rate limit, removing the repeated full-frame rescan on the paging hot
  path.
- The container image now defaults `WEB_CONCURRENCY` to 1 so the in-memory
  rate-limit store and the monthly priority ceiling hold at their configured
  numbers by default; running more than one worker is documented to require a
  shared `RATELIMIT_STORAGE_URI`.

### Docs
- README and `docs/ARCHITECTURE.md` now qualify the "network-less" and
  "secret-free" isolation claims by mode (kernel-enforced in `docker` mode; the
  static pre-check plus moderation gates in the default `subprocess` mode) and
  document the static pre-check as the non-LLM boundary.
- Documented the model-role mapping (`MODEL_PRO` is the default/draft tier;
  `MODEL_PRO_MAX` is the Pro-mode model) in `config.py`, `.env.example`, and the
  README to prevent misconfiguration.

### Internal
- Test suite grew from 334 to 400 passing (plus 4 skip-marked tests: 2 POSIX-only
  ones that run in CI and 2 real-Docker ones that always skipped, CI included);
  the README badge reflects the new count.

## [1.3.2] - 2026-07-16

Security hardening, a testing/boot-hygiene fix, and a dependency refresh. No
breaking changes.

### Security
- `/chat` now re-moderates the model's own generated script through the
  run-guard before approving it, not just the user's instruction. Previously
  the primary code-generation path saved generated code as approved after only
  a quality-validation pass, so in the default subprocess sandbox mode a
  benign-looking prompt that steered the model toward network, environment, or
  file-exfiltration code could reach execution with no code-level safety check.
  `/wrangle` and the edited-`/run` guard already did this; `/chat` now upholds
  the same "every executed generated script is moderated" invariant. A blocked
  script is not approved or run, and the request's credit (when billing is on)
  is refunded.

### Fixed
- Running the test suite no longer poisons the instance database. Testing with
  no `DATABASE_URL` now resolves to in-memory SQLite, so `python -m pytest`
  followed by an `APP_ENV=development` boot no longer crashes with a
  duplicate-column error from a half-migrated `instance/statlee.db`.

### Changed
- Refreshed pinned dependencies to current releases: google-genai 1.56 to
  2.12, redis 5.2 to 8.0, anthropic 0.111 to 0.117, openai 2.43 to 2.45,
  matplotlib 3.10 to 3.11, and filelock 3.25 to 3.30. The google-genai 2.x
  major bump changes only its Interactions API (unused here); the
  `generate_content` surface the app relies on is unchanged, confirmed by an
  API-surface check and a live Gemini round-trip on the upgraded SDK.

### Docs
- Re-recorded the README demo GIF and workspace screenshot against the current
  v1.3.x interface, from a real analysis run on the bundled sample dataset.

## [1.3.1] - 2026-07-14

User-interface rebuild on a shared design-token system. No breaking changes.

### Changed
- Adopted a token-layer front end: `index.html`, `app.css`, and a rebuilt
  Tailwind config drive every screen from one set of design tokens.
- Rebuilt the landing page and the password-reset page on the token system, and
  built out the run lifecycle and empty states across the workspace.

### Fixed
- Four UI bugs surfaced while rebuilding the run lifecycle and empty states.

### Internal
- Test suite grew from 311 to 331 passing (plus 4 skip-marked tests: 2
  POSIX-only ones that run in CI and 2 real-Docker ones that always skipped).

## [1.3.0] - 2026-07-11

Third audit-pass cycle: 21 findings closed (7 P1, 14 P2) across billing
ordering, prompt-material caps, database migrations, account recovery, the
execution sandbox, storage integrity, and browser-side hardening. No breaking
changes for end users. The test suite grew from 229 to 311 passing (plus 4
skip-marked tests: 2 POSIX-only ones that run in CI and 2 real-Docker ones that
always skipped).

### Security
- All client-supplied prompt material is now length-capped server-side before
  it reaches a model call (chat history, codebook, code, converse context,
  interpret output and plots, report fields), closing an unmetered
  input-token amplification vector on the operator's key.
- `/generate_report` now moderates client text and, when billing is enabled,
  debits a credit before the expensive first pass and refunds it if the stream
  fails. It was previously unbilled, unmoderated, and uncapped.
- `/interpret` grounds on the server-recorded last-run output, plots, and
  executed script instead of trusting the client's spoofable copies, and
  moderates the client fallback when no server run exists.
- Login and registration have a dedicated rate limit (`RATE_LIMIT_AUTH`,
  default 10 per minute).
- Responses carry a strict Content-Security-Policy (`script-src 'self'`),
  `X-Content-Type-Options: nosniff`, and `X-Frame-Options: DENY`. The inline
  boot script moved to a static file fed by a JSON data island, and inline
  onclick handlers were replaced with a delegated listener.
- CSRF tokens are compared in constant time, and malformed tokens are
  rejected cleanly.
- Data-page filters treat the search term literally instead of as a regular
  expression, removing a catastrophic-backtracking DoS and fixing searches
  that contain ordinary punctuation.
- Uploads are capped per identity (file count and total bytes) to prevent
  disk-fill abuse, and oversized `.txt` uploads are rejected before the slow
  PDF conversion starts.
- Email verification tokens expire after 48 hours.

### Added
- Database migrations (Flask-Migrate/Alembic). Fresh installs migrate to head
  at boot; legacy `create_all` databases are stamped at the v1.2.0 baseline
  and upgraded, so future schema changes no longer break existing
  deployments. gunicorn now preloads the app so migration runs once, not once
  per worker.
- Password reset flow: `/request_password_reset` (no account enumeration,
  rate-limited) and `/reset_password` with single-use one-hour tokens, built
  on the existing verification-email plumbing.
- `flask grant-credits` CLI and an optional `MONTHLY_FREE_CREDITS` lazy
  monthly top-up. The out-of-credits message now only promises a refresh when
  one is actually configured.
- `SANDBOX_WORK_ROOT` so the Docker sandbox works when the app itself runs in
  a container with a socket-mounted host daemon (sibling-container bind
  mounts), with the run directory made readable by the non-root runner.

### Fixed
- `/chat` validates the dataset before debiting a credit, so a stale or
  invalid filename no longer costs a credit or a unit of the operator's
  monthly ceiling, and a denied debit returns the ceiling unit it took.
- All three LLM backends detect token-ceiling truncation and raise a clear
  error instead of returning silently cut-off code.
- Wrangling no longer clobbers the approved analysis script (approved scripts
  are kept per content hash, newest five), and concurrent wrangles no longer
  lose version-history entries (cross-worker file lock on the manifest).
- Uploads whose filename ends in a reserved version suffix such as `__v2` are
  rejected instead of silently overwriting wrangle history.
- On POSIX, a timed-out subprocess-mode run now kills the whole process
  group, so grandchild processes no longer outlive the timeout.
- Saved history is capped at the newest 200 rows per user.

## [1.2.0] - 2026-07-07

Follow-up correctness and hardening cycle. Closes a second audit pass with one
P0 sandbox-safety fix and a set of P1/P2 fixes across the wrangle, billing,
config, auth, and chat layers. No breaking changes for end users. The test suite
grew from 216 to 229.

### Security
- The conversational data-wrangling path now run-guards the LLM-generated
  transform code through the moderation gate before it executes, closing a gap
  where wrangle transforms bypassed the check that analysis runs already had.

### Fixed
- `/chat` refunds the debited credit when a code-generation stream fails, so a
  failed request no longer charges the account (applies when billing is enabled).
- `/verify_email` is now rate-limited, matching the other token-consuming auth
  endpoints.
- Config emits a startup warning when `TRUST_PROXY_HOPS=0` in production, where
  the app is typically fronted by a proxy and rate limiting would otherwise key
  on the proxy IP.
- The chat pipeline surfaces the feature-selection fallback as an explicit SSE
  phase event instead of failing silently on wide datasets.

### Changed
- Gemini stream usage extraction is now explicit rather than relying on implicit
  attribute access, and the case-insensitive moderation-verdict parsing is
  documented as intentional.

### Tests
- Added skip-marked real-Docker sandbox integration tests (run when a Docker
  daemon and the `statlee-runner` image are available). Suite: 229 passing, 2
  skipped without Docker.

## [1.1.0] - 2026-07-02

Correctness and hardening release. A full-codebase audit closed 16 issues across
rate limiting, the LLM and sandbox layer, storage integrity, input validation, and
billing. No breaking changes for end users. The test suite grew from 178 to 216.

### Added
- `RATE_LIMIT_DEFAULT` is now enforced as a global fallback limit. It was parsed
  from the environment but never applied, so endpoints without an explicit limit
  (including `/login` and `/register`) were unthrottled.
- `EXEC_OUTPUT_LIMIT` is now read from the environment, and the data-wrangling
  sandbox applies the same output cap as analysis runs.
- `xlrd` dependency so `.xls` uploads work; the format was advertised but failed
  on every deployment.

### Fixed
- `.xls` uploads now succeed, and the dependency error names the correct engine.
- The Gemini client now has an HTTP timeout, so a stalled upstream call can no
  longer pin a worker thread until the whole worker is killed.
- The Docker execution sandbox terminates a timed-out container instead of leaving
  it running with its full memory and CPU allowance.
- Version-history, metadata, and approved-script files are written atomically, so a
  crash mid-write can no longer silently reset a dataset's cleaning history.
- Anonymous-file TTL cleanup now runs on ordinary traffic (not only on upload) and
  removes empty leftover subdirectories.
- Malformed JSON request bodies on several endpoints now return a clear 400 instead
  of a 500.
- Duplicate registration returns 409 instead of 500; a failed verification email
  now reports a distinct message instead of silently locking the account out.
- Per-account history fields are length-capped, and the history modal no longer
  shows the same run twice.

### Security
- Credit debiting is now a single atomic operation and runs only after the
  moderation gate, preventing double-spend or negative balances and charges for
  blocked requests (applies when billing is enabled).
- The legacy master-password comparison is now constant-time.

### Removed
- The unused, write-only `Dataset` table (it was never read back). On a pre-existing
  database the now-unreferenced `datasets` table remains but is harmless; it is
  simply no longer created or written.

## [1.0.1] - 2026-06-28

Maintenance and presentation release. The application's behavior is unchanged
from 1.0.0; this batches the README demo, a sample dataset, and a CI tooling bump.

### Added
- Animated end-to-end demo GIF in the README (upload, plain-English request,
  generated code, chart, and written report).
- `docs/examples/sample_survey.csv`, a synthetic sample dataset so a new user
  gets a first result without supplying their own data.

### Changed
- CI bumped to `actions/checkout@v5` and `actions/setup-python@v6`, clearing the
  GitHub Node 20 runtime deprecation. Test matrix (3.11 / 3.12) and steps are
  unchanged.

## [1.0.0] - 2026-06-23

First public release. STATlee turns a plain-English request over an uploaded
dataset into generated, moderated, sandboxed, and explained statistics.

### Analysis pipeline
- Natural-language request to runnable Python/R, with a default-deny moderation
  gate, optional feature-selection pass for wide datasets, and a debugging
  assistant when a run fails.
- Intelligent codebook that classifies each variable (nominal / ordinal /
  continuous) so the model will not run an invalid model on a categorical
  outcome. Codebooks can be inferred from a PDF data dictionary or a survey
  questionnaire.
- Multi-format ingestion: CSV, TSV, Excel (`.xlsx`/`.xls`), Stata (`.dta`), and
  SPSS (`.sav`), with native value labels seeding the codebook.
- Conversational data wrangling with a chat transcript, undo/redo version
  history, and one-click revert to the original upload.
- AI report builder grounded in real run outputs, plus one-click project export
  (data, script, plots, report).

### LLM layer
- Pluggable provider chosen with `LLM_PROVIDER`: Gemini (default), Anthropic, or
  OpenAI. Every call routes through one role-based service (`pro` / `flash` /
  `lite` / `draft`), so switching provider or model is a config change.
- Pro mode toggle routes code generation to a larger model on demand.
- Per-request token usage and an estimated per-model session cost surfaced live
  in the UI.

### Security and isolation
- Execution sandbox: throwaway working directory, secret-free environment, and
  POSIX resource limits. `SANDBOX_MODE=docker` adds a network-less, non-root,
  read-only, resource-capped container per run.
- Run-guard re-moderates any hand-edited script before it executes.
- CSRF double-submit protection, per-identity rate limiting (keyed on IP or
  account, not a resettable cookie), and per-session file isolation.

### Accounts, billing, and operations
- Optional email/password accounts with optional email verification, behind
  config flags.
- Billing seam (`check_and_debit`) with money-safety guardrails: a monthly
  ceiling on premium calls and a startup warning if billing is on without a cap.
- App-factory Flask layout in the `statlee` package, Docker / Docker Compose
  setup, and CI (ruff, byte-compile, pytest on 3.11 and 3.12).

### Tests
- 178 tests covering the full HTTP surface with an injected fake LLM, so the
  suite runs offline with no API key.

[1.4.1]: https://github.com/yib7/STATlee/releases/tag/v1.4.1
[1.4.0]: https://github.com/yib7/STATlee/releases/tag/v1.4.0
[1.3.2]: https://github.com/yib7/STATlee/releases/tag/v1.3.2
[1.3.1]: https://github.com/yib7/STATlee/releases/tag/v1.3.1
[1.3.0]: https://github.com/yib7/STATlee/releases/tag/v1.3.0
[1.2.0]: https://github.com/yib7/STATlee/releases/tag/v1.2.0
[1.1.0]: https://github.com/yib7/STATlee/releases/tag/v1.1.0
[1.0.1]: https://github.com/yib7/STATlee/releases/tag/v1.0.1
[1.0.0]: https://github.com/yib7/STATlee/releases/tag/v1.0.0
