"""作业 / 考试发布范围服务。

范围模型：
    effective_students = (基础范围 - 排除学生) ∪ 白名单学生
基础范围：
    all_enrolled       课程内全部在册学生
    selected_classes   课程已绑定的多个教学班
    whitelist_only     仅白名单学生
"""

from __future__ import annotations

import csv
import io

from sqlalchemy import and_, exists, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.errors import api_error
from app.models import (
    Assignment, AssignmentAudienceClass, AssignmentAudienceStudent,
    Course, CourseEnrollment, CourseTeachingClass, Exam, ExamAudienceClass,
    ExamAudienceStudent, TeachingClass, TeachingClassStudent, User,
)

ALL_ENROLLED = "all_enrolled"
SELECTED_CLASSES = "selected_classes"
WHITELIST_ONLY = "whitelist_only"
VALID_MODES = (ALL_ENROLLED, SELECTED_CLASSES, WHITELIST_ONLY)


def _link_models(task_type: str):
    if task_type == "assignment":
        return AssignmentAudienceClass, AssignmentAudienceStudent
    if task_type == "exam":
        return ExamAudienceClass, ExamAudienceStudent
    raise ValueError(f"unknown task_type: {task_type}")


def _task_model(db: Session, task_type: str, task_id: int, *, current: bool = False):
    model = Assignment if task_type == "assignment" else Exam
    task = (db.scalar(select(model).where(model.id == task_id).with_for_update()
                      .execution_options(populate_existing=True)) if current else db.get(model, task_id))
    if task is None:
        raise api_error(404, "TASK_NOT_FOUND", "任务不存在")
    return task


def _dedupe(ids: list[int] | None) -> list[int]:
    return list(dict.fromkeys(int(i) for i in (ids or [])))


def validate_student_ids(db: Session, student_ids: list[int]) -> None:
    ids = _dedupe(student_ids)
    if not ids:
        return
    students = db.scalars(
        select(User).where(User.id.in_(ids), User.role == "student", User.status == "active")
    ).all()
    found = {student.id for student in students}
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise api_error(422, "INVALID_AUDIENCE_STUDENTS", "包含不存在或不可用的学生 ID：" + "、".join(missing))


def _validate_offering_audience(db: Session, task, class_ids, include_ids, exclude_ids) -> None:
    """Check the task boundary before querying globally identifiable users/classes."""
    if task.offering_id is None:
        return
    from app.models.teaching import CourseOffering, OfferingEnrollment
    offering = db.get(CourseOffering, task.offering_id)
    if not offering or offering.course_id != task.course_id:
        raise api_error(422, "AUDIENCE_SCOPE_INVALID", "任务授课范围无效")
    if any(ident != offering.teaching_class_id for ident in _dedupe(class_ids)):
        raise api_error(422, "AUDIENCE_SCOPE_INVALID", "只能选择本任务所属教学班")
    selected = set(_dedupe(include_ids)) | set(_dedupe(exclude_ids))
    if selected:
        allowed = set(db.scalars(select(OfferingEnrollment.student_id).where(
            OfferingEnrollment.offering_id == task.offering_id,
            OfferingEnrollment.status == "active",
            OfferingEnrollment.student_id.in_(selected),
        )))
        if selected != allowed:
            # An unknown ID and an ID in another class receive the same error.
            raise api_error(422, "AUDIENCE_SCOPE_INVALID", "只能选择本授课班在册学生")


def validate_audience(
    db: Session,
    course: Course,
    *,
    audience_mode: str,
    audience_class_ids: list[int] | None,
    whitelist_student_ids: list[int] | None,
    excluded_student_ids: list[int] | None,
) -> None:
    if audience_mode not in VALID_MODES:
        raise api_error(422, "INVALID_AUDIENCE_MODE", "发布范围模式无效")
    class_ids = _dedupe(audience_class_ids)
    include_ids = _dedupe(whitelist_student_ids)
    exclude_ids = _dedupe(excluded_student_ids)

    overlap = sorted(set(include_ids) & set(exclude_ids))
    if overlap:
        raise api_error(422, "AUDIENCE_STUDENT_CONFLICT", "同一学生不能同时加入白名单与排除名单")

    if class_ids:
        classes = db.scalars(
            select(TeachingClass).where(
                TeachingClass.id.in_(class_ids),
                TeachingClass.status == "active",
            )
        ).all()
        found = {row.id for row in classes}
        missing = [str(i) for i in class_ids if i not in found]
        if missing:
            raise api_error(422, "INVALID_AUDIENCE_CLASSES", "包含不存在或已归档的教学班 ID：" + "、".join(missing))
        linked = set(
            db.scalars(
                select(CourseTeachingClass.teaching_class_id).where(
                    CourseTeachingClass.course_id == course.id,
                    CourseTeachingClass.teaching_class_id.in_(class_ids),
                )
            ).all()
        )
        not_linked = [str(i) for i in class_ids if i not in linked]
        if not_linked:
            raise api_error(422, "CLASS_NOT_IN_COURSE", "以下教学班未绑定当前课程：" + "、".join(not_linked))

    validate_student_ids(db, include_ids)
    validate_student_ids(db, exclude_ids)


