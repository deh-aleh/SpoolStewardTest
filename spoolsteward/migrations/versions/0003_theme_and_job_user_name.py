"""per-user theme, user name kept on every job row

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user') as b:
        b.add_column(sa.Column('theme', sa.String(length=8), server_default='auto', nullable=False))
    with op.batch_alter_table('job_log') as b:
        b.add_column(sa.Column('user_name', sa.String(length=128), nullable=True))
    op.execute('UPDATE job_log SET user_name = (SELECT username FROM "user" WHERE "user".id = job_log.user_id)')


def downgrade() -> None:
    with op.batch_alter_table('job_log') as b:
        b.drop_column('user_name')
    with op.batch_alter_table('user') as b:
        b.drop_column('theme')
