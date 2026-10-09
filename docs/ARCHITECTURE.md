# STATlee Architecture & Codebase Guide

A walkthrough of how STATlee is put together, for anyone reading the code for the
first time. It explains the request lifecycle, the analysis pipeline, the
security boundaries, and where each responsibility lives.

## Big picture

STATlee is a **Flask app-factory** (`app.py`) wiring together focused modules and
seven route blueprints. The frontend is **vanilla JavaScript** (no build step)
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
   ├─ routes/runs.py      /runs (the session workbook)
   ├─ routes/codebook.py  /codebook (+ import, export)
   └─ routes/converse.py  /converse
        │
        ├─ llm.py        role-based LLM calls (+ usage, cache)
        ├─ prompts.py    every prompt string builder
        ├─ storage.py    per-identity files + dataset versions
        ├─ runs.py       session workbook: every run kept under runs/<run_id>/
        ├─ codebook.py   layered, editable variable codebook
        ├─ methods.py    parser for the methods and assumptions block
        ├─ bundle.py     reproducibility bundle pieces (provenance, README)
        ├─ environment.py  sandbox environment probe for the bundle
        ├─ codecheck.py  static allowlist pre-check before any execution
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
`current_app.config['STATLEE']`. `.env.example` documents every variable the
app reads, and `tests/test_env_example.py` fails when one is read but not
documented (or documented but never read).

Settings added in the 2026-10 audit cycle:

| Variable | Default | What it does |
|---|---|---|
| `PUBLIC_BASE_URL` | empty | Origin for emailed reset and verification links. Required in production for both. |
| `BIND_HOST` | `127.0.0.1` | Interface `python wsgi.py` binds; `0.0.0.0` exposes it to the LAN. |
| `METRICS_TOKEN` | empty | Bearer token for `/metrics`; empty disables it in production. |
| `STATLEE_SKIP_BOOT_MIGRATE` | unset | Skip the boot schema upgrade (`flask db` and `statlee-doctor` skip it on their own). |
| `LLM_TIMEOUT_SECONDS` / `LLM_PRO_TIMEOUT_SECONDS` | 120 / 300 | Per-request model timeout; the second applies to the `pro_max` role. |
| `LLM_MAX_RETRIES` | 2 | Extra attempts on transient provider errors (408/429/5xx, timeouts). |
| `MAX_UPLOAD_UNCOMPRESSED_MB` / `MAX_UPLOAD_CELLS` | 512 / 10,000,000 | Decompression-bomb and size bounds on an upload's parse. |
| `RATE_LIMIT_DATA_PAGE` | `60 per minute` | Limit for the data viewer's paging route. |

Added with the session workbook:

| Variable | Default | What it does |
|---|---|---|
| `MAX_RUNS_PER_IDENTITY` | 50 | Runs kept per identity. Over the cap the oldest unstarred run is deleted (the oldest run when every one is starred). Below 1 warns and resets to 50. |

## Request lifecycle (`app.py`)

Every request passes through ordered middleware:

1. **`assign_request_context`**: mints an 8-char `request_id` (correlated into
   every log line via a logging filter), a per-browser `sid` (the file-isolation
   key), and a `csrf_token`.
2. **`csrf_protect`**: double-submit check on `POST/PUT/PATCH/DELETE`: the
   `X-CSRF-Token` header must equal the session token.
3. **`require_auth`**: gate everything except `PUBLIC_ENDPOINTS` (the loader,
   landing, health, and auth handshake). Behavior depends on the auth *mode*.
4. The view runs; **`stamp_request_id`** adds `X-Request-ID` to the response.

`/health` is exempt from the default rate limit, never mints a session, and
runs a `SELECT 1`, so it answers `503` when the database is unreachable.

`/metrics` (uptime plus every model's call and token totals for the whole
server) is operator data, so it has its own gate instead of the app's. With
`METRICS_TOKEN` set, a request must send it as `Authorization: Bearer <token>`
or `X-Metrics-Token` (compared in constant time), in every environment, and
needs no session or login; a scrape mints no cookie. With it unset, production
answers `404`, and development and testing keep the route behind the normal
auth gate.

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

