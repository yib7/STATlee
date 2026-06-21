# STATlee

**An AI-Assisted Data Analysis Platform for Social Scientists**

STATlee is a web-based, AI-driven data analysis platform that abstracts away the coding process. Designed primarily for social science students and researchers, it lets users upload datasets and use natural language to request complex analytical workflows without writing Python or R syntax.

It uses a role-based LLM architecture (Google Gemini) to generate, validate, and securely execute code in an isolated sandbox, then returns statistical interpretations and visualizations natively.

## Key Features

* **Intelligent Codebook:** Samples uploaded data to classify variables as Nominal, Ordinal, or Continuous, preventing statistical errors (e.g. linear regression on nominal data). Codebooks can also be extracted from a PDF data dictionary or *inferred from a survey questionnaire*.
* **Multi-format ingestion:** CSV, TSV, Excel (`.xlsx`/`.xls`), Stata (`.dta`), and SPSS (`.sav`) — normalized to CSV internally, with native variable labels seeding the codebook for free.
* **Role-based model routing:** Each step is addressed by a *role* (`pro`/`flash`/`lite`/`draft`) mapped to a model in config, so swapping a model is a config change, not a code change. Per-analysis token usage is surfaced in the UI.
* **Sandboxed execution:** Generated scripts run in a throwaway working directory with a secret-free environment and (on POSIX) resource limits. `SANDBOX_MODE=docker` runs each execution in a network-less, non-root, read-only sibling container.
* **Run-guard:** The server remembers the script it produced; the editable code editor lets you tweak it, and any hand-edited script is re-moderated before it is allowed to run.
* **Conversational data wrangling:** Describe a transform in plain English ("drop rows with missing income, then z-score age"); STATlee runs it in the sandbox and tracks every dataset version with undo/redo.
* **AI interpretation & auto-debugging:** Translates dense terminal output and p-values into plain-English Markdown insights — and switches to a debugging assistant when a run fails.
* **AI report builder:** Synthesizes an academic report grounded strictly in your actual outputs, with targeted "revise this passage" edits, and exports the whole project (data, script, plots, report) as a zip.
* **Converse tab:** A guarded methods mentor for follow-up questions and a *guide mode* that helps turn a vague hunch into a rigorous, ready-to-run analysis prompt.
* **Optional accounts:** Anonymous sandbox use by default (nothing persisted); optional email/password accounts persist datasets and analysis history.

## Architecture

The application lives in the `statlee/` package and uses the Flask app-factory
pattern with focused modules (entry point: `wsgi.py` → `statlee.app:app`):

| Module | Responsibility |
|---|---|
| `statlee/config.py` | Validated, env-driven configuration (one source of truth). |
| `statlee/app.py` | App factory + cross-cutting middleware (sessions, CSRF, rate limits, request-id logging). |
| `statlee/storage.py` | Per-identity file isolation + dataset version control. |
| `statlee/sandbox.py` | Isolated code execution (subprocess or Docker). |
| `statlee/llm.py` | Provider-agnostic LLM service with usage tracking. |
| `statlee/prompts.py` | Every prompt builder in one reviewable place. |
| `statlee/datatools.py` | Multi-format ingestion + metadata profiling. |
| `statlee/models.py` | SQLAlchemy models (users, datasets, runs, issue reports). |
| `statlee/routes/` | Blueprints: `auth`, `datasets`, `analyze`, `converse`, `misc`. |

The full roadmap and status of each feature lives in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

**Operations:** taking STATlee live without surprises on your bill is covered in
the [Deployment Playbook](DEPLOYMENT_PLAYBOOK.md); the (illustrative) pricing
model and how the billing seam backs it are in [Pricing](PRICING.md).

## Getting Started

Because STATlee executes dynamically generated code, running it via Docker is recommended for a secure, isolated sandbox.

### Prerequisites

* Docker Desktop installed and running.
* A Google Gemini API key.

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/statlee.git
cd statlee
```

### 2. Configure environment variables

Copy the example file and fill in your values (see [.env.example](../.env.example) for the full, documented list):

```bash
cp .env.example .env
```

```env
# Required
GEMINI_API_KEY=your_api_key_here
# development | production | testing  (production enforces secrets + secure cookies)
APP_ENV=production
# Required in production — generate with:
#   python -c "import secrets; print(secrets.token_hex(32))"
FLASK_SECRET_KEY=
# Optional: lock the whole UI behind a single password
PASSWORD=
```

### 3. Build and run with Docker

```bash
docker-compose up --build
```

Then open **http://localhost:5000**.

To enable true container isolation for code execution, build the runner image and set `SANDBOX_MODE=docker`:

```bash
docker build -f runner.Dockerfile -t statlee-runner .
```

---

## Local Developer Setup (Without Docker)

Running generated code directly on your host is *not recommended for untrusted input*, but is convenient for development.

1. Ensure **Python 3.11+** (and **R**, if you want R execution) are installed.
2. Create and activate a virtual environment:

   ```bash
   python -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   ```

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Run the app (development mode does not require an API key to boot, but LLM endpoints will error until one is set):

   ```bash
   # PowerShell:  $env:APP_ENV="development"; python wsgi.py
   APP_ENV=development python wsgi.py
   ```

**LLM.** STATlee uses Google Gemini — set `GEMINI_API_KEY` (required in
production). Model ids per role can be pinned with `MODEL_PRO` / `MODEL_FLASH` /
`MODEL_FLASH_LITE`. See `.env.example` for all variables.

## Development & Testing

```bash
# Install dev tooling (pytest + ruff, on top of the app requirements)
pip install -r requirements-dev.txt

# Lint
ruff check .

# Run the test suite (uses a fake LLM client — no API key or network needed)
pytest -q
```

The test suite injects a deterministic fake LLM service, so the entire HTTP surface (uploads, codebook, wrangling, run-guard, converse, export, auth, CSRF) is exercised offline. CI runs ruff + byte-compile + pytest on every push and PR (see `.github/workflows/ci.yml`). Optional local hooks are configured in `.pre-commit-config.yaml` (`pre-commit install`).

## Tech Stack

* **Frontend:** Vanilla JavaScript (modular `CC` namespace), HTML5, Tailwind CSS, CodeMirror, vendored/pinned `marked` + `DOMPurify`.
* **Backend:** Python, Flask (app factory + blueprints), Pandas, Flask-SQLAlchemy, Flask-Login, Flask-Limiter.
* **AI Integration:** Google GenAI SDK (`gemini-3.5-flash`, `gemini-3.1-flash-lite`).
* **Data formats:** pandas, openpyxl (Excel), pyreadstat (Stata/SPSS), pypdf + fpdf2 (codebooks).
* **Infrastructure:** Docker, Docker Compose, Gunicorn; SQLite (dev) / PostgreSQL (prod).

## Website

To try STATlee without local setup, visit the deployed version at: https://codecaster-th8m.onrender.com/

> The live URL still carries the old Render service name (`codecaster-th8m`); it
> will change once the Render service is renamed during the next deploy.
