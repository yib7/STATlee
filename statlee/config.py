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
    openai_max_tokens: int = 8192

    # --- Server -------------------------------------------------------------
    port: int = 5000
    max_upload_mb: int = 16

    # --- Storage / database --------------------------------------------------
    upload_root: str = ''           # empty -> a fresh temp dir is created
    database_url: str = ''          # empty -> sqlite file under instance dir
    storage_backend: str = 'local'  # 'local' | 's3' (7.3)
    s3_bucket: str = ''
    s3_prefix: str = 'statlee'
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

    # --- Model routing (3.4) --------------------------------------------------
    # Default code generation runs on gemini-3.5-flash (cheap, fast, near-parity),
    # with gemini-3.1-flash-lite for the lighter flash/lite roles. The in-app
    # "Pro mode" toggle re-routes code generation to model_pro_max
    # (gemini-3.1-pro-preview) for the hardest analyses — a bigger, stronger, costlier
    # model. Cost ordering: pro_max (2.00/12.00) > pro=flash-default (1.50/9.00) >
    # flash = lite (0.25/1.50). Override any role independently via MODEL_* env.
    model_pro: str = 'gemini-3.5-flash'        # default code-gen / 'draft' role
    # "Pro mode" code-gen upgrade. Pinned to the -preview snapshot deliberately:
    # gemini-3.1-pro ships only under the -preview id (no non-preview GA alias),
    # so this is the current supported snapshot for that tier, not a stale pin.
    model_pro_max: str = 'gemini-3.1-pro-preview'
    model_flash: str = 'gemini-3.1-flash-lite'
    model_flash_lite: str = 'gemini-3.1-flash-lite'
    converse_role: str = 'flash'        # downshift candidate: pro -> flash
    # Conversational data-cleaning ("wrangle") tier. Defaults to the cheapest
    # model since these are short, structured transforms ("delete column 2",
    # "filter for X") — keeps per-edit cost (and the operator's bill) low.
    wrangle_role: str = 'lite'

    # Per-model price estimates (USD per 1M tokens) for the in-app session cost
    # display (3.4). Web-verified Gemini paid-tier rates (ai.google.dev, Jun
    # 2026). DISPLAY ONLY — these never gate, trigger, or change any spend; they
    # only let the client show an approximate session cost. Override by editing
    # this map or constructing Config with your own.
    # Non-Gemini rows are published per-1M rates (verified Jun 2026) for the
    # per-provider default models below — DISPLAY ONLY, never gate spend.
    # Override MODEL_* and this map together if you pin different models.
    model_prices: dict = field(default_factory=lambda: {
        'gemini-3.1-pro-preview': {'input': 2.00, 'output': 12.00},
        'gemini-3.5-flash': {'input': 1.50, 'output': 9.00},
        'gemini-3.1-flash-lite': {'input': 0.25, 'output': 1.50},
        # Anthropic / Claude defaults
        'claude-opus-4-8': {'input': 5.00, 'output': 25.00},
        'claude-sonnet-4-6': {'input': 3.00, 'output': 15.00},
        'claude-haiku-4-5': {'input': 1.00, 'output': 5.00},
        # OpenAI defaults
        'gpt-5.5': {'input': 5.00, 'output': 30.00},
        'gpt-5.4': {'input': 2.50, 'output': 15.00},
        'gpt-5.4-mini': {'input': 0.75, 'output': 4.50},
        'gpt-5.4-nano': {'input': 0.20, 'output': 1.25},
    })

    # --- Analysis tunables -----------------------------------------------------
    feature_selection_threshold: int = 15
    data_page_row_cap: int = 500_000    # refuse to page datasets beyond this (5.8)
    pdf_max_pages: int = 50

    # --- Auth (7.1) -------------------------------------------------------------
    accounts_enabled: bool = True
    require_login: bool = False         # False keeps the anonymous sandbox mode
    require_email_verification: bool = False  # gate accounts until email confirmed

    # --- Billing / cost guardrails (workstream E) -------------------------------
    billing_enabled: bool = False       # turn on credit debit in check_and_debit
    # Hard ceiling on priority (high-tier) requests per calendar month, enforced
    # on the server's own key so nobody can run up an unbounded bill. 0 disables.
    # NOTE: per-process — across multiple workers each enforces its own count.
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
    # X-Forwarded-For. Render fronts the app with exactly one proxy, so this
    # defaults to 1 in production. Keep it 0 anywhere the app is exposed
    # directly, or clients could spoof X-Forwarded-For to dodge IP limits.
    trust_proxy_hops: int = 0

    # --- CSRF (1.5) -----------------------------------------------------------------
    csrf_enabled: bool = True

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
        'anthropic': {'pro': 'claude-opus-4-8', 'pro_max': 'claude-opus-4-8',
                      'flash': 'claude-sonnet-4-6', 'lite': 'claude-haiku-4-5'},
        'openai': {'pro': 'gpt-5.4', 'pro_max': 'gpt-5.5',
                   'flash': 'gpt-5.4-mini', 'lite': 'gpt-5.4-nano'},
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
            openai_max_tokens=_env_int('OPENAI_MAX_TOKENS', 8192),
            port=_env_int('PORT', 5000),
            max_upload_mb=_env_int('MAX_UPLOAD_MB', 16),
            upload_root=os.environ.get('UPLOAD_ROOT', '').strip(),
            database_url=os.environ.get('DATABASE_URL', '').strip(),
            storage_backend=os.environ.get('STORAGE_BACKEND', 'local').strip().lower(),
            s3_bucket=os.environ.get('S3_BUCKET', '').strip(),
            s3_prefix=os.environ.get('S3_PREFIX', 'statlee').strip(),
            file_ttl_seconds=_env_int('FILE_TTL_SECONDS', 7200),
            max_datasets_per_identity=_env_int('MAX_DATASETS_PER_IDENTITY', 10),
            max_bytes_per_identity=_env_int(
                'MAX_BYTES_PER_IDENTITY', 200 * 1024 * 1024),
            max_upload_uncompressed_mb=_env_int(
                'MAX_UPLOAD_UNCOMPRESSED_MB', 512),
            max_upload_cells=_env_int('MAX_UPLOAD_CELLS', 10_000_000),
            sandbox_mode=os.environ.get('SANDBOX_MODE', 'subprocess').strip().lower(),
            runner_image=os.environ.get('RUNNER_IMAGE', 'statlee-runner').strip(),
            sandbox_work_root=os.environ.get('SANDBOX_WORK_ROOT', '').strip(),
            exec_timeout=_env_int('EXEC_TIMEOUT', 60),
            exec_memory_mb=_env_int('EXEC_MEMORY_MB', 2048),
            exec_output_limit=_env_int('EXEC_OUTPUT_LIMIT', 256 * 1024),
            model_pro=os.environ.get('MODEL_PRO', md['pro']).strip(),
            model_pro_max=os.environ.get('MODEL_PRO_MAX', md['pro_max']).strip(),
            model_flash=os.environ.get('MODEL_FLASH', md['flash']).strip(),
            model_flash_lite=os.environ.get('MODEL_FLASH_LITE', md['lite']).strip(),
            converse_role=os.environ.get('CONVERSE_ROLE', 'flash').strip().lower(),
            wrangle_role=os.environ.get('WRANGLE_ROLE', 'lite').strip().lower(),
            feature_selection_threshold=_env_int('FEATURE_SELECTION_THRESHOLD', 15),
            data_page_row_cap=_env_int('DATA_PAGE_ROW_CAP', 500_000),
            pdf_max_pages=_env_int('PDF_MAX_PAGES', 50),
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
            trust_proxy_hops=_env_int(
                'TRUST_PROXY_HOPS',
                1 if os.environ.get('APP_ENV', '').strip().lower() == 'production'
                else 0),
            smtp_host=os.environ.get('SMTP_HOST', '').strip(),
            smtp_port=_env_int('SMTP_PORT', 587),
            smtp_user=os.environ.get('SMTP_USER', '').strip(),
            smtp_password=os.environ.get('SMTP_PASSWORD', ''),
            issue_report_to=os.environ.get('ISSUE_REPORT_TO', '').strip(),
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
        if self.storage_backend not in ('local', 's3'):
            raise ValueError("STORAGE_BACKEND must be 'local' or 's3'")
        if self.storage_backend == 's3' and not self.s3_bucket:
            raise ValueError("STORAGE_BACKEND=s3 requires S3_BUCKET")

        if (self.is_production and self.rate_limit_enabled
                and self.rate_limit_storage_uri.startswith('memory://')):
            self._warn(
                "RATELIMIT_STORAGE_URI is in-memory (memory://) in production — "
                "rate-limit buckets are per-worker and reset on restart, so the "
                "configured limits do not hold across multiple gunicorn workers "
                "and a redeploy clears them, weakening bill-abuse protection. Set "
                "a shared store (e.g. RATELIMIT_STORAGE_URI=redis://...) or pin "
                "WEB_CONCURRENCY=1.")

        if (self.is_production and self.billing_enabled
                and self.monthly_priority_call_ceiling <= 0):
            self._warn(
                "BILLING_ENABLED is on but MONTHLY_PRIORITY_CALL_CEILING is unset "
                "(<=0): there is NO monthly cap on priority requests billed to the "
                "operator's API key, so one abusive session could run up an "
                "unbounded bill. Set MONTHLY_PRIORITY_CALL_CEILING to a low number.")

        if self.is_production and self.trust_proxy_hops <= 0:
            self._warn(
                "TRUST_PROXY_HOPS is 0 in production. If the app sits behind a "
                "reverse proxy, ProxyFix is off, so get_remote_address() returns "
                "the proxy's IP for every anonymous caller: they all share ONE "
                "rate-limit bucket and a single attacker can exhaust it. Set "
                "TRUST_PROXY_HOPS to the number of trusted proxies in front of "
                "the app (e.g. 1). Keep it 0 only if the app is exposed directly, "
                "since a client could otherwise spoof X-Forwarded-For.")

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

    def resolved_upload_root(self):
        """Return the storage root, creating a temp dir when unset."""
        if self.upload_root:
            os.makedirs(self.upload_root, exist_ok=True)
            return self.upload_root
        self.upload_root = tempfile.mkdtemp(prefix='statlee_')
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
