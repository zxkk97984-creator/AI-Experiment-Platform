"""gradebook templates: category weight modes, auto-bound task columns, weight allocation

Revision ID: 20261013_0004
Revises: 20261013_0003
Create Date: 2026-10-13 04:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '20261013_0004'
down_revision = '20261013_0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 一次迁移做完本批成绩册结构改动，避免反复 ALTER 同一批表。
    with op.batch_alter_table('courses') as batch:
        batch.add_column(sa.Column('default_gradebook_config', sa.JSON(), nullable=True))

    with op.batch_alter_table('gradebook_categories') as batch:
        batch.add_column(sa.Column('weight_mode', sa.String(length=20), nullable=False, server_default='equal'))
        batch.create_check_constraint('ck_gradebook_category_weight_mode', "weight_mode IN ('equal', 'custom')")
    # 本批之前建立的成绩册都是教师逐项手填比例，按自定义权重复原；
    # 否则新任务会被静默按等权计入，改动既有班级的总评比例。
    op.execute("UPDATE gradebook_categories SET weight_mode = 'custom'")

    with op.batch_alter_table('gradebook_columns') as batch:
        batch.alter_column('weight', existing_type=sa.Numeric(precision=8, scale=4), nullable=True)
        batch.add_column(sa.Column('auto_bound', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        batch.add_column(sa.Column('bound_at', sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column('bound_by_id', sa.Integer(), nullable=True))
        # 外键单列出来：add_column 内联 ForeignKey 在非重建（MySQL）路径不会生成约束。
        batch.create_foreign_key('fk_gradebook_columns_bound_by_id', 'users', ['bound_by_id'], ['id'])
        batch.add_column(sa.Column('allocation_status', sa.String(length=20), nullable=False, server_default='allocated'))
        batch.create_check_constraint('ck_gradebook_column_allocation', "allocation_status IN ('allocated', 'pending')")
        # 同一班级成绩册内一个任务只能有一个分项；manual 分项的 source_id 为 NULL，
        # 而 MySQL 与 SQLite 都视 NULL 互不相等，因此人工分项不受影响。
        batch.create_unique_constraint('uq_gradebook_column_source', ['gradebook_id', 'source_kind', 'source_id'])


def downgrade() -> None:
    # 旧代码没有等权语义，NULL 权重先归一为 0（不计入），才能恢复 NOT NULL。
    op.execute('UPDATE gradebook_columns SET weight = 0 WHERE weight IS NULL')
    with op.batch_alter_table('gradebook_columns') as batch:
        batch.drop_constraint('uq_gradebook_column_source', type_='unique')
        batch.drop_constraint('ck_gradebook_column_allocation', type_='check')
        batch.drop_column('allocation_status')
        batch.drop_constraint('fk_gradebook_columns_bound_by_id', type_='foreignkey')
        batch.drop_column('bound_by_id')
        batch.drop_column('bound_at')
        batch.drop_column('auto_bound')
        batch.alter_column('weight', existing_type=sa.Numeric(precision=8, scale=4), nullable=False)

    with op.batch_alter_table('gradebook_categories') as batch:
        batch.drop_constraint('ck_gradebook_category_weight_mode', type_='check')
        batch.drop_column('weight_mode')

    with op.batch_alter_table('courses') as batch:
        batch.drop_column('default_gradebook_config')
