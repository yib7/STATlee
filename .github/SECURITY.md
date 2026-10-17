# Security Policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue or pull request.

Use GitHub's private reporting: open the repository's Security tab and choose
"Report a vulnerability" ([Privately reporting a security
vulnerability](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).
That opens a private advisory only the maintainer can see.

Include the affected file or route, the configuration you ran (especially
`SANDBOX_MODE` and `LLM_PROVIDER`), and the steps to reproduce. A minimal
proof of concept helps more than a long description.

Expect a first response within about a week. STATlee is a portfolio project
maintained by one person, so timelines are best-effort rather than contractual.

## What is in scope

STATlee runs model-generated code, so the parts most worth scrutiny are:

- Sandbox escape (`statlee/sandbox.py`), where generated Python/R could read
  host secrets, reach the network, or persist outside its throwaway directory.
- Run-guard bypass (`statlee/routes/analyze.py`), where a hand-edited script
  reaches execution without being re-moderated.
- Static pre-check bypass (`statlee/codecheck.py`), where a Python script
  reaches a refused module, a dangerous `os`/`sys`/`shutil` member, a
  reflection escape, or a constant out-of-directory path in a way the allowlist
  is meant to catch (the limitations listed below excepted).
- Moderation bypass (`statlee/prompts.py` plus the route gates) that lets a
  disallowed request through.
- Cross-identity data access, where one session or account reads another's
  uploads or results.
- Auth, CSRF, and rate-limit keying weaknesses that allow account takeover,
  forged state-changing requests, or resetting a rate-limit bucket.
- Emailed-link poisoning, where a reset or verification link points anywhere
  other than `PUBLIC_BASE_URL`, or its token is logged outside development.
- Operator data exposure, where `/metrics` answers without its token in
  production.

## Known boundaries (not vulnerabilities)

These are documented design choices, not bugs to report:

- `SANDBOX_MODE=subprocess` (the default) runs generated code on the host with a
  secret-free environment and, on POSIX, resource limits. It is the dev-friendly
  mode and is **not** a hard isolation boundary. Use `SANDBOX_MODE=docker` for
  network-less, non-root, read-only container isolation per run.
- The default in-memory rate-limit store is per-worker. Production must set a
  shared store (`RATELIMIT_STORAGE_URI`) or pin `WEB_CONCURRENCY=1`.
- `statlee/codecheck.py` is a best-effort static layer in front of the LLM
  code review and the sandbox, not a proof that a script is safe. Known gaps:
  paths built entirely at runtime (`open(var)`, `Path.home() / ".ssh"`) are not
  resolved; internals of allowlisted libraries are covered only where an
  attribute chain names a policed module or primitive; format-string attribute
  traversal is not modeled; non-destructive `os`/`shutil` calls with dynamic
  arguments are allowed; and the R check is a regex denylist, so calls built
  from strings outside `do.call` (`match.fun(paste0(...))`) can get through.
  The sandbox is the boundary for these.
- `/metrics` exposes server-wide model usage. In production it answers `404`
  unless `METRICS_TOKEN` is set, and then only to that token. In development
  and testing it stays open to the normal app gate when no token is set.
- Password-reset and verification emails need `PUBLIC_BASE_URL` in production;
  without it no link is sent rather than one built from the request's `Host`.

## Supported versions

| Version | Supported |
|---|---|
| 1.6.x | Yes |
| 1.4.x and older | No: upgrade to 1.6 (see "Upgrading from 1.4.x" in [CHANGELOG.md](https://github.com/yib7/STATlee/blob/main/CHANGELOG.md)) |

1.5.0 was never released on its own; its changes ship in 1.6.0. Fixes land on
`main` and in the next 1.6.x release. There is no backport branch.
