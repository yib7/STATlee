"""SP1: one Alembic revision adds the nullable workbook link columns
(run_id, dataset_version, model) to analysis_runs, and an existing history
row survives the upgrade with the new columns NULL."""
import sqlite3

from sqlalchemy import inspect as sa_inspect
from test_migrations import _dev_config, dev_app_factory  # noqa: F401

from statlee import app as app_module
from statlee.extensions import db

PREVIOUS_HEAD = '733d2d970515'


def _columns(application):
    with application.app_context():
        return {c['name']: c for c in
                sa_inspect(db.engine).get_columns('analysis_runs')}


def test_head_has_nullable_workbook_columns(tmp_path, dev_app_factory):  # noqa: F811
    cols = _columns(dev_app_factory(_dev_config(tmp_path, 'wb.db')))
    for name in ('run_id', 'dataset_version', 'model'):
        assert name in cols, name
        assert cols[name]['nullable'] is True
    assert cols['run_id']['type'].length == 16
    assert cols['model']['type'].length == 64


def test_upgrade_from_previous_head_keeps_history_rows(tmp_path,
                                                       dev_app_factory):  # noqa: F811
    from flask_migrate import downgrade

    db_path = tmp_path / 'old.db'
    application = dev_app_factory(_dev_config(tmp_path, 'old.db'))
    with application.app_context():
        downgrade(directory=app_module._MIGRATIONS_DIR, revision=PREVIOUS_HEAD)
        db.engine.dispose()
    con = sqlite3.connect(str(db_path))
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info(analysis_runs)')}
        assert 'run_id' not in cols
        con.execute("INSERT INTO users (id, email, password_hash, plan, "
                    "credits, email_verified) VALUES (1, 'a@b.c', 'x', "
                    "'free', 0, 0)")
        con.execute("INSERT INTO analysis_runs (user_id, prompt) "
                    "VALUES (1, 'kept')")
        con.commit()
    finally:
        con.close()
    app_module._upgraded_uris.clear()
    application = dev_app_factory(_dev_config(tmp_path, 'old.db'))
    with application.app_context():
        from statlee.models import AnalysisRun
        row = db.session.execute(db.select(AnalysisRun)).scalar_one()
        assert row.prompt == 'kept'
        assert row.run_id is None and row.dataset_version is None
        assert row.model is None
        db.engine.dispose()
