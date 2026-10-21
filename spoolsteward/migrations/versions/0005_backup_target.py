"""scheduled backups, one set of destinations per administrator

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'backup_target',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
        sa.Column('owner_name', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('is_global', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('folder', sa.String(length=200), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('hour', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('keep_daily', sa.Integer(), nullable=False, server_default='7'),
        sa.Column('keep_weekly', sa.Integer(), nullable=False, server_default='4'),
        sa.Column('keep_monthly', sa.Integer(), nullable=False, server_default='12'),
        sa.Column('mode', sa.String(length=16), nullable=False, server_default='none'),
        sa.Column('recipient', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_status', sa.String(length=8), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_file', sa.String(length=200), nullable=True),
        sa.Column('last_size', sa.Integer(), nullable=True),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table('backup_target')
