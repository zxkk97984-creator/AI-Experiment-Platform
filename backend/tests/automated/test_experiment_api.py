"""实验 API 测试 — v5 统一模型后的验收测试"""
from sqlalchemy import select

from app.models import Course, ExperimentModule, ExperimentRecord, NotebookTemplate, User

from teaching_test_support import grant_course_students, grant_experiment
from conftest import auth_header, create_course_db, create_user, login, seed_basic_environment


def _seed_student_catalog(db_session_factory):
    seed_basic_environment(db_session_factory)
    """创建四态目录数据，并放入一条其他学生记录验证隔离。"""
    from datetime import datetime, timedelta, timezone

    from app.models import (
        ExperimentModule,
        ExperimentRecord,
        NotebookTemplate,
        NotebookTemplateVersion,
    )

    owner = create_user(db_session_factory, "catalog_owner", "teacher")
    student = create_user(db_session_factory, "catalog_student", "student")
    other_student = create_user(db_session_factory, "catalog_other", "student")

    with db_session_factory() as db:
        template = NotebookTemplate(
            name="Catalog template",
            status="published",
            owner_id=owner.id,
        )
        db.add(template)
        db.flush()
        version = NotebookTemplateVersion(
            template_id=template.id,
            version_number=1,
            sha256="catalog-v1",
            cells=[],
            published_by_id=owner.id,
        )
        db.add(version)
        db.flush()

        template.current_version_id = version.id
        course = Course(title="Catalog course", teacher_id=owner.id, status="published")
        db.add(course); db.flush()
        course_id = course.id
        modules = [
            ExperimentModule(name="01 Python 入门", status="published", owner_id=owner.id),
            ExperimentModule(name="02 NumPy 基础", status="published", owner_id=owner.id),
            ExperimentModule(name="03 可视化", status="published", owner_id=owner.id),
            ExperimentModule(name="04 数据清洗", status="published", owner_id=owner.id),
            ExperimentModule(name="隐藏草稿", status="draft", owner_id=owner.id),
        ]
        for module in modules: module.template_id = template.id
        db.add_all(modules)
        db.flush()

        now = datetime.now(timezone.utc)
        db.add_all([
            ExperimentRecord(
                module_id=modules[0].id,
                template_version_id=version.id,
                student_id=student.id,
                status="started",
                updated_at=now - timedelta(days=3),
            ),
            ExperimentRecord(
                module_id=modules[1].id,
                template_version_id=version.id,
                student_id=student.id,
                status="submitted",
                updated_at=now - timedelta(days=2),
            ),
            ExperimentRecord(
                module_id=modules[2].id,
                template_version_id=version.id,
                student_id=student.id,
                status="graded",
                updated_at=now - timedelta(days=1),
            ),
            ExperimentRecord(
                module_id=modules[3].id,
                template_version_id=version.id,
                student_id=other_student.id,
                status="graded",
                updated_at=now,
            ),
        ])
        db.commit()

    for module in modules[:4]:
        offering_id = grant_experiment(db_session_factory, 'module', module.id,
            ['catalog_student', 'catalog_other'], course_id=course_id)
        with db_session_factory() as db:
            for row in db.scalars(select(ExperimentRecord).where(ExperimentRecord.module_id == module.id)):
                row.offering_id, row.scope_key = offering_id, f'offering:{offering_id}'
                # Granting setup authority must not replace the intentionally
                # distinct history timestamps under test.
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(row, 'updated_at')
            db.commit()
    return student


