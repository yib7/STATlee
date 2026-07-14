# Ship Checklist Plan — final.md Execution

**Branch:** `autopilot/ship-final` (isolated worktree)

**Scope:** Execute final.md shipping checklist all the way through. Mark each item pass/fail/N/A with evidence. Hold merge and publish until user consent.

---

## Phase 1 — Secrets & Safety [P0/P1]

**Checkpoint:** All P0 items confirmed clean, all P1 items confirmed clean or logged N/A.

- [ ] **P0** Gitleaks scan — zero hits in full history
- [ ] **P0** Rotate/revoke any secrets ever committed (none found → skip)
- [ ] **P0** `.gitignore` covers env, secrets, personal data, build artifacts
- [ ] **P0** No PII, real names, personal emails, account IDs in tracked files
- [ ] **P0** Sample/demo/seed data is synthetic — no real user data, PII, screenshots with usernames
- [ ] **P0** `.env.example` exists and documents every required env var
- [ ] **P0** Legal right to publish everything — bundled datasets, fonts, icons, images, third-party code
- [ ] **P1** Release license compatible with dependency licenses (Elastic 2.0 → MIT deps OK)
- [ ] **P1** No oversized or unintended files in repo or history (5 MB cutoff)

**Test command:** `gitleaks detect --verbose` (or fallback grep), `du -sh .git`, review `.gitignore`, spot-check README media paths

---

## Phase 2 — Code Quality [P0/P1/P2]

**Checkpoint:** Full clean run: tests pass, linter clean, build succeeds, no debug artifacts, no lingering TODOs.

- [ ] **P0** Test suite passes cleanly — `python -m pytest -q`
- [ ] **P1** Tests are hermetic and deterministic — no network, API keys, wall-clock time, timezone deps
- [ ] **P0** Runs correctly on every supported platform (Windows claimed → verify or update README)
- [ ] **P1** Line endings and text encoding normalized (`.gitattributes`, UTF-8 explicit)
- [ ] **P0** No performance issues — code reasonably efficient
- [ ] **P0** Project builds/compiles cleanly — `pip install -e .` (Python project) + `python -c "import statlee"` compile check
- [ ] **P0** No debug artifacts — stray `print()` / `console.log()`, hardcoded test values, large commented-out blocks
- [ ] **P1** Linter clean — `ruff check .`
- [ ] **P1** Dead code removed — unused imports, unreferenced files, orphaned functions
- [ ] **P1** No lingering `TODO` / `FIXME` in code
- [ ] **P1** No hardcoded environment-specific values — `localhost` URLs, dev ports, absolute paths, machine names
- [ ] **P1** Fails gracefully on obvious bad paths — missing config, absent input, malformed input, no network → clear error, not raw stack trace
- [ ] **P2** Test coverage feels honest — happy path + at least one edge case per major feature

**Test command:** `python -m pytest -q`, `ruff check .`, `python -c "import compileall; compileall.compile_dir('statlee')"`, manual grep for hardcoded paths/TODO

---

## Phase 3 — Security [P0/P1/P2]

**Checkpoint:** No secrets leaked to unintended services, security audit clean or findings resolved, dependency scan clean, auth/input validation confirmed.

- [ ] **P0** No secret or credential reaches any service other than its intended one
- [ ] **P1** `/security-review` or equivalent audit run — findings documented or resolved
- [ ] **P1** Dependency vulnerability scan — `pip-audit` — no unresolved high/critical (or documented reason deferred)
- [ ] **P1** All user inputs validated / sanitized at system boundary (file uploads, API params)
- [ ] **P1** Rate limits or abuse protection in place for web / API surface
- [ ] **P1** Auth routes can't be bypassed; protected endpoints reject unauthenticated requests
- [ ] **P1** Transport and headers hardened for hosted apps — HTTPS enforced, CORS scoped, cookies `Secure+HttpOnly`, CSP headers (N/A for CLI)
- [ ] **P1** No unexpected telemetry or phone-home
- [ ] **P2** Errors shown to users leak nothing sensitive — no stack traces, secrets, internal paths

