"""Centralized, validated configuration (roadmap 1.3).

Reads and validates the environment exactly once. ``app.py`` builds the Flask
app from a ``Config`` instance; every tunable lives here instead of being a
literal buried in a route body.
"""
import logging
import os
import tempfile
from dataclasses import dataclass, field

logger = logging.getLogger('statly.config')

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

    # --- Secrets / providers ------------------------------------------------
    gemini_api_key: str = ''
    flask_secret_key: str = ''
    app_password: str = ''          # legacy single-password gate (optional)

    # --- Server -------------------------------------------------------------
    port: int = 5000
    max_upload_mb: int = 16

    # --- Storage / database --------------------------------------------------
    upload_root: str = ''           # empty -> a fresh temp dir is created
    database_url: str = ''          # empty -> sqlite file under instance dir
    storage_backend: str = 'local'  # 'local' | 's3' (7.3)
    s3_bucket: str = ''
    s3_prefix: str = 'statly'
    file_ttl_seconds: int = 7200    # anonymous-file cleanup window (2h)

    # --- Execution sandbox (Tier 0) ------------------------------------------
    sandbox_mode: str = 'subprocess'   # 'subprocess' | 'docker' (0.3)
    runner_image: str = 'statly-runner'
    exec_timeout: int = 60
    exec_memory_mb: int = 2048
    exec_output_limit: int = 256 * 1024   # truncate captured stdout/stderr

    # --- Model routing (3.4) --------------------------------------------------
    model_pro: str = 'gemini-3.1-pro-preview'
    model_flash: str = 'gemini-3-flash-preview'
    model_flash_lite: str = 'gemini-3.1-flash-lite-preview'
    converse_role: str = 'flash'        # downshift candidate: pro -> flash

    # --- Analysis tunables -----------------------------------------------------
    feature_selection_threshold: int = 15
    data_page_row_cap: int = 500_000    # refuse to page datasets beyond this (5.8)
    pdf_max_pages: int = 50

    # --- Auth (7.1) -------------------------------------------------------------
    accounts_enabled: bool = True
    require_login: bool = False         # False keeps the anonymous sandbox mode

    # --- Rate limits (1.4) --------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_default: str = '120 per minute'
    rate_limit_run: str = '10 per minute'
    rate_limit_chat: str = '20 per minute'
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

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls):
        cfg = cls(
            env=os.environ.get('APP_ENV', 'development').strip().lower(),
            gemini_api_key=os.environ.get('GEMINI_API_KEY', '').strip(),
            flask_secret_key=os.environ.get('FLASK_SECRET_KEY', '').strip(),
            app_password=os.environ.get('PASSWORD', '').strip(),
            port=_env_int('PORT', 5000),
            max_upload_mb=_env_int('MAX_UPLOAD_MB', 16),
            upload_root=os.environ.get('UPLOAD_ROOT', '').strip(),
            database_url=os.environ.get('DATABASE_URL', '').strip(),
            storage_backend=os.environ.get('STORAGE_BACKEND', 'local').strip().lower(),
            s3_bucket=os.environ.get('S3_BUCKET', '').strip(),
            s3_prefix=os.environ.get('S3_PREFIX', 'statly').strip(),
            file_ttl_seconds=_env_int('FILE_TTL_SECONDS', 7200),
            sandbox_mode=os.environ.get('SANDBOX_MODE', 'subprocess').strip().lower(),
            runner_image=os.environ.get('RUNNER_IMAGE', 'statly-runner').strip(),
            exec_timeout=_env_int('EXEC_TIMEOUT', 60),
            exec_memory_mb=_env_int('EXEC_MEMORY_MB', 2048),
            model_pro=os.environ.get('MODEL_PRO', cls.model_pro).strip(),
            model_flash=os.environ.get('MODEL_FLASH', cls.model_flash).strip(),
            model_flash_lite=os.environ.get('MODEL_FLASH_LITE', cls.model_flash_lite).strip(),
            converse_role=os.environ.get('CONVERSE_ROLE', 'flash').strip().lower(),
            feature_selection_threshold=_env_int('FEATURE_SELECTION_THRESHOLD', 15),
            data_page_row_cap=_env_int('DATA_PAGE_ROW_CAP', 500_000),
            pdf_max_pages=_env_int('PDF_MAX_PAGES', 50),
            accounts_enabled=_env_bool('ACCOUNTS_ENABLED', True),
            require_login=_env_bool('REQUIRE_LOGIN', False),
            rate_limit_enabled=_env_bool('RATE_LIMIT_ENABLED', True),
            rate_limit_default=os.environ.get('RATE_LIMIT_DEFAULT', '120 per minute'),
            rate_limit_run=os.environ.get('RATE_LIMIT_RUN', '10 per minute'),
            rate_limit_chat=os.environ.get('RATE_LIMIT_CHAT', '20 per minute'),
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
    def validate(self):
        """Fail fast on hard requirements; warn loudly on soft ones."""
        if self.env not in VALID_ENVS:
            raise ValueError(
                f"APP_ENV must be one of {VALID_ENVS}, got {self.env!r}")

        if self.is_production:
            missing = []
            if not self.gemini_api_key:
                missing.append('GEMINI_API_KEY')
            if not self.flask_secret_key:
                missing.append('FLASK_SECRET_KEY')
            if missing:
                raise ValueError(
                    "Refusing to start in production without: "
                    + ", ".join(missing))
        else:
            if not self.gemini_api_key and self.env != 'testing':
                self._warn(
                    "GEMINI_API_KEY is not set — LLM endpoints will return "
                    "errors until it is configured.")
            if not self.flask_secret_key and self.env != 'testing':
                self._warn(
                    "FLASK_SECRET_KEY not set — using a random key. Logins "
                    "reset on restart and fail across multiple workers.")

        if self.sandbox_mode not in ('subprocess', 'docker'):
            raise ValueError("SANDBOX_MODE must be 'subprocess' or 'docker'")
        if self.is_production and self.sandbox_mode == 'subprocess':
            self._warn(
                "SANDBOX_MODE=subprocess in production — generated code runs as "
                "the app user with full filesystem read access. Use "
                "SANDBOX_MODE=docker for real isolation in production.")
        if self.storage_backend not in ('local', 's3'):
            raise ValueError("STORAGE_BACKEND must be 'local' or 's3'")
        if self.storage_backend == 's3' and not self.s3_bucket:
            raise ValueError("STORAGE_BACKEND=s3 requires S3_BUCKET")

        if self.converse_role not in ('pro', 'flash', 'lite'):
            self._warn(f"CONVERSE_ROLE {self.converse_role!r} unknown; using 'flash'.")
            self.converse_role = 'flash'

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
        self.upload_root = tempfile.mkdtemp(prefix='statly_')
        return self.upload_root

    def resolved_database_url(self, instance_dir):
        if self.database_url:
            return self.database_url
        os.makedirs(instance_dir, exist_ok=True)
        db_path = os.path.join(instance_dir, 'statly.db')
        return 'sqlite:///' + db_path.replace('\\', '/')
