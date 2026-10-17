# STATlee

**An AI-Assisted Data Analysis Platform for Social Scientists**

STATlee is a web-based, AI-driven data analysis platform that abstracts away the coding process. Designed primarily for social science students and researchers, it lets users upload datasets and use natural language to request complex analytical workflows without writing Python or R syntax.

It uses a role-based LLM architecture (pluggable across Google Gemini, Anthropic Claude, and OpenAI) to generate, validate, and securely execute code in an isolated sandbox, then returns statistical interpretations and visualizations natively.

## Key Features

* **Editable codebook:** The Codebook tab lists every variable with its type, label, value labels, missing codes, role (analyze, ID, survey weight, ignore) and scale group. It is seeded from `.sav`/`.dta` labels, a PDF data dictionary or survey questionnaire, and a model classification of a data sample; your edits always win, survive cleaning, and export and import as JSON. The model is told about missing codes, weights and ID columns.
* **Run workbook:** Every run is kept in a numbered run rail in Results: star or rename a run, reopen an older one read-only (output, plots, its own interpretation), load its script back into the editor, and choose which runs the report is grounded in.
* **Methods and assumptions card:** Each script prints a short summary of the test, N used, assumption checks (pass, warn, fail) and effect sizes, shown as a card above the terminal and cited in the interpretation. If a script prints none, the card says so.
* **Multi-format ingestion:** CSV, TSV, Excel (`.xlsx`/`.xls`), Stata (`.dta`), and SPSS (`.sav`), normalized to CSV internally, with native variable labels seeding the codebook for free.
* **Role-based model routing:** Each step is addressed by a *role* (`pro`/`flash`/`lite`/`draft`) mapped to a model in config, so swapping a model is a config change, not a code change. Per-analysis token usage is surfaced in the UI.
* **Sandboxed execution:** Generated scripts run in a throwaway working directory with a secret-free environment and (on POSIX) resource limits. `SANDBOX_MODE=docker` runs each execution in a network-less, non-root, read-only sibling container.
* **Run-guard:** The server remembers the script it produced; the editable code editor lets you tweak it, and any hand-edited script is re-moderated before it is allowed to run.
* **Conversational data wrangling:** Describe a transform in plain English ("drop rows with missing income, then z-score age"); STATlee runs it in the sandbox and tracks every dataset version with undo/redo.
* **AI interpretation & auto-debugging:** Translates dense terminal output and p-values into plain-English Markdown insights, and switches to a debugging assistant when a run fails.
* **AI report builder:** Synthesizes an academic report grounded strictly in your actual outputs, with targeted "revise this passage" edits.
* **Reproducibility bundle:** The export zip runs as unzipped: the analysed data and the original upload, `cleaning.py` replaying your cleaning steps, the analysis script, `codebook.json`, `provenance.json` (every upload, cleaning step, run and interpretation with times, models and SHA-256 hashes), `environment.txt`, a README, and one folder per run. Choose all, starred or the latest run, and whether your typed prompts are included.
* **Converse tab:** A guarded methods mentor for follow-up questions and a *guide mode* that helps turn a vague hunch into a rigorous, ready-to-run analysis prompt.
* **Optional accounts:** Anonymous sandbox use by default (nothing persisted); optional email/password accounts persist datasets and analysis history.

## Architecture

The application lives in the `statlee/` package and uses the Flask app-factory
pattern with focused modules (entry point: `wsgi:app`, which loads `.env` and calls
`statlee.app.create_app()`; importing the package itself has no side effects):

| Module | Responsibility |
|---|---|
| `statlee/config.py` | Validated, env-driven configuration (one source of truth). |
| `statlee/app.py` | App factory + cross-cutting middleware (sessions, CSRF, rate limits, request-id logging). |
| `statlee/storage.py` | Per-identity file isolation + dataset version control. |
| `statlee/runs.py` | Session workbook: every run kept under `runs/<run_id>/`, with the run and byte caps. |
| `statlee/codebook.py` | Layered, editable variable codebook (user > file > PDF > model). |
| `statlee/methods.py` | Parser for the methods and assumptions block scripts print. |
| `statlee/bundle.py`, `statlee/environment.py` | Reproducibility bundle: provenance, README, environment probe. |
| `statlee/codecheck.py` | Non-LLM static pre-check (Python import allowlist, R regex denylist). |
| `statlee/sandbox.py` | Isolated code execution (subprocess or Docker). |
| `statlee/llm.py` | Role-based LLM service (pluggable Gemini / Claude / OpenAI backend) with usage tracking. |
| `statlee/billing.py` | Monetization seam (off by default). |
| `statlee/prompts.py` | Every prompt builder in one reviewable place. |
| `statlee/datatools.py` | Multi-format ingestion + metadata profiling. |
| `statlee/models.py` | SQLAlchemy models (users, runs, issue reports). |
| `statlee/routes/` | Blueprints: `auth`, `datasets`, `export`, `report`, `analyze`, `converse`, `misc`, `runs`, `codebook`. |
| `statlee/migrations/` | Alembic schema migrations, applied at boot. |

