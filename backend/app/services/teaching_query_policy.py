"""Apply teaching scope before pagination/aggregation, including legacy endpoints.

认证关键复查（黑名单）在 Redis 故障时必须 503，绝不当成"未撤销"放行。

Route authorization remains mandatory. This ORM boundary also protects joins,
dashboard counts and lazy loads that otherwise accidentally use course ownership.
"""
import logging

from sqlalchemy import and_, event, exists, or_, select
from sqlalchemy.orm import Session, with_loader_criteria


logger = logging.getLogger(__name__)


@event.listens_for(Session, 'do_orm_execute')
def _scope_queries(state):
    if not state.is_select or state.is_column_load or state.is_relationship_load:
        return
    actor = state.session.info.get('teaching_actor')
    if not actor or actor['role'] == 'admin':
        return
    from app.models import (Assignment, CodeGrade, Course, CourseEnrollment, CourseTeachingClass,
        Exam, ExamAnswer, ExamSubmission, ExperimentRecord, ExperimentSubmission, JudgeQuestion,
        LessonProgress, Submission)
    from app.models.teaching import CourseOffering, OfferingEnrollment
    ident, role = actor['id'], actor['role']
    if role == 'teacher':
        scopes = select(CourseOffering.id).where(CourseOffering.teacher_id == ident,
            CourseOffering.status.in_(['active', 'archived']))
        content = select(Course.id).where(or_(Course.teacher_id == ident,
            Course.id.in_(select(CourseOffering.course_id).where(CourseOffering.id.in_(scopes)))))
        rules = [
            (Assignment, or_(Assignment.offering_id.in_(scopes), and_(Assignment.offering_id.is_(None), Assignment.course_id.in_(content)))),
            (Exam, or_(Exam.offering_id.in_(scopes), and_(Exam.offering_id.is_(None), Exam.course_id.in_(content)))),
            (ExperimentRecord, ExperimentRecord.offering_id.in_(scopes)),
            (ExperimentSubmission, exists(select(ExperimentRecord.id).where(ExperimentRecord.id == ExperimentSubmission.record_id, ExperimentRecord.offering_id.in_(scopes)).correlate(ExperimentSubmission))),
            (Submission, exists(select(JudgeQuestion.id).join(Assignment).where(JudgeQuestion.id == Submission.question_id, Assignment.offering_id.in_(scopes)).correlate(Submission))),
            (ExamSubmission, exists(select(Exam.id).where(Exam.id == ExamSubmission.exam_id, Exam.offering_id.in_(scopes)).correlate(ExamSubmission))),
            (ExamAnswer, exists(select(ExamSubmission.id).join(Exam).where(ExamSubmission.id == ExamAnswer.submission_id, Exam.offering_id.in_(scopes)).correlate(ExamAnswer))),
            (CourseEnrollment, exists(select(OfferingEnrollment.id).join(CourseOffering).where(
                OfferingEnrollment.student_id == CourseEnrollment.student_id, CourseOffering.course_id == CourseEnrollment.course_id,
                OfferingEnrollment.offering_id.in_(scopes), OfferingEnrollment.status == 'active').correlate(CourseEnrollment))),
            (CourseTeachingClass, CourseTeachingClass.teaching_class_id.in_(select(CourseOffering.teaching_class_id).where(CourseOffering.id.in_(scopes)))),
            (LessonProgress, LessonProgress.offering_id.in_(scopes)),
        ]
        own_code = exists(select(Submission.id).join(JudgeQuestion).join(Assignment).where(
            Submission.id == CodeGrade.submission_id, Assignment.offering_id.in_(scopes)).correlate(CodeGrade))
        own_exam = exists(select(ExamAnswer.id).join(ExamSubmission).join(Exam).where(
            ExamAnswer.id == CodeGrade.exam_answer_id, Exam.offering_id.in_(scopes)).correlate(CodeGrade))
        rules.append((CodeGrade, or_(own_code, own_exam)))
    else:
        scopes = select(CourseOffering.id).join(OfferingEnrollment).where(
            OfferingEnrollment.student_id == ident, OfferingEnrollment.status == 'active', CourseOffering.status == 'active')
        rules = [(Assignment, Assignment.offering_id.in_(scopes)), (Exam, Exam.offering_id.in_(scopes)),
            (ExperimentRecord, ExperimentRecord.student_id == ident),
            (ExperimentSubmission, exists(select(ExperimentRecord.id).where(ExperimentRecord.id == ExperimentSubmission.record_id, ExperimentRecord.student_id == ident).correlate(ExperimentSubmission))),
            (Submission, Submission.student_id == ident), (ExamSubmission, ExamSubmission.student_id == ident),
            (CourseTeachingClass, CourseTeachingClass.teaching_class_id.in_(select(CourseOffering.teaching_class_id).where(CourseOffering.id.in_(scopes)))),
            (CourseEnrollment, CourseEnrollment.student_id == ident), (LessonProgress, LessonProgress.student_id == ident)]
        # Only the explicitly marked, student-owned history projection may read
        # task metadata after a transfer/archive. Submission/record ownership
        # filters remain in force; ordinary task routes never set this option.
        if state.execution_options.get('teaching_history') is True:
            rules = [(model, predicate) for model, predicate in rules if model not in (Assignment, Exam)]
    statement = state.statement
    for model, predicate in rules:
        statement = statement.options(with_loader_criteria(model, predicate, include_aliases=True))
    state.statement = statement


