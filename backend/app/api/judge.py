import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.dependencies import PaginationParams, get_current_user, get_db, get_redis_client, pagination
from app.errors import api_error
from app.models import (
    Assignment, CodeGrade, Course, JudgeQuestion, ManualScoreAudit, Submission, User,
)
from app.schemas import (
    ImportDiagnosticRead, ManualScoreRequest, PaginatedResponse, SampleRunResponse,
    StudentSubmissionRead, SubmissionCreate, SubmissionRead,
)
from app.schemas.unified_submissions import TeacherJudgeSubmissionRead
from app.services.environment_service import (
    installed_imports_for_version,
    public_environment_summary,
    require_runnable_version,
    resolve_effective_policy,
    resolve_run_image_ref,
)
from app.services.import_policy import classify_imports
from app.services.time_utils import as_utc, utc_now
from app.worker.judge_worker import _get_timeout, _run_docker_pytest, _status_from_pytest
from app.services.student_ai_results import build_student_grading_breakdown
from app.services.audience_service import require_student_assignment_access
from app.services.offering_service import require_student_task_write

router = APIRouter(prefix="/judge", tags=["judge"])

# 幂等正文摘要有固定版本：只包含不可变的请求内容（题目 + 原样代码），
# 不含环境/截止/次数等可变配置——否则同一请求会算出不同摘要。
REQUEST_DIGEST_VERSION = 1


def submission_request_digest(question_id: int, code: str) -> str:
    """固定 v1 规范 JSON 的 SHA-256 摘要（不归一化代码空白/换行）。"""
    import hashlib
    import json as _json

    payload = _json.dumps(
        {"v": REQUEST_DIGEST_VERSION, "question_id": int(question_id), "code": code},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _existing_idempotent_submission(db, student_id: int, client_request_id: str, digest: str):
    """返回 (submission, conflict)：命中同键同正文 → 原提交；同键不同正文 → 冲突。"""
    existing = db.scalar(
        select(Submission).where(
            Submission.student_id == student_id,
            Submission.client_request_id == client_request_id,
        )
    )
    if existing is None:
        return None, False
    if existing.request_digest == digest:
        return existing, False
    return None, True


def require_assignment_before_deadline(assignment: Assignment) -> None:
    """拒绝截止时刻及之后的新自测/提交；无截止时间的作业保持开放。"""
    due_at = as_utc(assignment.due_at)
    if due_at is not None and utc_now() >= due_at:
        raise api_error(
            403,
            "ASSIGNMENT_DEADLINE_PASSED",
            "作业已截止，请联系教师延长截止时间后再试",
        )


def require_submission(submission_id: int, db: Session) -> Submission:
    submission = db.get(Submission, submission_id)
    if not submission:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "提交不存在")
    return submission


def can_view_submission(submission: Submission, user: User, db: Session) -> bool:
    if user.role == "admin":
        return True
    if user.role == "teacher":
        question = db.get(JudgeQuestion, submission.question_id)
        if question:
            assignment = db.get(Assignment, question.assignment_id)
            if assignment:
                course = db.get(Course, assignment.course_id)
                from app.services.offering_service import teacher_offering_ids
                if assignment.offering_id in teacher_offering_ids(db, user.id):
                    return True
        return False
    return submission.student_id == user.id


