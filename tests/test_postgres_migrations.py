"""Boot migrations against a real PostgreSQL (D5-11).

PostgreSQL is the documented production database, but migrations were only
ever exercised on SQLite (``batch_alter_table``). This runs only when
``STATLEE_TEST_POSTGRES_URL`` points at a disposable database (the CI
``postgres`` job provides one); it drops every table it creates.
"""
import os

import pytest

PG_URL = os.environ.get('STATLEE_TEST_POSTGRES_URL', '').strip()

pytestmark = pytest.mark.skipif(
    not PG_URL, reason='STATLEE_TEST_POSTGRES_URL not set (CI postgres job only)')


def _boot(tmp_path, fake_llm):
    from statlee import llm
    from statlee.app import create_app
    from statlee.config import Config
    cfg = Config(env='development', database_url=PG_URL,
                 upload_root=str(tmp_path / 'uploads'),
                 flask_secret_key='test-secret-key', rate_limit_enabled=False)
    cfg.validate()
    application = create_app(cfg)
    llm.set_service(fake_llm)
    return application


@pytest.fixture
def clean_pg():
    from sqlalchemy import create_engine, text
    engine = create_engine(PG_URL)

    def _drop():
        with engine.begin() as conn:
            conn.execute(text('DROP SCHEMA public CASCADE'))
            conn.execute(text('CREATE SCHEMA public'))

    _drop()
    yield
    _drop()
    engine.dispose()


def test_postgres_fresh_db_migrates_to_head_and_matches_models(tmp_path, fake_llm,
                                                               clean_pg):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from statlee import app as app_module
    from statlee.extensions import db
    from statlee.models import User

    application = _boot(tmp_path, fake_llm)
    with application.app_context():
        current, head, missing = app_module.schema_report()
        assert current == head
        assert missing == []
        with db.engine.connect() as conn:
            assert compare_metadata(MigrationContext.configure(conn),
                                    db.metadata) == []
        user = User(email='pg@example.com')
        user.set_password('password123')
        db.session.add(user)
        db.session.commit()
        db.session.remove()
        db.engine.dispose()

    # A second process booting the same DB is a clean no-op upgrade.
    app_module._upgraded_uris.discard(PG_URL)
    again = _boot(tmp_path, fake_llm)
    with again.app_context():
        assert db.session.execute(
            db.select(User).filter_by(email='pg@example.com')).scalar_one()
        db.session.remove()
        db.engine.dispose()