def save_audience(
    db: Session,
    *,
    task_type: str,
    task_id: int,
    course: Course,
    audience_mode: str,
    audience_class_ids: list[int] | None,
    whitelist_student_ids: list[int] | None,
    excluded_student_ids: list[int] | None,
    actor_id: int | None = None,
) -> None:
    task = _task_model(db, task_type, task_id)
    _validate_offering_audience(db, task, audience_class_ids, whitelist_student_ids, excluded_student_ids)
    validate_audience(
        db, course, audience_mode=audience_mode,
        audience_class_ids=audience_class_ids,
        whitelist_student_ids=whitelist_student_ids,
        excluded_student_ids=excluded_student_ids,
    )
    class_model, _student_model = _link_models(task_type)
    # 删除旧范围
    if task_type == "assignment":
        old_classes = db.scalars(select(AssignmentAudienceClass).where(AssignmentAudienceClass.assignment_id == task.id)).all()
        old_students = db.scalars(select(AssignmentAudienceStudent).where(AssignmentAudienceStudent.assignment_id == task.id)).all()
    else:
        old_classes = db.scalars(select(ExamAudienceClass).where(ExamAudienceClass.exam_id == task.id)).all()
        old_students = db.scalars(select(ExamAudienceStudent).where(ExamAudienceStudent.exam_id == task.id)).all()
    for row in old_classes:
        db.delete(row)
    for row in old_students:
        db.delete(row)
    db.flush()

    task.audience_mode = audience_mode
    for class_id in _dedupe(audience_class_ids):
        if task_type == "exam":
            db.add(ExamAudienceClass(exam_id=task_id, teaching_class_id=class_id))
        else:
            db.add(AssignmentAudienceClass(assignment_id=task_id, teaching_class_id=class_id))
    for student_id in _dedupe(whitelist_student_ids):
        if task_type == "exam":
            db.add(ExamAudienceStudent(exam_id=task_id, student_id=student_id, kind="include", created_by_id=actor_id))
        else:
            db.add(AssignmentAudienceStudent(assignment_id=task_id, student_id=student_id, kind="include", created_by_id=actor_id))
    for student_id in _dedupe(excluded_student_ids):
        if task_type == "exam":
            db.add(ExamAudienceStudent(exam_id=task_id, student_id=student_id, kind="exclude", created_by_id=actor_id))
        else:
            db.add(AssignmentAudienceStudent(assignment_id=task_id, student_id=student_id, kind="exclude", created_by_id=actor_id))
    db.flush()


def effective_student_ids(db: Session, *, task_type: str, task_id: int, course: Course,
                          current: bool = False) -> set[int]:
    # Write admission calls this while holding the policy lock. FOR UPDATE is
    # required on every membership read: the lock alone does not reset an
    # InnoDB snapshot established earlier by authentication or task lookup.
    def rows(query):
        if current:
            query = query.with_for_update().execution_options(populate_existing=True)
        return db.scalars(query).all()

    task = _task_model(db, task_type, task_id, current=current)
    mode = task.audience_mode or ALL_ENROLLED
    base: set[int] = set()
    if mode == ALL_ENROLLED:
        base = set(rows(
            select(CourseEnrollment.student_id).where(
                CourseEnrollment.course_id == course.id,
                CourseEnrollment.status == "enrolled",
            )
        ))
    elif mode == SELECTED_CLASSES:
        if task_type == "assignment":
            class_ids = rows(
                select(AssignmentAudienceClass.teaching_class_id).where(AssignmentAudienceClass.assignment_id == task.id)
            )
        else:
            class_ids = rows(
                select(ExamAudienceClass.teaching_class_id).where(ExamAudienceClass.exam_id == task.id)
            )
        if class_ids:
            base = set(rows(
                select(TeachingClassStudent.student_id).where(
                    TeachingClassStudent.teaching_class_id.in_(class_ids),
                    TeachingClassStudent.status == "active",
                )
            ))

    if task_type == "assignment":
        students = rows(select(AssignmentAudienceStudent).where(AssignmentAudienceStudent.assignment_id == task.id))
    else:
        students = rows(select(ExamAudienceStudent).where(ExamAudienceStudent.exam_id == task.id))
    include_ids = {row.student_id for row in students if row.kind == "include"}
    exclude_ids = {row.student_id for row in students if row.kind == "exclude"}
    result = (base - exclude_ids) | include_ids
    from app.models.teaching import CourseOffering, OfferingEnrollment
    if task.offering_id is None:
        return set()
    offering = (db.scalar(select(CourseOffering).where(CourseOffering.id == task.offering_id)
                          .with_for_update().execution_options(populate_existing=True))
                if current else db.get(CourseOffering, task.offering_id))
    if not offering or offering.status != "active":
        return set()
    members = set(rows(select(OfferingEnrollment.student_id).where(OfferingEnrollment.offering_id == task.offering_id, OfferingEnrollment.status == "active")))
    return result & members


