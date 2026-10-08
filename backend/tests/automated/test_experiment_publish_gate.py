"""实验模块发布门禁测试：POST 仅 draft、PATCH 强类型（无 status/未知字段拒绝）、
publish 需模板就绪、unpublish 专用端点、角色可见性。"""

from app import models
from teaching_test_support import grant_experiment
from conftest import auth_header, create_user, login, seed_basic_environment

API = "/api/v1"


def _create_template_with_version(db_session_factory, name="模板"):
    """DB 领域 fixture：模板 + 已发布版本（current_version_id 就绪）"""
    seed_basic_environment(db_session_factory)
    with db_session_factory() as db:
        tmpl = models.NotebookTemplate(name=name, status="published", draft_cells=[], owner_id=1)
        db.add(tmpl)
        db.flush()
        ver = models.NotebookTemplateVersion(
            template_id=tmpl.id,
            version_number=1,
            sha256="a" * 64,
            cells=[],
            cell_order=[],
            published_by_id=1,
        )
        db.add(ver)
        db.flush()
        tmpl.current_version_id = ver.id
        db.commit()
        return tmpl.id


def _create_template_without_version(db_session_factory, name="无版本模板"):
    seed_basic_environment(db_session_factory)
    with db_session_factory() as db:
        tmpl = models.NotebookTemplate(name=name, status="draft", draft_cells=[], owner_id=1)
        db.add(tmpl)
        db.commit()
        return tmpl.id


def _module_owner(client, db_session_factory, username="teacher"):
    create_user(db_session_factory, username, "teacher")
    token, _ = login(client, username)
    return token


def _teacher(client, db_session_factory, username="teacher"):
    create_user(db_session_factory, username, "teacher")
    token, _ = login(client, username)
    return token


