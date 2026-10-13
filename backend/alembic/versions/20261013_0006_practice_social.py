"""practice badges and platform board opt-in (batch 6)

Additive 批次 6 激励与榜单偏好：

* 新表 ``practice_badge_awards``：四个徽章（首题/10 题/50 题/连续 7 天），
  唯一 ``(student_id, badge_key)`` 保证重复投递只发一条；
* 新表 ``practice_board_preferences``：全平台榜主动加入/退出（默认不加入）。

约束全部具名且与 model 一致；downgrade 对称（只删本迁移新建的表与索引，
不动任何既有表的数据，因此不需要 DAI_ALLOW_*_DOWNGRADE 生产开关）。

Revision ID: 20261013_0006
Revises: 20261013_0005
Create Date: 2026-10-13 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '20261013_0006'
down_revision = '20261013_0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "practice_badge_awards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("badge_key", sa.String(length=30), nullable=False),
        sa.Column("progress_value", sa.Integer(), nullable=False),
        # 触发发放的提交；普通整数不留外键（激励记录不参与提交表引用完整性）
        sa.Column("source_submission_id", sa.Integer(), nullable=True),
        sa.Column("awarded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "badge_key IN ('first_solved', 'solved_10', 'solved_50', 'streak_7')",
            name="ck_practice_badge_key"),
        sa.UniqueConstraint("student_id", "badge_key",
                            name="uq_practice_badge_student_key"),
    )
    op.create_index("ix_practice_badge_awards_student_id", "practice_badge_awards",
                    ["student_id"], unique=False)
    op.create_index("ix_practice_badge_awards_student", "practice_badge_awards",
                    ["student_id", "awarded_at"], unique=False)

    op.create_table(
        "practice_board_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("platform_opt_in", sa.Boolean(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("student_id", name="uq_practice_board_pref_student"),
    )
    op.create_index("ix_practice_board_preferences_student_id", "practice_board_preferences",
                    ["student_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_practice_board_preferences_student_id",
                  table_name="practice_board_preferences")
    op.drop_table("practice_board_preferences")
    op.drop_index("ix_practice_badge_awards_student", table_name="practice_badge_awards")
    op.drop_index("ix_practice_badge_awards_student_id", table_name="practice_badge_awards")
    op.drop_table("practice_badge_awards")