A deeper architecture walkthrough is in [ARCHITECTURE.md](ARCHITECTURE.md).

**Operations:** the (illustrative) pricing model and how the billing seam backs
it are in [Pricing](PRICING.md). STATlee is not currently deployed to a public
website; it runs locally.

## Getting Started

Install and first run are the numbered steps in the main README:
[Quickstart](../README.md#quickstart) (Docker Compose, the supported way to run
STATlee) and [Development](../README.md#development) (a local virtual
environment for contributors). This section adds what those steps leave out.

### Settings beyond the API key

`cp .env.example .env` gives you a working development config; the only value
you must fill in is the selected provider's key. Every variable is documented in
[.env.example](../.env.example). The ones people change first:

```env
# development | production | testing. Keep development for a local compose run:
# production enforces secrets and sets Secure cookies, which browsers drop over
# plain http://localhost.
APP_ENV=development
# Required in production, generate with:
#   python -c "import secrets; print(secrets.token_hex(32))"
FLASK_SECRET_KEY=
# Optional: lock the whole UI behind a single password
PASSWORD=
```

Compose passes every variable in `.env` to the container through `env_file`,
while the file itself stays masked inside it. It publishes on `127.0.0.1:5000`
only. The SQLite DB and uploads persist in the `statlee-instance` named volume,
and the container runs as an unprivileged user.

### Docker sandbox mode

By default (`SANDBOX_MODE=subprocess`) generated code runs as a limited child
process inside the app container. `SANDBOX_MODE=docker` instead runs each
analysis in a throwaway, network-less container built from
`docker/runner.Dockerfile`:

```bash
docker build -f docker/runner.Dockerfile -t statlee-runner .
```

The app image does **not** include the `docker` CLI, so docker mode only works
when the app itself runs on a host (or a derived image) with the docker CLI and
daemon access; from the shipped compose setup every run would fail with
"Interpreter not available".

### PostgreSQL

SQLite is the default and needs nothing. For PostgreSQL set
`DATABASE_URL=postgresql+psycopg://user:pass@host:5432/statlee`; the `psycopg`
driver is already in the Docker image and in `requirements/app.lock`.

### Running without Docker

The README's [Development](../README.md#development) steps run the app straight
on your host. Generated code then runs on your machine, so this is *not
recommended for untrusted input*. The dev server listens on `127.0.0.1:5000`
only; `BIND_HOST=0.0.0.0` exposes it to your network, where anyone could run
generated code billed to your key. To install only what the app needs (no test
tools), use `pip install -r requirements/app.lock` instead of `dev.lock`.

**LLM.** Pick a provider with `LLM_PROVIDER` (gemini default, anthropic, or
openai) and set that vendor's key (`GEMINI_API_KEY` / `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY`), required in production. Model ids per role can be pinned with
`MODEL_PRO` / `MODEL_PRO_MAX` / `MODEL_FLASH` / `MODEL_FLASH_LITE`. See
`.env.example` for all variables.

## Development & Testing

The commands are in the README's [Development](../README.md#development)
steps (`pip install -r requirements/dev.lock`, `python -m pytest`,
`python -m ruff check .`).

The test suite injects a deterministic fake LLM service, so the entire HTTP surface (uploads, codebook, wrangling, run-guard, converse, export, auth, CSRF) is exercised offline. CI runs ruff, pip-audit on the lockfiles, pytest on Linux and Windows, both Docker images and the README setup steps on every push and PR (see `.github/workflows/ci.yml`). Optional local hooks are configured in `.pre-commit-config.yaml` (`pre-commit install`).

**Dependencies.** `requirements/*.txt` hold the hand-edited direct pins;
`requirements/app.lock`, `dev.lock` and `runner.lock` are compiled from them
with every transitive package pinned and hashed, and everything installs from
the locks. After editing a `.txt`, regenerate the locks with the `uv pip compile`
commands at the top of `requirements/base.txt`; `tests/test_lockfiles.py` fails
until you do.

## Tech Stack

* **Frontend:** Vanilla JavaScript (modular `CC` namespace), HTML5, Tailwind CSS, CodeMirror, vendored/pinned `marked` + `DOMPurify`.
* **Backend:** Python, Flask (app factory + blueprints), Pandas, Flask-SQLAlchemy, Flask-Login, Flask-Limiter.
* **AI Integration:** Pluggable LLM provider: Google GenAI SDK (default), with optional Anthropic and OpenAI SDKs.
* **Data formats:** pandas, openpyxl (Excel), pyreadstat (Stata/SPSS), pypdf + fpdf2 (codebooks).
* **Infrastructure:** Docker, Docker Compose, Gunicorn; SQLite (dev) / PostgreSQL (prod, `psycopg` driver from `requirements/postgres.txt`, included in the Docker image; migrations are tested against PostgreSQL in CI).

## Hosting

STATlee is not currently deployed; it runs locally (see the
[Quickstart](../README.md#quickstart) in the main README).