def require_effective_audience(db: Session, *, task_type: str, task_id: int, course: Course) -> set[int]:
    task = _task_model(db, task_type, task_id)
    if task.audience_mode == SELECTED_CLASSES:
        class_count = db.scalar(
            select(AssignmentAudienceClass.id).where(AssignmentAudienceClass.assignment_id == task.id).limit(1)
        ) if task_type == "assignment" else db.scalar(
            select(ExamAudienceClass.id).where(ExamAudienceClass.exam_id == task.id).limit(1)
        )
        if class_count is None:
            raise api_error(422, "AUDIENCE_CLASS_REQUIRED", "指定班级发布时必须至少选择一个教学班")
    if task.audience_mode == WHITELIST_ONLY:
        include_count = db.scalar(
            select(AssignmentAudienceStudent.id).where(
                AssignmentAudienceStudent.assignment_id == task.id,
                AssignmentAudienceStudent.kind == "include",
            ).limit(1)
        ) if task_type == "assignment" else db.scalar(
            select(ExamAudienceStudent.id).where(
                ExamAudienceStudent.exam_id == task.id,
                ExamAudienceStudent.kind == "include",
            ).limit(1)
        )
        if include_count is None:
            raise api_error(422, "AUDIENCE_WHITELIST_REQUIRED", "仅白名单发布时必须至少选择一名学生")
    student_ids = effective_student_ids(db, task_type=task_type, task_id=task_id, course=course)
    if not student_ids:
        raise api_error(422, "AUDIENCE_EMPTY", "发布范围为空，请设置考生/学生范围")
    return student_ids


def assignment_visible_condition(student_id: int):
    """学生端 SQL 可见条件——与 Assignment 关联使用。"""
    enrolled = exists().where(
        CourseEnrollment.course_id == Assignment.course_id,
        CourseEnrollment.student_id == student_id,
        CourseEnrollment.status == "enrolled",
    )
    class_member = exists(
        select(AssignmentAudienceClass.id)
        .join(TeachingClassStudent, TeachingClassStudent.teaching_class_id == AssignmentAudienceClass.teaching_class_id)
        .where(
            AssignmentAudienceClass.assignment_id == Assignment.id,
            TeachingClassStudent.student_id == student_id,
            TeachingClassStudent.status == "active",
        )
    )
    include = exists().where(
        AssignmentAudienceStudent.assignment_id == Assignment.id,
        AssignmentAudienceStudent.student_id == student_id,
        AssignmentAudienceStudent.kind == "include",
    )
    exclude = exists().where(
        AssignmentAudienceStudent.assignment_id == Assignment.id,
        AssignmentAudienceStudent.student_id == student_id,
        AssignmentAudienceStudent.kind == "exclude",
    )
    base = and_(
        Assignment.status == "published",
        or_(
            and_(Assignment.audience_mode == ALL_ENROLLED, enrolled),
            and_(Assignment.audience_mode == SELECTED_CLASSES, class_member),
            and_(Assignment.audience_mode == WHITELIST_ONLY, False),
        ),
    )
    from app.services.offering_service import offering_student_predicate
    return and_(or_(base, include), or_(~exclude, include), offering_student_predicate(Assignment.offering_id, student_id))


