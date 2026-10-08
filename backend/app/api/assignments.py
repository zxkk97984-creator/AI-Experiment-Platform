from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session

from app.api.courses import can_access_course_content, require_course
from app.config import Settings, get_settings
from app.dependencies import get_current_user, get_db, require_roles, PaginationParams, pagination
from app.errors import api_error
from app.models import (
    Assignment,
    CodeGrade,
    Course,
    JudgeQuestion,
    QuestionRubric,
    Submission,
    User,
)
from app.schemas.assignment_progress import AssignmentProgressRead
from app.schemas import (
    AssignmentCreate,
    CourseStudentImportResult,
    CourseStudentImportRow,
    AssignmentRead,
    AssignmentUpdate,
    JudgeQuestionCreate,
    JudgeQuestionRead,
    JudgeQuestionUpdate,
    PaginatedResponse,
)
from app.services.time_utils import utc_now
from app.services.audience_service import (
    assignment_visible_condition, import_audience_students, parse_student_csv,
    populate_audience_cache, require_effective_audience, save_audience,
    require_student_assignment_access,
)

from app.services.offering_service import teacher_task_condition
from sqlalchemy import and_, or_

router = APIRouter(prefix="/assignments", tags=["assignments"])


def require_assignment(assignment_id: int, db: Session) -> Assignment:
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        raise api_error(404, "ASSIGNMENT_NOT_FOUND", "作业不存在")
    return assignment


def _submitted_map(db: Session, assignments: list[Assignment], student_id: int) -> dict[int, bool]:
    """批量计算学生对每个作业的已交状态，避免逐作业 N+1 查询。

    语义与 dashboard 待办判定互补：「至少一题无该学生提交记录」即待办，
    因此「全部题目都有提交记录」才算已交；
    零题作业没有任何可提交的题目，永远视为未提交（未完成）。
    """
    if not assignments:
        return {}
    assignment_ids = [a.id for a in assignments]
    rows = db.execute(
        select(JudgeQuestion.assignment_id, JudgeQuestion.id)
        .where(JudgeQuestion.assignment_id.in_(assignment_ids))
    ).all()
    qids_by_assignment: dict[int, list[int]] = {}
    for aid, qid in rows:
        qids_by_assignment.setdefault(aid, []).append(qid)
    submitted_qids: set[int] = set()
    all_qids = [qid for _, qid in rows]
    if all_qids:
        submitted_qids = set(
            db.scalars(
                select(Submission.question_id).where(
                    Submission.student_id == student_id,
                    Submission.question_id.in_(all_qids),
                )
            ).all()
        )
    return {
        a.id: (
            False
            if not qids_by_assignment.get(a.id)
            else all(qid in submitted_qids for qid in qids_by_assignment[a.id])
        )
        for a in assignments
    }


def ensure_assignment_manager(assignment: Assignment, user: User, db: Session):
    from app.services.offering_service import task_content_manager
    task_content_manager(db, assignment, user)


def _assignment_has_submissions(db: Session, assignment_id: int) -> bool:
    return (
        db.scalar(
            select(Submission.id)
            .join(JudgeQuestion, Submission.question_id == JudgeQuestion.id)
            .where(JudgeQuestion.assignment_id == assignment_id)
            .limit(1)
        )
        is not None
    )


def ensure_scoring_editable(db: Session, assignment: Assignment) -> None:
    """评分输入与规则（题目结构/分值/测试/环境/Rubric/AI 配置）不可变守卫：

    - published 状态禁止修改；
    - 即使取消发布，只要存在任何学生提交，仍禁止修改；
    - 标题、描述、截止时间等非评分元数据不受此守卫限制。
    """
    if assignment.status == "published":
        raise api_error(
            409,
            "ASSIGNMENT_NOT_EDITABLE",
            "已发布作业的评分配置不可修改，请先取消发布",
        )
    if _assignment_has_submissions(db, assignment.id):
        raise api_error(
            409,
            "ASSIGNMENT_HAS_SUBMISSIONS",
            "该作业已有学生提交，评分输入与规则不可修改",
        )


def _assignment_with_summary(db: Session, assignment: Assignment) -> AssignmentRead:
    """读响应附加学生可见环境摘要（Phase 5）——不含 digest/tag/构建日志。"""
    data = AssignmentRead.model_validate(assignment)
    if assignment.environment_version_id is not None:
        from app.services.environment_service import (
            public_environment_summary,
            resolve_effective_policy,
        )
        data.environment_summary = public_environment_summary(
            db, assignment.environment_version_id,
            resolve_effective_policy(assignment, assignment),
        )
    return data


