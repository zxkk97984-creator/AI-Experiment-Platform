import math
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.courses import can_access_course_content
from app.config import Settings, get_settings
from app.dependencies import PaginationParams, get_current_user, get_db, get_redis_client, pagination
from app.errors import api_error
from app.models import Assignment, CodeGrade, Course, CourseEnrollment, JudgeQuestion, Submission, User
from app.schemas import ImportDiagnosticRead, PaginatedResponse, SampleRunResponse, SubmissionCreate, SubmissionRead
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
from app.services.audience_service import student_in_assignment_audience
from app.services.offering_service import require_student_task_write

router = APIRouter(prefix="/judge", tags=["judge"])


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


@router.post("/submissions", response_model=SubmissionRead, status_code=status.HTTP_201_CREATED)
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
    if assignment.status != "published":
        raise api_error(400, "ASSIGNMENT_NOT_AVAILABLE", "作业不可提交")
    assignment = require_student_task_write(db, assignment, current_user)
    course = db.get(Course, assignment.course_id)
    if current_user.role == "student":
        if not course or not (
            student_in_assignment_audience(db, assignment, current_user.id)
            or can_access_course_content(course, current_user, db)
        ):
            raise api_error(403, "FORBIDDEN", "没有权限提交该题目")
        if not student_in_assignment_audience(db, assignment, current_user.id):
            raise api_error(403, "NOT_IN_ASSIGNMENT_AUDIENCE", "你不在本次作业发布范围内")
    elif not course or not can_access_course_content(course, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限提交该题目")
    require_assignment_before_deadline(assignment)
    # check max attempts
    if question.max_attempts is not None:
        count = db.scalar(
            select(func.count()).select_from(Submission).where(
                Submission.question_id == payload.question_id,
                Submission.student_id == current_user.id,
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
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)

    # 使用统一入队入口：条件 UPDATE pending→queued + Redis 推送
    from app.services.judge_queue import enqueue_job
    enqueue_job(db, job_type="assignment", object_id=submission.id)

    return submission

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
        # 教师只看自己课程的提交
        query = query.join(JudgeQuestion, Submission.question_id == JudgeQuestion.id).join(
            Assignment, JudgeQuestion.assignment_id == Assignment.id
        ).join(Course, Assignment.course_id == Course.id).where(
            Course.teacher_id == current_user.id
        )
        count_query = count_query.join(JudgeQuestion, Submission.question_id == JudgeQuestion.id).join(
            Assignment, JudgeQuestion.assignment_id == Assignment.id
        ).join(Course, Assignment.course_id == Course.id).where(
            Course.teacher_id == current_user.id
        )
    total = db.scalar(count_query) or 0
    submissions = db.scalars(query.order_by(Submission.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return PaginatedResponse(items=[SubmissionRead.model_validate(item) for item in submissions], page=page, page_size=page_size, total=total)


@router.get("/submissions/{submission_id}", response_model=SubmissionRead)
def get_submission(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    submission = require_submission(submission_id, db)
    if not can_view_submission(submission, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限查看该提交")
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


@router.get("/submissions/{submission_id}/result", response_model=SubmissionRead)
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
    if assignment.status != "published":
        raise api_error(403, "ASSIGNMENT_NOT_AVAILABLE", "作业未发布")
    course = db.get(Course, assignment.course_id)
    if not course or course.status != "published":
        raise api_error(403, "COURSE_NOT_AVAILABLE", "课程未发布")
    enrollment = db.scalar(
        select(CourseEnrollment).where(
            CourseEnrollment.course_id == course.id,
            CourseEnrollment.student_id == current_user.id,
            CourseEnrollment.status == "enrolled",
        )
    )
    if not enrollment:
        raise api_error(403, "NOT_ENROLLED", "请先选课")
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
