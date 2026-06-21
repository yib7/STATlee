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
- [x] Confirm baseline green: 135 passed + ruff clean + factory boots (31 routes)
- [x] Cycle-2 scaffolding committed (spec, archived cycle-1 plan, this plan, DECISIONS) @ 3c4986b

## SP1 — Model swap (3.1-pro → 3.5-flash) + price table
**Files:** `statlee/config.py`, `statlee/routes/misc.py` (index), `statlee/templates/index.html`
(CC_BOOT), `.env.example`, `README.md`, `docs/README.md`; test `tests/test_config.py`,
`tests/test_ui_markup.py`.
- [x] Test: `cfg.model_pro == 'gemini-3.5-flash'`; `active_model_prices()` covers all three models
- [x] Implement: `model_pro` default → `gemini-3.5-flash`; `model_prices` dict + `active_model_prices()`
- [x] Implement: `index()` passes `model_prices`; `index.html` emits `window.CC_BOOT.prices`
- [x] Update `.env.example` (`MODEL_PRO=gemini-3.5-flash`), docs/README model-id line
- [x] Test (markup): `GET /` contains `CC_BOOT` `prices` with the three model ids
- [x] **Checkpoint:** 138 passed + ruff clean + boot. Commit `feat(config): swap pro->3.5-flash + price table`

## SP2 — Per-model usage threading + session-cost tooltip
**Files:** new `statlee/usage.py` (or helper in `routes/__init__`), `routes/analyze.py`,
`routes/converse.py`, `routes/datasets.py`, `routes/misc.py`, `static/js/api.js`; test
`tests/test_usage.py`, extend `tests/test_datasets_routes.py`.
- [x] Test: `usage_breakdown` totals + by_model (+ empty + missing-model cases) — `tests/test_usage.py`
- [x] Implement `statlee/usage.py`; `_sum_usage` delegates; wrapped every client-facing `usage`
  emission (analyze chat/interpret/method, converse, datasets classify/suggest/extract/**wrangle**, misc report)
- [x] Test: `/suggest` (reroll) + `/wrangle` responses include `usage.by_model` — `tests/test_datasets_routes.py`
- [x] Frontend: `CC.addUsage` accumulates `by_model`; `CC.sessionCostUSD()`; tooltip shows tokens +
  `≈ $cost` + per-model lines (`CC_BOOT.prices`); wrangle handler now records usage
- [x] **Checkpoint:** 143 passed + ruff + boot + JS syntax OK. Commit `feat(usage): per-model breakdown + session cost`

## SP3 — Data-viewer zoom
**Files:** `statlee/templates/index.html` (Data Viewer toolbar), `static/js/data.js` (or ui.js),
`static/css/app.css`; test `tests/test_ui_markup.py`.
- [x] Test (markup): `GET /` contains the three zoom-control ids
- [x] Implement zoom toolbar in the Data Viewer card; CSS `zoom` on `#dataTable` (0.6–1.8, step 0.1);
  Ctrl+wheel zooms, plain wheel still scrolls; reset shows %; `.data-zoom-btn` in app.css
- [x] **Checkpoint:** 144 passed + ruff + JS OK. Commit `feat(data): zoom controls on the data viewer`

## SP4 — On-demand suggestions when auto-suggest is OFF
**Files:** `statlee/templates/index.html` (suggestions panel), `static/js/data.js`; test
`tests/test_ui_markup.py`.
- [x] Test (markup): `GET /` contains `id="suggestNowBtn"`
- [x] Implement: off-branch reveals the panel + `suggestNowBtn`; click → `fetchSuggestions`; the
  button hides once a fetch starts (reroll in header still works)
- [x] **Checkpoint:** 145 passed + ruff + JS OK. Commit `feat(suggest): on-demand generate button`

## SP5 — Remove decorative emojis (UI + root README) — late sweep
**Files:** `statlee/templates/index.html`, `static/js/data.js`, `statlee/templates/landing.html`,
`README.md`; test `tests/test_no_emoji.py`.
- [x] Test: `tests/test_no_emoji.py` guards index/landing/all JS/README; allows →, box-drawing, math
- [x] Implement: `⚡`→bolt SVG (index priority + landing eyebrow/card); `✦`→bolt SVG, `✓`→check SVG
  (data.js); landing 5 card emojis→SVG icons; price `✓`→pure-CSS check; README 7 header emojis stripped
- [x] **Checkpoint:** guard green + 149 passed + ruff + boot. Commit `chore(ui): remove decorative emojis`

## SP6 — Bigger Analysis History popup
**Files:** `statlee/templates/index.html` (`#historyModal`); test `tests/test_ui_markup.py`.
- [x] Test (markup): history modal panel has `max-w-3xl` and `max-h-[85vh]`
- [x] Implement: widened `#historyModal` panel (`max-w-xl`→`max-w-3xl`, `max-h-[80vh]`→`[85vh]`)
- [x] **Checkpoint:** 146 passed + ruff. Commit `feat(history): larger history dialog`

## SP7 — Report as a top tab
**Files:** `statlee/templates/index.html` (tab bar + new `contentReport` pane, remove sidebar
`#reportBtn` + `#reportModal`), `static/js/ui.js` (VIEWS + paneBSelect), `static/js/tools.js`
(report wiring), test `tests/test_ui_markup.py`.
- [x] Test (markup): `tabReport` + `contentReport` present; `reportModal`/`reportBtn` gone; `value="report"`
- [x] Implement: Report tab + `contentReport` pane (builder markup moved out of modal); `VIEWS` gains
  `Report`; split selector gains `report`; removed sidebar button + modal; dropped dead `tools.js`
  listener; pane header hints "run an analysis first"
- [x] **Checkpoint:** 148 passed + ruff + boot; zero orphan `reportModal`/`reportBtn` refs. Commit done

## SP8 — Compact codebook listing
**Files:** `static/js/ui.js` (`renderCodebookUI`), `static/css/app.css` if needed; test
read-verified (no stable markup hook — rendered client-side).
- [x] Implement: `#codebookList` → `codebook-grid` (responsive `auto-fill` grid); dense chips
  (name + abbreviated tag), description on hover (`title`); `.codebook-grid` in app.css
- [x] Test (markup): `#codebookList` carries `codebook-grid`
- [x] **Checkpoint:** 147 passed + ruff + JS OK. Commit `feat(codebook): compact grid listing`

## SP9 — Finish
- [x] Final verification: **149 passed** + ruff clean + boot (31 routes) + all 6 JS files valid
- [x] `superpowers:finishing-a-development-branch`; `.autopilot/MILESTONES.md` updated; merge options
  presented (NOT merged — human gate).

## Blocked (filled during the run)
- (none yet)
