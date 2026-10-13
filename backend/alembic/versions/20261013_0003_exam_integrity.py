"""exam integrity: question types, timing modes, shuffle, exam events

Revision ID: 20261013_0003
Revises: 20261013_0002
Create Date: 2026-10-13 03:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '20261013_0003'
down_revision = '20261013_0002'
branch_labels = None
depends_on = None

_QUESTION_TYPES_WITH_TRUE_FALSE = (
    "question_type IN ('single_choice', 'multi_choice', 'true_false', 'fill_blank', 'code')"
)
_QUESTION_TYPES_WITHOUT_TRUE_FALSE = (
    "question_type IN ('single_choice', 'multi_choice', 'fill_blank', 'code')"
)


def upgrade() -> None:
    # 一次迁移做完本批全部考试结构改动，避免反复 ALTER 同一批表。
    with op.batch_alter_table('exams') as batch:
        batch.add_column(sa.Column('exam_kind', sa.String(length=20), nullable=False, server_default='quiz'))
        batch.add_column(sa.Column('timing_mode', sa.String(length=20), nullable=False, server_default='unified'))
        batch.add_column(sa.Column('shuffle_questions', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        batch.add_column(sa.Column('shuffle_options', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        batch.create_index('ix_exams_exam_kind', ['exam_kind'])
        batch.create_check_constraint(
            'ck_exam_kind', "exam_kind IN ('quiz', 'midterm', 'final', 'other')"
        )
        batch.create_check_constraint(
            'ck_exam_timing_mode', "timing_mode IN ('unified', 'per_student')"
        )

    with op.batch_alter_table('exam_submissions') as batch:
        batch.add_column(sa.Column('question_order', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('option_order', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('write_generation', sa.Integer(), nullable=False, server_default='1'))
        batch.add_column(sa.Column('active_device_id', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('active_device_token_hash', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('blur_count', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('away_total_ms', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('device_switch_count', sa.Integer(), nullable=False, server_default='0'))

    # 判断题：约束名保持不变，只扩充允许值（SQLite 走 batch 重建表）
    with op.batch_alter_table('exam_questions') as batch:
        batch.drop_constraint('ck_exam_question_type', type_='check')
        batch.create_check_constraint('ck_exam_question_type', _QUESTION_TYPES_WITH_TRUE_FALSE)

    op.create_table(
        'exam_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('exam_id', sa.Integer(), nullable=False),
        sa.Column('submission_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('client_ts', sa.DateTime(timezone=True), nullable=True),
        sa.Column('server_ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('ip_hash', sa.String(length=64), nullable=True),
        sa.Column('ua_hash', sa.String(length=64), nullable=True),
        sa.Column('dedupe_key', sa.String(length=120), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint(
            "kind IN ('blur', 'focus', 'reconnect', 'device_switch', 'autosave_rejected', 'warning')",
            name='ck_exam_event_kind',
        ),
        sa.ForeignKeyConstraint(['exam_id'], ['exams.id'], ),
        sa.ForeignKeyConstraint(['submission_id'], ['exam_submissions.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('submission_id', 'dedupe_key', name='uq_exam_event_dedupe'),
    )
    op.create_index(op.f('ix_exam_events_exam_id'), 'exam_events', ['exam_id'], unique=False)
    op.create_index(op.f('ix_exam_events_submission_id'), 'exam_events', ['submission_id'], unique=False)
    op.create_index('ix_exam_events_exam_server_ts', 'exam_events', ['exam_id', 'server_ts'], unique=False)


def downgrade() -> None:
    import os
    if os.environ.get('DAI_ENVIRONMENT', 'development').strip().lower() == 'production' \
            or os.environ.get('DAI_ALLOW_EXAM_INTEGRITY_DOWNGRADE') != '1':
        raise RuntimeError(
            'Exam integrity downgrade drops recorded proctor events and cannot keep true_false questions; '
            'production uses forward migrations and backups'
        )
    op.drop_index('ix_exam_events_exam_server_ts', table_name='exam_events')
    op.drop_index(op.f('ix_exam_events_submission_id'), table_name='exam_events')
    op.drop_index(op.f('ix_exam_events_exam_id'), table_name='exam_events')
    op.drop_table('exam_events')

    with op.batch_alter_table('exam_questions') as batch:
        batch.drop_constraint('ck_exam_question_type', type_='check')
        batch.create_check_constraint('ck_exam_question_type', _QUESTION_TYPES_WITHOUT_TRUE_FALSE)

    with op.batch_alter_table('exam_submissions') as batch:
        for column in ('device_switch_count', 'away_total_ms', 'blur_count',
                       'active_device_token_hash', 'active_device_id', 'write_generation',
                       'option_order', 'question_order'):
            batch.drop_column(column)

    with op.batch_alter_table('exams') as batch:
        batch.drop_constraint('ck_exam_timing_mode', type_='check')
        batch.drop_constraint('ck_exam_kind', type_='check')
        batch.drop_index('ix_exams_exam_kind')
        for column in ('shuffle_options', 'shuffle_questions', 'timing_mode', 'exam_kind'):
            batch.drop_column(column)
