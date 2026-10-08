"""Common authorization predicates for content and immutable teaching context."""
from sqlalchemy import exists, select

from app.errors import api_error
from app.models import AcademicTerm, Course, CourseEnrollment, TeachingClass, User
from app.models.teaching import CourseOffering, OfferingEnrollment
from app.services.time_utils import utc_now, as_utc


def teacher_offering_ids(db, user_id, course_id=None):
    query = select(CourseOffering.id).where(CourseOffering.teacher_id == user_id,
                                           CourseOffering.status.in_(['active', 'archived']))
    if course_id is not None:
        query = query.where(CourseOffering.course_id == course_id)
    return list(db.scalars(query))


def student_offering_ids(db, user_id, course_id=None):
    query = select(CourseOffering.id).join(OfferingEnrollment).join(Course, Course.id == CourseOffering.course_id).where(
        Course.status == "published",
        OfferingEnrollment.student_id == user_id, OfferingEnrollment.status == 'active',
        CourseOffering.status == 'active')
    if course_id is not None:
        query = query.where(CourseOffering.course_id == course_id)
    return list(db.scalars(query))


def content_course_ids(db, user):
    if user.role == 'admin':
        return list(db.scalars(select(Course.id)))
    offerings = teacher_offering_ids(db, user.id) if user.role == 'teacher' else student_offering_ids(db, user.id)
    ids = set(db.scalars(select(CourseOffering.course_id).where(CourseOffering.id.in_(offerings))))
    if user.role == 'teacher':
        ids.update(db.scalars(select(Course.id).where(Course.teacher_id == user.id)))
    return list(ids)


def can_edit_course_content(db, course, user):
    return user.role == 'admin' or (user.role == 'teacher' and course.teacher_id == user.id)


def can_read_course_content(db, course, user):
    if user.role == 'admin' or can_edit_course_content(db, course, user):
        return True
    if user.role == 'teacher':
        return bool(teacher_offering_ids(db, user.id, course.id))
    return (user.role == 'student' and course.status == 'published'
            and bool(student_offering_ids(db, user.id, course.id)))


def require_offering(db, ident, user, write=False):
    if write:
        from app.services.teaching_query_policy import lock_current_actor
        user = lock_current_actor(db, user)
        offering = db.scalar(select(CourseOffering).where(CourseOffering.id == ident)
                             .with_for_update().execution_options(populate_existing=True))
    else:
        offering = db.get(CourseOffering, ident)
    if not offering:
        raise api_error(404, 'OFFERING_NOT_FOUND', '授课实例不存在')
    term = klass = course = None
    if write:
        # FOR UPDATE is an InnoDB current read, even when authentication or a
        # previous page preview has already established a RR snapshot.
        term = db.scalar(select(AcademicTerm).where(AcademicTerm.id == offering.academic_term_id)
                         .with_for_update().execution_options(populate_existing=True))
        klass = db.scalar(select(TeachingClass).where(TeachingClass.id == offering.teaching_class_id)
                          .with_for_update().execution_options(populate_existing=True))
        course = db.scalar(select(Course).where(Course.id == offering.course_id)
                           .with_for_update().execution_options(populate_existing=True))
    allowed = user.role == 'admin'
    if user.role == 'teacher':
        allowed = offering.teacher_id == user.id and offering.status in ('active', 'archived')
    elif user.role == 'student':
        query = select(OfferingEnrollment).where(OfferingEnrollment.offering_id == ident, OfferingEnrollment.student_id == user.id)
        membership = db.scalar(query.with_for_update().execution_options(populate_existing=True) if write else query)
        allowed = bool(membership and offering.status in ('active', 'archived') and (not write or (membership.status == 'active' and course and course.status == 'published')))
    if not allowed:
        raise api_error(403, 'FORBIDDEN', '无权访问该班级的教学数据')
    if write:
        if offering.status != 'active' or not term or term.status == 'closed' or not klass or klass.status == 'archived':
            raise api_error(409, 'OFFERING_READ_ONLY', '授课实例、班级或学期已归档，当前只读')
    return offering


