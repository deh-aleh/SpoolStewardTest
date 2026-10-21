"""notices: things administrators should know about (the bell in the portal)

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'notice',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('kind', sa.String(length=48), nullable=False),
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('first_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.Column('detail_json', sa.JSON(), nullable=True),
        sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dismissed_by', sa.String(length=128), nullable=True),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_notice_kind', 'notice', ['kind'])


def downgrade() -> None:
    op.drop_index('ix_notice_kind', table_name='notice')
    op.drop_table('notice')
