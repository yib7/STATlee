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


def _head_revision():
    """The current Alembic head, read from the migrations dir. Boot upgrades to
    head, so a DB stamped after boot should carry this (not the baseline, once
    later migrations exist)."""
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    cfg = AlembicConfig()
    cfg.set_main_option('script_location', app_module._MIGRATIONS_DIR)
    return ScriptDirectory.from_config(cfg).get_current_head()


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
    # A fresh DB is migrated all the way to head, not just to the baseline.
    assert _stamped_revision(tmp_path, 'fresh.db') == _head_revision()


def test_legacy_create_all_db_is_stamped_and_data_survives(
        tmp_path, dev_app_factory):
    """A pre-migration DB (tables, no alembic_version) is stamped at the
    baseline and upgraded without touching existing rows."""
    from flask import Flask
    from sqlalchemy import text

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
        # create_all() builds today's full schema, but a genuinely old DB
        # predates the post-baseline columns. Strip them so the boot upgrade
        # has real ALTERs to run (and would surface a broken migration). The
        # index on password_reset_token must go before its column can be dropped.
        db.session.execute(text('DROP INDEX IF EXISTS ix_users_password_reset_token'))
        for col in ('credits_month', 'token_issued_at',
                    'password_reset_token', 'reset_token_issued_at'):
            db.session.execute(text(f'ALTER TABLE users DROP COLUMN {col}'))
        for col in ('run_id', 'dataset_version', 'model'):   # SP1 workbook link
            db.session.execute(
                text(f'ALTER TABLE analysis_runs DROP COLUMN {col}'))
        db.session.commit()
        assert 'alembic_version' not in sa_inspect(db.engine).get_table_names()
        db.engine.dispose()

    cfg = _dev_config(tmp_path, 'legacy.db')
    application = dev_app_factory(cfg)

    tables = _table_names(application)
    assert 'alembic_version' in tables
    # Stamped at the baseline, then upgraded to head (which adds the columns
    # we stripped above), so the recorded revision is head.
    assert _stamped_revision(tmp_path, 'legacy.db') == _head_revision()
    with application.app_context():
        survivor = db.session.query(User).filter_by(
            email='veteran@example.com').one()
        assert survivor.check_password('hunter2!')
        # The migration actually restored the post-baseline columns.
        cols = {c['name'] for c in sa_inspect(db.engine).get_columns('users')}
        assert {'credits_month', 'password_reset_token'} <= cols


def test_already_migrated_db_boots_again_cleanly(tmp_path, dev_app_factory):
    """Second boot on the same DB is a no-op upgrade, not an error."""
    cfg = _dev_config(tmp_path, 'again.db')
    dev_app_factory(cfg)
    # Simulate a fresh process: forget that this URI was already upgraded.
    app_module._upgraded_uris.discard(cfg.database_url)

    application = dev_app_factory(_dev_config(tmp_path, 'again.db'))
    assert 'users' in _table_names(application)
    assert _stamped_revision(tmp_path, 'again.db') == _head_revision()


def _seed_create_all_db(tmp_path, db_name, drop_columns=()):
    """A DB built by create_all() from today's models (no alembic_version),
    optionally with some post-baseline user columns stripped, holding one
    user row."""
    from flask import Flask
    from sqlalchemy import text

    from statlee.models import User

    uri = 'sqlite:///' + str(tmp_path / db_name).replace('\\', '/')
    seed = Flask('seed')
    seed.config['SQLALCHEMY_DATABASE_URI'] = uri
    db.init_app(seed)
    with seed.app_context():
        db.create_all()
        user = User(email='veteran@example.com')
        user.set_password('hunter2!')
        db.session.add(user)
        db.session.commit()
        if 'password_reset_token' in drop_columns:
            db.session.execute(
                text('DROP INDEX IF EXISTS ix_users_password_reset_token'))
        for col in drop_columns:
            db.session.execute(text(f'ALTER TABLE users DROP COLUMN {col}'))
        db.session.commit()
        db.engine.dispose()


