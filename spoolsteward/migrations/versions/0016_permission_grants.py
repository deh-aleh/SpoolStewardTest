"""permissions granted after a request (or directly by an administrator), for a while or for good

Only adds a table (expand): the previous version keeps working on this database and ignores it. The request kind
"permission" needs no change: the kind column is plain text (the enum has no CHECK constraint).

Revision ID: 0016
Revises: 0015
"""
from alembic import op
import sqlalchemy as sa

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'permission_grant',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('perm', sa.String(length=16), nullable=False),
        sa.Column('printer', sa.String(length=128), nullable=True),
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('request_id', sa.Integer(), sa.ForeignKey('quota_request.id'), nullable=True),
        sa.Column('granted_by', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_by', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
        sa.Column('expiry_notified_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_permission_grant_user_id', 'permission_grant', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_permission_grant_user_id', table_name='permission_grant')
    op.drop_table('permission_grant')
