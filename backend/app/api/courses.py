import csv
import io

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.dependencies import PaginationParams, get_current_user, get_db, pagination, require_roles
from app.errors import api_error
from app.services.course_access_service import student_visible_course_predicate
from app.models import (
    AcademicTerm, Chapter, Course, CourseEnrollment, CourseTeachingClass,
    CourseWhitelistStudent, Lesson, NotebookTemplate, TeachingClass,
    TeachingClassStudent, User,
)
from app.schemas import (
    ChapterCreate,
    ChapterRead,
    ChapterUpdate,
    CourseCreate,
    CourseListRead,
    CourseListSummary,
    CourseRead,
    CourseStudentCreate,
    CourseStudentImportResult,
    CourseStudentImportRow,
    CourseStudentRead,
    CourseUpdate,
    CourseWhitelistCreate,
    CourseWhitelistEntryRead,
    CourseWhitelistListRead,
    EnrollmentRead,
    LessonCreate,
    LessonRead,
    LessonUpdate,
    PaginatedResponse,
    TeachingClassSummary,
)
from app.services.roster_service import sync_course_class_enrollments
from app.services.storage_object_binding_service import retire_bound_object
from app.storage import StorageArea, StorageService

router = APIRouter(tags=["courses"])


def require_course(course_id: int, db: Session) -> Course:
    course = db.get(Course, course_id)
    if not course:
        raise api_error(404, "COURSE_NOT_FOUND", "课程不存在")
    return course


def require_chapter(chapter_id: int, db: Session) -> Chapter:
    chapter = db.get(Chapter, chapter_id)
    if not chapter:
        raise api_error(404, "CHAPTER_NOT_FOUND", "章节不存在")
    return chapter


def ensure_course_manager(course: Course, user: User):
    if user.role == "admin":
        return
    if user.role == "teacher" and course.teacher_id == user.id:
        return
    raise api_error(403, "FORBIDDEN", "没有权限管理该课程")


def is_student_enrolled(course_id: int, student_id: int, db: Session) -> bool:
    """学生是否已选课且 enrollment 状态为 enrolled"""
    return bool(
        db.scalar(
            select(CourseEnrollment).where(
                CourseEnrollment.course_id == course_id,
                CourseEnrollment.student_id == student_id,
                CourseEnrollment.status == "enrolled",
            )
        )
    )


def is_student_manually_enrolled(course_id: int, student_id: int, db: Session) -> bool:
    """学生是否由教师手动加入课程且当前仍为有效选课状态。"""
    return bool(
        db.scalar(
            select(CourseEnrollment.id).where(
                CourseEnrollment.course_id == course_id,
                CourseEnrollment.student_id == student_id,
                CourseEnrollment.status == "enrolled",
                CourseEnrollment.origin == "manual",
            )
        )
    )


def has_enrollment_record(course_id: int, student_id: int, db: Session) -> bool:
    """是否存在任意状态的 enrollment 记录（含 dropped）"""
    return bool(
        db.scalar(
            select(CourseEnrollment.id).where(
                CourseEnrollment.course_id == course_id,
                CourseEnrollment.student_id == student_id,
            )
        )
    )


def is_student_whitelisted(course_id: int, student_id: int, db: Session) -> bool:
    """学生是否在课程白名单中"""
    return bool(
        db.scalar(
            select(CourseWhitelistStudent.id).where(
                CourseWhitelistStudent.course_id == course_id,
                CourseWhitelistStudent.student_id == student_id,
            )
        )
    )


def is_student_in_course_class(course_id: int, student_id: int, db: Session) -> bool:
    """学生是否属于课程绑定的任一有效教学班"""
    return bool(
        db.scalar(
            select(TeachingClassStudent.id)
            .join(
                CourseTeachingClass,
                CourseTeachingClass.teaching_class_id == TeachingClassStudent.teaching_class_id,
            )
            .where(
                CourseTeachingClass.course_id == course_id,
                TeachingClassStudent.student_id == student_id,
                TeachingClassStudent.status == "active",
            )
        )
    )


def can_view_course(course: Course, user: User, db: Session) -> bool:
    """课程可见性：能否发现课程、读取课程元数据。

    - admin：任意课程
    - teacher：仅自己的课程
    - student：published + 教学班成员或教师手动加入 / 白名单成员 / 存量已选（private）
    - 其他角色：fail closed
    """
    if user.role == "admin":
        return True
    if user.role == "teacher":
        return course.teacher_id == user.id
    if user.role != "student":
        return False
    if course.status != "published":
        return False
    if course.visibility == "public":
        # 兼容迁移前的旧值：旧 public 仍按公开课程处理，迁移后新课程使用 class。
        return True
    if course.visibility == "class":
        return (
            is_student_in_course_class(course.id, user.id, db)
            or is_student_manually_enrolled(course.id, user.id, db)
        )
    if course.visibility == "whitelist":
        return is_student_whitelisted(course.id, user.id, db)
    if course.visibility == "private":
        # 保留存量已选学生的访问
        return is_student_enrolled(course.id, user.id, db)
    # 非法/未知数据库值 fail closed
    return False