def exam_visible_condition(student_id: int):
    """学生端 SQL 可见条件——与 Exam 关联使用。"""
    enrolled = exists().where(
        CourseEnrollment.course_id == Exam.course_id,
        CourseEnrollment.student_id == student_id,
        CourseEnrollment.status == "enrolled",
    )
    class_member = exists(
        select(ExamAudienceClass.id)
        .join(TeachingClassStudent, TeachingClassStudent.teaching_class_id == ExamAudienceClass.teaching_class_id)
        .where(
            ExamAudienceClass.exam_id == Exam.id,
            TeachingClassStudent.student_id == student_id,
            TeachingClassStudent.status == "active",
        )
    )
    include = exists().where(
        ExamAudienceStudent.exam_id == Exam.id,
        ExamAudienceStudent.student_id == student_id,
        ExamAudienceStudent.kind == "include",
    )
    exclude = exists().where(
        ExamAudienceStudent.exam_id == Exam.id,
        ExamAudienceStudent.student_id == student_id,
        ExamAudienceStudent.kind == "exclude",
    )
    base = and_(
        Exam.status == "published",
        or_(
            and_(Exam.audience_mode == ALL_ENROLLED, enrolled),
            and_(Exam.audience_mode == SELECTED_CLASSES, class_member),
            and_(Exam.audience_mode == WHITELIST_ONLY, False),
        ),
    )
    from app.services.offering_service import offering_student_predicate
    return and_(or_(base, include), or_(~exclude, include), offering_student_predicate(Exam.offering_id, student_id))


def student_in_assignment_audience(db: Session, assignment: Assignment, student_id: int,
                                   *, current: bool = False) -> bool:
    return student_id in effective_student_ids(
        db, task_type="assignment", task_id=assignment.id,
        course=assignment.course if assignment.course else None, current=current,
    ) if assignment.course else False


def require_student_assignment_access(db: Session, assignment: Assignment, student: User,
                                     *, current: bool = False) -> None:
    """One read boundary for task metadata, questions, self-tests and submission.

    Callers performing a write also acquire the current teaching-context guard
    before this check, so execution never relies on the unlocked read decision.
    """
    if student.role != 'student':
        raise api_error(403, 'FORBIDDEN', '仅学生可以访问作业作答入口')
    course = (db.scalar(select(Course).where(Course.id == assignment.course_id).with_for_update()
                        .execution_options(populate_existing=True)) if current else db.get(Course, assignment.course_id))
    if not course or course.status != 'published':
        raise api_error(403, 'COURSE_NOT_AVAILABLE', '课程未发布')
    if assignment.status != 'published':
        raise api_error(403, 'ASSIGNMENT_NOT_AVAILABLE', '作业未发布')
    if not student_in_assignment_audience(db, assignment, student.id, current=current):
        raise api_error(403, 'NOT_IN_ASSIGNMENT_AUDIENCE', '你不在本次作业发布范围内')


def student_in_exam_audience(db: Session, exam: Exam, student_id: int) -> bool:
    course = exam.course or db.get(Course, exam.course_id)
    return student_id in effective_student_ids(
        db, task_type="exam", task_id=exam.id, course=course,
    ) if course else False


def populate_audience_cache(db: Session, *, task_type: str, tasks: list) -> None:
    """批量预填任务对象上的 audience_class_ids / whitelist / excluded 缓存。

    - 一次 UNION ALL 查询返回全部范围关系，避免列表接口逐任务触发 2 次惰性加载。
    - 私有缓存字段与模型 property 配合使用。
    """
    if not tasks:
        return
    task_ids = [task.id for task in tasks]
    if task_type == "assignment":
        class_rows = (
            select(AssignmentAudienceClass.assignment_id.label("task_id"), literal("class").label("kind"), AssignmentAudienceClass.teaching_class_id.label("value"))
            .where(AssignmentAudienceClass.assignment_id.in_(task_ids))
        )
        student_rows = (
            select(AssignmentAudienceStudent.assignment_id.label("task_id"), AssignmentAudienceStudent.kind.label("kind"), AssignmentAudienceStudent.student_id.label("value"))
            .where(AssignmentAudienceStudent.assignment_id.in_(task_ids))
        )
    else:
        class_rows = (
            select(ExamAudienceClass.exam_id.label("task_id"), literal("class").label("kind"), ExamAudienceClass.teaching_class_id.label("value"))
            .where(ExamAudienceClass.exam_id.in_(task_ids))
        )
        student_rows = (
            select(ExamAudienceStudent.exam_id.label("task_id"), ExamAudienceStudent.kind.label("kind"), ExamAudienceStudent.student_id.label("value"))
            .where(ExamAudienceStudent.exam_id.in_(task_ids))
        )
    rows = db.execute(union_all(class_rows, student_rows)).all()
    grouped = {task.id: {"_audience_class_ids": [], "_whitelist_student_ids": [], "_excluded_student_ids": []} for task in tasks}
    for task_id, kind, value in rows:
        bucket = grouped.get(task_id)
        if bucket is None:
            continue
        if kind == "class":
            bucket["_audience_class_ids"].append(value)
        elif kind == "include":
            bucket["_whitelist_student_ids"].append(value)
        elif kind == "exclude":
            bucket["_excluded_student_ids"].append(value)
    for task in tasks:
        task.__dict__.update(grouped[task.id])


