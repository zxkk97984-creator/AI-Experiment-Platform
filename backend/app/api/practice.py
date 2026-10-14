"""自主练习 API（批次 5）：学生做题与自己提交，教师只读排行榜。

权限边界（必过验收）：

* 学生只能看自己的提交——``GET /practice/submissions/{id}`` 非本人一律 404
  （与"记录不存在"同形，不泄漏他人提交是否存在）；
* 教师只有排行榜只读入口，**拿不到学生源码与他人评分详情**；
* 练习域不纳入 ``services/teaching_query_policy`` 的教师行级过滤（该文件是显式模型
  白名单，练习模型不入名单即自动豁免），所以这里的行级隔离由本模块显式保证。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.dependencies import PaginationParams, get_db, get_redis_client, pagination, require_roles
from app.errors import api_error
from app.models import User
from app.models.practice import PracticeProblem, PracticeSubmission, PracticeUsageLog
from app.models.teaching import CourseOffering, OfferingEnrollment
from app.schemas.practice import PracticeSubmissionCreate
from app.services import practice_queue, practice_service

router = APIRouter(prefix="/practice", tags=["自主练习"])


def _published_problem(db: Session, problem_id: int) -> PracticeProblem:
    problem = db.get(PracticeProblem, problem_id)
    if problem is None or problem.status != "published":
        raise api_error(404, "NOT_FOUND", "练习题不存在")
    return problem


def _quota_meta(db: Session, *, student_id: int, settings: Settings,
                settings_row) -> dict:
    """额度/队列元信息：前端据此显示「今日额度已用完」「排队中」两态。"""
    today = datetime.now(timezone.utc).date()
    used = float(db.scalar(
        select(func.coalesce(func.sum(PracticeUsageLog.amount), 0.0))
        .where(PracticeUsageLog.date == today,
               PracticeUsageLog.metric == "submissions",
               PracticeUsageLog.scope == "student",
               PracticeUsageLog.student_id == student_id)
    ) or 0.0)
    cap = int(settings_row.daily_submission_cap_per_student or 0)
    active = db.scalars(
        select(PracticeSubmission.id).where(
            PracticeSubmission.student_id == student_id,
            PracticeSubmission.grading_status.in_(("queued", "running")),
        ).order_by(PracticeSubmission.id).limit(20)
    ).all()
    return {
        "quota_exhausted": bool(cap > 0 and used >= cap),
        "quota": {"metric": "submissions", "limit": cap, "used": used},
        "queue": {
            "queued": practice_queue.queued_count(db),
            "running": practice_queue.running_count(db),
            "max_concurrent": int(settings_row.max_concurrent_practice_jobs or 1),
        },
        "active_submissions": list(active),
        "ai_budget": {
            "tokens_used": float(db.scalar(
                select(func.coalesce(func.sum(PracticeUsageLog.amount), 0.0))
                .where(PracticeUsageLog.date == today,
                       PracticeUsageLog.metric == "ai_tokens",
                       PracticeUsageLog.scope == "platform")
            ) or 0.0),
            "tokens_limit": int(settings_row.daily_ai_budget_tokens or 0),
        },
    }


@router.get("/problems")
def list_problems(
    difficulty: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    completion: str | None = Query(default=None, pattern="^(all|unsolved|attempted|solved)$"),
    search: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
    page: PaginationParams = Depends(pagination),
    settings: Settings = Depends(get_settings),
):
    query = select(PracticeProblem).where(PracticeProblem.status == "published")
    count_q = select(func.count()).select_from(PracticeProblem).where(
        PracticeProblem.status == "published")
    if difficulty:
        if difficulty not in ("easy", "medium", "hard"):
            raise api_error(422, "INVALID_DIFFICULTY", "难度必须是 easy/medium/hard")
        query = query.where(PracticeProblem.difficulty == difficulty)
        count_q = count_q.where(PracticeProblem.difficulty == difficulty)
    if search:
        like = f"%{search.strip()}%"
        query = query.where(or_(PracticeProblem.title.ilike(like),
                                PracticeProblem.prompt.ilike(like)))
        count_q = count_q.where(or_(PracticeProblem.title.ilike(like),
                                    PracticeProblem.prompt.ilike(like)))

    rows = db.scalars(query.order_by(PracticeProblem.id)).all()
    items = []
    for problem in rows:
        if tag and tag not in (problem.tags or []):
            continue
        payload = practice_service.problem_read(db, problem, student_id=current_user.id)
        if completion and completion != "all" and payload["my_status"] != completion:
            continue
        items.append(payload)
    total = len(items)
    start = (page.page - 1) * page.page_size
    items = items[start:start + page.page_size]
    settings_row = practice_service.settings_row(db)
    return {
        "items": items, "total": total, "page": page.page, "page_size": page.page_size,
        "meta": _quota_meta(db, student_id=current_user.id, settings=settings,
                            settings_row=settings_row),
    }


@router.get("/problems/{problem_id}")
def get_problem(
    problem_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student", "admin")),
):
    problem = _published_problem(db, problem_id)
    payload = practice_service.problem_read(
        db, problem,
        student_id=current_user.id if current_user.role == "student" else None,
        detail=True,
    )
    if current_user.role == "student":
        settings_row = practice_service.settings_row(db)
        payload["effective_grading_mode"] = practice_service.effective_grading_mode(
            problem, settings_row)
    return payload


@router.post("/problems/{problem_id}/submissions", status_code=202)
def submit_solution(
    problem_id: int,
    data: PracticeSubmissionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
    settings: Settings = Depends(get_settings),
    redis_client=Depends(get_redis_client),
):
    problem = _published_problem(db, problem_id)
    submission, created = practice_service.create_submission(
        db, redis_client, student=current_user, problem=problem, code=data.code,
        client_request_id=data.client_request_id,
        token_estimate=settings.practice_ai_token_estimate,
    )
    if created:
        practice_queue.push_submission(redis_client, submission.id)
    return practice_service.submission_read(db, submission, settings=settings)


@router.get("/submissions/{submission_id}")
def get_submission(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student", "admin")),
    settings: Settings = Depends(get_settings),
):
    submission = db.get(PracticeSubmission, submission_id)
    if submission is None:
        raise api_error(404, "NOT_FOUND", "提交记录不存在")
    if current_user.role != "admin" and submission.student_id != current_user.id:
        # 与"不存在"同形：不泄漏他人提交是否存在
        raise api_error(404, "NOT_FOUND", "提交记录不存在")
    return practice_service.submission_read(db, submission, settings=settings)


@router.get("/me")
def my_practice(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
    page: PaginationParams = Depends(pagination),
    settings: Settings = Depends(get_settings),
):
    settings_row = practice_service.settings_row(db)
    summary = practice_service.student_summary(db, student_id=current_user.id,
                                               settings=settings_row)
    query = select(PracticeSubmission).where(
        PracticeSubmission.student_id == current_user.id)
    total = db.scalar(
        select(func.count()).select_from(PracticeSubmission)
        .where(PracticeSubmission.student_id == current_user.id)
    ) or 0
    rows = db.scalars(
        query.order_by(PracticeSubmission.id.desc())
        .offset((page.page - 1) * page.page_size).limit(page.page_size)
    ).all()
    items = []
    for submission in rows:
        problem = db.get(PracticeProblem, submission.problem_id)
        items.append({
            "id": submission.id,
            "problem_id": submission.problem_id,
            "problem_title": problem.title if problem is not None else None,
            "difficulty": problem.difficulty if problem is not None else None,
            "score": submission.score,
            "deterministic_score": submission.deterministic_score,
            "max_score": submission.max_score,
            "grading_status": submission.grading_status,
            "grading_mode": submission.grading_mode,
            "created_at": (submission.created_at.isoformat()
                           if submission.created_at else None),
        })
    return {
        "summary": summary, "items": items, "total": total,
        "page": page.page, "page_size": page.page_size,
        "meta": _quota_meta(db, student_id=current_user.id, settings=settings,
                            settings_row=settings_row),
    }


def _offering_scope(db: Session, *, scope_kind: str, scope_id: int | None,
                    viewer: User) -> tuple[set[int] | None, int]:
    """榜单作用域：返回 (学生 id 集合, scope_id)；None 表示不限（全平台榜）。"""
    if scope_kind == "platform":
        return None, 0
    if scope_id is None:
        raise api_error(422, "PRACTICE_SCOPE_REQUIRED", "班内榜必须提供 scope_id")
    offering = db.get(CourseOffering, scope_id)
    if offering is None:
        raise api_error(404, "NOT_FOUND", "教学班不存在")
    if viewer.role == "teacher":
        if offering.teacher_id != viewer.id:
            raise api_error(403, "FORBIDDEN", "只能查看本人任课班级的排行榜")
    elif viewer.role == "student":
        enrolled = db.scalar(
            select(OfferingEnrollment.id).where(
                OfferingEnrollment.offering_id == scope_id,
                OfferingEnrollment.student_id == viewer.id,
                OfferingEnrollment.status == "active",
            )
        )
        if enrolled is None:
            raise api_error(403, "FORBIDDEN", "只能查看已加入教学班的排行榜")
    student_ids = set(db.scalars(
        select(OfferingEnrollment.student_id).where(
            OfferingEnrollment.offering_id == scope_id,
            OfferingEnrollment.status == "active",
        )
    ).all())
    return student_ids, scope_id


@router.get("/leaderboard")
def leaderboard(
    scope: str = Query(default="platform", pattern="^(platform|offering)$"),
    scope_id: int | None = Query(default=None),
    board: str = Query(default="deterministic", pattern="^(deterministic|ai)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student", "teacher", "admin")),
    settings: Settings = Depends(get_settings),
):
    """Legacy response shape backed by the canonical privacy and ranking query."""
    _student_ids, resolved_scope = _offering_scope(
        db, scope_kind=scope, scope_id=scope_id, viewer=current_user)
    from app.services import practice_leaderboard

    canonical = practice_leaderboard.board(
        db, board_kind=board, scope_kind=scope, scope_id=resolved_scope,
        viewer=current_user, settings=settings, view="top",
    )
    return {
        "items": canonical["items"],
        "total": canonical["total"],
        "board": board,
        "scope": scope,
        "generated_at": canonical["generated_at"],
    }
