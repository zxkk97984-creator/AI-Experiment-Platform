from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile

import nbformat
import pytest

from app.models import (
    Chapter,
    Course,
    CourseEnrollment,
    ExperimentModule,
    ExperimentRecord,
    Lesson,
    NotebookTemplate,
    NotebookTemplateVersion,
)
from conftest import auth_header, create_user, login, seed_basic_environment
from teaching_test_support import grant_experiment


def _cell(
    cell_id: str,
    source: str,
    order: int,
    *,
    cell_type: str = "code",
    editable: bool = True,
    hidden: bool = False,
) -> dict:
    return {
        "id": cell_id,
        "type": cell_type,
        "source": source,
        "order": order,
        "student_editable": editable,
        "source_hidden": hidden,
    }


@pytest.fixture()
def studio_context(client, db_session_factory):
    users = {}
    tokens = {}
    for username, role in (
        ("studio_admin", "admin"),
        ("studio_teacher", "teacher"),
        ("studio_teacher_other", "teacher"),
        ("studio_module_owner", "teacher"),
        ("studio_module_other", "teacher"),
        ("studio_student", "student"),
        ("studio_student_new", "student"),
    ):
        users[username] = create_user(db_session_factory, username, role)
        tokens[username], _ = login(client, username)

    seed_basic_environment(db_session_factory)
    with db_session_factory() as db:
        course = Course(
            title="Studio course",
            status="published",
            teacher_id=users["studio_teacher"].id,
        )
        other_course = Course(
            title="Other course",
            status="published",
            teacher_id=users["studio_teacher_other"].id,
        )
        db.add_all([course, other_course])
        db.flush()
        chapter = Chapter(course_id=course.id, title="Chapter", order_index=0)
        other_chapter = Chapter(
            course_id=other_course.id, title="Other chapter", order_index=0
        )
        db.add_all([chapter, other_chapter])
        db.flush()
        lesson = Lesson(chapter_id=chapter.id, title="Lesson", order_index=0)
        other_lesson = Lesson(
            chapter_id=other_chapter.id, title="Other lesson", order_index=0
        )
        own_module = ExperimentModule(
            name="Independent module",
            status="published",
            owner_id=users["studio_module_owner"].id,
        )
        teacher_module = ExperimentModule(
            name="Teacher module",
            status="draft",
            owner_id=users["studio_teacher"].id,
        )
        other_module = ExperimentModule(
            name="Other independent module",
            status="draft",
            owner_id=users["studio_module_other"].id,
        )
        db.add_all([lesson, other_lesson, own_module, teacher_module, other_module])
        db.flush()
        db.add_all(
            [
                CourseEnrollment(
                    course_id=course.id,
                    student_id=users["studio_student"].id,
                    status="enrolled",
                ),
                CourseEnrollment(
                    course_id=course.id,
                    student_id=users["studio_student_new"].id,
                    status="enrolled",
                ),
            ]
        )
        db.commit()
        ids = {
            "course_id": course.id,
            "lesson_id": lesson.id,
            "other_lesson_id": other_lesson.id,
            "module_id": own_module.id,
            "teacher_module_id": teacher_module.id,
            "other_module_id": other_module.id,
        }

    return {"users": users, "tokens": tokens, **ids}


def _headers(ctx, username):
    return auth_header(ctx["tokens"][username])


