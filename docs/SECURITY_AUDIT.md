# Security Audit — CodeCaster

_Adversarial review against the question: "Can a random internet user run up my
Gemini bill or read my keys?"_ Date: 2026-06-14.

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
  (Gemini key, Flask secret, SMTP creds, DB URL) is ever placed in the child
  environment. Windows-only entries (`APPDATA`, `SYSTEMROOT`, …) are plain
  paths, not secrets, and don't apply to the Linux/Docker production path.
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

- **Moderation is topical, not a security control.** The generation-time
  `moderation` gate embeds the user prompt and keys off `'BLOCK' in text`, so a
  prompt-injection that prevents the model from emitting `BLOCK` "passes." This
  is acceptable because execution safety is enforced by the sandbox +
  `code_moderation` run-guard, not by topical moderation. If moderation is ever
  relied on as a boundary, switch to a structured verdict (e.g. JSON
  `{"decision": "block|pass"}`) and default-deny on parse failure.
- **`/metrics` is reachable in open mode.** When `REQUIRE_LOGIN=false` and no
  `PASSWORD` is set, anyone can read aggregate token counts. Low sensitivity (no
  per-user data, no secrets). Lock down by setting `REQUIRE_LOGIN=true` or
  adding it to an authenticated-only set if exposed publicly.
- **No per-account spend ceiling yet.** Rate limits cap velocity but not total
  monthly spend. PLAN workstream E leaves the seam (`check_and_debit`) for
  per-account credit caps; before opening a free tier, add a hard monthly spend
  ceiling on the server's own key and email verification on signup.

## API-terms compliance
- **Google Gemini API:** usage must follow the Gemini API Additional Terms and
  the Prohibited Use Policy. The app's moderation gate blocks malware/illegal
  requests, matching the prohibited-use requirements. A short compliance note +
  "Powered by Google Gemini" attribution is added to the README/footer.
- **Anthropic:** the optional Claude code-drafting path was **removed** (the app
  is Gemini-only now), so no Anthropic attribution is required.

## Verification
`pytest -q` → all pass (incl. the 4 new tests above); `ruff check .` clean;
`APP_ENV=testing python -c "import app"` boots cleanly.
