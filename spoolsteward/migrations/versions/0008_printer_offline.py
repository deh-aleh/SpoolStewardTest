"""since when a USB printer's queue has been unreachable (automatic removal)

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'printer_offline',
        sa.Column('printer', sa.String(length=128), nullable=False),
        sa.Column('since', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('printer'),
    )


def downgrade() -> None:
    op.drop_table('printer_offline')
