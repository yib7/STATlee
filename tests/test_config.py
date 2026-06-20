"""Config validation (1.3): fail-fast on hard requirements, soft warnings
elsewhere, and provider/sandbox/storage sanity checks."""
import pytest

from statlee.config import Config


def test_invalid_env_raises():
    with pytest.raises(ValueError):
        Config(env='staging').validate()


def test_production_requires_secrets():
    cfg = Config(env='production', gemini_api_key='', flask_secret_key='')
    with pytest.raises(ValueError) as exc:
        cfg.validate()
    assert 'GEMINI_API_KEY' in str(exc.value)
    assert 'FLASK_SECRET_KEY' in str(exc.value)


def test_production_ok_with_secrets():
    cfg = Config(env='production', gemini_api_key='k', flask_secret_key='s')
    cfg.validate()   # must not raise
    assert cfg.is_production


def test_testing_env_is_quiet_without_keys():
    cfg = Config(env='testing')
    cfg.validate()
    assert cfg.warnings == []        # testing suppresses the missing-key warnings


def test_invalid_sandbox_mode_raises():
    with pytest.raises(ValueError):
        Config(env='development', sandbox_mode='vm').validate()


def test_s3_backend_requires_bucket():
    with pytest.raises(ValueError):
        Config(env='development', storage_backend='s3', s3_bucket='').validate()


def test_unknown_converse_role_defaults_to_flash():
    cfg = Config(env='development', converse_role='ultra')
    cfg.validate()
    assert cfg.converse_role == 'flash'


def test_wrangle_role_defaults_to_lite():
    # Conversational data-cleaning runs on the cheapest tier by default.
    assert Config.wrangle_role == 'lite'
    cfg = Config(env='development')
    cfg.validate()
    assert cfg.wrangle_role == 'lite'


def test_unknown_wrangle_role_defaults_to_lite():
    cfg = Config(env='development', wrangle_role='ultra')
    cfg.validate()
    assert cfg.wrangle_role == 'lite'


def test_wrangle_role_from_env(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'testing')
    monkeypatch.setenv('WRANGLE_ROLE', 'flash')
    cfg = Config.from_env()
    assert cfg.wrangle_role == 'flash'


def test_production_subprocess_sandbox_warns():
    cfg = Config(env='production', gemini_api_key='k', flask_secret_key='s',
                 sandbox_mode='subprocess')
    cfg.validate()
    assert any('SANDBOX_MODE=subprocess' in w for w in cfg.warnings)


def test_production_docker_sandbox_is_quiet():
    cfg = Config(env='production', gemini_api_key='k', flask_secret_key='s',
                 sandbox_mode='docker')
    cfg.validate()
    assert not any('SANDBOX_MODE' in w for w in cfg.warnings)


def test_production_requires_gemini_key():
    cfg = Config(env='production', gemini_api_key='', flask_secret_key='s')
    with pytest.raises(ValueError) as exc:
        cfg.validate()
    assert 'GEMINI_API_KEY' in str(exc.value)


def test_production_ok_with_gemini_key():
    cfg = Config(env='production', gemini_api_key='k', flask_secret_key='s')
    cfg.validate()   # must not raise
    assert cfg.is_production


def test_model_defaults_are_gemini(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'testing')
    for var in ('MODEL_PRO', 'MODEL_FLASH', 'MODEL_FLASH_LITE'):
        monkeypatch.delenv(var, raising=False)
    cfg = Config.from_env()
    assert cfg.model_pro == Config.model_pro
    assert cfg.model_pro.startswith('gemini')


def test_explicit_model_env_overrides_default(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'testing')
    monkeypatch.setenv('MODEL_PRO', 'gemini-custom')
    cfg = Config.from_env()
    assert cfg.model_pro == 'gemini-custom'


def test_limiter_storage_uri_from_env(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'testing')
    monkeypatch.setenv('RATELIMIT_STORAGE_URI', 'redis://localhost:6379/0')
    monkeypatch.delenv('REDIS_URL', raising=False)
    cfg = Config.from_env()
    assert cfg.rate_limit_storage_uri == 'redis://localhost:6379/0'


def test_limiter_storage_uri_falls_back_to_redis_url(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'testing')
    monkeypatch.delenv('RATELIMIT_STORAGE_URI', raising=False)
    monkeypatch.setenv('REDIS_URL', 'redis://cache:6379/1')
    cfg = Config.from_env()
    assert cfg.rate_limit_storage_uri == 'redis://cache:6379/1'


def test_billing_without_ceiling_warns_in_production():
    # Money-safety guardrail: billing on + no ceiling = unbounded operator bill.
    cfg = Config(env='production', gemini_api_key='k', flask_secret_key='s',
                 billing_enabled=True, monthly_priority_call_ceiling=0)
    cfg.validate()
    assert any('ceiling' in w.lower() for w in cfg.warnings)


def test_billing_with_ceiling_is_quiet():
    cfg = Config(env='production', gemini_api_key='k', flask_secret_key='s',
                 billing_enabled=True, monthly_priority_call_ceiling=500)
    cfg.validate()
    assert not any('MONTHLY_PRIORITY_CALL_CEILING' in w for w in cfg.warnings)


def test_billing_off_does_not_warn_about_ceiling():
    cfg = Config(env='production', gemini_api_key='k', flask_secret_key='s',
                 billing_enabled=False, monthly_priority_call_ceiling=0)
    cfg.validate()
    assert not any('MONTHLY_PRIORITY_CALL_CEILING' in w for w in cfg.warnings)


def test_production_memory_limiter_warns():
    cfg = Config(env='production', gemini_api_key='k', flask_secret_key='s',
                 rate_limit_storage_uri='memory://')
    cfg.validate()
    assert any('memory://' in w for w in cfg.warnings)


def test_production_shared_limiter_is_quiet():
    cfg = Config(env='production', gemini_api_key='k', flask_secret_key='s',
                 rate_limit_storage_uri='redis://localhost:6379')
    cfg.validate()
    assert not any('RATELIMIT_STORAGE_URI' in w for w in cfg.warnings)
