# PLAN — STATlee: data-editing polish + money-safe hosting playbook

> On autopilot. Resume point = the first unchecked box below. Isolated on branch
> `autopilot/data-polish-hosting` (forked from the Gemini-only HEAD, never `main`).
> Autonomy contract: `.autopilot/AUTONOMY.md`. New ideas → `.autopilot/BACKLOG.md`.
> Assumptions / reversible decisions → `.autopilot/DECISIONS.md`. Shipped history →
> `.autopilot/MILESTONES.md`.

## Scope (frozen)

Three things: (1) verify the app runs smoothly; (2) **polish the already-built conversational
data-cleaning feature** (wrangle/undo/redo/reset) — route it through `gemini-3.1-flash-lite`, add a
"revert dataset to original upload" control distinct from the full-session reset, and make the box
feel like a back-and-forth mini-chat; (3) make the app **money-safe to host later for free** —
set zero-cost guardrail defaults and write a GitHub-only deploy + resume-showcase pricing playbook.

**OUT of scope:** actually deploying anything, creating hosting/payment accounts, wiring a real
payment processor, spending any money, merging to `main`, and resolving the main↔branch Anthropic
multi-provider divergence (left as a separate human decision).

## SP1 — Health baseline (task 1: "everything working smoothly")

**Checkpoint (observable "done"):** `python -m pytest -q` green, `ruff check .` clean, app factory
boots, and the wrangle→undo→redo→revert→reset flow works at the route level.

- [x] Re-run full suite + ruff + app-boot on this branch — **124 passed**, ruff clean, factory boots (30 routes)
- [x] Add/confirm a route-level smoke test exercising upload→wrangle→undo→redo→reset — already covered (test_wrangle_creates_new_version, test_version_control_undo_redo_over_http, test_reset_clears_workspace)
- [x] Tests: full `pytest` green + ruff clean

## SP2 — Data-editing polish (task 3: "Polish what's there")

**Checkpoint:** new tests green + full suite green + ruff clean; wrangle runs on the configured
lite role; a "revert to original" route restores v1; changelog renders as a chat-style transcript.

- [x] Add tunable `wrangle_role` config (default `lite`) and route `/wrangle` through it
- [x] Backend: `storage.revert_to_original` (copies v1 as a new undo-able version) + dedicated
      `/revert_dataset` route; history intact
- [x] Frontend: "Original" (revert) button wired to `/revert_dataset`, with confirm + undo-able
- [x] Frontend: changelog now renders as a chat transcript (user instruction bubble → applied
      summary bubble per turn) with optimistic "Applying…" echo; undo/redo/revert in the header
- [x] **Bonus fix:** Data Cleaning panel was unreachable on first upload (hidden until a version
      change) — `/upload` now returns the v1 changelog and the panel renders immediately
- [x] Tests: `wrangle_role` (3), revert-to-original storage+route (3), wrangle-uses-lite (1),
      upload-returns-changelog (1) — **132 passed**, ruff clean, data.js syntax OK

## SP3 — Money-safety defaults (task 2, the safe/free part)

**Checkpoint:** config grows a guardrail warning when billing is on without a spend ceiling in
production; `.env.example` documents the money-safe settings; full suite green.

- [x] `Config.validate()` warns if `billing_enabled` and `monthly_priority_call_ceiling<=0` in prod
- [x] Document money-safe env defaults in `.env.example` (MONEY SAFETY block, low ceiling=200,
      WRANGLE_ROLE added)
- [x] Tests: 3 config warning tests — **135 passed**, ruff clean

## SP4 — Deploy + pricing playbook (task 2: docs only, $0)

**Checkpoint:** `docs/DEPLOYMENT_PLAYBOOK.md` + `docs/PRICING.md` exist and are internally
consistent; README links to them and to the data-cleaning feature.

- [ ] `docs/DEPLOYMENT_PLAYBOOK.md` — GitHub-only-now stance + step-by-step "when ready to deploy"
      free-tier path with the exact money-safety checklist (caps, env vars, what NEVER to enable)
- [ ] `docs/PRICING.md` — resume-showcase pricing tiers + how the existing billing seam backs them
- [ ] Update `README.md` to reference both docs and the conversational data-cleaning feature
- [ ] Tests: docs render / internal links resolve (manual check), full suite still green

## Blocked (filled in during the run)

- (none yet)
