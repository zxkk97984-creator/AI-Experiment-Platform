"""单次成绩异议服务（批次 2）。

范围：作业（submission）、考试编程题（exam_answer）、实验（lab_submission）；
自主练习（practice_submission）由批次 5 的练习域接管，本批只保留目标类型。

不变量：

* 加锁顺序复用 ``api/ai_grading.py`` 的 target → CodeGrade → appeal，避免死锁；
* 改分**只走**既有 override 路径（三类 409 + 清 claim_token + 递增 CodeGrade.result_revision
  + 写 GradeOverride），绝不直接赋值分数；
* 重评走既有 retry 机制（重置为 pending → 入队），appeal 置 ``regrading``，
  重评完成前不关闭处理流程；
* 受理/维持/改分/重评/拒绝全部写 ``AuditEvent``；
* 同一 (target, result_revision) 只允许一条非撤回申请，撤回后可再申请。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from redis import Redis
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.errors import api_error
from app.models import (
    Assignment,
    AuditEvent,
    CodeGrade,
    Course,
    CourseOffering,
    Exam,
    ExamAnswer,
    ExamQuestion,
    ExamSubmission,
    ExperimentRecord,
    ExperimentSubmission,
    GradeAppeal,
    JudgeQuestion,
    LabGradeRevision,
    LabSubmission,
    Submission,
    User,
)
from app.services.offering_service import teacher_course_condition

logger = logging.getLogger("dai.appeal")

APPEAL_TARGET_KINDS = ("submission", "exam_answer", "lab_submission", "practice_submission")
APPEAL_OPEN_STATUSES = ("pending", "regrading")
APPEAL_TERMINAL_STATUSES = ("upheld", "adjusted", "rejected")
APPEAL_DAILY_LIMIT = 5
APPEAL_MIN_REASON = 20
APPEAL_MAX_REASON = 2000


@dataclass(frozen=True)
class AppealTarget:
    """被申请结果的解析结果：分数基线、结果修订号、可见时间与展示信息。"""

    target_kind: str
    target_id: int
    title: str
    route: str | None
    score_before: float | None
    result_revision: int
    visible_at: datetime
    code_grade: CodeGrade | None
    scorable: bool


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def appeal_dedupe_key(target_kind: str, target_id: int, revision: int) -> str:
    return f"{target_kind}:{target_id}:{revision}"


def resolve_appeal_target(
    db: Session, *, target_kind: str, target_id: int, student_id: int,
) -> AppealTarget:
    """解析目标并校验归属——fail-closed：解析不到或不属于该学生一律 404。"""
    if target_kind not in APPEAL_TARGET_KINDS:
        raise api_error(400, "INVALID_TARGET_KIND", "不支持的成绩异议目标类型")

    if target_kind == "submission":
        submission = db.get(Submission, target_id)
        if submission is None or submission.student_id != student_id:
            raise api_error(404, "APPEAL_TARGET_NOT_FOUND", "成绩记录不存在")
        question = db.get(JudgeQuestion, submission.question_id)
        assignment = db.get(Assignment, question.assignment_id) if question else None
        grade = db.scalar(select(CodeGrade).where(CodeGrade.submission_id == submission.id))
        visible = (grade.finished_at if grade and grade.finished_at else None) \
            or submission.finished_at or submission.created_at
        return AppealTarget(
            target_kind=target_kind, target_id=submission.id,
            title=(question.title if question else "作业提交"),
            route=f"/student/submissions/{submission.id}",
            score_before=grade.final_score_100 if grade is not None else submission.score,
            result_revision=(grade.result_revision or 0) if grade is not None else 0,
            visible_at=visible, code_grade=grade,
            scorable=bool(grade is not None and grade.mode == "active"),
        )

    if target_kind == "exam_answer":
        answer = db.get(ExamAnswer, target_id)
        parent = db.get(ExamSubmission, answer.submission_id) if answer is not None else None
        if answer is None or parent is None or parent.student_id != student_id:
            raise api_error(404, "APPEAL_TARGET_NOT_FOUND", "成绩记录不存在")
        question = db.get(ExamQuestion, answer.question_id)
        grade = db.scalar(select(CodeGrade).where(CodeGrade.exam_answer_id == answer.id))
        visible = (grade.finished_at if grade and grade.finished_at else None) \
            or answer.finished_at or answer.created_at
        return AppealTarget(
            target_kind=target_kind, target_id=answer.id,
            title=(question.prompt if question else "考试编程题"),
            # 直接用题目上的 exam_id 拼路由：学生请求作用域策略会过滤 Exam 读取，
            # 依赖 db.get(Exam) 会让路由在学生侧静默变成 None。
            route=f"/student/exams/{question.exam_id}" if question else None,
            score_before=grade.scaled_score if grade is not None else answer.score,
            result_revision=(grade.result_revision or 0) if grade is not None else (answer.result_revision or 0),
            visible_at=visible, code_grade=grade,
            scorable=bool(grade is not None and grade.mode == "active"),
        )

    if target_kind == "lab_submission":
        state = db.get(LabSubmission, target_id)
        submission = db.get(ExperimentSubmission, target_id)
        if state is None or submission is None or submission.student_id != student_id:
            raise api_error(404, "APPEAL_TARGET_NOT_FOUND", "成绩记录不存在")
        grade = db.get(LabGradeRevision, state.published_grade_id) if state.published_grade_id else None
        return AppealTarget(
            target_kind=target_kind, target_id=state.submission_id,
            title="实验提交",
            route="/student/experiments",
            score_before=grade.total if grade is not None else None,
            result_revision=state.published_grade_id or 0,
            visible_at=(_as_utc(grade.published_at) if grade is not None else None)
            or submission.submitted_at or submission.created_at,
            code_grade=None,
            scorable=grade is not None,
        )

    # 自主练习：批次 5 接管（本轮先按不可申请处理，绝不静默接受）
    raise api_error(409, "APPEAL_TARGET_UNSUPPORTED", "该成绩类型暂不支持异议申请")


def check_daily_appeal_limit(redis_client: Redis, student_id: int) -> None:
    """每生每天最多 5 次异议：复用 reauth 的限流键模式；Redis 故障 fail-closed。"""
    key = f"appeal:daily:{student_id}:{datetime.now(timezone.utc).date().isoformat()}"
    try:
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, 48 * 3600)
    except Exception as exc:
        logger.error("appeal_rate_limit_unavailable", extra={"student_id": student_id})
        raise api_error(503, "APPEAL_RATE_LIMIT_UNAVAILABLE", "限流服务不可用，请稍后再试") from exc
    if count > APPEAL_DAILY_LIMIT:
        raise api_error(429, "APPEAL_DAILY_LIMITED",
                        f"每天最多提交 {APPEAL_DAILY_LIMIT} 次成绩异议，请明天再试")


def create_appeal(
    db: Session, redis_client: Redis, *, student: User, target_kind: str, target_id: int,
    reason: str, window_days: int,
) -> GradeAppeal:
    reason = (reason or "").strip()
    if len(reason) < APPEAL_MIN_REASON or len(reason) > APPEAL_MAX_REASON:
        raise api_error(422, "APPEAL_REASON_INVALID",
                        f"异议说明需 {APPEAL_MIN_REASON}-{APPEAL_MAX_REASON} 字")
    target = resolve_appeal_target(db, target_kind=target_kind, target_id=target_id,
                                   student_id=student.id)
    if not target.scorable:
        raise api_error(409, "APPEAL_NOT_SCORED", "该结果尚未产生正式 AI 成绩，不能申请异议")
    if target.code_grade is not None and target.code_grade.status in ("pending", "queued", "running"):
        raise api_error(409, "GRADING_IN_PROGRESS", "评分仍在进行，请等成绩公布后再申请")
    if target.code_grade is not None and target.code_grade.needs_teacher_review:
        raise api_error(409, "APPEAL_NOT_SCORED", "该结果已转人工复核，请等待教师处理")

    now = datetime.now(timezone.utc)
    visible_at = _as_utc(target.visible_at) or now
    deadline = visible_at + timedelta(days=window_days)
    if now > deadline:
        raise api_error(403, "APPEAL_WINDOW_CLOSED",
                        f"异议申请窗口已关闭（成绩公布后 {window_days} 天内可申请）")

    dedupe_key = appeal_dedupe_key(target_kind, target_id, target.result_revision)
    existing = db.scalar(select(GradeAppeal).where(GradeAppeal.dedupe_key == dedupe_key))
    if existing is not None:
        raise api_error(409, "APPEAL_DUPLICATE", "该次结果已提交过异议，请等待处理",
                        fields={"existing_appeal_id": existing.id})

    check_daily_appeal_limit(redis_client, student.id)

    appeal = GradeAppeal(
        target_kind=target_kind, target_id=target_id, student_id=student.id,
        code_grade_id=target.code_grade.id if target.code_grade is not None else None,
        result_revision_snapshot=target.result_revision,
        score_before=target.score_before,
        reason=reason,
        status="pending",
        route=target.route,
        visible_at=visible_at,
        dedupe_key=dedupe_key,
    )
    db.add(appeal)
    db.flush()
    db.add(AuditEvent(
        actor_id=student.id, action="appeal.created",
        target_type="grade_appeal", target_id=str(appeal.id),
        reason=f"学生申请成绩异议（{target_kind}#{target_id}）",
        changes={"target_kind": target_kind, "target_id": target_id,
                 "result_revision": target.result_revision, "score_before": target.score_before},
    ))
    db.commit()
    db.refresh(appeal)
    return appeal


def withdraw_appeal(db: Session, *, appeal: GradeAppeal, student: User) -> GradeAppeal:
    if appeal.student_id != student.id:
        raise api_error(403, "FORBIDDEN", "只能撤回自己的异议申请")
    if appeal.status not in APPEAL_OPEN_STATUSES:
        raise api_error(409, "APPEAL_NOT_OPEN", "该异议已处理完成，不能撤回")
    appeal.status = "withdrawn"
    appeal.withdrawn_at = datetime.now(timezone.utc)
    # 清空唯一键：撤回后允许对同一结果版本重新申请
    appeal.dedupe_key = None
    db.add(AuditEvent(
        actor_id=student.id, action="appeal.withdrawn", target_type="grade_appeal",
        target_id=str(appeal.id), reason="学生撤回成绩异议",
    ))
    db.commit()
    db.refresh(appeal)
    return appeal


def reply_appeal(db: Session, *, appeal: GradeAppeal, student: User, message: str) -> GradeAppeal:
    if appeal.student_id != student.id:
        raise api_error(403, "FORBIDDEN", "只能回复自己的异议申请")
    if appeal.status not in APPEAL_OPEN_STATUSES:
        raise api_error(409, "APPEAL_NOT_OPEN", "该异议已处理完成，不能补充说明")
    message = (message or "").strip()
    if not message or len(message) > APPEAL_MAX_REASON:
        raise api_error(422, "APPEAL_REPLY_INVALID", f"补充说明需 1-{APPEAL_MAX_REASON} 字")
    appeal.student_reply = message
    db.add(AuditEvent(
        actor_id=student.id, action="appeal.replied", target_type="grade_appeal",
        target_id=str(appeal.id), reason="学生补充异议说明",
    ))
    db.commit()
    db.refresh(appeal)
    return appeal


# ── 作用域与列表 ────────────────────────────────────────────────


def appeal_scope_condition(db: Session, user: User):
    """教师可见范围：本人在授课程/授课班的目标；管理员不过滤；练习域教师不可见。"""
    if user.role == "admin":
        return None
    course_ids = select(Course.id).where(teacher_course_condition(user.id))
    offering_ids = select(CourseOffering.id).where(
        CourseOffering.teacher_id == user.id,
        CourseOffering.status.in_(("active", "archived")),
    )
    submission_scope = GradeAppeal.target_id.in_(
        select(Submission.id)
        .join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
        .join(Assignment, Assignment.id == JudgeQuestion.assignment_id)
        .where(Assignment.course_id.in_(course_ids))
    )
    exam_scope = GradeAppeal.target_id.in_(
        select(ExamAnswer.id)
        .join(ExamQuestion, ExamQuestion.id == ExamAnswer.question_id)
        .join(Exam, Exam.id == ExamQuestion.exam_id)
        .where(Exam.course_id.in_(course_ids))
    )
    lab_scope = GradeAppeal.target_id.in_(
        select(LabSubmission.submission_id)
        .join(ExperimentSubmission, ExperimentSubmission.id == LabSubmission.submission_id)
        .join(ExperimentRecord, ExperimentRecord.id == ExperimentSubmission.record_id)
        .where(ExperimentRecord.offering_id.in_(offering_ids))
    )
    return or_(
        and_(GradeAppeal.target_kind == "submission", submission_scope),
        and_(GradeAppeal.target_kind == "exam_answer", exam_scope),
        and_(GradeAppeal.target_kind == "lab_submission", lab_scope),
    )


def require_appeal_permission(db: Session, appeal: GradeAppeal, actor: User) -> None:
    condition = appeal_scope_condition(db, actor)
    if condition is None:
        return
    visible = db.scalar(select(GradeAppeal.id).where(GradeAppeal.id == appeal.id).where(condition))
    if visible is None:
        raise api_error(403, "FORBIDDEN", "无权处理该成绩异议")


def _default_handler(actor: User) -> str:
    return "教师" if actor.role == "teacher" else "管理员"


def handle_appeal(
    db: Session, redis_client: Redis, *, appeal: GradeAppeal, actor: User, action: str,
    comment: str = "", score: float | None = None, functional_score: float | None = None,
    robustness_score: float | None = None, algorithm_score: float | None = None,
    quality_score: float | None = None, final_score_100: float | None = None,
) -> dict:
    """处理异议：维持 / 改分（走既有 override 通道）/ 重评（走既有 retry）/ 拒绝。"""
    require_appeal_permission(db, appeal, actor)
    if appeal.status not in APPEAL_OPEN_STATUSES:
        raise api_error(409, "APPEAL_CLOSED", "该异议已处理完成")
    comment = (comment or "").strip()

    if action in ("uphold", "reject"):
        if not comment:
            raise api_error(422, "APPEAL_COMMENT_REQUIRED", "维持或拒绝必须填写处理说明")
        appeal.status = "upheld" if action == "uphold" else "rejected"
        appeal.score_after = appeal.score_before
        appeal.handler_comment = comment
        appeal.resolved_by_id = actor.id
        appeal.resolved_at = datetime.now(timezone.utc)
        db.add(AuditEvent(
            actor_id=actor.id, action=f"appeal.{appeal.status}", target_type="grade_appeal",
            target_id=str(appeal.id), reason=comment,
            changes={"score_before": appeal.score_before, "score_after": appeal.score_after},
        ))
        db.commit()
        db.refresh(appeal)
        return _handle_result(appeal)

    if action == "adjust":
        return _adjust_appeal(
            db, appeal, actor, comment=comment, score=score,
            functional_score=functional_score, robustness_score=robustness_score,
            algorithm_score=algorithm_score, quality_score=quality_score,
            final_score_100=final_score_100,
        )

    if action == "regrade":
        return _regrade_appeal(db, redis_client, appeal, actor, comment)

    raise api_error(400, "INVALID_ACTION", "action 必须是 uphold/adjust/regrade/reject")


def _handle_result(appeal: GradeAppeal) -> dict:
    return {
        "ok": True, "appeal_id": appeal.id, "status": appeal.status,
        "score_before": appeal.score_before, "score_after": appeal.score_after,
    }


def _adjust_appeal(
    db: Session, appeal: GradeAppeal, actor: User, *, comment: str,
    score: float | None, functional_score: float | None, robustness_score: float | None,
    algorithm_score: float | None, quality_score: float | None, final_score_100: float | None,
) -> dict:
    """改分：作业/考试编程题走既有 override 路径；实验走既有 lab manual_review 通道。"""
    if not comment:
        raise api_error(422, "APPEAL_COMMENT_REQUIRED", "改分必须填写处理说明")

    if appeal.target_kind in ("submission", "exam_answer"):
        from app.api.ai_grading import _apply_grade_override
        from app.schemas.ai_grading import GradeOverrideCreate

        grade = db.get(CodeGrade, appeal.code_grade_id) if appeal.code_grade_id else None
        if grade is None:
            raise api_error(409, "APPEAL_TARGET_MISSING", "评分记录不存在，无法改分")
        payload = GradeOverrideCreate(
            algorithm_score=algorithm_score, quality_score=quality_score,
            functional_score=functional_score, robustness_score=robustness_score,
            final_score_100=final_score_100 if final_score_100 is not None else score,
            reason=comment,
        )
        # 既有 override 路径自带：target → CodeGrade 锁序 + 三类 409 + 清 claim_token
        # + 递增 CodeGrade.result_revision + 写 GradeOverride
        result = _apply_grade_override(db, grade.id, payload, actor)
        appeal.status = "adjusted"
        appeal.score_after = (result.get("replacement") or {}).get("final_score_100")
    elif appeal.target_kind == "lab_submission":
        from app.schemas.lab import LabReviewRequest
        from app.services.lab_grading import manual_review

        submission = db.get(ExperimentSubmission, appeal.target_id)
        if submission is None:
            raise api_error(404, "APPEAL_TARGET_NOT_FOUND", "实验提交不存在")
        if score is None:
            raise api_error(422, "APPEAL_SCORE_REQUIRED", "实验改分必须给出 0-100 的总分")
        manual_review(db, submission, actor,
                      LabReviewRequest(score=score, feedback=comment, reason=comment))
        appeal.status = "adjusted"
        appeal.score_after = score
    else:
        raise api_error(409, "APPEAL_TARGET_UNSUPPORTED", "该成绩类型暂不支持人工改分")

    appeal.handler_comment = comment
    appeal.resolved_by_id = actor.id
    appeal.resolved_at = datetime.now(timezone.utc)
    db.add(AuditEvent(
        actor_id=actor.id, action="appeal.adjusted", target_type="grade_appeal",
        target_id=str(appeal.id), reason=comment,
        changes={"score_before": appeal.score_before, "score_after": appeal.score_after,
                 "target_kind": appeal.target_kind, "target_id": appeal.target_id},
    ))
    db.commit()
    db.refresh(appeal)
    return _handle_result(appeal)


def _regrade_appeal(
    db: Session, redis_client: Redis, appeal: GradeAppeal, actor: User, comment: str,
) -> dict:
    """重评：走既有 retry 机制，appeal 置 regrading，完成前不关闭处理流程。"""
    if not comment:
        raise api_error(422, "APPEAL_COMMENT_REQUIRED", "发起重评必须填写处理说明")

    # 先落 appeal 状态与审计，再触发重评：重评入队内部会 commit，
    # 顺序反过来会留下「已重新入队但异议仍显示待处理」的半提交窗口。
    appeal.status = "regrading"
    appeal.handler_comment = comment
    appeal.score_after = None
    appeal.resolved_at = None
    db.add(AuditEvent(
        actor_id=actor.id, action="appeal.regrade_requested", target_type="grade_appeal",
        target_id=str(appeal.id), reason=comment,
        changes={"target_kind": appeal.target_kind, "target_id": appeal.target_id},
    ))
    db.flush()

    if appeal.target_kind in ("submission", "exam_answer"):
        from app.api.ai_grading import _requeue_grade

        grade = db.get(CodeGrade, appeal.code_grade_id) if appeal.code_grade_id else None
        if grade is None:
            raise api_error(409, "APPEAL_TARGET_MISSING", "评分记录不存在，无法发起重评")
        # allow_completed：异议本身就是「对已完成结果的合法重评理由」；
        # 其余保护（来源版本校验、三类 409、清认领、代次递增）与普通 retry 完全一致
        result = _requeue_grade(db, redis_client, grade, allow_completed=True, commit=False)
        if not result.get("ok"):
            raise api_error(409, "APPEAL_REGRADE_REJECTED", result.get("message") or "重评入队失败")
    elif appeal.target_kind == "lab_submission":
        from app.services.lab_submission_service import enqueue_regrade

        submission = db.get(ExperimentSubmission, appeal.target_id)
        state = db.get(LabSubmission, appeal.target_id)
        if submission is None or state is None:
            raise api_error(404, "APPEAL_TARGET_NOT_FOUND", "实验提交不存在")
        enqueue_regrade(db, submission, actor, reason=f"成绩异议重评：{comment}")
        # 重评由实验判题 worker 异步完成，现有复核标志让教师侧能看到待办
        state.review_requested = True
        state.review_reason = f"成绩异议重评：{comment}"[:2000]
    else:
        raise api_error(409, "APPEAL_TARGET_UNSUPPORTED", "该成绩类型暂不支持重评")

    db.commit()
    db.refresh(appeal)
    return _handle_result(appeal)


def resolve_regrading_appeals(db: Session, code_grade_id: int) -> list[int]:
    """AI 终态写入成功后回查该目标的重评异议：按分数是否变化转 adjusted / upheld。

    在 ``ai_grading_queue.finish_ai_grade`` 的成功分支调用。重评仍失败（review_required /
    无新分数）时保持 ``regrading``，绝不提前关闭处理流程。
    """
    grade = db.get(CodeGrade, code_grade_id)
    if grade is None or grade.status != "completed" or grade.final_score_100 is None:
        return []
    if grade.submission_id:
        target_kind, target_id = "submission", grade.submission_id
    elif grade.exam_answer_id:
        target_kind, target_id = "exam_answer", grade.exam_answer_id
    else:
        return []

    rows = db.scalars(
        select(GradeAppeal).where(
            GradeAppeal.target_kind == target_kind,
            GradeAppeal.target_id == target_id,
            GradeAppeal.status == "regrading",
        )
    ).all()
    resolved: list[int] = []
    now = datetime.now(timezone.utc)
    for appeal in rows:
        after = float(grade.final_score_100)
        before = appeal.score_before
        changed = before is not None and abs(float(before) - after) > 1e-9
        appeal.status = "adjusted" if changed else "upheld"
        appeal.score_after = after
        appeal.resolved_at = now
        if not appeal.handler_comment:
            appeal.handler_comment = "重评完成"
        db.add(AuditEvent(
            actor_id=None, action=f"appeal.regrade_{appeal.status}", target_type="grade_appeal",
            target_id=str(appeal.id), reason="重评完成，按分数变化自动结转",
            changes={"score_before": before, "score_after": after,
                     "code_grade_id": code_grade_id, "result_revision": grade.result_revision},
        ))
        resolved.append(appeal.id)
    if resolved:
        db.commit()
        logger.info("appeal_regrade_resolved", extra={"appeals": resolved, "grade": code_grade_id})
    return resolved


# ── 通知派生（供 notification_service 使用） ──────────────────────


def teacher_pending_appeal_items(db: Session, teacher: User, limit: int = 20) -> dict[str, dict]:
    condition = appeal_scope_condition(db, teacher)
    if condition is None:
        return {}
    rows = db.scalars(
        select(GradeAppeal).where(GradeAppeal.status.in_(APPEAL_OPEN_STATUSES))
        .where(condition)
        .order_by(GradeAppeal.created_at.desc(), GradeAppeal.id.desc()).limit(limit)
    ).all()
    items: dict[str, dict] = {}
    for appeal in rows:
        student = db.get(User, appeal.student_id)
        status_text = "待处理" if appeal.status == "pending" else "重评中"
        items[f"appeal:{appeal.id}:pending"] = {
            "type": "appeal",
            "title": f"成绩异议{status_text}",
            "content": f"{student.real_name if student else '学生'} 对 {appeal.target_kind} 成绩提出异议"
                       f"（原分 {appeal.score_before if appeal.score_before is not None else '—'}）",
            "entity_kind": "grade_appeal",
            "entity_id": appeal.id,
            "route": f"/teacher/ai-grading?appeal={appeal.id}",
            "priority": "important",
        }
    return items


def count_offering_pending_appeals(db: Session, offering_id: int) -> int:
    """教学班范围内未决（待处理/重评中）异议数——成绩册只提示，不阻止发布。"""
    submission_scope = GradeAppeal.target_id.in_(
        select(Submission.id)
        .join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
        .join(Assignment, Assignment.id == JudgeQuestion.assignment_id)
        .where(Assignment.offering_id == offering_id)
    )
    exam_scope = GradeAppeal.target_id.in_(
        select(ExamAnswer.id)
        .join(ExamQuestion, ExamQuestion.id == ExamAnswer.question_id)
        .join(Exam, Exam.id == ExamQuestion.exam_id)
        .where(Exam.offering_id == offering_id)
    )
    lab_scope = GradeAppeal.target_id.in_(
        select(LabSubmission.submission_id)
        .join(ExperimentSubmission, ExperimentSubmission.id == LabSubmission.submission_id)
        .join(ExperimentRecord, ExperimentRecord.id == ExperimentSubmission.record_id)
        .where(ExperimentRecord.offering_id == offering_id)
    )
    return int(db.scalar(
        select(func.count()).select_from(GradeAppeal).where(
            GradeAppeal.status.in_(APPEAL_OPEN_STATUSES),
            or_(
                and_(GradeAppeal.target_kind == "submission", submission_scope),
                and_(GradeAppeal.target_kind == "exam_answer", exam_scope),
                and_(GradeAppeal.target_kind == "lab_submission", lab_scope),
            ),
        )
    ) or 0)
