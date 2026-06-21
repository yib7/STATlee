# PLAN — STATlee UI polish (autopilot cycle 2)

> Autopilot single-plan. Resume = first unchecked box. Contract: `.autopilot/AUTONOMY.md`.
> Spec: `docs/superpowers/specs/2026-06-20-ui-polish-cycle2-design.md`.
> Branch: `autopilot/ui-polish-cycle2` (off `claude/hardcore-hellman-c0fb41` @ e8829fc).

**Goal:** Eight UI/UX polish items + a money-saving model swap, all verified by the
Python suite (markup assertions via the test client) + ruff + boot smoke.

**Tech:** Flask app-factory, vanilla-JS `CC` namespace, precompiled/purged Tailwind
(custom styling goes in `static/css/app.css`, NOT new Tailwind utilities), Gemini-only LLM.

## Global constraints
- No secrets, no real spend. Model swap + cost figures are config/display only.
- Tailwind is purged — new visual styling lands in `app.css`, reuse known classes in HTML.
- Keep `→`, box-drawing, math signs. Remove only decorative pictographs (UI + root README).
- Baseline before changes: **135 passed**, ruff clean.

## Scope (frozen)
SP1 model swap+prices · SP2 per-model cost tooltip · SP3 data-viewer zoom ·
SP4 on-demand suggestions · SP5 emoji scrub · SP6 bigger history popup ·
SP7 Report as a tab · SP8 compact codebook. Order chosen to minimize index.html churn
conflicts; emoji scrub (SP5) runs late as a final sweep.

---

## SP0 — Baseline + scaffolding
- [ ] Confirm baseline green: `python -m pytest -q` (135) + `ruff check .` clean + factory boots
- [ ] Cycle-2 scaffolding committed (spec, archived cycle-1 plan, this plan, DECISIONS/BACKLOG)

## SP1 — Model swap (3.1-pro → 3.5-flash) + price table
**Files:** `statlee/config.py`, `statlee/routes/misc.py` (index), `statlee/templates/index.html`
(CC_BOOT), `.env.example`, `README.md`, `docs/README.md`; test `tests/test_config.py`,
`tests/test_ui_markup.py`.
- [ ] Test: `cfg.model_pro == 'gemini-3.5-flash'`; `cfg.model_prices` has input/output for the
  three active models (pro/flash/lite); price values numeric & > 0
- [ ] Implement: `model_pro` default → `gemini-3.5-flash`; add `model_prices` dict (3.5-flash
  1.50/9.00, 3-flash-preview 0.50/3.00, 3.1-flash-lite-preview 0.25/1.50); helper to expose the
  active-model price map
- [ ] Implement: `index()` passes `prices=...`; `index.html` emits `window.CC_BOOT.prices = {{...}}`
- [ ] Update `.env.example` (`MODEL_PRO=gemini-3.5-flash`), README + docs/README model-id lines
- [ ] Test (markup): `GET /` HTML contains `CC_BOOT` `prices` with the three model ids + numbers
- [ ] **Checkpoint:** pytest green + ruff clean + boot. Commit `feat(config): swap pro→3.5-flash + price table`

## SP2 — Per-model usage threading + session-cost tooltip
**Files:** new `statlee/usage.py` (or helper in `routes/__init__`), `routes/analyze.py`,
`routes/converse.py`, `routes/datasets.py`, `routes/misc.py`, `static/js/api.js`; test
`tests/test_usage.py`, extend `tests/test_datasets_routes.py`.
- [ ] Test: `usage_breakdown({'model':'m1','input':10,'output':5}, {'model':'m1','input':1,'output':2},
  {'model':'m2','input':4,'output':0})` → totals input 15/output 7/calls 3 and
  `by_model == {'m1':{input:11,output:7,calls:2}, 'm2':{input:4,output:0,calls:1}}`
- [ ] Implement `usage_breakdown(*usages)`; replace `_sum_usage` + every client-facing `usage`
  emission to include `by_model` (analyze chat/run/interpret, converse, datasets classify/suggest/
  wrangle/method/extract, misc report)
- [ ] Test: `/suggest` (and `/classify_variables`) response `usage` includes `by_model`
- [ ] Frontend: `CC.addUsage` accumulates `state.usage.by_model`; `usageBadge` tooltip shows
  total tokens + `≈ $cost` (from `CC_BOOT.prices`) + per-model lines; add `CC.sessionCostUSD()`
- [ ] **Checkpoint:** pytest green + ruff + boot. Commit `feat(usage): per-model breakdown + session cost`