Signing in (`/login`, a signed-in `/register`, `/verify_email`) moves the
browser session's anonymous workspace into the account
(`storage.adopt_workspace`): each dataset moves as a whole group (file, version
files, manifest, metadata), and a dataset whose name the account already holds
is skipped, so the account copy is never overwritten. `/login` and `/register`
return `workspace: {moved: [...], skipped: [...]}` with the stored names, and
the client names any skipped dataset.

Password-reset and email-verification links are built from `PUBLIC_BASE_URL`,
never from the request's `Host` header. With no SMTP configured the link (and
its token) is logged only in development; other environments log a warning
without it. In production a missing `PUBLIC_BASE_URL` means no link is sent;
for verification, `/register` then answers `202 verification_email_failed`.

## Identity & storage (`storage.py`)

Each principal gets an isolated directory:

- logged-in users → `user_<id>/` (durable, as long as the storage root is),
- anonymous sessions → `anon_<sid>/` (expired after `FILE_TTL_SECONDS` of
  inactivity).

Anonymous expiry goes by the session's **last activity**, not by each file's
age: every storage access touches a `.last_seen` marker (at most once a
minute), and the cleanup removes a whole `anon_*` workspace in one `rmtree`
once its newest file is older than the TTL. An active session never loses its
original upload while its later versions survive.

The storage root is `UPLOAD_ROOT`, or `instance/uploads` when unset (testing
uses a throwaway temp dir). In a container, keep it on a persistent volume (the
Dockerfile sets `UPLOAD_ROOT=/app/instance/uploads` and compose mounts a named
volume at `/app/instance`), or logged-in users' files go with the container.

