from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.environments import EnvironmentSummaryRead, ImportDiagnosticRead
from app.roles import UserRole
from app.security import validate_password_rules

# ── 输入硬上限（TASK-004） ────────────────────────────────────
# 超限在 Schema 校验层拒绝（422），发生在写库、入队或启动 Docker 之前。
MAX_CODE_CHARS = 50_000  # 代码/隐藏测试：字符数上限
MAX_CODE_BYTES = 64 * 1024  # 代码/隐藏测试：UTF-8 字节上限
MAX_TEXT_ANSWER_CHARS = 20_000  # 考试文本答案单项：字符数上限
MAX_TEXT_ANSWER_BYTES = 64 * 1024  # 考试文本答案单项：UTF-8 字节上限


def _utf8_bytes(v: str) -> int:
    return len(v.encode("utf-8"))


def _bounded(v: str, label: str, max_chars: int, max_bytes: int) -> str:
    if len(v) > max_chars:
        raise ValueError(f"{label} 超过字符上限 {max_chars}")
    if _utf8_bytes(v) > max_bytes:
        raise ValueError(f"{label} UTF-8 字节数超过上限 {max_bytes}")
    return v


def validate_code_size(v: str) -> str:
    return _bounded(v, "代码", MAX_CODE_CHARS, MAX_CODE_BYTES)


def validate_text_answer_size(v: str) -> str:
    return _bounded(v, "文本答案", MAX_TEXT_ANSWER_CHARS, MAX_TEXT_ANSWER_BYTES)


class PaginatedResponse(BaseModel):
    items: list[Any]
    page: int = 1
    page_size: int = 20
    total: int = 0


# ── AI 服务状态（TASK-020 / F-21） ─────────────────────────────


class AIServiceStatus(BaseModel):
    enabled: bool
    ready: bool


# ── 学习进度（TASK-018 / F-06） ────────────────────────────────


class LessonProgressRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    lesson_id: int
    status: Literal["in_progress", "completed"]
    last_accessed_at: datetime | None


class CourseProgressRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    course_id: int
    total: int
    completed: int
    percent: int
    next_lesson_id: int | None
    items: list[LessonProgressRead]


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str | None = None  # 可选：Cookie 模式下由 auth endpoint 读取


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    student_no: str | None = None
    real_name: str
    department: str | None = None
    role: UserRole
    status: str
    nickname: str | None = None
    must_change_password: bool = False
    temporary_password_expires_at: datetime | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserRead


class UserCreate(BaseModel):
    username: str = Field(default="", max_length=80)
    password: str | None = None
    real_name: str = Field(min_length=1, max_length=120)
    department: str | None = Field(default=None, max_length=120)
    student_no: str | None = Field(default=None, max_length=80)
    role: UserRole
    status: Literal["active", "disabled"] = "active"

    @field_validator("password")
    @classmethod
    def _limit_password(cls, v: str | None) -> str | None:
        if v is not None:
            validate_password_rules(v)
        return v


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=500)
    real_name: str | None = Field(default=None, min_length=1, max_length=120)
    department: str | None = Field(default=None, max_length=120)
    student_no: str | None = Field(default=None, max_length=80)
    role: UserRole | None = None
    status: Literal["active", "disabled"] | None = None


class PasswordUpdate(BaseModel):
    # 本人改密必须提交 current_password；管理员重置无需旧密码
    password: str
    current_password: str | None = None

    @field_validator("password")
    @classmethod
    def _limit_password(cls, v: str) -> str:
        validate_password_rules(v)
        return v


