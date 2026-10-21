"""accounts keys requests archive

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09 21:50:37.562527
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


_USER_UQ = {"uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s"}


def upgrade() -> None:
    bind = op.get_bind()
    dupes = bind.execute(sa.text('SELECT username FROM "user" GROUP BY username HAVING COUNT(*) > 1')).fetchall()
    if dupes:
        raise RuntimeError(
            "usernames must now be unique across providers, but these exist twice: "
            + ", ".join(r[0] for r in dupes) + ". Rename or merge them, then upgrade again."
        )
    op.create_table('audit_archive',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_table', sa.String(length=32), nullable=False),
    sa.Column('path', sa.String(length=512), nullable=False),
    sa.Column('first_ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('first_id', sa.Integer(), nullable=False),
    sa.Column('last_id', sa.Integer(), nullable=False),
    sa.Column('rows', sa.Integer(), nullable=False),
    sa.Column('bytes', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('admin_audit',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('actor_name', sa.String(length=128), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target_type', sa.String(length=32), nullable=True),
    sa.Column('target_id', sa.Integer(), nullable=True),
    sa.Column('detail_json', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['actor_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    with op.batch_alter_table('admin_audit', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_admin_audit_ts'), ['ts'], unique=False)

    op.create_table('print_key',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=64), nullable=False),
    sa.Column('key_prefix', sa.String(length=16), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_ip', sa.String(length=64), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash')
    )
    with op.batch_alter_table('print_key', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_print_key_user_id'), ['user_id'], unique=False)

    op.create_table('quota_request',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Enum('quota', 'group_change', name='requestkind'), nullable=False),
    sa.Column('scope', sa.Enum('temporary', 'permanent', name='requestscope'), nullable=True),
    sa.Column('params_json', sa.JSON(), nullable=True),
    sa.Column('reason', sa.String(length=500), nullable=False),
    sa.Column('status', sa.Enum('pending', 'approved', 'rejected', 'expired', 'canceled', name='requeststatus'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_by', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.String(length=500), nullable=True),
    sa.ForeignKeyConstraint(['decided_by'], ['user.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('quota_request', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_quota_request_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_quota_request_user_id'), ['user_id'], unique=False)

    op.create_table('quota_grant',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('metric', sa.Enum('jobs', 'pages', name='metric'), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('scope', sa.Enum('temporary', 'permanent', name='requestscope'), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('request_id', sa.Integer(), nullable=True),
    sa.Column('granted_by', sa.Integer(), nullable=True),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['granted_by'], ['user.id'], ),
    sa.ForeignKeyConstraint(['request_id'], ['quota_request.id'], ),
    sa.ForeignKeyConstraint(['revoked_by'], ['user.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('quota_grant', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_quota_grant_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('group', schema=None) as batch_op:
        batch_op.add_column(sa.Column('requestable', sa.Boolean(), nullable=False, server_default=sa.false()))

    # ids of the audit tables must never be reused (they identify rows across archive files)
    with op.batch_alter_table("job_log", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
        pass
    # the old unnamed UNIQUE(provider, username) goes away: the name alone must be unique now
    with op.batch_alter_table("user", naming_convention=_USER_UQ, recreate="always",
                              table_kwargs={"sqlite_autoincrement": True}) as batch_op:
        batch_op.drop_constraint("uq_user_provider_username", type_="unique")
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('password_hash', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_unique_constraint('uq_user_username', ['username'])



def downgrade() -> None:
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_constraint('uq_user_username', type_='unique')
        batch_op.drop_column('password_changed_at')
        batch_op.drop_column('must_change_password')
        batch_op.drop_column('password_hash')
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_user_provider_username", ["provider", "username"])

    with op.batch_alter_table('group', schema=None) as batch_op:
        batch_op.drop_column('requestable')

    with op.batch_alter_table('quota_grant', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_quota_grant_user_id'))

    op.drop_table('quota_grant')
    with op.batch_alter_table('quota_request', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_quota_request_user_id'))
        batch_op.drop_index(batch_op.f('ix_quota_request_status'))

    op.drop_table('quota_request')
    with op.batch_alter_table('print_key', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_print_key_user_id'))

    op.drop_table('print_key')
    with op.batch_alter_table('admin_audit', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_admin_audit_ts'))

    op.drop_table('admin_audit')
    op.drop_table('audit_archive')
