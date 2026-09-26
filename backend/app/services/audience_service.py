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


def _task_model(db: Session, task_type: str, task_id: int):
    model = Assignment if task_type == "assignment" else Exam
    task = db.get(model, task_id)
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


def effective_student_ids(db: Session, *, task_type: str, task_id: int, course: Course) -> set[int]:
    task = _task_model(db, task_type, task_id)
    mode = task.audience_mode or ALL_ENROLLED
    base: set[int] = set()
    if mode == ALL_ENROLLED:
        base = set(db.scalars(
            select(CourseEnrollment.student_id).where(
                CourseEnrollment.course_id == course.id,
                CourseEnrollment.status == "enrolled",
            )
        ).all())
    elif mode == SELECTED_CLASSES:
        if task_type == "assignment":
            class_ids = list(db.scalars(
                select(AssignmentAudienceClass.teaching_class_id).where(AssignmentAudienceClass.assignment_id == task.id)
            ).all())
        else:
            class_ids = list(db.scalars(
                select(ExamAudienceClass.teaching_class_id).where(ExamAudienceClass.exam_id == task.id)
            ).all())
        if class_ids:
            base = set(db.scalars(
                select(TeachingClassStudent.student_id).where(
                    TeachingClassStudent.teaching_class_id.in_(class_ids),
                    TeachingClassStudent.status == "active",
                )
            ).all())

    if task_type == "assignment":
        students = db.scalars(select(AssignmentAudienceStudent).where(AssignmentAudienceStudent.assignment_id == task.id)).all()
    else:
        students = db.scalars(select(ExamAudienceStudent).where(ExamAudienceStudent.exam_id == task.id)).all()
    include_ids = {row.student_id for row in students if row.kind == "include"}
    exclude_ids = {row.student_id for row in students if row.kind == "exclude"}
    return (base - exclude_ids) | include_ids


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
    return and_(or_(base, include), or_(~exclude, include))


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
    return and_(or_(base, include), or_(~exclude, include))


def student_in_assignment_audience(db: Session, assignment: Assignment, student_id: int) -> bool:
    return student_id in effective_student_ids(
        db, task_type="assignment", task_id=assignment.id,
        course=assignment.course if assignment.course else None,
    ) if assignment.course else False


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


def import_audience_students(
    db: Session, *, task_type: str, task_id: int, kind: str, rows: list[dict],
) -> dict:
    """按 CSV 行导入任务级白名单 / 排除名单。返回 created/updated/skipped/errors。"""
    if kind not in ("include", "exclude"):
        raise ValueError("kind 必须为 include 或 exclude")
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
        from app.services.roster_service import resolve_student_identity
        student = resolve_student_identity(db, student_no, username, real_name)
        if student is None:
            result["skipped"] += 1
            result["errors"].append({"row": index, "student_no": student_no, "username": username, "status": "skipped", "message": f"学生不存在或不可用（{real_name or student_no or username}）"})
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