class StatusUpdate(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
    status: Literal["active", "disabled"]


# class 为新的“教学班可见”；public 仅作为旧数据/旧客户端兼容值保留。
CourseVisibility = Literal["private", "class", "whitelist", "public"]


class AcademicTermCreate(BaseModel):
    code: str
    name: str
    start_date: date
    end_date: date
    status: Literal["planned", "active", "closed"] = "planned"

    @model_validator(mode="after")
    def _dates_in_order(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date 不能早于 start_date")
        return self


class AcademicTermUpdate(BaseModel):
    code: str | None = None
    name: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: Literal["planned", "active", "closed"] | None = None


class AcademicTermRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    start_date: date
    end_date: date
    status: str


class TeachingClassCreate(BaseModel):
    academic_term_id: int
    code: str
    name: str
    status: Literal["active", "archived"] = "active"


class TeachingClassUpdate(BaseModel):
    code: str | None = None
    name: str | None = None
    status: Literal["active", "archived"] | None = None


class TeachingClassSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    academic_term_id: int
    code: str
    name: str
    status: str
    student_count: int = 0


class TeachingClassStudentBatch(BaseModel):
    student_ids: list[int]


class CourseCreate(BaseModel):
    title: str
    code: str | None = None
    description: str | None = None
    # 创建只允许草稿；发布必须走 PATCH 且经过完整性门禁（COURSE_INCOMPLETE）
    status: Literal["draft"] = "draft"
    cover: str | None = None
    start_time: datetime | None = None
    visibility: CourseVisibility = "class"  # 可见范围：仅自己 / 教学班 / 指定学生
    default_score: float = 100.0  # 默认评分（满分制）
    academic_term_id: int | None = None
    teaching_class_ids: list[int] = Field(default_factory=list)


class CourseUpdate(BaseModel):
    """更新课程：全部字段可选，仅更新传入字段"""
    title: str | None = None
    code: str | None = None
    description: str | None = None
    status: Literal["draft", "published", "archived"] | None = None
    cover: str | None = None
    start_time: datetime | None = None
    visibility: CourseVisibility | None = None
    default_score: float | None = None
    academic_term_id: int | None = None
    teaching_class_ids: list[int] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_null_settings(cls, data: Any) -> Any:
        """可见范围与默认评分为非空字段，显式传 null 直接 422（而非落库 500）"""
        if isinstance(data, dict):
            if "visibility" in data and data["visibility"] is None:
                raise ValueError("visibility 不能为 null")
            if "default_score" in data and data["default_score"] is None:
                raise ValueError("default_score 不能为 null")
        return data


class CourseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str | None = None
    title: str
    description: str | None = None
    status: str
    teacher_id: int | None = None
    cover: str | None = None
    start_time: datetime | None = None
    visibility: CourseVisibility = "class"
    default_score: float = 100.0
    academic_term_id: int | None = None
    academic_term: AcademicTermRead | None = None
    teaching_classes: list[TeachingClassSummary] = Field(default_factory=list)
    chapter_count: int = 0
    lesson_count: int = 0
    student_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # 学生视角选课状态（教师/管理员返回 false 默认值）
    is_enrolled: bool = False
    can_enroll: bool = False
    enrollment_origin: str | None = None


class CourseListSummary(BaseModel):
    total: int = 0
    published: int = 0
    draft: int = 0
    archived: int = 0


class CourseListRead(PaginatedResponse):
    summary: CourseListSummary = Field(default_factory=CourseListSummary)


class CourseStudentRead(BaseModel):
    id: int
    username: str
    student_no: str | None = None
    real_name: str
    status: str
    enrollment_origin: str
    teaching_classes: list[TeachingClassSummary] = Field(default_factory=list)


class CourseStudentCreate(BaseModel):
    student_id: int


class CourseStudentImportRow(BaseModel):
    row: int
    student_no: str | None = None
    username: str | None = None
    status: Literal["created", "updated", "skipped"] = "skipped"
    message: str = ""


class CourseStudentImportResult(BaseModel):
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[CourseStudentImportRow] = Field(default_factory=list)


# ── 课程白名单 ─────────────────────────────────────────────────


class CourseWhitelistCreate(BaseModel):
    student_id: int


class CourseWhitelistEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    course_id: int
    student: UserRead
    created_at: datetime


class CourseWhitelistListRead(BaseModel):
    items: list[CourseWhitelistEntryRead] = Field(default_factory=list)
    page: int = 1
    page_size: int = 20
    total: int = 0


class ChapterCreate(BaseModel):
    title: str
    order_index: int = 0


class ChapterUpdate(BaseModel):
    """编辑章节：标题 / 排序位置均为可选"""
    title: str | None = None
    order_index: int | None = None


def normalize_video_url(v: str | None) -> str | None:
    """归一化并校验外链地址：空白归一为 None，非空只允许 http(s)，长度上限 500。"""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if len(v) > 500:
        raise ValueError("video_url 长度不能超过 500 字符")
    if not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("video_url 只允许 http:// 或 https:// 地址")
    return v


class LessonCreate(BaseModel):
    title: str
    content_type: str = "markdown"
    content: str | None = None
    notebook_path: str | None = None
    video_url: str | None = None
    due_at: datetime | None = None
    order_index: int = 0
    status: Literal["draft", "published", "pending"] = "draft"

    @field_validator("video_url")
    @classmethod
    def _normalize_video_url(cls, v: str | None) -> str | None:
        """外链校验：空白归一为 None，只允许 http(s) 地址，拒绝 javascript:/data:/本地路径/无 scheme"""
        return normalize_video_url(v)


class LessonUpdate(BaseModel):
    """更新课时：全部字段可选；chapter_id 用于移动到其他章节"""
    title: str | None = None
    content_type: str | None = None
    content: str | None = None
    notebook_path: str | None = None
    video_url: str | None = None
    due_at: datetime | None = None
    order_index: int | None = None
    chapter_id: int | None = None
    status: Literal["draft", "published", "pending"] | None = None

    @field_validator("video_url")
    @classmethod
    def _normalize_video_url(cls, v: str | None) -> str | None:
        """外链校验：空白归一为 None，只允许 http(s) 地址，拒绝 javascript:/data:/本地路径/无 scheme"""
        return normalize_video_url(v)


class LessonRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chapter_id: int
    title: str
    content_type: str
    content: str | None = None
    # Notebook 编辑入口需要读取已有绑定；模板仍是服务端内部实现细节
    template_id: int | None = None
    notebook_path: str | None = None
    video_url: str | None = None
    # 视频来源：external（外链）/ upload（本地上传）；storage_key 仅服务端使用，不返回
    video_source: str = "external"
    video_filename: str | None = None
    video_content_type: str | None = None
    video_size: int | None = None
    due_at: datetime | None = None
    order_index: int
    status: str = "published"


class LessonVideoPlaybackRead(BaseModel):
    """已签名短期播放地址"""
    url: str
    expires_at: datetime


class LessonVideoUploadRead(BaseModel):
    """上传成功响应：课时最新状态 + 可直接播放的签名地址"""
    lesson: LessonRead
    playback_url: str
    expires_at: datetime


class ChapterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    course_id: int
    title: str
    order_index: int
    lessons: list[LessonRead] = Field(default_factory=list)


class EnrollmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    course_id: int
    student_id: int
    status: str


class AssignmentCreate(BaseModel):
    offering_id: int | None = Field(default=None, gt=0)
    course_id: int
    title: str
    description: str | None = None
    # 创建只允许草稿；发布必须走 POST /assignments/{id}/publish
    status: Literal["draft"] = "draft"
    due_at: datetime | None = None
    # ── 环境档位绑定（Phase 4：教师选择） ─────────────────────
    # 教师显式选择 available 环境版本；省略时服务层解析 basic 当前可用版本。
    environment_version_id: int | None = None
    import_policy_mode: Literal["unrestricted", "restricted"] = "unrestricted"
    allowed_imports: list[str] = Field(default_factory=list)
    audience_mode: Literal["all_enrolled", "selected_classes", "whitelist_only"] = "all_enrolled"
    audience_class_ids: list[int] = Field(default_factory=list)
    whitelist_student_ids: list[int] = Field(default_factory=list)
    excluded_student_ids: list[int] = Field(default_factory=list)

    @field_validator("allowed_imports")
    @classmethod
    def _check_allowed_imports(cls, v: list[str]) -> list[str]:
        from app.services.import_policy import validate_import_names

        return validate_import_names(v)


class AssignmentUpdate(BaseModel):
    """更新作业：status 不可修改，发布/取消发布只能走 /publish 与 /unpublish"""
    title: str | None = None
    description: str | None = None
    due_at: datetime | None = None
    # 环境字段仅在作业 draft 状态可修改（API 层门禁）；exclude_unset 区分未传与显式 null
    environment_version_id: int | None = None
    import_policy_mode: Literal["unrestricted", "restricted"] | None = None
    allowed_imports: list[str] | None = None
    audience_mode: Literal["all_enrolled", "selected_classes", "whitelist_only"] | None = None
    audience_class_ids: list[int] | None = None
    whitelist_student_ids: list[int] | None = None
    excluded_student_ids: list[int] | None = None

    @field_validator("allowed_imports")
    @classmethod
    def _check_allowed_imports(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        from app.services.import_policy import validate_import_names

        return validate_import_names(v)


class AssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    offering_id: int | None = None
    id: int
    course_id: int
    title: str
    description: str | None = None
    status: str
    due_at: datetime | None = None
    published_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # 作业默认环境与 import 教学策略（教师读响应返回；学生端摘要由 Phase 5 提供）
    environment_version_id: int | None = None
    import_policy_mode: str = "unrestricted"
    allowed_imports: list[str] = Field(default_factory=list)
    # Phase 5：学生可见环境摘要（不含 digest/tag/构建日志）；未绑定或不可用时为 None
    environment_summary: EnvironmentSummaryRead | None = None
    # 当前学生对作业的提交状态：全部题目都有提交记录才算已交（与 dashboard 待办语义互补）。
    # 仅学生作业列表接口计算该值；教师/管理员视图与详情接口保持默认 False。
    is_submitted: bool = False
    audience_mode: str = "all_enrolled"
    audience_class_ids: list[int] = Field(default_factory=list)
    whitelist_student_ids: list[int] = Field(default_factory=list)
    excluded_student_ids: list[int] = Field(default_factory=list)


class PublicCase(BaseModel):
    """公开样例——统一格式：{"args": [1, 2], "expected": 3}

    自动迁移旧格式 {"input": [1,2], "expected": 3} → {"args": [1,2], "expected": 3}。
    expected 为必填；拒绝同时传入冲突的 input 和 args。
    """
    model_config = ConfigDict(extra="allow")
    args: list[Any] = Field(default_factory=list)
    expected: Any = Field(...)

    @model_validator(mode="before")
    @classmethod
    def _migrate_input_to_args(cls, data: Any) -> Any:
        """迁移旧格式：input → args；拒绝同时传入非空 input 和 args。

        修复：先保存 input 的值，再构建不含 input 的新 dict，
        确保结果中不残留 input 字段，且不修改调用者的原 dict。
        """
        if not isinstance(data, dict):
            return data
        has_input = "input" in data
        has_args = "args" in data  # 仅检查 key 存在性，不检查值是否为空
        if has_input and has_args:
            raise ValueError("不能同时传入 input 和 args，请统一使用 args")
        if has_input:
            # 保存 input 的值，构建不含 input 字段的新 dict
            input_val = data["input"]
            data = {k: v for k, v in data.items() if k != "input"}
            data["args"] = input_val
        return data

    @model_validator(mode="after")
    def _reject_unknown_fields(self) -> "PublicCase":
        """拒绝未知多余字段（模拟 extra=forbid 但排除已处理的 input）"""
        # 此方法保留以兼容未来扩展——当前 extra=allow 不做额外检查
        return self


class JudgeQuestionCreate(BaseModel):
    title: str
    description: str | None = None
    function_name: str
    signature: str | None = None
    starter_code: str | None = None
    public_cases: list[PublicCase] = Field(default_factory=list)
    hidden_tests: str
    time_limit_ms: int = 10000
    memory_limit_mb: int = 256
    grading_mode: Literal["legacy", "shadow", "active"] | None = None
    # ── 环境档位绑定（Phase 4：题目覆盖） ─────────────────────
    # environment_version_id 为 None 表示继承作业默认环境；
    # import_policy_mode：inherit 继承作业 / unrestricted 不限制 / restricted 自定义白名单。
    environment_version_id: int | None = None
    import_policy_mode: Literal["inherit", "unrestricted", "restricted"] = "inherit"
    allowed_imports: list[str] = Field(default_factory=list)

    @field_validator("allowed_imports")
    @classmethod
    def _check_allowed_imports(cls, v: list[str]) -> list[str]:
        from app.services.import_policy import validate_import_names

        return validate_import_names(v)

    @field_validator("starter_code", "hidden_tests")
    @classmethod
    def _limit_code_fields(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return validate_code_size(v)


class JudgeQuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    assignment_id: int
    title: str
    description: str | None = None
    function_name: str
    signature: str | None = None
    starter_code: str | None = None
    public_cases: list[Any] = Field(default_factory=list)
    time_limit_ms: int
    memory_limit_mb: int
    grading_mode: Literal["legacy", "shadow", "active"]
    environment_version_id: int | None = None
    import_policy_mode: str = "inherit"
    allowed_imports: list[str] = Field(default_factory=list)
    # Phase 5：题目 effective environment summary（覆盖时显示题目环境，否则作业默认）
    environment_summary: EnvironmentSummaryRead | None = None


class JudgeQuestionUpdate(BaseModel):
    """作业编程题更新——所有字段可选，仅更新传入字段"""
    title: str | None = None
    description: str | None = None
    function_name: str | None = None
    signature: str | None = None
    starter_code: str | None = None
    public_cases: list[PublicCase] | None = None
    hidden_tests: str | None = None
    time_limit_ms: int | None = None
    memory_limit_mb: int | None = None
    grading_mode: Literal["legacy", "shadow", "active"] | None = None
    environment_version_id: int | None = None
    import_policy_mode: Literal["inherit", "unrestricted", "restricted"] | None = None
    allowed_imports: list[str] | None = None

    @field_validator("allowed_imports")
    @classmethod
    def _check_allowed_imports(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        from app.services.import_policy import validate_import_names

        return validate_import_names(v)

    @field_validator("starter_code", "hidden_tests")
    @classmethod
    def _limit_code_fields(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return validate_code_size(v)


class SubmissionCreate(BaseModel):
    question_id: int
    code: str

    @field_validator("code")
    @classmethod
    def _limit_code(cls, v: str) -> str:
        return validate_code_size(v)


class SubmissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question_id: int
    student_id: int
    code: str
    status: str
    stdout: str | None = None
    stderr: str | None = None
    score: float | None = None
    result_details: dict | None = None
    tests_passed: int | None = None
    tests_total: int | None = None
    execution_time_ms: int | None = None
    grading_breakdown: dict | None = None  # 仅 active 模式学生可见
    # Phase 5：结构化 import/环境诊断——学生 API 只返回安全中文信息，不含裸 traceback
    diagnostic: ImportDiagnosticRead | None = None


class SampleRunResponse(BaseModel):
    """sample-run 响应"""
    output: str = ""
    status: str = ""
    execution_time_ms: int = 0
    # Phase 5：结构化 import 诊断（IMPORT_NOT_ALLOWED 等），None 表示无诊断
    diagnostic: ImportDiagnosticRead | None = None


class ExamSampleRunRequest(BaseModel):
    """考试编程题公开样例自测；题目身份仅取自 URL，避免请求体歧义。"""
    code: str

    @field_validator("code")
    @classmethod
    def _limit_code(cls, value: str) -> str:
        return validate_code_size(value)


class ExamCreate(BaseModel):
    offering_id: int | None = Field(default=None, gt=0)
    course_id: int
    title: str
    status: Literal["draft"] = "draft"
    duration_minutes: int = 60
    start_at: datetime | None = None
    end_at: datetime | None = None
    show_score_after_grading: bool = False
    show_questions_after_review: bool = False
    show_answers_after_review: bool = False
    audience_mode: Literal["all_enrolled", "selected_classes", "whitelist_only"] = "all_enrolled"
    audience_class_ids: list[int] = Field(default_factory=list)
    whitelist_student_ids: list[int] = Field(default_factory=list)
    excluded_student_ids: list[int] = Field(default_factory=list)


class ExamUpdate(BaseModel):
    title: str | None = None
    status: Literal["draft", "published"] | None = None
    duration_minutes: int | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    show_score_after_grading: bool | None = None
    show_questions_after_review: bool | None = None
    show_answers_after_review: bool | None = None
    audience_mode: Literal["all_enrolled", "selected_classes", "whitelist_only"] | None = None
    audience_class_ids: list[int] | None = None
    whitelist_student_ids: list[int] | None = None
    excluded_student_ids: list[int] | None = None


class ExamRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    offering_id: int | None = None
    id: int
    course_id: int
    title: str
    status: str
    duration_minutes: int
    start_at: datetime | None = None
    end_at: datetime | None = None
    show_score_after_grading: bool = False
    show_questions_after_review: bool = False
    show_answers_after_review: bool = False
    review_released_at: datetime | None = None
    max_score: float = 0
    server_now: datetime | None = None
    student_status: str | None = None
    is_completed: bool = False
    can_start: bool = False
    # 当前学生对考试的已交状态：存在 submitted/grading/graded 任一状态的提交记录即视为已考
    # （与 dashboard 待办语义一致）。仅学生考试列表接口计算该值；教师/管理员视图与详情接口保持默认 False。
    is_submitted: bool = False
    audience_mode: str = "all_enrolled"
    audience_class_ids: list[int] = Field(default_factory=list)
    whitelist_student_ids: list[int] = Field(default_factory=list)
    excluded_student_ids: list[int] = Field(default_factory=list)


class ExamSubmitRequest(BaseModel):
    score: float = 0


class ExamAnswerSaveItem(BaseModel):
    question_id: int
    selected_options: list[str] | None = None
    code_answer: str | None = None
    text_answers: dict[str, str] | None = None
    expected_version: int = 0

    @field_validator("code_answer")
    @classmethod
    def _limit_code_answer(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return validate_code_size(v)

    @field_validator("text_answers")
    @classmethod
    def _limit_text_answers(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return v
        return {key: validate_text_answer_size(value) for key, value in v.items()}


class ExamAnswerBatchRequest(BaseModel):
    answers: list[ExamAnswerSaveItem] = Field(default_factory=list, max_length=200)


class ExamTimeExtensionRequest(BaseModel):
    minutes: int = Field(gt=0, le=180)


class ExamSessionRead(BaseModel):
    id: int | None = None
    status: str | None = None
    expires_at: datetime | None = None
    score: float | None = None
    exam: dict
    submission: dict | None = None
    questions: list[dict] = Field(default_factory=list)
    saved_answers: list[dict] = Field(default_factory=list)
    visibility: dict = Field(default_factory=dict)
    server_now: datetime


class ExamQuestionCreate(BaseModel):
    question_type: str
    prompt: str
    options: dict | None = None
    correct_answer: dict = Field(default_factory=dict)
    points: float = 1
    order_index: int = 0
    starter_code: str | None = None
    public_cases: list[PublicCase] | None = None
    hidden_tests: str | None = None
    time_limit_ms: int | None = None
    memory_limit_mb: int | None = None
    grading_mode: Literal["legacy", "shadow", "active"] | None = None


class ExamQuestionUpdate(BaseModel):
    """考试题目更新——所有字段可选，仅更新传入的字段"""
    question_type: str | None = None
    prompt: str | None = None
    options: dict | None = None
    correct_answer: dict | None = None
    points: float | None = None
    order_index: int | None = None
    starter_code: str | None = None
    public_cases: list[PublicCase] | None = None
    hidden_tests: str | None = None
    time_limit_ms: int | None = None
    memory_limit_mb: int | None = None


class ExamQuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    exam_id: int
    question_type: str
    prompt: str
    options: dict | None = None
    points: float
    order_index: int
    starter_code: str | None = None
    public_cases: list | None = None
    grading_mode: Literal["legacy", "shadow", "active"]


class ExamQuestionTeacherRead(ExamQuestionRead):
    """教师题目视图——包含编辑所需私有字段，禁止用于学生响应。"""

    correct_answer: dict = Field(default_factory=dict)
    hidden_tests: str | None = None
    time_limit_ms: int | None = None
    memory_limit_mb: int | None = None
    grading_mode: Literal["legacy", "shadow", "active"] | None = None
    teacher_constraints: dict = Field(default_factory=dict)
    reference_solution: str | None = None
    test_groups: list = Field(default_factory=list)
    score_cap_rules: list = Field(default_factory=list)
    has_locked_rubric: bool = False


class ExamAnswerScoreUpdate(BaseModel):
    """教师手动修改逐题得分——分值不可低于 0 或高于本题满分（后端再校验），且必须填写改分理由。"""

    score: float = Field(ge=0, allow_inf_nan=False)
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("改分理由至少 3 个字符")
        return value


class ExamSubmissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    exam_id: int
    student_id: int
    status: str
    score: float | None = None
    started_at: datetime | None = None
    expires_at: datetime | None = None
    last_saved_at: datetime | None = None
    submission_reason: str | None = None
    submitted_at: datetime | None = None
    review_reason: str | None = None
    review_required_at: datetime | None = None


class ExamGradeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    exam_id: int
    student_id: int
    score: float


class ExamRetryRequest(BaseModel):
    """显式重试 review_required 提交——选中的 system_error 答案"""

    answer_ids: list[int]


# ── Notebook 模板 Schemas ─────────────────────────────────────


class NotebookCellSchema(BaseModel):
    """统一的 Cell 定义"""
    id: str
    type: str  # "markdown" | "code"
    source: str = ""
    order: int = 0
    student_editable: bool = True
    source_hidden: bool = False


class NotebookTemplateCreate(BaseModel):
    name: str
    description: str | None = None


class NotebookTemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class NotebookTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None = None
    status: str
    current_version_id: int | None = None
    owner_id: int
    draft_revision: int


class NotebookTemplateVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    template_id: int
    version_number: int
    sha256: str
    cells: list[NotebookCellSchema] = Field(default_factory=list)
    cell_order: list[str] = Field(default_factory=list)
    assets_dir: str | None = None
    published_at: datetime


# ── 实验模块 Schemas ──────────────────────────────────────────


class ExperimentModuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str | None = None
    template_id: int | None = None
    due_at: datetime | None = None
    # 创建只允许草稿；发布必须走 POST /experiments/modules/{id}/publish
    status: Literal["draft"] = "draft"


class ExperimentModuleUpdate(BaseModel):
    """更新模块：status 不可修改，发布/取消发布只能走 publish/unpublish 端点"""
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    description: str | None = None
    template_id: int | None = None
    due_at: datetime | None = None


class ExperimentModuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None = None
    entry_url: str | None = None
    template_id: int | None = None
    owner_id: int | None = None
    status: str
    due_at: datetime | None = None
    origin: str = "manual"


class StudentExperimentModuleRead(BaseModel):
    id: int
    name: str
    offering_id: int
    course_title: str | None = None
    term_name: str | None = None
    class_name: str | None = None
    learning_status: Literal["not_started", "started", "submitted", "graded"]
    last_learning_at: datetime | None = None


class StudentExperimentCatalogSummary(BaseModel):
    total: int = 0
    not_started: int = 0
    started: int = 0
    submitted: int = 0
    graded: int = 0


class StudentExperimentCatalogRead(BaseModel):
    items: list[StudentExperimentModuleRead] = Field(default_factory=list)
    page: int = 1
    page_size: int = 10
    total: int = 0
    summary: StudentExperimentCatalogSummary = Field(
        default_factory=StudentExperimentCatalogSummary
    )


# ── 统一实验记录 Schemas ──────────────────────────────────────


class ExperimentCellsSaveRequest(BaseModel):
    """学生保存 cells。仅 student_editable code cells。"""
    cells: dict[str, str]  # {cell_id: source_code}
    record_revision: int    # 乐观并发控制


class ExperimentCellExecuteRequest(BaseModel):
    code: str

    @field_validator("code")
    @classmethod
    def _limit_code(cls, v: str) -> str:
        return validate_code_size(v)


class ExperimentCellExecuteResponse(BaseModel):
    outputs: list[dict] = Field(default_factory=list)
    execution_time_ms: int | None = None
    execution_count: int = 0
    # Phase 5：Kernel 输出中的 ModuleNotFoundError 兜底归类诊断（无裸 traceback）
    diagnostic: ImportDiagnosticRead | None = None


class ExperimentCellOut(BaseModel):
    """返回给前端的 cell（学生视角不包含 source_hidden cells）"""
    id: str
    type: str  # "markdown" | "code"
    source: str = ""
    order: int = 0
    student_editable: bool = True
    # source_hidden=true 的 cell 完全不返回
    outputs: dict | None = None
    is_running: bool = False


class ExperimentRecordRead(BaseModel):
    offering_id: int | None = None
    id: int
    lesson_id: int | None = None
    module_id: int | None = None
    student_id: int
    status: str
    template_version_id: int
    record_revision: int
    cells_sources: dict[str, str] = Field(default_factory=dict)
    started_at: datetime | None = None
    submitted_at: datetime | None = None


class ExperimentSubmissionRead(BaseModel):
    """实验提交记录"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    lab_enabled: bool = False
    lab_status: str | None = None
    record_id: int
    attempt_number: int
    client_request_id: str | None = None
    cells_snapshot: dict = Field(default_factory=dict)
    submitted_at: datetime | None = None
    score: float | None = None
    feedback: str | None = None
    reviewed_by_id: int | None = None
    reviewed_at: datetime | None = None
    # 前端展示辅助字段（列表查询时填充）
    student_name: str | None = None
    student_username: str | None = None
    entry_name: str | None = None
    entry_id: int | None = None
    entry_type: Literal["lesson", "module"] | None = None
    course_id: int | None = None
    course_name: str | None = None


class ExperimentSubmissionSummary(BaseModel):
    total: int = 0
    pending: int = 0
    graded: int = 0


class ExperimentSubmissionFilterOption(BaseModel):
    id: int
    name: str


class ExperimentSubmissionFilterOptions(BaseModel):
    courses: list[ExperimentSubmissionFilterOption] = Field(default_factory=list)
    entries: list[ExperimentSubmissionFilterOption] = Field(default_factory=list)


class ExperimentSubmissionListRead(BaseModel):
    items: list[ExperimentSubmissionRead] = Field(default_factory=list)
    page: int = 1
    page_size: int = 10
    total: int = 0
    summary: ExperimentSubmissionSummary = Field(default_factory=ExperimentSubmissionSummary)
    filter_options: ExperimentSubmissionFilterOptions = Field(default_factory=ExperimentSubmissionFilterOptions)


class ExperimentCellMetadata(BaseModel):
    type: str
    order: int


class ExperimentSubmissionDetailRead(ExperimentSubmissionRead):
    """不可变提交快照及其展示上下文。"""

    # 模型层允许 NULL（历史数据 outputs_snapshot 为 NULL），端点归一化为 {} 后返回
    outputs_snapshot: dict | None = None
    cell_metadata: dict[str, ExperimentCellMetadata] = Field(default_factory=dict)


class ExperimentSubmitRequest(BaseModel):
    """实验提交请求——client_request_id 用于幂等"""
    client_request_id: UUID  # UUID v4，Pydantic 自动校验格式
    artifact_id: int | None = Field(default=None, gt=0)
    expected_record_revision: int | None = Field(default=None, ge=1)


class ExperimentReviewUpdate(BaseModel):
    """教师评分反馈"""
    score: float | None = None
    feedback: str | None = None


class ExperimentRecordDetailResponse(BaseModel):
    offering_id: int | None = None
    """GET /records/{id} 完整响应（学生不含 source_hidden cells）"""
    id: int
    lab_enabled: bool = False
    lesson_id: int | None = None
    module_id: int | None = None
    student_id: int
    status: str
    template_version_id: int
    record_revision: int
    entry_name: str = ""             # 模块名或课时名
    entry_description: str | None = None
    cells: list[ExperimentCellOut] = Field(default_factory=list)
    execution_count: int = 0
    # Phase 5：学生可见环境摘要（NotebookPlayer 标题下提示用，不含 digest/tag）
    environment_summary: EnvironmentSummaryRead | None = None


# ── Jupyter（旧）──────────────────────────────────────────────


class JupyterEntryResponse(BaseModel):
    iframe_url: str
    deprecated: bool = True


class ExperimentRecordCreate(BaseModel):
    """[已废弃] 旧实验记录创建"""
    module_id: int
    status: str = "started"
    metadata: dict[str, Any] = Field(default_factory=dict)


# Studio schemas live in a dedicated module to keep this compatibility module
# importable for existing API consumers.
from .studio import (  # noqa: E402
    StudioCell,
    StudioDraftUpdate,
    StudioAssetRead,
    StudioImportCreate,
    StudioImportExisting,
    StudioPreviewRunRequest,
    StudioPreviewRunResponse,
    StudioTemplateBindRequest,
    StudioTemplateCreate,
    StudioTemplateMetadataUpdate,
    StudioTemplateRead,
    StudioVersionRead,
)
