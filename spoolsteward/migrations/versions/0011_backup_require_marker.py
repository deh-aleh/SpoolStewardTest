"""backups: optionally write only into a folder that holds a marker file (unmounted network share protection)

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('backup_target') as b:
        b.add_column(sa.Column('require_marker', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('backup_target') as b:
        b.drop_column('require_marker')
