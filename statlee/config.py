"""Centralized, validated configuration (roadmap 1.3).

Reads and validates the environment exactly once. ``app.py`` builds the Flask
app from a ``Config`` instance; every tunable lives here instead of being a
literal buried in a route body.
"""
import logging
import os
import tempfile
from dataclasses import dataclass, field

logger = logging.getLogger('statlee.config')

VALID_ENVS = ('development', 'production', 'testing')


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer value for %s: %r", name, raw)
        return default


@dataclass
class Config:
    # --- Environment -------------------------------------------------------
    env: str = 'development'

    # --- Secrets ------------------------------------------------------------
    gemini_api_key: str = ''
    flask_secret_key: str = ''
    app_password: str = ''          # legacy single-password gate (optional)

    # --- LLM provider -------------------------------------------------------
    # Which vendor backs every model call. Gemini is the default; a self-hoster
    # can switch to Claude or OpenAI with their own key. Per-provider default
    # model ids (per role) are applied in from_env(); MODEL_* env still overrides
    # any single role regardless of provider.
    llm_provider: str = 'gemini'             # 'gemini' | 'anthropic' | 'openai'
    anthropic_api_key: str = ''
    anthropic_max_tokens: int = 8192
    anthropic_stream_max_tokens: int = 16000
    openai_api_key: str = ''
    # OpenAI's max_completion_tokens also covers hidden reasoning tokens on the
    # gpt-6.x reasoning defaults (D3-16), so the budgets sit well above the visible
    # output a script needs. Streams (code drafts) get the larger one.
    openai_max_tokens: int = 16000
    openai_stream_max_tokens: int = 32000
    # Per-call client timeouts and retries for every provider (D3-5). The SDK
    # defaults were 600 s x 3 attempts for Anthropic/OpenAI (up to ~30 min on
    # one worker thread) and a single attempt for Gemini. The Pro budget covers
    # the 'pro_max' model (long Pro drafts and report drafts).
    llm_timeout_seconds: int = 120
    llm_pro_timeout_seconds: int = 300
    llm_max_retries: int = 2

    # --- Server -------------------------------------------------------------
    port: int = 5000
    # Interface the `python wsgi.py` dev server binds (D5-8). Loopback by
    # default: the local run is open-auth and executes generated code, so LAN
    # exposure is an explicit BIND_HOST=0.0.0.0 opt-in. gunicorn (the container)
    # binds via its own --bind flag and ignores this.
    bind_host: str = '127.0.0.1'
    max_upload_mb: int = 16

    # --- Storage / database --------------------------------------------------
    upload_root: str = ''           # empty -> <instance>/uploads (temp dir in testing)
    database_url: str = ''          # empty -> sqlite file under instance dir
    # Only 'local' is implemented. 's3' (roadmap 7.3) is rejected by validate()
    # until a real backend exists, rather than silently writing to local disk.
    storage_backend: str = 'local'
    file_ttl_seconds: int = 7200    # anonymous-file cleanup window (2h)
    # Per-identity storage quota (P2-4). The 2h TTL is the only other reclaim,
    # so without these one IP could park many uploads (20/min x 16MB) on disk
    # for hours before cleanup runs. Enforced at /upload and /upload_pdf with a
    # cheap directory scan BEFORE the incoming file is saved; 0 disables either
    # cap. Counts uploaded files (not version artifacts) for the count cap, and
    # true on-disk bytes (uploads + version artifacts + sidecars) for the byte
    # cap, so the byte cap reflects real disk pressure.
    max_datasets_per_identity: int = 10
    max_bytes_per_identity: int = 200 * 1024 * 1024   # 200 MB
    # Session workbook (SP1): runs kept per identity. Over the cap the oldest
    # unstarred run is evicted (the oldest run when every one is starred).
    max_runs_per_identity: int = 50
    # Parse-time bounds against a decompression bomb (P2-2). max_upload_mb caps
    # the request body, but normalize_to_csv fully materializes Excel/Stata/SPSS
    # into a pandas DataFrame in the web request handler before writing CSV, so
    # a crafted 16 MB .xlsx (a zip) can decompress to gigabytes of cells and
    # OOM/pin a worker. These bound the parse and reject early; 0 disables
    # either check (matching the quota knobs' convention above).
    # Reject a zip-based upload (.xlsx) whose total UNCOMPRESSED size exceeds
    # this — guards the zip-bomb ratio (16 MB compressed -> GBs).
    max_upload_uncompressed_mb: int = 512
    # Reject a dataset whose rows*cols exceeds this — bounds DataFrame memory.
    # Very generous for social-science surveys (typically <1M cells).
    max_upload_cells: int = 10_000_000

    # --- Execution sandbox (Tier 0) ------------------------------------------
    sandbox_mode: str = 'subprocess'   # 'subprocess' | 'docker' (0.3)
    runner_image: str = 'statlee-runner'
    # Parent dir for per-run throwaway sandbox work dirs. Empty -> system temp.
    # With SANDBOX_MODE=docker and the app itself containerized (host docker
    # socket mounted), set this to a path the operator bind-mounts at the SAME
    # absolute path into the app container, so the host daemon can resolve the
    # run dir passed to `docker run -v` (P1-5).
    sandbox_work_root: str = ''
    exec_timeout: int = 60
    exec_memory_mb: int = 2048
    exec_output_limit: int = 256 * 1024   # truncate captured stdout/stderr
    # Largest single file a run may write, in both sandbox modes (B-10). It sits
    # above the biggest CSV MAX_UPLOAD_CELLS allows, so a /wrangle rewrite of
    # the dataset fits; all files a run writes together are capped at twice it.
    exec_file_limit_mb: int = 256

    # --- Model routing (3.4) --------------------------------------------------
    # Default code generation runs on gemini-3.8-flash (cheap, fast, the
    # strongest Flash), with gemini-3.5-flash-lite for the lighter flash/lite
    # roles. The in-app "Pro mode" toggle re-routes code generation to
    # model_pro_max (gemini-4-argon, the GA flagship reasoning model) for the
    # hardest analyses: a bigger, stronger, costlier model. Cost ordering:
    # pro_max (4.00/20.00) > pro (1.50/7.50) > flash = lite (0.30/2.50).
    # Override any role independently via MODEL_* env.
    # FOOTGUN (P2-4): model_pro is the DEFAULT code-gen tier (roles 'pro' AND
    # 'draft' both resolve to it in llm._resolve) — it is NOT the "Pro mode"
    # model. Setting MODEL_PRO changes every normal generation; to change what
    # the in-app Pro-mode toggle runs, set MODEL_PRO_MAX (model_pro_max) below.
    model_pro: str = 'gemini-3.8-flash'        # default code-gen / 'draft' role
    # "Pro mode" code-gen upgrade. gemini-4-argon is a stable (GA) id, which
    # replaces the old gemini-3.1-pro-preview pin (3.1 Pro never left preview).
    model_pro_max: str = 'gemini-4-argon'
    model_flash: str = 'gemini-3.5-flash-lite'
    model_flash_lite: str = 'gemini-3.5-flash-lite'
    converse_role: str = 'flash'        # downshift candidate: pro -> flash
    # Conversational data-cleaning ("wrangle") tier. Defaults to the cheapest
    # model since these are short, structured transforms ("delete column 2",
    # "filter for X") — keeps per-edit cost (and the operator's bill) low.
    wrangle_role: str = 'lite'

    # Per-model price estimates (USD per 1M tokens) for the in-app session cost
    # display (3.4). Web-verified Gemini paid-tier rates (ai.google.dev, Oct
    # 2026). DISPLAY ONLY — these never gate, trigger, or change any spend; they
    # only let the client show an approximate session cost. Override by editing
    # this map or constructing Config with your own.
    # Introductory discounts (gemini-3.8-flash and gemini-4-argon run at half
    # these rates until Dec 2026 / Jan 2027) are ignored: the rows are the
    # standard rates, so the estimate errs high, never low.
    # Non-Gemini rows are published per-1M rates (verified Oct 2026) for the
    # per-provider default models below — DISPLAY ONLY, never gate spend.
    # Override MODEL_* and this map together if you pin different models.
    model_prices: dict = field(default_factory=lambda: {
        'gemini-4-argon': {'input': 4.00, 'output': 20.00},
        'gemini-3.8-flash': {'input': 1.50, 'output': 7.50},
        'gemini-3.5-flash-lite': {'input': 0.30, 'output': 2.50},
        # Anthropic / Claude defaults (haiku-5-5: the <=100k-token prompt rate)
        'claude-fable-5-1': {'input': 10.00, 'output': 50.00},
        'claude-opus-5-5': {'input': 4.00, 'output': 20.00},
        'claude-sonnet-5-5': {'input': 2.00, 'output': 10.00},
        'claude-haiku-5-5': {'input': 0.10, 'output': 0.50},
        # OpenAI defaults
        'gpt-6-astra': {'input': 10.00, 'output': 50.00},
        'gpt-6.1-sol': {'input': 2.00, 'output': 10.00},
        'gpt-6-luna': {'input': 0.10, 'output': 0.50},
    })

    # --- Analysis tunables -----------------------------------------------------
    feature_selection_threshold: int = 15
    data_page_row_cap: int = 500_000    # refuse to page datasets beyond this (5.8)
    pdf_max_pages: int = 50

    # --- Auth (7.1) -------------------------------------------------------------
    # Public origin (scheme://host[:port]) that emailed links point at (D5-7).
    # Password-reset links are built from it, never from the request's Host
    # header (which a caller controls). Required in production for resets.
    public_base_url: str = ''
    accounts_enabled: bool = True
    require_login: bool = False         # False keeps the anonymous sandbox mode
    require_email_verification: bool = False  # gate accounts until email confirmed

    # --- Billing / cost guardrails (workstream E) -------------------------------
    billing_enabled: bool = False       # turn on credit debit in check_and_debit
    # Hard ceiling on priority (high-tier) requests per calendar month, enforced
    # on the server's own key so nobody can run up an unbounded bill. Applies
    # whether or not billing is enabled (D5-3). 0 disables.
    # NOTE: per-process and in-memory — across multiple workers each enforces
    # its own count, and the count resets on every restart or redeploy.
    monthly_priority_call_ceiling: int = 0
    # Free credits granted to a logged-in free-plan user at the start of each
    # calendar month, applied lazily on their first billed request that month
    # (P2-10). 0 (default) disables the top-up, and the out-of-credits message
    # then makes no promise of a monthly reset. When >0 the reset is real, so
    # the deny message truthfully points at it.
    monthly_free_credits: int = 0

    # --- Rate limits (1.4) --------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_default: str = '120 per minute'
    rate_limit_run: str = '10 per minute'
    rate_limit_chat: str = '20 per minute'
    # /data_page re-reads the active CSV and re-applies every column filter over
    # the whole frame per request, so it gets a tighter cap than the 120/min
    # default (paging is cheaper than an LLM call but far from free).
    rate_limit_data_page: str = '60 per minute'
    # Guards the token-consuming /verify_email endpoint against brute force
    # (token entropy is high, so this is defense-in-depth, not the primary
    # control).
    rate_limit_verify: str = '5 per minute'
    # Guards /login and /register against credential stuffing and mass account
    # creation; the app default (120/min) is far too generous for password
    # guessing.
    rate_limit_auth: str = '10 per minute'
    # Backing store for rate-limit counters. Default 'memory://' is per-process:
    # with >1 gunicorn worker the buckets are NOT shared (each worker enforces
    # its own copy) and they reset on every restart, which weakens the
    # bill-abuse protection. Point this at a shared store (e.g. redis://...) in
    # production, or pin WEB_CONCURRENCY=1. See validate().
    rate_limit_storage_uri: str = 'memory://'
    # Number of trusted reverse-proxy hops in front of the app. >0 enables
    # ProxyFix so rate limiting and logging see the real client IP from
    # X-Forwarded-For. Defaults to 0 everywhere (D5-13): trusting the header on
    # a directly exposed app lets a client spoof X-Forwarded-For and get a fresh
    # rate-limit bucket per request. Behind exactly one proxy (Render), set
    # TRUST_PROXY_HOPS=1; validate() warns in production while it is 0.
    trust_proxy_hops: int = 0

    # --- CSRF (1.5) -----------------------------------------------------------------
    csrf_enabled: bool = True

    # --- /metrics (3.3) ----------------------------------------------------------------
    # Bearer token for the operator-only /metrics snapshot (uptime and every
    # model's call/token totals). Set: a request must carry it (Authorization:
    # Bearer <token> or X-Metrics-Token), in every environment, and needs no
    # session or login. Unset: /metrics is a 404 in production and open behind
    # the normal app gate in development/testing.
    metrics_token: str = ''

    # --- Issue reporting (6.3) ---------------------------------------------------------
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_user: str = ''
    smtp_password: str = ''
    issue_report_to: str = ''

    warnings: list = field(default_factory=list)

    # Per-provider default model ids by role, applied in from_env() when the
    # operator hasn't pinned MODEL_* explicitly. Keeps "set LLM_PROVIDER + key"
    # working out of the box for each vendor.
    _PROVIDER_MODEL_DEFAULTS = {
        'anthropic': {'pro': 'claude-opus-5-5', 'pro_max': 'claude-fable-5-1',
                      'flash': 'claude-sonnet-5-5', 'lite': 'claude-haiku-5-5'},
        'openai': {'pro': 'gpt-6.1-sol', 'pro_max': 'gpt-6-astra',
                   'flash': 'gpt-6-luna', 'lite': 'gpt-6-luna'},
    }

    # ------------------------------------------------------------------
    @classmethod
    def _provider_model_defaults(cls, provider):
        """Role→model-id default map for ``provider`` (Gemini fields are the
        fallback for any provider without a preset)."""
        preset = cls._PROVIDER_MODEL_DEFAULTS.get(provider)
        if preset:
            return preset
        return {'pro': cls.model_pro, 'pro_max': cls.model_pro_max,
                'flash': cls.model_flash, 'lite': cls.model_flash_lite}

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls):
        provider = os.environ.get('LLM_PROVIDER', 'gemini').strip().lower()
        md = cls._provider_model_defaults(provider)
        cfg = cls(
            env=os.environ.get('APP_ENV', 'development').strip().lower(),
            gemini_api_key=os.environ.get('GEMINI_API_KEY', '').strip(),
            flask_secret_key=os.environ.get('FLASK_SECRET_KEY', '').strip(),
            app_password=os.environ.get('PASSWORD', '').strip(),
            llm_provider=provider,
            anthropic_api_key=os.environ.get('ANTHROPIC_API_KEY', '').strip(),
            anthropic_max_tokens=_env_int('ANTHROPIC_MAX_TOKENS', 8192),
            anthropic_stream_max_tokens=_env_int(
                'ANTHROPIC_STREAM_MAX_TOKENS', 16000),
            openai_api_key=os.environ.get('OPENAI_API_KEY', '').strip(),
            openai_max_tokens=_env_int('OPENAI_MAX_TOKENS', 16000),
            openai_stream_max_tokens=_env_int('OPENAI_STREAM_MAX_TOKENS', 32000),
            llm_timeout_seconds=_env_int('LLM_TIMEOUT_SECONDS', 120),
            llm_pro_timeout_seconds=_env_int('LLM_PRO_TIMEOUT_SECONDS', 300),
            llm_max_retries=_env_int('LLM_MAX_RETRIES', 2),
            port=_env_int('PORT', 5000),
            bind_host=os.environ.get('BIND_HOST', '127.0.0.1').strip() or '127.0.0.1',
            max_upload_mb=_env_int('MAX_UPLOAD_MB', 16),
            upload_root=os.environ.get('UPLOAD_ROOT', '').strip(),
            database_url=os.environ.get('DATABASE_URL', '').strip(),
            storage_backend=os.environ.get('STORAGE_BACKEND', 'local').strip().lower(),
            file_ttl_seconds=_env_int('FILE_TTL_SECONDS', 7200),
            max_datasets_per_identity=_env_int('MAX_DATASETS_PER_IDENTITY', 10),
            max_bytes_per_identity=_env_int(
                'MAX_BYTES_PER_IDENTITY', 200 * 1024 * 1024),
            max_runs_per_identity=_env_int('MAX_RUNS_PER_IDENTITY', 50),
            max_upload_uncompressed_mb=_env_int(
                'MAX_UPLOAD_UNCOMPRESSED_MB', 512),
            max_upload_cells=_env_int('MAX_UPLOAD_CELLS', 10_000_000),
            sandbox_mode=os.environ.get('SANDBOX_MODE', 'subprocess').strip().lower(),
            runner_image=os.environ.get('RUNNER_IMAGE', 'statlee-runner').strip(),
            sandbox_work_root=os.environ.get('SANDBOX_WORK_ROOT', '').strip(),
            exec_timeout=_env_int('EXEC_TIMEOUT', 60),
            exec_memory_mb=_env_int('EXEC_MEMORY_MB', 2048),
            exec_output_limit=_env_int('EXEC_OUTPUT_LIMIT', 256 * 1024),
            exec_file_limit_mb=_env_int('EXEC_FILE_LIMIT_MB', 256),
            model_pro=os.environ.get('MODEL_PRO', md['pro']).strip(),
            model_pro_max=os.environ.get('MODEL_PRO_MAX', md['pro_max']).strip(),
            model_flash=os.environ.get('MODEL_FLASH', md['flash']).strip(),
            model_flash_lite=os.environ.get('MODEL_FLASH_LITE', md['lite']).strip(),
            converse_role=os.environ.get('CONVERSE_ROLE', 'flash').strip().lower(),
            wrangle_role=os.environ.get('WRANGLE_ROLE', 'lite').strip().lower(),
            feature_selection_threshold=_env_int('FEATURE_SELECTION_THRESHOLD', 15),
            data_page_row_cap=_env_int('DATA_PAGE_ROW_CAP', 500_000),
            pdf_max_pages=_env_int('PDF_MAX_PAGES', 50),
            public_base_url=os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/'),
            accounts_enabled=_env_bool('ACCOUNTS_ENABLED', True),
            require_login=_env_bool('REQUIRE_LOGIN', False),
            require_email_verification=_env_bool('REQUIRE_EMAIL_VERIFICATION', False),
            billing_enabled=_env_bool('BILLING_ENABLED', False),
            monthly_priority_call_ceiling=_env_int('MONTHLY_PRIORITY_CALL_CEILING', 0),
            monthly_free_credits=_env_int('MONTHLY_FREE_CREDITS', 0),
            rate_limit_enabled=_env_bool('RATE_LIMIT_ENABLED', True),
            rate_limit_default=os.environ.get('RATE_LIMIT_DEFAULT', '120 per minute'),
            rate_limit_run=os.environ.get('RATE_LIMIT_RUN', '10 per minute'),
            rate_limit_chat=os.environ.get('RATE_LIMIT_CHAT', '20 per minute'),
            rate_limit_data_page=os.environ.get(
                'RATE_LIMIT_DATA_PAGE', '60 per minute'),
            rate_limit_verify=os.environ.get('RATE_LIMIT_VERIFY', '5 per minute'),
            rate_limit_auth=os.environ.get('RATE_LIMIT_AUTH', '10 per minute'),
            rate_limit_storage_uri=(
                os.environ.get('RATELIMIT_STORAGE_URI')
                or os.environ.get('REDIS_URL') or 'memory://').strip(),
            trust_proxy_hops=_env_int('TRUST_PROXY_HOPS', 0),
            smtp_host=os.environ.get('SMTP_HOST', '').strip(),
            smtp_port=_env_int('SMTP_PORT', 587),
            smtp_user=os.environ.get('SMTP_USER', '').strip(),
            smtp_password=os.environ.get('SMTP_PASSWORD', ''),
            issue_report_to=os.environ.get('ISSUE_REPORT_TO', '').strip(),
            metrics_token=os.environ.get('METRICS_TOKEN', '').strip(),
        )
        cfg.validate()
        return cfg

    # ------------------------------------------------------------------
    def active_model_prices(self):
        """Price map (USD per 1M tokens) for the models actually in use, keyed
        by model id — exactly what the client needs to estimate session cost.
        Models with no known price are simply omitted (they contribute 0)."""
        active = {self.model_pro, self.model_pro_max,
                  self.model_flash, self.model_flash_lite}
        return {mid: self.model_prices[mid] for mid in active
                if mid in self.model_prices}

    # ------------------------------------------------------------------
    def validate(self):
        """Fail fast on hard requirements; warn loudly on soft ones."""
        if self.env not in VALID_ENVS:
            raise ValueError(
                f"APP_ENV must be one of {VALID_ENVS}, got {self.env!r}")

        if self.llm_provider not in ('gemini', 'anthropic', 'openai'):
            raise ValueError(
                "LLM_PROVIDER must be 'gemini', 'anthropic', or 'openai'")

        # The API key the selected provider needs. (Anthropic also supports a
        # keyless local OAuth/subscription login, but a real deploy should use
        # an API key, so it's still required in production.)
        key_name, key_val = {
            'gemini': ('GEMINI_API_KEY', self.gemini_api_key),
            'anthropic': ('ANTHROPIC_API_KEY', self.anthropic_api_key),
            'openai': ('OPENAI_API_KEY', self.openai_api_key),
        }[self.llm_provider]

        if self.is_production:
            missing = []
            if not key_val:
                missing.append(key_name)
            if not self.flask_secret_key:
                missing.append('FLASK_SECRET_KEY')
            if missing:
                raise ValueError(
                    "Refusing to start in production without: "
                    + ", ".join(missing))
        else:
            if not key_val and self.env != 'testing':
                self._warn(
                    f"{key_name} is not set — LLM endpoints will return "
                    "errors until it is configured.")
            if not self.flask_secret_key and self.env != 'testing':
                self._warn(
                    "FLASK_SECRET_KEY not set — using a random key. Logins "
                    "reset on restart and fail across multiple workers.")

        if self.sandbox_mode not in ('subprocess', 'docker'):
            raise ValueError("SANDBOX_MODE must be 'subprocess' or 'docker'")
        if self.sandbox_mode == 'subprocess' and not self.is_testing:
            # Warn in DEVELOPMENT too, not only production (P1-1b). Subprocess
            # mode has no network or filesystem isolation: generated code runs
            # as the app user with full read access, so the AST static pre-check
            # (statlee/codecheck.py) and the LLM moderation gates are the only
            # barriers, and both are best-effort. A self-hoster whose machine
            # holds real secrets or network access should run SANDBOX_MODE=docker.
            self._warn(
                "SANDBOX_MODE=subprocess — generated code runs as the app user "
                "with no network or filesystem isolation; the AST static "
                "pre-check and LLM moderation are the only barriers. Use "
                "SANDBOX_MODE=docker for kernel-enforced isolation where the "
                "host holds secrets or network access that matters.")
        if self.storage_backend == 's3':
            # D5-10: there is no S3 backend; accepting the setting meant every
            # read and write silently stayed on local disk.
            raise ValueError(
                "STORAGE_BACKEND=s3 is not implemented yet: uploads would still "
                "be written to local disk. Use STORAGE_BACKEND=local with "
                "UPLOAD_ROOT on a persistent volume.")
        if self.storage_backend != 'local':
            raise ValueError("STORAGE_BACKEND must be 'local'")

        if (self.is_production and self.rate_limit_enabled
                and self.rate_limit_storage_uri.startswith('memory://')):
            self._warn(
                "RATELIMIT_STORAGE_URI is in-memory (memory://) in production — "
                "rate-limit buckets are per-worker and reset on restart, so the "
                "configured limits do not hold across multiple gunicorn workers "
                "and a redeploy clears them, weakening bill-abuse protection. Set "
                "a shared store (e.g. RATELIMIT_STORAGE_URI=redis://...) or pin "
                "WEB_CONCURRENCY=1.")

        if self.public_base_url and not self.public_base_url.startswith(
                ('http://', 'https://')):
            raise ValueError(
                "PUBLIC_BASE_URL must start with http:// or https:// "
                f"(got {self.public_base_url!r})")
        self.public_base_url = self.public_base_url.rstrip('/')

        if self.is_production and self.accounts_enabled:
            if not self.smtp_host:
                self._warn(
                    "ACCOUNTS_ENABLED with no SMTP_HOST in production: password "
                    "reset and email verification links cannot be delivered "
                    "(and are not logged outside development). Configure SMTP_*.")
            if not self.public_base_url:
                self._warn(
                    "ACCOUNTS_ENABLED with no PUBLIC_BASE_URL in production: "
                    "password reset and email verification links are not sent, "
                    "because the only other "
                    "source for their host is the request's Host header, which "
                    "the requester controls. Set PUBLIC_BASE_URL=https://<your "
                    "domain>.")

        if self.is_production and not self.upload_root:
            self._warn(
                "UPLOAD_ROOT is unset in production; uploads go to "
                "<instance>/uploads next to the app. Set UPLOAD_ROOT to a path on "
                "a persistent volume, or logged-in users' files are lost when "
                "the container or host is replaced.")

        if self.is_production and self.monthly_priority_call_ceiling <= 0:
            # The ceiling applies with billing on AND off (D5-3); during a
            # billing-off free trial it is the only cap on priority spend.
            self._warn(
                "MONTHLY_PRIORITY_CALL_CEILING is unset (<=0): there is NO monthly "
                "cap on priority (Pro-mode / report) requests billed to the "
                "operator's API key, with BILLING_ENABLED on or off, so one "
                "abusive session could run up an unbounded bill. Set "
                "MONTHLY_PRIORITY_CALL_CEILING to a low number.")

        if self.is_production and self.trust_proxy_hops <= 0:
            self._warn(
                "TRUST_PROXY_HOPS is 0 in production (the default). If the app "
                "is exposed directly, that is correct. If it sits behind a "
                "reverse proxy, ProxyFix is off, so get_remote_address() returns "
                "the proxy's IP for every anonymous caller: they all share ONE "
                "rate-limit bucket and a single attacker can exhaust it. Set "
                "TRUST_PROXY_HOPS to the number of trusted proxies in front of "
                "the app (e.g. 1). Keep it 0 only if the app is exposed directly, "
                "since a client could otherwise spoof X-Forwarded-For.")

        # D3-5: a zero/negative timeout would mean "no timeout" in the SDKs,
        # and unbounded retries would pin a worker thread.
        if self.llm_timeout_seconds < 1:
            self._warn("LLM_TIMEOUT_SECONDS must be >= 1; using 120.")
            self.llm_timeout_seconds = 120
        if self.llm_pro_timeout_seconds < self.llm_timeout_seconds:
            self.llm_pro_timeout_seconds = self.llm_timeout_seconds
        if not 0 <= self.llm_max_retries <= 5:
            self._warn("LLM_MAX_RETRIES must be between 0 and 5; using 2.")
            self.llm_max_retries = 2

        if self.max_runs_per_identity < 1:
            self._warn("MAX_RUNS_PER_IDENTITY must be >= 1; using 50.")
            self.max_runs_per_identity = 50

        if self.converse_role not in ('pro', 'flash', 'lite'):
            self._warn(f"CONVERSE_ROLE {self.converse_role!r} unknown; using 'flash'.")
            self.converse_role = 'flash'

        if self.wrangle_role not in ('pro', 'flash', 'lite'):
            self._warn(f"WRANGLE_ROLE {self.wrangle_role!r} unknown; using 'lite'.")
            self.wrangle_role = 'lite'

    def _warn(self, message):
        self.warnings.append(message)
        logger.warning(message)

    # ------------------------------------------------------------------
    @property
    def is_production(self):
        return self.env == 'production'

    @property
    def is_testing(self):
        return self.env == 'testing'

    def external_url(self, path):
        """Absolute URL for the app-relative ``path`` (e.g. a ``url_for``
        result) on ``PUBLIC_BASE_URL``, or ``None`` when that is unset."""
        if not self.public_base_url:
            return None
        return self.public_base_url.rstrip('/') + '/' + path.lstrip('/')

    def resolved_upload_root(self, instance_dir=None):
        """Return the storage root, creating it if needed.

        Unset, it is a stable ``<instance_dir>/uploads`` (D5-9): a fresh temp
        dir per process used to move storage on every restart, so logged-in
        users' durable files vanished and the old root was never reclaimed.
        Testing (or a caller with no instance dir) keeps a throwaway temp dir.
        """
        if not self.upload_root:
            if self.is_testing or not instance_dir:
                self.upload_root = tempfile.mkdtemp(prefix='statlee_')
                return self.upload_root
            self.upload_root = os.path.join(instance_dir, 'uploads')
        os.makedirs(self.upload_root, exist_ok=True)
        return self.upload_root

    def resolved_database_url(self, instance_dir):
        if self.database_url:
            return self.database_url
        # A testing app with no explicit DATABASE_URL must not write the shared
        # instance/statlee.db. Importing the package sets APP_ENV=testing and
        # builds a module-level app; a create_all database left at the instance
        # path would be misread by a later dev/prod boot as a legacy (baseline-
        # schema) database and crash replaying migrations onto columns that
        # create_all already made. Tests that need a real file set DATABASE_URL
        # (the conftest fixtures do); everything else gets a throwaway in-memory
        # database.
        if self.is_testing:
            return 'sqlite://'
        os.makedirs(instance_dir, exist_ok=True)
        db_path = os.path.join(instance_dir, 'statlee.db')
        return 'sqlite:///' + db_path.replace('\\', '/')