**Test command:** `pip-audit`, manual review of `sandbox.py` + `routes/analyze.py` for injection/auth bypass, check `SECURITY.md` present

---

## Phase 4 — Organization [P1/P2]

**Checkpoint:** File/folder structure is clear and self-explanatory, no dead code, `.gitignore` justified, `docs/ARCHITECTURE.md` present and accurate.

- [ ] **P1** File and folder names consistent and self-explanatory
- [ ] **P1** Root directory not cluttered — related files grouped, unnecessary clutter removed
- [ ] **P1** Folder structure matches actual need — no single-file folders or empty placeholders
- [ ] **P1** Each module has one clear purpose — no God files
- [ ] **P1** No orphaned files that nothing references
- [ ] **P2** Every line in `.gitignore` still earns its place — prune stale patterns, add `# why` comments for non-obvious ones
- [ ] **P2** `docs/ARCHITECTURE.md` present — explains system in plain English for newcomer in <5 minutes

**Test command:** Manual inspection of file tree, `git grep` for references to potentially-dead files, read `docs/ARCHITECTURE.md`

---

## Phase 5 — Setup / Developer Experience [P0/P1/P2]

**Checkpoint:** README install + run steps accurate and complete on a fresh clone, setup minimal and clear, fresh-machine reproduction works end-to-end.

- [ ] **P0** README install + run steps accurate and complete — tested end-to-end on clean path
- [ ] **P0** Don't overwhelm — cut redundant/unpopular setup paths, streamlined recipe
- [ ] **P0** Setup is explicit, sequentially numbered steps — each one concrete action, no branching
- [ ] **P0** Fresh-machine reproduction — clone into brand-new dir, no pre-existing state, follow README only, install every declared dep from lockfile, run end-to-end
- [ ] **P0** README balances concise utility, welcoming tone, scannable visual styling
- [ ] **P0** All dependencies declared (`requirements.txt`, `setup.py`, etc.)
- [ ] **P1** Runtime version pinned (`.python-version` / `.nvmrc`) and lockfile committed (`poetry.lock` / `Pipfile.lock`)
- [ ] **P1** Setup single-command or clearly scripted — `pip install -e .` + `python wsgi.py`
- [ ] **P1** Setup friction minimized for audience — every step removable/automated/defaulted
- [ ] **P1** App user launches repeatedly has single entry point
- [ ] **P1** Platform-specific requirements noted (OS, runtime, external API keys)
- [ ] **P2** First run produces visible output or demo mode — not blank screen

**Test command:** Clone into temp dir, follow README end-to-end from scratch, confirm app boots and responds to a sample request

---

## Phase 6 — Dependency & Version Health [P1]

**Checkpoint:** All major packages on current stable release, no alpha/beta/RC unless necessary, all versions mutually compatible, AI models current and supported.

- [ ] **P1** Packages, runtimes, frameworks on current stable release — audit with `pip list --outdated`
- [ ] **P1** Newest doesn't mean unreleased — no alpha/beta/RC/nightly/`-dev` unless required (with reason noted)
- [ ] **P1** All versions mutually compatible — no conflicting peer-deps, no lib demanding older runtime, clean `pip install`
- [ ] **P1** AI models on current supported snapshot — not deprecated/sunset (Gemini/Anthropic/OpenAI)

**Test command:** `pip list --outdated`, `pip check`, inspect `requirements.txt`, verify model IDs in code

---

## Phase 7 — GitHub Presentation [P0/P1/P2]

**Checkpoint:** README has all five elements, repo description/topics set, CI badge passes, no leftover planning docs, sole authorship, media renders, diagrams render, license present, commit history clean, live demo URL (if deployed), no broken links.

