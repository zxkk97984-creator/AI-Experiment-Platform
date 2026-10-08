"""AI 评分 API——题目配置、Rubric 管理、教师复核、重评"""
from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, or_
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.config import Settings, get_settings
from app.dependencies import PaginationParams, get_db, get_redis_client, pagination, require_roles
from app.errors import api_error
from app.models import (
    Assignment, CodeGrade, Course, Exam, ExamAnswer, ExamQuestion,
    ExamSubmission, GradeOverride, JudgeQuestion, QuestionRubric, Submission, User,
)
from app.schemas import AIServiceStatus, PaginatedResponse
from app.schemas.ai_grading import (
    AIQuestionConfigUpdate, GradeOverrideCreate, RubricDocument,
    TestGroupsGenerateRequest, TestGroupsGenerateResponse,
)
from app.services.ai_client import AIServiceError, DeepSeekClient
from app.services.op_metrics import ai_metrics_sink
from app.services.ai_prompts import build_test_group_snapshot
from app.services.rubric_service import (
    RubricGenerationError, build_question_snapshot, generate_rubric,
    get_latest_locked_rubric, lock_rubric, update_draft_rubric,
)
from app.services.test_group_generator import (
    PreflightUnavailableError, TestGroupValidationError, generate_test_groups,
)

logger = logging.getLogger("dai.ai_grading")
from app.services.ai_grading_queue import enqueue_ai_grade
from app.services.score_merger import merge_scores

router = APIRouter(prefix="/ai-grading", tags=["AI 评分"])


def _teacher_or_admin(user: User):
    if user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "仅教师和管理员可访问")


@router.get("/status", response_model=AIServiceStatus)
def ai_service_status(
    settings: Settings = Depends(get_settings),
    _: User = Depends(require_roles("admin", "teacher")),
):
    """TASK-020：AI 服务状态——前端据此显示禁用提示（enabled=开关，ready=开关+Key 齐备）。"""
    return AIServiceStatus(enabled=settings.ai_enabled, ready=settings.ai_ready)


def _check_generation_rate_limit(redis_client, user_id: int, scope: str) -> None:
    """AI 高成本生成的用户级限流：同一 scope 每 60 秒最多 5 次。

    TASK-028/F-23：Redis 故障时 fail-closed 返回 503——限流失效不得放行
    高成本生成（生成路径每用户每次调用都有真实成本）。
    """
    limit_key = f"ai:gen:{scope}:{user_id}"
    try:
        count = redis_client.incr(limit_key)
        if count == 1:
            redis_client.expire(limit_key, 60)
    except Exception as exc:
        logger.error(
            "ai_rate_limit_unavailable",
            extra={"scope": scope, "user_id": user_id},
        )
        raise api_error(
            503, "AI_RATE_LIMIT_UNAVAILABLE", "限流服务不可用，请稍后再试"
        ) from exc
    if count > 5:
        raise api_error(429, "AI_RATE_LIMITED", "生成过于频繁，请稍后再试")


def _ensure_assignment_scoring_editable(db: Session, kind: str, question_id: int) -> None:
    """作业类题目的 AI 配置/Rubric 属于评分事实：发布后或存在提交后禁止修改。

    考试题由考试自身生命周期管理，不受此守卫限制。
    """
    if kind != "assignment":
        return
    question = db.get(JudgeQuestion, question_id)
    if question is None:
        raise api_error(404, "NOT_FOUND", "题目不存在")
    assignment = db.get(Assignment, question.assignment_id)
    if assignment is None:
        raise api_error(404, "NOT_FOUND", "作业不存在")
    from app.api.assignments import ensure_scoring_editable

    ensure_scoring_editable(db, assignment)


def _ensure_course_teacher(db: Session, course_id: int, user: User):
    """验证用户是该课程的教师或 admin"""
    if user.role == "admin":
        return
    course = db.get(Course, course_id)
    if not course:
        raise api_error(404, "NOT_FOUND", "课程不存在")
    task = db.info.get("teaching_question_task")
    if task is not None and task.course_id == course_id and task.offering_id:
        from app.services.offering_service import task_content_manager
        task_content_manager(db, task, user)
        return
    if course.teacher_id != user.id:
        raise api_error(403, "FORBIDDEN", "仅课程负责人可修改共享评分配置")


def _get_course_id_for_question(db: Session, kind: str, question_id: int) -> int:
    if kind == "assignment":
        q = db.get(JudgeQuestion, question_id)
        if q is None:
            raise api_error(404, "NOT_FOUND", "题目不存在")
        a = db.get(Assignment, q.assignment_id)
        if a is None:
            raise api_error(404, "NOT_FOUND", "作业不存在")
        db.info["teaching_question_task"] = a
        return a.course_id
    elif kind == "exam":
        q = db.get(ExamQuestion, question_id)
        if q is None:
            raise api_error(404, "NOT_FOUND", "题目不存在")
        e = db.get(Exam, q.exam_id)
        if e is None:
            raise api_error(404, "NOT_FOUND", "考试不存在")
        db.info["teaching_question_task"] = e
        return e.course_id
    raise api_error(400, "INVALID_KIND", "kind 必须为 assignment 或 exam")


