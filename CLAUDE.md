# STATlee

Flask web app (`statlee` package) that turns uploaded datasets into statistical analysis via a
pluggable LLM backend (Gemini default; Anthropic/OpenAI optional via `LLM_PROVIDER`). Tests:
`python -m pytest -q`. Lint: `ruff check .`. Run: `APP_ENV=development python wsgi.py` (needs the
selected provider's API key in `.env` for live LLM calls).

Architecture: `docs/ARCHITECTURE.md`. Security model: `docs/SECURITY_AUDIT.md`.