`resolve_path()` is the safety chokepoint: it runs `secure_filename`, joins
against the caller's own root, then does a `realpath` containment check, so no
filename (traversal, absolute path) can escape the caller's directory. Datasets
are **version-controlled**: each wrangle writes `stem__vN.csv` tracked by a JSON
manifest with an active-version pointer (undo/redo truncates the redo branch like
an editor's undo stack). Each version entry keeps the transform `code` and an
`op` (`transform` or `revert`), so an export can replay the cleaning. Names
whose stem ends in `__vN` are reserved for these files: an upload with one is
refused, and every route that names a dataset answers `400` for one.

Manifest writes run under a cross-process file lock. The manifest also carries
a **version token** (a fresh `upload_id` per upload plus a `rev` counter bumped
by every upload, wrangle, undo, redo and revert). `/wrangle` reads the token
before it reads the data and commits only if it is unchanged; otherwise it
answers `409`. A wrangle that read the old data therefore never lands on top of
a same-name re-upload, or of an undo followed by a new edit (where the version
number and file name repeat). `/dataset_versions` returns the changelog plus
the active version's `headers` and `profile`, which is what the client re-reads
after a `409`.

The **approved-script** store (`.approved_script.json`) backs the run-guard. It
is keyed on `sha256(language + code)` (a script approved as Python must be
re-checked before it runs under R) and updated under a file lock.

### Session workbook (`runs.py`, `routes/runs.py`)

Every run is kept, not just the last one. A run is a directory under the
caller's own root:

```
<identity_root>/runs/
  index.json            {"runs": [ids, oldest first], "latest": id, "next": n}
  .index.lock
  r0001/
    meta.json           run_id, created_at, prompt, filename, dataset_version,
                        dataset_sha256, model, role, name, starred, methods,
                        prompt_inherited
    script.json         {code, language}
    status.json         {returncode, timed_out}
    output.txt
    plot_1.png ...
    interpretation.md   written later, by /interpret for that run
```

`save_run` allocates the id, writes the run into a hidden temp directory, renames
it into place and updates the index, all under one file lock, so a reader that
goes through the index never sees a half-written run. `next` only grows, so an id
is never reused after an eviction. A missing or corrupt index is rebuilt from the
run directories. A run id is `^r[0-9]{4,6}$`, must be listed in the caller's own
index and resolves under the caller's `runs/` only: a malformed, unknown, evicted
or foreign id is the same `404` (no existence oracle). A pre-workbook
`.last_run/` is adopted as the first run on first access.

Routes: `GET /runs` (summaries, oldest first), `GET /runs/<run_id>` (the full
run, including the parsed methods card), `PATCH /runs/<run_id>` (`name`, up to 80
characters, and `starred`). `/run` returns its `run_id`; `/interpret` and
`/converse` take an optional `run_id` (default: the latest run); `/generate_report`
takes optional `run_ids` and grounds on each selected run's output,
interpretation and methods card, in run order. All of that grounding is the
server's record, never client text. A run records the question, dataset name,
active dataset version and its sha256, and the drafting role and model. A re-run
of an unchanged script keeps the provenance of the most recent run of that
script; an edited or pasted script gets the latest generation's question marked
`prompt_inherited`, with no role or model. `/interpret` on an older run reads
that run's own dataset version, and omits the row count and column context when
the file is gone or its hash no longer matches.

The store is capped by `MAX_RUNS_PER_IDENTITY` and by a byte budget: 25 percent
of `MAX_BYTES_PER_IDENTITY` (`runs.RUNS_BYTES_SHARE_PERCENT`; 50 MB of the
default 200 MB, or 50 MB when that cap is off), so stored runs always leave room
for uploads. Past either cap the oldest unstarred run is evicted (the oldest run
when all are starred; never the run just saved). A run keeps at most 20 plots
and 10 MB of plots (`sandbox.PLOTS_MAX`, `sandbox.PLOTS_BYTES_MAX`); the output
says how many were not kept. `/run` reads the dataset's version number, path and
hash from one manifest read before the sandbox starts, so a wrangle that lands
mid-run cannot pair the new version with the old bytes. A locked file in a
legacy `.last_run/` (Windows) leaves it in place and adoption retries on a later
access. For account users the history
rows (`analysis_runs`) link back to a run through nullable `run_id`,
`dataset_version` and `model` columns; a restored row gets its full output and
plots only while its run still exists (`run_available`).

Limits worth knowing:

- Run plots count toward `MAX_BYTES_PER_IDENTITY`; the byte budget above keeps
  the workbook to a quarter of it.
- Signing in moves an anonymous workbook into the account only when the account
  has no runs. If it has its own, the account's workbook wins and the anonymous
  runs expire with the anonymous workspace; the two are not merged.

### Codebook (`codebook.py`, `routes/codebook.py`)

The codebook is server state, kept in the dataset's meta sidecar as one layer
per source, each holding only the fields that source set:

```
"codebook": {"layers": {"user": {col: {...}}, "native": {...}, "pdf": {...}, "llm": {...}}}
```

The merged entry for a column is worked out per request, field by field, in the
order **user > native > pdf > llm**, so a seed or a re-classification can never
overwrite something the user typed. An entry has `type` (continuous, ordinal,
nominal, binary, text, id, date), `label`, `value_labels`, `missing_codes`,
`role` (analyze, id, weight, ignore) and `scale_group`, plus `source`,
`user_fields` and `field_sources` for provenance. Sources:

- **native**: the labels, value labels and user-missing codes in a `.sav` or
  `.dta` file, read at upload;
- **pdf**: the description extracted from an attached data dictionary or
  questionnaire (`/extract_pdf_codebook` with `dataset`);
- **llm**: the type from `/classify_variables`;
- **user**: `PUT /codebook` and `POST /codebook/import`.

Because the merge runs over the active version's columns, a wrangled dataset
inherits entries by column name, a new column shows a default entry, and a
dropped column disappears and comes back on undo. A sha256-keyed archive
(`.codebook__<sha>.json`, the 50 newest kept) restores the user, pdf and llm
layers when the same content is uploaded again, even under another name.
Archives move with the workspace at sign-in, except one the account already
holds for the same content.

Routes: `GET /codebook`, `PUT /codebook` (partial: only the fields sent become
user fields), `POST /codebook/import` and `GET /codebook/export`. The export is
the `statlee-codebook` document that import accepts back, and that the bundle
ships as `codebook.json`. Responses carry an explicit `columns` list because
`jsonify` sorts object keys. Every field is length-clamped (labels 2000
characters, 200 value labels, 50 missing codes), and writes are locked
read-modify-write cycles with an atomic replace. The stored codebook (all
layers, as JSON) is capped at 4 MB (`codebook.CODEBOOK_BYTES_MAX`): a write that
would grow it past that is refused with a `413` and nothing is stored; one that
shrinks an oversized codebook still goes through.

Prompts read only the server codebook. A client-sent `codebook` or `pdf_mapping`
is accepted but ignored (logged at debug). The text is user- or model-supplied,
so it sits inside the untrusted-data delimiters and under the prompt clamps
(`prompts.untrusted` shortens any `<<<` or `>>>` run in its payload, so no text
can close or forge a block). The
draft prompt also gets the field rules (missing codes are treated as missing; id
and ignore columns are never modelled; a weight column is applied for weighted
estimates; scale groups) and a `CODEBOOK ROLES` block naming them.

### Methods and assumptions block (`methods.py`)

The draft prompt (`prompts.METHODS_BLOCK`) asks every script to print this at the
very end of its output, with Python `print()` or R `cat()`:

```
== METHOD ==
test: <name>; outcome: <var>; predictors: <vars>; n_used: <int>; dropped: <int> (<reason>)
== ASSUMPTIONS ==
<check name>: <statistic/result> -> PASS|WARN|FAIL
== EFFECT ==
<effect size name> = <value> [95% CI <lo>, <hi>]
== END ==
```

`methods.parse(output)` never raises. It tolerates case and spacing, header
synonyms, Windows line endings, R's `[1] "..."` prefixes, a missing `END`, extra
lines, a repeated block (the last wins) and a verdict followed by a short note.
A check line with no verdict, or a negated one ("did not pass"), is kept with
status `unknown` rather than guessed as a pass. Lines that are the contract's own
unfilled placeholders are dropped. The result is
`{method, assumptions: [{name, detail, status}], effects, reported}`; with no
block it is the empty card with `reported: false`.

`/run` parses the output once and stores the card in the run's `meta.json`
(`methods: null` still means "saved before this existed" and is parsed lazily
when served). The Results tab shows it above the terminal. `/interpret` and
`/generate_report` are given each run's card as server-side context, so the
write-up cites the test, the N used and each check's verdict (WARN and FAIL as
caveats), and says so when the card is not reported. A failed run's debug path
gets no card. The block depends on the model following the format: the parser is
lenient, but when a script prints no block the card reads "Assumptions not
reported" and nothing is inferred.