def require_no_started_exams(db, *, term_id=None, class_id=None, offering_id=None):
    """Check closure under the caller's policy lock using an InnoDB current read.

    Expired attempts still need collection; publication and roster changes do
    not make an outstanding attempt safe to abandon. This never runs a job.
    """
    from app.models import Exam, ExamSubmission

    if sum(value is not None for value in (term_id, class_id, offering_id)) != 1:
        raise ValueError('Exactly one teaching context is required')
    query = (select(ExamSubmission.id).join(Exam, Exam.id == ExamSubmission.exam_id)
             .join(CourseOffering, CourseOffering.id == Exam.offering_id)
             .where(ExamSubmission.status == 'started'))
    if term_id is not None:
        query = query.where(CourseOffering.academic_term_id == term_id)
    elif class_id is not None:
        query = query.where(CourseOffering.teaching_class_id == class_id)
    else:
        query = query.where(CourseOffering.id == offering_id)
    if db.scalar(query.order_by(ExamSubmission.id).limit(1).with_for_update()) is not None:
        raise api_error(409, 'EXAMS_IN_PROGRESS', '仍有考试未收卷，请先完成收卷后再关闭或归档')


def require_student_task_write(db, task, student):
    """Refresh task and teaching authority before each short student write."""
    from app.services.teaching_query_policy import lock_current_actor

    student = lock_current_actor(db, student)
    if student.role != 'student':
        raise api_error(403, 'FORBIDDEN', '仅学生可以提交教学任务')
    model = type(task)
    current = db.scalar(select(model).where(model.id == task.id).with_for_update()
                        .execution_options(populate_existing=True))
    if not current:
        raise api_error(404, 'TASK_NOT_FOUND', '教学任务不存在或无权访问')
    if current.offering_id is None:
        raise api_error(403, 'TASK_SCOPE_REQUIRED', '历史任务尚未确认授课归属')
    require_offering(db, current.offering_id, student, write=True)
    if current.status != 'published':
        raise api_error(403, f'{model.__name__.upper()}_NOT_AVAILABLE', '教学任务未发布')
    return current


def record_teaching_access(db, record, user, write=False):
    if write:
        from app.models import ExperimentRecord
        from app.services.teaching_query_policy import lock_current_actor
        user = lock_current_actor(db, user)
        # Context can be explicitly reassigned by an administrator. Refresh
        # the record under the same policy lock before checking that context.
        current = db.scalar(select(ExperimentRecord).where(ExperimentRecord.id == record.id)
                            .with_for_update().execution_options(populate_existing=True))
        if not current:
            raise api_error(404, 'RECORD_NOT_FOUND', '实验记录不存在')
        record = current
    if record.offering_id:
        require_offering(db, record.offering_id, user, write=write)
        if user.role == 'student' and record.student_id != user.id:
            raise api_error(403, 'FORBIDDEN', '只能访问自己的记录')
    elif user.role != 'admin' and not (user.role == 'student' and record.student_id == user.id and not write):
        raise api_error(403, 'LEGACY_SCOPE_UNASSIGNED', '历史记录尚未由管理员确认班级归属')
    return record


def scoped_student_ids(db, user):
    if user.role == 'admin':
        return select(User.id).where(User.role == 'student')
    ids = teacher_offering_ids(db, user.id) if user.role == 'teacher' else []
    return select(OfferingEnrollment.student_id).where(OfferingEnrollment.offering_id.in_(ids),
                                                       OfferingEnrollment.status == 'active')


def teacher_course_condition(user_id):
    return Course.id.in_(select(CourseOffering.course_id).where(CourseOffering.teacher_id == user_id,
                                                               CourseOffering.status.in_(['active', 'archived'])))


def teacher_record_condition(user_id):
    from app.models import ExperimentRecord
    return ExperimentRecord.offering_id.in_(select(CourseOffering.id).where(
        CourseOffering.teacher_id == user_id, CourseOffering.status.in_(['active', 'archived'])))


def teacher_task_condition(model, user_id):
    return model.offering_id.in_(select(CourseOffering.id).where(
        CourseOffering.teacher_id == user_id, CourseOffering.status.in_(['active', 'archived'])))


def require_task_manager(db, task, user, write=False):
    if write:
        from app.services.teaching_query_policy import lock_current_actor
        user = lock_current_actor(db, user)
        model = type(task)
        task = db.scalar(select(model).where(model.id == task.id).with_for_update()
                         .execution_options(populate_existing=True))
        if not task:
            raise api_error(404, 'TASK_NOT_FOUND', '教学任务不存在或无权访问')
    if user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "仅授课教师或管理员可管理教学任务")
    if task.offering_id:
        return require_offering(db, task.offering_id, user, write=write)
    course = db.get(Course, task.course_id)
    if user.role == 'admin' or (not write and user.role == 'teacher' and course and course.teacher_id == user.id):
        return None
    raise api_error(403, 'TASK_SCOPE_REQUIRED', '教学任务需绑定管理员分配的授课实例')


