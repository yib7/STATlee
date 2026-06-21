# Design — STATlee UI polish (autopilot cycle 2)

Date: 2026-06-20 · Branch: `autopilot/ui-polish-cycle2`

Frozen scope for a batch of UI/UX polish + a model/cost change. Eight small,
independent units. No secrets, no real spend (the model swap and cost display
change config and on-screen text only; no API call is made during this work).

## Decisions taken (user, batched)

1. **Emoji scrub scope:** product UI + root `README.md` only. Leave internal
   `/docs` planning files (their `✓/✕/✅/❌` status marks) untouched. Functional
   typography (`→` arrows, file-tree box-drawing, math signs `≈ ≤ ≥`) stays
   everywhere — it is "required", not slop.
2. **Report:** promote to a top tab; **remove** the left-sidebar "Report" button.
3. **Cost prices:** use current real Gemini prices (web-verified below), seeded
   as defaults and overridable via config. Display-only — never spends money.
   Plus: **swap the `pro` model from `gemini-3.1-pro-preview` → `gemini-3.5-flash`**
   (cheaper, faster, near-parity per the user).

## Web-verified Gemini pricing (paid tier, USD per 1M tokens, Jun 2026)

| Role (app) | Model ID | Input | Output |
|---|---|---:|---:|
| `pro` / `draft` | `gemini-3.5-flash` (was `gemini-3.1-pro-preview`) | 1.50 | 9.00 |
| `flash` | `gemini-3-flash-preview` | 0.50 | 3.00 |
| `lite` | `gemini-3.1-flash-lite-preview` | 0.25 | 1.50 |

Source: ai.google.dev/gemini-api/docs/pricing (and corroborating trackers).
Swap keeps tier ordering coherent — `pro` stays the priciest/highest tier.

## Units of work

### SP1 — Model swap + price table (config)
- `config.py`: `model_pro` default → `gemini-3.5-flash`. Add `model_prices`:
  `{ model_id: {"input": usd_per_1M, "output": usd_per_1M} }` with the three
  rows above as defaults; overridable (kept simple — defaults in code).
- `routes/misc.py index()`: inject a small `prices` map (active pro/flash/lite
  models → price) into the page so the client can compute cost. Surface via
  `window.CC_BOOT.prices`.
- Update `.env.example`, `README.md`, `docs/README.md` model-id references.
- **Checkpoint/tests:** `cfg.model_pro == 'gemini-3.5-flash'`; `model_prices`
  has entries for all three active models; `GET /` HTML contains the injected
  `CC_BOOT.prices` with numeric values.

### SP2 — Per-model usage threading + session-cost tooltip
- Backend helper `usage_breakdown(*usages)` → `{input, output, calls, by_model:
  {model: {input, output, calls}}}` built from per-call usage dicts (each
  already carries `model`). Replace every client-facing usage emission
  (analyze/converse/datasets/misc) so payloads include `by_model`.
- Frontend `CC.addUsage`: accumulate `by_model`; `usageBadge` tooltip shows
  total tokens + **≈ $session cost** + per-model line items. Cost = Σ
  (in/1e6·price.input + out/1e6·price.output) using `CC_BOOT.prices`.
- **Checkpoint/tests:** helper unit tests (Python); a route response (e.g.
  `/suggest`) includes `usage.by_model`; tooltip wiring present (read-verified).

### SP3 — Data-viewer zoom
- Zoom controls (− / reset% / +) in the Data Viewer toolbar; scale table
  font-size (≈60%–180%); Ctrl+wheel over the table also zooms. CSS var on the
  table container.
- **Checkpoint:** `GET /` contains the zoom control ids; boots; suite green.

### SP4 — On-demand suggestions when auto-suggest is OFF
- When `autosuggest` pref is off, after upload show the Suggested-Analysis panel
  with a single "Generate analysis ideas" button (instead of silently skipping),
  so the user need not re-upload. Clicking calls the existing `fetchSuggestions`.
- **Checkpoint:** `GET /` contains the on-demand button id; `/suggest` still
  green; read-verified wiring.

### SP5 — Remove decorative emojis (UI + root README)
- Strip pictographs from `index.html` (`⚡`), `static/js/data.js` (`✦`, `✓`),
  `templates/landing.html` (`💬 🧭 🔒 📖 📂 ⚡ ✓`), and root `README.md`
  header emojis. Replace with nothing or an existing inline SVG where the glyph
  carried meaning (e.g. the priority "⚡"). Keep `→`, box-drawing, math.
- **Checkpoint/test:** a guard test scans the user-facing templates + JS and
  asserts zero emoji pictographs remain.

### SP6 — Bigger Analysis History popup
- `historyModal`: `max-w-xl → max-w-3xl`, `max-h-[80vh] → max-h-[85vh]`.
- **Checkpoint:** `GET /` shows the widened classes.

### SP7 — Report as a top tab
- Add `tabReport` to the tab bar and a `contentReport` pane holding the report
  builder UI (moved out of `#reportModal`). Add `Report` to `VIEWS` and the
  split-pane `paneBSelect`. Remove the sidebar `#reportBtn` and `#reportModal`.
  Until a run exists, the pane shows a "run an analysis first" placeholder.
- **Checkpoint:** `GET /` contains `id="tabReport"` + `id="contentReport"`, no
  `id="reportModal"`, no sidebar `id="reportBtn"`; boots; suite green.

### SP8 — Compact codebook listing
- `renderCodebookUI`: render variables as a responsive 2-column grid of dense
  chips (name + small classification tag), description on hover (title), instead
  of one tall single-column list.
- **Checkpoint:** `GET /` unaffected structurally; read-verified denser render;
  boots.

## Testing approach

Python `pytest` is the only harness (no JS runner). Observable verification:
- Unit tests: config (SP1), `usage_breakdown` + route `by_model` (SP2),
  emoji-guard (SP5).
- Markup tests: `GET /` via the test client and assert the structural changes
  for SP3/SP4/SP6/SP7/SP8 (real, observable output).
- Each phase: full `pytest` green + `ruff` clean + app-factory boot smoke.
- Pure-frontend logic (zoom scaling, cost math, tab switching) is read-verified
  and boot-smoke-verified; CC_BOOT/markup assertions cover the seams.

## Non-goals
- No deploy, no real API calls, no Stripe, no repo/Render rename (backlog).
- `flash`/`lite` model IDs unchanged (only `pro` swapped, per the instruction).
