"""Administrator teaching allocation and teacher-owned class delivery."""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.dependencies import get_current_user, get_db, require_roles, PaginationParams, pagination
from app.errors import api_error
from app.models import (AcademicTerm, Assignment, Chapter, Course, CourseTeachingClass, Exam,
    ExperimentModule, ExperimentRecord, Lesson, NotebookTemplate, TeachingClass, TeachingClassStudent, User)
from app.models.teaching import CourseOffering, OfferingEnrollment, OfferingExperiment
from app.services import offering_service as scope
from app.services.account_service import audit, lock_policy
from app.services.teaching_query_policy import lock_current_actor

router = APIRouter(tags=['offerings'])


@router.get('/courses/{course_id}/teaching-templates')
def teaching_templates(course_id: int, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    from app.models import LabAssessment, NotebookTemplateVersion
    course = db.get(Course, course_id)
    if not course or not scope.can_read_course_content(db, course, user):
        raise api_error(403, 'FORBIDDEN', '无权读取该课程模板')
    tasks = []
    for kind, model in [('assignment', Assignment), ('exam', Exam)]:
        tasks.extend({'id': r.id, 'kind': kind, 'title': r.title} for r in db.scalars(select(model).where(model.course_id == course_id, model.offering_id.is_(None))))
    entries = [('lesson', r, r.title) for r in db.scalars(select(Lesson).join(Chapter).where(Chapter.course_id == course_id, Lesson.template_id.is_not(None)))]
    modules = select(ExperimentModule).where(ExperimentModule.template_id.is_not(None))
    if user.role != 'admin':
        shared_modules = select(OfferingExperiment.entry_id).join(CourseOffering).where(
            CourseOffering.course_id == course_id, OfferingExperiment.entry_kind == 'module',
            OfferingExperiment.status == 'published')
        modules = modules.where(or_(ExperimentModule.owner_id == user.id, ExperimentModule.id.in_(shared_modules)))
    entries.extend(('module', r, r.name) for r in db.scalars(modules))
    experiments = []
    for kind, r, title in entries:
        template = db.get(NotebookTemplate, r.template_id)
        version = db.get(NotebookTemplateVersion, template.current_version_id) if template and template.current_version_id else None
        assessments = db.scalars(select(LabAssessment).where(LabAssessment.entry_key == f'{kind}:{r.id}', LabAssessment.status == 'published')).all()
        experiments.append({'kind': kind, 'id': r.id, 'title': title, 'status': r.status,
            'template_version_id': version.id if version else None, 'cells': version.cells if version else [],
            'environment_version_id': version.environment_version_id if version else None,
            'assessments': [{'id': a.id, 'version': a.version} for a in assessments]})
    return {'tasks': tasks, 'experiments': experiments}


class OfferingCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    course_id: int = Field(gt=0)
    teaching_class_id: int = Field(gt=0)
    academic_term_id: int = Field(gt=0)
    teacher_id: int = Field(gt=0)
    status: Literal['active', 'pending_assignment'] = 'active'


class OfferingUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    teacher_id: int | None = Field(default=None, gt=0)
    status: Literal['active', 'archived', 'pending_assignment'] | None = None
    reason: str = Field(min_length=1, max_length=500)


class Members(BaseModel):
    student_ids: list[int] = Field(min_length=1, max_length=5000)
    reason: str = Field(min_length=1, max_length=500)


class Transfer(BaseModel):
    destination_offering_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class Reason(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator('reason')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('必须填写原因')
        return value.strip()


class RecordTransfer(Reason):
    source_offering_id: int = Field(gt=0)


class Owner(BaseModel):
    teacher_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class TaskCopy(BaseModel):
    kind: Literal['assignment', 'exam']
    source_id: int = Field(gt=0)


class ExperimentRelease(BaseModel):
    entry_kind: Literal['lesson', 'module']
    entry_id: int = Field(gt=0)
    assessment_id: int | None = Field(default=None, gt=0)
    opens_at: datetime | None = None
    deadline: datetime | None = None
    status: Literal['draft', 'published'] = 'published'

    @field_validator('opens_at', 'deadline')
    @classmethod
    def utc_time(cls, value):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError('时间必须包含时区')
            return value.astimezone(timezone.utc)
        return value


def offering_read(db, row):
    course, term, klass, teacher = db.get(Course, row.course_id), db.get(AcademicTerm, row.academic_term_id), db.get(TeachingClass, row.teaching_class_id), db.get(User, row.teacher_id) if row.teacher_id else None
    return {**{key: getattr(row, key) for key in ('id', 'course_id', 'academic_term_id', 'teaching_class_id', 'teacher_id', 'status', 'leaderboard_named')},
            'course_title': course.title, 'term_name': term.name, 'class_name': klass.name,
            'teacher_name': teacher.real_name if teacher else None}


def _teacher(db, ident):
    teacher = _current(db, User, ident)
    if not teacher or teacher.role != 'teacher' or teacher.status != 'active':
        raise api_error(422, 'INVALID_TEACHER', '授课教师不存在或不可用')
    return teacher


def _current(db, model, ident):
    return db.scalar(select(model).where(model.id == ident).with_for_update()
                     .execution_options(populate_existing=True))


@router.get('/offerings')
def list_offerings(course_id: int | None = None, db=Depends(get_db), user=Depends(get_current_user)):
    query = select(CourseOffering)
    if user.role == 'teacher':
        query = query.where(CourseOffering.id.in_(scope.teacher_offering_ids(db, user.id)))
    elif user.role == 'student':
        query = query.where(CourseOffering.id.in_(scope.student_offering_ids(db, user.id)))
    if course_id:
        query = query.where(CourseOffering.course_id == course_id)
    rows = db.scalars(query.order_by(CourseOffering.id.desc())).all()
    return {'items': [offering_read(db, row) for row in rows], 'total': len(rows)}


@router.post('/offerings', status_code=201)
def create_offering(payload: OfferingCreate, db=Depends(get_db), user=Depends(require_roles('admin'))):
    user = lock_current_actor(db, user)
    course, klass, term = _current(db, Course, payload.course_id), _current(db, TeachingClass, payload.teaching_class_id), _current(db, AcademicTerm, payload.academic_term_id)
    if not course or not klass or not term or klass.academic_term_id != term.id:
        raise api_error(422, 'OFFERING_CONTEXT_INVALID', '课程、班级或学期无效')
    if klass.status != 'active' or term.status == 'closed':
        raise api_error(409, 'OFFERING_READ_ONLY', '班级或学期已归档')
    _teacher(db, payload.teacher_id)
    row = CourseOffering(**payload.model_dump())
    db.add(row)
    try:
        db.flush()
        if not db.scalar(select(CourseTeachingClass.id).where(CourseTeachingClass.course_id == course.id, CourseTeachingClass.teaching_class_id == klass.id)):
            db.add(CourseTeachingClass(course_id=course.id, teaching_class_id=klass.id))
        if row.status == 'active':
            for ident in db.scalars(select(TeachingClassStudent.student_id).where(TeachingClassStudent.teaching_class_id == klass.id, TeachingClassStudent.status == 'active')):
                scope.enroll_student(db, row, ident)
        audit(db, user.id, 'offering.create', 'course_offering', row.id, changes=payload.model_dump())
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, 'OFFERING_CONFLICT', '授课实例或学生成绩归属发生冲突')
    return offering_read(db, row)


@router.get('/offerings/{offering_id}')
def get_offering(offering_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    return offering_read(db, scope.require_offering(db, offering_id, user))


@router.patch('/offerings/{offering_id}')
def update_offering(offering_id: int, payload: OfferingUpdate, db=Depends(get_db), user=Depends(require_roles('admin'))):
    user = lock_current_actor(db, user)
    row = _current(db, CourseOffering, offering_id)
    if not row:
        raise api_error(404, 'OFFERING_NOT_FOUND', '授课实例不存在')
    if payload.teacher_id:
        _teacher(db, payload.teacher_id)
        row.teacher_id = payload.teacher_id
    if payload.status:
        if payload.status == 'active':
            if not row.teacher_id:
                raise api_error(422, 'TEACHER_REQUIRED', '激活前需指定教师')
            _teacher(db, row.teacher_id)
            term, klass = _current(db, AcademicTerm, row.academic_term_id), _current(db, TeachingClass, row.teaching_class_id)
            if not term or term.status == 'closed' or not klass or klass.status == 'archived':
                raise api_error(409, 'OFFERING_READ_ONLY', '班级或学期已归档，不能激活')
            row.status = 'active'
            for ident in db.scalars(select(TeachingClassStudent.student_id).where(TeachingClassStudent.teaching_class_id == row.teaching_class_id, TeachingClassStudent.status == 'active')):
                scope.enroll_student(db, row, ident)
        else:
            row.status = payload.status
            for member in db.scalars(select(OfferingEnrollment).where(OfferingEnrollment.offering_id == row.id)):
                member.active_scope_key = None
    audit(db, user.id, 'offering.update', 'course_offering', row.id, reason=payload.reason, changes=payload.model_dump(exclude_unset=True))
    db.commit()
    return offering_read(db, row)


@router.post('/courses/{course_id}/content-owner')
def set_content_owner(course_id: int, payload: Owner, db=Depends(get_db), user=Depends(require_roles('admin'))):
    actor = lock_current_actor(db, user)
    if not actor or actor.role != 'admin' or actor.status != 'active':
        raise api_error(403, 'FORBIDDEN', '管理员权限已变化，请重新登录')
    course = db.scalar(select(Course).where(Course.id == course_id).with_for_update().execution_options(populate_existing=True))
    if not course:
        raise api_error(404, 'COURSE_NOT_FOUND', '课程不存在')
    _teacher(db, payload.teacher_id)
    course.teacher_id = payload.teacher_id
    audit(db, user.id, 'course.owner.transfer', 'course', course_id, reason=payload.reason, changes={'teacher_id': payload.teacher_id})
    db.commit()
    return {'teacher_id': course.teacher_id}


@router.get('/offerings/{offering_id}/students')
def students(offering_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    scope.require_offering(db, offering_id, user)
    query = select(User, OfferingEnrollment.status).join(OfferingEnrollment, OfferingEnrollment.student_id == User.id).where(OfferingEnrollment.offering_id == offering_id)
    if user.role == 'student':
        query = query.where(User.id == user.id)
    rows = db.execute(query.order_by(User.student_no, User.id)).all()
    return {'items': [{'id': s.id, 'student_no': s.student_no, 'real_name': s.real_name, 'status': state} for s, state in rows]}


@router.post('/offerings/{offering_id}/students')
def add_students(offering_id: int, payload: Members, db=Depends(get_db), user=Depends(require_roles('admin'))):
    lock_policy(db)
    row = scope.require_offering(db, offering_id, user, write=True)
    for ident in set(payload.student_ids):
        scope.enroll_student(db, row, ident, origin="manual")
    audit(db, user.id, 'offering.students.add', 'course_offering', row.id, reason=payload.reason, changes={'student_ids': payload.student_ids})
    db.commit()
    return students(offering_id, db, user)


@router.post('/offerings/{offering_id}/students/{student_id}/transfer')
def transfer(offering_id: int, student_id: int, payload: Transfer, db=Depends(get_db), user=Depends(require_roles('admin'))):
    lock_policy(db)
    source = scope.require_offering(db, offering_id, user, write=True)
    target = scope.require_offering(db, payload.destination_offering_id, user, write=True)
    if (source.course_id, source.academic_term_id) != (target.course_id, target.academic_term_id):
        raise api_error(422, 'TRANSFER_CONTEXT_MISMATCH', '转班须在同课程同学期内')
    member = db.scalar(select(OfferingEnrollment).where(OfferingEnrollment.offering_id == source.id, OfferingEnrollment.student_id == student_id, OfferingEnrollment.status == 'active')
                       .with_for_update().execution_options(populate_existing=True))
    if not member:
        raise api_error(404, 'MEMBER_NOT_FOUND', '学生不在原班名单中')
    member.status, member.active_scope_key = 'transferred', None
    db.flush()
    scope.enroll_student(db, target, student_id, origin="manual")
    audit(db, user.id, 'offering.student.transfer', 'user', student_id, reason=payload.reason, changes={'from': source.id, 'to': target.id})
    db.commit()
    return {'ok': True}


@router.post('/offerings/{offering_id}/tasks/copy', status_code=201)
def copy_task(offering_id: int, payload: TaskCopy, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    offering = scope.require_offering(db, offering_id, user, write=True)
    model = Assignment if payload.kind == 'assignment' else Exam
    original = db.get(model, payload.source_id)
    if not original or original.course_id != offering.course_id or original.offering_id is not None:
        raise api_error(422, 'TASK_TEMPLATE_INVALID', '请选择同课程的共享任务模板')
    values = {c.name: deepcopy(getattr(original, c.name)) for c in model.__table__.columns if c.name not in {'id', 'created_at', 'updated_at'}}
    values.update(offering_id=offering.id, source_task_id=original.id, created_by_id=user.id, status='draft', audience_mode='all_enrolled')
    for key in ('published_at', 'review_released_at', 'review_released_by_id'):
        if key in values:
            values[key] = None
    copied = model(**values)
    db.add(copied)
    db.flush()
    for original_question in original.questions:
        question_model = type(original_question)
        qvalues = {c.name: deepcopy(getattr(original_question, c.name)) for c in question_model.__table__.columns if c.name not in {'id', 'created_at', 'updated_at'}}
        qvalues['assignment_id' if payload.kind == 'assignment' else 'exam_id'] = copied.id
        db.add(question_model(**qvalues))
    audit(db, user.id, 'offering.task.copy', payload.kind, copied.id, changes={'source_id': original.id, 'offering_id': offering.id})
    db.commit()
    return {'id': copied.id, 'kind': payload.kind, 'offering_id': offering.id}


@router.get('/offerings/{offering_id}/experiments')
def experiments(offering_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    scope.require_offering(db, offering_id, user)
    query = select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id)
    if user.role == 'student':
        query = query.where(OfferingExperiment.status == 'published')
    return {'items': [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in db.scalars(query)]}


@router.post('/offerings/{offering_id}/experiments')
def release_experiment(offering_id: int, payload: ExperimentRelease, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    from app.models import LabAssessment
    offering = scope.require_offering(db, offering_id, user, write=True)
    entry = _current(db, Lesson if payload.entry_kind == 'lesson' else ExperimentModule, payload.entry_id)
    if not entry or not entry.template_id:
        raise api_error(422, 'EXPERIMENT_TEMPLATE_REQUIRED', '实验入口未关联模板')
    if entry.status != 'published':
        raise api_error(422, 'EXPERIMENT_ENTRY_UNPUBLISHED', '课程负责人需先发布实验内容')
    if payload.entry_kind == 'lesson' and entry.content_type != 'notebook':
        raise api_error(422, 'EXPERIMENT_ENTRY_INVALID', '仅 Notebook 课时可作为本班在线实验发布')
    if payload.entry_kind == 'lesson':
        chapter = _current(db, Chapter, entry.chapter_id)
        if not chapter or chapter.course_id != offering.course_id:
            raise api_error(422, 'EXPERIMENT_SCOPE_MISMATCH', '课时不属于本课程')
    if payload.entry_kind == 'module' and user.role != 'admin' and entry.owner_id != user.id:
        shared = db.scalar(select(OfferingExperiment.id).join(CourseOffering).where(
            CourseOffering.course_id == offering.course_id, OfferingExperiment.entry_kind == 'module',
            OfferingExperiment.entry_id == entry.id, OfferingExperiment.status == 'published')
            .order_by(OfferingExperiment.id).limit(1).with_for_update())
        if not shared:
            raise api_error(403, 'FORBIDDEN', '请由模块负责人或管理员先将实验模板纳入本课程')
    template = _current(db, NotebookTemplate, entry.template_id)
    if not template or not template.current_version_id:
        raise api_error(422, 'TEMPLATE_UNPUBLISHED', 'Notebook模板尚未发布')
    from app.services.time_utils import as_utc
    if payload.deadline and payload.opens_at and as_utc(payload.deadline) <= as_utc(payload.opens_at):
        raise api_error(422, 'INVALID_WINDOW', '截止时间需晚于开放时间')
    if payload.assessment_id:
        assessment = _current(db, LabAssessment, payload.assessment_id)
        if not assessment or assessment.entry_key != f'{payload.entry_kind}:{entry.id}' or assessment.status != 'published' or assessment.template_version_id != template.current_version_id:
            raise api_error(422, 'ASSESSMENT_VERSION_MISMATCH', '需选择同入口同模板的已发布评测版本')
    row = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id,
        OfferingExperiment.entry_kind == payload.entry_kind, OfferingExperiment.entry_id == payload.entry_id)
        .with_for_update().execution_options(populate_existing=True))
    if row and db.scalar(select(ExperimentRecord.id).where(ExperimentRecord.offering_id == offering_id,
        (ExperimentRecord.lesson_id == payload.entry_id) if payload.entry_kind == 'lesson' else (ExperimentRecord.module_id == payload.entry_id))
        .limit(1).with_for_update()):
        if row.template_version_id != template.current_version_id or row.assessment_id != payload.assessment_id:
            raise api_error(409, 'EXPERIMENT_VERSION_LOCKED', '已有学生记录，变更评分版本请使用显式重评流程')
    if not row:
        row = OfferingExperiment(offering_id=offering_id, template_version_id=template.current_version_id, **payload.model_dump())
        db.add(row)
    else:
        for key, value in payload.model_dump().items():
            setattr(row, key, value)
        row.template_version_id = template.current_version_id
    audit(db, user.id, 'offering.experiment.release', 'course_offering', offering_id, changes=payload.model_dump(mode='json'))
    db.commit()
    return {'id': row.id, 'offering_id': row.offering_id, 'assessment_id': row.assessment_id, 'status': row.status}


@router.post('/offerings/{offering_id}/legacy-records/{record_id}/assign')
def assign_legacy(offering_id: int, record_id: int, payload: Reason, db=Depends(get_db), user=Depends(require_roles('admin'))):
    from app.models import LabRun
    lock_policy(db)
    offering = scope.require_offering(db, offering_id, user, write=True)
    record = db.scalar(select(ExperimentRecord).where(ExperimentRecord.id == record_id).with_for_update().execution_options(populate_existing=True))
    if not record or record.offering_id is not None:
        raise api_error(409, 'LEGACY_RECORD_REQUIRED', '只能确认未归属的历史记录')
    if db.scalar(select(LabRun.id).where(LabRun.record_id == record_id,
            LabRun.status.in_(['queued', 'running', 'recovering'])).limit(1)):
        raise api_error(409, 'HISTORY_SCOPE_BUSY', '历史实验正在评分或运行，完成后再确认归属')
    if record.lesson_id and db.get(Lesson, record.lesson_id).chapter.course_id != offering.course_id:
        raise api_error(422, 'RECORD_SCOPE_MISMATCH', '历史记录不属于本课程')
    kind, entry_id = ('lesson', record.lesson_id) if record.lesson_id else ('module', record.module_id)
    delivery = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id,
        OfferingExperiment.entry_kind == kind, OfferingExperiment.entry_id == entry_id)
        .with_for_update().execution_options(populate_existing=True))
    if delivery and delivery.pending_assessment_id:
        raise api_error(409, 'HISTORY_SCOPE_BUSY', '本班实验正在整批重评，完成后再确认历史归属')
    if record.module_id and not db.scalar(select(OfferingExperiment.id).where(
            OfferingExperiment.offering_id == offering_id, OfferingExperiment.entry_kind == 'module',
            OfferingExperiment.entry_id == record.module_id)):
        raise api_error(422, 'RECORD_SCOPE_MISMATCH', '请先确认该实验属于本授课实例')
    if not db.scalar(select(OfferingEnrollment.id).where(OfferingEnrollment.offering_id == offering_id, OfferingEnrollment.student_id == record.student_id)):
        raise api_error(422, 'STUDENT_NOT_IN_OFFERING', '需先确认学生的班级归属')
    record.offering_id, record.scope_key = offering_id, f'offering:{offering_id}'
    audit(db, user.id, 'record.scope.assign', 'experiment_record', record_id, reason=payload.reason, changes={'offering_id': offering_id})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, 'HISTORY_SCOPE_CONFLICT', '本班已存在该学生的实验记录，历史记录保留原归属')
    return {'ok': True}