def _create_module(client, token, **payload):
    resp = client.post(
        f"{API}/experiments/modules",
        headers=auth_header(token),
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_module_rejects_published_status(client, db_session_factory):
    token = _module_owner(client, db_session_factory)
    resp = client.post(
        f"{API}/experiments/modules",
        headers=auth_header(token),
        json={"name": "旁路模块", "status": "published"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_patch_module_rejects_status_and_unknown_fields(client, db_session_factory):
    token = _module_owner(client, db_session_factory)
    module = _create_module(client, token, name="强类型模块")
    # status 字段不存在于 Update Schema（extra=forbid → 422）
    resp = client.patch(
        f"{API}/experiments/modules/{module['id']}",
        headers=auth_header(token),
        json={"status": "published"},
    )
    assert resp.status_code == 422, resp.text
    # 未知字段同样拒绝（不再静默丢弃）
    resp = client.patch(
        f"{API}/experiments/modules/{module['id']}",
        headers=auth_header(token),
        json={"entry_url": "https://example.com/x"},
    )
    assert resp.status_code == 422, resp.text
    # 合法元数据更新成功
    resp = client.patch(
        f"{API}/experiments/modules/{module['id']}",
        headers=auth_header(token),
        json={"name": "改名模块"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "改名模块"
    assert resp.json()["status"] == "draft"


def test_publish_requires_bound_template(client, db_session_factory):
    token = _module_owner(client, db_session_factory)
    module = _create_module(client, token, name="无模板模块")
    resp = client.post(
        f"{API}/experiments/modules/{module['id']}/publish",
        headers=auth_header(token),
    )
    assert resp.status_code == 422, resp.text
    # 教师创建模块时系统会自动绑定空白模板；尚未发布版本时不能发布。
    assert resp.json()["detail"]["code"] == "MODULE_TEMPLATE_NOT_READY"


def test_publish_rejects_template_without_version(client, db_session_factory):
    token = _module_owner(client, db_session_factory)
    tid = _create_template_without_version(db_session_factory)
    module = _create_module(client, token, name="模板无版本")
    with db_session_factory() as db:
        db.get(models.ExperimentModule, module["id"]).template_id = tid
        db.commit()
    resp = client.post(
        f"{API}/experiments/modules/{module['id']}/publish",
        headers=auth_header(token),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "MODULE_TEMPLATE_NOT_READY"


def test_publish_succeeds_with_ready_template(client, db_session_factory):
    token = _module_owner(client, db_session_factory)
    tid = _create_template_with_version(db_session_factory)
    module = _create_module(client, token, name="合法模块")
    with db_session_factory() as db:
        db.get(models.ExperimentModule, module["id"]).template_id = tid
        db.commit()
    resp = client.post(
        f"{API}/experiments/modules/{module['id']}/publish",
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "published"


def test_unpublish_returns_to_draft(client, db_session_factory):
    token = _module_owner(client, db_session_factory)
    tid = _create_template_with_version(db_session_factory)
    module = _create_module(client, token, name="下架模块")
    with db_session_factory() as db:
        db.get(models.ExperimentModule, module["id"]).template_id = tid
        db.commit()
    assert (
        client.post(
            f"{API}/experiments/modules/{module['id']}/publish",
            headers=auth_header(token),
        ).status_code
        == 200
    )
    resp = client.post(
        f"{API}/experiments/modules/{module['id']}/unpublish",
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "draft"
    # 重复取消发布 → 409
    resp = client.post(
        f"{API}/experiments/modules/{module['id']}/unpublish",
        headers=auth_header(token),
    )
    assert resp.status_code == 409


def test_non_owner_teacher_cannot_publish_or_unpublish(client, db_session_factory):
    owner_token = _module_owner(client, db_session_factory, "owner_teacher")
    teacher_token = _teacher(client, db_session_factory, "teacher_other")
    tid = _create_template_with_version(db_session_factory)
    module = _create_module(client, owner_token, name="他人模块")
    with db_session_factory() as db:
        db.get(models.ExperimentModule, module["id"]).template_id = tid
        db.commit()
    resp = client.post(
        f"{API}/experiments/modules/{module['id']}/publish",
        headers=auth_header(teacher_token),
    )
    assert resp.status_code == 403, resp.text


def test_student_only_sees_published_and_teacher_lists_own_modules(client, db_session_factory):
    """列表/详情可见性：学生只见 published；教师列表只见自己的模块。"""
    create_user(db_session_factory, "teacher_a", "teacher")
    create_user(db_session_factory, "teacher_b", "teacher")
    create_user(db_session_factory, "stu", "student")
    teacher_a_tok, _ = login(client, "teacher_a")
    teacher_b_tok, _ = login(client, "teacher_b")
    stu_tok, _ = login(client, "stu")
    tid = _create_template_with_version(db_session_factory)

    draft = _create_module(client, teacher_a_tok, name="A 的草稿")
    published = _create_module(client, teacher_a_tok, name="A 的已发布")
    with db_session_factory() as db:
        db.get(models.ExperimentModule, published["id"]).template_id = tid
        db.commit()
    assert (
        client.post(
            f"{API}/experiments/modules/{published['id']}/publish",
            headers=auth_header(teacher_a_tok),
        ).status_code
        == 200
    )

    # teacher_b 列表只见自己的（空），但可读 A 的模块元数据
    teacher_b_list = client.get(f"{API}/experiments/modules", headers=auth_header(teacher_b_tok)).json()
    assert all(item["id"] not in (draft["id"], published["id"]) for item in teacher_b_list["items"])
    assert (
        client.get(
            f"{API}/experiments/modules/{published['id']}", headers=auth_header(teacher_b_tok)
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"{API}/experiments/modules/{draft['id']}", headers=auth_header(teacher_b_tok)
        ).status_code
        == 200
    )

    grant_experiment(db_session_factory, "module", published["id"], ["stu"])
    # 学生列表只见 published
    stu_list = client.get(f"{API}/experiments/modules", headers=auth_header(stu_tok)).json()
    ids = [item["id"] for item in stu_list["items"]]
    assert published["id"] in ids
    assert draft["id"] not in ids
