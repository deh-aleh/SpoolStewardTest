"""the position of a printer in the list belongs to each administrator (replaces the shared column of 0009)

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'printer_order',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('printer', sa.String(length=128), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('user_id', 'printer'),
    )
    with op.batch_alter_table('printer_seen') as b:
        b.drop_column('position')


def downgrade() -> None:
    with op.batch_alter_table('printer_seen') as b:
        b.add_column(sa.Column('position', sa.Integer(), nullable=True))
    op.drop_table('printer_order')