def _tables_in_file(tmp_path, db_name):
    con = sqlite3.connect(str(tmp_path / db_name))
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_unstamped_db_already_at_head_is_stamped_head(tmp_path, dev_app_factory):
    """D5-1: a create_all() DB built from today's models already has every
    post-baseline column. Stamping it at the baseline and replaying the head
    migration crashed with `duplicate column name: credits_month`, and the
    stamp survived the failure, so every later boot crashed the same way. A
    schema that already matches head must be stamped head instead."""
    _seed_create_all_db(tmp_path, 'athead.db')

    application = dev_app_factory(_dev_config(tmp_path, 'athead.db'))

    assert _stamped_revision(tmp_path, 'athead.db') == _head_revision()
    with application.app_context():
        from statlee.models import User
        assert db.session.query(User).filter_by(
            email='veteran@example.com').one().check_password('hunter2!')


def test_failed_legacy_upgrade_leaves_no_stamp(tmp_path, dev_app_factory):
    """D5-1: when the legacy upgrade fails (here a half-migrated DB that has
    credits_month but not the other head columns), the baseline stamp must be
    rolled back. Otherwise SQLite's non-transactional DDL leaves the DB stamped
    and every later boot takes the 'already migrated' path into the same
    crash."""
    _seed_create_all_db(tmp_path, 'half.db', drop_columns=(
        'token_issued_at', 'password_reset_token', 'reset_token_issued_at'))

    with pytest.raises(Exception, match='credits_month'):
        dev_app_factory(_dev_config(tmp_path, 'half.db'))

    assert 'alembic_version' not in _tables_in_file(tmp_path, 'half.db')


@pytest.mark.parametrize('argv, expected', [
    (['/venv/bin/flask', '--app', 'wsgi', 'db', 'stamp', 'head'], True),
    (['C:\\venv\\Scripts\\flask.exe', 'db', 'current'], True),
    (['/usr/lib/python3/site-packages/flask/__main__.py', '-A', 'wsgi', 'db',
      'downgrade'], True),
    (['/venv/bin/flask', '--app=wsgi', 'statlee-doctor'], True),
    (['/venv/bin/flask', '--app', 'wsgi', 'grant-credits', 'a@b.c', '1'], False),
    (['/venv/bin/flask', '--app', 'db', 'run'], False),
    (['/venv/bin/gunicorn', 'wsgi:app', 'db'], False),
    (['wsgi.py'], False),
])
def test_schema_repair_command_detection(argv, expected):
    assert app_module._is_schema_repair_command(argv) is expected


def test_flask_db_invocation_skips_boot_migration(tmp_path, dev_app_factory,
                                                  monkeypatch):
    """D5-1: `flask --app wsgi db stamp head` imports wsgi, which builds the
    app. The boot upgrade must not run first, or a DB it cannot upgrade makes
    the Alembic CLI (the repair tool) crash before its command executes."""
    from flask_migrate import stamp

    # The crash-looping state: stamped at the baseline, columns already at head.
    _seed_create_all_db(tmp_path, 'poisoned.db')
    con = sqlite3.connect(str(tmp_path / 'poisoned.db'))
    con.execute('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)')
    con.execute("INSERT INTO alembic_version VALUES (?)", (app_module.BASELINE_REVISION,))
    con.commit()
    con.close()

    monkeypatch.setattr(app_module.sys, 'argv',
                        ['/venv/bin/flask', '--app', 'wsgi', 'db', 'stamp', 'head'])
    repair_app = dev_app_factory(_dev_config(tmp_path, 'poisoned.db'))  # no crash
    assert _stamped_revision(tmp_path, 'poisoned.db') == app_module.BASELINE_REVISION

    # What `db stamp head` then does:
    monkeypatch.setenv('STATLEE_INPROCESS_MIGRATION', '1')
    with repair_app.app_context():
        stamp(revision='head')
    monkeypatch.delenv('STATLEE_INPROCESS_MIGRATION')

    # A normal boot is healthy again.
    monkeypatch.setattr(app_module.sys, 'argv', ['wsgi.py'])
    app_module._upgraded_uris.clear()
    dev_app_factory(_dev_config(tmp_path, 'poisoned.db'))
    assert _stamped_revision(tmp_path, 'poisoned.db') == _head_revision()


def test_skip_boot_migrate_env_leaves_db_untouched(tmp_path, dev_app_factory,
                                                   monkeypatch):
    monkeypatch.setenv('STATLEE_SKIP_BOOT_MIGRATE', '1')
    dev_app_factory(_dev_config(tmp_path, 'skipped.db'))
    assert 'alembic_version' not in _tables_in_file(tmp_path, 'skipped.db')


