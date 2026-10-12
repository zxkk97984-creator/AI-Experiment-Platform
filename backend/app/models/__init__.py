from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, CHAR, CheckConstraint, Date, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base
from app.storage.object_metadata import (
    StorageObjectBackend,
    StorageObjectStatus,
    StorageQuarantineStatus,
)

# 控制面主键：MySQL 使用 BIGINT（计划 4.x），SQLite 测试库回退 INTEGER（rowid 别名，可自增）
BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


def resolve_basic_env_version_id(context) -> int | None:
    """业务记录未显式指定环境时，惰性绑定 basic 档位当前可用版本（Phase 3）。

    - 与迁移 B（c5d6e7f8a901）的校验条件一致：slug=basic、status=available、
      image_digest 非空、版本号最大。
    - 无可用版本（测试库未 seed）返回 None：模型层可空，Phase 4 服务层接管后
      创建路径必须显式提供 environment_version_id。
    - 迁移 B 部署后生产库必存在 basic 可用版本，NOT NULL 约束由该默认值满足。
    """
    return context.connection.execute(
        text(
            "SELECT ev.id FROM environment_versions ev"
            " JOIN environment_profiles ep ON ep.id = ev.profile_id"
            " WHERE ep.slug = 'basic'"
            "   AND ev.status = 'available'"
            "   AND ev.image_digest IS NOT NULL"
            " ORDER BY ev.version_number DESC LIMIT 1"
        )
    ).scalar()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


