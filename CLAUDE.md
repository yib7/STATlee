# STATlee

Flask web app (`statlee` package) that turns uploaded datasets into statistical analysis via a
Gemini backend. Tests: `python -m pytest -q`. Lint: `ruff check .`. Run: `APP_ENV=development
python wsgi.py` (needs `GEMINI_API_KEY` in `.env` for live LLM calls).

Autopilot runs: the autonomy contract is `.autopilot/AUTONOMY.md` — restate it in full to every
subagent. Current plan + resume point: `.autopilot/PLAN.md`.
