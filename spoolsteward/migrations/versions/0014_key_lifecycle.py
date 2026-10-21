"""print keys: single use, disabled by an administrator, "about to expire" notice; notices addressed to one user

Only adds columns (expand): the previous version keeps working on this database.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('print_key') as b:
        b.add_column(sa.Column('single_use', sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column('used_at', sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column('expiry_notified_at', sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table('notice') as b:
        b.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
        b.create_index('ix_notice_user_id', ['user_id'])
    with op.batch_alter_table('user') as b:
        b.add_column(sa.Column('own_notices_seen_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('user') as b:
        b.drop_column('own_notices_seen_at')
    with op.batch_alter_table('notice') as b:
        b.drop_index('ix_notice_user_id')
        b.drop_column('user_id')
    with op.batch_alter_table('print_key') as b:
        b.drop_column('expiry_notified_at')
        b.drop_column('disabled_at')
        b.drop_column('used_at')
        b.drop_column('single_use')