@router.get('/offerings/{offering_id}/legacy-review')
def legacy_review(offering_id: int, paging: PaginationParams = Depends(pagination), db=Depends(get_db), user=Depends(require_roles('admin'))):
    """Historical facts remain unassigned until an administrator confirms them."""
    from app.models import ExamSubmission, JudgeQuestion, Submission
    offering = scope.require_offering(db, offering_id, user)
    lesson_ids = select(Lesson.id).join(Chapter).where(Chapter.course_id == offering.course_id)
    module_ids = select(OfferingExperiment.entry_id).where(OfferingExperiment.offering_id == offering_id,
        OfferingExperiment.entry_kind == 'module')
    previous = select(CourseOffering.id).where(CourseOffering.course_id == offering.course_id,
        CourseOffering.academic_term_id == offering.academic_term_id, CourseOffering.id != offering_id)
    active_members = select(OfferingEnrollment.student_id).where(OfferingEnrollment.offering_id == offering_id,
        OfferingEnrollment.status == 'active')
    query = select(ExperimentRecord).where(or_(ExperimentRecord.offering_id.is_(None),
        (ExperimentRecord.offering_id.in_(previous) & ExperimentRecord.student_id.in_(active_members))),
        or_(ExperimentRecord.lesson_id.in_(lesson_ids), ExperimentRecord.module_id.in_(module_ids))).order_by(ExperimentRecord.id)
    count = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    records = db.scalars(query.offset((paging.page - 1) * paging.page_size).limit(paging.page_size)).all()
    roster_ids = set(db.scalars(select(OfferingEnrollment.student_id).where(OfferingEnrollment.offering_id == offering_id)))
    result = []
    for record in records:
        student = db.get(User, record.student_id)
        entry = db.get(Lesson if record.lesson_id else ExperimentModule, record.lesson_id or record.module_id)
        result.append({'id': record.id, 'title': entry.title if record.lesson_id else entry.name,
            'student_id': record.student_id, 'student_no': student.student_no or student.username,
            'student_name': student.real_name, 'status': record.status,
            'template_version_id': record.template_version_id, 'source_offering_id': record.offering_id,
            'can_confirm': record.student_id in roster_ids})
    tasks, task_totals = [], {}
    for kind, model in [('assignment', Assignment), ('exam', Exam)]:
        participant_query = (select(Submission.student_id).join(JudgeQuestion).where(JudgeQuestion.assignment_id == model.id)
            if kind == 'assignment' else select(ExamSubmission.student_id).where(ExamSubmission.exam_id == model.id))
        task_query = select(model).where(model.course_id == offering.course_id, model.offering_id.is_(None),
            exists(participant_query)).order_by(model.id)
        task_totals[kind] = db.scalar(select(func.count()).select_from(task_query.subquery())) or 0
        # Task cards are bounded independently of the record page.
        for task in db.scalars(task_query.offset((paging.page - 1) * paging.page_size).limit(paging.page_size)):
            ids = (db.scalars(select(Submission.student_id).join(JudgeQuestion).where(JudgeQuestion.assignment_id == task.id)).all()
                if kind == 'assignment' else db.scalars(select(ExamSubmission.student_id).where(ExamSubmission.exam_id == task.id)).all())
            participants = set(ids)
            tasks.append({'id': task.id, 'kind': kind, 'title': task.title, 'participants': len(participants),
                'unmatched': len(participants - roster_ids), 'can_confirm': participants <= roster_ids})
    return {'items': result, 'total': count, 'page': paging.page, 'page_size': paging.page_size,
            'tasks': tasks, 'task_totals': task_totals}