### Reproducibility bundle (`/export`, `bundle.py`, `environment.py`)

The zip runs as unzipped. Its layout:

```
<name>.csv                   the active version, under the name the script reads
data/original_<name>.csv     the original upload, after CSV conversion
cleaning.py                  replays the recorded cleaning steps from the original
analysis.py | analysis.R     the script
codebook.json                the statlee-codebook export (importable)
provenance.json              what produced what (below)
environment.txt              Python/R versions and the analysis package versions
README.md                    how to run it, contents, how it was produced
report.md, full_report.md    the report, when one exists
outputs/run_0001/            one folder per included run:
    output.txt, plot_N.png, methods.json, interpretation.md, analysis.py|R
```

- `analysis.py|R` is the script of the latest run within the selected set, so its
  `script_sha256` matches a provenance run step and `report.md` describes the
  same run. With no selected run that kept a script, it falls back to the
  reviewed editor code, else the newer of the latest run's script and the newest
  approved script, but only ones that belong to the exported dataset (an
  approval whose context names it, a run that recorded it). With none, no script
  is bundled and the README says no analysis has been run on the dataset yet.
  `report.md` likewise carries only that dataset's latest run; a client-sent
  interpretation is never used. `cleaning.py` is written only when there are
  recorded steps and both dataset files are in the zip (a revert restarts the
  lineage, undone steps are left out, and a step that predates code recording
  omits it).
