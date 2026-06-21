# Milestones — STATlee

The project's durable accomplishment log. **Append-only.** It's how the project remembers what it
has shipped across every autopilot cycle. Updated at the end of each cycle.

## Current state

STATlee is a Flask web app (the `statlee` package) that turns uploaded datasets
(CSV/TSV/Excel/Stata/SPSS) into statistical analysis via a Gemini backend: AI codebook +
suggestions, sandboxed Python/R execution, **conversational data cleaning** (chat-style transcript,
undo/redo, revert-to-original, runs on the cheap `lite` tier), moderation (default-deny), rate
limiting, optional accounts, and a billing seam with money-safety guardrails (all behind config
flags). **135 tests pass; `ruff` clean.** Not yet deployed — runs locally, by design; GitHub repo
at `Yibarek1/STATlee`. Hosting + pricing are documented (`docs/DEPLOYMENT_PLAYBOOK.md`,
`docs/PRICING.md`) but intentionally not live ($0 spend).

## Cycles (newest first)

### Cycle 1 — data-editing polish + money-safe hosting playbook — 2026-06-20 — branch `autopilot/data-polish-hosting` → fast-forward merged into `claude/hardcore-hellman-c0fb41` @ `55f7ec6` (local; not pushed)

- SP1 Health baseline — verified 124→ green baseline (tests, ruff, boot); wrangle flow already covered.
- SP2 Data-editing polish — `WRANGLE_ROLE=lite`; `revert_to_original` + `/revert_dataset`;
  chat-style changelog transcript with optimistic echo; **fixed** a latent bug where the cleaning
  panel was unreachable on first upload.
- SP3 Money-safety defaults — startup warning when billing is on without a spend ceiling; documented
  money-safe `.env.example` (low ceiling, WRANGLE_ROLE, MONEY SAFETY block).
- SP4 Deploy + pricing playbook — `docs/DEPLOYMENT_PLAYBOOK.md` + `docs/PRICING.md`; README/doc
  links + test badge refreshed.
- Net: +11 tests (124 → 135). Commits: scaffolding, feat(wrangle), feat(config), docs.
- Deferred / cut: actual deploy, payment (Stripe) wiring, main↔branch Anthropic divergence, Render
  rename (see BACKLOG.md). No live click-through of the UI (would spend Gemini credits).
