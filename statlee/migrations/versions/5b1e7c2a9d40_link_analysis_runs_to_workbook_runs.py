"""SP1 session workbook: link analysis_runs rows to workbook runs

Revision ID: 5b1e7c2a9d40
Revises: 733d2d970515
Create Date: 2026-10-08 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5b1e7c2a9d40'
down_revision = '733d2d970515'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('analysis_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('run_id', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('dataset_version', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('model', sa.String(length=64), nullable=True))


def downgrade():
    with op.batch_alter_table('analysis_runs', schema=None) as batch_op:
        batch_op.drop_column('model')
        batch_op.drop_column('dataset_version')
        batch_op.drop_column('run_id')