@router.get("", response_model=PaginatedResponse)
def list_assignments(
    course_id: int | None = None,
    pagination: PaginationParams = Depends(pagination),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page, page_size = pagination.page, pagination.page_size
    query = select(Assignment)
    count_query = select(func.count()).select_from(Assignment)
    if course_id:
        query = query.where(Assignment.course_id == course_id)
        count_query = count_query.where(Assignment.course_id == course_id)
    if current_user.role == "student":
        # 学生只看到 published course + 自己属于发布范围的作业
        query = (
            query.join(Course, Assignment.course_id == Course.id)
            .where(Assignment.status == "published")
            .where(Course.status == "published")
            .where(assignment_visible_condition(current_user.id))
        )
        count_query = (
            count_query.join(Course, Assignment.course_id == Course.id)
            .where(Assignment.status == "published")
            .where(Course.status == "published")
            .where(assignment_visible_condition(current_user.id))
        )
    elif current_user.role == "teacher":
        query = query.join(Course, Assignment.course_id == Course.id).where(or_(teacher_task_condition(Assignment, current_user.id), and_(Assignment.offering_id.is_(None), Course.teacher_id == current_user.id)))
        count_query = count_query.join(Course, Assignment.course_id == Course.id).where(or_(teacher_task_condition(Assignment, current_user.id), and_(Assignment.offering_id.is_(None), Course.teacher_id == current_user.id)))
    elif current_user.role != "admin":
        # Any unsupported role: empty
        query = query.where(Assignment.id == -1)
        count_query = count_query.where(Assignment.id == -1)
    total = db.scalar(count_query) or 0
    assignments = db.scalars(query.order_by(Assignment.id).offset((page - 1) * page_size).limit(page_size)).all()
    populate_audience_cache(db, task_type="assignment", tasks=list(assignments))
    # 仅学生视角计算提交状态，供任务中心区分已交/待办（教师/管理员保持默认 False）
    submitted_map = _submitted_map(db, assignments, current_user.id) if current_user.role == "student" else {}
    items = []
    for item in assignments:
        data = AssignmentRead.model_validate(item).model_dump()
        if current_user.role == "student":
            # 零题作业永远未提交（无题可答），进入任务中心「未完成」逻辑
            data["is_submitted"] = submitted_map.get(item.id, False)
        items.append(data)
    return PaginatedResponse(items=items, page=page, page_size=page_size, total=total)


@router.post("", response_model=AssignmentRead, status_code=status.HTTP_201_CREATED)
def create_assignment(
    payload: AssignmentCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
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
    data = payload.model_dump()
    audience_mode = data.pop("audience_mode")
    audience_class_ids = data.pop("audience_class_ids")
    whitelist_student_ids = data.pop("whitelist_student_ids")
    excluded_student_ids = data.pop("excluded_student_ids")
    # Phase 4：教师显式选择的环境必须 available；省略时服务层解析 basic 当前可用版本
    from app.services.environment_service import (
        resolve_basic_available_version,
        validate_environment_selection,
    )

    validate_environment_selection(db, data["environment_version_id"], settings=settings)
    if data["environment_version_id"] is None:
        basic = resolve_basic_available_version(db, settings=settings)
        data["environment_version_id"] = basic.id if basic else None
    assignment = Assignment(**data, created_by_id=current_user.id)
    db.add(assignment)
    db.flush()
    save_audience(
        db, task_type="assignment", task_id=assignment.id, course=course,
        audience_mode=audience_mode, audience_class_ids=audience_class_ids,
        whitelist_student_ids=whitelist_student_ids, excluded_student_ids=excluded_student_ids,
        actor_id=current_user.id,
    )
    db.commit()
    db.refresh(assignment)
    return assignment


@router.get("/{assignment_id}", response_model=AssignmentRead)
def get_assignment(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    assignment = require_assignment(assignment_id, db)
    course = db.get(Course, assignment.course_id)
    if current_user.role == "student":
        require_student_assignment_access(db, assignment, current_user)
    elif not course or not can_access_course_content(course, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限查看该作业")
    return _assignment_with_summary(db, assignment)


@router.get("/{assignment_id}/my-progress", response_model=AssignmentProgressRead)
def my_progress(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
):
    assignment = require_assignment(assignment_id, db)
    require_student_assignment_access(db, assignment, current_user)
    progress = (select(
        Submission.question_id,
        func.max(Submission.id).label("latest_submission_id"),
        func.max(case((Submission.status.in_(("accepted", "graded")), 1), else_=0)).label("completed"),
    ).join(JudgeQuestion, Submission.question_id == JudgeQuestion.id)
        .where(JudgeQuestion.assignment_id == assignment_id, Submission.student_id == current_user.id)
        .group_by(Submission.question_id).subquery())
    rows = db.execute(select(JudgeQuestion.id, progress.c.latest_submission_id,
                             Submission.status, progress.c.completed)
                      .outerjoin(progress, progress.c.question_id == JudgeQuestion.id)
                      .outerjoin(Submission, Submission.id == progress.c.latest_submission_id)
                      .where(JudgeQuestion.assignment_id == assignment_id).order_by(JudgeQuestion.id))
    return {"items": [{"question_id": qid, "latest_submission_id": sid,
                       "status": state, "completed": bool(completed)}
                      for qid, sid, state, completed in rows]}


@router.patch("/{assignment_id}", response_model=AssignmentRead)
def update_assignment(
    assignment_id: int,
    payload: AssignmentUpdate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    assignment = require_assignment(assignment_id, db)
    ensure_assignment_manager(assignment, current_user, db)
    updates = payload.model_dump(exclude_unset=True)
    audience_updates = {key: updates.pop(key) for key in (
        "audience_mode", "audience_class_ids", "whitelist_student_ids", "excluded_student_ids",
    ) if key in updates}
    # Phase 4 门禁：已发布作业的环境字段不可直接改，必须先回到 draft（发布后绑定不可变）
    from app.services.environment_service import ENV_FIELDS, validate_environment_selection

    env_changes = [k for k in ENV_FIELDS if k in updates]
    if env_changes:
        # 环境设置属于评分输入：发布后或已有提交后不可修改
        ensure_scoring_editable(db, assignment)
    if updates.get("environment_version_id") is not None:
        validate_environment_selection(db, updates["environment_version_id"], settings=settings)
    for key, value in updates.items():
        setattr(assignment, key, value)
    if audience_updates:
        previous_mode = assignment.audience_mode
        has_submissions = db.scalar(
            select(Submission.id)
            .join(JudgeQuestion, Submission.question_id == JudgeQuestion.id)
            .where(JudgeQuestion.assignment_id == assignment.id)
            .limit(1)
        ) is not None
        if assignment.status == "published" and has_submissions:
            if audience_updates.get("audience_mode", previous_mode) != previous_mode:
                raise api_error(409, "ASSIGNMENT_AUDIENCE_MODE_LOCKED", "已有学生提交，不能切换发布基础范围")
            from app.services.audience_service import effective_student_ids
            current_ids = effective_student_ids(
                db, task_type="assignment", task_id=assignment.id,
                course=assignment.course if assignment.course else db.get(Course, assignment.course_id),
            )
        current = {
            "audience_mode": previous_mode,
            "audience_class_ids": assignment.audience_class_ids,
            "whitelist_student_ids": assignment.whitelist_student_ids,
            "excluded_student_ids": assignment.excluded_student_ids,
        }
        current.update(audience_updates)
        save_audience(
            db, task_type="assignment", task_id=assignment.id,
            course=assignment.course if assignment.course else db.get(Course, assignment.course_id),
            actor_id=current_user.id, **current,
        )
        if assignment.status == "published" and has_submissions:
            started_ids = set(db.scalars(
                select(Submission.student_id)
                .join(JudgeQuestion, Submission.question_id == JudgeQuestion.id)
                .where(JudgeQuestion.assignment_id == assignment.id)
            ).all())
            from app.services.audience_service import effective_student_ids as _effective_ids
            new_ids = _effective_ids(
                db, task_type="assignment", task_id=assignment.id,
                course=assignment.course if assignment.course else db.get(Course, assignment.course_id),
            )
            if (current_ids - new_ids) & started_ids:
                raise api_error(409, "ASSIGNMENT_AUDIENCE_STUDENT_SUBMITTED", "不能移除已经提交作业的学生")
    db.commit()
    db.refresh(assignment)
    return assignment


@router.post("/{assignment_id}/audience/import", response_model=CourseStudentImportResult)
async def import_assignment_audience(
    assignment_id: int,
    kind: str = "include",
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CSV 导入作业范围白名单（include）或排除名单（exclude）。"""
    if kind not in ("include", "exclude"):
        raise api_error(422, "INVALID_AUDIENCE_KIND", "kind 必须为 include 或 exclude")
    assignment = require_assignment(assignment_id, db)
    ensure_assignment_manager(assignment, current_user, db)
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise api_error(422, "CSV_TOO_LARGE", "CSV 文件不能超过 2 MB")
    rows, error = parse_student_csv(content)
    if error:
        raise api_error(422, "CSV_INVALID", error)
    result = import_audience_students(db, task_type="assignment", task_id=assignment.id, kind=kind, rows=rows)
    return CourseStudentImportResult(
        created=result["created"], updated=result["updated"], skipped=result["skipped"],
        errors=[CourseStudentImportRow(**row) for row in result["errors"]],
    )


@router.post("/{assignment_id}/publish", response_model=AssignmentRead)
def publish_assignment(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    assignment = require_assignment(assignment_id, db)
    ensure_assignment_manager(assignment, current_user, db)

    # Phase 4 环境门禁：默认环境与题目覆盖环境必须 available、内存不得低于环境最低值
    from app.services.environment_service import validate_publish_gate

    all_questions = db.scalars(
        select(JudgeQuestion).where(JudgeQuestion.assignment_id == assignment_id)
    ).all()
    validate_publish_gate(db, assignment, all_questions)

    # 发布要求至少包含一道题（零题作业不得进入 published，也不会被视作已提交）
    if not all_questions:
        raise api_error(
            422,
            "ASSIGNMENT_HAS_NO_QUESTIONS",
            "作业必须至少包含一道题才能发布",
        )
    require_effective_audience(
        db, task_type="assignment", task_id=assignment.id,
        course=assignment.course if assignment.course else db.get(Course, assignment.course_id),
    )

    # AI 评分门禁：非 legacy 题目需要锁定 Rubric
    questions = [q for q in all_questions if q.grading_mode != "legacy"]

    if questions:
        if not settings.ai_ready:
            raise api_error(503, "AI_NOT_READY", "发布含 AI 评分的题目需要配置 DAI_AI_API_KEY")
        from app.services.ai_client import DeepSeekClient, AIServiceError
        from app.services.op_metrics import ai_metrics_sink
        from app.services.rubric_service import ensure_locked_rubrics_for_publish
        try:
            client = DeepSeekClient(settings, metrics_sink=ai_metrics_sink())
            ensure_locked_rubrics_for_publish(db, client, questions)
        except AIServiceError as exc:
            raise api_error(503, "AI_RUBRIC_UNAVAILABLE", f"Rubric 生成失败: {exc}")

    assignment.status = "published"
    if assignment.published_at is None:
        assignment.published_at = utc_now()
    db.commit()
    db.refresh(assignment)
    return assignment


@router.delete("/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assignment(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除草稿作业：事务内级联清理题目/提交/Rubric/AI 评分记录（外键 RESTRICT，顺序重要）"""
    assignment = require_assignment(assignment_id, db)
    ensure_assignment_manager(assignment, current_user, db)
    if assignment.status != "draft":
        raise api_error(409, "ASSIGNMENT_NOT_DRAFT", "仅草稿作业可删除")
    # 有提交检查（草稿理论无提交，防边界）：任一题目存在提交即拒绝
    has_submission = db.scalar(
        select(Submission.id)
        .join(JudgeQuestion, Submission.question_id == JudgeQuestion.id)
        .where(JudgeQuestion.assignment_id == assignment_id)
        .limit(1)
    )
    if has_submission:
        raise api_error(409, "ASSIGNMENT_HAS_SUBMISSIONS", "该作业已有学生提交记录，不可删除")

    question_ids = db.scalars(
        select(JudgeQuestion.id).where(JudgeQuestion.assignment_id == assignment_id)
    ).all()
    submission_ids = db.scalars(
        select(Submission.id).where(Submission.question_id.in_(question_ids))
    ).all() if question_ids else []
    # 级联清理：code_grades 外键引用 question_rubrics 与 submissions，必须先于二者删除
    if submission_ids:
        db.execute(delete(CodeGrade).where(CodeGrade.submission_id.in_(submission_ids)))
    if question_ids:
        db.execute(delete(QuestionRubric).where(QuestionRubric.judge_question_id.in_(question_ids)))
        db.execute(delete(Submission).where(Submission.question_id.in_(question_ids)))
        db.execute(delete(JudgeQuestion).where(JudgeQuestion.id.in_(question_ids)))
    db.delete(assignment)
    db.commit()
    return None


@router.post("/{assignment_id}/unpublish", response_model=AssignmentRead)
def unpublish_assignment(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取消发布：published → draft，学生端立即不可见；不解除已锁定 Rubric，再发布走现有门禁"""
    assignment = require_assignment(assignment_id, db)
    ensure_assignment_manager(assignment, current_user, db)
    if assignment.status != "published":
        raise api_error(409, "ASSIGNMENT_NOT_PUBLISHED", "仅已发布的作业可取消发布")
    assignment.status = "draft"
    db.commit()
    db.refresh(assignment)
    return assignment


@router.get("/{assignment_id}/questions", response_model=PaginatedResponse)
def list_questions(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    assignment = require_assignment(assignment_id, db)
    course = db.get(Course, assignment.course_id)
    if current_user.role == 'student':
        require_student_assignment_access(db, assignment, current_user)
    elif not course or not can_access_course_content(course, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限查看题目")
    questions = db.scalars(select(JudgeQuestion).where(JudgeQuestion.assignment_id == assignment_id).order_by(JudgeQuestion.id)).all()
    items = []
    for question in questions:
        data = JudgeQuestionRead.model_validate(question)
        # Phase 5：题目 effective environment summary（覆盖时显示题目环境，否则作业默认）
        env_id = question.environment_version_id or assignment.environment_version_id
        if env_id is not None:
            from app.services.environment_service import (
                public_environment_summary,
                resolve_effective_policy,
            )
            data.environment_summary = public_environment_summary(
                db, env_id, resolve_effective_policy(assignment, question),
            )
        items.append(data)
    return PaginatedResponse(items=items, page=1, page_size=len(items) or 20, total=len(items))


@router.post("/{assignment_id}/questions", response_model=JudgeQuestionRead, status_code=status.HTTP_201_CREATED)
def create_question(
    assignment_id: int,
    payload: JudgeQuestionCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    assignment = require_assignment(assignment_id, db)
    ensure_assignment_manager(assignment, current_user, db)
    ensure_scoring_editable(db, assignment)
    data = payload.model_dump(exclude_unset=True)
    # JSON null 与未提供字段语义一致：新建编程题默认进入 active。
    if data.get("grading_mode") is None:
        data["grading_mode"] = "active"
    # Phase 4：环境字段显式传参——未提供时显式置 None 表示继承作业默认
    # （模型层惰性默认仅服务旧 ORM 创建路径，教师 API 路径显式接管）
    data.setdefault("environment_version_id", None)
    from app.services.environment_service import validate_environment_selection

    validate_environment_selection(db, data.get("environment_version_id"), settings=settings)
    question = JudgeQuestion(assignment_id=assignment_id, **data)
    db.add(question)
    db.commit()
    db.refresh(question)
    return question


@router.patch("/{assignment_id}/questions/{question_id}", response_model=JudgeQuestionRead)
def update_question(
    assignment_id: int,
    question_id: int,
    payload: JudgeQuestionUpdate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    """更新作业编程题——要求教师所有权且评分配置处于可编辑状态"""
    assignment = require_assignment(assignment_id, db)
    ensure_assignment_manager(assignment, current_user, db)
    ensure_scoring_editable(db, assignment)
    question = db.get(JudgeQuestion, question_id)
    if not question or question.assignment_id != assignment_id:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在或不属于该作业")
    updates = payload.model_dump(exclude_unset=True)
    # Phase 4：题目覆盖环境必须 available；None 表示继承作业默认
    from app.services.environment_service import validate_environment_selection

    if updates.get("environment_version_id") is not None:
        validate_environment_selection(db, updates["environment_version_id"], settings=settings)
    for key, value in updates.items():
        setattr(question, key, value)
    db.commit()
    db.refresh(question)
    return question


@router.delete("/{assignment_id}/questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(
    assignment_id: int,
    question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除作业编程题（TASK-017）。

    仅无任何提交的 draft 作业允许删除（已发布/有提交 → 409，复用评分可编辑守卫）；
    同一事务清理题目从属 Rubric 数据，不级联删除提交/成绩（守卫保证本路径无提交记录）。
    """
    assignment = require_assignment(assignment_id, db)
    ensure_assignment_manager(assignment, current_user, db)
    ensure_scoring_editable(db, assignment)
    question = db.get(JudgeQuestion, question_id)
    if not question or question.assignment_id != assignment_id:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在或不属于该作业")
    db.execute(
        delete(QuestionRubric).where(QuestionRubric.judge_question_id == question_id)
    )
    db.delete(question)
    db.commit()
    return None