@router.post('/offerings/{offering_id}/legacy-tasks/{kind}/{task_id}/assign')
def assign_legacy_task(offering_id: int, kind: Literal['assignment', 'exam'], task_id: int,
                       payload: Reason, db=Depends(get_db), user=Depends(require_roles('admin'))):
    from app.models import ExamSubmission, JudgeQuestion, Submission
    lock_policy(db)
    offering = scope.require_offering(db, offering_id, user, write=True)
    model = Assignment if kind == 'assignment' else Exam
    task = db.scalar(select(model).where(model.id == task_id).with_for_update().execution_options(populate_existing=True))
    if not task or task.offering_id is not None or task.course_id != offering.course_id:
        raise api_error(409, 'LEGACY_TASK_REQUIRED', '只能确认同课程未归属的历史任务')
    participant_query = (select(Submission.student_id).join(JudgeQuestion).where(JudgeQuestion.assignment_id == task.id)
        if kind == 'assignment' else select(ExamSubmission.student_id).where(ExamSubmission.exam_id == task.id))
    participants = set(db.scalars(participant_query))
    members = set(db.scalars(select(OfferingEnrollment.student_id).where(OfferingEnrollment.offering_id == offering_id)))
    if not participants or not participants <= members:
        raise api_error(409, 'HISTORY_SCOPE_AMBIGUOUS', '历史参与者未全部归属本班；请保留原记录并逐人核对，不能将跨班任务整体转入')
    task.offering_id = offering_id
    audit(db, user.id, 'task.scope.assign', kind, task_id, reason=payload.reason,
          changes={'offering_id': offering_id, 'participants': len(participants)})
    db.commit()
    return {'ok': True}


