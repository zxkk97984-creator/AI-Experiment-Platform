"""课程发布前的基本信息完整性门禁测试。"""
from datetime import date

from conftest import auth_header, create_user, login
from app.models import AcademicTerm, Chapter, Course, Lesson, NotebookTemplate, NotebookTemplateVersion, TeachingClass


API = "/api/v1"


def _teacher_token(client, db_session_factory):
    create_user(db_session_factory, "publish_teacher", "teacher")
    token, _ = login(client, "publish_teacher")
    return token


def _complete_course(client, db_session_factory, token, term_id, class_id, title="Complete course"):
    created = client.post(
        f"{API}/courses",
        headers=auth_header(token),
        json={
            "title": title,
            "description": "Course description",
            "status": "draft",
            "academic_term_id": term_id,
            "cover": "covers/complete.png",
            "start_time": "2026-09-01T08:00:00",
            "visibility": "public",
            "default_score": 100,
        },
    )
    assert created.status_code == 201, created.text
    course_id = created.json()["id"]
    # A content owner may prepare shared content; class authority is explicitly
    # allocated by an administrator, never granted by the teacher's own payload.
    create_user(db_session_factory, "publish_admin", "admin")
    admin_token, _ = login(client, "publish_admin")
    with db_session_factory() as db:
        teacher_id = db.get(Course, course_id).teacher_id
    allocated = client.post(f"{API}/offerings", headers=auth_header(admin_token), json={
        "course_id": course_id, "academic_term_id": term_id,
        "teaching_class_id": class_id, "teacher_id": teacher_id,
    })
    assert allocated.status_code == 201, allocated.text
    return course_id


def _publish_ready_template(db_session_factory, owner_id, lesson_id):
    """为课时绑定一个已带发布版本的模板（模拟教师已完成首次配置）。"""
    cells = [
        {
            "id": "task",
            "type": "markdown",
            "source": "# 动手实验",
            "order": 0,
            "student_editable": False,
            "source_hidden": False,
        }
    ]
    with db_session_factory() as db:
        template = NotebookTemplate(
            name=f"Lesson template {lesson_id}",
            owner_id=owner_id,
            status="published",
            draft_cells=cells,
            draft_revision=1,
            draft_import_policy_mode="unrestricted",
            draft_allowed_imports=[],
        )
        db.add(template)
        db.flush()
        version = NotebookTemplateVersion(
            template_id=template.id,
            version_number=1,
            sha256="a" * 64,
            cells=cells,
            cell_order=["task"],
            published_by_id=owner_id,
            import_policy_mode="unrestricted",
            allowed_imports=[],
        )
        db.add(version)
        db.flush()
        template.current_version_id = version.id
        lesson = db.get(Lesson, lesson_id)
        lesson.template_id = template.id
        db.commit()


def _add_notebook_lesson(db_session_factory, course_id, title="动手实验课", *, status="published"):
    with db_session_factory() as db:
        chapter = Chapter(course_id=course_id, title=f"{title} · 章节", order_index=0)
        db.add(chapter)
        db.flush()
        lesson = Lesson(
            chapter_id=chapter.id,
            title=title,
            content_type="notebook",
            status=status,
            order_index=0,
        )
        db.add(lesson)
        db.commit()
        return lesson.id


def _academic_data(db_session_factory):
    with db_session_factory() as db:
        term = AcademicTerm(
            code="PUBLISH-TERM",
            name="Publish term",
            start_date=date(2026, 9, 1),
            end_date=date(2027, 1, 20),
            status="active",
        )
        db.add(term)
        db.flush()
        teaching_class = TeachingClass(
            academic_term_id=term.id,
            code="PUBLISH-CLASS",
            name="Publish class",
            status="active",
        )
        db.add(teaching_class)
        db.commit()
        return term.id, teaching_class.id


