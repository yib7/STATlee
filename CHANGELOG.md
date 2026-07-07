# Changelog

All notable changes to STATlee are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Follow-up correctness and hardening cycle. Closes a second audit pass with one
P0 sandbox-safety fix and a set of P1/P2 fixes across the wrangle, billing,
config, auth, and chat layers. No breaking changes for end users. The test suite
grew from 216 to 229.

### Security
- The conversational data-wrangling path now run-guards the LLM-generated
  transform code through the moderation gate before it executes, closing a gap
  where wrangle transforms bypassed the check that analysis runs already had.

### Fixed
- `/chat` refunds the debited credit when a code-generation stream fails, so a
  failed request no longer charges the account (applies when billing is enabled).
- `/verify_email` is now rate-limited, matching the other token-consuming auth
  endpoints.
- Config emits a startup warning when `TRUST_PROXY_HOPS=0` in production, where
  the app is typically fronted by a proxy and rate limiting would otherwise key
  on the proxy IP.
- The chat pipeline surfaces the feature-selection fallback as an explicit SSE
  phase event instead of failing silently on wide datasets.

### Changed
- Gemini stream usage extraction is now explicit rather than relying on implicit
  attribute access, and the case-insensitive moderation-verdict parsing is
  documented as intentional.

### Tests
- Added skip-marked real-Docker sandbox integration tests (run when a Docker
  daemon and the `statlee-runner` image are available). Suite: 229 passing, 2
  skipped without Docker.

## [1.1.0] - 2026-07-02

Correctness and hardening release. A full-codebase audit closed 16 issues across
rate limiting, the LLM and sandbox layer, storage integrity, input validation, and
billing. No breaking changes for end users. The test suite grew from 178 to 216.

### Added
- `RATE_LIMIT_DEFAULT` is now enforced as a global fallback limit. It was parsed
  from the environment but never applied, so endpoints without an explicit limit
  (including `/login` and `/register`) were unthrottled.
- `EXEC_OUTPUT_LIMIT` is now read from the environment, and the data-wrangling
  sandbox applies the same output cap as analysis runs.
- `xlrd` dependency so `.xls` uploads work; the format was advertised but failed
  on every deployment.

### Fixed
- `.xls` uploads now succeed, and the dependency error names the correct engine.
- The Gemini client now has an HTTP timeout, so a stalled upstream call can no
  longer pin a worker thread until the whole worker is killed.
- The Docker execution sandbox terminates a timed-out container instead of leaving
  it running with its full memory and CPU allowance.
- Version-history, metadata, and approved-script files are written atomically, so a
  crash mid-write can no longer silently reset a dataset's cleaning history.
- Anonymous-file TTL cleanup now runs on ordinary traffic (not only on upload) and
  removes empty leftover subdirectories.
- Malformed JSON request bodies on several endpoints now return a clear 400 instead
  of a 500.
- Duplicate registration returns 409 instead of 500; a failed verification email
  now reports a distinct message instead of silently locking the account out.
- Per-account history fields are length-capped, and the history modal no longer
  shows the same run twice.

### Security
- Credit debiting is now a single atomic operation and runs only after the
  moderation gate, preventing double-spend or negative balances and charges for
  blocked requests (applies when billing is enabled).
- The legacy master-password comparison is now constant-time.

### Removed
- The unused, write-only `Dataset` table (it was never read back). On a pre-existing
  database the now-unreferenced `datasets` table remains but is harmless; it is
  simply no longer created or written.

## [1.0.1] - 2026-06-28

Maintenance and presentation release. The application's behavior is unchanged
from 1.0.0; this batches the README demo, a sample dataset, and a CI tooling bump.

### Added
- Animated end-to-end demo GIF in the README (upload, plain-English request,
  generated code, chart, and written report).
- `docs/examples/sample_survey.csv`, a synthetic sample dataset so a new user
  gets a first result without supplying their own data.

### Changed
- CI bumped to `actions/checkout@v5` and `actions/setup-python@v6`, clearing the
  GitHub Node 20 runtime deprecation. Test matrix (3.11 / 3.12) and steps are
  unchanged.

## [1.0.0] - 2026-06-23

First public release. STATlee turns a plain-English request over an uploaded
dataset into generated, moderated, sandboxed, and explained statistics.

### Analysis pipeline
- Natural-language request to runnable Python/R, with a default-deny moderation
  gate, optional feature-selection pass for wide datasets, and a debugging
  assistant when a run fails.
- Intelligent codebook that classifies each variable (nominal / ordinal /
  continuous) so the model will not run an invalid model on a categorical
  outcome. Codebooks can be inferred from a PDF data dictionary or a survey
  questionnaire.
- Multi-format ingestion: CSV, TSV, Excel (`.xlsx`/`.xls`), Stata (`.dta`), and
  SPSS (`.sav`), with native value labels seeding the codebook.
- Conversational data wrangling with a chat transcript, undo/redo version
  history, and one-click revert to the original upload.
- AI report builder grounded in real run outputs, plus one-click project export
  (data, script, plots, report).

### LLM layer
- Pluggable provider chosen with `LLM_PROVIDER`: Gemini (default), Anthropic, or
  OpenAI. Every call routes through one role-based service (`pro` / `flash` /
  `lite` / `draft`), so switching provider or model is a config change.
- Pro mode toggle routes code generation to a larger model on demand.
- Per-request token usage and an estimated per-model session cost surfaced live
  in the UI.

### Security and isolation
- Execution sandbox: throwaway working directory, secret-free environment, and
  POSIX resource limits. `SANDBOX_MODE=docker` adds a network-less, non-root,
  read-only, resource-capped container per run.
- Run-guard re-moderates any hand-edited script before it executes.
- CSRF double-submit protection, per-identity rate limiting (keyed on IP or
  account, not a resettable cookie), and per-session file isolation.

### Accounts, billing, and operations
- Optional email/password accounts with optional email verification, behind
  config flags.
- Billing seam (`check_and_debit`) with money-safety guardrails: a monthly
  ceiling on premium calls and a startup warning if billing is on without a cap.
- App-factory Flask layout in the `statlee` package, Docker / Docker Compose
  setup, and CI (ruff, byte-compile, pytest on 3.11 and 3.12).

### Tests
- 178 tests covering the full HTTP surface with an injected fake LLM, so the
  suite runs offline with no API key.

[1.1.0]: https://github.com/yib7/STATlee/releases/tag/v1.1.0
[1.0.1]: https://github.com/yib7/STATlee/releases/tag/v1.0.1
[1.0.0]: https://github.com/yib7/STATlee/releases/tag/v1.0.0