@router.post('/offerings/{offering_id}/records/{record_id}/transfer')
def transfer_record_grade(offering_id: int, record_id: int, payload: RecordTransfer,
                          db=Depends(get_db), user=Depends(require_roles('admin'))):
    from app.models import LabRecord, LabRun
    lock_policy(db)
    target = scope.require_offering(db, offering_id, user, write=True)
    source = scope.require_offering(db, payload.source_offering_id, user, write=True)
    if target.id == source.id or (target.course_id, target.academic_term_id) != (source.course_id, source.academic_term_id):
        raise api_error(422, 'TRANSFER_CONTEXT_MISMATCH', '成绩转入须在同课程同学期不同班级内')
    record = db.scalar(select(ExperimentRecord).where(ExperimentRecord.id == record_id).with_for_update().execution_options(populate_existing=True))
    if not record or record.offering_id != source.id:
        raise api_error(409, 'HISTORY_SCOPE_CHANGED', '历史成绩归属已改变，请刷新核对')
    if not db.scalar(select(OfferingEnrollment.id).where(OfferingEnrollment.offering_id == target.id,
            OfferingEnrollment.student_id == record.student_id, OfferingEnrollment.status == 'active')):
        raise api_error(422, 'STUDENT_NOT_IN_OFFERING', '学生需先办理转班')
    kind, entry_id = ('lesson', record.lesson_id) if record.lesson_id else ('module', record.module_id)
    deliveries = list(db.scalars(select(OfferingExperiment).where(OfferingExperiment.offering_id.in_([source.id, target.id]),
        OfferingExperiment.entry_kind == kind, OfferingExperiment.entry_id == entry_id)
        .order_by(OfferingExperiment.id).with_for_update().execution_options(populate_existing=True)))
    delivery = next((row for row in deliveries if row.offering_id == target.id), None)
    if any(row.pending_assessment_id for row in deliveries) or db.scalar(select(LabRun.id).where(
            LabRun.record_id == record.id, LabRun.status.in_(['queued', 'running', 'recovering'])).limit(1)):
        raise api_error(409, 'GRADE_TRANSFER_BUSY', '实验正在运行或整批重评，请完成核对后再转入成绩')
    binding = db.scalar(select(LabRecord).where(LabRecord.record_id == record.id).with_for_update())
    if (not delivery or delivery.status != 'published' or delivery.pending_assessment_id
            or delivery.template_version_id != record.template_version_id
            or delivery.assessment_id != (binding.assessment_id if binding else None)):
        raise api_error(409, 'GRADE_VERSION_MISMATCH', '两班模板或评分版本不一致，请人工核对成绩；原成绩保持原归属')
    record.offering_id, record.scope_key = target.id, f'offering:{target.id}'
    audit(db, user.id, 'record.grade.transfer', 'experiment_record', record.id, reason=payload.reason,
        changes={'from': source.id, 'to': target.id})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, 'HISTORY_SCOPE_CONFLICT', '目标班级已有本实验记录，不能覆盖')
    return {'ok': True}


