from typing import Literal
from pydantic import BaseModel, Field
from app.services.account_service import audit, lock_policy
from app.services.time_utils import utc_now

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dependencies import PaginationParams, get_current_user, get_db, pagination, require_roles
from app.errors import api_error
from app.models import AcademicTerm, Course, CourseTeachingClass, TeachingClass, TeachingClassStudent, User
from app.schemas import (
    AcademicTermCreate, AcademicTermRead, AcademicTermUpdate, PaginatedResponse,
    TeachingClassCreate, TeachingClassStudentBatch, TeachingClassSummary,
    TeachingClassUpdate, UserRead,
)
from app.services.roster_service import sync_courses_for_class

router = APIRouter(tags=["academics"])


def _term_writable(term: AcademicTerm) -> None:
    if term.status == "closed":
        raise api_error(409, "ACADEMIC_TERM_CLOSED", "已关闭学期不可修改教务数据")


def _class_summary(db: Session, teaching_class: TeachingClass) -> TeachingClassSummary:
    count = db.scalar(select(func.count()).select_from(TeachingClassStudent).where(
        TeachingClassStudent.teaching_class_id == teaching_class.id,
        TeachingClassStudent.status == "active",
    )) or 0
    data = TeachingClassSummary.model_validate(teaching_class).model_dump()
    data["student_count"] = count
    return TeachingClassSummary.model_validate(data)


