# Credits & Attribution

STATlee is built on excellent open-source libraries and a commercial AI API.
Thank you to the maintainers of every project below. Each dependency is governed
by its own license; consult the linked project for full terms.

STATlee has a pluggable LLM provider (`LLM_PROVIDER`); Gemini is the default, and
Anthropic and OpenAI are optional. Only the selected provider's SDK is used at
runtime.

| Service | Use | Terms |
|---|---|---|
| **Google Gemini** (via the `google-genai` SDK) | Default provider: natural-language → code generation, moderation, and interpretation. | [Gemini API Additional Terms](https://ai.google.dev/gemini-api/terms) · [Prohibited Use Policy](https://ai.google.dev/gemini-api/terms#use-policy) |
| **Anthropic Claude** (via the `anthropic` SDK) | Optional provider (`LLM_PROVIDER=anthropic`). | [Anthropic Usage Policies](https://www.anthropic.com/legal/aup) |
| **OpenAI** (via the `openai` SDK) | Optional provider (`LLM_PROVIDER=openai`). | [OpenAI Usage Policies](https://openai.com/policies/usage-policies/) |

AI analysis is **powered by your configured provider** (Google Gemini by default).
STATlee's moderation gate enforces the provider's prohibited-use rules by blocking
malware/illegal requests.

## Backend (Python)

Direct dependencies, at the versions pinned in `requirements.txt` and
`requirements-postgres.txt` (`tests/test_credits.py` keeps this table and the
pins in step).

| Library | Version | Purpose | License |
|---|---|---|---|
| [Flask](https://flask.palletsprojects.com/) | 3.1.3 | Web framework (app factory + blueprints) | BSD-3-Clause |
| [Werkzeug](https://werkzeug.palletsprojects.com/) | 3.1.9 | WSGI utilities, security helpers, ProxyFix | BSD-3-Clause |
| [google-genai](https://github.com/googleapis/python-genai) | 2.31.0 | Gemini SDK (default provider) | Apache-2.0 |
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | 1.15.0 | Claude SDK (optional provider) | MIT |
| [openai](https://github.com/openai/openai-python) | 3.32.0 | OpenAI SDK (optional provider) | Apache-2.0 |
| [Flask-SQLAlchemy](https://flask-sqlalchemy.palletsprojects.com/) | 3.1.1 | ORM + models (on [SQLAlchemy](https://www.sqlalchemy.org/), MIT) | BSD-3-Clause |
| [Flask-Migrate](https://flask-migrate.readthedocs.io/) | 4.1.0 | Database schema migrations | MIT |
| [Alembic](https://alembic.sqlalchemy.org/) | 1.20.0 | Migration engine under Flask-Migrate | MIT |
| [Flask-Login](https://flask-login.readthedocs.io/) | 0.6.3 | Session/auth management | MIT |
| [Flask-Limiter](https://flask-limiter.readthedocs.io/) | 4.1.1 | Per-identity rate limiting | MIT |
| [filelock](https://github.com/tox-dev/filelock) | 4.2.0 | Cross-worker lock on the dataset version manifest | MIT |
| [pandas](https://pandas.pydata.org/) | 3.0.6 | Data loading and manipulation | BSD-3-Clause |
| [statsmodels](https://www.statsmodels.org/) | 0.15.0 | Statistical models | BSD-3-Clause |
| [matplotlib](https://matplotlib.org/) | 3.11.2 | Plotting | Matplotlib License (PSF-based) |
| [seaborn](https://seaborn.pydata.org/) | 0.13.2 | Statistical visualization | BSD-3-Clause |
| [openpyxl](https://openpyxl.readthedocs.io/) | 3.1.5 | Excel (`.xlsx`) ingestion | MIT |
| [xlrd](https://github.com/python-excel/xlrd) | 2.0.2 | Legacy Excel (`.xls`) ingestion | BSD-3-Clause |
| [redis-py](https://github.com/redis/redis-py) | 8.1.0 | Shared rate-limit store (`RATELIMIT_STORAGE_URI=redis://`) | MIT |
| [psycopg 3](https://www.psycopg.org/psycopg3/) | 3.3.6 | PostgreSQL driver (`requirements-postgres.txt`, production DB) | LGPL-3.0 |
| [pyreadstat](https://github.com/Roche/pyreadstat) | 1.3.6 | Stata (`.dta`) / SPSS (`.sav`) ingestion | Apache-2.0 |
| [pypdf](https://pypdf.readthedocs.io/) | 6.21.0 | PDF codebook reading | BSD-3-Clause |
| [fpdf2](https://py-pdf.github.io/fpdf2/) | 2.8.9 | TXT→PDF codebook conversion | LGPL-3.0 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | 1.2.4 | `.env` loading | BSD-3-Clause |
| [gunicorn](https://gunicorn.org/) | 26.2.0 | Production WSGI server | MIT |

psycopg and fpdf2 (LGPL-3.0) are used unmodified as separately installed
packages that a user can replace, which the LGPL permits under any license for
the calling code, including the Elastic License 2.0. No dependency is GPL or
AGPL. Notable transitive licenses: certifi (MPL-2.0), numpy (BSD-3-Clause with
bundled 0BSD/MIT/Zlib/CC0 parts), pillow (MIT-CMU), cryptography
(Apache-2.0 or BSD-3-Clause).

## Frontend (vendored & pinned)

| Library | Purpose | License |
|---|---|---|
| [Tailwind CSS](https://tailwindcss.com/) | Utility-first styling (built, purged) | MIT, (c) Tailwind Labs, Inc. |
| [CodeMirror 5](https://codemirror.net/5/) 5.65.21 (latest 5.x; the app's editor code targets the v5 API, v6 is a rewrite), incl. the Python and R modes and the material-darker theme (`statlee/static/vendor/codemirror.min.*`, `cm-*.min.*`, jsDelivr-minified from the npm package) | In-app code editor | MIT, (c) Marijn Haverbeke and others |
| [marked](https://marked.js.org/) 18.1.1 (`statlee/static/vendor/marked.min.js`, the package's `lib/marked.umd.js`) | Markdown rendering | MIT, (c) Christopher Jeffrey and MarkedJS |
| [DOMPurify](https://github.com/cure53/DOMPurify) 3.4.17 (`statlee/static/vendor/purify.min.js`) | HTML sanitization (XSS defense for all LLM output) | Apache-2.0 / MPL-2.0, (c) Cure53 and contributors |
| [Heroicons](https://heroicons.com/) v1 | Inline SVG icons in the templates and JS (some redrawn or trimmed) | MIT, (c) Tailwind Labs, Inc. |
| [Feather](https://feathericons.com/) | Inline SVG icons (shield, book) | MIT, (c) Cole Bemis |
| [Inter](https://rsms.me/inter/), [Syne](https://fonts.google.com/specimen/Syne), [JetBrains Mono](https://www.jetbrains.com/lp/mono/) | Typefaces, loaded from Google Fonts at runtime (not bundled) | SIL Open Font License 1.1 |

The STATlee logo (`statlee/static/images/`), the README screenshots in `docs/images/`, and the
synthetic sample dataset in `docs/examples/` are original to this project.

## Infrastructure

| Tool | Purpose |
|---|---|
| [Docker](https://www.docker.com/) / Docker Compose | Containerized app + sandbox runner |
| [Render](https://render.com/) | Reference host for a hosted deploy (not currently deployed) |
| [R](https://www.r-project.org/) with dplyr, ggplot2 and MASS (Debian `r-base`, `r-cran-*`) | R analyses in the Docker images, run as a separate `Rscript` process (R and MASS are GPL-2/GPL-3, dplyr and ggplot2 MIT; installed at image build, not linked into STATlee) |
| [pytest](https://pytest.org/) + [ruff](https://docs.astral.sh/ruff/) | Test runner + linter (dev) |
| [Playwright](https://playwright.dev/python/) | Browser end-to-end tests (dev/CI, Apache-2.0) |
| [pip-audit](https://github.com/pypa/pip-audit) | Dependency vulnerability audit (dev/CI) |

---

_If you believe an attribution is missing or incorrect, please open an issue._