@router.get('/offerings/{offering_id}/experiments/{kind}/{entry_id}/assessment')
def offering_assessment(offering_id: int, kind: Literal['lesson', 'module'], entry_id: int, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    from app.models import LabAssessment
    from app.services.lab_assessment_service import assessment_read
    scope.require_offering(db, offering_id, user)
    delivery = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id, OfferingExperiment.entry_kind == kind, OfferingExperiment.entry_id == entry_id))
    if not delivery:
        raise api_error(409, 'EXPERIMENT_RELEASE_REQUIRED', '请先选择共享评测版本并发布本班实验')
    key = f'offering:{offering_id}:{kind}:{entry_id}'
    rows = list(db.scalars(select(LabAssessment).where(LabAssessment.entry_key == key).order_by(LabAssessment.version.desc())))
    draft = next((r for r in rows if r.status == 'draft'), None)
    published = next((r for r in rows if r.status == 'published'), None) or (db.get(LabAssessment, delivery.assessment_id) if delivery.assessment_id else None)
    result = {'draft': assessment_read(draft, teacher=True), 'published': assessment_read(published, teacher=True)}
    if result['published'] and published.owner_id != user.id:
        result['published']['validation_run_id'] = None
    return result


from app.config import get_settings
from app.schemas.lab import LabAssessmentConfig


