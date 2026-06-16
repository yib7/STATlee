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
    # No real billing yet. These exist so the priority toggle and a future
    # paid tier have a place to live; ``billing.check_and_debit`` is the only
    # code that should read/write ``credits``. See docs/IMPLEMENTATION_PLAN.md.
    plan = db.Column(db.String(32), nullable=False, default='free')
    credits = db.Column(db.Integer, nullable=False, default=0)

    datasets = db.relationship('Dataset', backref='user', lazy=True,
                               cascade='all, delete-orphan')
    runs = db.relationship('AnalysisRun', backref='user', lazy=True,
                           cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Dataset(db.Model):
    __tablename__ = 'datasets'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)


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
