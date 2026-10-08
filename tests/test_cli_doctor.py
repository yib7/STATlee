"""`flask statlee-doctor` (D5-2) and grant-credits input bounds."""
from sqlalchemy import text

from statlee import app as app_module
from statlee.config import Config
from statlee.extensions import db


def _dev_app(tmp_path, fake_llm, name):
    from statlee import llm
    cfg = Config(
        env='development',
        upload_root=str(tmp_path / 'uploads'),
        database_url='sqlite:///' + str(tmp_path / name).replace('\\', '/'),
        flask_secret_key='test-secret-key', rate_limit_enabled=False)
    cfg.validate()
    application = app_module.create_app(cfg)
    llm.set_service(fake_llm)
    return application


def test_doctor_reports_ok_on_a_migrated_db(tmp_path, fake_llm):
    application = _dev_app(tmp_path, fake_llm, 'ok.db')
    result = application.test_cli_runner().invoke(args=['statlee-doctor'])
    assert result.exit_code == 0, result.output
    assert 'Schema OK' in result.output


def test_doctor_names_missing_columns(tmp_path, fake_llm):
    application = _dev_app(tmp_path, fake_llm, 'drift.db')
    with application.app_context():
        db.session.execute(text('ALTER TABLE users DROP COLUMN email_verified'))
        db.session.commit()
        db.engine.dispose()
    result = application.test_cli_runner().invoke(args=['statlee-doctor'])
    assert result.exit_code != 0
    assert 'users.email_verified' in result.output


def test_grant_credits_rejects_non_positive_amounts(app):
    from statlee.models import User
    with app.app_context():
        user = User(email='grant@example.com')
        user.set_password('password123')
        db.session.add(user)
        db.session.commit()
    runner = app.test_cli_runner()
    for amount in ('0', '-5'):
        result = runner.invoke(args=['grant-credits', 'grant@example.com', '--', amount]
                               if amount.startswith('-') else
                               ['grant-credits', 'grant@example.com', amount])
        assert result.exit_code != 0, amount
    with app.app_context():
        assert db.session.execute(
            db.select(User).filter_by(email='grant@example.com')).scalar_one().credits == 0