def can_access_course_content(course: Course, user: User, db: Session) -> bool:
    """内容权限：能否读取章节/课时或参加作业、考试、提交等学习活动。

    学生必须同时满足可见性要求与有效选课；白名单只决定能否发现并选课，不能替代选课。
    """
    if user.role in ("admin", "teacher"):
        return can_view_course(course, user, db)
    if user.role == "student":
        return (
            can_view_course(course, user, db)
            and is_student_enrolled(course.id, user.id, db)
        )
    return False


def _class_summaries(course: Course, class_counts: dict[int, int] | None = None) -> list[TeachingClassSummary]:
    class_counts = class_counts or {}
    return [TeachingClassSummary(
        id=link.teaching_class.id,
        academic_term_id=link.teaching_class.academic_term_id,
        code=link.teaching_class.code,
        name=link.teaching_class.name,
        status=link.teaching_class.status,
        student_count=class_counts.get(link.teaching_class.id, 0),
    ) for link in course.teaching_class_links]


def serialize_course(course: Course, is_enrolled: bool = False, can_enroll: bool = False,
                     enrollment_origin: str | None = None, counts: dict | None = None,
                     class_counts: dict[int, int] | None = None) -> CourseRead:
    """构造带学生选课状态的 CourseRead 响应"""
    data = CourseRead.model_validate(course).model_dump()
    data["is_enrolled"] = is_enrolled
    data["can_enroll"] = can_enroll
    data["enrollment_origin"] = enrollment_origin
    data["teaching_classes"] = _class_summaries(course, class_counts)
    if counts:
        data.update(counts)
    return CourseRead.model_validate(data)


@router.get("/courses", response_model=CourseListRead)
def list_courses(
    pagination: PaginationParams = Depends(pagination),
    q: str | None = None,
    status_filter: str | None = None,
    academic_term_id: int | None = None,
    sort_by: str = "updated",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page, page_size = pagination.page, pagination.page_size
    page_size = max(1, min(page_size, 100))
    query = select(Course).options(
        selectinload(Course.academic_term),
        selectinload(Course.teaching_class_links).selectinload(CourseTeachingClass.teaching_class),
    )
    count_query = select(func.count()).select_from(Course)
    access_filters = []
    if current_user.role == "student":
        access_filters.append(student_visible_course_predicate(current_user.id))
    elif current_user.role == "teacher":
        access_filters.append(Course.teacher_id == current_user.id)
    elif current_user.role not in ("admin",):
        access_filters.append(Course.id == -1)
    query = query.where(*access_filters)
    count_query = count_query.where(*access_filters)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Course.title.ilike(like), Course.code.ilike(like), Course.description.ilike(like)))
        count_query = count_query.where(or_(Course.title.ilike(like), Course.code.ilike(like), Course.description.ilike(like)))
    if status_filter:
        query = query.where(Course.status == status_filter)
        count_query = count_query.where(Course.status == status_filter)
    if academic_term_id is not None:
        query = query.where(Course.academic_term_id == academic_term_id)
        count_query = count_query.where(Course.academic_term_id == academic_term_id)
    total = db.scalar(count_query) or 0
    order = Course.title.asc() if sort_by == "title" else Course.updated_at.desc()
    courses = db.scalars(query.order_by(order, Course.id).offset((page - 1) * page_size).limit(page_size)).all()
    course_ids = [course.id for course in courses]
    chapter_counts, lesson_counts, student_counts = {}, {}, {}
    if course_ids:
        chapter_counts = dict(db.execute(select(Chapter.course_id, func.count(Chapter.id)).where(Chapter.course_id.in_(course_ids)).group_by(Chapter.course_id)).all())
        lesson_counts = dict(db.execute(select(Chapter.course_id, func.count(Lesson.id)).join(Lesson, Lesson.chapter_id == Chapter.id).where(Chapter.course_id.in_(course_ids)).group_by(Chapter.course_id)).all())
        student_counts = dict(db.execute(select(CourseEnrollment.course_id, func.count(func.distinct(CourseEnrollment.student_id))).where(
            CourseEnrollment.course_id.in_(course_ids), CourseEnrollment.status == "enrolled"
        ).group_by(CourseEnrollment.course_id)).all())
    class_ids = [link.teaching_class_id for course in courses for link in course.teaching_class_links]
    class_counts = dict(db.execute(select(TeachingClassStudent.teaching_class_id, func.count(func.distinct(TeachingClassStudent.student_id))).where(
        TeachingClassStudent.teaching_class_id.in_(class_ids), TeachingClassStudent.status == "active"
    ).group_by(TeachingClassStudent.teaching_class_id)).all()) if class_ids else {}
    items = [
        serialize_course(course, is_enrolled, can_enroll, origin, {
            "chapter_count": chapter_counts.get(course.id, 0),
            "lesson_count": lesson_counts.get(course.id, 0),
            "student_count": student_counts.get(course.id, 0),
        }, class_counts)
        for course, is_enrolled, can_enroll, origin in _course_student_flags(courses, current_user, db)
    ]
    status_rows = db.execute(select(Course.status, func.count(Course.id)).where(*access_filters).group_by(Course.status)).all()
    by_status = dict(status_rows)
    return CourseListRead(items=items, page=page, page_size=page_size, total=total, summary=CourseListSummary(
        total=sum(by_status.values()), published=by_status.get("published", 0),
        draft=by_status.get("draft", 0), archived=by_status.get("archived", 0),
    ))


