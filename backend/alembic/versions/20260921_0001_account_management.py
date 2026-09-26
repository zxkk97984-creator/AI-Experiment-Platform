"""Account management, persistent batches and audit events.

Existing accounts retain their credentials and unrestricted login state.
"""
import os
from alembic import op
import sqlalchemy as sa

revision = "20260921_0001"
down_revision = "20260820_0003"
branch_labels = None
depends_on = None


LEGACY_LOOKUP_INDEXES = (
    ('environment_profiles', 'fk_env_profiles_current_version', 'current_version_id'),
    ('environment_drafts', 'fk_env_drafts_source_version', 'source_version_id'),
    ('environment_publications', 'fk_env_publications_previous_version', 'previous_version_id'),
)


def upgrade():
    # Preserve MySQL's existing FK-supporting lookup indexes and declare them on SQLite too.
    inspector = sa.inspect(op.get_bind())
    for table, name, column in LEGACY_LOOKUP_INDEXES:
        if inspector.has_table(table) and name not in {i['name'] for i in inspector.get_indexes(table)}:
            op.create_index(name, table, [column])
    # Additive migration: no existing credential or membership is rewritten.
    op.add_column('users', sa.Column('nickname', sa.String(length=80), nullable=True))
    op.add_column('users', sa.Column('must_change_password', sa.Boolean(), server_default='0', nullable=False))
    op.add_column('users', sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('temporary_password_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('credential_version', sa.Integer(), server_default='1', nullable=False))
    op.add_column('teaching_class_students', sa.Column('joined_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('teaching_class_students', sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('account_security_policies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('password_ciphertext', sa.Text(), nullable=True),
    sa.Column('temporary_days', sa.Integer(), server_default='7', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('updated_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('admin_batches',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('scope', sa.JSON(), nullable=False),
    sa.Column('summary', sa.JSON(), nullable=False),
    sa.Column('preview_digest', sa.String(length=64), nullable=False),
    sa.Column('policy_version', sa.Integer(), nullable=False),
    sa.Column('password_ciphertext', sa.Text(), nullable=True),
    sa.Column('reason', sa.String(length=500), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_token', sa.String(length=36), nullable=True),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('error_code', sa.String(length=80), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_admin_batches_status_created', 'admin_batches', ['status', 'created_at'], unique=False)
    op.create_index(op.f('ix_admin_batches_actor_id'), 'admin_batches', ['actor_id'], unique=False)
    op.create_table('admin_batch_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('batch_id', sa.String(length=36), nullable=False),
    sa.Column('row_number', sa.Integer(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('expected_version', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('prepared_hash', sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(['batch_id'], ['admin_batches.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('batch_id', 'row_number', name='uq_admin_batch_item_row')
    )
    op.create_index('ix_admin_batch_items_status', 'admin_batch_items', ['batch_id', 'status'], unique=False)
    op.create_table('audit_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('action', sa.String(length=80), nullable=False),
    sa.Column('target_type', sa.String(length=40), nullable=False),
    sa.Column('target_id', sa.String(length=80), nullable=False),
    sa.Column('batch_id', sa.String(length=36), nullable=True),
    sa.Column('request_id', sa.String(length=80), nullable=True),
    sa.Column('reason', sa.String(length=500), nullable=True),
    sa.Column('changes', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_events_actor_id'), 'audit_events', ['actor_id'], unique=False)
    op.create_index('ix_audit_events_target_created', 'audit_events', ['target_type', 'target_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_audit_events_action'), 'audit_events', ['action'], unique=False)
    op.create_index(op.f('ix_audit_events_batch_id'), 'audit_events', ['batch_id'], unique=False)



def downgrade():
    if os.getenv('DAI_ALLOW_ACCOUNT_MANAGEMENT_DOWNGRADE', '').lower() != 'true':
        raise RuntimeError('Account credential migration is forward-only; disposable tests must explicitly opt in')
    if os.getenv('DAI_ENVIRONMENT', 'development') == 'production':
        raise RuntimeError('Never remove account credential gates from a production database')
    if op.get_bind().dialect.name == 'sqlite':
        inspector = sa.inspect(op.get_bind())
        for table, name, _column in LEGACY_LOOKUP_INDEXES:
            if inspector.has_table(table) and name in {i['name'] for i in inspector.get_indexes(table)}:
                op.drop_index(name, table_name=table)
    for table in ('audit_events', 'admin_batch_items', 'admin_batches', 'account_security_policies'):
        op.drop_table(table)
    with op.batch_alter_table('teaching_class_students') as batch:
        batch.drop_column('removed_at')
        batch.drop_column('joined_at')
    with op.batch_alter_table('users') as batch:
        for column in ('credential_version', 'temporary_password_expires_at', 'password_changed_at', 'must_change_password', 'nickname'):
            batch.drop_column(column)