# ── 用户 ─────────────────────────────────────────────────────


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    student_no: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    real_name: Mapped[str] = mapped_column(String(120))
    department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    role: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    # ── 会话撤销（TASK-012） ───────────────────────────────────
    # 改密/管理员重置/禁用时递增；Access/Refresh 携带 sv，
    # 认证与刷新时对比数据库值，旧 Token 立即 401。
    session_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    nickname: Mapped[str | None] = mapped_column(String(80), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    temporary_password_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    credential_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    teaching_class_memberships: Mapped[list["TeachingClassStudent"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )


# ── 统一文件对象元数据 ────────────────────────────────────────


class StorageObject(TimestampMixin, Base):
    """持久化文件对象的事实记录；本阶段不绑定任何业务表。"""

    __tablename__ = "storage_objects"
    __table_args__ = (
        UniqueConstraint("namespace", "object_key", name="uq_storage_objects_namespace_key"),
        CheckConstraint("namespace <> ''", name="ck_storage_objects_namespace_nonempty"),
        CheckConstraint("object_key <> ''", name="ck_storage_objects_key_nonempty"),
        CheckConstraint("backend <> ''", name="ck_storage_objects_backend_nonempty"),
        CheckConstraint(
            "status IN ('staging', 'active', 'deleting', 'deleted', 'failed')",
            name="ck_storage_objects_status",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_storage_objects_size_nonnegative",
        ),
        CheckConstraint(
            "sha256 IS NULL OR length(sha256) = 64",
            name="ck_storage_objects_sha256_length",
        ),
        CheckConstraint("version >= 1", name="ck_storage_objects_version_positive"),
        CheckConstraint(
            "(status = 'deleted' AND deleted_at IS NOT NULL)"
            " OR (status <> 'deleted' AND deleted_at IS NULL)",
            name="ck_storage_objects_deleted_at_status",
        ),
        Index("ix_storage_objects_namespace_status", "namespace", "status"),
        Index("ix_storage_objects_status_deleted_at", "status", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    backend: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StorageObjectBackend.LOCAL.value, server_default="local"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=StorageObjectStatus.STAGING.value, server_default="staging"
    )
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])


class StorageQuarantine(TimestampMixin, Base):
    """Persistent quarantine ledger for reconcile findings.

    A finding is deliberately kept outside ``storage_objects``.  It records
    that a physical object or metadata row needs observation and a later,
    safe collection attempt; it is not itself a business reference.
    """

    __tablename__ = "storage_quarantines"
    __table_args__ = (
        UniqueConstraint(
            "backend",
            "area",
            "object_key",
            "kind",
            name="uq_storage_quarantine_target_kind",
        ),
        CheckConstraint(
            "backend IN ('local', 's3')",
            name="ck_storage_quarantine_backend",
        ),
        CheckConstraint(
            "status IN ('quarantined', 'failed', 'resolved')",
            name="ck_storage_quarantine_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_storage_quarantine_attempts_nonnegative"),
        CheckConstraint(
            "(status = 'resolved' AND resolved_at IS NOT NULL)"
            " OR (status <> 'resolved' AND resolved_at IS NULL)",
            name="ck_storage_quarantine_resolved_at_status",
        ),
        Index(
            "ix_storage_quarantines_status_until",
            "status",
            "quarantine_until",
        ),
        Index(
            "ix_storage_quarantines_object_status",
            "object_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    area: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    object_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_objects.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=StorageQuarantineStatus.QUARANTINED.value,
        server_default=StorageQuarantineStatus.QUARANTINED.value,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    quarantine_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    storage_object: Mapped[StorageObject | None] = relationship(
        foreign_keys=[object_id]
    )


# ── 学期 / 教学班 ────────────────────────────────────────────


class AcademicTerm(TimestampMixin, Base):
    __tablename__ = "academic_terms"
    __table_args__ = (UniqueConstraint("code", name="uq_academic_terms_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(120))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="planned", index=True)

    teaching_classes: Mapped[list["TeachingClass"]] = relationship(back_populates="academic_term")
    courses: Mapped[list["Course"]] = relationship(back_populates="academic_term")


class TeachingClass(TimestampMixin, Base):
    __tablename__ = "teaching_classes"
    __table_args__ = (UniqueConstraint("academic_term_id", "code", name="uq_teaching_class_term_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    academic_term_id: Mapped[int] = mapped_column(ForeignKey("academic_terms.id"), index=True)
    code: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)

    academic_term: Mapped[AcademicTerm] = relationship(back_populates="teaching_classes")
    student_memberships: Mapped[list["TeachingClassStudent"]] = relationship(
        back_populates="teaching_class", cascade="all, delete-orphan"
    )
    course_links: Mapped[list["CourseTeachingClass"]] = relationship(
        back_populates="teaching_class", cascade="all, delete-orphan"
    )


class TeachingClassStudent(TimestampMixin, Base):
    __tablename__ = "teaching_class_students"
    __table_args__ = (
        UniqueConstraint("teaching_class_id", "student_id", name="uq_teaching_class_student"),
        Index("ix_class_students_class", "teaching_class_id"),
        Index("ix_class_students_student", "student_id"),
        Index("ix_class_students_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teaching_class_id: Mapped[int] = mapped_column(ForeignKey("teaching_classes.id", ondelete="CASCADE"))
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="active")

    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    teaching_class: Mapped[TeachingClass] = relationship(back_populates="student_memberships")
    student: Mapped[User] = relationship(back_populates="teaching_class_memberships")


class CourseTeachingClass(TimestampMixin, Base):
    __tablename__ = "course_teaching_classes"
    __table_args__ = (
        UniqueConstraint("course_id", "teaching_class_id", name="uq_course_teaching_class"),
        Index("ix_course_classes_course", "course_id"),
        Index("ix_course_classes_class", "teaching_class_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    teaching_class_id: Mapped[int] = mapped_column(ForeignKey("teaching_classes.id", ondelete="CASCADE"))

    course: Mapped["Course"] = relationship(back_populates="teaching_class_links")
    teaching_class: Mapped[TeachingClass] = relationship(back_populates="course_links")


# ── 课程 / 章节 / 课时 ────────────────────────────────────────


class Course(TimestampMixin, Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    teacher_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    academic_term_id: Mapped[int | None] = mapped_column(ForeignKey("academic_terms.id"), nullable=True, index=True)
    # ── 课程设置 ──────────────────────────────────────────────
    cover: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 新上传封面使用对象元数据绑定；为空时继续读取 legacy cover 字段。
    cover_object_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_objects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), default="class", server_default="class", nullable=False)
    default_score: Mapped[float] = mapped_column(Float, default=100.0, server_default="100")
    # 课程级成绩册模板：只存分类与规则，不存任务 ID；首次开班时复制到教学班成绩册。
    default_gradebook_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    teacher: Mapped[User | None] = relationship()
    cover_object: Mapped[StorageObject | None] = relationship(foreign_keys=[cover_object_id])
    academic_term: Mapped[AcademicTerm | None] = relationship(back_populates="courses")
    teaching_class_links: Mapped[list[CourseTeachingClass]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    chapters: Mapped[list["Chapter"]] = relationship(
        back_populates="course",
        cascade="all, delete-orphan",
        order_by="Chapter.order_index",
    )
    whitelist_entries: Mapped[list["CourseWhitelistStudent"]] = relationship(
        back_populates="course",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Chapter(TimestampMixin, Base):
    __tablename__ = "chapters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    course: Mapped[Course] = relationship(back_populates="chapters")
    lessons: Mapped[list["Lesson"]] = relationship(
        back_populates="chapter",
        cascade="all, delete-orphan",
        order_by="Lesson.order_index",
    )


class Lesson(TimestampMixin, Base):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(30), default="markdown")
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("notebook_templates.id"), nullable=True)
    notebook_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    video_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 视频来源：external（外链，使用 video_url）/ upload（本地上传，使用 storage key）
    video_source: Mapped[str] = mapped_column(
        String(20), default="external", server_default="external", nullable=False
    )
    video_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)  # 仅服务端使用，不暴露给客户端
    video_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 安全化后的原文件名
    video_content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    video_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 新上传视频使用对象元数据绑定；为空时继续读取 legacy video_storage_key。
    video_object_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_objects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    # 发布状态：draft（草稿）/ published（已发布）/ pending（待发布）
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="published", nullable=False)

    chapter: Mapped[Chapter] = relationship(back_populates="lessons")
    notebook_template: Mapped["NotebookTemplate | None"] = relationship(foreign_keys=[template_id])
    video_object: Mapped[StorageObject | None] = relationship(foreign_keys=[video_object_id])


# ── 选课 ─────────────────────────────────────────────────────


class CourseEnrollment(TimestampMixin, Base):
    __tablename__ = "course_enrollments"
    __table_args__ = (UniqueConstraint("course_id", "student_id", name="uq_course_student"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="enrolled")
    origin: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual", index=True)

    course: Mapped[Course] = relationship()
    student: Mapped[User] = relationship()


class LessonProgress(TimestampMixin, Base):
    """TASK-018：服务端学习进度事实（跨设备一致）。

    - 唯一键 (lesson_id, student_id)；状态仅 in_progress/completed
    - 打开课时只记录 in_progress 与最后访问时间；完成必须显式操作
    - 不记录视频播放位置/停留时长，不做自动完成
    """
    __tablename__ = "lesson_progress"
    __table_args__ = (
        UniqueConstraint("scope_key", "lesson_id", "student_id", name="uq_lesson_progress_lesson_student"),
        Index("ix_lesson_progress_student_status", "student_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offering_id: Mapped[int | None] = mapped_column(ForeignKey("course_offerings.id"), nullable=True, index=True)
    scope_key: Mapped[str] = mapped_column(String(80), nullable=False, default="legacy", server_default="legacy")
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    lesson: Mapped[Lesson] = relationship()
    student: Mapped[User] = relationship()


class CourseWhitelistStudent(TimestampMixin, Base):
    """课程白名单——教师指定可见的学生（与选课关系相互独立）"""
    __tablename__ = "course_whitelist_students"
    __table_args__ = (
        UniqueConstraint("course_id", "student_id", name="uq_course_whitelist_student"),
        Index("ix_course_whitelist_students_student_course", "student_id", "course_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    course: Mapped[Course] = relationship(back_populates="whitelist_entries")
    student: Mapped[User] = relationship()


# ── 作业与判题 ───────────────────────────────────────────────


class Assignment(TimestampMixin, Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    offering_id: Mapped[int | None] = mapped_column(ForeignKey("course_offerings.id"), nullable=True, index=True)
    source_task_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # ── 环境档位绑定（Phase 3：迁移 B） ────────────────────────
    # 作业默认环境；发布后不可直接修改（Phase 4 门禁），历史提交保留自己的快照
    environment_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("environment_versions.id"), nullable=False,
        default=resolve_basic_env_version_id, index=True,
    )
    import_policy_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unrestricted"
    )  # unrestricted | restricted
    allowed_imports: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    audience_mode: Mapped[str] = mapped_column(
        String(20), default="all_enrolled", server_default="all_enrolled", nullable=False, index=True
    )
    audience_class_links: Mapped[list["AssignmentAudienceClass"]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )
    audience_student_links: Mapped[list["AssignmentAudienceStudent"]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )

    course: Mapped[Course] = relationship()
    questions: Mapped[list["JudgeQuestion"]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
    )

    @property
    def audience_class_ids(self) -> list[int]:
        if "_audience_class_ids" in self.__dict__:
            return self.__dict__["_audience_class_ids"]
        return [link.teaching_class_id for link in self.audience_class_links]

    @property
    def whitelist_student_ids(self) -> list[int]:
        if "_whitelist_student_ids" in self.__dict__:
            return self.__dict__["_whitelist_student_ids"]
        return [link.student_id for link in self.audience_student_links if link.kind == "include"]

    @property
    def excluded_student_ids(self) -> list[int]:
        if "_excluded_student_ids" in self.__dict__:
            return self.__dict__["_excluded_student_ids"]
        return [link.student_id for link in self.audience_student_links if link.kind == "exclude"]


class AssignmentAudienceClass(TimestampMixin, Base):
    """作业发布范围——选中的教学班（必须已绑定当前课程）。"""

    __tablename__ = "assignment_audience_classes"
    __table_args__ = (
        UniqueConstraint("assignment_id", "teaching_class_id", name="uq_assignment_audience_class"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    teaching_class_id: Mapped[int] = mapped_column(ForeignKey("teaching_classes.id"), index=True)

    assignment: Mapped["Assignment"] = relationship(back_populates="audience_class_links")


class AssignmentAudienceStudent(TimestampMixin, Base):
    """作业发布范围——额外加入 / 排除的学生。"""

    __tablename__ = "assignment_audience_students"
    __table_args__ = (
        UniqueConstraint("assignment_id", "student_id", "kind", name="uq_assignment_audience_student_kind"),
        Index("ix_assignment_audience_students_kind", "assignment_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="include")  # include | exclude
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    assignment: Mapped["Assignment"] = relationship(back_populates="audience_student_links")
    student: Mapped[User] = relationship(foreign_keys=[student_id])


class JudgeQuestion(TimestampMixin, Base):
    __tablename__ = "judge_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    function_name: Mapped[str] = mapped_column(String(120))
    signature: Mapped[str | None] = mapped_column(String(255), nullable=True)
    starter_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_cases: Mapped[list] = mapped_column(JSON, default=list)
    hidden_tests: Mapped[str] = mapped_column(Text)
    time_limit_ms: Mapped[int] = mapped_column(Integer, default=10000)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, default=256)
    max_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # ── AI 评分配置 ──────────────────────────────────────────
    grading_mode: Mapped[str] = mapped_column(String(20), default="legacy", index=True)
    teacher_constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    reference_solution: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_groups: Mapped[list] = mapped_column(JSON, default=list)
    score_cap_rules: Mapped[list] = mapped_column(JSON, default=list)
    # ── 环境档位绑定（Phase 3：迁移 B） ────────────────────────
    # 题目覆盖环境；NULL 语义为继承作业默认（回填阶段统一绑定 basic）。
    # Phase 4：不设 SQLAlchemy default——Python-side default 在值为 None 时也会被调用，
    # 会破坏「NULL = 继承作业」语义；题目环境由服务层/API 层显式传参。
    environment_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("environment_versions.id"), nullable=True,
        index=True,
    )
    import_policy_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inherit"
    )  # inherit | unrestricted | restricted
    allowed_imports: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # ── 评分版本绑定（批次三 3B） ──────────────────────────────
    # 指向 judge_grading_versions.id；用整数而非外键，避免与版本表（反向引用题目）
    # 形成循环外键，迁移在 SQLite/MySQL 上都能安全批量执行。
    current_grading_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    assignment: Mapped[Assignment] = relationship(back_populates="questions")


class Submission(TimestampMixin, Base):
    __tablename__ = "submissions"
    __table_args__ = (
        Index("ix_submissions_gs_updated", "grading_status", "updated_at"),
        Index("ix_submissions_gs_finished", "grading_status", "finished_at"),
        UniqueConstraint("student_id", "client_request_id",
                         name="uq_submission_student_request"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("judge_questions.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    # ── 判题队列状态机（Task 1） ──────────────────────────────
    grading_status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending / queued / running / completed / system_error
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 认领代次：每次 queued→running 由 claim_job 现生成的随机 token。
    # 所有结果写入都必须带它做条件 UPDATE，使失去认领的旧 Worker 无法覆盖新结果。
    # 只增不减、任何业务路径（含教师重试）都不重置——这是它不能复用 attempt_count 的原因。
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 结果修订号：任何产生或改变结果的写入（判题终态、AI 终态、人工改分、受控重评）
    # 都在同一事务内原子 +1；人工评分/旧客户端据此拒绝过时写入。
    result_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # ── 判题结果 ──────────────────────────────────────────────
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    result_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tests_passed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tests_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ── 环境档位快照（Phase 3：迁移 B） ────────────────────────
    # 入队前冻结实际使用的环境版本与 import 策略，历史重判不受作业重新发布影响
    environment_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("environment_versions.id"), nullable=False,
        default=resolve_basic_env_version_id, index=True,
    )
    import_policy_mode_snapshot: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unrestricted"
    )
    allowed_imports_snapshot: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # ── 提交幂等（批次四） ─────────────────────────────────────
    # 可选请求键：唯一范围是"学生 + 请求键"。同键同正文返回原提交；
    # 同键不同正文 409 IDEMPOTENCY_KEY_REUSED；不同学生同键互不影响。
    client_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 正文摘要（固定 v1 规范 JSON：question_id + 原样 code，不含环境/截止等可变配置）
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 提交时绑定的评分版本（批次三 3B）：Worker 只读它，不得改用题目最新配置。
    # NULL = 无版本基线（旧题/未启用数据型判题）→ 后续评分转人工，不静默绑定最新版本。
    grading_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    question: Mapped[JudgeQuestion] = relationship()
    student: Mapped[User] = relationship()


# ── 考试 ─────────────────────────────────────────────────────


class Exam(TimestampMixin, Base):
    __tablename__ = "exams"
    __table_args__ = (
        CheckConstraint("exam_kind IN ('quiz', 'midterm', 'final', 'other')", name="ck_exam_kind"),
        CheckConstraint("timing_mode IN ('unified', 'per_student')", name="ck_exam_timing_mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    offering_id: Mapped[int | None] = mapped_column(ForeignKey("course_offerings.id"), nullable=True, index=True)
    source_task_id: Mapped[int | None] = mapped_column(ForeignKey("exams.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ── 课堂试用：考试类型与考试秩序（批次 3） ────────────────
    # exam_kind 供成绩册自动归类（批次 4 依赖）：quiz→平时、midterm→期中、final→期末、other→教师选择。
    exam_kind: Mapped[str] = mapped_column(
        String(20), default="quiz", server_default="quiz", nullable=False, index=True
    )
    # unified：交卷时间 = min(入场 + 时长, end_at)；per_student：入场 + 时长，
    # 此时 end_at 只表示「最晚可开考时间」。发布后锁定。
    timing_mode: Mapped[str] = mapped_column(
        String(20), default="unified", server_default="unified", nullable=False
    )
    shuffle_questions: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    shuffle_options: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    show_score_after_grading: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    show_questions_after_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    show_answers_after_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    review_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_released_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    audience_mode: Mapped[str] = mapped_column(
        String(20), default="all_enrolled", server_default="all_enrolled", nullable=False, index=True
    )
    audience_class_links: Mapped[list["ExamAudienceClass"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )
    audience_student_links: Mapped[list["ExamAudienceStudent"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )

    course: Mapped[Course] = relationship()
    questions: Mapped[list["ExamQuestion"]] = relationship(back_populates="exam", cascade="all, delete-orphan")

    @property
    def audience_class_ids(self) -> list[int]:
        if "_audience_class_ids" in self.__dict__:
            return self.__dict__["_audience_class_ids"]
        return [link.teaching_class_id for link in self.audience_class_links]

    @property
    def whitelist_student_ids(self) -> list[int]:
        if "_whitelist_student_ids" in self.__dict__:
            return self.__dict__["_whitelist_student_ids"]
        return [link.student_id for link in self.audience_student_links if link.kind == "include"]

    @property
    def excluded_student_ids(self) -> list[int]:
        if "_excluded_student_ids" in self.__dict__:
            return self.__dict__["_excluded_student_ids"]
        return [link.student_id for link in self.audience_student_links if link.kind == "exclude"]


class ExamAudienceClass(TimestampMixin, Base):
    """考试发布范围——选中的教学班（必须已绑定当前课程）。"""

    __tablename__ = "exam_audience_classes"
    __table_args__ = (
        UniqueConstraint("exam_id", "teaching_class_id", name="uq_exam_audience_class"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    teaching_class_id: Mapped[int] = mapped_column(ForeignKey("teaching_classes.id"), index=True)

    exam: Mapped["Exam"] = relationship(back_populates="audience_class_links")


class ExamAudienceStudent(TimestampMixin, Base):
    """考试发布范围——额外加入 / 排除的学生。"""

    __tablename__ = "exam_audience_students"
    __table_args__ = (
        UniqueConstraint("exam_id", "student_id", "kind", name="uq_exam_audience_student_kind"),
        Index("ix_exam_audience_students_kind", "exam_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="include")  # include | exclude
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    exam: Mapped["Exam"] = relationship(back_populates="audience_student_links")
    student: Mapped[User] = relationship(foreign_keys=[student_id])


class ExamSubmission(TimestampMixin, Base):
    """考试提交记录。answers 已删除，ExamAnswer 是唯一事实源。

    状态机：started -> submitted -> grading -> graded
            grading -> review_required（自动评分终止，需人工处理）
            review_required -> grading（仅显式受控重试）
    """
    __tablename__ = "exam_submissions"
    __table_args__ = (
        UniqueConstraint("exam_id", "student_id", name="uq_exam_student"),
        Index("ix_exam_submissions_status_expires", "status", "expires_at"),
        CheckConstraint(
            "status IN ('started', 'submitted', 'grading', 'graded', 'review_required')",
            name="ck_exam_submission_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="started", index=True)
    # started -> submitted -> grading -> graded；grading -> review_required -> grading
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submission_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ── 自动评分终止（需人工处理） ────────────────────────────
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # 只存脱敏短摘要
    review_required_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 考试开始时冻结的逐代码题评分版本映射：{question_id: grading_version_id}
    # 创建/补建答案只能从这里复制，不得读取题目最新配置。
    grading_version_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ── 考试秩序：乱序快照与答题设备代次（批次 3） ──────────────
    # 开考时与 freeze_exam_versions 同一事务写入后冻结；此后一切顺序以库为准，不再重算。
    question_order: Mapped[list | None] = mapped_column(JSON, nullable=True)
    option_order: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 写入代次：换机接管 +1；所有答题写入必须携带它，旧设备一律 409 DEVICE_SUPERSEDED。
    write_generation: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    active_device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_device_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 监考计数：由 exam_events 幂等重算（断网补报不重复累加），只供教师查看，不扣分。
    blur_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    away_total_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    device_switch_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    exam: Mapped[Exam] = relationship()
    student: Mapped[User] = relationship()
    answers: Mapped[list["ExamAnswer"]] = relationship(back_populates="submission", cascade="all, delete-orphan")


class SchedulerLease(TimestampMixin, Base):
    """多实例任务租约——同一时刻只允许一个实例执行某类扫描/恢复任务。

    - task_name 主键，天然防重复插入
    - lease_until 过期后其他实例可接管（崩溃自动释放）
    - 同一 owner 可续租
    - 不持有长事务：每轮扫描小批量，TTL 大于正常扫描时长
    """

    __tablename__ = "scheduler_leases"

    task_name: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(120))
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExamGrade(TimestampMixin, Base):
    __tablename__ = "exam_grades"
    __table_args__ = (UniqueConstraint("exam_id", "student_id", name="uq_exam_grade"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    score: Mapped[float] = mapped_column(Float, default=0)


    exam: Mapped[Exam] = relationship()
    student: Mapped[User] = relationship()


class ExamQuestion(TimestampMixin, Base):
    """考试题目"""
    __tablename__ = "exam_questions"
    __table_args__ = (
        CheckConstraint(
            "question_type IN ('single_choice', 'multi_choice', 'true_false', 'fill_blank', 'code')",
            name="ck_exam_question_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    question_type: Mapped[str] = mapped_column(String(20))  # single_choice / multi_choice / fill_blank / code
    prompt: Mapped[str] = mapped_column(Text)
    options: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correct_answer: Mapped[dict] = mapped_column(JSON)  # {"correct":["A"]} 或 {"test_file":"..."}
    points: Mapped[float] = mapped_column(Float, default=0)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    starter_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_cases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    hidden_tests: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_limit_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_limit_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ── AI 评分配置 ──────────────────────────────────────────
    grading_mode: Mapped[str] = mapped_column(String(20), default="legacy", index=True)
    teacher_constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    reference_solution: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_groups: Mapped[list] = mapped_column(JSON, default=list)
    score_cap_rules: Mapped[list] = mapped_column(JSON, default=list)
    # 当前启用的评分版本（同 JudgeQuestion.current_grading_version_id）
    current_grading_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    exam: Mapped[Exam] = relationship(back_populates="questions")


class ExamAnswer(TimestampMixin, Base):
    """考试答题唯一事实源——逐题行记录，禁止并发改整块 JSON"""
    __tablename__ = "exam_answers"
    __table_args__ = (
        UniqueConstraint("submission_id", "question_id", name="uq_exam_answer_q"),
        Index("ix_exam_answers_gs_updated", "grading_status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("exam_submissions.id"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("exam_questions.id"), index=True)
    selected_options: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 选择题答案
    code_answer: Mapped[str | None] = mapped_column(Text, nullable=True)  # 编程题答案
    text_answers: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 填空题答案：blank id -> text
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tests_passed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tests_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ── 判题队列状态机（Task 1） ──────────────────────────────
    grading_status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending / queued / running / completed / system_error
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 认领代次（同 Submission.claim_token）：结果写入必须带它做条件 UPDATE
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 结果修订号（同 Submission.result_revision）：判题/AI/人工改分原子推进
    result_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # 考试开始时从父级冻结映射复制的评分版本（批次三 3B）
    grading_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # ── 判题结果 ──────────────────────────────────────────────
    result_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    system_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ── 教师手动改分（学生端可见） ─────────────────────────────
    manual_score_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_score_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    submission: Mapped[ExamSubmission] = relationship(back_populates="answers")
    question: Mapped[ExamQuestion] = relationship()


# ── Notebook 模板与版本 ───────────────────────────────────────


class NotebookTemplate(TimestampMixin, Base):
    """教师创建/编辑的实验模板"""
    __tablename__ = "notebook_templates"
    __table_args__ = (
        # FK 单独定义以使用 use_alter 解决循环依赖
        # migration 中通过 batch_alter_table 单独创建
        ForeignKeyConstraint(
            ["current_version_id"], ["notebook_template_versions.id"],
            use_alter=True, name="fk_template_current_version",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft / published
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    draft_cells: Mapped[list] = mapped_column(JSON, default=list)
    draft_revision: Mapped[int] = mapped_column(Integer, default=1)
    draft_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    draft_assets_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # ── 草稿环境绑定（Phase 3：迁移 B） ────────────────────────
    # 发布时复制到新的 NotebookTemplateVersion，历史版本不更新
    draft_environment_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("environment_versions.id"), nullable=False,
        default=resolve_basic_env_version_id, index=True,
    )
    draft_import_policy_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unrestricted"
    )  # unrestricted | restricted
    draft_allowed_imports: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # ── 草稿运行配置（Q1：发布前配置、发布后锁定） ──────────────
    # 发布时复制到新的 NotebookTemplateVersion，历史版本不更新
    draft_cell_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120, server_default=text("120")
    )
    draft_kernel_idle_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=900, server_default=text("900")
    )

    owner: Mapped[User] = relationship(foreign_keys=[owner_id])
    versions: Mapped[list["NotebookTemplateVersion"]] = relationship(
        back_populates="template", cascade="all, delete-orphan",
        order_by="NotebookTemplateVersion.version_number",
        foreign_keys="[NotebookTemplateVersion.template_id]",
    )
    current_version: Mapped["NotebookTemplateVersion | None"] = relationship(
        foreign_keys=[current_version_id],
        primaryjoin="NotebookTemplate.current_version_id == NotebookTemplateVersion.id",
        post_update=True,
    )


class NotebookTemplateVersion(Base):
    """每次发布的不可变快照——资源路径为相对路径"""
    __tablename__ = "notebook_template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version_number", name="uq_version_number_per_template"),
        Index("ix_template_versions_environment_version_id", "environment_version_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("notebook_templates.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    cells: Mapped[list] = mapped_column(JSON, default=list)  # 不可变快照
    cell_order: Mapped[list] = mapped_column(JSON, default=list)
    notebook_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    assets_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)  # 相对路径
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=func.now()
    )
    published_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # ── 发布环境快照（Phase 3：迁移 B） ────────────────────────
    # 从草稿复制，发布后不可变；已开始实验的记录不随新版本自动升级
    environment_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("environment_versions.id"), nullable=False,
        default=resolve_basic_env_version_id,
    )
    import_policy_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unrestricted"
    )
    allowed_imports: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # ── 发布运行配置快照（Q1） ────────────────────────────────
    # 从草稿复制，发布后不可变；已开始实验的记录不随新版本自动升级
    cell_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120, server_default=text("120")
    )
    kernel_idle_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=900, server_default=text("900")
    )

    template: Mapped[NotebookTemplate] = relationship(
        back_populates="versions",
        foreign_keys=[template_id],
    )
    published_by: Mapped[User] = relationship()


class StudioAssetManifest(TimestampMixin, Base):
    """A draft or published Studio asset collection.

    The manifest owns the collection relationship; individual files remain
    ``StorageObject`` rows and are never represented as directory objects.
    Exactly one of ``template_id`` (draft) and ``version_id`` (published
    snapshot) is set.
    """

    __tablename__ = "studio_asset_manifests"
    __table_args__ = (
        UniqueConstraint("template_id", name="uq_studio_asset_manifest_template"),
        UniqueConstraint("version_id", name="uq_studio_asset_manifest_version"),
        CheckConstraint(
            "(template_id IS NOT NULL AND version_id IS NULL)"
            " OR (template_id IS NULL AND version_id IS NOT NULL)",
            name="ck_studio_asset_manifest_owner",
        ),
        CheckConstraint("revision >= 1", name="ck_studio_asset_manifest_revision_positive"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("notebook_templates.id", ondelete="CASCADE"), nullable=True
    )
    version_id: Mapped[int | None] = mapped_column(
        ForeignKey("notebook_template_versions.id", ondelete="CASCADE"), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    entries: Mapped[list["StudioAssetManifestEntry"]] = relationship(
        back_populates="manifest",
        cascade="all, delete-orphan",
        order_by="StudioAssetManifestEntry.relative_path",
    )


class StudioAssetManifestEntry(TimestampMixin, Base):
    """One relative resource path bound to one concrete StorageObject."""

    __tablename__ = "studio_asset_manifest_entries"
    __table_args__ = (
        UniqueConstraint(
            "manifest_id",
            "relative_path",
            name="uq_studio_asset_manifest_entry_path",
        ),
        CheckConstraint(
            "relative_path <> ''",
            name="ck_studio_asset_manifest_entry_path_nonempty",
        ),
        Index("ix_studio_asset_manifest_entries_manifest_id", "manifest_id"),
        Index("ix_studio_asset_manifest_entries_storage_object_id", "storage_object_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    manifest_id: Mapped[int] = mapped_column(
        ForeignKey("studio_asset_manifests.id", ondelete="CASCADE"), nullable=False
    )
    storage_object_id: Mapped[int] = mapped_column(
        ForeignKey("storage_objects.id", ondelete="RESTRICT"), nullable=False
    )
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)

    manifest: Mapped[StudioAssetManifest] = relationship(back_populates="entries")
    storage_object: Mapped[StorageObject] = relationship()


# ── 实验模块 ──────────────────────────────────────────────────


class ExperimentModule(TimestampMixin, Base):
    __tablename__ = "experiment_modules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("notebook_templates.id"), nullable=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notebook_template: Mapped[NotebookTemplate | None] = relationship(foreign_keys=[template_id])
    owner: Mapped[User | None] = relationship(foreign_keys=[owner_id])


# ── 统一实验记录（替代 NotebookRecord）─────────────────────────


class ExperimentRecord(TimestampMixin, Base):
    """统一的实验/Notebook 学生记录。lesson_id 和 module_id 二选一。"""
    __tablename__ = "experiment_records"
    __table_args__ = (
        UniqueConstraint("scope_key", "lesson_id", "student_id", name="uq_record_lesson_student"),
        UniqueConstraint("scope_key", "module_id", "student_id", name="uq_record_module_student"),
        CheckConstraint(
            "(lesson_id IS NULL) != (module_id IS NULL)",
            name="ck_record_entry_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offering_id: Mapped[int | None] = mapped_column(ForeignKey("course_offerings.id"), nullable=True, index=True)
    scope_key: Mapped[str] = mapped_column(String(80), nullable=False, default="legacy", server_default="legacy")
    lesson_id: Mapped[int | None] = mapped_column(ForeignKey("lessons.id"), nullable=True, index=True)
    module_id: Mapped[int | None] = mapped_column(ForeignKey("experiment_modules.id"), nullable=True, index=True)
    template_version_id: Mapped[int] = mapped_column(ForeignKey("notebook_template_versions.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="started")
    cells_sources: Mapped[dict] = mapped_column(JSON, default=dict)  # {cell_id: source}
    cells_outputs: Mapped[dict] = mapped_column(JSON, default=dict)  # {cell_id: {outputs, execution_count}}
    record_revision: Mapped[int] = mapped_column(Integer, default=1)  # 乐观并发控制
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ── 环境快照（Phase 3：迁移 B） ────────────────────────────
    # 创建记录时从 NotebookTemplateVersion 复制；已存在记录不随模板新版本自动升级
    environment_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("environment_versions.id"), nullable=False,
        default=resolve_basic_env_version_id, index=True,
    )

    lesson: Mapped[Lesson | None] = relationship(foreign_keys=[lesson_id])
    module: Mapped[ExperimentModule | None] = relationship(foreign_keys=[module_id])
    template_version: Mapped[NotebookTemplateVersion] = relationship()
    student: Mapped[User] = relationship()
    submissions: Mapped[list["ExperimentSubmission"]] = relationship(
        back_populates="record", cascade="all, delete-orphan"
    )


class ExperimentSubmission(TimestampMixin, Base):
    """每次提交的记录，cells_snapshot 不可变

    client_request_id 由前端生成，用于幂等提交：
      同一 record_id + client_request_id 重复请求返回已有提交。
    """
    __tablename__ = "experiment_submissions"
    __table_args__ = (
        UniqueConstraint("record_id", "attempt_number", name="uq_experiment_submission_attempt"),
        UniqueConstraint("record_id", "client_request_id", name="uq_experiment_submission_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("experiment_records.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    client_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    cells_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    outputs_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # ── 教师评分反馈（Task 8） ────────────────────────────────
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    record: Mapped[ExperimentRecord] = relationship(back_populates="submissions")
    reviewed_by: Mapped["User | None"] = relationship(foreign_keys=[reviewed_by_id])


# ── AI 智能代码评分 ────────────────────────────────────────────


class QuestionRubric(TimestampMixin, Base):
    """题目专属 Rubric——版本化管理，关联作业题或考试编程题"""
    __tablename__ = "question_rubrics"
    __table_args__ = (
        CheckConstraint(
            "(judge_question_id IS NULL) != (exam_question_id IS NULL)",
            name="ck_rubric_xor_target",
        ),
        UniqueConstraint("judge_question_id", "version", name="uq_rubric_judge_version"),
        UniqueConstraint("exam_question_id", "version", name="uq_rubric_exam_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    judge_question_id: Mapped[int | None] = mapped_column(
        ForeignKey("judge_questions.id"), nullable=True, index=True
    )
    exam_question_id: Mapped[int | None] = mapped_column(
        ForeignKey("exam_questions.id"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    source_snapshot: Mapped[dict] = mapped_column(JSON)
    rubric_json: Mapped[dict] = mapped_column(JSON)
    model_name: Mapped[str] = mapped_column(String(120))
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ManualScoreAudit(TimestampMixin, Base):
    """人工评分审计（append-only）：每次人工给分都新增一行，永不修改或删除。

    ``result_details.manual_review_history`` 只是展示用的最近若干条；
    审计事实以本表为准（谁、何时、给了多少分、理由、写入后的结果修订号）。
    """

    __tablename__ = "manual_score_audits"
    __table_args__ = (
        Index("ix_manual_score_audits_target", "target_kind", "target_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # submission | exam_answer
    target_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)
    # 本次写入后的结果修订号（便于与 Submission.result_revision 对照）
    result_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="manual_score")


class JudgeGradingVersion(TimestampMixin, Base):
    """不可变评分版本（批次三 3B）。

    - ``judge_question_id`` 与 ``exam_question_id`` 严格二选一；
    - ``draft`` 可编辑（新版本另存）；``confirmed`` 之后 payload 不可修改；
      ``active`` 供新提交绑定；``retired`` 只禁止新绑定，不使历史绑定失效；
    - 保存入口函数、数据用例、比较规则、F/R 权重、来源摘要与绑定的 Rubric，
      以及会影响 AI 评审的评分上下文快照。
    """

    __tablename__ = "judge_grading_versions"
    __table_args__ = (
        CheckConstraint(
            "(judge_question_id IS NULL) != (exam_question_id IS NULL)",
            name="ck_grading_version_xor_target",
        ),
        UniqueConstraint("judge_question_id", "version", name="uq_grading_version_judge"),
        UniqueConstraint("exam_question_id", "version", name="uq_grading_version_exam"),
        Index("ix_grading_versions_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    judge_question_id: Mapped[int | None] = mapped_column(
        ForeignKey("judge_questions.id"), nullable=True, index=True
    )
    exam_question_id: Mapped[int | None] = mapped_column(
        ForeignKey("exam_questions.id"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    # draft / confirmed / active / retired
    status: Mapped[str] = mapped_column(String(20), default="draft")
    # 执行方式（与成绩采用策略 legacy/shadow/active 正交）
    execution_mode: Mapped[str] = mapped_column(String(20), default="data_v1")
    grading_mode_snapshot: Mapped[str] = mapped_column(String(20), default="active")
    entry_function: Mapped[str] = mapped_column(String(120))
    # 数据用例与比较规则（payload: schema_version/entry_function/execution_model/binary_cases/groups）
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    compare_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    score_cap_rules: Mapped[list] = mapped_column(JSON, default=list)
    context_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    rubric_id: Mapped[int | None] = mapped_column(
        ForeignKey("question_rubrics.id"), nullable=True
    )
    source_kind: Mapped[str] = mapped_column(String(30), default="manual")
    source_digest: Mapped[str] = mapped_column(String(64))
    spec_digest: Mapped[str] = mapped_column(String(64))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CodeGrade(TimestampMixin, Base):
    """统一 AI 代码评分记录——XOR 外键关联 Submission 或 ExamAnswer"""
    __tablename__ = "code_grades"
    __table_args__ = (
        CheckConstraint(
            "(submission_id IS NULL) != (exam_answer_id IS NULL)",
            name="ck_code_grade_xor_target",
        ),
        Index("ix_code_grades_review_status", "needs_teacher_review", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int | None] = mapped_column(
        ForeignKey("submissions.id"), nullable=True, unique=True
    )
    exam_answer_id: Mapped[int | None] = mapped_column(
        ForeignKey("exam_answers.id"), nullable=True, unique=True
    )
    rubric_id: Mapped[int] = mapped_column(ForeignKey("question_rubrics.id"))
    mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    # 结果修订号（同 Submission.result_revision）：每次 AI 终态写入原子 +1
    result_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    functional_score: Mapped[float] = mapped_column(Float, default=0)
    algorithm_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    robustness_score: Mapped[float] = mapped_column(Float, default=0)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_score_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    scaled_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    deterministic_details: Mapped[dict] = mapped_column(JSON, default=dict)
    static_analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    ai_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_teacher_review: Mapped[bool] = mapped_column(default=False)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 认领代次（同 Submission.claim_token）：AI 评分写入必须带它做条件 UPDATE
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class GradeOverride(TimestampMixin, Base):
    """教师覆盖审计记录——不可变、不可级联删除"""
    __tablename__ = "grade_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_grade_id: Mapped[int] = mapped_column(ForeignKey("code_grades.id"), index=True)
    original_snapshot: Mapped[dict] = mapped_column(JSON)
    replacement_snapshot: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))


# ── 公告与已读回执 ─────────────────────────────────────────────


class Announcement(TimestampMixin, Base):
    """公告：全局（管理员）或课程（任课教师）范围，纯文本内容"""

    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    content: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(20), default="normal", index=True)
    scope: Mapped[str] = mapped_column(String(20), index=True)  # global / course
    course_id: Mapped[int | None] = mapped_column(
        ForeignKey("courses.id"), nullable=True, index=True
    )
    offering_id: Mapped[int | None] = mapped_column(ForeignKey("course_offerings.id"), nullable=True, index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    course: Mapped[Course | None] = relationship()
    author: Mapped[User] = relationship()


class AnnouncementRead(Base):
    """已读回执——(announcement_id, user_id) 唯一，标记已读幂等"""

    __tablename__ = "announcement_reads"
    __table_args__ = (
        UniqueConstraint("announcement_id", "user_id", name="uq_announcement_read_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    announcement_id: Mapped[int] = mapped_column(
        ForeignKey("announcements.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Notification(TimestampMixin, Base):
    """站内通知——当前由工作台待办与公告派生，已读状态持久化。"""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("recipient_id", "dedupe_key", name="uq_notification_recipient_dedupe"),
        Index("ix_notifications_recipient_visible", "recipient_id", "visible"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(String(500), default="")
    entity_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route: Mapped[str | None] = mapped_column(String(255), nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal", index=True)
    dedupe_key: Mapped[str] = mapped_column(String(180))
    visible: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))


class NotificationRead(Base):
    """通知已读回执——(notification_id, user_id) 唯一。"""

    __tablename__ = "notification_reads"
    __table_args__ = (
        UniqueConstraint("notification_id", "user_id", name="uq_notification_read_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    notification_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("notifications.id", ondelete="CASCADE"), index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserPreference(TimestampMixin, Base):
    """用户偏好——JSON 保存，当前支持侧栏折叠等前端展示偏好。"""

    __tablename__ = "user_preferences"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)


# ── 环境档位控制面（Phase 1：迁移 A） ─────────────────────────


class PackageCatalog(TimestampMixin, Base):
    """受控包目录——供应链输入唯一事实源。

    - 已被环境版本引用的条目不可原地修改包名/版本/import 名/来源；
      "编辑"创建新条目并通过 supersedes_id 关联旧条目。
    - "删除"统一实现为停用（status=inactive），不物理删除历史条目。
    """

    __tablename__ = "package_catalog"
    __table_args__ = (
        UniqueConstraint("normalized_name", "locked_version", "source_key", name="uq_pkg_name_version_source"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    normalized_name: Mapped[str] = mapped_column(String(128), nullable=False)
    pip_name: Mapped[str] = mapped_column(String(128), nullable=False)
    locked_version: Mapped[str] = mapped_column(String(64), nullable=False)
    import_names: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    category_tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_key: Mapped[str] = mapped_column(String(32), nullable=False)  # pypi | pytorch_cpu
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | inactive
    supersedes_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("package_catalog.id"), nullable=True
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    supersedes: Mapped["PackageCatalog | None"] = relationship(
        remote_side="PackageCatalog.id", foreign_keys=[supersedes_id]
    )
    version_links: Mapped[list["ProfileVersionPackage"]] = relationship(back_populates="package")


class EnvironmentProfile(TimestampMixin, Base):
    """环境档位——教师选择的环境维度；当前版本由显式指针决定。"""

    __tablename__ = "environment_profiles"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_env_profile_slug"),
        Index("fk_env_profiles_current_version", "current_version_id"),
        # Profile.current_version_id 与 EnvironmentVersion.profile_id 形成循环
        # 外键；迁移中同样在表创建后单独建立此约束。
        ForeignKeyConstraint(
            ["id", "current_version_id"],
            ["environment_versions.profile_id", "environment_versions.id"],
            use_alter=True,
            name="fk_env_profiles_current_version_profile",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | inactive
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    current_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    versions: Mapped[list["EnvironmentVersion"]] = relationship(
        back_populates="profile",
        order_by="EnvironmentVersion.version_number",
        foreign_keys="[EnvironmentVersion.profile_id]",
    )
    current_version: Mapped["EnvironmentVersion | None"] = relationship(
        foreign_keys=[current_version_id],
        primaryjoin="EnvironmentProfile.current_version_id == EnvironmentVersion.id",
        post_update=True,
    )


class EnvironmentVersion(TimestampMixin, Base):
    """不可变环境版本——进入 available 后包集合/基础镜像/资源参数/digest 全部冻结。

    status: draft | queued | building | available | failed | inactive
    停用只改变状态，不删除镜像或关联数据。
    """

    __tablename__ = "environment_versions"
    __table_args__ = (
        UniqueConstraint("profile_id", "version_number", name="uq_env_version_per_profile"),
        UniqueConstraint("profile_id", "id", name="uq_env_version_profile_id"),
        Index("ix_env_versions_profile_id", "profile_id"),
        Index("ix_env_versions_status", "status"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("environment_profiles.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("environment_versions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    # draft | queued | building | available | failed | inactive
    build_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="legacy", server_default="legacy"
    )
    base_image_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    image_tag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_digest: Mapped[str | None] = mapped_column(String(255), nullable=True)
    python_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="3.12", server_default="3.12"
    )
    minimum_memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    dockerfile_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    requested_spec: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: {"schema_version": 1, "python_packages": [], "system_packages": []},
    )
    resolved_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolution_lock: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolution_lock_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    # Phase 1 compatibility field; V2 reads resolved_spec first and falls back
    # to this field for historical rows.
    resolved_packages: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_published_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    profile: Mapped[EnvironmentProfile] = relationship(
        back_populates="versions",
        foreign_keys=[profile_id],
    )
    source_version: Mapped["EnvironmentVersion | None"] = relationship(
        remote_side="EnvironmentVersion.id", foreign_keys=[source_version_id]
    )
    package_links: Mapped[list["ProfileVersionPackage"]] = relationship(
        back_populates="version",
        order_by="ProfileVersionPackage.display_order",
    )


class ProfileVersionPackage(Base):
    """版本 × 包 关联——包集合必须关联版本，不可直接关联 profile。

    否则 v2 新增/升级包会污染 v1 的历史包集合。
    """

    __tablename__ = "profile_version_packages"

    environment_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("environment_versions.id"), primary_key=True
    )
    package_catalog_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("package_catalog.id"), primary_key=True
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)

    version: Mapped[EnvironmentVersion] = relationship(back_populates="package_links")
    package: Mapped[PackageCatalog] = relationship(back_populates="version_links")


class EnvironmentBuildJob(TimestampMixin, Base):
    """环境构建任务——DB 是任务事实源，Redis list 只负责唤醒。

    状态机：queued → building → succeeded
                              ↘ failed
                              ↘ timed_out
    """

    __tablename__ = "environment_build_jobs"
    __table_args__ = (
        Index("ix_env_build_jobs_status_created", "status", "created_at"),
        Index("ix_env_build_jobs_version_id", "environment_version_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    environment_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("environment_versions.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    # queued | building | succeeded | failed | timed_out
    build_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="legacy", server_default="legacy"
    )
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_of_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("environment_build_jobs.id"), nullable=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    build_config_fingerprint: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    log_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    version: Mapped[EnvironmentVersion] = relationship()
    retry_of: Mapped["EnvironmentBuildJob | None"] = relationship(
        remote_side="EnvironmentBuildJob.id", foreign_keys=[retry_of_id]
    )


class EnvironmentDraft(TimestampMixin, Base):
    """每个 Profile 唯一的管理员可编辑草稿。"""

    __tablename__ = "environment_drafts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["profile_id", "source_version_id"],
            ["environment_versions.profile_id", "environment_versions.id"],
            name="fk_env_drafts_source_version_profile",
        ),
        ForeignKeyConstraint(
            ["profile_id", "candidate_version_id"],
            ["environment_versions.profile_id", "environment_versions.id"],
            name="fk_env_drafts_candidate_version_profile",
        ),
        CheckConstraint(
            "state IN ('editing', 'building', 'ready', 'failed')",
            name="ck_env_drafts_state",
        ),
        Index("ix_env_drafts_candidate_version_id", "candidate_version_id"),
        Index("fk_env_drafts_source_version", "source_version_id"),
        Index("ix_env_drafts_active_build_job_id", "active_build_job_id"),
    )

    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("environment_profiles.id", ondelete="CASCADE"), primary_key=True
    )
    source_version_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    candidate_version_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    active_build_job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("environment_build_jobs.id"), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="editing", server_default="editing")
    python_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="3.12", server_default="3.12"
    )
    minimum_memory_mb: Mapped[int] = mapped_column(
        Integer, nullable=False, default=256, server_default="256"
    )
    requested_spec: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: {"schema_version": 1, "python_packages": [], "system_packages": []},
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    profile: Mapped[EnvironmentProfile] = relationship(
        foreign_keys=[profile_id],
        primaryjoin="EnvironmentDraft.profile_id == EnvironmentProfile.id",
    )
    source_version: Mapped["EnvironmentVersion | None"] = relationship(
        foreign_keys=[source_version_id],
        primaryjoin="EnvironmentDraft.source_version_id == EnvironmentVersion.id",
    )
    candidate_version: Mapped["EnvironmentVersion | None"] = relationship(
        foreign_keys=[candidate_version_id],
        primaryjoin="EnvironmentDraft.candidate_version_id == EnvironmentVersion.id",
    )
    active_build_job: Mapped["EnvironmentBuildJob | None"] = relationship(
        foreign_keys=[active_build_job_id],
        primaryjoin="EnvironmentDraft.active_build_job_id == EnvironmentBuildJob.id",
    )


class EnvironmentPublication(TimestampMixin, Base):
    """不可修改的发布/回滚审计记录。"""

    __tablename__ = "environment_publications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["profile_id", "version_id"],
            ["environment_versions.profile_id", "environment_versions.id"],
            name="fk_env_publications_version_profile",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["profile_id", "previous_version_id"],
            ["environment_versions.profile_id", "environment_versions.id"],
            name="fk_env_publications_previous_version_profile",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "action IN ('publish', 'rollback', 'migration_baseline')",
            name="ck_env_publications_action",
        ),
        Index("ix_env_publications_profile_created", "profile_id", "created_at"),
        Index("ix_env_publications_version_id", "version_id"),
        Index("fk_env_publications_previous_version", "previous_version_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("environment_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    version_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    previous_version_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    published_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    profile: Mapped[EnvironmentProfile] = relationship(foreign_keys=[profile_id])
    version: Mapped[EnvironmentVersion] = relationship(foreign_keys=[version_id])
    previous_version: Mapped["EnvironmentVersion | None"] = relationship(
        foreign_keys=[previous_version_id]
    )


class AccountSecurityPolicy(TimestampMixin, Base):
    __tablename__ = "account_security_policies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    password_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    temporary_days: Mapped[int] = mapped_column(Integer, default=7, server_default="7")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class AdminBatch(TimestampMixin, Base):
    __tablename__ = "admin_batches"
    __table_args__ = (Index("ix_admin_batches_status_created", "status", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="preview")
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    preview_digest: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    password_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)


class AdminBatchItem(Base):
    __tablename__ = "admin_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="uq_admin_batch_item_row"),
        Index("ix_admin_batch_items_status", "batch_id", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("admin_batches.id", ondelete="CASCADE"))
    row_number: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expected_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    prepared_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_target_created", "target_type", "target_id", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(80))
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

# Additive lab domain, imported after legacy model definitions to avoid cycles.
from .lab import (LabAssessment, LabRecord, LabRun, LabSubmission, LabGradeRevision,
                  LabRegradeBatch, LabResourceLease)  # noqa: E402,F401
from .lab_data import LabDatasetVersion, LabArtifact, LabFile  # noqa: E402,F401

from .teaching import CourseOffering, OfferingEnrollment, OfferingExperiment  # noqa: E402,F401

from .credential_delivery import CredentialDelivery  # noqa: E402,F401
from .course_material import CourseMaterial, ChapterActivityLink  # noqa: E402,F401

from .gradebook import Gradebook, GradebookCategory, GradebookColumn, GradebookEntry, GradebookAdjustment, GradebookSnapshot  # noqa: E402,F401

from .kernel_exec import KernelExecution  # noqa: E402,F401

from .ai_setup import AiGradingSetupJob, RubricTrialRun  # noqa: E402,F401
from .appeal import GradeAppeal  # noqa: E402,F401

from .exam_event import ExamEvent  # noqa: E402,F401
