# Changelog

All notable changes to STATlee are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.0]: https://github.com/yib7/STATlee/releases/tag/v1.0.0
