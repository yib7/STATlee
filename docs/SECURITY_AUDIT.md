# Security Audit — STATlee

_Adversarial review against the question: "Can a random internet user run up my
LLM bill or read my keys?"_ Date: 2026-06-14 (updated 2026-06-20 for the
structured-verdict moderation gate; the app uses Google Gemini).

The review covered rate limiting, the `/run` run-guard, the execution sandbox,
upload limits, moderation/prompt-injection, secret handling, CORS, and the SSE
endpoints. Findings below are grouped **fixed in this pass**, **verified clean**,
and **residual risk (documented)**.

## Threat model (what actually protects you)
The real security boundary for executed code is the **sandbox**, not the topical
moderation gate:
- Generated code runs with a **secret-free environment** (`sandbox._safe_env` —
  explicit allowlist; `GEMINI_API_KEY` is never present).
- Each run gets a **throwaway working directory** containing only the one
  dataset; the dir is deleted afterward.
- `SANDBOX_MODE=docker` adds true isolation: network-less, non-root, read-only,
  resource-capped sibling container.
- The `/run` **run-guard** only executes the server-approved script (or a
  re-moderated hand-edit), so the model's output can't be swapped for arbitrary
  code without passing `code_moderation` first.

The bill-abuse boundary is **rate limiting** + (future) per-account credit caps.

---

## Update — 2026-06-19

- **Rate-limit store now holds across workers — MEDIUM.** The limiter previously
  used an in-memory store while the container runs multiple gunicorn workers, so
  buckets were per-worker and reset on restart, loosening the bill-abuse limits.
  `RATELIMIT_STORAGE_URI` (config) now selects the backing store; production
  warns when it is still `memory://`. Set a shared store (e.g. `redis://`) or pin
  `WEB_CONCURRENCY=1`.
- **Moderation now fails closed.** See the resolved residual-risk note below.

---

## Fixed in this pass

### 1. Rate-limit bypass by dropping cookies — **HIGH**
**Was:** the limiter keyed on the server-set `sid` session cookie
(`_session_key` → `session.get('sid')`). Because the server mints that cookie,
an abuser who simply discards cookies between requests received a fresh, empty
rate-limit bucket every request — fully bypassing the limits on the expensive
LLM endpoints and able to run up the Gemini bill.

**Fix:** `extensions._rate_limit_key` now keys on **client IP** for anonymous
traffic and **`user_<id>`** for logged-in users. Dropping cookies no longer
changes the bucket. Added `ProxyFix` (gated on `TRUST_PROXY_HOPS`, default `1`
in production) so the real client IP is read from `X-Forwarded-For` behind
Render's proxy rather than the proxy's own address.
_Tests:_ `test_rate_limit_key_is_ip_for_anonymous`,
`test_rate_limit_key_is_account_for_logged_in`.

### 2. Expensive endpoints had no rate limit at all — **HIGH**
**Was:** only `/chat` and `/run` carried `@limiter.limit`. The configured
`rate_limit_default` was **never wired** to the limiter, so every other
model-calling endpoint was unthrottled: `/interpret` (flash), `/converse`,
`/method_prompt` (flash), `/generate_report` (**pro** — most expensive),
`/suggest`, `/classify_variables`, `/extract_pdf_codebook`, `/wrangle`
(LLM + sandbox), plus `/upload` / `/upload_pdf` (parsing cost) and
`/report_issue` (DB/email spam).

**Fix:** added explicit `@limiter.limit` decorators to all of the above —
`rate_limit_chat` (20/min) for the LLM/parse endpoints, `rate_limit_run`
(10/min) for `/wrangle` since it executes code in the sandbox like `/run`.
Static assets and `/health` are intentionally left unthrottled.

### 3. Subprocess sandbox in production — **MEDIUM (defense-in-depth)**
**Was:** nothing flagged running the weaker `subprocess` sandbox in production.
In that mode generated code runs as the app user with full filesystem read
access (no container, no chroot) — the topical/code moderation gates are the
only barrier.

**Fix:** `Config.validate()` now emits a loud warning when
`env=production and SANDBOX_MODE=subprocess`, steering production to
`SANDBOX_MODE=docker`. _Tests:_ `test_production_subprocess_sandbox_warns`,
`test_production_docker_sandbox_is_quiet`.

---

## Verified clean (no change needed)

- **Secret scrubbing (`sandbox._safe_env`).** Explicit allowlist; no app secret
  (Gemini key, Flask secret, SMTP creds, DB URL) is ever placed in
  the child environment. Windows-only entries (`APPDATA`, `SYSTEMROOT`, …) are
  plain paths, not secrets, and don't apply to the Linux/Docker production path.
- **`/run` run-guard.** Per-identity approved-script store; if the submitted
  code differs from the approved script it is re-moderated via
  `code_moderation` and rejected on `BLOCK`. No path observed that runs
  unapproved, unmoderated code. (`routes/analyze.py`, `storage.save/load_approved_script`.)
- **Path traversal / cross-identity reads.** `storage.resolve_path` runs
  `secure_filename` then a `realpath` containment check against the caller's
  own `user_<id>`/`anon_<sid>` root; traversal and absolute paths resolve to
  `None`. Covered by `test_resolve_path_*` and `test_identity_isolation`.
- **Upload size.** `MAX_CONTENT_LENGTH = max_upload_mb (16) * 1MB` → framework
  `413`; extension allowlist (`datatools.SUPPORTED_EXTENSIONS`) before parse.
- **CSRF.** Double-submit token from the session checked on every
  `POST/PUT/DELETE`; all SSE endpoints are POST and therefore covered. Session
  cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` in production.
- **CORS.** No CORS headers are emitted, so the browser same-origin policy
  blocks cross-origin reads by default. Combined with the CSRF token + Lax
  cookies, no wildcard exposure exists. No `flask-cors` wildcard present.
- **Error leakage (1.6).** Handlers return generic messages; `str(e)` is logged
  server-side, never returned to the client.

---

## Residual risk (documented, accepted for this round)

- **Moderation is topical, not a security control.** Execution safety is enforced
  by the sandbox + `code_moderation` run-guard, not by the topical gate.
  _Update 2026-06-19:_ the prior fail-open weakness (keying off `'BLOCK' in text`,
  which a prompt-injection could suppress) is **resolved** — `moderation` and
  `code_moderation` now return a structured JSON verdict
  (`{"decision": "pass"|"block"}`) and `routes.moderation_blocked` **defaults to
  deny** on any non-`pass` or unparseable reply. The sandbox remains the real
  boundary regardless.
- **`/metrics` is reachable in open mode.** When `REQUIRE_LOGIN=false` and no
  `PASSWORD` is set, anyone can read aggregate token counts. Low sensitivity (no
  per-user data, no secrets). Lock down by setting `REQUIRE_LOGIN=true` or
  adding it to an authenticated-only set if exposed publicly.
- **No per-account spend ceiling yet.** Rate limits cap velocity but not total
  monthly spend. PLAN workstream E leaves the seam (`check_and_debit`) for
  per-account credit caps; before opening a free tier, add a hard monthly spend
  ceiling on the server's own key and email verification on signup.

## API-terms compliance
The app uses the **Google Gemini API**; the operator must comply with its terms
for their deployment. Usage must follow the Gemini API Additional Terms and the
Prohibited Use Policy. The moderation gate blocks malware/illegal requests,
matching the prohibited-use requirements. A compliance note + "Powered by Google
Gemini" attribution is shown in the README/footer.

## Verification
`pytest -q` → all pass (incl. the 4 new tests above); `ruff check .` clean;
`APP_ENV=testing python -c "import statlee.app"` boots cleanly.
