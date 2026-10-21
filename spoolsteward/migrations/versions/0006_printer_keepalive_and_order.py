"""periodic maintenance print per printer, first-seen time of every queue (listing order)

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'printer_seen',
        sa.Column('printer', sa.String(length=128), primary_key=True),
        sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'printer_keepalive',
        sa.Column('printer', sa.String(length=128), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('interval_hours', sa.Integer(), nullable=False, server_default='168'),
        sa.Column('color', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('enabled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_status', sa.String(length=8), nullable=True),
        sa.Column('last_error', sa.String(length=500), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by', sa.String(length=128), nullable=False, server_default=''),
    )


def downgrade() -> None:
    op.drop_table('printer_keepalive')
    op.drop_table('printer_seen')
