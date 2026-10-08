"""Task 8: 教师实验评分反馈——权限、评分落库、终态"""
import uuid

from sqlalchemy import select
from app.models import ExperimentRecord, ExperimentSubmission, Lesson
from conftest import auth_header, create_course_db, create_user, login

from teaching_test_support import grant_experiment

API = "/api/v1"


def _setup_submission(client, db_sf):
    """创建已提交的实验记录（带 notebook template）"""
    create_user(db_sf, "erv_t", "teacher")
    create_user(db_sf, "erv_s", "student")
    t_tok, _ = login(client, "erv_t")
    s_tok, _ = login(client, "erv_s")

    cid = create_course_db(db_sf, teacher_username="erv_t", title="RVC", status="published", visibility="public")
    client.post(f"{API}/courses/{cid}/enroll", headers=auth_header(s_tok))
    ch = client.post(f"{API}/courses/{cid}/chapters", headers=auth_header(t_tok),
                     json={"title": "Ch1", "order_index": 1})
    chid = ch.json()["id"]

    with db_sf() as db:
        from app.models import NotebookTemplate, NotebookTemplateVersion, User
        teacher = db.scalar(select(User).where(User.username == "erv_t"))
        tpl = NotebookTemplate(name="RevNB", description="x", status="published",
                               owner_id=teacher.id, draft_cells=[], draft_revision=1)
        db.add(tpl); db.flush()
        ver = NotebookTemplateVersion(
            template_id=tpl.id,
            version_number=1,
            sha256="abc",
            cells=[
                {"id": "m1", "type": "markdown", "source": "# Instructions", "order": 0},
                {"id": "c1", "type": "code", "source": "x", "order": 1},
            ],
            cell_order=["m1", "c1"],
            published_by_id=teacher.id,
        )
        db.add(ver); db.flush()
        tpl.current_version_id = ver.id
        db.commit()
        nbid = tpl.id

    le = client.post(f"{API}/chapters/{chid}/lessons", headers=auth_header(t_tok), json={
        "title": "L1", "content_type": "notebook", "order_index": 1,
    })
    leid = le.json()["id"]
    with db_sf() as db:
        lesson = db.get(Lesson, leid)
        lesson.template_id = nbid
        db.commit()

    grant_experiment(db_sf, "lesson", leid, ["erv_s"])
    rec = client.post(f"{API}/experiments/records/ensure-for-lesson/{leid}",
                      headers=auth_header(s_tok))
    rid = rec.json()["id"]
    with db_sf() as db:
        record = db.get(ExperimentRecord, rid)
        record.cells_sources = {"c1": "print('submitted')"}
        record.cells_outputs = {
            "c1": {
                "execution_count": 1,
                "outputs": [{"output_type": "stream", "text": "submitted\n"}],
            }
        }
        db.commit()
    sub = client.post(f"{API}/experiments/records/{rid}/submit",
                      headers=auth_header(s_tok),
                      json={"client_request_id": str(uuid.uuid4())})
    return {"t_tok": t_tok, "s_tok": s_tok, "rid": rid, "sub_id": sub.json()["id"],
            "cid": cid}