# 作业/考试名单导入的业务上限：CSV 2 MiB（Nginx 侧放行 3 MiB 以覆盖 multipart 余量）
MAX_AUDIENCE_CSV_BYTES = 2 * 1024 * 1024


async def read_bounded_audience_csv(upload) -> bytes:
    """有界读取上传文件：最多读 2 MiB + 1 字节即可判定超限，绝不无界读入内存。"""
    from app.errors import api_error

    content = await upload.read(MAX_AUDIENCE_CSV_BYTES + 1)
    if len(content) > MAX_AUDIENCE_CSV_BYTES:
        raise api_error(422, "CSV_TOO_LARGE", "CSV 文件不能超过 2 MB")
    return content


def parse_student_csv(content: bytes) -> tuple[list[dict], str | None]:
    """解析学生范围 CSV：UTF-8（含 BOM）或 GB18030；返回行列表或错误。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return [], "仅支持 UTF-8 或 GB18030 编码的 CSV 文件"
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], "CSV 缺少表头"
    rows = []
    for row in reader:
        cleaned = {key.strip(): value.strip() for key, value in row.items() if key}
        if any(cleaned.values()):
            rows.append(cleaned)
    return rows, None


def _csv_student_identifier(row: dict) -> tuple[str | None, str | None, str | None]:
    headers = {key.lower(): key for key in row}
    def pick(*aliases):
        for alias in aliases:
            if alias in headers and row.get(headers[alias]):
                return row[headers[alias]]
        return None
    return (
        pick("学号", "student_no", "studentno"),
        pick("账号", "用户名", "username"),
        pick("姓名", "name", "real_name"),
    )


def _resolve_task_student(db: Session, task, student_no, username, real_name):
    if task.offering_id is None:
        from app.services.roster_service import resolve_student_identity
        return resolve_student_identity(db, student_no, username, real_name)
    from app.models.teaching import OfferingEnrollment
    query = select(User).join(OfferingEnrollment, OfferingEnrollment.student_id == User.id).where(
        OfferingEnrollment.offering_id == task.offering_id,
        OfferingEnrollment.status == "active", User.role == "student", User.status == "active",
    )
    if student_no:
        query = query.where(User.student_no == student_no)
    if username:
        query = query.where(User.username == username)
    student = db.scalar(query)
    if real_name and student and real_name.strip() != student.real_name:
        return None
    return student


def import_audience_students(
    db: Session, *, task_type: str, task_id: int, kind: str, rows: list[dict],
) -> dict:
    """按 CSV 行导入任务级白名单 / 排除名单。返回 created/updated/skipped/errors。"""
    if kind not in ("include", "exclude"):
        raise ValueError("kind 必须为 include 或 exclude")
    task = _task_model(db, task_type, task_id)
    class_model, student_model = _link_models(task_type)
    if task_type == "assignment":
        existing_rows = db.scalars(select(AssignmentAudienceStudent).where(
            AssignmentAudienceStudent.assignment_id == task_id,
        )).all()
    else:
        existing_rows = db.scalars(select(ExamAudienceStudent).where(
            ExamAudienceStudent.exam_id == task_id,
        )).all()
    existing = {(row.student_id, row.kind): row for row in existing_rows}

    result = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
    seen: set[int] = set()
    for index, row in enumerate(rows, start=1):
        student_no, username, real_name = _csv_student_identifier(row)
        student_no = student_no or None
        username = username or None
        if not student_no and not username:
            result["skipped"] += 1
            result["errors"].append({"row": index, "student_no": student_no, "username": username, "status": "skipped", "message": "缺少学号或账号列"})
            continue
        student = _resolve_task_student(db, task, student_no, username, real_name)
        if student is None:
            result["skipped"] += 1
            message = "学生不存在、不可用或不在本授课班" if task.offering_id is not None else f"学生不存在或不可用（{real_name or student_no or username}）"
            result["errors"].append({"row": index, "student_no": student_no, "username": username, "status": "skipped", "message": message})
            continue
        if student.id in seen:
            result["skipped"] += 1
            continue
        seen.add(student.id)
        link = existing.get((student.id, kind))
        if link is None:
            if task_type == "assignment":
                db.add(AssignmentAudienceStudent(assignment_id=task_id, student_id=student.id, kind=kind))
            else:
                db.add(ExamAudienceStudent(exam_id=task_id, student_id=student.id, kind=kind))
            result["created"] += 1
        else:
            result["updated"] += 1
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result