- The export dialog sends `runs` (`all`, `starred` or `latest`; default `all`) and
  `include_prompts` (a boolean; default true). Any other value is a `400`. Runs
  are scoped to the exported dataset by its stored name and by content: a run
  whose recorded `dataset_sha256` is not one of the versions in this bundle's
  lineage (it read earlier content of a re-uploaded same-name dataset) is left
  out, and the README counts those. The rest are capped at
  `MAX_RUNS_PER_IDENTITY`. Runs saved before 1.6.0 did not record a dataset and
  are left out of dataset exports; the README says so.
- `provenance.json` is `{generated_at, app_version, llm_provider, steps}`. Steps
  are ordered by time and are of four kinds: `upload` (`dataset_sha256_after`),
  `wrangle` (instruction, model, role, before and after hashes, the transform's
  `script_sha256`), `run` (question, model, role, `dataset_sha256_before`,
  `script_sha256`, `run_id`, and `prompt_inherited`, an extension to the
  provenance format) and `interpret` (`run_id`). Only recorded fields are
  written, never nulls, and it holds no data values and no system prompts.
- With `include_prompts=false` every `prompt` and `prompt_inherited` is dropped
  from the provenance, along with the request list in `report.md` and the
  instruction fallback in `cleaning.py` comments. Text written by the model
  (interpretations, reports, script comments) is not scrubbed and may still
  restate the question; the README and the dialog say so.
- `environment.txt` comes from a probe script (`environment.PYTHON_PROBE`, a
  fixed list of the analysis stack, reading `__version__`; R lists its installed
  packages when `Rscript` exists) that goes through `codecheck` and the sandbox
  like any other code. It runs lazily on the first export per boot and is cached
  per sandbox mode. A failed Python probe, or an R probe that did not finish, is
  cached for `FAILURE_TTL` (5 minutes) and then retried; a full success is cached
  until restart. Concurrent first exports share one probe, and the cache lock is
  never held while the sandbox runs. The file therefore describes the sandbox as
  it was when captured.
- The zip is built in a `SpooledTemporaryFile` (16 MB in memory, then on disk),
  PNGs are stored uncompressed, and every file copied in is checked to resolve
  under the caller's identity root. `/export` carries the same rate limit as
  `/run`, because it can start the probe.

## The analysis pipeline (`routes/analyze.py`)

`POST /chat` is the heart of the app. It streams over SSE:

1. **Moderation** (`lite`, temp 0): synchronous; must 403 before any stream.
2. **Feature selection** (`flash`): on wide datasets, pick the minimal columns.
3. **Draft** (`draft`→pro): stream the first script, deltas shown live.
4. **Validation** (`lite`): a second pass that replaces the draft client-side.
5. **Code moderation** (`lite`, temp 0): the run-guard re-moderates the final
   script itself; a `BLOCK` (or a moderation failure) ends the stream with an
   error and nothing is approved.
6. **Static check** (`codecheck.check_code`): the deterministic AST allowlist
   (Python) or regex denylist (R) runs as the last gate; a hit also ends the
   stream unapproved.
7. **Save approved script**: only a script that passed both gates is remembered,
   so `/run` executes exactly what was produced and checked.