def offering_student_predicate(offering_id, student_id):
    return exists(select(OfferingEnrollment.id).where(OfferingEnrollment.offering_id == offering_id,
        OfferingEnrollment.student_id == student_id, OfferingEnrollment.status == 'active'))


def enroll_student(db, offering, student_id, origin="class"):
    student = db.scalar(select(User).where(User.id == student_id).with_for_update()
                        .execution_options(populate_existing=True))
    if not student or student.role != 'student' or student.status != 'active':
        raise api_error(422, 'INVALID_STUDENT', '学生不存在或不可用')
    projection = db.scalar(select(CourseEnrollment).where(CourseEnrollment.course_id == offering.course_id,
        CourseEnrollment.student_id == student_id).with_for_update().execution_options(populate_existing=True))
    existing = db.scalar(select(OfferingEnrollment).where(OfferingEnrollment.offering_id == offering.id,
        OfferingEnrollment.student_id == student_id).with_for_update().execution_options(populate_existing=True))
    if origin == 'class' and ((existing and existing.origin == 'manual' and existing.status != 'active') or (projection and projection.origin == 'manual' and projection.status == 'dropped')):
        return existing
    key = f'{offering.course_id}:{offering.academic_term_id}:{student_id}'
    other = db.scalar(select(OfferingEnrollment).where(OfferingEnrollment.active_scope_key == key)
                      .with_for_update().execution_options(populate_existing=True))
    if other and other.offering_id != offering.id:
        raise api_error(409, 'STUDENT_OFFERING_CONFLICT', '该学生在同一课程学期已有成绩归属班，请先办理转班')
    row = existing
    if not row:
        row = OfferingEnrollment(offering_id=offering.id, student_id=student_id)
        db.add(row)
    row.status, row.active_scope_key = 'active', key
    if origin == 'manual' or not row.origin:
        row.origin = origin
    if not projection:
        db.add(CourseEnrollment(course_id=offering.course_id, student_id=student_id, status='enrolled', origin=origin))
    else:
        projection.status = 'enrolled'
        if origin == 'manual':
            projection.origin = 'manual'
    return row


def resolve_student_offering(db, course_id, user, offering_id=None):
    ids = student_offering_ids(db, user.id, course_id)
    if offering_id is not None:
        if offering_id not in ids:
            raise api_error(403, 'FORBIDDEN', '未加入该授课班级')
        return require_offering(db, offering_id, user, write=True)
    if len(ids) != 1:
        raise api_error(409, 'OFFERING_REQUIRED', '请选择已获授权的授课实例')
    return require_offering(db, ids[0], user, write=True)


def require_delivery(db, offering_id, kind, ident, user, write=True, lock=False):
    from app.models.teaching import OfferingExperiment
    offering = require_offering(db, offering_id, user, write=write)
    query = select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id,
        OfferingExperiment.entry_kind == kind, OfferingExperiment.entry_id == ident)
    if lock or write:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if not row or row.status != 'published':
        raise api_error(403, 'EXPERIMENT_NOT_RELEASED', '该实验尚未向本班发布')
    now = utc_now()
    if write and ((row.opens_at and as_utc(row.opens_at) > now) or (row.deadline and as_utc(row.deadline) < now)):
        raise api_error(409, 'EXPERIMENT_OUTSIDE_WINDOW', '当前不在本班实验开放时间内')
    return offering, row


def task_content_manager(db, task, user):
    writing = db.info.get("teaching_request_method") not in ("GET", "HEAD")
    if writing:
        from app.services.teaching_query_policy import lock_current_actor
        user = lock_current_actor(db, user)
        model = type(task)
        task = db.scalar(select(model).where(model.id == task.id).with_for_update()
                         .execution_options(populate_existing=True))
        if not task:
            raise api_error(404, 'TASK_NOT_FOUND', '教学任务不存在或无权访问')
    if user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "仅授课教师或管理员可管理教学任务")
    if task.offering_id:
        require_offering(db, task.offering_id, user, write=writing)
    else:
        course = (db.scalar(select(Course).where(Course.id == task.course_id).with_for_update()
                            .execution_options(populate_existing=True)) if writing else db.get(Course, task.course_id))
        if not course or not can_edit_course_content(db, course, user):
            raise api_error(403, 'FORBIDDEN', '无权修改共享任务模板')


def require_submission_context(db, task, user, write=False):
    if task.offering_id is None and user.role != 'admin':
        raise api_error(403, 'TASK_SCOPE_REQUIRED', '历史任务尚未确认授课归属')
    return require_task_manager(db, task, user, write=write)