def test_student_catalog_merges_status_summary_and_isolates_records(client, db_session_factory):
    _seed_student_catalog(db_session_factory)
    token, _ = login(client, "catalog_student")

    response = client.get(
        "/api/v1/experiments/modules/student-catalog",
        headers=auth_header(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["learning_status"] for item in body["items"]] == [
        "started", "submitted", "graded", "not_started",
    ]
    assert body["summary"] == {
        "total": 4,
        "not_started": 1,
        "started": 1,
        "submitted": 1,
        "graded": 1,
    }
    assert body["items"][3]["last_learning_at"] is None
    assert all(item["name"] != "隐藏草稿" for item in body["items"])


def test_student_catalog_search_filter_sort_and_pagination(client, db_session_factory):
    _seed_student_catalog(db_session_factory)
    token, _ = login(client, "catalog_student")
    headers = auth_header(token)

    filtered = client.get(
        "/api/v1/experiments/modules/student-catalog?status=submitted&q=NumPy",
        headers=headers,
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["name"] == "02 NumPy 基础"
    assert filtered.json()["summary"]["total"] == 4

    recent = client.get(
        "/api/v1/experiments/modules/student-catalog?sort=recent_desc&page=2&page_size=2",
        headers=headers,
    )
    assert recent.status_code == 200, recent.text
    assert recent.json()["page"] == 2
    assert recent.json()["page_size"] == 2
    assert recent.json()["total"] == 4
    assert [item["name"] for item in recent.json()["items"]] == [
        "01 Python 入门", "04 数据清洗",
    ]


def _new_catalog_offering(db, student, module, course, code):
    """Explicit fixture grant for a second term using the shared module."""
    from datetime import date
    from app.models import AcademicTerm, TeachingClass
    from app.models.teaching import CourseOffering, OfferingExperiment
    from app.services.offering_service import enroll_student

    term = AcademicTerm(code=code, name=f"学期 {code}", start_date=date(2027, 9, 1),
                        end_date=date(2028, 1, 1), status="active")
    db.add(term); db.flush()
    klass = TeachingClass(academic_term_id=term.id, code=code, name=f"教学班 {code}", status="active")
    db.add(klass); db.flush()
    offering = CourseOffering(course_id=course.id, academic_term_id=term.id,
                              teaching_class_id=klass.id, teacher_id=course.teacher_id, status="active")
    db.add(offering); db.flush()
    enroll_student(db, offering, student.id)
    version_id = db.get(NotebookTemplate, module.template_id).current_version_id
    db.add(OfferingExperiment(offering_id=offering.id, entry_kind="module", entry_id=module.id,
                             template_version_id=version_id, status="published"))
    db.flush()
    return offering, version_id


def test_student_catalog_new_term_does_not_reuse_archived_record(client, db_session_factory):
    from app.models import AcademicTerm
    from app.models.teaching import CourseOffering

    student = _seed_student_catalog(db_session_factory)
    with db_session_factory() as db:
        module = db.scalar(select(ExperimentModule).where(ExperimentModule.name == "03 可视化"))
        old_record = db.scalar(select(ExperimentRecord).where(
            ExperimentRecord.module_id == module.id, ExperimentRecord.student_id == student.id))
        old_offering = db.get(CourseOffering, old_record.offering_id)
        old_offering.status = "archived"
        db.get(AcademicTerm, old_offering.academic_term_id).status = "closed"
        course = db.get(Course, old_offering.course_id)
        new_offering, _ = _new_catalog_offering(db, student, module, course, "NEW-TERM")
        new_id, old_record_id = new_offering.id, old_record.id
        db.commit()

    token, _ = login(client, "catalog_student")
    response = client.get("/api/v1/experiments/modules/student-catalog", headers=auth_header(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["summary"] == {"total": 1, "not_started": 1, "started": 0, "submitted": 0, "graded": 0}
    item = body["items"][0]
    assert item["offering_id"] == new_id and item["learning_status"] == "not_started"
    assert item["last_learning_at"] is None
    assert (item["course_title"], item["term_name"], item["class_name"]) == (
        "Catalog course", "学期 NEW-TERM", "教学班 NEW-TERM")
    with db_session_factory() as db:
        assert db.get(ExperimentRecord, old_record_id).status == "graded"


def test_student_catalog_separates_shared_module_offerings_and_their_records(client, db_session_factory):
    from app.models.teaching import CourseOffering

    student = _seed_student_catalog(db_session_factory)
    with db_session_factory() as db:
        module = db.scalar(select(ExperimentModule).where(ExperimentModule.name == "02 NumPy 基础"))
        old_record = db.scalar(select(ExperimentRecord).where(
            ExperimentRecord.module_id == module.id, ExperimentRecord.student_id == student.id))
        original_id = old_record.offering_id
        course = db.get(Course, db.get(CourseOffering, original_id).course_id)
        new_offering, version_id = _new_catalog_offering(db, student, module, course, "PARALLEL")
        db.add(ExperimentRecord(module_id=module.id, template_version_id=version_id,
            student_id=student.id, offering_id=new_offering.id, scope_key=f"offering:{new_offering.id}", status="graded"))
        other_student = db.scalar(select(User).where(User.username == "catalog_other"))
        _new_catalog_offering(db, other_student, module, course, "OTHER-CLASS")
        new_id, module_id = new_offering.id, module.id
        db.commit()

    token, _ = login(client, "catalog_student")
    headers = auth_header(token)
    response = client.get("/api/v1/experiments/modules/student-catalog", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 5
    assert body["summary"] == {"total": 5, "not_started": 1, "started": 1, "submitted": 1, "graded": 2}
    assert [(item["offering_id"], item["learning_status"]) for item in body["items"] if item["id"] == module_id] == [
        (original_id, "submitted"), (new_id, "graded")]
    assert len({(item["offering_id"], item["id"]) for item in body["items"]}) == 5
    filtered = client.get("/api/v1/experiments/modules/student-catalog?status=graded&q=NumPy", headers=headers)
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1 and filtered.json()["items"][0]["offering_id"] == new_id
    assert filtered.json()["summary"] == body["summary"]
    paged = client.get("/api/v1/experiments/modules/student-catalog?page=2&page_size=2", headers=headers)
    assert paged.status_code == 200, paged.text
    assert paged.json()["total"] == 5
    assert [(item["id"], item["offering_id"]) for item in paged.json()["items"]][0] == (module_id, new_id)


def test_student_catalog_rejects_non_student_roles(client, db_session_factory):
    create_user(db_session_factory, "catalog_teacher", "teacher")
    token, _ = login(client, "catalog_teacher")
    response = client.get(
        "/api/v1/experiments/modules/student-catalog",
        headers=auth_header(token),
    )
    assert response.status_code == 403


def test_teacher_can_create_and_update_own_module_only(client, db_session_factory):
    """教师创建模块时同时获得一个可编辑的空白 Notebook。"""
    teacher = create_user(db_session_factory, "teacher_module", "teacher")
    create_user(db_session_factory, "teacher_module_other", "teacher")
    teacher_tok, _ = login(client, "teacher_module")
    other_teacher_tok, _ = login(client, "teacher_module_other")

    own = client.post(
        "/api/v1/experiments/modules",
        headers=auth_header(teacher_tok),
        json={"name": "教师实验模块", "description": "教师创建"},
    )
    assert own.status_code == 201, own.text
    assert own.json()["template_id"] is not None

    with db_session_factory() as db:
        module = db.get(ExperimentModule, own.json()["id"])
        template = db.get(NotebookTemplate, own.json()["template_id"])
        assert module.template_id == template.id
        assert module.owner_id == teacher.id
        assert template.owner_id == teacher.id
        assert template.draft_cells == []
        assert db.scalar(
            select(ExperimentModule).where(
                ExperimentModule.template_id == template.id
            )
        ).id == module.id

    own_update = client.post(
        f"/api/v1/experiments/modules/{own.json()['id']}/publish",
        headers=auth_header(teacher_tok),
    )
    # 空白模板尚未发布版本 → 仍不能直接发布模块
    assert own_update.status_code == 422, own_update.text
    assert own_update.json()["detail"]["code"] == "MODULE_TEMPLATE_NOT_READY"

    other = client.post(
        "/api/v1/experiments/modules",
        headers=auth_header(other_teacher_tok),
        json={"name": "其他教师实验模块"},
    )
    assert other.status_code == 201, other.text

    listed = client.get(
        "/api/v1/experiments/modules",
        headers=auth_header(teacher_tok),
    )
    assert [item["id"] for item in listed.json()["items"]] == [own.json()["id"]]

    other_update = client.patch(
        f"/api/v1/experiments/modules/{other.json()['id']}",
        headers=auth_header(teacher_tok),
        json={"name": "越权改名"},
    )
    assert other_update.status_code == 403


def test_admin_cannot_create_experiment_module(client, db_session_factory):
    create_user(db_session_factory, "module_admin", "admin")
    admin_token, _ = login(client, "module_admin")

    response = client.post(
        "/api/v1/experiments/modules",
        headers=auth_header(admin_token),
        json={"name": "管理员不应创建的模块"},
    )

    assert response.status_code == 403, response.text


def test_teacher_can_initialize_legacy_module_without_template(
    client, db_session_factory
):
    """历史孤立模块首次编辑时自动补齐空白 Notebook，且操作幂等。"""
    teacher = create_user(db_session_factory, "legacy_module_teacher", "teacher")
    teacher_token, _ = login(client, "legacy_module_teacher")

    with db_session_factory() as db:
        module = ExperimentModule(
            name="历史孤立实验",
            description="需要恢复编辑器",
            owner_id=teacher.id,
            status="draft",
        )
        db.add(module)
        db.commit()
        db.refresh(module)
        module_id = module.id

    first = client.post(
        f"/api/v1/experiments/modules/{module_id}/template",
        headers=auth_header(teacher_token),
    )
    assert first.status_code == 200, first.text
    template_id = first.json()["template_id"]
    assert template_id is not None

    second = client.post(
        f"/api/v1/experiments/modules/{module_id}/template",
        headers=auth_header(teacher_token),
    )
    assert second.status_code == 200, second.text
    assert second.json()["template_id"] == template_id


def test_student_cannot_read_draft_module(client, db_session_factory):
    """学生不能查看 draft 状态的实验模块"""
    create_user(db_session_factory, "teacher1", "teacher")
    create_user(db_session_factory, "student2", "student")
    d_tok, _ = login(client, "teacher1")
    s_tok, _ = login(client, "student2")

    m = client.post("/api/v1/experiments/modules", headers=auth_header(d_tok), json={
        "name": "Draft Module", "status": "draft",
    })
    assert m.status_code == 201
    mid = m.json()["id"]

    r = client.get("/api/v1/experiments/modules", headers=auth_header(s_tok))
    assert r.status_code == 200
    ids = [item["id"] for item in r.json()["items"]]
    assert mid not in ids


def test_module_publish_requires_template_ready(client, db_session_factory):
    """模块发布走专用端点；PATCH 不再能改 status"""
    create_user(db_session_factory, "teacher2", "teacher")
    d_tok, _ = login(client, "teacher2")

    m = client.post("/api/v1/experiments/modules", headers=auth_header(d_tok), json={
        "name": "To Publish", "status": "draft",
    })
    mid = m.json()["id"]

    # PATCH status 被拒绝（status 不在 Update Schema 中）
    r = client.patch(f"/api/v1/experiments/modules/{mid}", headers=auth_header(d_tok), json={
        "status": "published",
    })
    assert r.status_code == 422, r.text

    # 创建教师模块会自动绑定空白模板；模板尚无已发布版本时不能发布
    r = client.post(f"/api/v1/experiments/modules/{mid}/publish", headers=auth_header(d_tok))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "MODULE_TEMPLATE_NOT_READY"


def test_notebooks_deprecation_header(client, db_session_factory):
    """旧 notebooks API 返回 Deprecation 头"""
    create_user(db_session_factory, "student_dep", "student")
    tok, _ = login(client, "student_dep")
    r = client.get("/api/v1/notebooks/999", headers=auth_header(tok))
    assert r.headers.get("Deprecation") == "true"


def test_ensure_record_validation(client, db_session_factory):
    """ensure-for-lesson 不存在模板时返回 TEMPLATE_NOT_FOUND"""
    create_user(db_session_factory, "teacher_t", "teacher")
    create_user(db_session_factory, "student_s", "student")
    t_tok, _ = login(client, "teacher_t")
    s_tok, _ = login(client, "student_s")

    cid = create_course_db(
        db_session_factory, teacher_username="teacher_t", title="Test",
        status="published", visibility="public",
    )
    ch = client.post(f"/api/v1/courses/{cid}/chapters", headers=auth_header(t_tok), json={
        "title": "Ch1",
    })
    le = client.post(f"/api/v1/chapters/{ch.json()['id']}/lessons", headers=auth_header(t_tok), json={
        "title": "Lesson", "content_type": "markdown",
    })
    grant_course_students(db_session_factory, cid, ["student_s"])
    with db_session_factory() as db:
        from app.models import Lesson
        db.get(Lesson, le.json()["id"]).status = "published"
        db.commit()

    r = client.post(
        f"/api/v1/experiments/records/ensure-for-lesson/{le.json()['id']}",
        headers=auth_header(s_tok),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "EXPERIMENT_NOT_RELEASED"


def test_p0_4_teacher_submission_isolation(client, db_session_factory):
    """P0-4: 教师 A 不能看到教师 B 课程的实验提交"""
    # 创建教师 A 和课程 A
    create_user(db_session_factory, "t_iso_a", "teacher")
    create_user(db_session_factory, "s_iso_a", "student")
    t_a_tok, _ = login(client, "t_iso_a")
    s_a_tok, _ = login(client, "s_iso_a")

    # 教师 B 和课程 B
    create_user(db_session_factory, "t_iso_b", "teacher")
    create_user(db_session_factory, "s_iso_b", "student")
    t_b_tok, _ = login(client, "t_iso_b")
    s_b_tok, _ = login(client, "s_iso_b")

    # 教师 A 创建课程、章节、课时
    cid_a = create_course_db(
        db_session_factory, teacher_username="t_iso_a", title="教师A的课程",
        status="published", visibility="public",
    )
    client.post(f"/api/v1/courses/{cid_a}/enroll", headers=auth_header(s_a_tok))

    ch_a = client.post(f"/api/v1/courses/{cid_a}/chapters", headers=auth_header(t_a_tok), json={
        "title": "第一章", "order_index": 1,
    })
    chid_a = ch_a.json()["id"]

    # 需要创建模板才能创建课时（使用测试的 db_session_factory）
    from app.models import NotebookTemplate, NotebookTemplateVersion
    with db_session_factory() as db:
        tpl = NotebookTemplate(name="tpl_a", status="published", owner_id=1)
        db.add(tpl)
        db.flush()
        ver = NotebookTemplateVersion(
            template_id=tpl.id, version_number=1, sha256="abc",
            cells=[{"id": "c1", "type": "code", "source": "print(1)", "order": 0}],
            published_by_id=1,
        )
        db.add(ver)
        db.flush()
        tpl.current_version_id = ver.id
        db.commit()
        tpl_a_id = tpl.id

    les_a = client.post(f"/api/v1/chapters/{chid_a}/lessons", headers=auth_header(t_a_tok), json={
        "title": "课时A", "content_type": "markdown", "content": "# A",
        "order_index": 1,
    })
    lid_a = les_a.json()["id"]
    # 设置 template_id（LessonCreate schema 不含此字段，需单独更新）
    from app.database import SessionLocal as _SL
    from app.models import Lesson as _Lesson
    with db_session_factory() as db:
        lesson = db.get(_Lesson, lid_a)
        lesson.template_id = tpl_a_id
        db.commit()

    grant_experiment(db_session_factory, "lesson", lid_a, ["s_iso_a"])
    # 学生 A 创建实验记录并提交
    rec_a = client.post(f"/api/v1/experiments/records/ensure-for-lesson/{lid_a}",
                        headers=auth_header(s_a_tok))
    rid_a = rec_a.json()["id"]
    sub_a = client.post(f"/api/v1/experiments/records/{rid_a}/submit",
                        headers=auth_header(s_a_tok),
                        json={"client_request_id": "00000000-0000-0000-0000-000000000001"})
    assert sub_a.status_code == 201

    # 教师 A 可以查看自己课程的提交
    r_a = client.get("/api/v1/experiments/submissions", headers=auth_header(t_a_tok))
    assert r_a.status_code == 200
    items_a = r_a.json()["items"]
    assert len(items_a) >= 1, "教师 A 应该能看到自己课程的提交"

    # 教师 B 不应该看到教师 A 课程的提交（教师 B 没有任何课程）
    r_b = client.get("/api/v1/experiments/submissions", headers=auth_header(t_b_tok))
    assert r_b.status_code == 200
    items_b = r_b.json()["items"]
    assert len(items_b) == 0, f"教师 B 不应看到其他教师的提交，但看到了 {len(items_b)} 条"


def test_p0_4b_teacher_sees_own_module_submissions(client, db_session_factory):
    """回归：教师能看到自己实验模块（module 链路）的提交，其他教师不可见。

    曾按 Course.teacher_id 过滤教师可见提交，模块实验的 lesson_id 为 NULL、
    course 链路为空，所有模块实验提交被排除，教师端提交列表恒为空。
    """
    create_user(db_session_factory, "t_mod_a", "teacher")
    create_user(db_session_factory, "t_mod_b", "teacher")
    create_user(db_session_factory, "s_mod_a", "student")
    t_a_tok, _ = login(client, "t_mod_a")
    t_b_tok, _ = login(client, "t_mod_b")
    s_a_tok, _ = login(client, "s_mod_a")

    # 教师 A 创建实验模块（系统自动创建 Notebook 模板并绑定）
    mod = client.post("/api/v1/experiments/modules", headers=auth_header(t_a_tok),
                      json={"name": "模块A", "description": "desc"})
    assert mod.status_code == 201, mod.text
    mod_id = mod.json()["id"]
    tpl_id = mod.json()["template_id"]
    assert tpl_id, "创建模块应自动生成模板"

    # 发布模板版本 → 发布模块（与真实教师操作一致）
    pub_tpl = client.post(f"/api/v1/studio/templates/{tpl_id}/publish",
                          headers=auth_header(t_a_tok))
    assert pub_tpl.status_code == 201, pub_tpl.text
    pub_mod = client.post(f"/api/v1/experiments/modules/{mod_id}/publish",
                          headers=auth_header(t_a_tok))
    assert pub_mod.status_code == 200, pub_mod.text

    # 学生 A 创建模块实验记录并提交
    grant_experiment(db_session_factory, "module", mod_id, ["s_mod_a"])
    rec = client.post(f"/api/v1/experiments/records/ensure-for-module/{mod_id}",
                      headers=auth_header(s_a_tok))
    assert rec.status_code == 200, rec.text
    rid = rec.json()["id"]
    sub = client.post(f"/api/v1/experiments/records/{rid}/submit",
                      headers=auth_header(s_a_tok),
                      json={"client_request_id": "00000000-0000-0000-0000-000000000099"})
    assert sub.status_code == 201, sub.text
    sub_id = sub.json()["id"]

    # 教师 A 应能看到自己模块的提交
    r_a = client.get("/api/v1/experiments/submissions", headers=auth_header(t_a_tok))
    assert r_a.status_code == 200
    items_a = r_a.json()["items"]
    assert any(it["id"] == sub_id for it in items_a), \
        "教师 A 应能看到自己实验模块的提交"

    # 教师 B 不应看到教师 A 模块的提交
    r_b = client.get("/api/v1/experiments/submissions", headers=auth_header(t_b_tok))
    assert r_b.status_code == 200
    assert all(it["id"] != sub_id for it in r_b.json()["items"]), \
        "教师 B 不应看到其他教师模块的提交"