`POST /run` enforces the **run-guard** (roadmap 0.4): it executes a script
without a second review only when this session approved that exact
`(language, code)` pair. Anything else (a hand-edit, a script restored from
history into a fresh session, a paste) is **re-moderated** via
`code_moderation` first: `403` on `BLOCK`, `503` when moderation is
unavailable (fail closed). The static check then runs on every script, approved
or not. The outcome (`returncode`, `timed_out`) is persisted with the run. A
timeout is an executed failure, so `/run` answers `200` with `success: false`,
`timed_out: true` and whatever the script printed before it was stopped.
`POST /interpret` streams a plain-English write-up, or switches to a debugging
assistant when the persisted outcome says the run failed (a non-zero exit code
or a timeout); it stores its text with the run.

`/wrangle` puts its generated transform through the same gates: instruction
moderation, code moderation, the static check and the sandbox, then the
version-token check above before the new version is saved.

### Grounding of `/converse` and `/generate_report`

Both ground on what the server stored, not on what the client sends. `/converse`
uses the stored interpretation of the last run as its results context, and
treats the editor code as already reviewed only when it is approved for either
language or is the script that produced the last run. `/generate_report` takes
the terminal output and interpretation from the server's record of each
selected run (the latest by default).
Client copies are used only where no server record exists. Those copies, an
edited or pasted script, the chat and converse history, and (for a revision)
the selected passage and the report are moderated together with the request,
failing closed.

The in-app **Pro mode** toggle threads a `pro` flag from the UI into the analysis
path: when set, code generation runs on the larger `pro_max` model
(e.g. `gemini-3.1-pro-preview`) instead of the standard code model, and the request still
passes through `billing.check_and_debit`.

## LLM service (`llm.py`)

All model access funnels through `LLMService`, addressed by **role**
(`pro`/`flash`/`lite`/`draft`, plus `pro_max` for Pro mode) mapped to a model id
in config. The backend is **pluggable**: `LLM_PROVIDER` selects Gemini (default),
Anthropic, or OpenAI behind one provider-neutral seam. Highlights:

- **Usage accounting** per model, exposed at `/metrics` (operator-gated, see
  above) and surfaced live in the UI (the `usageBadge`). Output counts include
  thinking/reasoning tokens, cached input is reported separately, a cache hit is
  counted as a hit rather than a billed call, and a failed or truncated call
  still records what the provider billed.
- **Typed output errors.** A provider safety block or refusal raises
  `BlockedGenerationError`, no text `EmptyGenerationError`, a stop at the output
  limit `TruncatedGenerationError`, and an unparseable or wrongly shaped JSON
  reply `MalformedReplyError`. Each carries a user-facing message and a
  `reason` code (`blocked`, `empty`, `truncated`, `malformed`). SSE routes send
  it on the `error` event; JSON routes answer `422` (blocked) or `502` (the
  rest) with `error` and `reason`. The client words each reason with a next
  step. No empty or truncated `/chat` script is ever approved, and its credit
  is refunded.
- **Timeouts and retries.** Every request has a timeout (`LLM_TIMEOUT_SECONDS`,
  or `LLM_PRO_TIMEOUT_SECONDS` for `pro_max`) and up to `LLM_MAX_RETRIES` retries
  with backoff on transient errors. Retries wrap the request only: a stream that
  dies midway is not replayed, which would bill twice.
