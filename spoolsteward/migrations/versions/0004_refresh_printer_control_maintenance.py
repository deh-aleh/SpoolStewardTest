"""audit auto-refresh per user, per-printer switches, gateway-wide settings (maintenance)

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user') as b:
        b.add_column(sa.Column('refresh_seconds', sa.Integer(), server_default='30', nullable=False))
    op.create_table(
        'printer_control',
        sa.Column('printer', sa.String(length=128), primary_key=True),
        sa.Column('blocked', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('mdns_hidden', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'gateway_setting',
        sa.Column('key', sa.String(length=64), primary_key=True),
        sa.Column('value', sa.String(length=255), nullable=False, server_default=''),
    )


def downgrade() -> None:
    op.drop_table('gateway_setting')
    op.drop_table('printer_control')
    with op.batch_alter_table('user') as b:
        b.drop_column('refresh_seconds')