## SP3 — Data-viewer zoom
**Files:** `statlee/templates/index.html` (Data Viewer toolbar), `static/js/data.js` (or ui.js),
`static/css/app.css`; test `tests/test_ui_markup.py`.
- [ ] Test (markup): `GET /` contains `id="dataZoomIn"`, `id="dataZoomOut"`, `id="dataZoomReset"`
- [ ] Implement zoom controls scaling table font-size via a CSS var on `#tableScrollContainer`
  (range ~60%–180%, step 10%); Ctrl+wheel over the table also zooms; reset button shows %
- [ ] **Checkpoint:** pytest green + ruff + boot. Commit `feat(data): zoom controls on the data viewer`

## SP4 — On-demand suggestions when auto-suggest is OFF
**Files:** `statlee/templates/index.html` (suggestions panel), `static/js/data.js`; test
`tests/test_ui_markup.py`.
- [ ] Test (markup): `GET /` contains `id="suggestNowBtn"`
- [ ] Implement: when `autosuggest` pref is off, `runPostUploadPipeline` reveals the panel with a
  "Generate analysis ideas" button (id `suggestNowBtn`) instead of skipping; click → `fetchSuggestions`
- [ ] **Checkpoint:** pytest green + ruff + boot. Commit `feat(suggest): on-demand generate button`

## SP5 — Remove decorative emojis (UI + root README) — late sweep
**Files:** `statlee/templates/index.html`, `static/js/data.js`, `statlee/templates/landing.html`,
`README.md`; test `tests/test_no_emoji.py`.
- [ ] Test: guard scans `index.html`, `landing.html`, `static/js/*.js`, `README.md` for emoji
  pictographs (ranges U+1F000–1FAFF, U+2600–27BF dingbats/symbols, ⚡✦✓ specifically) → asserts none
- [ ] Implement: replace `⚡` (priority) with the existing bolt SVG or plain text; drop `✦`/`✓`
  decorative glyphs in data.js; strip the 6 header emojis in README + landing pictographs
- [ ] **Checkpoint:** guard test + full pytest green + ruff + boot. Commit `chore(ui): remove decorative emojis`

## SP6 — Bigger Analysis History popup
**Files:** `statlee/templates/index.html` (`#historyModal`); test `tests/test_ui_markup.py`.
- [ ] Test (markup): `GET /` history modal has `max-w-3xl` and `max-h-[85vh]`
- [ ] Implement: widen `#historyModal` inner panel (`max-w-xl`→`max-w-3xl`, `max-h-[80vh]`→`[85vh]`)
- [ ] **Checkpoint:** pytest green + ruff + boot. Commit `feat(history): larger history dialog`

## SP7 — Report as a top tab
**Files:** `statlee/templates/index.html` (tab bar + new `contentReport` pane, remove sidebar
`#reportBtn` + `#reportModal`), `static/js/ui.js` (VIEWS + paneBSelect), `static/js/tools.js`
(report wiring), test `tests/test_ui_markup.py`.
- [ ] Test (markup): `GET /` contains `id="tabReport"` + `id="contentReport"`; NOT `id="reportModal"`;
  NOT `id="reportBtn"`; `paneBSelect` has a `report` option
- [ ] Implement: add Report tab + pane (move builder markup out of the modal), `VIEWS` gains `Report`,
  `switchTab`/split handle it, remove sidebar button + modal, rewire `tools.js` listeners; pane shows
  a "run an analysis first" placeholder until `lastRun` exists
- [ ] **Checkpoint:** pytest green + ruff + boot; no orphaned `reportModal`/`reportBtn` refs in JS.
  Commit `feat(report): promote report builder to a workspace tab`

## SP8 — Compact codebook listing
**Files:** `static/js/ui.js` (`renderCodebookUI`), `static/css/app.css` if needed; test
read-verified (no stable markup hook — rendered client-side).
- [ ] Implement: render codebook as a responsive 2-column grid of dense chips (name + small tag),
  description on hover (`title`) rather than one tall column; container `#codebookList` gets a grid class
- [ ] **Checkpoint:** pytest green + ruff + boot; read-verify denser layout. Commit `feat(codebook): compact grid listing`

## SP9 — Finish
- [ ] Full suite green + ruff clean + boot (final verification, read actual output)
- [ ] `superpowers:finishing-a-development-branch`; update `.autopilot/MILESTONES.md`; present
  merge options (DO NOT merge — human gate). Notify user.

## Blocked (filled during the run)
- (none yet)
