"""interface language chosen by each user (NULL = follow the browser)

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user') as b:
        b.add_column(sa.Column('language', sa.String(length=8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('user') as b:
        b.drop_column('language')