@router.post('/offerings/{offering_id}/experiments/{kind}/{entry_id}/assessment/check-script')
async def upload_offering_check_script(offering_id: int, kind: Literal['lesson', 'module'], entry_id: int,
        file: UploadFile = File(), db=Depends(get_db), user=Depends(require_roles('teacher', 'admin')),
        settings=Depends(get_settings)):
    from app.services.lab_check_script import MAX_CHECK_SCRIPT_BYTES, validate_check_script
    if not settings.lab_enabled:
        raise api_error(503, 'LAB_DISABLED', '实验训练与自动评测尚未启用')
    content = await file.read(MAX_CHECK_SCRIPT_BYTES + 1)
    # Check current authorization after the bounded read, before returning text.
    # This class route grants no right to mutate the shared content owner's rule.
    scope.require_offering(db, offering_id, user, write=True)
    delivery = db.scalar(select(OfferingExperiment).where(
        OfferingExperiment.offering_id == offering_id, OfferingExperiment.entry_kind == kind,
        OfferingExperiment.entry_id == entry_id).with_for_update().execution_options(populate_existing=True))
    if not delivery or delivery.status != 'published':
        raise api_error(409, 'EXPERIMENT_RELEASE_REQUIRED', '请先选择共享评测版本并发布本班实验')
    return {'script': validate_check_script(file.filename, content)}


