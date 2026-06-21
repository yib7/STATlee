# Milestones — STATlee

The project's durable accomplishment log. **Append-only.** It's how the project remembers what it
has shipped across every autopilot cycle. Updated at the end of each cycle.

## Current state

STATlee is a Flask web app (the `statlee` package) that turns uploaded datasets
(CSV/TSV/Excel/Stata/SPSS) into statistical analysis via a Gemini backend: AI codebook +
suggestions, sandboxed Python/R execution, **conversational data cleaning** (chat-style transcript,
undo/redo, revert-to-original, runs on the cheap `lite` tier), moderation (default-deny), rate
limiting, optional accounts, and a billing seam with money-safety guardrails (all behind config
flags). The workspace exposes Source Code / Data Viewer (with zoom) / Analysis Results / Converse /
**Report** / Codebook tabs; the usage badge shows an estimated **per-model session cost**. Premium
tier runs on `gemini-3.5-flash` (pro/draft); flash/lite unchanged. **149 tests pass; `ruff` clean.**
Not yet deployed — runs locally, by design; GitHub repo at `Yibarek1/STATlee`. Hosting + pricing are
documented (`docs/DEPLOYMENT_PLAYBOOK.md`, `docs/PRICING.md`) but intentionally not live ($0 spend).

## Cycles (newest first)

### Cycle 2 — UI polish + model/cost changes — 2026-06-20 — branch `autopilot/ui-polish-cycle2` (off `claude/hardcore-hellman-c0fb41` @ e8829fc; local, NOT merged — awaiting human gate)

- SP1 Model swap + price table — `pro`/`draft` now `gemini-3.5-flash` (was 3.1-pro; cheaper/faster);
  `config.model_prices` (web-verified Gemini paid-tier rates) + `active_model_prices()`; injected to
  the client via `CC_BOOT.prices`. Display-only — no spend.
- SP2 Per-model usage + session cost — shared `statlee/usage.py:usage_breakdown` threads a `by_model`
  split through every client usage payload (incl. wrangle, newly reported); usage-badge tooltip now
  shows tokens + ≈ session $ + per-model lines (`CC.sessionCostUSD`).
- SP3 Data-viewer zoom — −/100%/+ controls + Ctrl+wheel (CSS `zoom` on the table, 0.6–1.8).
- SP4 On-demand suggestions — when auto-suggest is off, a "Generate analysis ideas" button appears
  (no re-upload needed).
- SP5 Emoji scrub — removed decorative pictographs from index/landing/JS + root README (SVG/CSS
  replacements); `tests/test_no_emoji.py` guards it. Functional →/trees/math kept (user choice).
- SP6 Bigger history dialog — `max-w-xl/80vh` → `max-w-3xl/85vh`.
- SP7 Report as a tab — report builder moved from a modal to a top workspace tab (+ split-pane
  option); sidebar "Report" button + modal removed (user choice).
- SP8 Compact codebook — responsive `auto-fill` grid of dense chips; descriptions on hover.
- Net: +14 tests (135 → 149). Commits: scaffolding, feat(config), feat(usage), feat(data),
  feat(suggest), feat(history), feat(codebook), feat(report), chore(ui emoji).
- Deferred / cut: no deploy, no live click-test (would spend Gemini credits); `flash`/`lite` model
  ids unchanged; main↔branch divergence + Render rename still in BACKLOG.


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