@router.get("/academic-terms", response_model=PaginatedResponse)
def list_terms(pagination: PaginationParams = Depends(pagination), db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    page, page_size = pagination.page, pagination.page_size
    query = select(AcademicTerm).order_by(AcademicTerm.start_date.desc(), AcademicTerm.id.desc())
    total = db.scalar(select(func.count()).select_from(AcademicTerm)) or 0
    rows = db.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    return PaginatedResponse(items=[AcademicTermRead.model_validate(x) for x in rows], page=page, page_size=page_size, total=total)


@router.post("/academic-terms", response_model=AcademicTermRead, status_code=status.HTTP_201_CREATED)
def create_term(payload: AcademicTermCreate, db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    lock_policy(db)
    term = AcademicTerm(**payload.model_dump())
    db.add(term)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, "ACADEMIC_TERM_CODE_EXISTS", "学期编码已存在")
    db.refresh(term)
    return term


@router.patch("/academic-terms/{term_id}", response_model=AcademicTermRead)
def update_term(term_id: int, payload: AcademicTermUpdate, db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    lock_policy(db)
    term = db.get(AcademicTerm, term_id)
    if not term:
        raise api_error(404, "ACADEMIC_TERM_NOT_FOUND", "学期不存在")
    updates = payload.model_dump(exclude_unset=True)
    if term.status == "closed":
        _term_writable(term)
    start = updates.get("start_date", term.start_date)
    end = updates.get("end_date", term.end_date)
    if end < start:
        raise api_error(422, "INVALID_TERM_DATES", "学期结束日期不能早于开始日期")
    audit(db, _.id, 'term.update', 'academic_term', term_id, changes=updates)
    for key, value in updates.items():
        setattr(term, key, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, "ACADEMIC_TERM_CODE_EXISTS", "学期编码已存在")
    db.refresh(term)
    return term


@router.delete("/academic-terms/{term_id}", response_model=AcademicTermRead)
def close_term(term_id: int, db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    lock_policy(db)
    term = db.get(AcademicTerm, term_id)
    if not term:
        raise api_error(404, "ACADEMIC_TERM_NOT_FOUND", "学期不存在")
    audit(db, _.id, "term.close", "academic_term", term_id)
    term.status = "closed"
    db.commit(); db.refresh(term)
    return term


def _teacher_class_scope(current_user: User):
    """教师只能查看与自己课程绑定的教学班；admin 返回 None 表示不限。"""
    if current_user.role == "admin":
        return None
    if current_user.role == "teacher":
        return (
            select(TeachingClass.id)
            .join(CourseTeachingClass, CourseTeachingClass.teaching_class_id == TeachingClass.id)
            .join(Course, Course.id == CourseTeachingClass.course_id)
            .where(Course.teacher_id == current_user.id)
        )
    return select(TeachingClass.id).where(TeachingClass.id == -1)


@router.get("/teaching-classes", response_model=PaginatedResponse)
def list_classes(academic_term_id: int | None = None, q: str | None = None,
                 scope: Literal["selectable", "linked"] = "selectable",
                 pagination: PaginationParams = Depends(pagination),
                 db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    page, page_size = pagination.page, pagination.page_size
    filters = []
    if scope == "linked":
        scope_ids = _teacher_class_scope(current_user)
        if scope_ids is not None:
            filters.append(TeachingClass.id.in_(scope_ids))
    if academic_term_id is not None:
        filters.append(TeachingClass.academic_term_id == academic_term_id)
    if q:
        like = f"%{q}%"
        filters.append(or_(TeachingClass.code.ilike(like), TeachingClass.name.ilike(like)))
    query = select(TeachingClass).where(*filters)
    total = db.scalar(select(func.count()).select_from(TeachingClass).where(*filters)) or 0
    rows = db.scalars(query.order_by(TeachingClass.id).offset((page - 1) * page_size).limit(page_size)).all()
    return PaginatedResponse(items=[_class_summary(db, row) for row in rows], page=page, page_size=page_size, total=total)


@router.post("/teaching-classes", response_model=TeachingClassSummary, status_code=status.HTTP_201_CREATED)
def create_class(payload: TeachingClassCreate, db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    lock_policy(db)
    term = db.get(AcademicTerm, payload.academic_term_id)
    if not term:
        raise api_error(404, "ACADEMIC_TERM_NOT_FOUND", "学期不存在")
    _term_writable(term)
    row = TeachingClass(**payload.model_dump())
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, "TEACHING_CLASS_CODE_EXISTS", "该学期班级编码已存在")
    db.refresh(row)
    return _class_summary(db, row)


@router.patch("/teaching-classes/{class_id}", response_model=TeachingClassSummary)
def update_class(class_id: int, payload: TeachingClassUpdate, db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    lock_policy(db)
    row = db.scalar(select(TeachingClass).where(TeachingClass.id == class_id).execution_options(populate_existing=True))
    if not row:
        raise api_error(404, "TEACHING_CLASS_NOT_FOUND", "教学班不存在")
    _term_writable(row.academic_term)
    if row.status == 'archived':
        raise api_error(409, 'TEACHING_CLASS_ARCHIVED', '已归档教学班不可修改')
    audit(db, _.id, 'class.update', 'teaching_class', class_id, changes=payload.model_dump(exclude_unset=True))
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, "TEACHING_CLASS_CODE_EXISTS", "该学期班级编码已存在")
    db.refresh(row)
    return _class_summary(db, row)


@router.delete("/teaching-classes/{class_id}", response_model=TeachingClassSummary)
def archive_class(class_id: int, db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    lock_policy(db)
    row = db.scalar(select(TeachingClass).where(TeachingClass.id == class_id).execution_options(populate_existing=True))
    if not row:
        raise api_error(404, "TEACHING_CLASS_NOT_FOUND", "教学班不存在")
    _term_writable(row.academic_term)
    audit(db, _.id, "class.archive", "teaching_class", class_id)
    row.status = "archived"
    db.commit(); db.refresh(row)
    return _class_summary(db, row)


def _query_class_students(
    class_id: int,
    db: Session,
    page: int,
    page_size: int,
    q: str | None = None,
) -> PaginatedResponse:
    """教学班学生名单分页查询（纯函数——供路由与内部调用复用，不依赖 FastAPI DI）。"""
    row = db.scalar(select(TeachingClass).where(TeachingClass.id == class_id).execution_options(populate_existing=True))
    if not row:
        raise api_error(404, "TEACHING_CLASS_NOT_FOUND", "教学班不存在")
    filters = [TeachingClassStudent.teaching_class_id == class_id, TeachingClassStudent.status == "active"]
    if q and q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(User.student_no.ilike(like), User.username.ilike(like), User.real_name.ilike(like)))
    total = db.scalar(
        select(func.count())
        .select_from(TeachingClassStudent)
        .join(User, TeachingClassStudent.student_id == User.id)
        .where(*filters)
    ) or 0
    students = db.scalars(select(User).join(TeachingClassStudent, TeachingClassStudent.student_id == User.id)
        .where(*filters).order_by(User.student_no, User.id).offset((page - 1) * page_size).limit(page_size)).all()
    return PaginatedResponse(items=[UserRead.model_validate(x) for x in students], page=page, page_size=page_size, total=total)


def _ensure_class_visible_to_teacher(db: Session, class_id: int, current_user: User) -> None:
    if current_user.role == "admin":
        return
    if current_user.role == "teacher":
        linked = db.scalar(
            select(Course.id)
            .join(CourseTeachingClass, CourseTeachingClass.course_id == Course.id)
            .where(
                CourseTeachingClass.teaching_class_id == class_id,
                Course.teacher_id == current_user.id,
            )
            .limit(1)
        )
        if linked is not None:
            return
    raise api_error(403, "FORBIDDEN", "没有权限查看该教学班名单")


@router.get("/teaching-classes/{class_id}/students", response_model=PaginatedResponse)
def list_class_students(
    class_id: int,
    q: str | None = None,
    pagination: PaginationParams = Depends(pagination),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "teacher")),
):
    page, page_size = pagination.page, pagination.page_size
    _ensure_class_visible_to_teacher(db, class_id, current_user)
    return _query_class_students(class_id, db, page, page_size, q=q)


@router.post("/teaching-classes/{class_id}/students", response_model=PaginatedResponse)
def add_class_students(class_id: int, payload: TeachingClassStudentBatch, db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    lock_policy(db)
    row = db.scalar(select(TeachingClass).where(TeachingClass.id == class_id).execution_options(populate_existing=True))
    if not row:
        raise api_error(404, "TEACHING_CLASS_NOT_FOUND", "教学班不存在")
    _term_writable(row.academic_term)
    if row.status == 'archived':
        raise api_error(409, 'TEACHING_CLASS_ARCHIVED', '已归档教学班不可修改')
    students = db.scalars(select(User).where(User.id.in_(payload.student_ids), User.role == "student", User.status == "active")).all()
    if len(students) != len(set(payload.student_ids)):
        raise api_error(422, "INVALID_STUDENTS", "名单包含不存在或不可用的学生")
    existing = {x.student_id: x for x in db.scalars(select(TeachingClassStudent).where(
        TeachingClassStudent.teaching_class_id == class_id,
        TeachingClassStudent.student_id.in_(payload.student_ids),
    )).all()}
    for student_id in set(payload.student_ids):
        if student_id in existing:
            if existing[student_id].status != "active":
                existing[student_id].status = "active"
                existing[student_id].joined_at = utc_now()
                existing[student_id].removed_at = None
        else:
            db.add(TeachingClassStudent(teaching_class_id=class_id, student_id=student_id, status="active", joined_at=utc_now()))
    audit(db, _.id, 'class.members_change', 'teaching_class', class_id)
    db.flush(); sync_courses_for_class(db, class_id); db.commit()
    return _query_class_students(class_id, db, page=1, page_size=20)


@router.delete("/teaching-classes/{class_id}/students/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_class_student(class_id: int, student_id: int, db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    lock_policy(db)
    row = db.scalar(select(TeachingClass).where(TeachingClass.id == class_id).execution_options(populate_existing=True))
    if not row:
        raise api_error(404, "TEACHING_CLASS_NOT_FOUND", "教学班不存在")
    _term_writable(row.academic_term)
    if row.status == 'archived':
        raise api_error(409, 'TEACHING_CLASS_ARCHIVED', '已归档教学班不可修改')
    membership = db.scalar(select(TeachingClassStudent).where(
        TeachingClassStudent.teaching_class_id == class_id,
        TeachingClassStudent.student_id == student_id,
    ))
    if not membership:
        raise api_error(404, "CLASS_STUDENT_NOT_FOUND", "学生不在该教学班")
    membership.status = "removed"
    membership.removed_at = utc_now()
    audit(db, _.id, 'class.members_change', 'teaching_class', class_id)
    db.flush(); sync_courses_for_class(db, class_id); db.commit()
    return None


class AcademicAction(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class TransferRequest(AcademicAction):
    target_class_id: int
    student_ids: list[int] = Field(min_length=1, max_length=5000)


@router.post('/academic-terms/{term_id}/reopen', response_model=AcademicTermRead)
def reopen_term(term_id: int, payload: AcademicAction, db: Session = Depends(get_db),
                actor: User = Depends(require_roles('admin'))):
    lock_policy(db)
    term = db.get(AcademicTerm, term_id)
    if not term:
        raise api_error(404, 'ACADEMIC_TERM_NOT_FOUND', '学期不存在')
    term.status = 'active'
    audit(db, actor.id, 'term.reopen', 'academic_term', term_id, reason=payload.reason)
    db.commit()
    return term


@router.post('/teaching-classes/{class_id}/restore', response_model=TeachingClassSummary)
def restore_class(class_id: int, payload: AcademicAction, db: Session = Depends(get_db),
                  actor: User = Depends(require_roles('admin'))):
    lock_policy(db)
    row = db.scalar(select(TeachingClass).where(TeachingClass.id == class_id).execution_options(populate_existing=True))
    if not row:
        raise api_error(404, 'TEACHING_CLASS_NOT_FOUND', '教学班不存在')
    _term_writable(row.academic_term)
    row.status = 'active'
    audit(db, actor.id, 'class.restore', 'teaching_class', class_id, reason=payload.reason)
    db.commit()
    return _class_summary(db, row)


@router.post('/teaching-classes/{class_id}/transfer')
def transfer_students(class_id: int, payload: TransferRequest, db: Session = Depends(get_db),
                      actor: User = Depends(require_roles('admin'))):
    lock_policy(db)
    source, target = db.get(TeachingClass, class_id), db.get(TeachingClass, payload.target_class_id)
    if not source or not target or source.id == target.id:
        raise api_error(422, 'INVALID_TRANSFER', '请选择不同的有效教学班')
    for row in (source, target):
        _term_writable(row.academic_term)
        if row.status != 'active':
            raise api_error(409, 'TEACHING_CLASS_ARCHIVED', '归档班级不可转班')
    if source.academic_term_id != target.academic_term_id:
        raise api_error(422, 'CROSS_TERM_TRANSFER', '转班必须在同一学期内')
    ids = set(payload.student_ids)
    members = db.scalars(select(TeachingClassStudent).where(
        TeachingClassStudent.teaching_class_id == class_id,
        TeachingClassStudent.student_id.in_(ids), TeachingClassStudent.status == 'active')).all()
    if len(members) != len(ids):
        raise api_error(409, 'ROSTER_CHANGED', '源班级名单已变化')
    for member in members:
        member.status, member.removed_at = 'removed', utc_now()
        link = db.scalar(select(TeachingClassStudent).where(
            TeachingClassStudent.teaching_class_id == target.id, TeachingClassStudent.student_id == member.student_id))
        if link is None:
            link = TeachingClassStudent(teaching_class_id=target.id, student_id=member.student_id)
            db.add(link)
        link.status, link.joined_at, link.removed_at = 'active', utc_now(), None
        audit(db, actor.id, 'class.transfer', 'user', member.student_id, reason=payload.reason,
              changes={'from_class_id': class_id, 'to_class_id': target.id})
    db.flush()
    for row in (source, target):
        sync_courses_for_class(db, row.id)
    db.commit()
    return {'transferred': len(ids)}