def _drift_db(tmp_path, db_name, dev_app_factory):
    """Head-stamped DB missing the baseline columns email_verified and
    verification_token: the state the developer's local instance DB is in."""
    from sqlalchemy import text
    application = dev_app_factory(_dev_config(tmp_path, db_name))
    with application.app_context():
        db.session.execute(text('DROP INDEX IF EXISTS ix_users_verification_token'))
        db.session.execute(text('ALTER TABLE users DROP COLUMN verification_token'))
        db.session.execute(text('ALTER TABLE users DROP COLUMN email_verified'))
        db.session.commit()
        db.engine.dispose()
    app_module._upgraded_uris.clear()


def test_stamped_db_missing_columns_logs_actionable_error(tmp_path, dev_app_factory,
                                                         caplog):
    """D5-2: a DB stamped at head that lacks model columns used to boot
    'healthy' and then 500 on every auth query. Boot must detect it and say
    exactly what is missing and how to fix it."""
    _drift_db(tmp_path, 'drift.db', dev_app_factory)

    with caplog.at_level('ERROR', logger='statlee'):
        dev_app_factory(_dev_config(tmp_path, 'drift.db'))   # dev: logs, boots

    text_ = ' '.join(r.getMessage() for r in caplog.records
                     if r.levelname == 'ERROR')
    assert 'users.email_verified' in text_
    assert 'users.verification_token' in text_
    assert 'statlee-doctor' in text_


def test_stamped_db_missing_columns_fails_boot_in_production(tmp_path,
                                                             dev_app_factory):
    _drift_db(tmp_path, 'drift_prod.db', dev_app_factory)
    cfg = _dev_config(tmp_path, 'drift_prod.db')
    cfg.env = 'production'
    cfg.gemini_api_key = 'not-a-real-key'
    with pytest.raises(RuntimeError, match='users.email_verified'):
        dev_app_factory(cfg)


def test_models_match_migration_head(tmp_path, dev_app_factory):
    """D5-15: tests build their DBs with create_all(), which always matches
    the models, so a model change without a migration passed the whole suite
    and then 500'd in production. A DB migrated to head must have exactly the
    models' schema (the in-process equivalent of `flask db check`)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    application = dev_app_factory(_dev_config(tmp_path, 'parity.db'))
    with application.app_context(), db.engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), db.metadata)
    assert diff == [], (
        'models and the migration head disagree; run `flask --app wsgi db '
        f'migrate -m "..."` and commit the revision. Diff: {diff}')


def test_testing_env_still_uses_create_all(app):
    """The test suite keeps fast create_all() DBs: no alembic_version."""
    tables = _table_names(app)
    assert 'users' in tables
    assert 'alembic_version' not in tables


def test_boot_upgrade_keeps_app_logging(tmp_path, dev_app_factory):
    """A boot upgrade must not let alembic's fileConfig reconfigure the root
    logger: app INFO logging and the RequestIdFilter have to survive.

    Seeds the state ``_setup_logging`` leaves behind (INFO root plus a handler
    carrying the RequestIdFilter), then asserts the boot upgrade preserves it.
    Without the env.py guard, alembic's fileConfig resets root to WARNING and
    drops the filter, so both assertions would fail.
    """
    import logging

    from statlee.app import RequestIdFilter

    root = logging.getLogger()
    saved_level = root.level
    saved_filters = {id(h): list(h.filters) for h in root.handlers}
    try:
        root.setLevel(logging.INFO)
        for h in root.handlers:
            if not any(isinstance(f, RequestIdFilter) for f in h.filters):
                h.addFilter(RequestIdFilter())
        assert root.handlers  # sanity: there is a handler to protect

        cfg = _dev_config(tmp_path, 'logging.db')
        dev_app_factory(cfg)  # runs the fresh-DB boot upgrade

        assert root.isEnabledFor(logging.INFO)
        assert any(isinstance(f, RequestIdFilter)
                   for h in root.handlers for f in h.filters)
    finally:
        root.setLevel(saved_level)
        for h in root.handlers:
            original = saved_filters.get(id(h))
            if original is not None:
                h.filters[:] = original