def _teacher_course_ids(db: Session, user: User) -> list[int]:
    """返回教师所教的所有课程 ID（admin 返回空列表表示不限制）"""
    if user.role == "admin":
        return []
    rows = db.scalars(
        select(Course.id).where(teacher_course_condition(user.id))
    ).all()
    return list(rows)


def _check_grade_permission(db: Session, cg: CodeGrade, user: User):
    """验证教师有权访问该评分记录——fail-closed（关联缺失时拒绝）"""
    course_ids = _teacher_course_ids(db, user)
    if not course_ids:
        raise api_error(403, "FORBIDDEN", "无权操作")
    if cg.submission_id:
        sub = db.get(Submission, cg.submission_id)
        if not sub:
            raise api_error(403, "FORBIDDEN", "提交记录不存在")
        q = db.get(JudgeQuestion, sub.question_id)
        if not q:
            raise api_error(403, "FORBIDDEN", "题目不存在")
        a = db.get(Assignment, q.assignment_id)
        if not a or a.course_id not in course_ids:
            raise api_error(403, "FORBIDDEN", "无权操作")
    elif cg.exam_answer_id:
        ans = db.get(ExamAnswer, cg.exam_answer_id)
        if not ans:
            raise api_error(403, "FORBIDDEN", "答案记录不存在")
        q = db.get(ExamQuestion, ans.question_id)
        if not q:
            raise api_error(403, "FORBIDDEN", "题目不存在")
        e = db.get(Exam, q.exam_id)
        if not e or e.course_id not in course_ids:
            raise api_error(403, "FORBIDDEN", "无权操作")
    else:
        raise api_error(403, "FORBIDDEN", "无效的评分记录")


# ── 题目配置 ──

