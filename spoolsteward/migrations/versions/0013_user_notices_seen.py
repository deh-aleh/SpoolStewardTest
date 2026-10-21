"""each administrator remembers when they last opened the notices (the red dot on the bell)

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user') as b:
        b.add_column(sa.Column('notices_seen_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('user') as b:
        b.drop_column('notices_seen_at')