def test_publish_rejects_incomplete_course(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    created = client.post(
        f"{API}/courses",
        headers=auth_header(token),
        json={"title": "Draft course", "status": "draft"},
    )
    assert created.status_code == 201, created.text

    response = client.patch(
        f"{API}/courses/{created.json()['id']}",
        headers=auth_header(token),
        json={"status": "published"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "COURSE_INCOMPLETE"
    assert "管理员确认的授课安排" in response.json()["detail"]["message"]
    assert "课程封面" in response.json()["detail"]["message"]


def test_publish_accepts_course_with_complete_basic_information(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    term_id, class_id = _academic_data(db_session_factory)
    course_id = _complete_course(client, db_session_factory, token, term_id, class_id)

    response = client.patch(
        f"{API}/courses/{course_id}",
        headers=auth_header(token),
        json={"status": "published"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"


# ── TASK-006：创建旁路关闭 ────────────────────────────────────


def test_create_course_rejects_published_status(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    response = client.post(
        f"{API}/courses",
        headers=auth_header(token),
        json={"title": "Bypass attempt", "status": "published"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_create_course_rejects_archived_status(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    response = client.post(
        f"{API}/courses",
        headers=auth_header(token),
        json={"title": "Bypass attempt", "status": "archived"},
    )
    assert response.status_code == 422, response.text


def test_create_course_accepts_draft_status(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    response = client.post(
        f"{API}/courses",
        headers=auth_header(token),
        json={"title": "Plain draft", "status": "draft"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "draft"


def test_update_course_rejects_unknown_status(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    created = client.post(
        f"{API}/courses",
        headers=auth_header(token),
        json={"title": "Draft course", "status": "draft"},
    )
    response = client.patch(
        f"{API}/courses/{created.json()['id']}",
        headers=auth_header(token),
        json={"status": "published_now"},
    )
    assert response.status_code == 422, response.text


def test_plain_update_does_not_trigger_publish(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    created = client.post(
        f"{API}/courses",
        headers=auth_header(token),
        json={"title": "Draft course", "status": "draft"},
    )
    # 不完整课程做普通字段更新（不带 status）不应触发发布门禁，也应保持 draft
    response = client.patch(
        f"{API}/courses/{created.json()['id']}",
        headers=auth_header(token),
        json={"title": "Renamed draft"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Renamed draft"
    assert body["status"] == "draft"


# ── Notebook 课时模板门禁 ─────────────────────────────────────


def test_publish_rejects_unconfigured_notebook_lesson(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    term_id, class_id = _academic_data(db_session_factory)
    course_id = _complete_course(client, db_session_factory, token, term_id, class_id)
    _add_notebook_lesson(db_session_factory, course_id, "未配置的实验课")

    response = client.patch(
        f"{API}/courses/{course_id}",
        headers=auth_header(token),
        json={"status": "published"},
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "NOTEBOOK_TEMPLATE_REQUIRED"
    assert "未配置的实验课" in detail["message"]


def test_publish_accepts_notebook_lesson_with_published_template(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    term_id, class_id = _academic_data(db_session_factory)
    course_id = _complete_course(client, db_session_factory, token, term_id, class_id)
    lesson_id = _add_notebook_lesson(db_session_factory, course_id)

    with db_session_factory() as db:
        owner_id = db.get(Lesson, lesson_id).chapter.course.teacher_id
    _publish_ready_template(db_session_factory, owner_id, lesson_id)

    response = client.patch(
        f"{API}/courses/{course_id}",
        headers=auth_header(token),
        json={"status": "published"},
    )
    assert response.status_code == 200, response.text


def test_create_published_notebook_lesson_rejected(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    created = client.post(
        f"{API}/courses",
        headers=auth_header(token),
        json={"title": "Draft course", "status": "draft"},
    )
    assert created.status_code == 201
    chapter = client.post(
        f"{API}/courses/{created.json()['id']}/chapters",
        headers=auth_header(token),
        json={"title": "Chapter"},
    )
    assert chapter.status_code == 201, chapter.text

    response = client.post(
        f"{API}/chapters/{chapter.json()['id']}/lessons",
        headers=auth_header(token),
        json={"title": "实验课", "content_type": "notebook", "status": "published"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "NOTEBOOK_TEMPLATE_REQUIRED"


def test_lesson_publish_requires_notebook_template(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    created = client.post(
        f"{API}/courses",
        headers=auth_header(token),
        json={"title": "Draft course", "status": "draft"},
    )
    chapter = client.post(
        f"{API}/courses/{created.json()['id']}/chapters",
        headers=auth_header(token),
        json={"title": "Chapter"},
    )
    lesson = client.post(
        f"{API}/chapters/{chapter.json()['id']}/lessons",
        headers=auth_header(token),
        json={"title": "草稿实验课", "content_type": "notebook", "status": "draft"},
    )
    assert lesson.status_code == 201, lesson.text

    blocked = client.patch(
        f"{API}/lessons/{lesson.json()['id']}",
        headers=auth_header(token),
        json={"status": "published"},
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["detail"]["code"] == "NOTEBOOK_TEMPLATE_REQUIRED"

    with db_session_factory() as db:
        teacher_id = db.get(Lesson, lesson.json()["id"]).chapter.course.teacher_id
    _publish_ready_template(db_session_factory, teacher_id, lesson.json()["id"])

    released = client.patch(
        f"{API}/lessons/{lesson.json()['id']}",
        headers=auth_header(token),
        json={"status": "published"},
    )
    assert released.status_code == 200, released.text


def test_switch_published_lesson_to_notebook_requires_template(client, db_session_factory):
    token = _teacher_token(client, db_session_factory)
    created = client.post(
        f"{API}/courses",
        headers=auth_header(token),
        json={"title": "Draft course", "status": "draft"},
    )
    chapter = client.post(
        f"{API}/courses/{created.json()['id']}/chapters",
        headers=auth_header(token),
        json={"title": "Chapter"},
    )
    lesson = client.post(
        f"{API}/chapters/{chapter.json()['id']}/lessons",
        headers=auth_header(token),
        json={"title": "图文课", "content_type": "markdown", "status": "published"},
    )
    assert lesson.status_code == 201, lesson.text

    switched = client.patch(
        f"{API}/lessons/{lesson.json()['id']}",
        headers=auth_header(token),
        json={"content_type": "notebook"},
    )
    assert switched.status_code == 422, switched.text
    assert switched.json()["detail"]["code"] == "NOTEBOOK_TEMPLATE_REQUIRED"
