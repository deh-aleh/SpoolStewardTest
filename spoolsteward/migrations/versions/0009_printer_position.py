"""manual position of a printer in the lists (moved up/down by an administrator)

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('printer_seen') as b:
        b.add_column(sa.Column('position', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('printer_seen') as b:
        b.drop_column('position')
