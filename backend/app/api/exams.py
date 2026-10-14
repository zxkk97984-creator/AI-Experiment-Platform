from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import ast
import keyword
import math
from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.api.courses import can_access_course_content, ensure_course_manager, require_course
from app.config import Settings, get_settings
from app.dependencies import get_current_user, get_db, get_redis_client, require_roles, PaginationParams, pagination
from app.errors import api_error
from app.models import (
    CodeGrade, Course, CourseEnrollment, Exam, ExamAnswer, ExamAudienceClass,
    ExamAudienceStudent, ExamGrade, ExamQuestion, ExamSubmission,
    ManualScoreAudit, QuestionRubric, TeachingClassStudent, User,
)
from app.schemas import CourseStudentImportResult, CourseStudentImportRow, ExamAnswerBatchRequest, ExamAnswerScoreUpdate, ExamCreate, ExamGradeRead, ExamQuestionCreate, ExamQuestionRead, ExamQuestionTeacherRead, ExamQuestionUpdate, ExamRead, ExamRetryRequest, ExamSampleRunRequest, ExamSessionRead, ExamSubmitPayload, ExamSubmitRequest, ExamSubmissionRead, ExamTakeoverRequest, ExamTimeExtensionRequest, PaginatedResponse, SampleRunResponse
from app.schemas.gradebook import ExamUpdateWithGradebook
from app.services.exam_service import build_proctor_overview, build_student_exam_session, build_student_exam_summary, create_question, delete_question, exam_max_score, extend_exam_submission, force_submit_exam_submission, get_my_grade, get_question, hash_client_identity, list_questions, record_exam_events, release_exam_review, require_exam_editable, retry_exam_submission as retry_exam_submission_service, save_answer, start_exam as svc_start_exam, student_exam_status, submit_exam as svc_submit_exam, takeover_exam_session, update_question, validate_publish
from app.services.time_utils import as_utc, utc_now
from app.services.audience_service import (
    effective_student_ids, exam_visible_condition, import_audience_students,
    parse_student_csv, populate_audience_cache, require_effective_audience,
    save_audience, student_in_exam_audience,
)
from app.worker.judge_worker import _run_docker_pytest, _status_from_pytest

from app.services.offering_service import require_student_task_write, task_content_manager, teacher_task_condition

def ensure_exam_manager(exam, user, db=None):
    from sqlalchemy.orm import object_session
    task_content_manager(db or object_session(exam), exam, user)

router = APIRouter(prefix="/exams", tags=["exams"])


def _client_fingerprint(request: Request, settings: Settings) -> tuple[str | None, str | None]:
    """监考事件的客户端标识一律以 HMAC 摘要落库，绝不保存 IP/UA 明文。"""
    from app.api.auth import _client_ip
    ip = _client_ip(request, settings)
    user_agent = request.headers.get("User-Agent")
    return hash_client_identity(settings, ip), hash_client_identity(settings, user_agent)


def _require_write_generation(payload: dict) -> int:
    """写入代次是必填参数：缺失即拒绝，绝不放行"漏带字段"的旧设备。"""
    value = payload.get("expected_write_generation")
    if value is None:
        raise api_error(
            422, "WRITE_GENERATION_REQUIRED",
            "缺少 expected_write_generation，请刷新页面后重试",
            fields={"expected_write_generation": ["该字段必填"]},
        )
    try:
        generation = int(value)
    except (TypeError, ValueError):
        raise api_error(422, "WRITE_GENERATION_REQUIRED", "expected_write_generation 必须是整数")
    if generation < 1:
        raise api_error(422, "WRITE_GENERATION_REQUIRED", "expected_write_generation 必须大于 0")
    return generation


def verify_student_reauth(request: Request, actor: User, password: str, redis,
                          settings: Settings, db: Session) -> None:
    """换机接管的二次验密：复用管理员复核的 `reauth:{username}` 限流与计次/清零语义。

    先例 backend/app/api/account_management.py 的 verify_admin；二者共用同一限流键，
    因此换机尝试与管理员复核共享失败计次，不会成为绕过限流的旁路。
    """
    from app.api.auth import _client_ip
    from app.dependencies import get_current_payload
    from app.services import account_service as accounts
    from app.services.auth_service import (check_login_rate_limits, record_login_failure,
                                           reset_login_failures)
    from app.services.teaching_query_policy import lock_current_actor

    actor = lock_current_actor(db, actor)
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise api_error(401, "INVALID_TOKEN", "缺少有效访问令牌")

    def current_token():
        payload = get_current_payload(token, settings, redis)
        if payload["sub"] != str(actor.id) or payload.get("sv") != actor.session_version:
            raise api_error(401, "SESSION_REVOKED", "会话已失效，请重新登录")

    current_token()
    key = "reauth:" + actor.username
    ip = _client_ip(request, settings)
    check_login_rate_limits(redis, settings, key, ip)
    try:
        accounts.verify_current(actor, password)
    except Exception:
        record_login_failure(redis, settings, key, ip)
        raise
    # 密码校验期间会话可能被登出/过期：校验成功后再次确认令牌有效，绝不把凭据交给失效会话。
    current_token()
    reset_login_failures(redis, key)


def _nulls_last_order(column, *, descending: bool = False):
    """Return a portable NULLS LAST ordering for MySQL and SQLite.

    SQLAlchemy's ``nullslast()`` emits ``NULLS LAST``.  PostgreSQL accepts
    that syntax, but MySQL does not, so use a boolean CASE key first and then
    the requested direction for the actual value.
    """

    value_order = column.desc() if descending else column.asc()
    return (
        case((column.is_(None), 1), else_=0).asc(),
        value_order,
    )


def _grade_sort_expressions() -> dict[str, tuple]:
    """Build the grade list ordering map without dialect-specific SQL."""

    score_desc = (*_nulls_last_order(ExamSubmission.score, descending=True), User.id.asc())
    score_asc = (*_nulls_last_order(ExamSubmission.score), User.id.asc())
    submitted_time = (*_nulls_last_order(ExamSubmission.submitted_at, descending=True), User.id.asc())
    name = (*_nulls_last_order(User.real_name), User.id.asc())
    return {
        "score_desc": score_desc,
        "score-desc": score_desc,
        "score_asc": score_asc,
        "score-asc": score_asc,
        "time": submitted_time,
        "name": name,
    }


def require_exam(exam_id: int, db: Session) -> Exam:
    exam = db.get(Exam, exam_id)
    if not exam:
        raise api_error(404, "EXAM_NOT_FOUND", "考试不存在")
    return exam


def _student_exam_allowed(exam: Exam, student: User, db: Session) -> bool:
    """任务白名单学生可以不选课；其他学生仍需课程访问权限。"""
    if student_in_exam_audience(db, exam, student.id):
        return True
    return bool(exam.course and can_access_course_content(exam.course, student, db))


def _with_submission_aliases(session: dict) -> dict:
    """兼容旧客户端的顶层提交字段，并以规范化后的嵌套值为唯一来源。"""
    submission = session.get("submission") or {}
    session.update({
        "id": submission.get("id"),
        "status": submission.get("status"),
        "expires_at": submission.get("expires_at"),
        "score": submission.get("score"),
    })
    return session


def _exam_device_cookie_name(exam_id: int) -> str:
    return f"dai_exam_device_{exam_id}"


def _set_exam_device_cookie(response: Response, exam_id: int, token: str,
                            settings: Settings) -> None:
    response.set_cookie(
        key=_exam_device_cookie_name(exam_id), value=token,
        path=f"/api/v1/exams/{exam_id}", httponly=True,
        secure=settings.environment == "production", samesite="strict",
    )


def _delete_exam_device_cookie(response: Response, exam_id: int,
                               settings: Settings) -> None:
    response.delete_cookie(
        key=_exam_device_cookie_name(exam_id), path=f"/api/v1/exams/{exam_id}",
        httponly=True, secure=settings.environment == "production", samesite="strict",
    )


def _with_exam_device_access(payload: dict, *, exam_id: int, request: Request,
                             db: Session, token_override: str | None = None) -> dict:
    submission_payload = payload.get("submission")
    if not submission_payload or submission_payload.get("status") != "started":
        payload["can_write"] = False
        payload["takeover_required"] = False
        return payload
    submission = db.get(ExamSubmission, submission_payload.get("id"))
    token = token_override or request.cookies.get(_exam_device_cookie_name(exam_id))
    can_write = False
    if submission is not None and submission.active_device_token_hash and token:
        import hashlib
        import secrets
        actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
        can_write = secrets.compare_digest(submission.active_device_token_hash, actual)
    payload["can_write"] = can_write
    payload["takeover_required"] = not can_write
    return payload