@router.post("/submissions", response_model=None, status_code=status.HTTP_201_CREATED)
def create_submission(
    payload: SubmissionCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
    redis_client = Depends(get_redis_client),
):
    if current_user.role != "student":
        raise api_error(403, "FORBIDDEN", "只有学生可以提交代码")
    question = db.get(JudgeQuestion, payload.question_id)
    if not question:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")
    assignment = db.get(Assignment, question.assignment_id)
    if not assignment:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")
    # Preserve the submission endpoint's established unpublished-task response.
    if assignment.status != "published":
        raise api_error(400, "ASSIGNMENT_NOT_AVAILABLE", "作业不可提交")
    assignment = require_student_task_write(db, assignment, current_user)
    require_student_assignment_access(db, assignment, current_user, current=True)

    # 幂等查询**先于**次数与截止限制：重放一个已接收的请求不应因"之后到点/用完次数"而失败。
    request_key = str(payload.client_request_id) if payload.client_request_id else None
    digest = submission_request_digest(payload.question_id, payload.code) if request_key else None
    if request_key:
        existing, conflict = _existing_idempotent_submission(
            db, current_user.id, request_key, digest
        )
        if conflict:
            raise api_error(409, "IDEMPOTENCY_KEY_REUSED",
                            "同一请求键已用于不同的提交内容，请使用新的请求键")
        if existing is not None:
            # A02：同键重放同样返回学生白名单投影，绝不回传 stdout/stderr/result_details
            return student_projection(existing, db)

    require_assignment_before_deadline(assignment)
    # check max attempts
    # 平台自身故障（system_error：Docker/环境/结果不可验证等）不消耗学生次数——
    # 只统计真正产生了判题结论的提交（见 docs 中的"系统错误不扣分"约定）。
    if question.max_attempts is not None:
        count = db.scalar(
            select(func.count()).select_from(Submission).where(
                Submission.question_id == payload.question_id,
                Submission.student_id == current_user.id,
                Submission.grading_status != "system_error",
            )
        ) or 0
        if count >= question.max_attempts:
            raise api_error(400, "MAX_ATTEMPTS_REACHED", f"已达到最大提交次数（{question.max_attempts}次）")
    # Phase 5（计划 8.2）：入队前冻结实际使用的环境版本与 import 策略快照。
    # 题目覆盖优先，否则作业默认环境；策略 inherit → 作业策略。
    env_id = question.environment_version_id or assignment.environment_version_id
    policy = resolve_effective_policy(assignment, question)
    if env_id is not None:
        # 已绑定版本只要求镜像仍可运行；它不必还是教师当前可选版本。
        require_runnable_version(db, env_id)
    submission = Submission(
        question_id=payload.question_id,
        student_id=current_user.id,
        code=payload.code,
        status="queued",
        grading_status="pending",
        attempt_count=0,
        environment_version_id=env_id,
        import_policy_mode_snapshot=policy.mode,
        allowed_imports_snapshot=sorted(policy.allowed_imports),
        client_request_id=request_key,
        request_digest=digest,
    )
    db.add(submission)
    # 批次三 3B：提交创建时绑定题目当前 active 评分版本；
    # 没有 active 版本时留空（后续评分转人工，绝不静默绑定最新配置）。
    from app.services.grading_version_service import bind_submission_version

    bind_submission_version(db, submission)
    try:
        db.commit()
    except IntegrityError:
        # 并发同键：唯一约束是事实源，读取 winner 并按幂等语义返回；
        # 其他数据库错误正常失败（不回退为"假装成功"）。
        db.rollback()
        winner, conflict = _existing_idempotent_submission(
            db, current_user.id, request_key or "", digest or ""
        )
        if winner is not None:
            return student_projection(winner, db)
        if conflict:
            raise api_error(409, "IDEMPOTENCY_KEY_REUSED",
                            "同一请求键已用于不同的提交内容，请使用新的请求键")
        raise
    db.refresh(submission)

    # 使用统一入队入口：条件 UPDATE pending→queued + Redis 推送
    from app.services.judge_queue import enqueue_job
    enqueue_job(db, job_type="assignment", object_id=submission.id)

    return student_projection(submission, db)

