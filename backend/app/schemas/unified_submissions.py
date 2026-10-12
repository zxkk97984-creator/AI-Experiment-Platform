"""统一提交中心契约——实验 / 作业 / 考试提交合并列表。"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas import PaginatedResponse


class UnifiedSubmissionRead(BaseModel):
    kind: str  # experiment | assignment | exam
    id: int
    student_name: str | None = None
    student_no: str | None = None
    entry_title: str | None = None
    course_id: int | None = None
    course_title: str | None = None
    status: str | None = None
    status_tone: str = "neutral"
    tests_passed: int | None = None
    tests_total: int | None = None
    ai_score: float | None = None
    score: float | None = None
    submitted_at: datetime | None = None
    route: str


class UnifiedSubmissionSummary(BaseModel):
    total: int = 0
    pending: int = 0
    graded: int = 0
    review: int = 0
    failed: int = 0


class UnifiedSubmissionFilterOption(BaseModel):
    id: int
    name: str
    kind: str | None = None


class UnifiedSubmissionFilterOptions(BaseModel):
    courses: list[UnifiedSubmissionFilterOption] = Field(default_factory=list)
    entries: list[UnifiedSubmissionFilterOption] = Field(default_factory=list)


class UnifiedSubmissionListRead(PaginatedResponse):
    items: list[UnifiedSubmissionRead] = Field(default_factory=list)
    summary: UnifiedSubmissionSummary = Field(default_factory=UnifiedSubmissionSummary)
    filter_options: UnifiedSubmissionFilterOptions = Field(default_factory=UnifiedSubmissionFilterOptions)


class TeacherJudgeSubmissionRead(BaseModel):
    """教师作业提交详情——包含学生、题目、作业与 AI 评分上下文。"""

    id: int
    question_id: int
    student_id: int
    student_name: str | None = None
    student_no: str | None = None
    code: str
    status: str
    grading_status: str
    score: float | None = None
    result_revision: int = 0
    created_at: datetime | None = None
    finished_at: datetime | None = None
    tests_passed: int | None = None
    tests_total: int | None = None
    result_details: dict | None = None
    execution_time_ms: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    question_title: str | None = None
    assignment_id: int | None = None
    assignment_title: str | None = None
    course_id: int | None = None
    course_title: str | None = None
    ai_grade_id: int | None = None
    ai_score: float | None = None
    ai_needs_review: bool = False
    ai_review_reason: str | None = None