def _submitted_ids(db: Session, exams: list[Exam], student_id: int) -> set[int]:
    """批量计算学生已提交的考试 id 集合，避免逐考试 N+1 查询。

    语义与 dashboard 待办判定一致：存在 submitted/grading/graded 任一状态的
    提交记录即视为已考。
    """
    if not exams:
        return set()
    exam_ids = [e.id for e in exams]
    return set(
        db.scalars(
            select(ExamSubmission.exam_id).where(
                ExamSubmission.exam_id.in_(exam_ids),
                ExamSubmission.student_id == student_id,
                ExamSubmission.status.in_(("submitted", "grading", "graded", "review_required")),
            )
        ).all()
    )


@router.get("", response_model=PaginatedResponse)
def list_exams(
    q: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    course_id: int | None = None,
    sort: str = "updated_desc",
    pagination: PaginationParams = Depends(pagination),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page, page_size = pagination.page, pagination.page_size
    server_now = utc_now()
    query = select(Exam).join(Course, Exam.course_id == Course.id)
    if current_user.role == "student":
        query = (
            query.where(Exam.status == "published")
            .where(Course.status == "published")
            .where(exam_visible_condition(current_user.id))
        )
    elif current_user.role == "teacher":
        query = query.where(or_(teacher_task_condition(Exam, current_user.id), and_(Exam.offering_id.is_(None), Course.teacher_id == current_user.id)))
    elif current_user.role != "admin":
        # Any unsupported role: empty
        query = query.where(Exam.id == -1)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(Exam.title.ilike(like), Course.title.ilike(like)))
    if course_id is not None:
        query = query.where(Exam.course_id == course_id)
    if status_filter:
        normalized_status = status_filter.strip().lower()
        if normalized_status == "draft":
            query = query.where(Exam.status == "draft")
        elif normalized_status == "ended":
            query = query.where(
                Exam.status == "published",
                Exam.end_at.is_not(None),
                Exam.end_at < server_now,
            )
        elif normalized_status == "published":
            query = query.where(
                Exam.status == "published",
                or_(Exam.end_at.is_(None), Exam.end_at >= server_now),
            )
        else:
            raise api_error(422, "INVALID_EXAM_STATUS_FILTER", "考试状态筛选值无效")
    sort_expressions = {
        "updated": (Exam.updated_at.desc(), Exam.id.desc()),
        "updated_desc": (Exam.updated_at.desc(), Exam.id.desc()),
        "updated_asc": (Exam.updated_at.asc(), Exam.id.asc()),
        "title": (Exam.title.asc(), Exam.id.asc()),
        "title_asc": (Exam.title.asc(), Exam.id.asc()),
        "title_desc": (Exam.title.desc(), Exam.id.desc()),
        "id": (Exam.id.asc(),),
    }
    if sort not in sort_expressions:
        raise api_error(422, "INVALID_EXAM_SORT", "考试排序值无效")
    # TASK-022：窗口函数一次取回总数，避免额外的 count 查询；
    # joinedload 预取 course，避免逐项惰性加载
    rows = db.execute(
        query.options(joinedload(Exam.course))
        .add_columns(func.count().over().label("_total"))
        .order_by(*sort_expressions[sort])
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    total = rows[0]._total if rows else 0
    exams = [row.Exam for row in rows]
    populate_audience_cache(db, task_type="exam", tasks=exams)
    student_submissions = {}
    if current_user.role == "student" and exams:
        rows = db.scalars(select(ExamSubmission).where(
            ExamSubmission.student_id == current_user.id,
            ExamSubmission.exam_id.in_([exam.id for exam in exams]),
        )).all()
        student_submissions = {submission.exam_id: submission for submission in rows}
    # TASK-022：一次批量聚合题数/参与人数/应参加人数/最高分，避免逐考试 N+1。
    # 目标：列表 SQL 数不随 N 线性增长（每页 ≤5 次）。
    # 学生视图只需最高分（其余统计仅教师/管理员列表展示），按角色裁剪聚合。
    exam_ids = [exam.id for exam in exams]
    agg_maps = {}
    if exam_ids:
        question_rows = db.execute(
            select(
                ExamQuestion.exam_id,
                func.count(),
                func.coalesce(func.sum(ExamQuestion.points), 0.0),
            )
            .where(ExamQuestion.exam_id.in_(exam_ids))
            .group_by(ExamQuestion.exam_id)
        ).all()
        agg_maps["question"] = {row[0]: row[1] for row in question_rows}
        agg_maps["max_score"] = {row[0]: float(row[2]) for row in question_rows}
    if exam_ids and current_user.role != "student":
        participant_rows = db.execute(
            select(ExamSubmission.exam_id, func.count())
            .where(
                ExamSubmission.exam_id.in_(exam_ids),
                ExamSubmission.status.in_(("submitted", "grading", "graded", "review_required")),
            )
            .group_by(ExamSubmission.exam_id)
        ).all()
        agg_maps["participant"] = dict(participant_rows)
        course_ids = [exam.course_id for exam in exams]
        expected_rows = db.execute(
            select(CourseEnrollment.course_id, func.count())
            .where(
                CourseEnrollment.course_id.in_(course_ids),
                CourseEnrollment.status == "enrolled",
            )
            .group_by(CourseEnrollment.course_id)
        ).all()
        course_expected = dict(expected_rows)
        agg_maps["expected_by_exam"] = {}
        for exam in exams:
            has_custom_audience = (
                exam.audience_mode != "all_enrolled"
                or bool(exam.whitelist_student_ids)
                or bool(exam.excluded_student_ids)
            )
            if not has_custom_audience:
                agg_maps["expected_by_exam"][exam.id] = course_expected.get(exam.course_id, 0)
            else:
                agg_maps["expected_by_exam"][exam.id] = len(effective_student_ids(
                    db, task_type="exam", task_id=exam.id, course=exam.course,
                ))

    items = []
    for exam in exams:
        if current_user.role == "student":
            data = build_student_exam_summary(
                exam, student_submissions.get(exam.id), db, server_now,
                max_scores=agg_maps.get("max_score", {}),
            )
        else:
            data = ExamRead.model_validate(exam).model_dump()
            data.update({
                "course_title": exam.course.title if exam.course else "",
                "question_count": agg_maps.get("question", {}).get(exam.id, 0),
                "participant_count": agg_maps.get("participant", {}).get(exam.id, 0),
                "expected_count": agg_maps.get("expected_by_exam", {}).get(exam.id, 0),
                "created_at": exam.created_at,
                "updated_at": exam.updated_at,
                "max_score": agg_maps.get("max_score", {}).get(exam.id, 0.0),
                "server_now": server_now,
            })
        items.append(data)
    return PaginatedResponse(items=items, page=page, page_size=page_size, total=total)


@router.post("", response_model=ExamRead, status_code=status.HTTP_201_CREATED)
def create_exam(
    payload: ExamCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
):
    course = require_course(payload.course_id, db)
    if payload.offering_id:
        from app.services.offering_service import require_offering
        offering = require_offering(db, payload.offering_id, current_user, write=True)
        if offering.course_id != course.id:
            raise api_error(422, 'TASK_SCOPE_MISMATCH', '任务课程须与授课实例一致')
    elif current_user.role == 'teacher' and course.teacher_id != current_user.id:
        raise api_error(403, 'FORBIDDEN', '共享模板由课程负责人维护；班级任务请选择授课实例')
    # 创建考试时强制 draft，发布需通过 update 接口触发 validate_publish()
    exam_data = payload.model_dump()
    exam_data["status"] = "draft"
    audience_mode = exam_data.pop("audience_mode")
    audience_class_ids = exam_data.pop("audience_class_ids")
    whitelist_student_ids = exam_data.pop("whitelist_student_ids")
    excluded_student_ids = exam_data.pop("excluded_student_ids")
    if exam_data.get("show_answers_after_review"):
        exam_data["show_questions_after_review"] = True
    exam = Exam(**exam_data, created_by_id=current_user.id)
    db.add(exam)
    db.flush()
    save_audience(
        db, task_type="exam", task_id=exam.id, course=course,
        audience_mode=audience_mode, audience_class_ids=audience_class_ids,
        whitelist_student_ids=whitelist_student_ids, excluded_student_ids=excluded_student_ids,
        actor_id=current_user.id,
    )
    db.commit()
    db.refresh(exam)
    return exam


@router.get("/{exam_id}", response_model=ExamRead)
def get_exam(exam_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    exam = require_exam(exam_id, db)
    if not can_access_course_content(exam.course, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限查看该考试")
    if current_user.role == "student" and exam.status != "published":
        raise api_error(403, "EXAM_NOT_AVAILABLE", "考试未发布")
    if current_user.role == "student":
        if not _student_exam_allowed(exam, current_user, db):
            raise api_error(403, "FORBIDDEN", "没有权限查看该考试")
        if not student_in_exam_audience(db, exam, current_user.id):
            raise api_error(403, "NOT_IN_EXAM_AUDIENCE", "你不在本次考试范围内")
        submission = db.scalar(select(ExamSubmission).where(
            ExamSubmission.exam_id == exam.id,
            ExamSubmission.student_id == current_user.id,
        ))
        return build_student_exam_summary(exam, submission, db)
    data = ExamRead.model_validate(exam).model_dump()
    data["max_score"] = exam_max_score(exam.id, db)
    return data


@router.patch("/{exam_id}", response_model=ExamRead)
def update_exam(
    exam_id: int,
    payload: ExamUpdateWithGradebook,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    exam = require_exam(exam_id, db)
    ensure_exam_manager(exam, current_user)
    previous_status = exam.status
    changes = payload.model_dump(exclude_unset=True)
    changes.pop("gradebook_binding", None)
    audience_updates = {key: changes.pop(key) for key in (
        "audience_mode", "audience_class_ids", "whitelist_student_ids", "excluded_student_ids",
    ) if key in changes}
    has_attempts = db.scalar(select(ExamSubmission.id).where(ExamSubmission.exam_id == exam_id).limit(1)) is not None
    previous_mode = exam.audience_mode
    if has_attempts:
        from app.services.time_utils import as_utc
        duration_changed = "duration_minutes" in changes and changes["duration_minutes"] != exam.duration_minutes
        start_changed = "start_at" in changes and as_utc(changes["start_at"]) != as_utc(exam.start_at)
        if duration_changed or start_changed:
            raise api_error(409, "EXAM_ALREADY_STARTED", "已有学生开始考试，不能修改开始时间或考试时长")
        if changes.get("status") == "draft":
            raise api_error(409, "EXAM_ALREADY_STARTED", "已有学生开始考试，不能取消发布")
        if "end_at" in changes:
            if changes["end_at"] is None or (exam.end_at is not None and as_utc(changes["end_at"]) < as_utc(exam.end_at)):
                raise api_error(409, "EXAM_ALREADY_STARTED", "已有学生开始考试，最晚进入时间只能延后")
    if changes.get("show_answers_after_review"):
        changes["show_questions_after_review"] = True
    if previous_status == "published":
        # 发布后锁定时间模式 / 乱序 / 时长：已下发的会话不能被中途改写；
        # 需要延时走带审计与事件留痕的 submissions/{id}/extend 入口。
        locked = [
            key for key in ("timing_mode", "shuffle_questions", "shuffle_options", "duration_minutes")
            if key in changes and changes[key] != getattr(exam, key)
        ]
        if locked:
            raise api_error(
                422, "EXAM_SETTING_LOCKED",
                "考试已发布，时间模式、乱序开关与考试时长不能修改；如需延长作答时间请使用延时入口",
                fields={key: ["发布后锁定"] for key in locked},
            )
    for key, value in changes.items():
        setattr(exam, key, value)
    if audience_updates:
        # 已发布且有学生开始后：禁止切换基础模式，且不能移除已开始的学生
        if previous_status == "published" and has_attempts:
            if audience_updates.get("audience_mode", previous_mode) != previous_mode:
                raise api_error(409, "EXAM_AUDIENCE_MODE_LOCKED", "已有学生开始考试，不能切换考生基础范围")
            current_ids = set()
            from app.services.audience_service import effective_student_ids
            current_ids = effective_student_ids(db, task_type="exam", task_id=exam_id, course=exam.course)
        current = {
            "audience_mode": previous_mode,
            "audience_class_ids": exam.audience_class_ids,
            "whitelist_student_ids": exam.whitelist_student_ids,
            "excluded_student_ids": exam.excluded_student_ids,
        }
        current.update(audience_updates)
        save_audience(
            db, task_type="exam", task_id=exam_id, course=exam.course,
            actor_id=current_user.id, **current,
        )
        if previous_status == "published" and has_attempts:
            started_ids = set(db.scalars(
                select(ExamSubmission.student_id).where(ExamSubmission.exam_id == exam_id)
            ).all())
            new_ids = set()
            from app.services.audience_service import effective_student_ids as _effective_ids
            new_ids = _effective_ids(db, task_type="exam", task_id=exam_id, course=exam.course)
            removed_started = (current_ids - new_ids) & started_ids
            if removed_started:
                raise api_error(409, "EXAM_AUDIENCE_STUDENT_STARTED", "不能移除已经开始考试的学生")
    # 发布时强制校验
    if exam.status == "published":
        validate_publish(exam, db)
        require_effective_audience(db, task_type="exam", task_id=exam.id, course=exam.course)
        # AI 评分门禁只在草稿首次发布时执行；之后调整公开策略或延后
        # 最晚进入时间不应因为运行期配置变化而被无关门禁阻塞。
        code_questions = db.scalars(
            select(ExamQuestion).where(
                ExamQuestion.exam_id == exam_id,
                ExamQuestion.question_type == "code",
                ExamQuestion.grading_mode != "legacy",
            ).order_by(ExamQuestion.order_index)
        ).all()
        if code_questions and previous_status != "published":
            if not settings.ai_ready:
                raise api_error(503, "AI_NOT_READY", "发布含 AI 评分的考试需要配置 DAI_AI_API_KEY")
            missing = []
            for position, question in enumerate(code_questions, start=1):
                locked = db.scalar(select(QuestionRubric.id).where(
                    QuestionRubric.exam_question_id == question.id,
                    QuestionRubric.status == "locked",
                ).limit(1))
                if locked is None:
                    missing.append(str(position))
            if missing:
                raise api_error(422, "AI_RUBRIC_REQUIRED", "以下编程题尚未锁定 Rubric：第 " + "、".join(missing) + " 题")
        # 首次发布时按 exam_kind 自动归入成绩册分类；重复发布不会产生第二个分项。
        if previous_status != "published" and exam.offering_id:
            from app.services import gradebook_service as grades

            binding = payload.gradebook_binding
            if (binding is None or binding.enabled) and grades.bound_column(db, exam.offering_id, "exam", exam.id) is None:
                category_key = binding.category_key if binding and binding.category_key else grades.EXAM_CATEGORY_BY_KIND.get(exam.exam_kind)
                if not category_key:
                    raise api_error(422, "GRADEBOOK_CATEGORY_REQUIRED", "「其他」类型考试必须选择计入总评的分类，或取消计入课程总评")
                grades.bind_task_column(db, exam.offering_id, current_user.id, source_kind="exam", source_id=exam.id,
                                        title=exam.title, category_key=category_key)
    db.commit()
    db.refresh(exam)
    return exam


@router.post("/{exam_id}/audience/import", response_model=CourseStudentImportResult)
async def import_exam_audience(
    exam_id: int,
    kind: str = "include",
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CSV 导入考试范围白名单（include）或排除名单（exclude）。"""
    if kind not in ("include", "exclude"):
        raise api_error(422, "INVALID_AUDIENCE_KIND", "kind 必须为 include 或 exclude")
    exam = require_exam(exam_id, db)
    ensure_exam_manager(exam, current_user)
    from app.services.audience_service import read_bounded_audience_csv

    content = await read_bounded_audience_csv(file)
    rows, error = parse_student_csv(content)
    if error:
        raise api_error(422, "CSV_INVALID", error)
    result = import_audience_students(db, task_type="exam", task_id=exam.id, kind=kind, rows=rows)
    return CourseStudentImportResult(
        created=result["created"], updated=result["updated"], skipped=result["skipped"],
        errors=[CourseStudentImportRow(**row) for row in result["errors"]],
    )


@router.get("/{exam_id}/session", response_model=ExamSessionRead)
def get_exam_session(
    exam_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
):
    exam = require_exam(exam_id, db)
    if exam.status != "published" or not _student_exam_allowed(exam, current_user, db):
        raise api_error(403, "EXAM_NOT_AVAILABLE", "考试未发布或无权参加")
    if not student_in_exam_audience(db, exam, current_user.id):
        raise api_error(403, "NOT_IN_EXAM_AUDIENCE", "你不在本次考试范围内")
    payload = _with_submission_aliases(build_student_exam_session(exam, current_user, db))
    return _with_exam_device_access(payload, exam_id=exam_id, request=request, db=db)


@router.post(
    "/{exam_id}/questions/{question_id}/sample-run",
    response_model=SampleRunResponse,
)
def run_exam_public_cases(
    exam_id: int,
    question_id: int,
    payload: ExamSampleRunRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(require_roles("student")),
):
    """运行考试编程题的公开样例，不读取隐藏测试、不产生正式评分。"""
    exam = require_exam(exam_id, db)
    exam = require_student_task_write(db, exam, current_user)
    if exam.status != "published" or not _student_exam_allowed(exam, current_user, db):
        raise api_error(403, "EXAM_NOT_AVAILABLE", "考试未发布或无权参加")
    if not student_in_exam_audience(db, exam, current_user.id):
        raise api_error(403, "NOT_IN_EXAM_AUDIENCE", "你不在本次考试范围内")

    submission = db.scalar(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam_id,
        ExamSubmission.student_id == current_user.id,
    ).with_for_update().execution_options(populate_existing=True))
    now = utc_now()
    if not submission or submission.status != "started":
        raise api_error(403, "EXAM_NOT_STARTED", "考试未开始或已结束")
    from app.services.exam_service import require_exam_device_write
    require_exam_device_write(
        db, submission, expected_write_generation=payload.expected_write_generation,
        device_token=request.cookies.get(_exam_device_cookie_name(exam_id)),
        source="sample_run",
    )
    if submission.expires_at and as_utc(submission.expires_at) <= now:
        raise api_error(403, "EXAM_EXPIRED", "考试已过期")

    question = db.get(ExamQuestion, question_id)
    if not question or question.exam_id != exam_id:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")
    if question.question_type != "code":
        raise api_error(422, "QUESTION_TYPE_INVALID", "仅编程题支持运行自测")

    public_cases = question.public_cases or []
    if not public_cases:
        return SampleRunResponse(output="", status="no_public_cases", execution_time_ms=0)
    function_name = str((question.teacher_constraints or {}).get("require_function") or "")
    if not function_name:
        try:
            tree = ast.parse(question.starter_code or "")
            function_name = next(
                node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
        except (SyntaxError, StopIteration):
            function_name = ""
    if not function_name.isidentifier() or keyword.iskeyword(function_name):
        raise api_error(422, "PUBLIC_CASES_INVALID", "题目未配置有效的公开样例入口函数")

    test_lines = [f"from user_code import {function_name}", "", "def test_public_cases():"]
    for case in public_cases:
        args = case.get("args", [])
        expected = case.get("expected")
        args_text = ", ".join(repr(arg) for arg in args)
        test_lines.append(f"    assert {function_name}({args_text}) == {expected!r}")

    timeout_seconds = max(
        min(math.ceil((question.time_limit_ms or 10_000) / 1000), settings.judge_timeout_seconds),
        1,
    )
    memory_mb = question.memory_limit_mb or settings.judge_memory_limit_mb
    # Sample execution has no database completion write. Release teaching locks
    # before the potentially slow Docker call using these frozen scalar inputs.
    db.commit()
    with tempfile.TemporaryDirectory(prefix="dai-exam-sample-") as temp_dir:
        workdir = Path(temp_dir)
        (workdir / "user_code.py").write_text(payload.code, encoding="utf-8")
        (workdir / "test_public_cases.py").write_text("\n".join(test_lines) + "\n", encoding="utf-8")
        try:
            stdout, stderr, returncode, elapsed_ms = _run_docker_pytest(
                workdir,
                settings,
                timeout_seconds,
                memory_mb,
                test_filename="test_public_cases.py",
            )
        except FileNotFoundError:
            raise api_error(503, "JUDGE_UNAVAILABLE", "判题服务不可用（Docker 未就绪）")

    run_status, _score = _status_from_pytest(returncode, stdout, stderr)
    output = f"{stdout}\n{stderr}"[:20_000]
    return SampleRunResponse(output=output, status=run_status, execution_time_ms=elapsed_ms)


@router.post("/{exam_id}/start", response_model=ExamSessionRead, status_code=status.HTTP_201_CREATED)
def start_exam(
    exam_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(require_roles("student")),
):
    exam = require_exam(exam_id, db)
    course = db.get(Course, exam.course_id)
    if not course or not _student_exam_allowed(exam, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限参加该考试")
    if exam.status != "published":
        raise api_error(403, "EXAM_NOT_AVAILABLE", "考试未发布")
    if not student_in_exam_audience(db, exam, current_user.id):
        raise api_error(403, "NOT_IN_EXAM_AUDIENCE", "你不在本次考试范围内")

    existing = db.scalar(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam_id,
        ExamSubmission.student_id == current_user.id,
    ))
    # 只有首次开始受全局进入窗口限制。开始请求若因断网重试，已有进行中记录
    # 仍可幂等恢复，不能因为此时已过最晚进入时间而丢失会话。
    from app.services.time_utils import as_utc, utc_now
    now = utc_now()
    if existing is None:
        if exam.start_at is not None and as_utc(exam.start_at) > now:
            raise api_error(403, "EXAM_NOT_STARTED", "考试尚未开始")
        if exam.end_at is not None and as_utc(exam.end_at) <= now:
            raise api_error(403, "EXAM_EXPIRED", "考试已结束")

    started = svc_start_exam(exam, current_user, db, settings=settings)
    issued_token = None
    if existing is None:
        # Only the request that wins first-attempt creation may bind a credential.
        # A retry after a lost response cannot silently bind another device.
        submission = db.scalar(select(ExamSubmission).where(
            ExamSubmission.id == started.id).with_for_update()
            .execution_options(populate_existing=True))
        if submission is not None and not submission.active_device_token_hash:
            import hashlib
            import secrets
            issued_token = secrets.token_urlsafe(32)
            submission.active_device_token_hash = hashlib.sha256(
                issued_token.encode("utf-8")).hexdigest()
            db.commit()
            _set_exam_device_cookie(response, exam_id, issued_token, settings)
    result = _with_submission_aliases(build_student_exam_session(exam, current_user, db))
    return _with_exam_device_access(result, exam_id=exam_id, request=request, db=db,
                                    token_override=issued_token)


@router.post("/{exam_id}/takeover", status_code=status.HTTP_200_OK)
def takeover_exam(
    exam_id: int,
    payload: ExamTakeoverRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    redis=Depends(get_redis_client),
    current_user: User = Depends(require_roles("student")),
):
    """学生二次验密后换机接管：旧设备立即失去答题写入权限，剩余时间不重置。"""
    exam = require_exam(exam_id, db)
    if exam.status != "published" or not can_access_course_content(exam.course, current_user, db):
        raise api_error(403, "EXAM_NOT_AVAILABLE", "考试未发布或无权参加")
    if not _student_exam_allowed(exam, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限参加该考试")
    if not student_in_exam_audience(db, exam, current_user.id):
        raise api_error(403, "NOT_IN_EXAM_AUDIENCE", "你不在本次考试范围内")
    verify_student_reauth(request, current_user, payload.current_password, redis, settings, db)
    ip_hash, ua_hash = _client_fingerprint(request, settings)
    result = takeover_exam_session(
        db, exam, current_user,
        expected_write_generation=payload.expected_write_generation,
        device_id=payload.device_id, ip_hash=ip_hash, ua_hash=ua_hash,
    )
    token = result.get("submission", {}).pop("device_token", None)
    if not token:
        raise api_error(500, "DEVICE_CREDENTIAL_MISSING", "换机凭证未能生成，请重试")
    _set_exam_device_cookie(response, exam_id, token, settings)
    result["can_write"] = True
    result["takeover_required"] = False
    return result


@router.post("/{exam_id}/submit", response_model=ExamSessionRead, status_code=status.HTTP_201_CREATED)
def submit_exam(
    exam_id: int,
    payload: ExamSubmitPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(require_roles("student")),
):
    # A received submission remains an idempotent read even after the offering
    # is archived or its audience changes. First prove ownership of a terminal
    # attempt; only then opt into the narrow historical task projection.
    completed = db.scalar(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam_id,
        ExamSubmission.student_id == current_user.id,
        ExamSubmission.status.in_(("submitted", "grading", "graded", "review_required")),
    ))
    if completed is not None:
        historical_exam = db.scalar(select(Exam).where(Exam.id == exam_id)
                                    .execution_options(teaching_history=True))
        if historical_exam is not None:
            _delete_exam_device_cookie(response, exam_id, settings)
            result = _with_submission_aliases(
                build_student_exam_session(historical_exam, current_user, db))
            return _with_exam_device_access(result, exam_id=exam_id, request=request, db=db)
    exam = require_exam(exam_id, db)
    course = db.get(Course, exam.course_id)
    if not course or not _student_exam_allowed(exam, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限提交该考试")
    if exam.status != "published":
        raise api_error(403, "EXAM_NOT_AVAILABLE", "考试未发布")
    if not student_in_exam_audience(db, exam, current_user.id):
        raise api_error(403, "NOT_IN_EXAM_AUDIENCE", "你不在本次考试范围内")
    # 必须有提交记录（至少 started），已 grading/graded/submitted 由 service 层幂等处理
    sub = db.scalar(
        select(ExamSubmission).where(
            ExamSubmission.exam_id == exam_id,
            ExamSubmission.student_id == current_user.id,
        )
    )
    if not sub:
        raise api_error(403, "EXAM_NOT_STARTED", "请先开始考试")
    # 幂等：重复提交返回当前状态，不报错（review_required 不自动重试）
    if sub.status in ("submitted", "grading", "graded", "review_required"):
        _delete_exam_device_cookie(response, exam_id, settings)
        result = _with_submission_aliases(build_student_exam_session(exam, current_user, db))
        return _with_exam_device_access(result, exam_id=exam_id, request=request, db=db)
    ip_hash, ua_hash = _client_fingerprint(request, settings)
    svc_submit_exam(exam, current_user, db,
                    expected_write_generation=payload.expected_write_generation,
                    device_token=request.cookies.get(_exam_device_cookie_name(exam_id)),
                    ip_hash=ip_hash, ua_hash=ua_hash)
    _delete_exam_device_cookie(response, exam_id, settings)
    result = _with_submission_aliases(build_student_exam_session(exam, current_user, db))
    return _with_exam_device_access(result, exam_id=exam_id, request=request, db=db)


@router.post("/{exam_id}/submissions/{submission_id}/retry", response_model=ExamSubmissionRead)
def retry_exam_submission(
    exam_id: int,
    submission_id: int,
    payload: ExamRetryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
):
    """显式重试 review_required 的考试提交（教师/管理员受控入口）"""
    exam = require_exam(exam_id, db)
    ensure_exam_manager(exam, current_user)
    sub = db.get(ExamSubmission, submission_id)
    if not sub or sub.exam_id != exam_id:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "考试提交不存在")
    return retry_exam_submission_service(submission_id, payload.answer_ids, current_user, db)


@router.post("/{exam_id}/review-release", response_model=ExamRead)
def publish_exam_review(
    exam_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
):
    exam = require_exam(exam_id, db)
    ensure_exam_manager(exam, current_user)
    return release_exam_review(exam, current_user, db)


@router.patch("/{exam_id}/submissions/{submission_id}/extend", response_model=ExamSubmissionRead)
def extend_submission_time(
    exam_id: int,
    submission_id: int,
    payload: ExamTimeExtensionRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(require_roles("teacher", "admin")),
):
    exam = require_exam(exam_id, db)
    ensure_exam_manager(exam, current_user)
    submission = db.get(ExamSubmission, submission_id)
    if not submission or submission.exam_id != exam_id:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "考试提交不存在")
    return extend_exam_submission(submission, payload.minutes, current_user, db, settings=settings)


@router.get("/{exam_id}/proctor")
def exam_proctor(
    exam_id: int,
    limit: int = Query(default=200, ge=1, le=500),
    student_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
):
    """只读监考视图：切屏次数、累计离开时长、换机次数、最后保存时间与事件时间线。

    只记录、只展示：不自动交卷、不自动扣分、不产生任何写操作。
    """
    exam = require_exam(exam_id, db)
    ensure_exam_manager(exam, current_user)
    return build_proctor_overview(exam, db, limit=limit, student_id=student_id)


@router.post("/{exam_id}/submissions/{submission_id}/force-submit", response_model=ExamSubmissionRead)
def force_submit_submission(
    exam_id: int,
    submission_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
):
    exam = require_exam(exam_id, db)
    ensure_exam_manager(exam, current_user)
    submission = db.get(ExamSubmission, submission_id)
    if not submission or submission.exam_id != exam_id:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "考试提交不存在")
    return force_submit_exam_submission(submission, current_user, db)


@router.get("/{exam_id}/grades")
def exam_grades(
    exam_id: int,
    q: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    score_filter: str = Query(default="all", alias="score"),
    sort: str = "score_desc",
    pagination: PaginationParams = Depends(pagination),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
):
    server_now = utc_now()
    exam = require_exam(exam_id, db)
    if current_user.role == "teacher":
        ensure_exam_manager(exam, current_user)
    submission_join = and_(
        ExamSubmission.exam_id == exam_id,
        ExamSubmission.student_id == User.id,
    )

    enrolled = exists(
        select(CourseEnrollment.id).where(
            CourseEnrollment.course_id == Exam.course_id,
            CourseEnrollment.student_id == User.id,
            CourseEnrollment.status == "enrolled",
        )
    )
    selected_class_member = exists(
        select(ExamAudienceClass.id)
        .join(
            TeachingClassStudent,
            TeachingClassStudent.teaching_class_id == ExamAudienceClass.teaching_class_id,
        )
        .where(
            ExamAudienceClass.exam_id == Exam.id,
            TeachingClassStudent.student_id == User.id,
            TeachingClassStudent.status == "active",
        )
    )
    include_student = exists(
        select(ExamAudienceStudent.id).where(
            ExamAudienceStudent.exam_id == Exam.id,
            ExamAudienceStudent.student_id == User.id,
            ExamAudienceStudent.kind == "include",
        )
    )
    exclude_student = exists(
        select(ExamAudienceStudent.id).where(
            ExamAudienceStudent.exam_id == Exam.id,
            ExamAudienceStudent.student_id == User.id,
            ExamAudienceStudent.kind == "exclude",
        )
    )
    audience_condition = and_(
        or_(
            and_(Exam.audience_mode == "all_enrolled", enrolled),
            and_(Exam.audience_mode == "selected_classes", selected_class_member),
            include_student,
        ),
        or_(~exclude_student, include_student),
    )
    has_submission = exists(
        select(ExamSubmission.id).where(
            ExamSubmission.exam_id == exam_id,
            ExamSubmission.student_id == User.id,
        ).correlate(User, Exam)
    )
    student_scope = or_(audience_condition, has_submission)

    filters = []
    if q and q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(
            User.real_name.ilike(like),
            User.student_no.ilike(like),
            User.username.ilike(like),
        ))
    if score_filter not in {"all", "excellent", "good", "pass", "fail"}:
        raise api_error(422, "INVALID_GRADE_SCORE_FILTER", "成绩分数段筛选值无效")
    if score_filter != "all":
        score_ranges = {
            "excellent": (90, None),
            "good": (80, 90),
            "pass": (60, 80),
            "fail": (0, 60),
        }
        low, high = score_ranges[score_filter]
        score_filter_clause = [ExamSubmission.score.is_not(None), ExamSubmission.score >= low]
        if high is not None:
            score_filter_clause.append(ExamSubmission.score < high)
        filters.extend(score_filter_clause)
    if status_filter:
        normalized_status = status_filter.strip().lower()
        if normalized_status == "in_progress":
            filters.append(ExamSubmission.status == "started")
        elif normalized_status in {"submitted", "grading", "graded", "review_required"}:
            filters.append(ExamSubmission.status == normalized_status)
        elif normalized_status == "scheduled":
            filters.append(and_(
                ExamSubmission.id.is_(None),
                Exam.start_at.is_not(None),
                Exam.start_at > server_now,
            ))
        elif normalized_status == "ready":
            filters.append(and_(
                ExamSubmission.id.is_(None),
                or_(Exam.start_at.is_(None), Exam.start_at <= server_now),
                or_(Exam.end_at.is_(None), Exam.end_at > server_now),
            ))
        elif normalized_status == "missed":
            filters.append(and_(
                ExamSubmission.id.is_(None),
                Exam.end_at.is_not(None),
                Exam.end_at <= server_now,
            ))
        else:
            raise api_error(422, "INVALID_GRADE_STATUS_FILTER", "成绩状态筛选值无效")

    sort_expressions = _grade_sort_expressions()
    if sort not in sort_expressions:
        raise api_error(422, "INVALID_GRADE_SORT", "成绩排序值无效")

    # Join the one requested exam explicitly.  Besides making the status
    # predicates correct, this prevents another exam row from multiplying the
    # grade rows when the database contains multiple exams.
    base_query = (
        select(User, ExamSubmission)
        .select_from(User)
        .join(Exam, Exam.id == exam_id)
        .outerjoin(ExamSubmission, submission_join)
        .where(student_scope)
    )
    filtered_query = base_query.where(*filters)
    total = db.scalar(
        select(func.count()).select_from(filtered_query.order_by(None).subquery())
    ) or 0
    page, page_size = pagination.page, pagination.page_size
    rows = db.execute(
        filtered_query
        .order_by(*sort_expressions[sort])
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    def _grade_item(student: User, submission: ExamSubmission | None) -> dict:
        score = submission.score if submission else None
        derived_status = student_exam_status(exam, submission, server_now)[0]
        return {
            "id": submission.id if submission else f"absent-{student.id}",
            "exam_id": exam.id,
            "student_id": student.id,
            "student_name": student.real_name,
            "student_number": student.student_no or student.username,
            "submission_id": submission.id if submission else None,
            "status": derived_status,
            "score": score,
            "started_at": submission.started_at if submission else None,
            "expires_at": submission.expires_at if submission else None,
            "last_saved_at": submission.last_saved_at if submission else None,
            "submission_reason": submission.submission_reason if submission else None,
            "submitted_at": submission.submitted_at if submission else None,
            "graded_at": submission.graded_at if submission else None,
            "review_reason": submission.review_reason if submission else None,
        }

    status_keys = (
        "scheduled", "ready", "in_progress", "submitted", "grading", "graded", "review_required", "missed"
    )
    submission_status = case(
        (
            and_(
                ExamSubmission.status == "started",
                ExamSubmission.started_at.is_not(None),
                ExamSubmission.started_at > server_now,
            ),
            "scheduled",
        ),
        (ExamSubmission.status == "started", "in_progress"),
        (
            ExamSubmission.status.in_(
                ("submitted", "grading", "graded", "review_required")
            ),
            ExamSubmission.status,
        ),
        else_=ExamSubmission.status,
    )
    derived_status = case(
        (ExamSubmission.id.is_not(None), submission_status),
        (
            and_(Exam.start_at.is_not(None), Exam.start_at > server_now),
            "scheduled",
        ),
        (
            and_(Exam.end_at.is_not(None), Exam.end_at <= server_now),
            "missed",
        ),
        else_="ready",
    )
    submitted_statuses = ("submitted", "grading", "graded", "review_required")
    distribution_ranges = (
        ("90–100", 90, 101, "90_100"),
        ("80–89", 80, 90, "80_89"),
        ("70–79", 70, 80, "70_79"),
        ("60–69", 60, 70, "60_69"),
        ("0–59", 0, 60, "0_59"),
    )
    summary_columns = [
        func.count(User.id).label("expected_count"),
        func.sum(
            case((derived_status.in_(submitted_statuses), 1), else_=0)
        ).label("submitted_count"),
        func.count(ExamSubmission.score).label("graded_count"),
        func.avg(ExamSubmission.score).label("average_score"),
        func.max(ExamSubmission.score).label("highest_score"),
        func.sum(
            case(
                (
                    and_(
                        ExamSubmission.score.is_not(None),
                        ExamSubmission.score >= 60,
                    ),
                    1,
                ),
                else_=0,
            )
        ).label("pass_count"),
        func.sum(
            case(
                (
                    and_(
                        ExamSubmission.score.is_not(None),
                        ExamSubmission.score >= 90,
                    ),
                    1,
                ),
                else_=0,
            )
        ).label("excellent_count"),
    ]
    for _label, low, high, key in distribution_ranges:
        summary_columns.append(
            func.sum(
                case(
                    (
                        and_(
                            ExamSubmission.score.is_not(None),
                            ExamSubmission.score >= low,
                            ExamSubmission.score < high,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label(f"distribution_{key}")
        )
    for key in status_keys:
        summary_columns.append(
            func.sum(case((derived_status == key, 1), else_=0)).label(
                f"status_{key}"
            )
        )
    summary_row = db.execute(
        select(*summary_columns)
        .select_from(User)
        .join(Exam, Exam.id == exam_id)
        .outerjoin(ExamSubmission, submission_join)
        .where(student_scope)
    ).one()._mapping

    def _summary_int(key: str) -> int:
        return int(summary_row[key] or 0)

    scored_count = _summary_int("graded_count")
    average_score = summary_row["average_score"]
    highest_score = summary_row["highest_score"]
    distribution = [
        {"label": label, "count": _summary_int(f"distribution_{key}")}
        for label, _low, _high, key in distribution_ranges
    ]
    status_counts = {
        key: _summary_int(f"status_{key}") for key in status_keys
    }

    items = [_grade_item(student, submission) for student, submission in rows]

    question_count = db.scalar(
        select(func.count()).select_from(ExamQuestion).where(ExamQuestion.exam_id == exam_id)
    ) or 0
    total_score = db.scalar(
        select(func.sum(ExamQuestion.points)).where(ExamQuestion.exam_id == exam_id)
    ) or 0
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "exam": {
            "id": exam.id,
            "title": exam.title,
            "status": exam.status,
            "course_id": exam.course_id,
            "course_title": exam.course.title if exam.course else "",
            "duration_minutes": exam.duration_minutes,
            "question_count": question_count,
            "total_score": float(total_score),
            "start_at": exam.start_at,
            "end_at": exam.end_at,
            "show_score_after_grading": exam.show_score_after_grading,
            "show_questions_after_review": exam.show_questions_after_review,
            "show_answers_after_review": exam.show_answers_after_review,
            "review_released_at": exam.review_released_at,
            "server_now": server_now,
        },
        "summary": {
            "expected_count": _summary_int("expected_count"),
            "submitted_count": _summary_int("submitted_count"),
            "graded_count": scored_count,
            "average_score": round(float(average_score), 1) if average_score is not None else None,
            "highest_score": float(highest_score) if highest_score is not None else None,
            "pass_rate": round(_summary_int("pass_count") * 100 / scored_count, 1) if scored_count else 0,
            "excellent_rate": round(_summary_int("excellent_count") * 100 / scored_count, 1) if scored_count else 0,
            "status_counts": status_counts,
        },
        "distribution": distribution,
    }


def _question_teacher_payload(question: ExamQuestion, has_locked_rubric: bool) -> dict:
    """成绩详情中的教师题目视图——包含正确答案等解析字段。"""
    return {
        "id": question.id,
        "exam_id": question.exam_id,
        "question_type": question.question_type,
        "prompt": question.prompt,
        "options": question.options,
        "points": float(question.points),
        "order_index": question.order_index,
        "starter_code": question.starter_code,
        "public_cases": question.public_cases,
        "grading_mode": question.grading_mode,
        "correct_answer": question.correct_answer,
        "hidden_tests": question.hidden_tests,
        "time_limit_ms": question.time_limit_ms,
        "memory_limit_mb": question.memory_limit_mb,
        "teacher_constraints": question.teacher_constraints,
        "reference_solution": question.reference_solution,
        "test_groups": question.test_groups,
        "score_cap_rules": question.score_cap_rules,
        "has_locked_rubric": has_locked_rubric,
    }


def _exam_grade_detail_payload(exam: Exam, submission: ExamSubmission, db: Session) -> dict:
    """构建成绩详情页数据：试卷题目 + 逐题作答 + 成绩分析。"""
    questions = db.scalars(
        select(ExamQuestion)
        .where(ExamQuestion.exam_id == exam.id)
        .order_by(ExamQuestion.order_index, ExamQuestion.id)
    ).all()
    answers = db.scalars(
        select(ExamAnswer)
        .join(ExamQuestion, ExamQuestion.id == ExamAnswer.question_id)
        .where(ExamAnswer.submission_id == submission.id)
        .order_by(ExamQuestion.order_index, ExamQuestion.id)
    ).all()

    locked_ids: set[int] = set()
    if questions:
        locked_ids = set(db.scalars(
            select(QuestionRubric.exam_question_id).where(
                QuestionRubric.exam_question_id.in_([q.id for q in questions]),
                QuestionRubric.status == "locked",
            )
        ).all())

    question_map = {q.id: q for q in questions}
    answer_by_question = {a.question_id: a for a in answers}

    # 编程题关联的 AI 评分记录（exam_answer_id 唯一）——供详情页展示确定性/AI 评分状态
    code_grade_by_answer: dict[int, CodeGrade] = {}
    if answers:
        code_grades = db.scalars(
            select(CodeGrade).where(CodeGrade.exam_answer_id.in_([a.id for a in answers]))
        ).all()
        code_grade_by_answer = {cg.exam_answer_id: cg for cg in code_grades}

    from app.services.grading_version_service import frozen_points_for_question

    objective_score = 0.0
    objective_total = 0.0
    code_score = 0.0
    code_total = 0.0
    correct_count = 0
    points_snapshot_complete = True
    teacher_questions = []
    for question in questions:
        frozen_points = frozen_points_for_question(submission, question.id)
        question_payload = _question_teacher_payload(question, question.id in locked_ids)
        question_payload["points"] = frozen_points
        question_payload["points_snapshot_missing"] = frozen_points is None
        teacher_questions.append(question_payload)
        if frozen_points is None:
            points_snapshot_complete = False
            continue
        answer = answer_by_question.get(question.id)
        if question.question_type == "code":
            code_total += frozen_points
            if answer is not None:
                code_score += float(answer.score or 0)
        else:
            objective_total += frozen_points
            if answer is not None:
                objective_score += float(answer.score or 0)
        if answer is not None and answer.score is not None and float(answer.score) >= frozen_points:
            correct_count += 1

    elapsed_minutes = None
    if submission.started_at and submission.submitted_at:
        try:
            elapsed_minutes = max(1, round((submission.submitted_at - submission.started_at).total_seconds() / 60))
        except TypeError:
            elapsed_minutes = None

    return {
        "exam": {
            "id": exam.id,
            "title": exam.title,
            "course_title": exam.course.title if exam.course else "",
            "duration_minutes": exam.duration_minutes,
        },
        "student": {
            "id": submission.student.id,
            "name": submission.student.real_name,
            "number": submission.student.username,
        },
        "submission": {
            "id": submission.id,
            "status": submission.status,
            "score": submission.score,
            "started_at": submission.started_at,
            "expires_at": submission.expires_at,
            "last_saved_at": submission.last_saved_at,
            "submission_reason": submission.submission_reason,
            "submitted_at": submission.submitted_at,
            "graded_at": submission.graded_at,
            "elapsed_minutes": elapsed_minutes,
            "review_reason": submission.review_reason,
            "points_snapshot_complete": points_snapshot_complete,
        },
        "analysis": {
            "objective_score": round(objective_score, 2),
            "objective_total": round(objective_total, 2),
            "code_score": round(code_score, 2),
            "code_total": round(code_total, 2),
            "question_count": len(questions) or len(answers),
            "correct_count": correct_count,
        },
        "questions": teacher_questions,
        "answers": [
            _answer_payload(answer, question_map, code_grade_by_answer.get(answer.id), submission)
            for answer in answers
        ],
    }


def _tests_counts_from_details(details: dict | None) -> tuple[int | None, int | None]:
    """从确定性判题明细归一化样例通过数（与作业工作台口径一致）。"""
    groups = (details or {}).get("groups") or []
    if not groups:
        return None, None
    passed = total = 0
    for group in groups:
        counts = group.get("counts") or {}
        passed += int(counts.get("passed", 0) or 0)
        total += sum(int(counts.get(key, 0) or 0) for key in ("passed", "failed", "errors", "skipped"))
    return passed, total


def _ai_grading_payload(code_grade: CodeGrade | None) -> dict | None:
    """答案关联的 AI 评分上下文：确定性得分、AI 状态、失败原因。"""
    if code_grade is None:
        return None
    tests_passed, tests_total = _tests_counts_from_details(code_grade.deterministic_details)
    return {
        "id": code_grade.id,
        "mode": code_grade.mode,
        "status": code_grade.status,
        "functional_score": code_grade.functional_score,
        "robustness_score": code_grade.robustness_score,
        "algorithm_score": code_grade.algorithm_score,
        "quality_score": code_grade.quality_score,
        "final_score_100": code_grade.final_score_100,
        "scaled_score": code_grade.scaled_score,
        "tests_passed": tests_passed,
        "tests_total": tests_total,
        "needs_teacher_review": bool(code_grade.needs_teacher_review),
        "review_reason": code_grade.review_reason,
        "last_error": code_grade.last_error,
        "attempt_count": code_grade.attempt_count,
    }


def _answer_payload(answer: ExamAnswer, question_map: dict[int, ExamQuestion], code_grade: CodeGrade | None = None,
                    submission: ExamSubmission | None = None) -> dict:
    question = question_map.get(answer.question_id) or answer.question
    from app.services.grading_version_service import frozen_points_for_question

    points = frozen_points_for_question(submission, answer.question_id) if submission else None
    return {
        "id": answer.id,
        "question_id": answer.question_id,
        "order_index": question.order_index,
        "question_type": question.question_type,
        "prompt": question.prompt,
        "points": points,
        "score": float(answer.score) if answer.score is not None else None,
        "result_revision": answer.result_revision or 0,
        "points_snapshot_missing": points is None,
        "grading_status": answer.grading_status,
        "selected_options": answer.selected_options,
        "code_answer": answer.code_answer,
        "text_answers": answer.text_answers,
        "tests_passed": answer.tests_passed,
        "tests_total": answer.tests_total,
        "manual_score_reason": answer.manual_score_reason,
        "manual_score_at": answer.manual_score_at,
        "system_error": answer.system_error,
        "ai_grading": _ai_grading_payload(code_grade),
    }


def _require_grade_detail_access(exam: Exam, current_user: User) -> None:
    if current_user.role == "teacher":
        ensure_exam_manager(exam, current_user)


def _round_manual_score(value: float) -> float:
    """手动改分保留两位小数，避免把 17.93 这类 AI 评分吞成 17.9。"""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@router.get("/{exam_id}/grades/{submission_id}")
def exam_grade_detail(
    exam_id: int,
    submission_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
):
    exam = require_exam(exam_id, db)
    _require_grade_detail_access(exam, current_user)
    submission = db.get(ExamSubmission, submission_id)
    if not submission or submission.exam_id != exam_id:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "考试提交不存在")
    return _exam_grade_detail_payload(exam, submission, db)


def _set_manual_answer_score(exam: Exam, submission: ExamSubmission, answer: ExamAnswer,
                             question: ExamQuestion, score: float, reason: str,
                             expected_result_revision: int, actor: User, db: Session) -> dict:
    """写入逐题手动得分并重算父级总分。"""
    from app.services.grading_version_service import frozen_points_for_question

    max_points = frozen_points_for_question(submission, question.id)
    if max_points is None:
        raise api_error(409, "FROZEN_POINTS_MISSING", "该考试提交缺少可信分值快照，不能按当前题目配置修改正式成绩")
    if not math.isfinite(score) or score < 0 or score > max_points:
        raise api_error(
            422,
            "SCORE_OUT_OF_RANGE",
            f"得分必须在 0 到本题满分 {max_points:g} 分之间",
            fields={"score": [f"得分必须在 0 到 {max_points:g} 之间"]},
        )

    if answer.grading_status in ("pending", "queued", "running"):
        # 在途判题/AI 评分：人工改分会让迟到写入与新分数竞争，必须先受控取消或转人工
        raise api_error(
            409,
            "GRADING_IN_PROGRESS",
            "该题正在判题，请等待判题结束或先转入人工处理后再改分",
        )

    current_revision = answer.result_revision or 0
    if expected_result_revision != current_revision:
        raise api_error(409, "RESULT_REVISION_CONFLICT", "答案结果已更新，请刷新后重试")

    code_grades = []
    if question.question_type == "code":
        code_grades = db.scalars(
            select(CodeGrade).where(CodeGrade.exam_answer_id == answer.id)
            .order_by(CodeGrade.id).with_for_update()
        ).all()
        if any(grade.status in ("pending", "queued", "running") for grade in code_grades):
            raise api_error(409, "AI_GRADING_IN_PROGRESS", "AI 评分仍在进行，请等待或先转入人工处理")

    previous_score = answer.score
    now = utc_now()
    final_score = _round_manual_score(score)
    from sqlalchemy import update

    cas = db.execute(
        update(ExamAnswer)
        .execution_options(synchronize_session=False)
        .where(
            ExamAnswer.id == answer.id,
            ExamAnswer.result_revision == current_revision,
            ExamAnswer.grading_status.notin_(("pending", "queued", "running")),
        )
        .values(
            score=final_score,
            grading_status="completed",
            finished_at=now,
            manual_score_reason=reason.strip(),
            manual_score_at=now,
            claim_token=None,
            heartbeat_at=None,
            result_revision=ExamAnswer.result_revision + 1,
        )
    )
    if cas.rowcount != 1:
        db.rollback()
        raise api_error(409, "RESULT_REVISION_CONFLICT", "答案结果已更新，请刷新后重试")
    db.refresh(answer)

    db.add(ManualScoreAudit(
        target_kind="exam_answer", target_id=answer.id, score=final_score,
        reason=reason.strip(), actor_id=actor.id, actor_role=actor.role,
        result_revision=current_revision + 1, previous_score=previous_score,
        source="manual_score",
    ))

    db.flush()

    # 待复核提交：所有题目都有分数后自动转为已完成，并清除复核标记。
    if submission.status == "review_required":
        # 只要还有任何一道题没有分数（包括整题未作答、尚无 ExamAnswer 行），
        # 就保持待复核，不允许提前按已存在的答案汇总。
        missing = db.scalar(
            select(ExamQuestion.id)
            .outerjoin(
                ExamAnswer,
                and_(
                    ExamAnswer.question_id == ExamQuestion.id,
                    ExamAnswer.submission_id == submission.id,
                ),
            )
            .where(
                ExamQuestion.exam_id == submission.exam_id,
                or_(ExamAnswer.id.is_(None), ExamAnswer.score.is_(None)),
            )
            .limit(1)
        )
        if missing is not None:
            db.commit()
            db.refresh(submission)
            return _exam_grade_detail_payload(exam, submission, db)

    total = db.scalar(
        select(func.sum(ExamAnswer.score)).where(ExamAnswer.submission_id == submission.id)
    )
    total = _round_manual_score(float(total or 0))
    submission.score = total
    if submission.status == "review_required":
        submission.status = "graded"
        submission.review_reason = None
        submission.review_required_at = None
    submission.graded_at = utc_now()

    grade = db.scalar(
        select(ExamGrade).where(
            ExamGrade.exam_id == submission.exam_id,
            ExamGrade.student_id == submission.student_id,
        )
    )
    if grade is None:
        db.add(ExamGrade(exam_id=submission.exam_id, student_id=submission.student_id, score=total))
    else:
        grade.score = total

    db.commit()
    db.refresh(submission)
    return _exam_grade_detail_payload(exam, submission, db)


def _require_editable_submission(exam: Exam, submission_id: int, db: Session) -> ExamSubmission:
    submission = db.scalar(select(ExamSubmission).where(
        ExamSubmission.id == submission_id,
    ).with_for_update().execution_options(populate_existing=True))
    if not submission or submission.exam_id != exam.id:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "考试提交不存在")
    if submission.status not in ("graded", "review_required"):
        raise api_error(409, "SUBMISSION_NOT_EDITABLE", "仅已完成评分或待复核的提交可手动修改分数")
    return submission


@router.patch("/{exam_id}/grades/{submission_id}/answers/{answer_id}/score")
def update_exam_answer_score(
    exam_id: int,
    submission_id: int,
    answer_id: int,
    payload: ExamAnswerScoreUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
):
    """教师手动修改某道题的得分，并同步重算总分。"""
    exam = require_exam(exam_id, db)
    _require_grade_detail_access(exam, current_user)
    submission = _require_editable_submission(exam, submission_id, db)

    answer = db.scalar(
        select(ExamAnswer).where(
            ExamAnswer.id == answer_id,
            ExamAnswer.submission_id == submission_id,
        ).with_for_update().execution_options(populate_existing=True)
    )
    if answer is None:
        raise api_error(404, "ANSWER_NOT_FOUND", "答题记录不存在")
    question = db.get(ExamQuestion, answer.question_id)
    if question is None or question.exam_id != exam_id:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")
    return _set_manual_answer_score(exam, submission, answer, question, float(payload.score),
                                    payload.reason, payload.expected_result_revision, current_user, db)


@router.patch("/{exam_id}/grades/{submission_id}/questions/{question_id}/score")
def update_exam_question_score(
    exam_id: int,
    submission_id: int,
    question_id: int,
    payload: ExamAnswerScoreUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
):
    """对尚未生成答题记录的题目手动给分（例如学生整题未作答）。"""
    exam = require_exam(exam_id, db)
    _require_grade_detail_access(exam, current_user)
    submission = _require_editable_submission(exam, submission_id, db)

    question = db.scalar(
        select(ExamQuestion).where(
            ExamQuestion.id == question_id,
            ExamQuestion.exam_id == exam_id,
        )
    )
    if question is None:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")
    answer = db.scalar(
        select(ExamAnswer).where(
            ExamAnswer.submission_id == submission_id,
            ExamAnswer.question_id == question_id,
        )
    )
    if answer is None:
        answer = ExamAnswer(
            submission_id=submission_id,
            question_id=question_id,
            selected_options=None,
            code_answer=None,
            text_answers=None,
            version=1,
            grading_status="completed",
        )
        db.add(answer)
        db.flush()
        # 批次三 3B：人工评分入口补建答案时同样从冻结映射复制版本
        from app.services.grading_version_service import bind_exam_answer_version

        bind_exam_answer_version(db, answer)
    return _set_manual_answer_score(exam, submission, answer, question, float(payload.score),
                                    payload.reason, payload.expected_result_revision, current_user, db)


# ── 考试题目管理 ──

@router.get("/{exam_id}/questions", response_model=PaginatedResponse)
def get_questions(exam_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    exam = require_exam(exam_id, db)
    if current_user.role == "student":
        # 学生必须已选课且考试已发布
        if exam.status != "published":
            raise api_error(403, "EXAM_NOT_AVAILABLE", "考试未发布")
        if not can_access_course_content(exam.course, current_user, db):
            raise api_error(403, "FORBIDDEN", "请先选课")
        session = build_student_exam_session(exam, current_user, db)
        if not session["questions"]:
            raise api_error(403, "EXAM_NOT_STARTED", "请先开始考试或等待教师发布讲评")
        return PaginatedResponse(
            items=session["questions"], page=1, page_size=len(session["questions"]), total=len(session["questions"])
        )
    elif current_user.role == "teacher":
        # 教师只能看自己课程的考试题目
        course = db.get(Course, exam.course_id)
        ensure_exam_manager(exam, current_user, db)
    elif current_user.role not in ("admin",):
        # Unsupported roles cannot inspect exam questions.
        raise api_error(403, "FORBIDDEN", "无权查看考试题目")
    # admin 可以查看全部

    questions = list_questions(db, exam_id)
    if current_user.role in ("teacher", "admin"):
        locked_ids = set(db.scalars(select(QuestionRubric.exam_question_id).where(
            QuestionRubric.exam_question_id.in_([q.id for q in questions]),
            QuestionRubric.status == "locked",
        )).all()) if questions else set()
        items = [ExamQuestionTeacherRead.model_validate({
            **{column.name: getattr(q, column.name) for column in q.__table__.columns},
            "has_locked_rubric": q.id in locked_ids,
        }) for q in questions]
    else:
        items = [ExamQuestionRead.model_validate(q) for q in questions]
    return PaginatedResponse(items=items, page=1, page_size=len(items), total=len(items))

@router.post("/{exam_id}/questions", response_model=ExamQuestionRead, status_code=status.HTTP_201_CREATED)
def post_question(exam_id: int, payload: ExamQuestionCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return create_question(db, exam_id, payload.model_dump(exclude_unset=True), current_user)

@router.patch("/{exam_id}/questions/{question_id}", response_model=ExamQuestionRead)
def patch_question(exam_id: int, question_id: int, payload: ExamQuestionUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return update_question(db, exam_id, question_id, payload.model_dump(exclude_unset=True), current_user)

@router.delete("/{exam_id}/questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def del_question(exam_id: int, question_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    delete_question(db, exam_id, question_id, current_user)
    return None

# ── 学生答题 ──

@router.put("/{exam_id}/answers/{question_id}", status_code=status.HTTP_201_CREATED)
def put_answer(exam_id: int, question_id: int, payload: dict, request: Request,
               db: Session = Depends(get_db),
               settings: Settings = Depends(get_settings),
               current_user: User = Depends(require_roles("student"))):
    exam = require_exam(exam_id, db)
    if exam.status != "published" or not can_access_course_content(exam.course, current_user, db):
        raise api_error(403, "EXAM_NOT_AVAILABLE", "考试未发布或无权参加")
    ip_hash, ua_hash = _client_fingerprint(request, settings)
    return save_answer(db, exam_id, question_id, current_user, payload,
                       expected_write_generation=_require_write_generation(payload),
                       device_token=request.cookies.get(_exam_device_cookie_name(exam_id)),
                       ip_hash=ip_hash, ua_hash=ua_hash)


@router.put("/{exam_id}/answers", status_code=status.HTTP_200_OK)
def put_answers_batch(
    exam_id: int,
    payload: ExamAnswerBatchRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(require_roles("student")),
):
    from app.services.exam_service import require_exam_device_write
    from app.services.time_utils import utc_now
    exam = require_exam(exam_id, db)
    # Reject closed or unauthorized teaching scopes before revealing device
    # credential state. The scope check is read/write-authority validation;
    # device and generation are still checked once, before any batch item/event.
    exam = require_student_task_write(db, exam, current_user)
    if exam.status != "published" or not can_access_course_content(exam.course, current_user, db):
        raise api_error(403, "EXAM_NOT_AVAILABLE", "考试未发布或无权参加")
    ip_hash, ua_hash = _client_fingerprint(request, settings)
    sub = db.scalar(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam_id,
        ExamSubmission.student_id == current_user.id,
    ))
    if sub is not None and sub.status == "started":
        # 循环和事件写入前整体校验，不能让旧设备部分写入批次。
        require_exam_device_write(
            db, sub, expected_write_generation=payload.expected_write_generation,
            device_token=request.cookies.get(_exam_device_cookie_name(exam_id)),
            source="save_answer_batch",
        )
    events_recorded = 0
    if sub is not None and sub.status == "started" and payload.events:
        events_recorded = record_exam_events(
            db, sub, [item.model_dump() for item in payload.events], ip_hash=ip_hash, ua_hash=ua_hash,
        )
        db.commit()
        db.refresh(sub)
    results = []
    for item in payload.answers:
        try:
            answer = save_answer(
                db, exam_id, item.question_id, current_user, item.model_dump(exclude_none=True),
                expected_write_generation=payload.expected_write_generation,
                device_token=request.cookies.get(_exam_device_cookie_name(exam_id)),
                ip_hash=ip_hash, ua_hash=ua_hash,
            )
            results.append({
                "question_id": item.question_id,
                "ok": True,
                "version": answer.version,
                "saved_at": answer.updated_at,
            })
        except HTTPException as exc:
            db.rollback()
            results.append({
                "question_id": item.question_id,
                "ok": False,
                "code": exc.detail.get("code", "SAVE_FAILED") if isinstance(exc.detail, dict) else "SAVE_FAILED",
                "message": exc.detail.get("message", str(exc.detail)) if isinstance(exc.detail, dict) else str(exc.detail),
            })
    return {
        "results": results,
        "server_now": utc_now(),
        "events_recorded": events_recorded,
        "write_generation": int(sub.write_generation) if sub is not None else None,
    }

@router.get("/{exam_id}/my-grade")
def my_grade(exam_id: int, db: Session = Depends(get_db), current_user: User = Depends(require_roles("student"))):
    exam = require_exam(exam_id, db)
    if exam.status != "published" or not can_access_course_content(exam.course, current_user, db):
        raise api_error(403, "EXAM_NOT_AVAILABLE", "考试未发布或无权参加")
    return get_my_grade(exam_id, current_user, db)
