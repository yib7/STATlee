# Credits & Attribution

Statly is built on excellent open-source libraries and a commercial AI API.
Thank you to the maintainers of every project below. Each dependency is governed
by its own license; consult the linked project for full terms.

## AI / API

| Service | Use | Terms |
|---|---|---|
| **Google Gemini** (via the `google-genai` SDK) | All natural-language → code generation, moderation, and interpretation. | [Gemini API Additional Terms](https://ai.google.dev/gemini-api/terms) · [Prohibited Use Policy](https://ai.google.dev/gemini-api/terms#use-policy) |

AI analysis is **powered by Google Gemini**. Statly's moderation gate enforces
the Gemini Prohibited Use Policy by blocking malware/illegal requests.

## Backend (Python)

| Library | Purpose | License |
|---|---|---|
| [Flask](https://flask.palletsprojects.com/) | Web framework (app factory + blueprints) | BSD-3-Clause |
| [Werkzeug](https://werkzeug.palletsprojects.com/) | WSGI utilities, security helpers, ProxyFix | BSD-3-Clause |
| [Flask-SQLAlchemy](https://flask-sqlalchemy.palletsprojects.com/) / [SQLAlchemy](https://www.sqlalchemy.org/) | ORM + models | BSD / MIT |
| [Flask-Login](https://flask-login.readthedocs.io/) | Session/auth management | MIT |
| [Flask-Limiter](https://flask-limiter.readthedocs.io/) | Per-identity rate limiting | MIT |
| [pandas](https://pandas.pydata.org/) | Data loading and manipulation | BSD-3-Clause |
| [statsmodels](https://www.statsmodels.org/) | Statistical models | BSD-3-Clause |
| [matplotlib](https://matplotlib.org/) | Plotting | matplotlib (BSD-style) |
| [seaborn](https://seaborn.pydata.org/) | Statistical visualization | BSD-3-Clause |
| [openpyxl](https://openpyxl.readthedocs.io/) | Excel (`.xlsx`) ingestion | MIT |
| [pyreadstat](https://github.com/Roche/pyreadstat) | Stata (`.dta`) / SPSS (`.sav`) ingestion | Apache-2.0 |
| [pypdf](https://pypdf.readthedocs.io/) | PDF codebook reading | BSD-3-Clause |
| [fpdf2](https://py-pdf.github.io/fpdf2/) | TXT→PDF codebook conversion | LGPL-3.0 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | `.env` loading | BSD-3-Clause |
| [gunicorn](https://gunicorn.org/) | Production WSGI server | MIT |

## Frontend (vendored & pinned)

| Library | Purpose | License |
|---|---|---|
| [Tailwind CSS](https://tailwindcss.com/) | Utility-first styling (built, purged) | MIT |
| [CodeMirror 5](https://codemirror.net/5/) | In-app code editor | MIT |
| [marked](https://marked.js.org/) | Markdown rendering | MIT |
| [DOMPurify](https://github.com/cure53/DOMPurify) | HTML sanitization (XSS defense for all LLM output) | Apache-2.0 / MPL-2.0 |
| [Inter](https://rsms.me/inter/), [Syne](https://fonts.google.com/specimen/Syne), [JetBrains Mono](https://www.jetbrains.com/lp/mono/) | Typefaces (Google Fonts) | SIL Open Font License 1.1 |

## Infrastructure

| Tool | Purpose |
|---|---|
| [Docker](https://www.docker.com/) / Docker Compose | Containerized app + sandbox runner |
| [Render](https://render.com/) | Hosting for the live demo |
| [pytest](https://pytest.org/) + [ruff](https://docs.astral.sh/ruff/) | Test runner + linter (dev) |

---

_If you believe an attribution is missing or incorrect, please open an issue._
