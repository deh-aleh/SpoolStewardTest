"""per-user delay after which secondary notices (forced prints) are read automatically

Only adds a column with a default (expand): the previous version keeps working on this database and ignores it.

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user') as b:
        b.add_column(sa.Column('secondary_read_minutes', sa.Integer(), nullable=False, server_default='20'))


def downgrade() -> None:
    with op.batch_alter_table('user') as b:
        b.drop_column('secondary_read_minutes')
