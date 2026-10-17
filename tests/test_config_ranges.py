"""Out-of-range and malformed numeric settings fall back to their defaults
with a warning that names the variable (release audit 2026-10-16, B-15 and
checklist 3.12), instead of breaking every request in an opaque way."""
import logging

import pytest

from statlee.config import Config


@pytest.mark.parametrize('var, attr, bad, default', [
    ('MAX_UPLOAD_MB', 'max_upload_mb', '0', 16),
    ('EXEC_TIMEOUT', 'exec_timeout', '0', 60),
    ('EXEC_TIMEOUT', 'exec_timeout', '-5', 60),
    ('EXEC_MEMORY_MB', 'exec_memory_mb', '0', 2048),
    ('EXEC_OUTPUT_LIMIT', 'exec_output_limit', '-1', 256 * 1024),
    ('EXEC_FILE_LIMIT_MB', 'exec_file_limit_mb', '0', 256),
])
def test_values_below_one_fall_back_with_a_named_warning(monkeypatch, var,
                                                         attr, bad, default):
    monkeypatch.setenv(var, bad)
    cfg = Config.from_env()
    assert getattr(cfg, attr) == default
    assert any(var in w and str(default) in w for w in cfg.warnings)


def test_negative_trust_proxy_hops_becomes_zero(monkeypatch):
    monkeypatch.setenv('TRUST_PROXY_HOPS', '-1')
    cfg = Config.from_env()
    assert cfg.trust_proxy_hops == 0
    assert any('TRUST_PROXY_HOPS' in w for w in cfg.warnings)


def test_valid_values_are_kept_without_warnings(monkeypatch):
    monkeypatch.setenv('EXEC_TIMEOUT', '5')
    monkeypatch.setenv('TRUST_PROXY_HOPS', '1')
    cfg = Config.from_env()
    assert (cfg.exec_timeout, cfg.trust_proxy_hops) == (5, 1)
    assert not any('EXEC_TIMEOUT' in w or 'TRUST_PROXY_HOPS' in w
                   for w in cfg.warnings)


def test_non_integer_value_is_reported_in_the_startup_warnings(monkeypatch):
    """3.12: EXEC_TIMEOUT=abc boots on the default and says which variable."""
    monkeypatch.setenv('EXEC_TIMEOUT', 'abc')
    cfg = Config.from_env()
    assert cfg.exec_timeout == 60
    warning = next(w for w in cfg.warnings if 'EXEC_TIMEOUT' in w)
    assert "'abc'" in warning and '60' in warning
    assert '—' not in warning


def test_a_bad_value_is_reported_once_per_load(monkeypatch):
    monkeypatch.setenv('EXEC_TIMEOUT', 'abc')
    Config.from_env()
    monkeypatch.delenv('EXEC_TIMEOUT')
    cfg = Config.from_env()
    assert not any('EXEC_TIMEOUT' in w for w in cfg.warnings)


def test_create_app_logs_the_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv('EXEC_TIMEOUT', 'abc')
    monkeypatch.setenv('UPLOAD_ROOT', str(tmp_path / 'u'))
    monkeypatch.setenv('DATABASE_URL', 'sqlite://')
    from statlee.app import create_app
    with caplog.at_level(logging.WARNING, logger='statlee'):
        create_app(Config.from_env())
    assert any('EXEC_TIMEOUT' in r.getMessage() for r in caplog.records)
