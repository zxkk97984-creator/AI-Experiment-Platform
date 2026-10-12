"""考试编程题的评分版本编辑契约（批次三 3C/3E）。

与作业侧同一套服务层（``grading_version_service``）与视图（``version_view``），
差别只有权限范围与题目类型：

* 教师必须拥有该考试所属课程/授课实例（``teacher_offering_ids``），管理员不受限；
* 启用/退役同步维护 ``exam_questions.current_grading_version_id``；
* 转换预检/应用读取考试题的 ``hidden_tests``（可选）与 ``function_name``。

考试执行路径在 ``judge_worker.process_exam_answer``：开考时冻结逐题版本，
答案只从冻结映射复制，考试中途改配置不影响已开始的考试。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.grading_versions import (
    AiDraftRequest,
    _assert_conversion_complete,
    ConversionApplyRequest,
    ConversionPreviewRequest,
    GradingVersionConfirmRequest,
    GradingVersionDraftRequest,
    require_version,
    version_view,
)
from app.dependencies import get_current_user, get_db
from app.errors import api_error
from app.models import Course, Exam, ExamQuestion, JudgeGradingVersion, User
from app.services import grading_conversion
from app.services import grading_version_service as versions
from app.services.offering_service import teacher_offering_ids

router = APIRouter(prefix="/exams", tags=["grading-versions"])

MAX_LIST_LIMIT = 50


def _require_exam_question(db: Session, question_id: int) -> ExamQuestion:
    question = db.get(ExamQuestion, question_id)
    if question is None:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")
    return question


def _require_exam_editor(db: Session, user: User, question: ExamQuestion) -> Exam:
    if user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "仅教师和管理员可以维护评分版本")
    exam = db.get(Exam, question.exam_id)
    if exam is None:
        raise api_error(403, "FORBIDDEN", "没有该考试的授课范围")
    if user.role == "teacher":
        scopes = teacher_offering_ids(db, user.id)
        course = db.get(Course, exam.course_id) if exam.course_id else None
        owns_course = course is not None and course.teacher_id == user.id
        if exam.offering_id not in scopes and not owns_course:
            raise api_error(403, "FORBIDDEN", "没有该考试的授课范围")
    return exam


def _require_exam_version(db: Session, version_id: int) -> JudgeGradingVersion:
    version = require_version(db, version_id)
    if version.exam_question_id is None:
        raise api_error(404, "GRADING_VERSION_NOT_FOUND", "评分版本不属于考试题")
    return version


def _exam_entry_function(question: ExamQuestion, requested: str | None) -> str:
    """考试题的入口函数：请求显式给出，或取自 ``correct_answer.function_name``。

    ``exam_questions`` 没有 ``function_name`` 列（与作业题不同），因此不做静默兜底：
    两者都没有时返回 422，避免把用例挂到错误的入口上。
    """
    if isinstance(requested, str) and requested.isidentifier():
        return requested
    candidate = (question.correct_answer or {}).get("function_name") \
        if isinstance(question.correct_answer, dict) else None
    if isinstance(candidate, str) and candidate.isidentifier():
        return candidate
    raise api_error(422, "ENTRY_FUNCTION_REQUIRED", "考试题未配置入口函数，请在请求中显式提供")


def _ensure_editable(db: Session, user: User, version: JudgeGradingVersion) -> ExamQuestion:
    question = _require_exam_question(db, version.exam_question_id)
    _require_exam_editor(db, user, question)
    return question


@router.get("/questions/{question_id}/grading-versions")
def list_exam_grading_versions(
    question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    question = _require_exam_question(db, question_id)
    _require_exam_editor(db, current_user, question)
    rows = db.scalars(
        select(JudgeGradingVersion)
        .where(JudgeGradingVersion.exam_question_id == question_id)
        .order_by(JudgeGradingVersion.version.desc())
        .limit(MAX_LIST_LIMIT)
    ).all()
    return {"items": [version_view(db, row) for row in rows],
            "current_grading_version_id": question.current_grading_version_id,
            "grading_mode": question.grading_mode}


@router.get("/questions/{question_id}/grading-versions/preflight")
def exam_grading_version_preflight(
    question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """考试题启用前核查：在途答案、无基线历史答案、仍在进行的考试场次。"""
    question = _require_exam_question(db, question_id)
    _require_exam_editor(db, current_user, question)
    return versions.enablement_preflight(db, question_kind="exam", question_id=question_id)


@router.post("/questions/{question_id}/grading-versions",
             status_code=status.HTTP_201_CREATED)
def create_exam_grading_version_draft(
    question_id: int,
    payload: GradingVersionDraftRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    question = _require_exam_question(db, question_id)
    _require_exam_editor(db, current_user, question)
    try:
        version = versions.create_draft(
            db, question_kind="exam", question_id=question_id,
            payload=payload.payload, entry_function=payload.entry_function,
            compare_rules=payload.compare_rules, score_cap_rules=payload.score_cap_rules,
            context_snapshot=payload.context_snapshot, rubric_id=payload.rubric_id,
            source_kind="manual", execution_mode=payload.execution_mode,
            actor_id=current_user.id,
        )
    except versions.GradingVersionError as exc:
        raise api_error(422, "INVALID_GRADING_VERSION", str(exc)) from exc
    db.commit()
    db.refresh(version)
    return version_view(db, version, detail=True)


@router.post("/questions/{question_id}/grading-versions/convert-preview")
def exam_convert_preview(
    question_id: int,
    payload: ConversionPreviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    question = _require_exam_question(db, question_id)
    _require_exam_editor(db, current_user, question)
    source = payload.source if payload.source is not None else (question.hidden_tests or "")
    entry = _exam_entry_function(question, payload.entry_function)
    try:
        report = grading_conversion.analyze_pytest_source(source, entry_function=entry)
    except ValueError as exc:
        raise api_error(422, "CONVERSION_PREVIEW_FAILED", str(exc)) from exc
    return {"summary": grading_conversion.conversion_summary(report),
            "cases": report["cases"], "functions": report["functions"],
            "entry_function": report["entry_function"]}


@router.post("/questions/{question_id}/grading-versions/convert-apply",
             status_code=status.HTTP_201_CREATED)
def exam_convert_apply(
    question_id: int,
    payload: ConversionApplyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    question = _require_exam_question(db, question_id)
    _require_exam_editor(db, current_user, question)
    source = payload.source if payload.source is not None else (question.hidden_tests or "")
    entry = _exam_entry_function(question, payload.entry_function)
    try:
        report = grading_conversion.analyze_pytest_source(source, entry_function=entry)
        report["notes"].append(
            "新版本按 isolated_case_v2 执行：每个用例重新启动容器；依赖跨用例共享状态的旧测试不会保留该行为。"
        )
        _assert_conversion_complete(report, allow_partial=payload.allow_partial)
        converted = grading_conversion.build_conversion_payload(
            report, entry_function=entry, group_id=payload.group_id,
            group_name=payload.group_name, max_score=payload.max_score)
        if not payload.confirm_isolated_case_v2:
            raise versions.GradingVersionError(
                "创建草稿前必须确认逐例独立执行；共享状态旧用例需人工处理或明确作为新规则"
            )
        version_payload = {
            "schema_version": 2, "entry_function": entry,
            "execution_model": "isolated_case_v2",
            "binary_cases": [
                {key: case[key] for key in ("id", "input", "expected", "compare")}
                for group in converted["groups"] for case in group["cases"]
            ],
            "groups": [],
        }
        context = dict(payload.context_snapshot or {"prompt": question.prompt})
        context["isolated_case_v2_confirmed"] = True
        if report["unsupported_count"]:
            context.update({"partial_conversion": True,
                            "unsupported_count": report["unsupported_count"],
                            "unsupported_reasons": grading_conversion.conversion_summary(
                                report)["reasons"]})
        version = versions.create_draft(
            db, question_kind="exam", question_id=question_id,
            payload=version_payload, entry_function=entry,
            context_snapshot=context,
            rubric_id=payload.rubric_id, source_kind="converted_from_pytest",
            grading_mode_snapshot="legacy",
            actor_id=current_user.id)
    except (ValueError, versions.GradingVersionError) as exc:
        raise api_error(422, "CONVERSION_APPLY_FAILED", str(exc)) from exc
    db.commit()
    db.refresh(version)
    return {"version": version_view(db, version, detail=True),
            "summary": grading_conversion.conversion_summary(report)}


@router.post("/questions/{question_id}/grading-versions/ai-draft",
             status_code=status.HTTP_201_CREATED)
def create_exam_ai_draft(
    question_id: int,
    payload: AiDraftRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    question = _require_exam_question(db, question_id)
    _require_exam_editor(db, current_user, question)
    context = dict(payload.context_snapshot or {})
    context.setdefault("prompt", question.prompt)
    if payload.model_name:
        context["draft_model"] = payload.model_name
    try:
        version = versions.create_draft(
            db, question_kind="exam", question_id=question_id,
            payload=payload.payload, entry_function=payload.entry_function,
            compare_rules=payload.compare_rules, context_snapshot=context,
            rubric_id=payload.rubric_id, source_kind="ai_draft", actor_id=current_user.id)
    except versions.GradingVersionError as exc:
        raise api_error(422, "INVALID_GRADING_VERSION", str(exc)) from exc
    db.commit()
    db.refresh(version)
    return version_view(db, version, detail=True)


@router.get("/grading-versions/{version_id}")
def get_exam_grading_version(
    version_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    version = _require_exam_version(db, version_id)
    _ensure_editable(db, current_user, version)
    return version_view(db, version, detail=True)


@router.post("/grading-versions/{version_id}/confirm")
def confirm_exam_grading_version(
    version_id: int,
    payload: GradingVersionConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    version = _require_exam_version(db, version_id)
    _ensure_editable(db, current_user, version)
    try:
        versions.confirm_version(db, version, actor_id=current_user.id,
                                 expected_spec_digest=payload.expected_spec_digest,
                                 confirm_isolated_case_v2=payload.confirm_isolated_case_v2)
    except versions.GradingVersionError as exc:
        raise api_error(409, "GRADING_VERSION_CONFLICT", str(exc)) from exc
    db.commit()
    db.refresh(version)
    return version_view(db, version, detail=True)


@router.post("/grading-versions/{version_id}/activate")
def activate_exam_grading_version(
    version_id: int,
    acknowledge_in_flight: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    version = _require_exam_version(db, version_id)
    question = _ensure_editable(db, current_user, version)
    if not versions.rubric_binding_ok(db, version):
        raise api_error(422, "RUBRIC_REQUIRED", "该评分模式必须绑定有效的 Rubric 后才能启用")
    try:
        versions.assert_enablement_allowed(
            db, question_kind="exam", question_id=question.id,
            acknowledge_in_flight=acknowledge_in_flight)
        versions.activate_version(db, version, actor_id=current_user.id)
    except versions.GradingVersionError as exc:
        code = ("IN_FLIGHT_GRADING" if "正在排队或判题中" in str(exc)
                else "GRADING_VERSION_CONFLICT")
        raise api_error(409, code, str(exc)) from exc
    versions.set_question_current_version(db, question_kind="exam",
                                         question_id=question.id, version_id=version.id)
    db.commit()
    db.refresh(version)
    return version_view(db, version, detail=True)


@router.post("/grading-versions/{version_id}/retire")
def retire_exam_grading_version(
    version_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    version = _require_exam_version(db, version_id)
    question = _ensure_editable(db, current_user, version)
    try:
        versions.retire_version(db, version)
    except versions.GradingVersionError as exc:
        raise api_error(409, "GRADING_VERSION_CONFLICT", str(exc)) from exc
    versions.set_question_current_version(db, question_kind="exam",
                                         question_id=question.id, version_id=None)
    db.commit()
    db.refresh(version)
    return version_view(db, version, detail=True)