def test_submission_detail_returns_immutable_cell_context(client, db_session_factory):
    """详情返回模板元数据和提交时输出，后续 record 变化不污染历史快照。"""
    ctx = _setup_submission(client, db_session_factory)

    with db_session_factory() as db:
        record = db.get(ExperimentRecord, ctx["rid"])
        record.cells_outputs = {
            "c1": {
                "execution_count": 2,
                "outputs": [{"output_type": "stream", "text": "changed\n"}],
            }
        }
        db.commit()

    r = client.get(
        f"{API}/experiments/submissions/{ctx['sub_id']}",
        headers=auth_header(ctx["t_tok"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cells_snapshot"]["m1"] == "# Instructions"
    assert body["cell_metadata"] == {
        "m1": {"type": "markdown", "order": 0},
        "c1": {"type": "code", "order": 1},
    }
    assert body["outputs_snapshot"]["c1"]["execution_count"] == 1
    assert body["outputs_snapshot"]["c1"]["outputs"][0]["text"] == "submitted\n"
    assert body["student_name"]
    assert body["student_username"] == "erv_s"
    assert body["course_id"] == ctx["cid"]
    assert body["course_name"] == "RVC"
    assert body["entry_name"] == "L1"
    assert body["entry_type"] == "lesson"


def test_submission_list_workspace_filters_summary_and_sort(client, db_session_factory):
    """工作台列表返回全局汇总、筛选项，并正确执行查询与排序。"""
    ctx = _setup_submission(client, db_session_factory)

    second = client.post(
        f"{API}/experiments/records/{ctx['rid']}/submit",
        headers=auth_header(ctx["s_tok"]),
        json={"client_request_id": str(uuid.uuid4())},
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]

    reviewed = client.patch(
        f"{API}/experiments/submissions/{ctx['sub_id']}/review",
        headers=auth_header(ctx["t_tok"]),
        json={"score": 88, "feedback": "已检查"},
    )
    assert reviewed.status_code == 200, reviewed.text

    base = client.get(
        f"{API}/experiments/submissions?page_size=10&sort=submitted_desc",
        headers=auth_header(ctx["t_tok"]),
    )
    assert base.status_code == 200, base.text
    body = base.json()
    assert body["summary"] == {"total": 2, "pending": 1, "graded": 1}
    assert body["items"][0]["id"] == second_id
    assert body["items"][0]["student_username"] == "erv_s"
    assert body["items"][0]["course_name"] == "RVC"
    assert body["items"][0]["entry_name"] == "L1"
    assert body["filter_options"]["courses"] == [{"id": ctx["cid"], "name": "RVC"}]
    entry = body["filter_options"]["entries"][0]

    pending = client.get(
        f"{API}/experiments/submissions?review_status=pending&q=erv_s"
        f"&course_id={ctx['cid']}&entry_id={entry['id']}",
        headers=auth_header(ctx["t_tok"]),
    )
    assert pending.status_code == 200, pending.text
    assert [item["id"] for item in pending.json()["items"]] == [second_id]
    assert pending.json()["summary"] == {"total": 2, "pending": 1, "graded": 1}

    graded = client.get(
        f"{API}/experiments/submissions?review_status=graded&sort=submitted_asc",
        headers=auth_header(ctx["t_tok"]),
    )
    assert graded.status_code == 200, graded.text
    assert [item["id"] for item in graded.json()["items"]] == [ctx["sub_id"]]

    no_match = client.get(
        f"{API}/experiments/submissions?q=not-a-real-student",
        headers=auth_header(ctx["t_tok"]),
    )
    assert no_match.status_code == 200, no_match.text
    assert no_match.json()["items"] == []
    assert no_match.json()["total"] == 0
    assert no_match.json()["summary"]["total"] == 2


# ═══════════════════════════════════════════════════════════════
# 1. 教师评分成功
# ═══════════════════════════════════════════════════════════════

def test_teacher_can_review_submission(client, db_session_factory):
    """教师可对自己的课程提交评分"""
    ctx = _setup_submission(client, db_session_factory)

    r = client.patch(f"{API}/experiments/submissions/{ctx['sub_id']}/review",
                     headers=auth_header(ctx["t_tok"]),
                     json={"score": 85.0, "feedback": "做得不错"})
    assert r.status_code == 200, r.text
    assert r.json()["score"] == 85.0
    assert r.json()["feedback"] == "做得不错"
    assert r.json()["reviewed_by_id"] is not None

    # 验证 record 状态变为 graded
    rec = client.get(f"{API}/experiments/records/{ctx['rid']}",
                     headers=auth_header(ctx["s_tok"]))
    assert rec.json()["status"] == "graded"


# ═══════════════════════════════════════════════════════════════
# 2. 跨教师越权
# ═══════════════════════════════════════════════════════════════

def test_other_teacher_cannot_review(client, db_session_factory):
    """另一教师无法评分他人的课程提交"""
    ctx = _setup_submission(client, db_session_factory)
    create_user(db_session_factory, "erv_t2", "teacher")
    t2_tok, _ = login(client, "erv_t2")

    r = client.patch(f"{API}/experiments/submissions/{ctx['sub_id']}/review",
                     headers=auth_header(t2_tok),
                     json={"score": 90.0})
    assert r.status_code == 404  # Unauthorized records are hidden by scoped queries.


# ═══════════════════════════════════════════════════════════════
# 3. 学生不能评分
# ═══════════════════════════════════════════════════════════════

def test_student_cannot_review(client, db_session_factory):
    """学生不能评分"""
    ctx = _setup_submission(client, db_session_factory)

    r = client.patch(f"{API}/experiments/submissions/{ctx['sub_id']}/review",
                     headers=auth_header(ctx["s_tok"]),
                     json={"score": 100.0})
    assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════
# 4. 评分后不可再次提交
# ═══════════════════════════════════════════════════════════════

def test_cannot_submit_after_graded(client, db_session_factory):
    """评分后 record 状态为 graded，不能再提交"""
    ctx = _setup_submission(client, db_session_factory)

    # 教师评分
    client.patch(f"{API}/experiments/submissions/{ctx['sub_id']}/review",
                 headers=auth_header(ctx["t_tok"]),
                 json={"score": 90.0})

    # 学生尝试再次提交
    r = client.post(f"{API}/experiments/records/{ctx['rid']}/submit",
                    headers=auth_header(ctx["s_tok"]),
                    json={"client_request_id": str(uuid.uuid4())})
    assert r.status_code == 400, f"评分后应拒绝提交: {r.status_code}"


# ═══════════════════════════════════════════════════════════════
# 5. 模块实验提交：详情与评分（回归：曾对模块记录一律 403）
# ═══════════════════════════════════════════════════════════════

def test_teacher_can_view_and_review_module_submission(client, db_session_factory):
    """教师可查看并评分自己实验模块的提交；其他教师 403。

    曾只按 lesson→chapter→course 链路校验教师权限，模块实验记录
    lesson_id 为 NULL，详情接口与评分接口一律 403——教师端打不开
    模块提交详情、无法评分。
    """
    create_user(db_session_factory, "erv_mt", "teacher")
    create_user(db_session_factory, "erv_mt2", "teacher")
    create_user(db_session_factory, "erv_ms", "student")
    t_tok, _ = login(client, "erv_mt")
    t2_tok, _ = login(client, "erv_mt2")
    s_tok, _ = login(client, "erv_ms")

    # 教师创建实验模块（自动生成模板）并发布模板与模块
    mod = client.post(f"{API}/experiments/modules", headers=auth_header(t_tok),
                      json={"name": "模块实验A", "description": "desc"})
    assert mod.status_code == 201, mod.text
    mod_id = mod.json()["id"]
    tpl_id = mod.json()["template_id"]
    pub_tpl = client.post(f"{API}/studio/templates/{tpl_id}/publish", headers=auth_header(t_tok))
    assert pub_tpl.status_code == 201, pub_tpl.text
    pub_mod = client.post(f"{API}/experiments/modules/{mod_id}/publish", headers=auth_header(t_tok))
    assert pub_mod.status_code == 200, pub_mod.text

    grant_experiment(db_session_factory, "module", mod_id, ["erv_ms"])
    # 学生创建模块实验记录并提交
    rec = client.post(f"{API}/experiments/records/ensure-for-module/{mod_id}",
                      headers=auth_header(s_tok))
    assert rec.status_code == 200, rec.text
    rid = rec.json()["id"]
    sub = client.post(f"{API}/experiments/records/{rid}/submit",
                      headers=auth_header(s_tok),
                      json={"client_request_id": str(uuid.uuid4())})
    assert sub.status_code == 201, sub.text
    sub_id = sub.json()["id"]

    # 教师查看详情：200 且 entry_type 为 module
    detail = client.get(f"{API}/experiments/submissions/{sub_id}", headers=auth_header(t_tok))
    assert detail.status_code == 200, detail.text
    assert detail.json()["entry_type"] == "module"
    assert detail.json()["entry_name"] == "模块实验A"

    # 历史数据 outputs_snapshot 为 NULL：详情接口必须归一化为 {} 而非 500
    with db_session_factory() as db:
        submission = db.get(ExperimentSubmission, sub_id)
        submission.outputs_snapshot = None
        db.commit()
    detail_null = client.get(f"{API}/experiments/submissions/{sub_id}", headers=auth_header(t_tok))
    assert detail_null.status_code == 200, detail_null.text
    assert detail_null.json()["outputs_snapshot"] == {}

    # 历史种子数据 cells_snapshot 嵌套为 {"cells": {...}}：详情接口必须解包为扁平结构
    with db_session_factory() as db:
        submission = db.get(ExperimentSubmission, sub_id)
        submission.cells_snapshot = {"cells": {"c1": "print('nested')"}}
        db.commit()
    detail_nested = client.get(f"{API}/experiments/submissions/{sub_id}", headers=auth_header(t_tok))
    assert detail_nested.status_code == 200, detail_nested.text
    assert detail_nested.json()["cells_snapshot"] == {"c1": "print('nested')"}

    # 教师评分：200 且落库
    review = client.patch(f"{API}/experiments/submissions/{sub_id}/review",
                          headers=auth_header(t_tok),
                          json={"score": 85.0, "feedback": "模块实验不错"})
    assert review.status_code == 200, review.text
    assert review.json()["score"] == 85.0

    # 其他教师查看详情与评分均 403
    other_detail = client.get(f"{API}/experiments/submissions/{sub_id}", headers=auth_header(t2_tok))
    assert other_detail.status_code == 404
    other_review = client.patch(f"{API}/experiments/submissions/{sub_id}/review",
                                headers=auth_header(t2_tok),
                                json={"score": 90.0})
    assert other_review.status_code == 404
