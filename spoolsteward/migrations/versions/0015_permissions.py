"""advanced permissions (colour, one-sided, printers, hours) and the queue names used in the past

Only adds tables (expand): the previous version keeps working on this database and ignores them.
`printer_former` starts from the printer names found in the audit, so queues deleted before this version are offered too.

Revision ID: 0015
Revises: 0014
"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'permission_rule',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('subject_type', sa.Enum('user', 'group', name='subjecttype'), nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=False),
        sa.Column('color', sa.Boolean(), nullable=True),
        sa.Column('one_sided', sa.Boolean(), nullable=True),
        sa.Column('printers_mode', sa.String(length=8), nullable=True),
        sa.Column('printers', sa.JSON(), nullable=True),
        sa.Column('hours_mode', sa.String(length=8), nullable=True),
        sa.Column('hours_days', sa.String(length=7), nullable=True),
        sa.Column('hours_start', sa.Integer(), nullable=True),
        sa.Column('hours_end', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('subject_type', 'subject_id'),
    )
    former = op.create_table(
        'printer_former',
        sa.Column('name', sa.String(length=128), primary_key=True),
        sa.Column('display', sa.String(length=128), nullable=False),
        sa.Column('removed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reason', sa.String(length=16), nullable=False),
    )
    # Read-only look at the audit: the last time each printer name was used by a print (scans store a fixed word there). Names of queues that exist today are not
    # offered (the page hides them), so the list needs no knowledge of CUPS here.
    rows = op.get_bind().exec_driver_sql(
        "SELECT MAX(printer), MAX(ts) FROM job_log WHERE kind = 'print' AND printer IS NOT NULL AND printer <> '' GROUP BY lower(printer)").fetchall()
    seed = []
    for display, ts in rows:
        if not display or len(display) > 128:
            continue
        if isinstance(ts, str):  # SQLite hands back text
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                ts = None
        ts = ts or datetime.now(timezone.utc)
        seed.append({'name': display.lower(), 'display': display, 'removed_at': ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
                     'reason': 'deleted'})
    if seed:
        op.bulk_insert(former, seed)


def downgrade() -> None:
    op.drop_table('printer_former')
    op.drop_table('permission_rule')
