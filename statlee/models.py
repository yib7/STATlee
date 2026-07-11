"""Database models (roadmap 7.2).

SQLite by default (zero-config dev), PostgreSQL in production via
``DATABASE_URL``. Anonymous sandbox sessions never touch these tables —
they only exist for logged-in users, preserving the "data not stored"
promise for anonymous use.
"""
from datetime import UTC, datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def _utcnow():
    return datetime.now(UTC)


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)

    # --- Monetization seam (workstream E) --------------------------------
    # No real billing yet. These exist so Pro mode and a future paid tier have
    # a place to live; ``billing.check_and_debit`` is the only code that should
    # read/write ``credits``.
    plan = db.Column(db.String(32), nullable=False, default='free')
    credits = db.Column(db.Integer, nullable=False, default=0)
    # 'YYYY-MM' of the last month this account's free credits were topped up.
    # Lets billing.check_and_debit apply MONTHLY_FREE_CREDITS lazily, exactly
    # once per calendar month on the first billed request of that month (P2-10).
    credits_month = db.Column(db.String(7), nullable=True)

    # --- Email verification (opt-in via REQUIRE_EMAIL_VERIFICATION) -------
    # Existing rows default to unverified; the flag is off by default so this
    # does not lock anyone out until an operator turns verification on.
    email_verified = db.Column(db.Boolean, nullable=False, default=False)
    verification_token = db.Column(db.String(64), nullable=True, index=True)
    # When the verification token was issued, so a stale/leaked link can expire
    # instead of granting login forever (P2-12).
    token_issued_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # --- Password reset (P2-11) ------------------------------------------
    # Single-use reset token plus its issue time (1h expiry). Mirrors the
    # verification-token machinery.
    password_reset_token = db.Column(db.String(64), nullable=True, index=True)
    reset_token_issued_at = db.Column(db.DateTime(timezone=True), nullable=True)

    runs = db.relationship('AnalysisRun', backref='user', lazy=True,
                           cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class AnalysisRun(db.Model):
    """One generate→run→interpret cycle, persisted for logged-in users (5.7/7.2)."""
    __tablename__ = 'analysis_runs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    dataset_name = db.Column(db.String(255))
    language = db.Column(db.String(16), default='Python')
    prompt = db.Column(db.Text)
    code = db.Column(db.Text)
    output = db.Column(db.Text)
    interpretation = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'dataset_name': self.dataset_name,
            'language': self.language,
            'prompt': self.prompt,
            'code': self.code,
            'output': self.output,
            'interpretation': self.interpretation,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class IssueReport(db.Model):
    """In-app diagnostic reports (6.3)."""
    __tablename__ = 'issue_reports'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    description = db.Column(db.Text, nullable=False)
    code = db.Column(db.Text)
    output = db.Column(db.Text)
    console_errors = db.Column(db.Text)
    user_agent = db.Column(db.String(512))
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)
