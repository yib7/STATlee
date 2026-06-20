# Milestones — STATlee

The project's durable accomplishment log. **Append-only.** It's how the project remembers what it
has shipped across every autopilot cycle. Updated at the end of each cycle.

## Current state

STATlee is a Flask web app (the `statlee` package) that turns uploaded datasets
(CSV/TSV/Excel/Stata/SPSS) into statistical analysis via a Gemini backend: AI codebook +
suggestions, sandboxed Python/R execution, conversational data wrangling with version control,
moderation (default-deny), rate limiting, optional accounts, and a billing seam (all behind config
flags). ~124 tests pass; `ruff` clean. Not yet deployed — runs locally; GitHub repo at
`Yibarek1/STATlee`.

## Cycles (newest first)

### Cycle 1 — data-editing polish + money-safe hosting playbook — 2026-06-20 — branch `autopilot/data-polish-hosting` → pending merge

- SP1 Health baseline — (in progress)
- SP2 Data-editing polish — (pending)
- SP3 Money-safety defaults — (pending)
- SP4 Deploy + pricing playbook — (pending)
- Deferred / cut: actual deploy, payment wiring, main↔branch Anthropic divergence (see BACKLOG.md)
