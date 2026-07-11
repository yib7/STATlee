"""Upgrade-on-boot schema migrations (P1-6).

``db.create_all()`` only creates missing tables, never new columns, so a
deployment whose database predates a column addition broke on upgrade. The
app factory now runs Alembic migrations at boot for every non-testing env:

- fresh DB (no tables)            -> migrate to head;
- legacy create_all() DB          -> stamp the baseline revision, upgrade;
- already-migrated DB             -> plain upgrade to head.

Testing env keeps plain ``create_all()`` (fast, throwaway DBs).
"""
import sqlite3

import pytest
from sqlalchemy import inspect as sa_inspect

from statlee import app as app_module
from statlee.config import Config
from statlee.extensions import db


def _dev_config(tmp_path, db_name):
    cfg = Config(
        env='development',
        upload_root=str(tmp_path / 'uploads'),
        database_url='sqlite:///' + str(tmp_path / db_name).replace('\\', '/'),
        flask_secret_key='test-secret-key',
        rate_limit_enabled=False,
        csrf_enabled=True,
        accounts_enabled=True,
        require_login=False,
    )
    cfg.validate()
    return cfg


def _table_names(application):
    with application.app_context():
        return set(sa_inspect(db.engine).get_table_names())


def _stamped_revision(tmp_path, db_name):
    con = sqlite3.connect(str(tmp_path / db_name))
    try:
        return con.execute('SELECT version_num FROM alembic_version').fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def dev_app_factory(fake_llm):
    """Build a create_app() for a dev config, injecting the fake LLM after."""
    from statlee import llm
    from statlee.app import create_app

    def _make(cfg):
        application = create_app(cfg)
        llm.set_service(fake_llm)
        return application

    return _make


def test_fresh_db_is_migrated_to_head(tmp_path, dev_app_factory):
    """A brand-new database gets the full schema plus the alembic stamp."""
    cfg = _dev_config(tmp_path, 'fresh.db')
    application = dev_app_factory(cfg)

    tables = _table_names(application)
    assert {'users', 'analysis_runs', 'issue_reports'} <= tables
    assert 'alembic_version' in tables
    assert (_stamped_revision(tmp_path, 'fresh.db')
            == app_module.BASELINE_REVISION)


def test_legacy_create_all_db_is_stamped_and_data_survives(
        tmp_path, dev_app_factory):
    """A pre-migration DB (tables, no alembic_version) is stamped at the
    baseline and upgraded without touching existing rows."""
    from flask import Flask

    from statlee.models import User

    uri = 'sqlite:///' + str(tmp_path / 'legacy.db').replace('\\', '/')
    seed = Flask('seed')
    seed.config['SQLALCHEMY_DATABASE_URI'] = uri
    db.init_app(seed)
    with seed.app_context():
        db.create_all()                      # what old deployments did
        user = User(email='veteran@example.com')
        user.set_password('hunter2!')
        db.session.add(user)
        db.session.commit()
        assert 'alembic_version' not in sa_inspect(db.engine).get_table_names()
        db.engine.dispose()

    cfg = _dev_config(tmp_path, 'legacy.db')
    application = dev_app_factory(cfg)

    tables = _table_names(application)
    assert 'alembic_version' in tables
    assert (_stamped_revision(tmp_path, 'legacy.db')
            == app_module.BASELINE_REVISION)
    with application.app_context():
        survivor = db.session.query(User).filter_by(
            email='veteran@example.com').one()
        assert survivor.check_password('hunter2!')


def test_already_migrated_db_boots_again_cleanly(tmp_path, dev_app_factory):
    """Second boot on the same DB is a no-op upgrade, not an error."""
    cfg = _dev_config(tmp_path, 'again.db')
    dev_app_factory(cfg)
    # Simulate a fresh process: forget that this URI was already upgraded.
    app_module._upgraded_uris.discard(cfg.database_url)

    application = dev_app_factory(_dev_config(tmp_path, 'again.db'))
    assert 'users' in _table_names(application)
    assert (_stamped_revision(tmp_path, 'again.db')
            == app_module.BASELINE_REVISION)


def test_testing_env_still_uses_create_all(app):
    """The test suite keeps fast create_all() DBs: no alembic_version."""
    tables = _table_names(app)
    assert 'users' in tables
    assert 'alembic_version' not in tables
