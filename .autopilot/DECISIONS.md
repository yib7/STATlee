# Decisions — assumptions & reversible calls made on autopilot

Format: `[date] <phase> — <decision/question> — <why> — <how to undo>`

## Open (need your answer)
- (none)

## Resolved
- [2026-06-20] setup — Hosting = **GitHub-only + deploy playbook** (user choice) — keep money risk
  at $0; deploy nothing now — **how to undo:** follow `docs/DEPLOYMENT_PLAYBOOK.md` later.
- [2026-06-20] setup — Pricing = **resume-showcase only** (user choice); still set the *free*
  operator spend-ceiling guardrails — safety costs nothing — **how to undo:** edit `docs/PRICING.md`.
- [2026-06-20] setup — Data editing = **polish the existing wrangle feature** (flash-lite + revert
  to original + chat feel) rather than rebuild — feature already exists (5.16/4.6) — **how to undo:**
  revert SP2 commits.
- [2026-06-20] setup — New cycle on a fresh branch `autopilot/data-polish-hosting` forked from the
  current Gemini-only HEAD, NOT from `origin/main` — main still carries the removed Anthropic
  multi-provider path; forking HEAD keeps the live direction — **how to undo:** delete the branch.
- [2026-06-20] SP1 — Did NOT add a new consolidated smoke test — the upload→wrangle→undo→redo→reset
  path is already covered by existing route tests; a duplicate adds maintenance with no coverage —
  **how to undo:** add an end-to-end test if desired.
- [2026-06-20] SP2 — Implemented revert as a forward "copy v1 to a new version" rather than a
  pointer jump to v1 — keeps the revert itself one-Undo recoverable, matching the user's "undo/redo
  for when things go wrong" ask — **how to undo:** change `storage.revert_to_original`.
- [2026-06-20] SP2 — Fixed a latent bug: the Data Cleaning panel (with the wrangle input) was
  hidden until a version change, so the FIRST edit was impossible. `/upload` now returns the v1
  changelog and the panel renders on upload — **how to undo:** revert the upload-response + data.js
  change.
- [2026-06-20] SP2 — Put new styling in `app.css` + reused existing Tailwind classes instead of new
  utilities — Tailwind here is precompiled/purged, so new utility classes wouldn't render and a
  rebuild needs node/tooling I shouldn't assume — **how to undo:** rebuild Tailwind and inline.
- [2026-06-20] SP2 — Did NOT boot the live app to click the UI — wrangle makes real Gemini calls
  (= spending money), a hard stop; backend is covered by tests w/ a fake LLM, frontend by syntax
  check + review — **how to undo:** user runs locally with their own GEMINI_API_KEY.
- [2026-06-20] setup — Running phases **inline** (not via subagents) — the environment's Agent tool
  carries a "don't spawn unless asked" guard and the scope is modest/well-understood; inline is the
  sanctioned slower-but-correct fallback — **how to undo:** n/a (execution strategy only).