@router.put('/offerings/{offering_id}/experiments/{kind}/{entry_id}/assessment')
def save_offering_assessment(offering_id: int, kind: Literal['lesson', 'module'], entry_id: int, payload: LabAssessmentConfig, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin')), settings=Depends(get_settings)):
    lock_policy(db)
    from app.models import LabAssessment, LabArtifact, LabDatasetVersion
    from app.services import lab_assessment_service as authoring
    scope.require_offering(db, offering_id, user, write=True)
    current = offering_assessment(offering_id, kind, entry_id, db, user)
    key = f'offering:{offering_id}:{kind}:{entry_id}'
    delivery = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id, OfferingExperiment.entry_kind == kind, OfferingExperiment.entry_id == entry_id).with_for_update().execution_options(populate_existing=True))
    draft = db.scalar(select(LabAssessment).where(LabAssessment.entry_key == key, LabAssessment.status == 'draft').with_for_update().execution_options(populate_existing=True))
    original = db.get(LabAssessment, delivery.assessment_id) if delivery.assessment_id else None
    config = authoring.bind_benchmark_config(payload.model_dump(), settings)
    for name, model in [('dataset_id', LabDatasetVersion), ('reference_artifact_id', LabArtifact)]:
        ident = config.get(name)
        if ident:
            resource = db.get(model, ident)
            if not resource or (user.role != 'admin' and resource.owner_id != user.id and (not original or original.config.get(name) != ident)):
                raise api_error(403, 'LAB_RESOURCE_FORBIDDEN', '不能引用未获授权的实验资源')
    if not draft:
        version = (db.scalar(select(func.max(LabAssessment.version)).where(LabAssessment.entry_key == key)) or 0) + 1
        draft = LabAssessment(entry_key=key, owner_id=user.id, version=version, status='draft', template_version_id=delivery.template_version_id)
        db.add(draft)
    new_hash = authoring.digest({'config': config, 'template_version_id': draft.template_version_id})
    if draft.config_hash != new_hash:
        draft.validated_hash, draft.validation_run_id = None, None
    draft.config, draft.config_hash = config, new_hash
    if original and original.config_hash == new_hash and original.validated_hash == original.config_hash:
        draft.validated_hash = new_hash
    db.flush()
    authoring.validate_configuration(db, draft, settings)
    audit(db, user.id, 'offering.assessment.save', 'lab_assessment', draft.id, changes={'offering_id': offering_id, 'version': draft.version})
    db.commit()
    return authoring.assessment_read(draft, teacher=True)


@router.post('/offerings/{offering_id}/experiments/{kind}/{entry_id}/assessment/baseline', status_code=202)
def offering_baseline(offering_id: int, kind: Literal['lesson', 'module'], entry_id: int, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin')), settings=Depends(get_settings)):
    lock_policy(db)
    from app.models import LabAssessment, LabRun
    from app.services import lab_assessment_service as authoring
    scope.require_offering(db, offering_id, user, write=True)
    delivery = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id, OfferingExperiment.entry_kind == kind, OfferingExperiment.entry_id == entry_id).with_for_update().execution_options(populate_existing=True))
    if not delivery:
        raise api_error(404, 'EXPERIMENT_RELEASE_REQUIRED', '实验尚未向本班发布')
    draft = db.scalar(select(LabAssessment).where(LabAssessment.entry_key == f'offering:{offering_id}:{kind}:{entry_id}', LabAssessment.status == 'draft').with_for_update())
    if not draft:
        raise api_error(404, 'LAB_DRAFT_REQUIRED', '请先保存本班评分配置')
    authoring.validate_configuration(db, draft, settings)
    old = db.get(LabRun, draft.validation_run_id) if draft.validation_run_id else None
    if old and old.status in ('queued', 'running', 'recovering') and old.snapshot.get('assessment_hash') == draft.config_hash:
        return authoring.run_read(old, teacher=True)
    run = authoring.create_run(db, owner_id=user.id, kind='baseline', assessment=draft, artifact_id=draft.config.get('reference_artifact_id'), snapshot={'cells': draft.config.get('reference_cells', [])})
    draft.validation_run_id = run.id
    db.commit()
    return authoring.run_read(run, teacher=True)