@router.get("/questions/{kind}/{question_id}/config")
def get_question_ai_config(
    kind: str, question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _teacher_or_admin(current_user)
    course_id = _get_course_id_for_question(db, kind, question_id)
    _ensure_course_teacher(db, course_id, current_user)

    if kind == "assignment":
        q = db.get(JudgeQuestion, question_id)
    else:
        q = db.get(ExamQuestion, question_id)
    if q is None:
        raise api_error(404, "NOT_FOUND", "题目不存在")

    return {
        "grading_mode": q.grading_mode,
        "teacher_constraints": q.teacher_constraints,
        "reference_solution": q.reference_solution,
        "test_groups": q.test_groups,
        "score_cap_rules": q.score_cap_rules,
        "hidden_tests": q.hidden_tests if hasattr(q, "hidden_tests") else None,
    }


@router.put("/questions/{kind}/{question_id}/config")
def update_question_ai_config(
    kind: str, question_id: int,
    data: AIQuestionConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _teacher_or_admin(current_user)
    course_id = _get_course_id_for_question(db, kind, question_id)
    _ensure_course_teacher(db, course_id, current_user)
    _ensure_assignment_scoring_editable(db, kind, question_id)

    if kind == "assignment":
        q = db.get(JudgeQuestion, question_id)
    else:
        q = db.get(ExamQuestion, question_id)
        if q and q.question_type != "code" and data.grading_mode != "legacy":
            raise api_error(400, "CHOICE_LEGACY_ONLY", "选择题只支持 legacy 模式")

    if q is None:
        raise api_error(404, "NOT_FOUND", "题目不存在")

    q.grading_mode = data.grading_mode
    q.teacher_constraints = data.teacher_constraints
    q.reference_solution = data.reference_solution
    q.test_groups = [g.model_dump() for g in data.test_groups]
    q.score_cap_rules = [r.model_dump() for r in data.score_cap_rules]
    db.commit()
    return {"ok": True, "grading_mode": q.grading_mode}


# ── Rubric 管理 ──

@router.get("/questions/{kind}/{question_id}/rubrics")
def list_rubrics(
    kind: str, question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _teacher_or_admin(current_user)
    course_id = _get_course_id_for_question(db, kind, question_id)
    _ensure_course_teacher(db, course_id, current_user)

    col = QuestionRubric.judge_question_id if kind == "assignment" else QuestionRubric.exam_question_id
    rubrics = db.scalars(
        select(QuestionRubric).where(col == question_id).order_by(QuestionRubric.version.desc())
    ).all()
    return {"items": [{
        "id": r.id, "version": r.version, "status": r.status,
        "source_hash": r.source_hash, "model_name": r.model_name,
        "locked_at": r.locked_at.isoformat() if r.locked_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rubrics]}


@router.post("/questions/{kind}/{question_id}/rubrics/generate")
def generate_rubric_endpoint(
    kind: str, question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    redis_client=Depends(get_redis_client),
):
    _teacher_or_admin(current_user)
    course_id = _get_course_id_for_question(db, kind, question_id)
    _ensure_course_teacher(db, course_id, current_user)
    _ensure_assignment_scoring_editable(db, kind, question_id)

    if not settings.ai_ready:
        raise api_error(503, "AI_NOT_READY", "AI 服务未配置 API Key")

    # TASK-028：Rubric 生成同样纳入限流（每用户每题目 60 秒 5 次，Redis 故障 503）
    _check_generation_rate_limit(redis_client, current_user.id, f"rubric:{kind}:{question_id}")

    if kind == "assignment":
        q = db.get(JudgeQuestion, question_id)
    else:
        q = db.get(ExamQuestion, question_id)
    if q is None:
        raise api_error(404, "NOT_FOUND", "题目不存在")

    title = getattr(q, 'title', None) or getattr(q, 'prompt', '')
    desc = getattr(q, 'description', None) if hasattr(q, 'description') else getattr(q, 'prompt', None)
    fn = getattr(q, 'function_name', None) if hasattr(q, 'function_name') else getattr(q, 'prompt', None)

    snapshot = build_question_snapshot(
        title=title, description=desc, function_name=fn,
        teacher_constraints=q.teacher_constraints,
        test_groups=q.test_groups,
        reference_solution=q.reference_solution,
        is_exam=(kind == "exam"),
    )

    client = DeepSeekClient(settings, metrics_sink=ai_metrics_sink())
    try:
        rubric = generate_rubric(db, client, kind=kind, question_id=question_id, snapshot=snapshot)
    except RubricGenerationError as exc:
        # AI 输出结构不合规：可读错误 + 可重试，而非 500
        raise api_error(502, "AI_GENERATION_INVALID", str(exc), fields={"retryable": True})
    except AIServiceError as exc:
        if exc.code == "timeout":
            raise api_error(504, "AI_GENERATION_TIMEOUT", "AI 生成超时，请稍后重试")
        if exc.code == "http_429":
            raise api_error(429, "AI_RATE_LIMITED", "AI 服务限流，请稍后重试")
        if exc.retryable:
            raise api_error(502, "AI_GENERATION_INVALID", f"AI 服务暂时不可用: {exc}", fields={"retryable": True})
        raise api_error(502, "AI_GENERATION_INVALID", f"AI 生成失败: {exc}", fields={"retryable": False})
    db.commit()
    return {"id": rubric.id, "version": rubric.version, "status": rubric.status, "rubric_json": rubric.rubric_json}


@router.post(
    "/questions/{kind}/{question_id}/test-groups/generate",
    response_model=TestGroupsGenerateResponse,
)
def generate_test_groups_endpoint(
    kind: str, question_id: int,
    data: TestGroupsGenerateRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    redis_client=Depends(get_redis_client),
):
    """AI 生成 F/R 测试组——只生成、不保存。

    教师草稿字段（teacher_constraints / reference_solution）未传时读取
    数据库配置；hidden_tests、题干等权威数据一律取自服务端，不接受
    客户端伪造。响应与错误日志均不含 hidden_tests 原文。
    """
    _teacher_or_admin(current_user)
    course_id = _get_course_id_for_question(db, kind, question_id)
    _ensure_course_teacher(db, course_id, current_user)

    if not settings.ai_ready:
        raise api_error(503, "AI_NOT_READY", "AI 服务未配置 API Key")

    # TASK-028/F-23：每用户每题目 60 秒内最多 5 次；Redis 故障时 fail-closed 返回
    # 503（高成本生成不得在限流失效时放行）
    _check_generation_rate_limit(redis_client, current_user.id, f"testgroups:{kind}:{question_id}")

    if kind == "assignment":
        q = db.get(JudgeQuestion, question_id)
    else:
        q = db.get(ExamQuestion, question_id)
    if q is None:
        raise api_error(404, "NOT_FOUND", "题目不存在")

    teacher_constraints = (
        data.teacher_constraints
        if data is not None and data.teacher_constraints is not None
        else q.teacher_constraints
    )
    reference_solution = (
        data.reference_solution
        if data is not None and data.reference_solution is not None
        else q.reference_solution
    )

    snapshot = build_test_group_snapshot(
        title=getattr(q, "title", None) or getattr(q, "prompt", ""),
        description=(
            getattr(q, "description", None)
            if hasattr(q, "description") else getattr(q, "prompt", None)
        ),
        function_name=getattr(q, "function_name", None),
        signature=getattr(q, "signature", None),
        starter_code=getattr(q, "starter_code", None),
        hidden_tests=getattr(q, "hidden_tests", None),
        reference_solution=reference_solution,
        teacher_constraints=teacher_constraints,
    )

    client = DeepSeekClient(settings, metrics_sink=ai_metrics_sink())
    try:
        with tempfile.TemporaryDirectory(prefix="dai-testgen-") as tmp:
            result = generate_test_groups(
                client, snapshot, settings,
                workdir=tmp,
                host_workdir=settings.judge_host_work_dir or None,
            )
    except TestGroupValidationError as exc:
        # 生成的 issues 只描述生成结果的缺陷，不含 hidden_tests 原文
        raise api_error(
            502, "AI_GENERATION_INVALID",
            "AI 生成测试组不合规，请重新生成或手动修改",
            fields={"issues": exc.issues, "retryable": True},
        )
    except PreflightUnavailableError as exc:
        raise api_error(503, "JUDGE_UNAVAILABLE", str(exc))
    except AIServiceError as exc:
        if exc.code == "timeout":
            raise api_error(504, "AI_GENERATION_TIMEOUT", "AI 生成超时，请稍后重试")
        if exc.code == "http_429":
            raise api_error(429, "AI_RATE_LIMITED", "AI 服务限流，请稍后重试")
        if exc.retryable:
            raise api_error(502, "AI_GENERATION_INVALID", f"AI 服务暂时不可用: {exc}", fields={"retryable": True})
        raise api_error(502, "AI_GENERATION_INVALID", f"AI 生成失败: {exc}", fields={"retryable": False})

    logger.info(
        "test_groups_generated",
        extra={
            "generation_id": result.generation_id,
            "kind": kind, "question_id": question_id, "user_id": current_user.id,
            "group_count": result.validation.group_count,
            "f_group_count": result.validation.f_group_count,
            "r_group_count": result.validation.r_group_count,
            "warnings": result.warnings,
        },
    )
    return result


@router.patch("/rubrics/{rubric_id}")
def patch_rubric(
    rubric_id: int, document: RubricDocument,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _teacher_or_admin(current_user)
    rubric = db.get(QuestionRubric, rubric_id)
    if rubric is None:
        raise api_error(404, "NOT_FOUND", "Rubric 不存在")

    qid = rubric.judge_question_id or rubric.exam_question_id
    k = "assignment" if rubric.judge_question_id else "exam"
    course_id = _get_course_id_for_question(db, k, qid)
    _ensure_course_teacher(db, course_id, current_user)
    _ensure_assignment_scoring_editable(db, k, qid)

    try:
        updated = update_draft_rubric(db, rubric_id, document)
    except ValueError as exc:
        raise api_error(400, "INVALID_STATE", str(exc))
    return {"id": updated.id, "status": updated.status}


@router.post("/rubrics/{rubric_id}/lock")
def lock_rubric_endpoint(
    rubric_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _teacher_or_admin(current_user)
    rubric = db.get(QuestionRubric, rubric_id)
    if rubric is None:
        raise api_error(404, "NOT_FOUND", "Rubric 不存在")

    qid = rubric.judge_question_id or rubric.exam_question_id
    k = "assignment" if rubric.judge_question_id else "exam"
    course_id = _get_course_id_for_question(db, k, qid)
    _ensure_course_teacher(db, course_id, current_user)
    _ensure_assignment_scoring_editable(db, k, qid)

    try:
        locked = lock_rubric(db, rubric_id)
    except ValueError as exc:
        raise api_error(400, "INVALID_STATE", str(exc))
    return {"id": locked.id, "status": locked.status, "locked_at": locked.locked_at.isoformat()}


# ── 评分列表与详情 ──

def _build_grade_base_query(db: Session, user: User, kind: str | None,
                             question_id: int | None, student_id: int | None, status: str | None,
                             student_name: str | None = None):
    """构建带权限筛选的 CodeGrade 查询。按 kind 构建单一路径避免重复 JOIN。"""
    if kind not in (None, "assignment", "exam"):
        raise api_error(400, "INVALID_KIND", "kind 必须为 assignment 或 exam")

    course_ids = _teacher_course_ids(db, user)
    is_admin = user.role == "admin"

    if kind == "assignment":
        # 单一路径：CodeGrade → Submission → JudgeQuestion → Assignment
        query = select(CodeGrade).join(Submission, CodeGrade.submission_id == Submission.id).join(
            JudgeQuestion, Submission.question_id == JudgeQuestion.id).join(
            Assignment, JudgeQuestion.assignment_id == Assignment.id)
        count_q = select(func.count()).select_from(CodeGrade).join(
            Submission, CodeGrade.submission_id == Submission.id).join(
            JudgeQuestion, Submission.question_id == JudgeQuestion.id).join(
            Assignment, JudgeQuestion.assignment_id == Assignment.id)
        if not is_admin:
            query = query.where(Assignment.course_id.in_(course_ids))
            count_q = count_q.where(Assignment.course_id.in_(course_ids))
    elif kind == "exam":
        # 单一路径：CodeGrade → ExamAnswer → ExamQuestion → Exam
        query = select(CodeGrade).join(ExamAnswer, CodeGrade.exam_answer_id == ExamAnswer.id).join(
            ExamQuestion, ExamAnswer.question_id == ExamQuestion.id).join(
            Exam, ExamQuestion.exam_id == Exam.id)
        count_q = select(func.count()).select_from(CodeGrade).join(
            ExamAnswer, CodeGrade.exam_answer_id == ExamAnswer.id).join(
            ExamQuestion, ExamAnswer.question_id == ExamQuestion.id).join(
            Exam, ExamQuestion.exam_id == Exam.id)
        if not is_admin:
            query = query.where(Exam.course_id.in_(course_ids))
            count_q = count_q.where(Exam.course_id.in_(course_ids))
    else:
        # 无 kind 筛选：一次性 JOIN 两条路径，后续组合筛选不得重复 JOIN。
        query = select(CodeGrade).distinct().outerjoin(
            Submission, CodeGrade.submission_id == Submission.id).outerjoin(
            JudgeQuestion, Submission.question_id == JudgeQuestion.id).outerjoin(
            Assignment, JudgeQuestion.assignment_id == Assignment.id).outerjoin(
            ExamAnswer, CodeGrade.exam_answer_id == ExamAnswer.id).outerjoin(
            ExamQuestion, ExamAnswer.question_id == ExamQuestion.id).outerjoin(
            Exam, ExamQuestion.exam_id == Exam.id)
        count_q = select(func.count(func.distinct(CodeGrade.id))).select_from(CodeGrade).outerjoin(
            Submission, CodeGrade.submission_id == Submission.id).outerjoin(
            JudgeQuestion, Submission.question_id == JudgeQuestion.id).outerjoin(
            Assignment, JudgeQuestion.assignment_id == Assignment.id).outerjoin(
            ExamAnswer, CodeGrade.exam_answer_id == ExamAnswer.id).outerjoin(
            ExamQuestion, ExamAnswer.question_id == ExamQuestion.id).outerjoin(
            Exam, ExamQuestion.exam_id == Exam.id)
        if not is_admin:
            permission_filter = or_(
                Assignment.course_id.in_(course_ids) if course_ids else False,
                Exam.course_id.in_(course_ids) if course_ids else False,
            )
            query = query.where(permission_filter)
            count_q = count_q.where(permission_filter)

    # question_id 筛选：需要时添加 JOIN（admin 可能尚未 JOIN 相关表）
    if question_id is not None:
        if kind == "exam":
            query = query.where(ExamAnswer.question_id == question_id)
            count_q = count_q.where(ExamAnswer.question_id == question_id)
        elif kind == "assignment":
            query = query.where(Submission.question_id == question_id)
            count_q = count_q.where(Submission.question_id == question_id)
        else:
            query = query.where(or_(
                Submission.question_id == question_id,
                ExamAnswer.question_id == question_id))
            count_q = count_q.where(or_(
                Submission.question_id == question_id,
                ExamAnswer.question_id == question_id))

    # student_id 筛选
    if student_id is not None:
        if kind == "exam":
            from app.models import ExamSubmission as _ES
            query = query.outerjoin(_ES, ExamAnswer.submission_id == _ES.id).where(_ES.student_id == student_id)
            count_q = count_q.outerjoin(_ES, ExamAnswer.submission_id == _ES.id).where(_ES.student_id == student_id)
        elif kind == "assignment":
            query = query.where(Submission.student_id == student_id)
            count_q = count_q.where(Submission.student_id == student_id)
        else:
            from app.models import ExamSubmission as _ES2
            query = query.outerjoin(
                _ES2, ExamAnswer.submission_id == _ES2.id).where(or_(
                Submission.student_id == student_id,
                _ES2.student_id == student_id))
            count_q = count_q.outerjoin(
                _ES2, ExamAnswer.submission_id == _ES2.id).where(or_(
                Submission.student_id == student_id,
                _ES2.student_id == student_id))

    # student_name 筛选：通过学生真实姓名（兼容用户名）模糊匹配，分别覆盖作业和考试路径。
    if student_name and student_name.strip():
        like = f"%{student_name.strip()}%"
        assignment_student_match = select(1).select_from(Submission).join(
            User, Submission.student_id == User.id
        ).where(
            Submission.id == CodeGrade.submission_id,
            or_(User.real_name.ilike(like), User.username.ilike(like)),
        ).exists()
        exam_student_match = select(1).select_from(ExamAnswer).join(
            ExamSubmission, ExamAnswer.submission_id == ExamSubmission.id
        ).join(User, ExamSubmission.student_id == User.id).where(
            ExamAnswer.id == CodeGrade.exam_answer_id,
            or_(User.real_name.ilike(like), User.username.ilike(like)),
        ).exists()
        name_filter = exam_student_match if kind == "exam" else (
            assignment_student_match if kind == "assignment" else or_(
                assignment_student_match, exam_student_match
            )
        )
        query = query.where(name_filter)
        count_q = count_q.where(name_filter)

    if status:
        query = query.where(CodeGrade.status == status)
        count_q = count_q.where(CodeGrade.status == status)

    return query, count_q


@router.get("/grades", response_model=PaginatedResponse)
def list_grades(
    kind: str | None = Query(None),
    question_id: int | None = Query(None),
    student_id: int | None = Query(None),
    student_name: str | None = Query(None),
    status: str | None = Query(None),
    pagination: PaginationParams = Depends(pagination),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page, page_size = pagination.page, pagination.page_size
    _teacher_or_admin(current_user)

    query, count_q = _build_grade_base_query(
        db, current_user, kind, question_id, student_id, status, student_name
    )

    total = db.scalar(count_q) or 0
    grades = db.scalars(
        query.order_by(CodeGrade.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()

    submission_ids = [cg.submission_id for cg in grades if cg.submission_id]
    exam_answer_ids = [cg.exam_answer_id for cg in grades if cg.exam_answer_id]
    students_by_submission = {}
    students_by_exam_answer = {}
    if submission_ids:
        students_by_submission = {
            submission_id: (student_id, student_name, student_username)
            for submission_id, student_id, student_name, student_username in db.execute(
                select(Submission.id, User.id, User.real_name, User.username)
                .join(User, Submission.student_id == User.id)
                .where(Submission.id.in_(submission_ids))
            ).all()
        }
    if exam_answer_ids:
        students_by_exam_answer = {
            exam_answer_id: (student_id, student_name, student_username)
            for exam_answer_id, student_id, student_name, student_username in db.execute(
                select(ExamAnswer.id, User.id, User.real_name, User.username)
                .join(ExamSubmission, ExamAnswer.submission_id == ExamSubmission.id)
                .join(User, ExamSubmission.student_id == User.id)
                .where(ExamAnswer.id.in_(exam_answer_ids))
            ).all()
        }

    items = []
    for cg in grades:
        student = students_by_submission.get(cg.submission_id) if cg.submission_id else None
        if student is None and cg.exam_answer_id:
            student = students_by_exam_answer.get(cg.exam_answer_id)
        items.append({
            "id": cg.id, "submission_id": cg.submission_id, "exam_answer_id": cg.exam_answer_id,
            "student_id": student[0] if student else None,
            "student_name": student[1] if student else None,
            "mode": cg.mode, "status": cg.status,
            "functional_score": cg.functional_score, "algorithm_score": cg.algorithm_score,
            "robustness_score": cg.robustness_score, "quality_score": cg.quality_score,
            "raw_total": cg.raw_total, "score_cap": cg.score_cap,
            "final_score_100": cg.final_score_100,
            "needs_teacher_review": cg.needs_teacher_review,
            "attempt_count": cg.attempt_count,
            "created_at": cg.created_at.isoformat() if cg.created_at else None,
        })
    return PaginatedResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/grades/{grade_id}")
def get_grade_detail(
    grade_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _teacher_or_admin(current_user)
    cg = db.get(CodeGrade, grade_id)
    if cg is None:
        raise api_error(404, "NOT_FOUND", "评分记录不存在")

    # 权限：fail-closed——关联缺失时拒绝访问
    if current_user.role != "admin":
        course_ids = _teacher_course_ids(db, current_user)
        if not course_ids:
            raise api_error(403, "FORBIDDEN", "无权访问")
        if cg.submission_id:
            sub = db.get(Submission, cg.submission_id)
            if not sub:
                raise api_error(403, "FORBIDDEN", "提交记录不存在")
            q = db.get(JudgeQuestion, sub.question_id)
            if not q:
                raise api_error(403, "FORBIDDEN", "题目不存在")
            a = db.get(Assignment, q.assignment_id)
            if not a or a.course_id not in course_ids:
                raise api_error(403, "FORBIDDEN", "无权访问")
        elif cg.exam_answer_id:
            ans = db.get(ExamAnswer, cg.exam_answer_id)
            if not ans:
                raise api_error(403, "FORBIDDEN", "答案记录不存在")
            q = db.get(ExamQuestion, ans.question_id)
            if not q:
                raise api_error(403, "FORBIDDEN", "题目不存在")
            e = db.get(Exam, q.exam_id)
            if not e or e.course_id not in course_ids:
                raise api_error(403, "FORBIDDEN", "无权访问")
        else:
            raise api_error(403, "FORBIDDEN", "无效的评分记录")

    overrides = db.scalars(
        select(GradeOverride).where(GradeOverride.code_grade_id == grade_id).order_by(GradeOverride.id.desc())
    ).all()

    # 获取学生代码与只读上下文（关联缺失时字段为 None，不抛错）
    student_code = None
    student_name = student_username = question_title = course_title = None
    submitted_at = None
    execution_time_ms = None
    if cg.submission_id:
        sub = db.get(Submission, cg.submission_id)
        if sub:
            student_code = sub.code
            submitted_at = sub.created_at
            execution_time_ms = sub.execution_time_ms
            student = db.get(User, sub.student_id)
            if student:
                student_name = student.real_name
                student_username = student.username
            q = db.get(JudgeQuestion, sub.question_id)
            if q:
                question_title = q.title
                a = db.get(Assignment, q.assignment_id)
                if a:
                    c = db.get(Course, a.course_id)
                    course_title = c.title if c else None
    elif cg.exam_answer_id:
        ans = db.get(ExamAnswer, cg.exam_answer_id)
        if ans:
            student_code = ans.code_answer
            submitted_at = ans.created_at
            es = db.get(ExamSubmission, ans.submission_id)
            if es:
                student = db.get(User, es.student_id)
                if student:
                    student_name = student.real_name
                    student_username = student.username
            eq = db.get(ExamQuestion, ans.question_id)
            if eq:
                question_title = eq.prompt
                ex = db.get(Exam, eq.exam_id)
                if ex:
                    c = db.get(Course, ex.course_id)
                    course_title = c.title if c else None

    return {
        "id": cg.id, "submission_id": cg.submission_id, "exam_answer_id": cg.exam_answer_id,
        "rubric_id": cg.rubric_id, "mode": cg.mode, "status": cg.status,
        "functional_score": cg.functional_score, "algorithm_score": cg.algorithm_score,
        "robustness_score": cg.robustness_score, "quality_score": cg.quality_score,
        "raw_total": cg.raw_total, "score_cap": cg.score_cap,
        "final_score_100": cg.final_score_100, "scaled_score": cg.scaled_score,
        "deterministic_details": cg.deterministic_details,
        "static_analysis": cg.static_analysis,
        "ai_result": cg.ai_result, "raw_response": cg.raw_response,
        "student_code": student_code,
        "needs_teacher_review": cg.needs_teacher_review,
        "review_reason": cg.review_reason,
        "attempt_count": cg.attempt_count, "last_error": cg.last_error,
        "student_name": student_name, "student_username": student_username,
        "question_title": question_title, "course_title": course_title,
        "submitted_at": submitted_at.isoformat() if submitted_at else None,
        "finished_at": cg.finished_at.isoformat() if cg.finished_at else None,
        "execution_time_ms": execution_time_ms,
        "overrides": [{
            "id": o.id, "original_snapshot": o.original_snapshot,
            "replacement_snapshot": o.replacement_snapshot,
            "reason": o.reason, "reviewer_id": o.reviewer_id,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        } for o in overrides],
    }


@router.post("/grades/{grade_id}/retry")
def retry_grade(
    grade_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    redis_client=Depends(get_redis_client),
):
    _teacher_or_admin(current_user)
    cg = db.get(CodeGrade, grade_id)
    if cg is None:
        raise api_error(404, "NOT_FOUND", "评分记录不存在")

    # 权限：教师只能操作自己课程的评分
    if current_user.role != "admin":
        _check_grade_permission(db, cg, current_user)

    # 条件重置：只重置失败终态（review_required/system_error），不碰 running/queued/completed
    if cg.status in ("running", "queued", "pending"):
        return {"ok": True, "grade_id": grade_id, "status": cg.status, "message": "评分进行中，不重复入队"}
    if cg.status == "completed":
        raise api_error(400, "ALREADY_COMPLETED", "评分已成功完成，无需重试。如需重新评分请使用重评功能。")

    cg.status = "pending"
    cg.last_error = None
    cg.attempt_count = 0
    db.commit()

    ok = enqueue_ai_grade(db, redis_client, grade_id)
    db.commit()
    return {"ok": ok, "grade_id": grade_id, "status": "queued" if ok else "pending"}


@router.post("/grades/{grade_id}/override")
def override_grade(
    grade_id: int,
    data: GradeOverrideCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _teacher_or_admin(current_user)
    cg = db.get(CodeGrade, grade_id)
    if cg is None:
        raise api_error(404, "NOT_FOUND", "评分记录不存在")

    # 权限
    if current_user.role != "admin":
        _check_grade_permission(db, cg, current_user)

    original = {
        "algorithm_score": cg.algorithm_score,
        "quality_score": cg.quality_score,
        "final_score_100": cg.final_score_100,
        "needs_teacher_review": cg.needs_teacher_review,
    }

    a_score = data.algorithm_score if data.algorithm_score is not None else cg.algorithm_score or 0
    q_score = data.quality_score if data.quality_score is not None else cg.quality_score or 0
    f_score = cg.functional_score or 0
    r_score = cg.robustness_score or 0

    if data.algorithm_score is not None:
        cg.algorithm_score = data.algorithm_score
    if data.quality_score is not None:
        cg.quality_score = data.quality_score

    if data.final_score_100 is not None:
        cg.final_score_100 = data.final_score_100
    else:
        # 教师未指定总分→合并 F+A+R+Q 重算
        merged = merge_scores(f=f_score, a=a_score, r=r_score, q=q_score, cap=cg.score_cap, exam_points=None)
        cg.raw_total = merged.raw_total
        cg.final_score_100 = merged.final_score_100

    # 考试 CodeGrade：始终按题目分值重算 scaled_score（无论教师改 A/Q 还是总分）
    if cg.exam_answer_id:
        ans = db.get(ExamAnswer, cg.exam_answer_id)
        if ans:
            eq = db.get(ExamQuestion, ans.question_id)
            if eq:
                cg.scaled_score = round((cg.final_score_100 or 0) / 100 * eq.points, 4)

    if cg.needs_teacher_review:
        cg.needs_teacher_review = False
        cg.review_reason = None

    replacement = {
        "algorithm_score": cg.algorithm_score,
        "quality_score": cg.quality_score,
        "final_score_100": cg.final_score_100,
        "needs_teacher_review": cg.needs_teacher_review,
    }

    override_record = GradeOverride(
        code_grade_id=grade_id, original_snapshot=original,
        replacement_snapshot=replacement, reason=data.reason, reviewer_id=current_user.id,
    )
    db.add(override_record)

    # 同步正式分 + 标记为已完成
    if cg.mode == "active":
        cg.status = "completed"
        cg.finished_at = datetime.now(timezone.utc)
        if cg.submission_id:
            sub = db.get(Submission, cg.submission_id)
            if sub:
                sub.score = cg.final_score_100
                sub.status = "graded"
        elif cg.exam_answer_id:
            ans = db.get(ExamAnswer, cg.exam_answer_id)
            if ans and cg.scaled_score is not None:
                ans.score = cg.scaled_score
                ans.grading_status = "completed"
                from app.services.exam_grading import finalize_if_ready, promote_review_required_if_complete
                db.flush()  # 确保 ans 更新对 finalize 可见
                finalize_if_ready(ans.submission_id, db)
                # 教师覆盖确认是显式受控动作：失败原因已消除则收口
                promote_review_required_if_complete(ans.submission_id, db)

    db.commit()
    return {"ok": True, "grade_id": grade_id, "original": original, "replacement": replacement}


@router.post("/questions/{kind}/{question_id}/regrade")
def regrade_question(
    kind: str,
    question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    redis_client=Depends(get_redis_client),
):
    _teacher_or_admin(current_user)
    course_id = _get_course_id_for_question(db, kind, question_id)
    _ensure_course_teacher(db, course_id, current_user)

    rubric = get_latest_locked_rubric(db, kind=kind, question_id=question_id)
    if rubric is None:
        raise api_error(400, "NO_RUBRIC", "该题目尚无锁定 Rubric")

    if kind == "assignment":
        q = db.get(JudgeQuestion, question_id)
    else:
        q = db.get(ExamQuestion, question_id)
    if q is None:
        raise api_error(404, "NOT_FOUND", "题目不存在")

    gmode = getattr(q, 'grading_mode', 'legacy') or 'legacy'
    count = 0
    queued = 0

    if kind == "assignment":
        subs = db.scalars(
            select(Submission).where(Submission.question_id == question_id)
        ).all()
        for sub in subs:
            existing = db.scalar(
                select(CodeGrade).where(CodeGrade.submission_id == sub.id)
            )
            if existing:
                # 跳过进行中的评分，防止并发覆盖
                if existing.status in ("running", "queued"):
                    continue
                existing.status = "pending"
                existing.rubric_id = rubric.id
                existing.last_error = None
                existing.attempt_count = 0
            else:
                f_score = (sub.result_details or {}).get("f_score", 0) if sub.result_details else 0
                r_score = (sub.result_details or {}).get("r_score", 0) if sub.result_details else 0
                det = (sub.result_details or {}).get("groups", []) if sub.result_details else []
                existing = CodeGrade(
                    submission_id=sub.id, rubric_id=rubric.id, mode=gmode, status="pending",
                    functional_score=f_score, robustness_score=r_score,
                    deterministic_details=det,
                )
                db.add(existing)
            count += 1
    else:
        answers = db.scalars(
            select(ExamAnswer).where(ExamAnswer.question_id == question_id)
        ).all()
        for ans in answers:
            existing = db.scalar(
                select(CodeGrade).where(CodeGrade.exam_answer_id == ans.id)
            )
            if existing:
                # 跳过进行中的评分，防止并发覆盖
                if existing.status in ("running", "queued"):
                    continue
                existing.status = "pending"
                existing.rubric_id = rubric.id
                existing.last_error = None
                existing.attempt_count = 0
            else:
                f_score = (ans.result_details or {}).get("f_score", 0) if ans.result_details else 0
                r_score = (ans.result_details or {}).get("r_score", 0) if ans.result_details else 0
                det = (ans.result_details or {}).get("groups", []) if ans.result_details else []
                existing = CodeGrade(
                    exam_answer_id=ans.id, rubric_id=rubric.id, mode=gmode, status="pending",
                    functional_score=f_score, robustness_score=r_score,
                    deterministic_details=det,
                )
                db.add(existing)
            count += 1

    db.commit()

    # 仅入队目标题目的 pendings
    if kind == "assignment":
        pends = db.scalars(
            select(CodeGrade).where(
                CodeGrade.submission_id.in_(
                    select(Submission.id).where(Submission.question_id == question_id)
                ),
                CodeGrade.status == "pending"
            )
        ).all()
    else:
        pends = db.scalars(
            select(CodeGrade).where(
                CodeGrade.exam_answer_id.in_(
                    select(ExamAnswer.id).where(ExamAnswer.question_id == question_id)
                ),
                CodeGrade.status == "pending"
            )
        ).all()

    for cg in pends:
        if enqueue_ai_grade(db, redis_client, cg.id):
            queued += 1
    db.commit()

    return {"ok": True, "total": count, "queued": queued}

from app.services.offering_service import teacher_course_condition