@router.get("/submissions", response_model=PaginatedResponse)
def list_submissions(
    pagination: PaginationParams = Depends(pagination),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page, page_size = pagination.page, pagination.page_size
    query = select(Submission)
    count_query = select(func.count()).select_from(Submission)
    if current_user.role == "student":
        query = query.where(Submission.student_id == current_user.id)
        count_query = count_query.where(Submission.student_id == current_user.id)
    elif current_user.role == "teacher":
        # Both page rows and count use the teaching-instance authority boundary.
        from app.services.offering_service import teacher_task_condition
        scope = teacher_task_condition(Assignment, current_user.id)
        query = query.join(JudgeQuestion, Submission.question_id == JudgeQuestion.id).join(
            Assignment, JudgeQuestion.assignment_id == Assignment.id
        ).where(scope)
        count_query = count_query.join(JudgeQuestion, Submission.question_id == JudgeQuestion.id).join(
            Assignment, JudgeQuestion.assignment_id == Assignment.id
        ).where(scope)
    total = db.scalar(count_query) or 0
    submissions = db.scalars(query.order_by(Submission.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    if current_user.role == "student":
        grade_rows = db.scalars(select(CodeGrade).where(
            CodeGrade.submission_id.in_([item.id for item in submissions])
        )).all() if submissions else []
        grades_by_submission = {grade.submission_id: grade for grade in grade_rows}
        items = [student_projection(item, ai_grade=grades_by_submission.get(item.id))
                 for item in submissions]
    else:
        items = [SubmissionRead.model_validate(item) for item in submissions]
    return PaginatedResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/submissions/{submission_id}", response_model=None)
def get_submission(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """提交详情：学生得到白名单投影，教师/管理员得到完整视图。

    学生响应体不含 stdout/stderr（原始判题输出可能包含隐藏测试信息）。
    """
    submission = require_submission(submission_id, db)
    if not can_view_submission(submission, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限查看该提交")
    if current_user.role == "student":
        return student_projection(submission, db)
    return _with_diagnostic(submission)


@router.get("/submissions/{submission_id}/teacher", response_model=TeacherJudgeSubmissionRead)
def get_teacher_submission_detail(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """教师作业提交详情：在 SubmissionRead 之上补充学生 / 题目 / 作业 / AI 评分上下文。"""
    if current_user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "仅教师和管理员可访问")
    submission = require_submission(submission_id, db)
    if not can_view_submission(submission, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限查看该提交")

    question = db.get(JudgeQuestion, submission.question_id)
    assignment = db.get(Assignment, question.assignment_id) if question else None
    course = db.get(Course, assignment.course_id) if assignment else None
    student = db.get(User, submission.student_id)
    code_grade = db.scalar(
        select(CodeGrade).where(CodeGrade.submission_id == submission.id)
    )
    return TeacherJudgeSubmissionRead(
        id=submission.id,
        question_id=submission.question_id,
        student_id=submission.student_id,
        student_name=student.real_name if student else None,
        student_no=student.student_no if student else None,
        code=submission.code,
        status=submission.status,
        grading_status=submission.grading_status,
        score=submission.score,
        created_at=submission.created_at,
        finished_at=submission.finished_at,
        tests_passed=submission.tests_passed,
        tests_total=submission.tests_total,
        result_details=submission.result_details,
        execution_time_ms=submission.execution_time_ms,
        stdout=submission.stdout,
        stderr=submission.stderr,
        question_title=question.title if question else None,
        assignment_id=assignment.id if assignment else None,
        assignment_title=assignment.title if assignment else None,
        course_id=course.id if course else None,
        course_title=course.title if course else None,
        ai_grade_id=code_grade.id if code_grade else None,
        ai_score=code_grade.final_score_100 if code_grade else None,
        ai_needs_review=code_grade.needs_teacher_review if code_grade else False,
        ai_review_reason=code_grade.review_reason if code_grade else None,
    )


@router.get("/submissions/{submission_id}/result", response_model=None)
def get_submission_result(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    submission = require_submission(submission_id, db)
    if not can_view_submission(submission, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限查看该判题结果")

    # 学生可见的 AI 分项：仅 active+completed
    if current_user.role == "student":
        cg = db.scalar(
            select(CodeGrade).where(
                CodeGrade.submission_id == submission_id,
                CodeGrade.mode == "active",
                CodeGrade.status == "completed",
            )
        )
        if cg and cg.ai_result:
            submission.grading_breakdown = build_student_grading_breakdown(cg)
        return student_projection(submission, db)

    return _with_diagnostic(submission)


def _with_diagnostic(submission: Submission) -> SubmissionRead:
    """把 result_details 中的结构化 diagnostic 提升为顶层字段（学生 API 安全中文信息）。"""
    data = SubmissionRead.model_validate(submission)
    diag = (submission.result_details or {}).get("diagnostic")
    if isinstance(diag, dict) and diag.get("code"):
        try:
            data.diagnostic = ImportDiagnosticRead.model_validate(diag)
        except Exception:
            data.diagnostic = None
    return data

def student_projection(submission: Submission, db: Session | None = None, *,
                       ai_grade: CodeGrade | None = None) -> StudentSubmissionRead:
    """白名单投影：只暴露学生可见字段，绝不返回 stdout/stderr 与隐藏测试内容。

    同时给出**终态与评分来源**，前端据此停止轮询并展示"等待复核/人工已定分"：
    * ``processing_terminal``：判题流程已到终态（completed/system_error），不必再轮询；
    * ``score_finalized``：已有最终分且无在途工作；
    * ``grading_source``：manual（教师人工定分）/ ai（AI 已完成）/ auto（自动判题）。
    """
    details = submission.result_details or {}
    manual = details.get("manual_review")
    grading_status = submission.grading_status
    processing_terminal = grading_status in ("completed", "system_error")
    if ai_grade is None and db is not None:
        ai_grade = db.scalar(select(CodeGrade).where(CodeGrade.submission_id == submission.id))
    active_ai_pending = bool(
        ai_grade is not None and ai_grade.mode == "active"
        and ai_grade.status in ("pending", "queued", "running")
    )
    active_ai_review = bool(
        ai_grade is not None and ai_grade.mode == "active"
        and (ai_grade.status == "review_required" or ai_grade.needs_teacher_review)
    )
    if active_ai_pending:
        processing_terminal = False
    score_finalized = (
        processing_terminal and submission.score is not None
        and not active_ai_pending and not active_ai_review
    )
    if isinstance(manual, dict) and manual.get("score") is not None:
        grading_source = "manual"
    elif (ai_grade is not None and ai_grade.mode == "active"
          and ai_grade.status == "completed" and score_finalized):
        grading_source = "ai"
    elif score_finalized:
        grading_source = "auto"
    else:
        grading_source = None
    return StudentSubmissionRead(
        id=submission.id,
        question_id=submission.question_id,
        student_id=submission.student_id,
        code=submission.code,
        status=submission.status,
        grading_status=submission.grading_status,
        score=submission.score,
        tests_passed=submission.tests_passed,
        tests_total=submission.tests_total,
        execution_time_ms=submission.execution_time_ms,
        grading_breakdown=getattr(submission, "grading_breakdown", None),
        diagnostic=_diagnostic_of(submission),
        manual_review=manual if isinstance(manual, dict) else None,
        processing_terminal=processing_terminal,
        score_finalized=score_finalized,
        grading_source=grading_source,
        created_at=submission.created_at,
        finished_at=submission.finished_at,
    )


def _diagnostic_of(submission: Submission) -> ImportDiagnosticRead | None:
    diag = (submission.result_details or {}).get("diagnostic")
    if isinstance(diag, dict) and diag.get("code"):
        try:
            return ImportDiagnosticRead.model_validate(diag)
        except Exception:
            return None
    return None


@router.patch("/submissions/{submission_id}/manual-score", response_model=TeacherJudgeSubmissionRead)
def manual_score_submission(
    submission_id: int,
    payload: ManualScoreRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """教师/管理员人工最终分（批次三 3D）。

    与自动判题、AI 评分相互独立：不改写历史结果，只产生**新的结果版本**
    （``result_revision`` 原子 +1）并记录评分者、说明与时间。

    拒绝条件：
    * 非教师/管理员 → 403；
    * 无该作业的授课范围 → 403；
    * 判题仍在运行（``grading_status == "running"``）→ 409 GRADING_IN_PROGRESS
      （在途写入持有认领，人工分必须先等它结束，避免相互覆盖）；
    * 传入的 ``expected_result_revision`` 与当前不一致 → 409 RESULT_REVISION_CONFLICT。
    """
    if current_user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "仅教师和管理员可以人工评分")
    # Lock the result row before touching associated AI records. AI terminal writers
    # follow this same target-then-CodeGrade order, so stale AI work cannot win later.
    submission = db.scalar(
        select(Submission).where(Submission.id == submission_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if submission is None:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "提交不存在")
    if not can_view_submission(submission, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限评分该提交")
    # A03：在途状态必须覆盖"排队/运行中的主判题"和"排队/运行中的 AI 评分记录"。
    # 只拒绝 running 会放过 queued 提交与 pending AI——它们随后会用旧认领写回并覆盖人工分。
    if submission.grading_status in ("queued", "running"):
        raise api_error(409, "GRADING_IN_PROGRESS", "判题仍在进行，请稍后重试")
    in_flight_grades = db.scalars(
        select(CodeGrade.id).where(
            CodeGrade.submission_id == submission_id,
            CodeGrade.status.in_(("pending", "queued", "running")),
        ).with_for_update()
    ).all()
    if in_flight_grades:
        raise api_error(409, "AI_GRADING_IN_PROGRESS", "AI 评分仍在进行，请稍后重试")
    current_revision = submission.result_revision or 0
    if payload.expected_result_revision != current_revision:
        raise api_error(409, "RESULT_REVISION_CONFLICT", "结果已更新，请刷新后重试")

    now = datetime.now(timezone.utc)
    details = dict(submission.result_details or {})
    history = details.get("manual_review_history")
    history = list(history) if isinstance(history, list) else []
    record = {
        "score": float(payload.score),
        "reason": payload.reason,
        "actor_id": current_user.id,
        "actor_role": current_user.role,
        "at": now.isoformat(),
    }
    history.append(record)
    details["manual_review_history"] = history[-10:]
    details["manual_review"] = record
    # 同一条条件 UPDATE 完成：结果、终态、修订号 CAS 与旧认领失效。
    # 以读到的修订号为条件 → 两个教师并发时只有一个命中，另一个 409。
    from sqlalchemy import update

    cas = db.execute(
        update(Submission)
        .execution_options(synchronize_session=False)
        .where(
            Submission.id == submission_id,
            Submission.result_revision == current_revision,
            Submission.grading_status.notin_(("queued", "running")),
        )
        .values(
            score=float(payload.score),
            status="graded",
            grading_status="completed",
            result_details=details,
            claim_token=None,
            heartbeat_at=None,
            result_revision=Submission.result_revision + 1,
            finished_at=submission.finished_at or now,
        )
    )
    if cas.rowcount != 1:
        db.rollback()
        raise api_error(409, "RESULT_REVISION_CONFLICT", "结果已更新，请刷新后重试")
    # 防御性收口：即使存在在途 AI 记录（竞态窗口内新建），也使其认领失效并标记人工已覆盖，
    # 保留 AI 原始证据字段（分数/明细）不被改写。
    db.execute(
        update(CodeGrade)
        .execution_options(synchronize_session=False)
        .where(
            CodeGrade.submission_id == submission_id,
            CodeGrade.status.in_(("pending", "queued", "running")),
        )
        .values(status="completed", needs_teacher_review=True,
                review_reason="教师人工评分已覆盖", claim_token=None)
    )
    # append-only 审计：每次人工给分新增一行（展示用 history 只是最近若干条）
    db.add(ManualScoreAudit(
        target_kind="submission", target_id=submission_id,
        score=float(payload.score), reason=payload.reason,
        actor_id=current_user.id, actor_role=current_user.role,
        result_revision=current_revision + 1, previous_score=submission.score,
        source="manual_score",
    ))
    db.commit()
    db.refresh(submission)
    return get_teacher_submission_detail(submission_id, db, current_user)


@router.post("/questions/{question_id}/sample-run", response_model=SampleRunResponse)
def sample_run(
    question_id: int,
    payload: SubmissionCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    """仅运行公开样例——不创建 Submission、不计提交次数、不访问隐藏测试"""
    if current_user.role != "student":
        raise api_error(403, "FORBIDDEN", "只有学生可以使用 sample-run")

    if question_id != payload.question_id:
        raise api_error(400, "QUESTION_MISMATCH", "题目 ID 不一致")
    question = db.get(JudgeQuestion, question_id)
    if not question:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")

    assignment = db.get(Assignment, question.assignment_id)
    if not assignment:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")
    assignment = require_student_task_write(db, assignment, current_user)
    require_student_assignment_access(db, assignment, current_user, current=True)

    # 幂等查询**先于**次数与截止限制：重放一个已接收的请求不应因"之后到点/用完次数"而失败。
    request_key = str(payload.client_request_id) if payload.client_request_id else None
    digest = submission_request_digest(payload.question_id, payload.code) if request_key else None
    if request_key:
        existing, conflict = _existing_idempotent_submission(
            db, current_user.id, request_key, digest
        )
        if conflict:
            raise api_error(409, "IDEMPOTENCY_KEY_REUSED",
                            "同一请求键已用于不同的提交内容，请使用新的请求键")
        if existing is not None:
            # A02：同键重放同样返回学生白名单投影，绝不回传 stdout/stderr/result_details
            return student_projection(existing, db)

    require_assignment_before_deadline(assignment)

    public_cases = question.public_cases or []
    if not public_cases:
        return SampleRunResponse(output="", status="no_public_cases", execution_time_ms=0)

    # Phase 5（计划 8.3）：解析题目有效环境（题目覆盖优先，否则作业默认），
    # sample-run 与正式判题使用同一 digest 镜像，不再默认 settings.judge_image。
    env_id = question.environment_version_id or assignment.environment_version_id
    policy = resolve_effective_policy(assignment, question)
    image_ref = None
    installed: set[str] = set()
    if env_id is not None:
        try:
            image_ref = resolve_run_image_ref(db, env_id)
        except Exception:
            raise api_error(503, "ENVIRONMENT_IMAGE_MISSING",
                            "运行环境暂不可用，本次提交不会扣分，请稍后重试")
        installed = installed_imports_for_version(db, env_id)

    # import 预检：IMPORT_NOT_ALLOWED 直接拦截（不跑 Docker）；IMPORT_NOT_INSTALLED 平台配置问题
    if policy.restricted:
        diagnostics = classify_imports(payload.code, policy, installed)
        if diagnostics:
            diagnostic = diagnostics[0]
            if diagnostic.code == "IMPORT_NOT_ALLOWED":
                return SampleRunResponse(
                    output="", status="import_not_allowed", execution_time_ms=0,
                    diagnostic=diagnostic,
                )
            return SampleRunResponse(
                output="", status="import_not_installed", execution_time_ms=0,
                diagnostic=diagnostic,
            )

    # 仅用公开样例构建测试文件
    test_code = f"{payload.code}\n\n"
    test_code += "def test_public_cases():\n"
    for idx, case in enumerate(public_cases):
        args = case.get("args", [])
        expected = case.get("expected")
        args_str = ", ".join(repr(a) for a in args)
        test_code += f"    assert {question.function_name}({args_str}) == {repr(expected)}\n"
    # All runtime inputs are resolved before releasing the short policy lock.
    # Self-tests persist no result and must not hold teaching locks during Docker.
    timeout_seconds = _get_timeout(question, settings)
    memory_mb = question.memory_limit_mb or settings.judge_memory_limit_mb
    db.commit()
    with tempfile.TemporaryDirectory(prefix="dai-sample-") as temp_dir:
        workdir = Path(temp_dir)
        (workdir / "test_sample.py").write_text(test_code, encoding="utf-8")

        try:
            stdout, stderr, returncode, elapsed_ms = _run_docker_pytest(
                workdir, settings, timeout_seconds, memory_mb, test_filename="test_sample.py",
                image_ref=image_ref,
            )
        except FileNotFoundError:
            raise api_error(503, "JUDGE_UNAVAILABLE", "判题服务不可用（Docker 未就绪）")

    status, _score = _status_from_pytest(returncode, stdout, stderr)
    return SampleRunResponse(
        output=f"{stdout}\n{stderr}",
        status=status,
        execution_time_ms=elapsed_ms,
    )
