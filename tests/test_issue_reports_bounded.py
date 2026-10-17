"""/report_issue storage and email are bounded (release audit 2026-10-16,
B-16): the table keeps only the newest rows and the operator gets at most
a few emails an hour per process."""
from conftest import post_json

from statlee.routes import misc


def test_issue_reports_keep_only_the_newest_rows(app, client, monkeypatch):
    from statlee.extensions import db
    from statlee.models import IssueReport
    monkeypatch.setattr(misc, 'ISSUE_REPORTS_MAX_ROWS', 3)
    ids = [post_json(client, '/report_issue',
                     {'description': f'issue {i}'}).get_json()['id']
           for i in range(5)]
    with app.app_context():
        kept = sorted(r.id for r in db.session.execute(
            db.select(IssueReport)).scalars())
    assert kept == ids[-3:]


def test_issue_emails_are_capped_per_hour(app, client, monkeypatch):
    cfg = app.config['STATLEE']
    cfg.smtp_host, cfg.issue_report_to = 'smtp.invalid', 'ops@example.com'
    sent = []
    monkeypatch.setattr(misc, '_email_report', lambda c, r: sent.append(r.id))
    monkeypatch.setattr(misc, 'ISSUE_EMAILS_PER_HOUR', 2)
    monkeypatch.setattr(misc, '_email_times', [])
    codes = [post_json(client, '/report_issue',
                       {'description': f'issue {i}'}).status_code
             for i in range(4)]
    assert codes == [200] * 4          # every report is still stored
    assert len(sent) == 2


def test_email_budget_frees_up_after_an_hour(monkeypatch):
    monkeypatch.setattr(misc, 'ISSUE_EMAILS_PER_HOUR', 1)
    monkeypatch.setattr(misc, '_email_times', [])
    assert misc._email_allowed(now=1000.0) is True
    assert misc._email_allowed(now=1001.0) is False
    assert misc._email_allowed(now=1000.0 + 3601) is True
