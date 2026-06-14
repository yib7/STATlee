"""Config validation (1.3): fail-fast on hard requirements, soft warnings
elsewhere, and provider/sandbox/storage sanity checks."""
import pytest

from config import Config


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
