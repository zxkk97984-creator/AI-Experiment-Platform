"""kernel async cell execution

Additive: 单元格执行改为异步任务（kernel_executions），并把单格超时/空闲回收
从硬编码变成「草稿可编辑、发布冻结」的模板配置。

Revision ID: 20261013_0001
Revises: f4a5b6c7d8e9
Create Date: 2026-10-13 03:05:00.000000

注意：提示词第四节说「当前 head 是 20261003_0001」，但实际迁移图里
20261003_0001 已有子节点 b7c8d9e0f101（judge_claim_token，commit 5ea6ebb），
链尾是 f4a5b6c7d8e9（manual_score_audit）。因此本迁移的父节点取真实链尾，
否则会产生第二个 head，`alembic upgrade head` 与既有迁移用例直接失败。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT


revision = '20261013_0001'
down_revision = 'f4a5b6c7d8e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'kernel_executions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('record_id', sa.Integer(), nullable=False),
        sa.Column('cell_id', sa.String(length=80), nullable=False),
        sa.Column('code', sa.Text().with_variant(MEDIUMTEXT(), 'mysql'), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('client_request_id', sa.String(length=64), nullable=True),
        sa.Column('timeout_seconds', sa.Integer(), server_default=sa.text('120'), nullable=False),
        sa.Column('context', sa.JSON(), nullable=False),
        sa.Column('outputs', sa.JSON(), nullable=False),
        sa.Column('execution_time_ms', sa.Integer(), nullable=True),
        sa.Column('execution_count', sa.Integer(), nullable=True),
        sa.Column('error_code', sa.String(length=80), nullable=True),
        sa.Column('error_message', sa.String(length=1000), nullable=True),
        sa.Column('state_lost', sa.Boolean(), nullable=False),
        sa.Column('node_id', sa.String(length=80), nullable=True),
        sa.Column('lease_token', sa.String(length=36), nullable=True),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'recovering', 'succeeded',"
            " 'timeout', 'failed', 'cancelled')",
            name='ck_kernel_execution_status',
        ),
        sa.CheckConstraint('timeout_seconds > 0', name='ck_kernel_execution_timeout'),
        sa.ForeignKeyConstraint(['record_id'], ['experiment_records.id'], name='fk_kernel_execution_record'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('record_id', 'client_request_id', name='uq_kernel_execution_request'),
    )
    op.create_index(op.f('ix_kernel_executions_record_id'), 'kernel_executions', ['record_id'], unique=False)
    op.create_index(op.f('ix_kernel_executions_status'), 'kernel_executions', ['status'], unique=False)
    op.create_index('ix_kernel_execution_queue', 'kernel_executions', ['status', 'created_at'], unique=False)
    op.create_index('ix_kernel_execution_record', 'kernel_executions', ['record_id', 'status'], unique=False)

    # 草稿可编辑、发布冻结（照 notebook_templates.draft_environment_version_id 先例）：
    # server_default 保证已有模板行可安全回填，模型侧同样声明 server_default。
    with op.batch_alter_table('notebook_templates') as batch_op:
        batch_op.add_column(sa.Column('draft_cell_timeout_seconds', sa.Integer(),
                                      nullable=False, server_default=sa.text('120')))
        batch_op.add_column(sa.Column('draft_kernel_idle_seconds', sa.Integer(),
                                      nullable=False, server_default=sa.text('900')))

    with op.batch_alter_table('notebook_template_versions') as batch_op:
        batch_op.add_column(sa.Column('cell_timeout_seconds', sa.Integer(),
                                      nullable=False, server_default=sa.text('120')))
        batch_op.add_column(sa.Column('kernel_idle_seconds', sa.Integer(),
                                      nullable=False, server_default=sa.text('900')))


def downgrade() -> None:
    with op.batch_alter_table('notebook_template_versions') as batch_op:
        batch_op.drop_column('kernel_idle_seconds')
        batch_op.drop_column('cell_timeout_seconds')

    with op.batch_alter_table('notebook_templates') as batch_op:
        batch_op.drop_column('draft_kernel_idle_seconds')
        batch_op.drop_column('draft_cell_timeout_seconds')

    op.drop_index('ix_kernel_execution_record', table_name='kernel_executions')
    op.drop_index('ix_kernel_execution_queue', table_name='kernel_executions')
    op.drop_index(op.f('ix_kernel_executions_status'), table_name='kernel_executions')
    op.drop_index(op.f('ix_kernel_executions_record_id'), table_name='kernel_executions')
    op.drop_table('kernel_executions')