def _course_student_flags(courses, current_user: User, db: Session):
    """批量计算本页课程的 is_enrolled / can_enroll（学生视角）"""
    if current_user.role != "student" or not courses:
        return [(course, False, False, None) for course in courses]
    enrollment_rows = db.scalars(
            select(CourseEnrollment).where(
                CourseEnrollment.student_id == current_user.id,
                CourseEnrollment.course_id.in_([c.id for c in courses]),
            )
        ).all()
    enrollments = {row.course_id: row for row in enrollment_rows}
    flags = []
    for course in courses:
        enrollment = enrollments.get(course.id)
        is_enrolled = enrollment is not None and enrollment.status == "enrolled"
        if enrollment and enrollment.origin == "manual" and enrollment.status == "dropped":
            can_enroll = False
        elif is_enrolled:
            can_enroll = False
        elif course.visibility in ("class", "public"):
            can_enroll = True
        elif course.visibility == "whitelist":
            # 能出现在学生结果中的 whitelist 课程即白名单成员
            can_enroll = True
        else:
            can_enroll = False
        flags.append((course, is_enrolled, can_enroll, enrollment.origin if enrollment else None))
    return flags


def _set_course_classes(db: Session, course: Course, class_ids: list[int]) -> None:
    class_ids = list(dict.fromkeys(class_ids))
    if class_ids and course.academic_term_id is None:
        raise api_error(422, "COURSE_TERM_REQUIRED", "绑定教学班前必须选择课程学期")
    classes = db.scalars(select(TeachingClass).where(TeachingClass.id.in_(class_ids))).all() if class_ids else []
    if len(classes) != len(class_ids):
        raise api_error(422, "INVALID_TEACHING_CLASSES", "包含不存在的教学班")
    if any(row.academic_term_id != course.academic_term_id for row in classes):
        raise api_error(422, "TEACHING_CLASS_TERM_MISMATCH", "教学班必须与课程属于同一学期")
    existing_ids = {link.teaching_class_id for link in course.teaching_class_links}
    if any(row.status != "active" and row.id not in existing_ids for row in classes):
        raise api_error(409, "TEACHING_CLASS_ARCHIVED", "已归档教学班不能新增绑定")
    removed = existing_ids - set(class_ids)
    if removed and db.scalar(select(TeachingClass.id).where(TeachingClass.id.in_(removed), TeachingClass.status == 'archived').limit(1)):
        raise api_error(409, 'TEACHING_CLASS_ARCHIVED', '已归档教学班关系已冻结')
    if existing_ids == set(class_ids):
        return
    for link in list(course.teaching_class_links):
        db.delete(link)
    db.flush()
    for class_id in class_ids:
        db.add(CourseTeachingClass(course_id=course.id, teaching_class_id=class_id))
    db.flush()
    sync_course_class_enrollments(db, course)


def _ensure_course_term_writable(course: Course) -> None:
    if course.academic_term is not None and course.academic_term.status == "closed":
        raise api_error(409, "ACADEMIC_TERM_CLOSED", "已关闭学期的课程只读")


def _ensure_course_publishable(db: Session, course: Course) -> None:
    """课程从草稿发布前，必须具备完整的课程基本信息与可用的 Notebook 实验内容。"""
    missing = []
    if not (course.title or "").strip():
        missing.append("课程名称")
    if not (course.description or "").strip():
        missing.append("课程简介")
    if course.academic_term_id is None:
        missing.append("所属学期")
    if not course.teaching_class_links:
        missing.append("教学班")
    if not (course.cover or "").strip():
        missing.append("课程封面")
    if course.start_time is None:
        missing.append("开课时间")
    if not (course.visibility or "").strip():
        missing.append("课程可见范围")
    if course.default_score is None:
        missing.append("默认评分")
    if missing:
        raise api_error(422, "COURSE_INCOMPLETE", f"发布前请完善：{'、'.join(missing)}")

    # Notebook 课时必须有绑定且已发布版本的模板，否则学生打开即 404
    unconfigured = [
        lesson.title
        for chapter in course.chapters
        for lesson in chapter.lessons
        if lesson.content_type == "notebook"
        and (
            lesson.template_id is None
            or (template := db.get(NotebookTemplate, lesson.template_id)) is None
            or not template.current_version_id
        )
    ]
    if unconfigured:
        preview = "、".join(unconfigured[:5]) + ("等" if len(unconfigured) > 5 else "")
        raise api_error(
            422,
            "NOTEBOOK_TEMPLATE_REQUIRED",
            f"以下课时尚未完成 Notebook 配置：{preview}。请进入课时编辑页完成模板配置并发布版本",
        )