- **Tolerant JSON.** `parse_json_reply` accepts a bare value, a fenced block or
  the first complete JSON value in the text, and each route shape-checks the
  result. Moderation still fails closed on anything it cannot read.
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
  every generated or edited script passes a deterministic check, wired into
  `/chat`, `/run` and `/wrangle`. For Python (stdlib `ast`) it is an
  **allowlist** model:
  - imports must be in `ALLOWED_PYTHON_MODULES` (the analysis stack plus
    side-effect-free stdlib, and `os`/`sys`/`shutil`/`pathlib`/`glob`/`io` for
    harmless uses such as `os.path`); everything else (`builtins`, `nt`/`posix`,
    `subprocess`, `socket`, `importlib`, `runpy`, `pickle`, ...) and every
    relative import is refused;
  - `eval`, `exec`, `compile`, `__import__`, `breakpoint`, `globals`, `locals`
    and `vars` are refused wherever the name appears (a call, `f = exec`, or a
    from-import), and `input()` when called;
  - dangerous members of `os`/`sys`/`shutil` are refused however the module is
    reached: an alias, an assignment, a constant `getattr`, or an attribute
    chain through another module (`pd.io.common.os.system`). Owners that are
    never legitimate (`subprocess`, `socket`, `ctypes`, `multiprocessing`, ...)
    are refused wholesale, as are primitives such as `Popen` and `urlopen` on
    any owner;
  - reflection escapes (`__globals__`, `__subclasses__`, `__dict__`,
    `__self__`, frame attributes, non-constant `getattr`/`setattr`,
    `operator.attrgetter`) are refused;
  - library file I/O (pandas `read_*`/`to_*`, numpy `load`/`save`, `savefig`,
    `Path(...)`, `os`/`shutil`/`glob` calls, `open`) is refused when a constant
    path, or the constant prefix of an f-string or concatenation, is absolute,
    uses `..`, starts with `~`, or is a URL. Pickle loading is refused outright.

  R cannot be parsed by `ast`, so it gets a coarser regex denylist (`system`,
  `shell`, network libraries, `do.call` with a dangerous or non-literal target,
  `.Call`/`.External`, `eval(parse())`, `get("system")`, ...). A dangerous
  construct fails closed; a script that does not parse fails open, since it
  cannot execute either.

  **Residual limitations** (best-effort by design): a path built entirely at
  runtime (`open(var)`, `Path.home() / ".ssh"`) is not resolved; internals of
  allowlisted libraries are covered only where an attribute chain names a
  policed owner or primitive, so private helpers that import or spawn by string
  are not modeled; format-string attribute traversal can read but is not
  modeled; non-destructive `os`/`shutil` members with dynamic arguments are
  allowed; and R calls built from strings outside `do.call`
  (`match.fun(paste0(...))`) can slip through. The LLM `code_moderation` pass
  and, in docker mode, the container are the other layers for these.
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
On a timeout, the output the script printed before it was stopped is kept and
the timeout notice is appended.

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
| `api.js` | `CC` namespace: state, fetch/CSRF helpers, SSE parser (one terminal callback per stream, per-reason error wording), sanitized markdown, usage badge, credit balance, toasts, console-error ring buffer. |
| `data.js` | Upload, data viewer paging, cleaning history (wrangle, undo/redo, the `409` reload). |
| `codebook.js` | The Codebook tab editor: per-variable rows (type, label, role, scale group, missing codes, value labels), debounced partial saves, import and export. |
| `workbook.js` | The run rail in Results: list, star, inline rename, read-only view of an older run, Load script, Interpret this run; the report's run checklist. |
| `methods.js` | The methods and assumptions card above the terminal. |
| `analyze.js` | Editor (CodeMirror), streamed generation, run + interpret, Pro mode toggle. |
| `converse.js` | Converse tab (mentor + guide mode). |
| `tools.js` / `ui.js` | Report builder, export dialog (runs to include, include my prompts), account dialog (sign-in keeps the workspace in place), tabs, sidebar, theming. |

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
`MODEL_*`, so a developer's `.env` or shell cannot steer results. Coverage
spans uploads, the workbook and codebook, methods parsing, the export bundle,
wrangling/versioning, the run-guard, converse, auth, CSRF, the rate-limit key
function, the billing seam, and Pro-mode routing. `tests/test_e2e_ui.py` drives the real frontend in Chromium
(Playwright) against a live localhost server that uses the same fake model; it
skips when Playwright is not installed. `ruff` lints; CI runs ruff +
byte-compile + pytest on push.

## Where to start reading

- Want the request flow? `app.py` → `routes/analyze.py`.
- Want the safety model? `codecheck.py` + `sandbox.py` + `storage.resolve_path`
  + the run-guard in `routes/analyze.py`.
- Want to swap a model or add a tier? `config.py` + `llm.py`.
