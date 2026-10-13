"""自主练习管理员 API（批次 5）：题库 CRUD / 发布归档 / 复制作业题 / 全局设置 /
AI 预检 / 用量 / 练习异议处理。

全部端点 ``require_roles("admin")``：教师访问一律 **403**（不是 404），
与"题库归管理员、教师只有排行榜只读入口"的产品规则一致。
"""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.dependencies import PaginationParams, get_db, get_redis_client, pagination, require_roles
from app.errors import api_error
from app.models import (Assignment, AuditEvent, GradeAppeal, JudgeGradingVersion,
                        JudgeQuestion, User)
from app.models.practice import PracticeProblem, PracticeSubmission
from app.schemas.practice import (
    CopyFromAssignmentRequest, PracticeAppealHandle, PracticeCaseInput,
    PracticeProblemCreate, PracticeProblemUpdate, PracticeSettingsUpdate,
)
from app.services import appeal_service, practice_queue, practice_service
from app.services.grading_values import ValueEncodingError, encode_value

router = APIRouter(prefix="/admin/practice", tags=["自主练习管理"])


# ─────────────────────────────────────────────────────────────
# 用例编码：管理员填 args/expected（原始 JSON），存储用平台类型化编码
# ─────────────────────────────────────────────────────────────

def _encode_cases(items: list[PracticeCaseInput]) -> list[dict]:
    encoded: list[dict] = []
    for index, item in enumerate(items):
        case_id = item.id or f"c{index + 1}"
        try:
            payload = {
                "id": case_id,
                "input": encode_value(tuple(item.args)),
                "expected": encode_value(item.expected),
            }
        except ValueEncodingError as exc:
            raise api_error(422, "PRACTICE_CASE_INVALID",
                            f"用例 {case_id} 的值不受支持: {exc}") from exc
        if item.compare:
            payload["compare"] = item.compare
        encoded.append(payload)
    return encoded


def _problem_read(db: Session, problem: PracticeProblem, *, detail: bool = True) -> dict:
    payload = practice_service.problem_read(db, problem, detail=False)
    # 当前评分版本（发布/改题后指向最新版本；未发布为 None）——所有管理端点同形状
    version = (db.get(JudgeGradingVersion, problem.current_grading_version_id)
               if problem.current_grading_version_id else None)
    payload["grading_version_id"] = version.id if version is not None else None
    payload["grading_version"] = version.version if version is not None else None
    if detail:
        payload.update({
            "prompt": problem.prompt,
            "starter_code": problem.starter_code,
            "function_name": problem.function_name,
            "public_cases": list(problem.public_cases or []),
            "hidden_tests": list(problem.hidden_tests or []),
            "test_groups": list(problem.test_groups or []),
            "teacher_constraints": dict(problem.teacher_constraints or {}),
            "reference_solution": problem.reference_solution,
            "score_cap_rules": list(problem.score_cap_rules or []),
            "source_kind": problem.source_kind,
            "source_judge_question_id": problem.source_judge_question_id,
            "published_at": problem.published_at.isoformat() if problem.published_at else None,
            "archived_at": problem.archived_at.isoformat() if problem.archived_at else None,
            "updated_at": problem.updated_at.isoformat() if problem.updated_at else None,
        })
    return payload


def _require_problem(db: Session, problem_id: int) -> PracticeProblem:
    problem = db.get(PracticeProblem, problem_id)
    if problem is None:
        raise api_error(404, "NOT_FOUND", "练习题不存在")
    return problem


# ─────────────────────────────────────────────────────────────
# 题库
# ─────────────────────────────────────────────────────────────

@router.get("/problems")
def list_problems(
    status: str | None = Query(default=None),
    difficulty: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
    page: PaginationParams = Depends(pagination),
):
    query = select(PracticeProblem)
    count_q = select(func.count()).select_from(PracticeProblem)
    if status:
        query = query.where(PracticeProblem.status == status)
        count_q = count_q.where(PracticeProblem.status == status)
    if difficulty:
        query = query.where(PracticeProblem.difficulty == difficulty)
        count_q = count_q.where(PracticeProblem.difficulty == difficulty)
    if search:
        like = f"%{search.strip()}%"
        query = query.where(or_(PracticeProblem.title.ilike(like),
                                PracticeProblem.prompt.ilike(like)))
        count_q = count_q.where(or_(PracticeProblem.title.ilike(like),
                                    PracticeProblem.prompt.ilike(like)))
    total = db.scalar(count_q) or 0
    rows = db.scalars(
        query.order_by(PracticeProblem.id.desc())
        .offset((page.page - 1) * page.page_size).limit(page.page_size)
    ).all()
    summary = {"total": total, "draft": 0, "published": 0, "archived": 0}
    for state, count in db.execute(
        select(PracticeProblem.status, func.count()).group_by(PracticeProblem.status)
    ).all():
        summary[state] = int(count)
    return {
        "items": [_problem_read(db, row) for row in rows],
        "total": total, "page": page.page, "page_size": page.page_size,
        "summary": summary,
    }