@router.post("/courses", response_model=CourseRead, status_code=status.HTTP_201_CREATED)
def create_course(
    payload: CourseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher", "admin")),
):
    from app.services.account_service import lock_policy
    lock_policy(db)
    course = Course(
        title=payload.title,
        description=payload.description,
        status=payload.status,
        cover=payload.cover,
        start_time=payload.start_time,
        visibility=payload.visibility,
        default_score=payload.default_score,
        academic_term_id=payload.academic_term_id,
        teacher_id=current_user.id if current_user.role == "teacher" else None,
    )
    db.add(course)
    db.flush()
    if payload.academic_term_id:
        term = db.get(AcademicTerm, payload.academic_term_id)
        if not term:
            raise api_error(422, "ACADEMIC_TERM_NOT_FOUND", "学期不存在")
        if term.status == "closed":
            raise api_error(409, "ACADEMIC_TERM_CLOSED", "已关闭学期不能新建或调整课程")
    _set_course_classes(db, course, payload.teaching_class_ids)
    db.commit()
    db.refresh(course)
    return get_course(course.id, db, current_user)


@router.get("/courses/{course_id}", response_model=CourseRead)
def get_course(course_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    course = require_course(course_id, db)
    if not can_view_course(course, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限查看该课程")
    is_enrolled = False
    can_enroll = False
    if current_user.role == "student":
        is_enrolled = is_student_enrolled(course.id, current_user.id, db)
        can_enroll = not is_enrolled and (
            course.visibility in ("public", "whitelist")
            or (course.visibility == "class" and is_student_in_course_class(course.id, current_user.id, db))
        )
    denied = db.scalar(select(CourseEnrollment.id).where(CourseEnrollment.course_id == course.id,
                       CourseEnrollment.student_id == current_user.id, CourseEnrollment.origin == 'manual',
                       CourseEnrollment.status == 'dropped'))
    if denied:
        can_enroll = False
    origin = None
    if current_user.role == "student" and is_enrolled:
        enrollment = db.scalar(select(CourseEnrollment).where(CourseEnrollment.course_id == course.id, CourseEnrollment.student_id == current_user.id))
        origin = enrollment.origin if enrollment else None
    counts = {
        "chapter_count": db.scalar(select(func.count()).select_from(Chapter).where(Chapter.course_id == course.id)) or 0,
        "lesson_count": db.scalar(select(func.count()).select_from(Lesson).join(Chapter).where(Chapter.course_id == course.id)) or 0,
        "student_count": db.scalar(select(func.count(func.distinct(CourseEnrollment.student_id))).where(CourseEnrollment.course_id == course.id, CourseEnrollment.status == "enrolled")) or 0,
    }
    return serialize_course(course, is_enrolled, can_enroll, origin, counts)


@router.patch("/courses/{course_id}", response_model=CourseRead)
def update_course(
    course_id: int,
    payload: CourseUpdate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    from app.services.account_service import lock_policy
    lock_policy(db)
    course = require_course(course_id, db)
    ensure_course_manager(course, current_user)
    _ensure_course_term_writable(course)
    updates = payload.model_dump(exclude_unset=True)
    publishing = updates.get("status") == "published" and course.status != "published"
    class_ids = updates.pop("teaching_class_ids", None)
    old_cover_key = course.cover
    old_cover_object_id = course.cover_object_id
    cover_binding_changed = "cover" in updates and updates["cover"] != course.cover
    if cover_binding_changed:
        # Direct legacy cover edits must detach a Phase 2B object.  The
        # physical object is retired only after this business-row commit.
        course.cover_object_id = None
    if "academic_term_id" in updates and updates["academic_term_id"] is not None:
        term = db.get(AcademicTerm, updates["academic_term_id"])
        if not term:
            raise api_error(422, "ACADEMIC_TERM_NOT_FOUND", "学期不存在")
        if term.status == "closed":
            raise api_error(409, "ACADEMIC_TERM_CLOSED", "已关闭学期不能新建或调整课程")
    for key, value in updates.items():
        setattr(course, key, value)
    if class_ids is not None:
        _set_course_classes(db, course, class_ids)
    elif "academic_term_id" in updates and course.teaching_class_links:
        if any(link.teaching_class.academic_term_id != course.academic_term_id for link in course.teaching_class_links):
            raise api_error(422, "TEACHING_CLASS_TERM_MISMATCH", "更换学期时必须同时重新选择教学班")
    if publishing:
        _ensure_course_publishable(db, course)
    db.commit()
    db.refresh(course)
    if cover_binding_changed and old_cover_object_id is not None:
        retire_bound_object(
            db,
            StorageService.from_settings(settings),
            object_id=old_cover_object_id,
            area=StorageArea.COVERS,
            legacy_key=old_cover_key,
            logger_name="course_cover",
        )
    return get_course(course.id, db, current_user)


@router.delete("/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_course(course_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from app.services.account_service import lock_policy
    lock_policy(db)
    course = require_course(course_id, db)
    ensure_course_manager(course, current_user)
    _ensure_course_term_writable(course)
    course.status = "archived"
    db.commit()
    return None


@router.post("/courses/{course_id}/enroll", response_model=EnrollmentRead, status_code=status.HTTP_201_CREATED)
def enroll_course(
    course_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
):
    from app.services.account_service import lock_policy
    lock_policy(db)
    course = require_course(course_id, db)
    _ensure_course_term_writable(course)
    if course.status != "published":
        raise api_error(400, "COURSE_NOT_PUBLISHED", "课程尚未发布")
    enrollment = db.scalar(
        select(CourseEnrollment).where(
            CourseEnrollment.course_id == course_id,
            CourseEnrollment.student_id == current_user.id,
        )
    )
    if enrollment and enrollment.origin == 'manual' and enrollment.status == 'dropped':
        raise api_error(403, 'COURSE_NOT_VISIBLE', '已被移出课程，请联系教师')
    if enrollment and enrollment.status == 'enrolled':
        return enrollment
    if course.visibility == "public":
        pass  # 所有学生可选
    elif course.visibility == "class":
        if not is_student_in_course_class(course.id, current_user.id, db):
            raise api_error(403, "COURSE_NOT_VISIBLE", "只有课程教学班学生可以选修该课程")
    elif course.visibility == "whitelist":
        if not is_student_whitelisted(course.id, current_user.id, db):
            raise api_error(403, "COURSE_NOT_VISIBLE", "没有权限选修该课程")
    elif course.visibility == "private":
        # A historical dropped row never authorizes re-entry into a private course.
        raise api_error(403, "COURSE_NOT_VISIBLE", "请联系教师重新授权该课程")
    else:
        raise api_error(403, "COURSE_NOT_VISIBLE", "没有权限选修该课程")
    if enrollment:
        enrollment.status = "enrolled"
        enrollment.origin = "self"
    else:
        enrollment = CourseEnrollment(course_id=course_id, student_id=current_user.id, status="enrolled", origin="self")
        db.add(enrollment)
    db.commit()
    db.refresh(enrollment)
    return enrollment


@router.delete("/courses/{course_id}/enroll", status_code=status.HTTP_204_NO_CONTENT)
def drop_course(
    course_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
):
    from app.services.account_service import lock_policy
    lock_policy(db)
    course = require_course(course_id, db)
    _ensure_course_term_writable(course)
    enrollment = db.scalar(
        select(CourseEnrollment).where(
            CourseEnrollment.course_id == course_id,
            CourseEnrollment.student_id == current_user.id,
        )
    )
    if enrollment:
        if enrollment.origin in {"class", "manual"} or is_student_in_course_class(course_id, current_user.id, db):
            raise api_error(409, "CLASS_ENROLLMENT_REQUIRED", "班级统一加入的课程不能自行退选")
        enrollment.status = "dropped"
        db.commit()
    return None


@router.get("/courses/{course_id}/students", response_model=PaginatedResponse)
def list_course_students(course_id: int,
                         pagination: PaginationParams = Depends(pagination),
                         db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    page, page_size = pagination.page, pagination.page_size
    course = require_course(course_id, db); ensure_course_manager(course, current_user)
    filters = (CourseEnrollment.course_id == course_id, CourseEnrollment.status == "enrolled")
    total = db.scalar(select(func.count()).select_from(CourseEnrollment).where(*filters)) or 0
    rows = db.execute(select(User, CourseEnrollment.origin).join(CourseEnrollment, CourseEnrollment.student_id == User.id)
        .where(*filters).order_by(User.student_no, User.id).offset((page - 1) * page_size).limit(page_size)).all()
    items = []
    class_links = course.teaching_class_links
    for student, origin in rows:
        memberships = {m.teaching_class_id for m in student.teaching_class_memberships if m.status == "active"}
        classes = [TeachingClassSummary.model_validate(link.teaching_class) for link in class_links if link.teaching_class_id in memberships]
        items.append(CourseStudentRead(id=student.id, username=student.username, student_no=student.student_no,
            real_name=student.real_name, status=student.status, enrollment_origin=origin, teaching_classes=classes))
    return PaginatedResponse(items=items, page=page, page_size=page_size, total=total)


def _parse_student_csv(content: bytes) -> tuple[list[dict], str | None]:
    """解析 UTF-8（含 BOM）或 GB18030 CSV；返回行列表或错误信息。"""
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
    normalized = {name.strip().lower(): name for name in reader.fieldnames}
    rows = []
    for row in reader:
        cleaned = {key.strip(): value.strip() for key, value in row.items() if key}
        if not any(cleaned.values()):
            continue
        rows.append(cleaned)
    return rows, None


def _student_identifier(row: dict, normalized: dict) -> tuple[str | None, str | None, str | None]:
    def pick(*keys):
        for key in keys:
            for alias in key:
                if alias in normalized and row.get(normalized[alias]):
                    return row[normalized[alias]]
        return None
    student_no = pick(("学号", "student_no", "studentno", "student number"))
    username = pick(("账号", "用户名", "username", "user"))
    real_name = pick(("姓名", "name", "real_name"))
    return student_no, username, real_name


@router.post("/courses/{course_id}/students/import", response_model=CourseStudentImportResult)
async def import_course_students(
    course_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CSV 导入课程名单：按学号优先、账号兜底匹配 active 学生；不存在的行跳过并报告。"""
    from app.services.account_service import lock_policy
    lock_policy(db)
    course = require_course(course_id, db)
    ensure_course_manager(course, current_user)
    _ensure_course_term_writable(course)

    content = await file.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise api_error(422, "CSV_TOO_LARGE", "CSV 文件不能超过 2 MB")
    rows, error = _parse_student_csv(content)
    if error:
        raise api_error(422, "CSV_INVALID", error)

    result = CourseStudentImportResult()
    seen_students: set[int] = set()
    for index, raw in enumerate(rows, start=1):
        student_no, username, real_name = _student_identifier(raw, {k.lower(): k for k in raw})
        student_no = student_no or None
        username = username or None
        if not student_no and not username:
            result.errors.append(CourseStudentImportRow(
                row=index, student_no=student_no, username=username,
                status="skipped", message="缺少学号或账号列",
            ))
            result.skipped += 1
            continue
        from app.services.roster_service import resolve_student_identity
        student = resolve_student_identity(db, student_no, username, real_name)
        if student is None:
            result.errors.append(CourseStudentImportRow(
                row=index, student_no=student_no, username=username,
                status="skipped", message=f"学生不存在或不可用（{real_name or student_no or username}）",
            ))
            result.skipped += 1
            continue
        if student.id in seen_students:
            result.skipped += 1
            continue
        seen_students.add(student.id)
        enrollment = db.scalar(
            select(CourseEnrollment).where(
                CourseEnrollment.course_id == course.id,
                CourseEnrollment.student_id == student.id,
            )
        )
        if enrollment is None:
            db.add(CourseEnrollment(
                course_id=course.id, student_id=student.id,
                status="enrolled", origin="manual",
            ))
            result.created += 1
        elif enrollment.status == "enrolled":
            result.skipped += 1
        elif enrollment.origin == "manual":
            result.skipped += 1
            result.errors.append(CourseStudentImportRow(
                row=index, student_no=student_no, username=username,
                status="skipped", message="学生已被人工移出，请通过单独添加操作重新授权",
            ))
        else:
            enrollment.status = "enrolled"
            enrollment.origin = "manual"
            result.updated += 1
    from app.services.account_service import audit
    audit(db, current_user.id, 'course.csv_import', 'course', course.id,
          changes={'created': result.created, 'updated': result.updated, 'skipped': result.skipped})
    db.commit()
    return result


@router.post("/courses/{course_id}/students", response_model=CourseStudentRead, status_code=status.HTTP_201_CREATED)
def add_course_student(course_id: int, payload: CourseStudentCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from app.services.account_service import lock_policy
    lock_policy(db)
    course = require_course(course_id, db); ensure_course_manager(course, current_user)
    _ensure_course_term_writable(course)
    student = db.get(User, payload.student_id)
    if not student or student.role != "student" or student.status != "active":
        raise api_error(422, "INVALID_STUDENT", "学生不存在或状态不可用")
    enrollment = db.scalar(select(CourseEnrollment).where(CourseEnrollment.course_id == course_id, CourseEnrollment.student_id == student.id))
    if enrollment:
        enrollment.status = "enrolled"; enrollment.origin = "manual"
    else:
        db.add(CourseEnrollment(course_id=course_id, student_id=student.id, status="enrolled", origin="manual"))
    db.commit()
    return CourseStudentRead(id=student.id, username=student.username, student_no=student.student_no,
        real_name=student.real_name, status=student.status, enrollment_origin="manual", teaching_classes=[])


@router.delete("/courses/{course_id}/students/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_course_student(course_id: int, student_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from app.services.account_service import lock_policy
    lock_policy(db)
    course = require_course(course_id, db); ensure_course_manager(course, current_user)
    _ensure_course_term_writable(course)
    enrollment = db.scalar(select(CourseEnrollment).where(CourseEnrollment.course_id == course_id, CourseEnrollment.student_id == student_id))
    if not enrollment:
        raise api_error(404, "COURSE_STUDENT_NOT_FOUND", "学生不在课程名单中")
    enrollment.status = "dropped"; enrollment.origin = "manual"
    db.commit()
    return None


@router.get("/courses/{course_id}/chapters", response_model=PaginatedResponse)
def list_chapters(
    course_id: int,
    pagination: PaginationParams = Depends(pagination),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page, page_size = pagination.page, pagination.page_size
    course = require_course(course_id, db)
    if not can_access_course_content(course, current_user, db):
        raise api_error(403, "FORBIDDEN", "没有权限查看该课程章节")
    query = (
        select(Chapter)
        .where(Chapter.course_id == course_id)
        .options(selectinload(Chapter.lessons))
        .order_by(Chapter.order_index, Chapter.id)
    )
    total = db.scalar(select(func.count()).select_from(Chapter).where(Chapter.course_id == course_id)) or 0
    chapters = db.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    return PaginatedResponse(items=[ChapterRead.model_validate(chapter) for chapter in chapters], page=page, page_size=page_size, total=total)


@router.post("/courses/{course_id}/chapters", response_model=ChapterRead, status_code=status.HTTP_201_CREATED)
def create_chapter(
    course_id: int,
    payload: ChapterCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    course = require_course(course_id, db)
    ensure_course_manager(course, current_user)
    _ensure_course_term_writable(course)
    chapter = Chapter(course_id=course_id, title=payload.title, order_index=payload.order_index)
    db.add(chapter)
    db.commit()
    db.refresh(chapter)
    return chapter


@router.post("/chapters/{chapter_id}/lessons", response_model=LessonRead, status_code=status.HTTP_201_CREATED)
def create_lesson(
    chapter_id: int,
    payload: LessonCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    chapter = require_chapter(chapter_id, db)
    ensure_course_manager(chapter.course, current_user)
    _ensure_course_term_writable(chapter.course)
    if payload.content_type == "notebook" and payload.status == "published":
        # 模板绑定依赖已存在的课时，直接以发布态创建必然产生学生不可用的实验入口
        raise api_error(
            422,
            "NOTEBOOK_TEMPLATE_REQUIRED",
            "Notebook 课时请先以草稿创建，完成模板配置后再发布",
        )
    lesson = Lesson(chapter_id=chapter_id, **payload.model_dump())
    db.add(lesson)
    db.commit()
    db.refresh(lesson)
    return lesson


@router.patch("/lessons/{lesson_id}", response_model=LessonRead)
def update_lesson(
    lesson_id: int,
    payload: LessonUpdate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    """更新课时：支持标题/内容/发布状态，传 chapter_id 可移动到其他章节。

    视频语义：
    - 非空 video_url：切换为 external，清空上传元数据，提交成功后删除旧本地文件；
    - video_url=None 且当前来源为 upload：不得隐式删除本地文件（必须走专用 DELETE 接口）；
    - content_type 从 video 改为其他类型：清空全部视频字段并清理本地文件。
    """
    lesson = db.get(Lesson, lesson_id)
    if not lesson:
        raise api_error(404, "LESSON_NOT_FOUND", "课时不存在")
    ensure_course_manager(lesson.chapter.course, current_user)
    _ensure_course_term_writable(lesson.chapter.course)
    data = payload.model_dump(exclude_unset=True)
    if data.get("chapter_id") is not None and data["chapter_id"] != lesson.chapter_id:
        target = db.get(Chapter, data["chapter_id"])
        if not target or target.course_id != lesson.chapter.course_id:
            raise api_error(400, "INVALID_CHAPTER", "目标章节不存在或不属于同一课程")

    old_key = lesson.video_storage_key
    old_object_id = lesson.video_object_id
    clear_files = False  # 提交成功后需要删除旧本地文件
    if "video_url" in data and data["video_url"]:
        # 写入非空外链：切换为 external 并清空上传元数据
        if lesson.video_source == "upload":
            lesson.video_source = "external"
            lesson.video_storage_key = None
            lesson.video_filename = None
            lesson.video_content_type = None
            lesson.video_size = None
            lesson.video_object_id = None
            clear_files = True
    if (
        data.get("content_type")
        and data["content_type"] != lesson.content_type
        and lesson.content_type == "video"
    ):
        # 视频课时改为其他类型：清空全部视频字段并清理本地文件
        lesson.video_source = "external"
        lesson.video_storage_key = None
        lesson.video_filename = None
        lesson.video_content_type = None
        lesson.video_size = None
        lesson.video_object_id = None
        lesson.video_url = None
        clear_files = True

    # Notebook 课时进入学生可见态前，必须已绑定且模板已有发布版本
    effective_content_type = data.get("content_type", lesson.content_type)
    effective_status = data.get("status", lesson.status)
    becomes_visible_notebook = (
        lesson.status != "published"
        and effective_status == "published"
        or (
            lesson.content_type != "notebook"
            and data.get("content_type") == "notebook"
            and effective_status == "published"
        )
    )
    if effective_content_type == "notebook" and becomes_visible_notebook:
        template = db.get(NotebookTemplate, lesson.template_id) if lesson.template_id else None
        if template is None:
            raise api_error(
                422,
                "NOTEBOOK_TEMPLATE_REQUIRED",
                "该课时为 Notebook 实验课，发布前请先进入课时编辑页完成模板配置",
            )
        if not template.current_version_id:
            raise api_error(
                422,
                "NOTEBOOK_TEMPLATE_REQUIRED",
                "该课时的 Notebook 模板尚未发布任何版本，请先在 Studio 中发布",
            )

    for key, value in data.items():
        setattr(lesson, key, value)
    db.commit()
    db.refresh(lesson)
    if clear_files:
        retire_bound_object(
            db,
            StorageService.from_settings(settings),
            object_id=old_object_id,
            area=StorageArea.VIDEOS,
            legacy_key=old_key,
            logger_name="lesson_video",
        )
    return lesson


@router.delete("/lessons/{lesson_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lesson(
    lesson_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    lesson = db.get(Lesson, lesson_id)
    if not lesson:
        raise api_error(404, "LESSON_NOT_FOUND", "课时不存在")
    ensure_course_manager(lesson.chapter.course, current_user)
    _ensure_course_term_writable(lesson.chapter.course)
    # 删除前保存对象绑定，数据库提交成功后清理本地文件
    old_key = lesson.video_storage_key
    old_object_id = lesson.video_object_id
    db.delete(lesson)
    db.commit()
    retire_bound_object(
        db,
        StorageService.from_settings(settings),
        object_id=old_object_id,
        area=StorageArea.VIDEOS,
        legacy_key=old_key,
        logger_name="lesson_video",
    )


@router.patch("/chapters/{chapter_id}", response_model=ChapterRead)
def update_chapter(
    chapter_id: int,
    payload: ChapterUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """编辑章节：标题 / 排序位置（移动章节）"""
    chapter = db.get(Chapter, chapter_id)
    if not chapter:
        raise api_error(404, "CHAPTER_NOT_FOUND", "章节不存在")
    ensure_course_manager(chapter.course, current_user)
    _ensure_course_term_writable(chapter.course)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(chapter, key, value)
    db.commit()
    db.refresh(chapter)
    return chapter


@router.delete("/chapters/{chapter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chapter(
    chapter_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    """删除章节：级联删除章节内全部课时，并清理章节内所有本地视频文件"""
    chapter = db.get(Chapter, chapter_id)
    if not chapter:
        raise api_error(404, "CHAPTER_NOT_FOUND", "章节不存在")
    ensure_course_manager(chapter.course, current_user)
    _ensure_course_term_writable(chapter.course)
    # 删除前收集章节内全部视频对象绑定，级联删除成功后逐个清理文件
    old_objects = [
        (lesson.video_object_id, lesson.video_storage_key)
        for lesson in chapter.lessons
        if lesson.video_object_id is not None or lesson.video_storage_key
    ]
    db.delete(chapter)
    db.commit()
    storage = StorageService.from_settings(settings)
    for object_id, key in old_objects:
        retire_bound_object(
            db,
            storage,
            object_id=object_id,
            area=StorageArea.VIDEOS,
            legacy_key=key,
            logger_name="lesson_video",
        )


# ── 课程白名单 ────────────────────────────────────────────────


@router.get("/courses/{course_id}/whitelist", response_model=CourseWhitelistListRead)
def list_whitelist(
    course_id: int,
    pagination: PaginationParams = Depends(pagination),
    q: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page, page_size = pagination.page, pagination.page_size
    course = require_course(course_id, db)
    ensure_course_manager(course, current_user)
    filters = [CourseWhitelistStudent.course_id == course_id]
    if q:
        like = f"%{q}%"
        filters.append(or_(User.username.ilike(like), User.real_name.ilike(like)))
    query = (
        select(CourseWhitelistStudent)
        .join(User, CourseWhitelistStudent.student_id == User.id)
        .where(*filters)
        .options(selectinload(CourseWhitelistStudent.student))
    )
    count_query = (
        select(func.count())
        .select_from(CourseWhitelistStudent)
        .join(User, CourseWhitelistStudent.student_id == User.id)
        .where(*filters)
    )
    total = db.scalar(count_query) or 0
    entries = db.scalars(
        query.order_by(CourseWhitelistStudent.id)
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    return CourseWhitelistListRead(
        items=[CourseWhitelistEntryRead.model_validate(entry) for entry in entries],
        page=page, page_size=page_size, total=total,
    )


@router.post("/courses/{course_id}/whitelist", response_model=CourseWhitelistEntryRead, status_code=status.HTTP_201_CREATED)
def add_whitelist_student(
    course_id: int,
    payload: CourseWhitelistCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    course = require_course(course_id, db)
    ensure_course_manager(course, current_user)
    _ensure_course_term_writable(course)
    student = db.get(User, payload.student_id)
    if not student:
        raise api_error(404, "USER_NOT_FOUND", "用户不存在")
    if student.role != "student":
        raise api_error(400, "INVALID_STUDENT_ROLE", "只有学生可以加入白名单")
    if student.status != "active":
        raise api_error(400, "STUDENT_NOT_ACTIVE", "该学生状态不可用")
    if is_student_whitelisted(course_id, payload.student_id, db):
        raise api_error(409, "WHITELIST_ENTRY_EXISTS", "该学生已在白名单中")
    entry = CourseWhitelistStudent(course_id=course_id, student_id=payload.student_id)
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        # 并发重复插入：唯一约束兜底，回滚后返回同一 409 而非 500
        db.rollback()
        raise api_error(409, "WHITELIST_ENTRY_EXISTS", "该学生已在白名单中")
    db.refresh(entry)
    return entry


@router.delete("/courses/{course_id}/whitelist/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_whitelist_student(
    course_id: int,
    student_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    course = require_course(course_id, db)
    ensure_course_manager(course, current_user)
    _ensure_course_term_writable(course)
    entry = db.scalar(
        select(CourseWhitelistStudent).where(
            CourseWhitelistStudent.course_id == course_id,
            CourseWhitelistStudent.student_id == student_id,
        )
    )
    if not entry:
        raise api_error(404, "WHITELIST_ENTRY_NOT_FOUND", "该学生不在白名单中")
    # 不删除也不修改 CourseEnrollment；若课程为 whitelist，学生立即失去可见性与内容权限
    db.delete(entry)
    db.commit()
    return None
