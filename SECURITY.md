# Security Policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue or pull request.

Use GitHub's private reporting: open the repository's **Security** tab and choose
**Report a vulnerability** ([Privately reporting a security
vulnerability](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).
That opens a private advisory only the maintainer can see.

Include the affected file or route, the configuration you ran (especially
`SANDBOX_MODE` and `LLM_PROVIDER`), and the steps to reproduce. A minimal
proof of concept helps more than a long description.

Expect a first response within about a week. STATlee is a portfolio project
maintained by one person, so timelines are best-effort rather than contractual.

## What is in scope

STATlee runs model-generated code, so the parts most worth scrutiny are:

- **Sandbox escape** (`statlee/sandbox.py`) where generated Python/R could read
  host secrets, reach the network, or persist outside its throwaway directory.
- **Run-guard bypass** (`statlee/routes/analyze.py`) where a hand-edited script
  reaches execution without being re-moderated.
- **Moderation bypass** (`statlee/prompts.py` plus the route gates) that lets a
  disallowed request through.
- **Cross-identity data access** where one session or account reads another's
  uploads or results.
- **Auth, CSRF, and rate-limit keying** weaknesses that allow account takeover,
  forged state-changing requests, or resetting a rate-limit bucket.

## Known boundaries (not vulnerabilities)

These are documented design choices, not bugs to report:

- `SANDBOX_MODE=subprocess` (the default) runs generated code on the host with a
  secret-free environment and, on POSIX, resource limits. It is the dev-friendly
  mode and is **not** a hard isolation boundary. Use `SANDBOX_MODE=docker` for
  network-less, non-root, read-only container isolation per run.
- The default in-memory rate-limit store is per-worker. Production must set a
  shared store (`RATELIMIT_STORAGE_URI`) or pin `WEB_CONCURRENCY=1`.

## Supported versions

This is a single-line project: fixes land on the latest `main`. There is no
backport branch.