- [ ] **P0** README has: project name, one-line description, screenshot + quality GIF (high-res, robust, <100MB), install/run steps, tech stack
- [ ] **P0** All README content represents current project state — no outdated features/explanations/GIFs
- [ ] **P0** GitHub repo description and topics set (not blank)
- [ ] **P0** README avoids long-winded paragraphs — short, digestible lines, less is more
- [ ] **P0** README is UP TO DATE — all information represents CURRENT STATE (CRITICAL)
- [ ] **P1** README frames project at right level — lead with engineering achievement, not "I made a thing for myself"
- [ ] **P1** CI workflow runs tests on every push, status badge in README
- [ ] **P1** `CREDITS.md` or attribution section if third-party code/templates/assets used
- [ ] **P1** No leftover planning docs, `PLAN.md`, AI session files, work-in-progress notes
- [ ] **P1** Sole authorship — only your name, no `Co-Authored-By:`, no trailers in commit history
- [ ] **P1** README and docs free of AI-generated writing patterns — no em-dashes as separators, no emojis, no hedging phrases
- [ ] **P1** README media renders on GitHub — repo-relative paths, committed files, no broken links
- [ ] **P2** README images/GIFs have meaningful alt text
- [ ] **P1** Diagrams render (mermaid blocks or committed PNG/SVG via repo-relative paths)
- [ ] **P2** License file present (MIT / Elastic 2.0 / GPL / etc.)
- [ ] **P2** Commit history presentable — no `asdf`, `wip`, `fix fix fix` messages
- [ ] **P2** Live demo URL in repo description and README (if deployed)
- [ ] **P2** No broken links in README or docs
- [ ] **P2** `SECURITY.md` present
- [ ] **P2** Release tagged — version tag, short CHANGELOG or GitHub Release

**Test command:** Clone to fresh dir, open README in browser, verify all paths resolve, check CI status, review git log, verify sole authorship with `git log --pretty=format:"%an"`

---

## Phase 8 — Web / UI Projects (Flask app applies)

**Checkpoint:** No browser console errors on main flows, mobile layout holds, UI visually appealing, basic a11y present.

- [ ] **P0** No errors in browser console on main user flows
- [ ] **P1** Layout holds up on mobile / narrow viewports
- [ ] **P1** UI is visually appealing — interactions smooth/responsive, stylistic choices deliberate/tasteful/coherent
- [ ] **P2** Basic accessibility — images have alt text, forms have labels, text contrast legible

**Test command:** Boot dev server, drive main flow in browser, open DevTools console, no errors; resize to mobile (375px), check layout; inspect a11y attributes

---

## Phase 9 — Final Gate [P0/P1]

**Checkpoint:** Full clean run of entire stack in order, every claim backed by fresh evidence, primary user journey works on clean clone, all boxes checked/N/A/deferred with logged issue.

- [ ] **P0** Full clean run in order: secrets scan → tests → build → lint — all pass, no suppressed warnings
- [ ] **P0** Every claim above backed by fresh output actually seen — not memory, not "should still pass"
- [ ] **P0** Primary user journey works end-to-end on clean clone
- [ ] **P0** Repo stays PRIVATE until every section checked/N/A (do NOT flip to public yet)
- [ ] **P0** Every box — P0, P1, P2 — is checked, marked N/A, or (P2 only) deferred to logged tracked issue
- [ ] **P1** Read README one final time as if never seen before — does it land?
- [ ] **P1** Branch hygiene — default branch correct, no stale half-merged branches, nothing public points at broken commit

**Test command:** Run full battery fresh (secrets → tests → build → lint), clone to fresh machine, run full workflow, read README cold, verify git state

---

## Summary

| Phase | Status | Evidence |
|-------|--------|----------|
| 1. Secrets & Safety | ⬜ | |
| 2. Code Quality | ⬜ | |
| 3. Security | ⬜ | |
| 4. Organization | ⬜ | |
| 5. Setup / DX | ⬜ | |
| 6. Dependency Health | ⬜ | |
| 7. GitHub Presentation | ⬜ | |
| 8. Web / UI | ⬜ | |
| 9. Final Gate | ⬜ | |

**Final decision:** Hold merge to `main` and publish until user consent.