@router.get("/problems/{problem_id}")
def get_problem(
    problem_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    """单题详情（前端编辑页深链接用）：形状与列表项/写操作响应完全一致。"""
    return _problem_read(db, _require_problem(db, problem_id))


@router.post("/problems", status_code=201)
def create_problem(    data: PracticeProblemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    problem = PracticeProblem(
        title=data.title,
        difficulty=data.difficulty,
        tags=list(data.tags),
        prompt=data.prompt,
        starter_code=data.starter_code,
        public_cases=_encode_cases(data.public_cases),
        hidden_tests=_encode_cases(data.hidden_tests),
        time_limit_ms=data.time_limit_ms,
        memory_limit_kb=data.memory_limit_kb,
        function_name=data.function_name,
        reference_solution=data.reference_solution,
        teacher_constraints=dict(data.teacher_constraints),
        grading_mode="deterministic",
        source_kind="admin_made",
        status="draft",
        created_by_id=current_user.id,
    )
    db.add(problem)
    db.flush()
    db.add(AuditEvent(
        actor_id=current_user.id, action="practice.problem_created",
        target_type="practice_problem", target_id=str(problem.id),
        reason="管理员新建练习题",
    ))
    db.commit()
    db.refresh(problem)
    return _problem_read(db, problem)


@router.patch("/problems/{problem_id}")
def update_problem(
    problem_id: int,
    data: PracticeProblemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    problem = _require_problem(db, problem_id)
    if problem.status == "archived":
        raise api_error(409, "PRACTICE_PROBLEM_ARCHIVED", "已归档题目不能修改，请先恢复为草稿")
    payload = data.model_dump(exclude_unset=True)
    for key in ("public_cases", "hidden_tests"):
        if key in payload and payload[key] is not None:
            problem.__setattr__(key, _encode_cases(
                [PracticeCaseInput.model_validate(item) for item in payload.pop(key)]))
    for key, value in payload.items():
        if value is not None:
            setattr(problem, key, value)
    db.add(AuditEvent(
        actor_id=current_user.id, action="practice.problem_updated",
        target_type="practice_problem", target_id=str(problem.id),
        reason="管理员修改练习题",
        changes={"status": problem.status},
    ))
    db.commit()
    db.refresh(problem)
    return _problem_read(db, problem)


@router.post("/problems/{problem_id}/publish")
def publish_problem(
    problem_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    problem = _require_problem(db, problem_id)
    # 不走「已发布就直接返回」的短路：改题后重新发布必须生成新评分版本；
    # 未改动时由 publish_problem 的内容寻址（source_digest 相同）复用原版本。
    version = practice_service.publish_problem(db, problem=problem, actor=current_user)
    db.add(AuditEvent(
        actor_id=current_user.id, action="practice.problem_published",
        target_type="practice_problem", target_id=str(problem.id),
        reason="管理员发布练习题",
        changes={"version_id": version.id, "version": version.version,
                 "grading_mode": problem.grading_mode},
    ))
    db.commit()
    return _problem_read(db, problem)


@router.post("/problems/{problem_id}/archive")
def archive_problem(
    problem_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    problem = _require_problem(db, problem_id)
    practice_service.archive_problem(db, problem=problem)
    db.add(AuditEvent(
        actor_id=current_user.id, action="practice.problem_archived",
        target_type="practice_problem", target_id=str(problem.id),
        reason="管理员归档练习题",
    ))
    db.commit()
    return _problem_read(db, problem)


@router.delete("/problems/{problem_id}")
def delete_problem(
    problem_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    problem = _require_problem(db, problem_id)
    submissions = db.scalar(
        select(func.count()).select_from(PracticeSubmission)
        .where(PracticeSubmission.problem_id == problem.id)
    ) or 0
    if submissions or problem.status != "draft":
        raise api_error(409, "PRACTICE_PROBLEM_IN_USE",
                        "只有草稿且没有提交记录的题目可以删除，其它情况请归档")
    db.add(AuditEvent(
        actor_id=current_user.id, action="practice.problem_deleted",
        target_type="practice_problem", target_id=str(problem.id),
        reason="管理员删除草稿练习题",
    ))
    db.delete(problem)
    db.commit()
    return {"ok": True, "id": problem_id}


@router.post("/problems/copy-from-assignment", status_code=201)
def copy_from_assignment(
    data: CopyFromAssignmentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    """从已有作业复制题目：集合级入口，一步生成新的草稿题。"""
    question = db.get(JudgeQuestion, data.question_id)
    if question is None or question.assignment_id != data.assignment_id:
        raise api_error(404, "NOT_FOUND", "作业题目不存在")
    assignment = db.get(Assignment, data.assignment_id)
    if assignment is None:
        raise api_error(404, "NOT_FOUND", "作业不存在")

    # 作业题目的 public_cases 用 args/expected 原始值：转成练习的类型化用例编码
    source_cases = question.public_cases or []
    converted = []
    for index, case in enumerate(source_cases):
        if not isinstance(case, dict):
            continue
        args = case.get("args")
        if args is None and "input" in case:
            args = case.get("input")
        converted.append(PracticeCaseInput(
            id=str(case.get("id") or f"c{index + 1}"),
            args=list(args or []),
            expected=case.get("expected"),
            compare=case.get("compare"),
        ))
    problem = PracticeProblem(
        title=data.title or question.title,
        difficulty="medium",
        tags=[],
        prompt=question.description or question.title,
        starter_code=question.starter_code,
        public_cases=_encode_cases(converted),
        # 作业题的 hidden_tests 是 pytest 源码，不能直接当练习用例：
        # 复制只带可见示例，隐藏用例必须由管理员补齐或走一键 AI 配置生成
        hidden_tests=[],
        test_groups=list(question.test_groups or []),
        time_limit_ms=int(question.time_limit_ms or 10000),
        memory_limit_kb=int((question.memory_limit_mb or 256) * 1024),
        function_name=question.function_name or "solution",
        teacher_constraints=dict(question.teacher_constraints or {}),
        reference_solution=question.reference_solution,
        score_cap_rules=list(question.score_cap_rules or []),
        grading_mode="deterministic",
        source_kind="from_assignment",
        source_judge_question_id=question.id,
        status="draft",
        created_by_id=current_user.id,
    )
    db.add(problem)
    db.flush()
    db.add(AuditEvent(
        actor_id=current_user.id, action="practice.problem_copied",
        target_type="practice_problem", target_id=str(problem.id),
        reason="管理员从作业复制练习题",
        changes={"assignment_id": data.assignment_id, "question_id": question.id},
    ))
    db.commit()
    db.refresh(problem)
    return _problem_read(db, problem)


# ─────────────────────────────────────────────────────────────
# 设置 / 预检 / 用量
# ─────────────────────────────────────────────────────────────

def _settings_read(row) -> dict:
    return {
        "grading_mode": row.grading_mode,
        "daily_submission_cap_per_student": int(row.daily_submission_cap_per_student or 0),
        "daily_ai_budget_tokens": int(row.daily_ai_budget_tokens or 0),
        "daily_ai_budget_amount": float(row.daily_ai_budget_amount or 0.0),
        "ai_amount_per_1k_tokens": float(row.ai_amount_per_1k_tokens or 0.0),
        "max_concurrent_practice_jobs": int(row.max_concurrent_practice_jobs or 1),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/settings")
def get_settings_endpoint(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    return _settings_read(practice_service.settings_row(db))


@router.patch("/settings")
def update_settings_endpoint(
    data: PracticeSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    """全局设置：切换到 AI 之前必须通过预检，未就绪时 409 并返回未准备题目清单。"""
    current = practice_service.settings_row(db)
    values = data.model_dump(exclude_unset=True)
    if values.get("grading_mode") == "ai" and current.grading_mode != "ai":
        report = practice_service.ai_preflight(db)
        if not report["ready"]:
            # 统一错误信封（main.py 的 _normalize_detail 只保留 code/message/fields），
            # 未就绪清单放 fields.problems，前端 detail.fields.problems 直接可用
            raise api_error(
                409, "AI_PREFLIGHT_FAILED",
                "有已发布题目的评分标准尚未准备完成，请补齐或下架后再开启 AI",
                fields={"problems": report["problems"]},
            )
    row = practice_service.update_settings(db, actor=current_user, values=values)
    db.add(AuditEvent(
        actor_id=current_user.id, action="practice.settings_updated",
        target_type="practice_settings", target_id="1",
        reason="管理员更新练习设置", changes=values,
    ))
    db.commit()
    return _settings_read(row)


@router.get("/ai-preflight")
def ai_preflight(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    report = practice_service.ai_preflight(db)
    settings_row = practice_service.settings_row(db)
    report["queue"] = {
        "queued": practice_queue.queued_count(db),
        "running": practice_queue.running_count(db),
        "max_concurrent": int(settings_row.max_concurrent_practice_jobs or 1),
    }
    report["ai_budget"] = {
        "tokens_limit": int(settings_row.daily_ai_budget_tokens or 0),
        "amount_limit": float(settings_row.daily_ai_budget_amount or 0.0),
    }
    return report


@router.get("/usage")
def usage(
    date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    on_date = date_cls.fromisoformat(date) if date else None
    return practice_service.usage_summary(db, on_date=on_date)


# ─────────────────────────────────────────────────────────────
# 练习异议（由管理员处理）
# ─────────────────────────────────────────────────────────────

def _appeal_read(db: Session, appeal: GradeAppeal) -> dict:
    problem_title = None
    if appeal.target_kind == "practice_submission":
        submission = db.get(PracticeSubmission, appeal.target_id)
        if submission is not None:
            problem = db.get(PracticeProblem, submission.problem_id)
            problem_title = problem.title if problem is not None else None
    student = db.get(User, appeal.student_id)
    return {
        "id": appeal.id, "target_kind": appeal.target_kind, "target_id": appeal.target_id,
        "problem_title": problem_title,
        "student_name": student.real_name if student else None,
        "student_no": student.student_no if student else None,
        "reason": appeal.reason, "status": appeal.status,
        "score_before": appeal.score_before, "score_after": appeal.score_after,
        "handler_comment": appeal.handler_comment, "student_reply": appeal.student_reply,
        "created_at": appeal.created_at.isoformat() if appeal.created_at else None,
        "resolved_at": appeal.resolved_at.isoformat() if appeal.resolved_at else None,
        "route": appeal.route,
    }


@router.get("/appeals")
def list_appeals(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
    page: PaginationParams = Depends(pagination),
):
    query = select(GradeAppeal).where(GradeAppeal.target_kind == "practice_submission")
    count_q = (select(func.count()).select_from(GradeAppeal)
               .where(GradeAppeal.target_kind == "practice_submission"))
    if status:
        query = query.where(GradeAppeal.status == status)
        count_q = count_q.where(GradeAppeal.status == status)
    total = db.scalar(count_q) or 0
    rows = db.scalars(
        query.order_by(GradeAppeal.created_at.desc(), GradeAppeal.id.desc())
        .offset((page.page - 1) * page.page_size).limit(page.page_size)
    ).all()
    return {
        "items": [_appeal_read(db, row) for row in rows],
        "total": total, "page": page.page, "page_size": page.page_size,
    }


@router.get("/submissions/{submission_id}")
def get_submission_detail(
    submission_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
    settings: Settings = Depends(get_settings),
):
    """管理员复核用：含源码与 AI 明细（教师无权访问，且不在教师端任何列表出现）。"""
    submission = db.get(PracticeSubmission, submission_id)
    if submission is None:
        raise api_error(404, "NOT_FOUND", "提交记录不存在")
    payload = practice_service.submission_read(db, submission, settings=settings)
    problem = db.get(PracticeProblem, submission.problem_id)
    payload["source"] = {
        "grading_mode": submission.grading_mode,
        "code_grade_id": submission.code_grade_id,
        "grading_version_id": submission.grading_version_id,
        "attempt_count": submission.attempt_count,
        "last_error": submission.last_error,
        "problem_id": submission.problem_id,
        "problem_title": problem.title if problem is not None else None,
    }
    grade = practice_service.completed_ai_grade(db, submission)
    if grade is not None:
        payload["code_grade"] = {
            "id": grade.id, "status": grade.status, "mode": grade.mode,
            "rubric_id": grade.rubric_id, "result_revision": grade.result_revision,
            "raw_total": grade.raw_total, "score_cap": grade.score_cap,
            "final_score_100": grade.final_score_100,
            "deterministic_details": grade.deterministic_details,
            "review_reason": grade.review_reason,
        }
    return payload


@router.post("/appeals/{appeal_id}/handle")
def handle_appeal(
    appeal_id: int,
    data: PracticeAppealHandle,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
    redis_client=Depends(get_redis_client),
):
    appeal = db.get(GradeAppeal, appeal_id)
    if appeal is None or appeal.target_kind != "practice_submission":
        raise api_error(404, "NOT_FOUND", "练习异议不存在")
    result = appeal_service.handle_appeal(
        db, redis_client, appeal=appeal, actor=current_user, action=data.action,
        comment=data.comment, score=data.score,
    )
    result["appeal"] = _appeal_read(db, db.get(GradeAppeal, appeal_id))
    return result