def _create_teacher_template(client, ctx, name="Teacher template"):
    response = client.post(
        "/api/v1/studio/templates",
        headers=_headers(ctx, "studio_teacher"),
        json={"name": name, "lesson_id": ctx["lesson_id"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _save_draft(client, ctx, template_id, revision, cells):
    return client.put(
        f"/api/v1/studio/templates/{template_id}/draft",
        headers=_headers(ctx, "studio_teacher"),
        json={"draft_revision": revision, "cells": cells},
    )


def test_studio_roles_owner_listing_and_context_binding(client, db_session_factory, studio_context):
    ctx = studio_context
    student = client.get(
        "/api/v1/studio/templates",
        headers=_headers(ctx, "studio_student"),
    )
    assert student.status_code == 403

    teacher_template = _create_teacher_template(client, ctx)
    assert teacher_template["draft_cells"] == []
    assert teacher_template["draft_revision"] == 1
    assert teacher_template["lesson_id"] == ctx["lesson_id"]

    with db_session_factory() as db:
        assert db.get(Lesson, ctx["lesson_id"]).template_id == teacher_template["id"]

    forbidden_context = client.post(
        "/api/v1/studio/templates",
        headers=_headers(ctx, "studio_teacher"),
        json={"name": "Cannot claim", "lesson_id": ctx["other_lesson_id"]},
    )
    assert forbidden_context.status_code == 403

    module_template = client.post(
        "/api/v1/studio/templates",
        headers=_headers(ctx, "studio_module_owner"),
        json={"name": "Independent", "module_id": ctx["module_id"]},
    )
    assert module_template.status_code == 201, module_template.text
    assert module_template.json()["module_id"] == ctx["module_id"]

    teacher_cannot_claim_module = client.post(
        "/api/v1/studio/templates",
        headers=_headers(ctx, "studio_teacher"),
        json={"name": "No module", "module_id": ctx["module_id"]},
    )
    assert teacher_cannot_claim_module.status_code == 403

    teacher_can_bind_own_module = client.post(
        f"/api/v1/studio/templates/{teacher_template['id']}/bind",
        headers=_headers(ctx, "studio_teacher"),
        json={"module_id": ctx["teacher_module_id"]},
    )
    assert teacher_can_bind_own_module.status_code == 200, teacher_can_bind_own_module.text
    assert teacher_can_bind_own_module.json()["module_id"] == ctx["teacher_module_id"]

    own = client.get(
        "/api/v1/studio/templates",
        headers=_headers(ctx, "studio_teacher"),
    )
    assert [item["id"] for item in own.json()] == [teacher_template["id"]]

    admin = client.get(
        "/api/v1/studio/templates",
        headers=_headers(ctx, "studio_admin"),
    )
    assert {item["id"] for item in admin.json()} == {
        teacher_template["id"],
        module_template.json()["id"],
    }


@pytest.mark.parametrize(
    ("operation", "method"),
    [
        ("", "get"),
        ("/draft", "put"),
        ("/publish", "post"),
        ("/versions", "get"),
        ("/export", "get"),
        ("/preview/run", "post"),
    ],
)
def test_cross_owner_is_rejected_for_every_studio_surface(
    client, studio_context, operation, method
):
    ctx = studio_context
    template = _create_teacher_template(client, ctx)
    url = f"/api/v1/studio/templates/{template['id']}{operation}"
    kwargs = {"headers": _headers(ctx, "studio_teacher_other")}
    if operation == "/draft":
        kwargs["json"] = {"draft_revision": 1, "cells": []}
    elif operation == "/preview/run":
        kwargs["json"] = {"cell_id": "missing"}
    response = getattr(client, method)(url, **kwargs)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "FORBIDDEN"


def test_metadata_patch_does_not_touch_draft_or_published_snapshot(client, studio_context):
    ctx = studio_context
    template = _create_teacher_template(client, ctx)
    cells = [_cell("c1", "print(1)", 7)]
    saved = _save_draft(client, ctx, template["id"], 1, cells)
    assert saved.status_code == 200, saved.text
    published = client.post(
        f"/api/v1/studio/templates/{template['id']}/publish",
        headers=_headers(ctx, "studio_teacher"),
    )
    assert published.status_code == 201, published.text

    patched = client.patch(
        f"/api/v1/studio/templates/{template['id']}",
        headers=_headers(ctx, "studio_teacher"),
        json={"name": "Renamed", "description": "Metadata only"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["draft_cells"] == [
        _cell("c1", "print(1)", 0)
    ]
    assert patched.json()["current_version"]["cells"] == [
        _cell("c1", "print(1)", 0)
    ]


@pytest.mark.parametrize(
    "cells",
    [
        [_cell("", "x", 0)],
        [_cell("dup", "x", 0), _cell("dup", "y", 1)],
        [_cell("bad", "x", 0, cell_type="raw")],
        [_cell("hidden-md", "# secret", 0, cell_type="markdown", hidden=True)],
    ],
)
def test_draft_cell_validation_and_revision_conflict(client, studio_context, cells):
    ctx = studio_context
    template = _create_teacher_template(client, ctx)
    invalid = _save_draft(client, ctx, template["id"], 1, cells)
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["detail"]["code"] == "VALIDATION_ERROR"

    good = _save_draft(
        client,
        ctx,
        template["id"],
        1,
        [_cell("second", "2", 10), _cell("first", "1", -2)],
    )
    assert good.status_code == 200, good.text
    assert good.json()["draft_revision"] == 2
    assert [(c["id"], c["order"]) for c in good.json()["draft_cells"]] == [
        ("first", 0),
        ("second", 1),
    ]

    stale = _save_draft(client, ctx, template["id"], 1, [])
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "REVISION_CONFLICT",
        "message": "草稿已被其他会话修改，请刷新后重试",
        "fields": {"current_revision": 2},
    }


def _notebook_bytes() -> bytes:
    notebook = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_markdown_cell("# Intro", id="intro"),
            nbformat.v4.new_code_cell(
                "print('hello')",
                id="code",
                execution_count=9,
                outputs=[nbformat.v4.new_output("stream", name="stdout", text="hello\n")],
                metadata={"dai": {"student_editable": False, "source_hidden": True}},
            ),
        ],
        metadata={"kernelspec": {"name": "python3", "display_name": "Python 3"}},
    )
    return nbformat.writes(notebook).encode("utf-8")


def test_ipynb_import_clears_outputs_and_existing_import_uses_revision(
    client, studio_context
):
    ctx = studio_context
    imported = client.post(
        "/api/v1/studio/templates/import",
        headers=_headers(ctx, "studio_teacher"),
        data={"name": "Imported", "lesson_id": str(ctx["lesson_id"])},
        files={"file": ("lesson.ipynb", _notebook_bytes(), "application/x-ipynb+json")},
    )
    assert imported.status_code == 201, imported.text
    body = imported.json()
    assert [cell["id"] for cell in body["draft_cells"]] == ["intro", "code"]
    assert body["draft_cells"][1]["student_editable"] is False
    assert body["draft_cells"][1]["source_hidden"] is True

    stale = client.post(
        f"/api/v1/studio/templates/{body['id']}/import",
        headers=_headers(ctx, "studio_teacher"),
        data={"draft_revision": "0"},
        files={"file": ("replacement.ipynb", _notebook_bytes())},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "REVISION_CONFLICT"


def _zip_bytes(entries: list[tuple[str, bytes, int | None]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content, mode in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            archive.writestr(info, content)
    return buffer.getvalue()


def test_zip_import_assets_are_relative_and_invalid_archives_leave_no_orphans(
    client, db_session_factory, studio_context, test_settings
):
    ctx = studio_context
    valid_zip = _zip_bytes(
        [
            ("lesson.ipynb", _notebook_bytes(), None),
            ("assets/data.csv", b"x,y\n1,2\n", None),
        ]
    )
    valid = client.post(
        "/api/v1/studio/templates/import",
        headers=_headers(ctx, "studio_teacher"),
        data={"name": "ZIP", "lesson_id": str(ctx["lesson_id"])},
        files={"file": ("lesson.zip", valid_zip, "application/zip")},
    )
    assert valid.status_code == 201, valid.text
    assets_dir = valid.json()["draft_assets_dir"]
    assert assets_dir and not assets_dir.startswith(("/", "\\"))
    assert ":" not in assets_dir
    assert (test_settings.studio_storage_path / assets_dir / "assets" / "data.csv").is_file()

    with db_session_factory() as db:
        before = db.query(NotebookTemplate).count()

    for filename, payload in (
        (
            "traversal.zip",
            _zip_bytes(
                [
                    ("lesson.ipynb", _notebook_bytes(), None),
                    ("../escape.txt", b"escape", None),
                ]
            ),
        ),
        (
            "ambiguous.zip",
            _zip_bytes(
                [
                    ("one.ipynb", _notebook_bytes(), None),
                    ("two.ipynb", _notebook_bytes(), None),
                ]
            ),
        ),
        (
            "symlink.zip",
            _zip_bytes(
                [
                    ("lesson.ipynb", _notebook_bytes(), None),
                    ("assets/link.txt", b"target", stat.S_IFLNK | 0o777),
                ]
            ),
        ),
    ):
        invalid = client.post(
            "/api/v1/studio/templates/import",
            headers=_headers(ctx, "studio_teacher"),
            data={"name": filename, "lesson_id": str(ctx["lesson_id"])},
            files={"file": (filename, payload, "application/zip")},
        )
        assert invalid.status_code == 400, invalid.text

    with db_session_factory() as db:
        assert db.query(NotebookTemplate).count() == before


def test_publish_history_sha_immutability_and_student_version_pinning(
    client, db_session_factory, studio_context
):
    ctx = studio_context
    template = _create_teacher_template(client, ctx)
    v1_cells = [_cell("c", "value = 1", 0)]
    assert _save_draft(client, ctx, template["id"], 1, v1_cells).status_code == 200
    v1_response = client.post(
        f"/api/v1/studio/templates/{template['id']}/publish",
        headers=_headers(ctx, "studio_teacher"),
    )
    assert v1_response.status_code == 201, v1_response.text
    v1 = v1_response.json()
    canonical = json.dumps(
        {"cells": v1_cells, "metadata": {}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert v1["sha256"] == hashlib.sha256(canonical).hexdigest()

    grant_experiment(db_session_factory, 'lesson', ctx['lesson_id'],
                     ['studio_student', 'studio_student_new'], template_version_id=v1['id'])

    old_record = client.post(
        f"/api/v1/experiments/records/ensure-for-lesson/{ctx['lesson_id']}",
        headers=_headers(ctx, "studio_student"),
    )
    assert old_record.status_code == 200, old_record.text
    assert old_record.json()["template_version_id"] == v1["id"]

    v2_cells = [_cell("c", "value = 2", 0), _cell("d", "print(value)", 1)]
    assert _save_draft(client, ctx, template["id"], 2, v2_cells).status_code == 200
    v2_response = client.post(
        f"/api/v1/studio/templates/{template['id']}/publish",
        headers=_headers(ctx, "studio_teacher"),
    )
    assert v2_response.status_code == 201, v2_response.text
    v2 = v2_response.json()
    assert v2["version_number"] == 2

    # Content publication and classroom release are separate administrator
    # decisions. New attempts use v2 after release; existing attempts keep v1.
    grant_experiment(db_session_factory, 'lesson', ctx['lesson_id'],
                     ['studio_student', 'studio_student_new'], template_version_id=v2['id'])

    history = client.get(
        f"/api/v1/studio/templates/{template['id']}/versions",
        headers=_headers(ctx, "studio_teacher"),
    )
    assert history.status_code == 200
    assert history.json()[0]["cells"] == v1_cells
    assert history.json()[1]["cells"] == v2_cells

    old_again = client.post(
        f"/api/v1/experiments/records/ensure-for-lesson/{ctx['lesson_id']}",
        headers=_headers(ctx, "studio_student"),
    )
    new_record = client.post(
        f"/api/v1/experiments/records/ensure-for-lesson/{ctx['lesson_id']}",
        headers=_headers(ctx, "studio_student_new"),
    )
    assert old_again.json()["template_version_id"] == v1["id"]
    assert new_record.json()["template_version_id"] == v2["id"]

    with db_session_factory() as db:
        assert db.get(NotebookTemplateVersion, v1["id"]).cells == v1_cells


def test_export_draft_and_version_are_parseable_and_output_free(client, studio_context):
    ctx = studio_context
    template = _create_teacher_template(client, ctx)
    cells = [
        _cell("md", "# Heading", 0, cell_type="markdown", editable=False),
        _cell("code", "print(42)", 1, editable=False, hidden=True),
    ]
    assert _save_draft(client, ctx, template["id"], 1, cells).status_code == 200
    published = client.post(
        f"/api/v1/studio/templates/{template['id']}/publish",
        headers=_headers(ctx, "studio_teacher"),
    ).json()

    for query in ("scope=draft", f"version_id={published['id']}"):
        response = client.get(
            f"/api/v1/studio/templates/{template['id']}/export?{query}",
            headers=_headers(ctx, "studio_teacher"),
        )
        assert response.status_code == 200, response.text
        assert "attachment" in response.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.namelist() == ["notebook.ipynb"]
            notebook = nbformat.reads(
                archive.read("notebook.ipynb").decode("utf-8"),
                as_version=4,
            )
        assert [cell.source for cell in notebook.cells] == ["# Heading", "print(42)"]
        assert notebook.cells[1].metadata["dai"] == {
            "student_editable": False,
            "source_hidden": True,
        }
        assert notebook.cells[1].outputs == []
        assert notebook.cells[1].execution_count is None


class FakeKernelManager:
    def __init__(self):
        self.events = []
        self.alive = set()
        self.initialized = {}
        self.fail_source = None

    def get_or_create_session(self, record_id, image_ref=None,
                              environment_version_id=None, lesson_storage_dir=""):
        self.events.append(("session", record_id))
        self.alive.add(record_id)
        return object()

    def is_template_initialized(self, record_id, version_id):
        return self.initialized.get(record_id) == version_id

    def execute(self, record_id, code):
        self.events.append(("execute", record_id, code))
        if code == self.fail_source:
            raise RuntimeError("boom")
        return {"outputs": [{"msg_type": "stream", "content": {"text": code}}], "execution_time_ms": 1}

    def mark_template_initialized(self, record_id, version_id):
        self.events.append(("mark", record_id, version_id))
        self.initialized[record_id] = version_id

    def destroy(self, record_id):
        self.events.append(("destroy", record_id))
        self.alive.discard(record_id)
        self.initialized.pop(record_id, None)


def test_preview_initializes_hidden_cells_once_and_draft_save_destroys_stale_session(
    client, studio_context, monkeypatch
):
    from app.api import studio as studio_api

    ctx = studio_context
    fake = FakeKernelManager()
    monkeypatch.setattr(studio_api, "get_kernel_manager", lambda: fake)
    template = _create_teacher_template(client, ctx)
    cells = [
        _cell("h2", "hidden_2()", 2, hidden=True, editable=False),
        _cell("visible", "show()", 3),
        _cell("h1", "hidden_1()", 1, hidden=True, editable=False),
    ]
    assert _save_draft(client, ctx, template["id"], 1, cells).status_code == 200

    for _ in range(2):
        response = client.post(
            f"/api/v1/studio/templates/{template['id']}/preview/run",
            headers=_headers(ctx, "studio_teacher"),
            json={"cell_id": "visible"},
        )
        assert response.status_code == 200, response.text

    execute_sources = [event[2] for event in fake.events if event[0] == "execute"]
    assert execute_sources == [
        "hidden_1()",
        "hidden_2()",
        "show()",
        "show()",
    ]
    session_ids = {event[1] for event in fake.events if len(event) > 1}
    assert len(session_ids) == 1
    session_id = session_ids.pop()
    assert session_id < 0

    saved = _save_draft(client, ctx, template["id"], 2, cells)
    assert saved.status_code == 200
    assert ("destroy", session_id) in fake.events
    assert fake.events[-1] == ("destroy", session_id)


def test_preview_hidden_failure_destroys_kernel(client, studio_context, monkeypatch):
    from app.api import studio as studio_api

    ctx = studio_context
    fake = FakeKernelManager()
    fake.fail_source = "explode()"
    monkeypatch.setattr(studio_api, "get_kernel_manager", lambda: fake)
    template = _create_teacher_template(client, ctx)
    cells = [
        _cell("hidden", "explode()", 0, hidden=True, editable=False),
        _cell("visible", "show()", 1),
    ]
    assert _save_draft(client, ctx, template["id"], 1, cells).status_code == 200
    response = client.post(
        f"/api/v1/studio/templates/{template['id']}/preview/run",
        headers=_headers(ctx, "studio_teacher"),
        json={"cell_id": "visible"},
    )
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "KERNEL_INIT_FAILED"
    session_id = next(event[1] for event in fake.events if event[0] == "session")
    assert ("destroy", session_id) in fake.events


def test_legacy_draft_cells_missing_flags_still_preview_run(
    client, db_session_factory, studio_context, monkeypatch
):
    """历史草稿数据缺少 source_hidden/student_editable 字段时，预览执行不应 500"""
    from app.api import studio as studio_api

    ctx = studio_context
    fake = FakeKernelManager()
    monkeypatch.setattr(studio_api, "get_kernel_manager", lambda: fake)
    template = _create_teacher_template(client, ctx)

    # 直接写库模拟旧版 seed 数据：markdown 无任何可选字段，code 缺 source_hidden
    legacy_cells = [
        {"id": "c1", "type": "markdown", "source": "# 标题", "order": 0},
        {"id": "c2", "type": "code", "source": "hidden_init()", "order": 1,
         "student_editable": False},
        {"id": "c3", "type": "code", "source": "show()", "order": 2,
         "student_editable": True},
    ]
    with db_session_factory() as db:
        record = db.get(NotebookTemplate, template["id"])
        record.draft_cells = legacy_cells
        db.commit()

    response = client.post(
        f"/api/v1/studio/templates/{template['id']}/preview/run",
        headers=_headers(ctx, "studio_teacher"),
        json={"cell_id": "c3"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["outputs"][0]["content"]["text"] == "show()"

    # 缺字段的 code cell 不应被误判为隐藏初始化 cell
    assert [e[2] for e in fake.events if e[0] == "execute"] == ["show()"]

    # GET 模板读取时补齐默认字段
    detail = client.get(
        f"/api/v1/studio/templates/{template['id']}",
        headers=_headers(ctx, "studio_teacher"),
    )
    assert detail.status_code == 200, detail.text
    by_id = {c["id"]: c for c in detail.json()["draft_cells"]}
    assert by_id["c3"]["source_hidden"] is False
    assert by_id["c3"]["student_editable"] is True
    assert by_id["c2"]["source_hidden"] is False


def test_preview_run_without_saved_draft_returns_404(
    client, studio_context, monkeypatch
):
    """新建模板尚未保存草稿（draft_cells 为空）时，预览运行应明确返回 404，
    提示先保存草稿，而不是 500 或静默失败。"""
    from app.api import studio as studio_api

    ctx = studio_context
    fake = FakeKernelManager()
    monkeypatch.setattr(studio_api, "get_kernel_manager", lambda: fake)
    template = _create_teacher_template(client, ctx)

    # 不保存任何草稿，直接预览运行前端本地新增的 cell_id
    response = client.post(
        f"/api/v1/studio/templates/{template['id']}/preview/run",
        headers=_headers(ctx, "studio_teacher"),
        json={"cell_id": "cell-abc12345"},
    )
    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "CELL_NOT_FOUND"
    assert "保存" in detail["message"]
    assert fake.events == []


def test_legacy_published_version_missing_flags_still_readable(
    client, db_session_factory, studio_context
):
    """历史发布版本缺字段时，模板读取/版本列表/导出不应 500"""
    ctx = studio_context
    template = _create_teacher_template(client, ctx)

    legacy_cells = [
        {"id": "c1", "type": "markdown", "source": "# 旧标题", "order": 0},
        {"id": "c2", "type": "code", "source": "print(1)", "order": 1},
    ]
    with db_session_factory() as db:
        record = db.get(NotebookTemplate, template["id"])
        record.draft_cells = legacy_cells
        db.commit()

    published = client.post(
        f"/api/v1/studio/templates/{template['id']}/publish",
        headers=_headers(ctx, "studio_teacher"),
    )
    assert published.status_code == 201, published.text

    detail = client.get(
        f"/api/v1/studio/templates/{template['id']}",
        headers=_headers(ctx, "studio_teacher"),
    )
    assert detail.status_code == 200, detail.text
    version_cells = detail.json()["current_version"]["cells"]
    by_id = {c["id"]: c for c in version_cells}
    assert by_id["c2"]["source_hidden"] is False
    assert by_id["c2"]["student_editable"] is True
    assert by_id["c1"]["student_editable"] is False

    history = client.get(
        f"/api/v1/studio/templates/{template['id']}/versions",
        headers=_headers(ctx, "studio_teacher"),
    )
    assert history.status_code == 200, history.text

    exported = client.get(
        f"/api/v1/studio/templates/{template['id']}/export?scope=draft",
        headers=_headers(ctx, "studio_teacher"),
    )
    assert exported.status_code == 200, exported.text


def test_rebind_blocked_when_template_bound_to_published_notebook_lesson(
    client, db_session_factory, studio_context
):
    """换绑不允许把「已发布 Notebook 课时」变成无模板孤儿。"""
    ctx = studio_context
    template = _create_teacher_template(client, ctx)

    with db_session_factory() as db:
        lesson = db.get(Lesson, ctx["lesson_id"])
        lesson.content_type = "notebook"
        lesson.status = "published"
        db.commit()

    response = client.post(
        f"/api/v1/studio/templates/{template['id']}/bind",
        headers=_headers(ctx, "studio_teacher"),
        json={"module_id": ctx["teacher_module_id"]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "REBIND_CONFLICT"
    assert "已发布的 Notebook 课时" in response.json()["detail"]["message"]

    with db_session_factory() as db:
        assert db.get(Lesson, ctx["lesson_id"]).template_id == template["id"]


def test_rebind_allowed_while_host_lesson_is_draft(client, db_session_factory, studio_context):
    """草稿课时可以被换绑（不产生学生可见的破坏）。"""
    ctx = studio_context
    template = _create_teacher_template(client, ctx)

    response = client.post(
        f"/api/v1/studio/templates/{template['id']}/bind",
        headers=_headers(ctx, "studio_teacher"),
        json={"module_id": ctx["teacher_module_id"]},
    )
    assert response.status_code == 200, response.text

    with db_session_factory() as db:
        assert db.get(Lesson, ctx["lesson_id"]).template_id is None
