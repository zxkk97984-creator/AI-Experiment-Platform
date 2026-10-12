from typing import Literal

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select

from app.api.courses import ensure_course_manager, require_course
from app.dependencies import get_current_user, get_db, require_roles
from app.errors import api_error
from app.models import AcademicTerm, Course, TeachingClass
from app.models.gradebook import GradebookSnapshot
from app.models.teaching import CourseOffering, OfferingEnrollment
from app.schemas.gradebook import AdjustmentUpdate, ConfigUpdate, CourseTemplateUpdate, EntriesUpdate, RevisionAction
from app.services import gradebook_service as grades
from app.services.account_service import audit, lock_policy
from app.services.offering_service import require_offering

router = APIRouter(prefix='/gradebooks', tags=['gradebooks'])


def template_payload(course):
    return {'course_id': course.id, 'customized': course.default_gradebook_config is not None,
            'categories': grades.course_template(course)}


@router.get('/courses/{course_id}/default-config')
def read_course_template(course_id: int, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    course = require_course(course_id, db)
    ensure_course_manager(course, user)
    return template_payload(course)


@router.put('/courses/{course_id}/default-config')
def save_course_template(course_id: int, payload: CourseTemplateUpdate, db=Depends(get_db),
                         user=Depends(require_roles('teacher', 'admin'))):
    # 课程模板只影响之后新开的班：已开班成绩册在首次建册时已复制，不在本请求里回溯。
    lock_policy(db)
    course = require_course(course_id, db)
    ensure_course_manager(course, user)
    course.default_gradebook_config = [
        {'key': item.key, 'title': item.title, 'weight': str(item.weight), 'weight_mode': item.weight_mode}
        for item in payload.categories]
    audit(db, user.id, 'gradebook.course_template', 'course', course.id,
          changes={'categories': [item.key for item in payload.categories]})
    db.commit()
    return template_payload(course)


@router.get('/offerings')
def list_offerings(db=Depends(get_db), user=Depends(get_current_user)):
    query = select(CourseOffering).order_by(CourseOffering.id.desc())
    if user.role == 'teacher': query = query.where(CourseOffering.teacher_id == user.id, CourseOffering.status.in_(['active', 'archived']))
    elif user.role == 'student': query = query.join(OfferingEnrollment).where(OfferingEnrollment.student_id == user.id).distinct()
    items = []
    for row in db.scalars(query):
        course, klass, term = db.get(Course, row.course_id), db.get(TeachingClass, row.teaching_class_id), db.get(AcademicTerm, row.academic_term_id)
        items.append({'id': row.id, 'course_title': course.title, 'class_name': klass.name, 'term_name': term.name,
                      'status': row.status, 'read_only': row.status != 'active' or term.status == 'closed' or klass.status == 'archived'})
    return {'items': items}


@router.get('/offerings/{offering_id}')
def read(offering_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    return grades.read_book(db, offering_id, user)


@router.put('/offerings/{offering_id}/config')
def configure(offering_id: int, payload: ConfigUpdate, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    grades.save_config(db, offering_id, user, payload)
    db.commit()
    return grades.read_book(db, offering_id, user)


@router.post('/offerings/{offering_id}/config/restore-default')
def restore_default(offering_id: int, payload: RevisionAction, db=Depends(get_db),
                    user=Depends(require_roles('teacher', 'admin'))):
    grades.restore_course_default(db, offering_id, user, payload)
    db.commit()
    return grades.read_book(db, offering_id, user)


@router.get('/offerings/{offering_id}/binding-options')
def binding_options(offering_id: int, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    # 发布表单只需要分类，不必触发整册草稿计算（含名单与来源预载）。
    require_offering(db, offering_id, user)
    book = grades.book_for(db, offering_id)
    config = grades.config_read(db, book) if book else grades.book_config_for(db, offering_id)
    return {'book_revision': book.revision if book else 0,
            'categories': [{'key': row['key'], 'title': row['title'], 'weight_mode': row['weight_mode']}
                           for row in config]}


@router.patch('/offerings/{offering_id}/entries')
def entries(offering_id: int, payload: EntriesUpdate, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    grades.update_entries(db, offering_id, user, payload)
    db.commit()
    return grades.read_book(db, offering_id, user)


@router.post('/offerings/{offering_id}/adjustment')
def adjustment(offering_id: int, payload: AdjustmentUpdate, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    grades.adjust(db, offering_id, user, payload)
    db.commit()
    return grades.read_book(db, offering_id, user)


@router.post('/offerings/{offering_id}/publish', status_code=201)
def publish(offering_id: int, payload: RevisionAction, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    snapshot = grades.publish(db, offering_id, user, payload)
    db.commit()
    return grades.snapshot_read(db, snapshot, user)


@router.post('/offerings/{offering_id}/archive', status_code=201)
def archive(offering_id: int, payload: RevisionAction, db=Depends(get_db), user=Depends(require_roles('teacher', 'admin'))):
    snapshot = grades.archive(db, offering_id, user, payload)
    db.commit()
    return grades.snapshot_read(db, snapshot, user)


def require_snapshot(db, snapshot_id):
    snapshot = db.get(GradebookSnapshot, snapshot_id)
    if not snapshot: raise api_error(404, 'GRADEBOOK_SNAPSHOT_NOT_FOUND', '成绩版本不存在')
    return snapshot


@router.get('/offerings/{offering_id}/snapshots')
def snapshots(offering_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    book = grades.book_for(db, offering_id)
    if user.role != 'student' or not book: require_offering(db, offering_id, user)
    rows = db.scalars(select(GradebookSnapshot).where(GradebookSnapshot.gradebook_id == book.id)
                      .order_by(GradebookSnapshot.revision.desc())).all() if book else []
    if user.role == 'student': rows = [r for r in rows if any(x['student_id'] == user.id for x in r.payload['rows'])]
    return {'items': [{'id': r.id, 'revision': r.revision, 'kind': r.kind, 'created_at': r.created_at, 'reason': r.reason} for r in rows]}


@router.get('/snapshots/{snapshot_id}')
def snapshot(snapshot_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    return grades.snapshot_read(db, require_snapshot(db, snapshot_id), user)


@router.get('/snapshots/{snapshot_id}/export')
def export(snapshot_id: str, format: Literal['xlsx', 'csv'] = 'xlsx', db=Depends(get_db), user=Depends(get_current_user)):
    result = grades.export_snapshot(db, require_snapshot(db, snapshot_id), user, format)
    db.commit()
    media = 'text/csv; charset=utf-8' if format == 'csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    return Response(result, media_type=media, headers={'Content-Disposition': f'attachment; filename="course-grades.{format}"',
                    'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})