@router.post('/offerings/{offering_id}/experiments/{kind}/{entry_id}/assessment/publish')
def publish_offering_assessment(offering_id: int, kind: Literal['lesson', 'module'], entry_id: int, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin')), settings=Depends(get_settings)):
    lock_policy(db)
    from app.models import LabAssessment, LabRecord
    from app.services import lab_assessment_service as authoring
    scope.require_offering(db, offering_id, user, write=True)
    delivery = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id, OfferingExperiment.entry_kind == kind, OfferingExperiment.entry_id == entry_id).with_for_update().execution_options(populate_existing=True))
    if not delivery:
        raise api_error(404, 'EXPERIMENT_RELEASE_REQUIRED', '实验尚未向本班发布')
    draft = db.scalar(select(LabAssessment).where(LabAssessment.entry_key == f'offering:{offering_id}:{kind}:{entry_id}', LabAssessment.status == 'draft').with_for_update())
    if not draft:
        raise api_error(404, 'LAB_DRAFT_REQUIRED', '请先保存本班评分配置')
    authoring.validate_configuration(db, draft, settings, publishing=True)
    delivery = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id, OfferingExperiment.entry_kind == kind, OfferingExperiment.entry_id == entry_id).with_for_update())
    has_records = bool(db.scalar(select(ExperimentRecord.id).join(LabRecord, LabRecord.record_id == ExperimentRecord.id).where(ExperimentRecord.offering_id == offering_id, (ExperimentRecord.lesson_id == entry_id) if kind == 'lesson' else (ExperimentRecord.module_id == entry_id))))
    draft.status, draft.published_at = 'published', datetime.now(timezone.utc)
    if has_records:
        delivery.pending_assessment_id = draft.id
    else:
        delivery.assessment_id = draft.id
    audit(db, user.id, 'offering.assessment.publish', 'lab_assessment', draft.id, changes={'offering_id': offering_id})
    db.commit()
    return authoring.assessment_read(draft, teacher=True)


from app.schemas.lab import LabRegradeRequest


@router.post('/offerings/{offering_id}/experiments/{kind}/{entry_id}/assessment/regrade', status_code=202)
def regrade_offering(offering_id: int, kind: Literal['lesson', 'module'], entry_id: int, payload: LabRegradeRequest, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    lock_policy(db)
    from app.models import LabAssessment, LabRecord, LabSubmission, LabRegradeBatch, ExperimentSubmission
    from app.services import lab_submission_service as submissions
    from app.services.lab_grading import lock_lab_records
    scope.require_offering(db, offering_id, user, write=True)
    delivery = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == offering_id, OfferingExperiment.entry_kind == kind, OfferingExperiment.entry_id == entry_id))
    target = db.get(LabAssessment, delivery.pending_assessment_id) if delivery and delivery.pending_assessment_id else None
    if not target or target.status != 'published':
        raise api_error(409, 'LAB_TARGET_REQUIRED', '请先发布新的本班评分版本')
    records = list(db.scalars(select(ExperimentRecord).join(LabRecord, LabRecord.record_id == ExperimentRecord.id).where(ExperimentRecord.offering_id == offering_id,
        (ExperimentRecord.lesson_id == entry_id) if kind == 'lesson' else (ExperimentRecord.module_id == entry_id))))
    lock_lab_records(db, [r.id for r in records])
    delivery = db.scalar(select(OfferingExperiment).where(OfferingExperiment.id == delivery.id).with_for_update().execution_options(populate_existing=True))
    if delivery.pending_assessment_id != target.id:
        raise api_error(409, 'LAB_TARGET_CHANGED', '目标评分版本已变化，请刷新后重试')
    if any(r.template_version_id != target.template_version_id for r in records):
        raise api_error(422, 'LAB_TEMPLATE_INCOMPATIBLE', '历史提交必须使用相同模板版本')
    attempts = list(db.scalars(select(ExperimentSubmission).join(LabSubmission, LabSubmission.submission_id == ExperimentSubmission.id).where(ExperimentSubmission.record_id.in_([r.id for r in records])).with_for_update()))
    if not attempts or set(payload.submission_ids) != {s.id for s in attempts}:
        raise api_error(422, 'LAB_FULL_HISTORY_REQUIRED', '必须包含本班该实验的全部正式提交')
    active = db.scalar(select(LabRegradeBatch.id).where(LabRegradeBatch.offering_id == offering_id, LabRegradeBatch.status == 'running'))
    if active:
        raise api_error(409, 'LAB_REGRADE_RUNNING', '本班已有重评正在执行')
    batch = LabRegradeBatch(offering_id=offering_id, assessment_id=target.id, owner_id=user.id, reason=payload.reason, submission_ids=sorted(payload.submission_ids))
    db.add(batch); db.flush()
    for attempt in attempts:
        submissions.enqueue_regrade(db, attempt, user, reason=payload.reason, assessment=target, batch_id=batch.id)
    audit(db, user.id, 'offering.assessment.regrade', 'course_offering', offering_id, reason=payload.reason, changes={'batch_id': batch.id, 'assessment_id': target.id})
    db.commit()
    return {'id': batch.id, 'status': batch.status, 'submission_ids': batch.submission_ids}