def install_request_scope(db, user):
    # Preserve authentication facts separately from mutable ORM identities.
    # populate_existing current reads can change ``user`` in place while a
    # request waits for an administrator's policy transaction to finish.
    db.info['teaching_actor'] = {
        'id': user.id, 'role': user.role, 'status': user.status,
        'session_version': user.session_version,
        'credential_version': user.credential_version,
    }


def lock_current_actor(db, user):
    """Authorize a short mutation against current committed account state.

    Ordinary GETs keep their unlocked query path. Callers commit/rollback before
    copying files, contacting providers or running Docker, then call this again
    before committing completion metadata. The policy row is acquired first so
    account, roster and teaching mutations have one transaction order.
    """
    from app.errors import api_error
    from app.models import User
    from app.services.account_service import ensure_full_account, lock_policy

    expected = dict(db.info.get('teaching_actor') or {
        'id': user.id, 'role': user.role, 'status': user.status,
        'session_version': user.session_version,
        'credential_version': user.credential_version,
    })
    if expected['id'] != user.id:
        raise api_error(403, 'FORBIDDEN', '账号身份与当前请求不一致')
    lock_policy(db)
    current = db.scalar(select(User).where(User.id == expected['id'])
                        .with_for_update().execution_options(populate_existing=True))
    if (not current or current.status != 'active'
            or any(getattr(current, key) != expected[key] for key in
                   ('role', 'status', 'session_version', 'credential_version'))):
        raise api_error(403, 'FORBIDDEN', '账号权限或会话已变化，请重新登录')
    jti, redis_client = db.info.get('teaching_token_jti'), db.info.get('teaching_token_redis')
    if jti and redis_client is not None:
        # 写入前撤销复查：Redis 不可用时必须 503（fail closed），
        # 既不能放行（把已撤销 Token 当有效），也不能冒泡成 500。
        try:
            revoked = redis_client.exists(f'blacklist:{jti}')
        except Exception as exc:  # noqa: BLE001 - 统一转 503
            logger.error('撤销复查失败（Redis 不可用）: %s', exc)
            raise api_error(503, 'SERVICE_UNAVAILABLE',
                            '认证服务暂时不可用，请稍后重试') from exc
        if revoked:
            raise api_error(401, 'TOKEN_REVOKED', 'Token 已失效')
    ensure_full_account(current)
    return current
