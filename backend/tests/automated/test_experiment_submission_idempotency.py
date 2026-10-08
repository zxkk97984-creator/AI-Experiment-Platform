"""Task 6: 实验提交原子化与幂等——client_request_id、深复制、并发"""
import uuid

import pytest

from sqlalchemy import select
from app.models import ExperimentRecord, ExperimentSubmission
from conftest import auth_header, create_course_db, create_user, login

from teaching_test_support import grant_experiment

API = "/api/v1"


def _setup_experiment(client, db_sf):
    """创建一个关联了 notebook template 的 lesson 实验记录"""
    create_user(db_sf, "esub_t", "teacher")
    create_user(db_sf, "esub_s", "student")
    t_tok, _ = login(client, "esub_t")
    s_tok, _ = login(client, "esub_s")

    cid = create_course_db(db_sf, teacher_username="esub_t", title="ESubC", status="published", visibility="public")
    client.post(f"{API}/courses/{cid}/enroll", headers=auth_header(s_tok))

    ch = client.post(f"{API}/courses/{cid}/chapters", headers=auth_header(t_tok),
                     json={"title": "Ch1", "order_index": 1})
    chid = ch.json()["id"]

    # 通过 DB 创建 template + version（避免 studio API 的绑定限制）
    with db_sf() as db:
        from app.models import NotebookTemplate, NotebookTemplateVersion, User, Lesson
        teacher = db.get(User, db.scalar(select(User.id).where(User.username == "esub_t")))
        tpl = NotebookTemplate(name="Test NB", description="test",
                               status="published", owner_id=teacher.id,
                               draft_cells=[], draft_revision=1)
        db.add(tpl)
        db.flush()
        ver = NotebookTemplateVersion(
            template_id=tpl.id, version_number=1, sha256="abc",
            cells=[{"id": "c1", "type": "code", "source": "print(1)", "order": 0,
                    "student_editable": True, "source_hidden": False}],
            cell_order=["c1"], published_by_id=teacher.id,
        )
        db.add(ver)
        db.flush()
        tpl.current_version_id = ver.id
        db.commit()
        nbid = tpl.id

    le = client.post(f"{API}/chapters/{chid}/lessons", headers=auth_header(t_tok), json={
        "title": "Lesson1", "content_type": "notebook", "order_index": 1,
    })
    leid = le.json()["id"]

    # 绑定 template 到 lesson
    with db_sf() as db:
        from app.models import Lesson
        lesson = db.get(Lesson, leid)
        lesson.template_id = nbid
        db.commit()

    # 学生创建实验记录
    grant_experiment(db_sf, "lesson", leid, ["esub_s"])
    rec = client.post(f"{API}/experiments/records/ensure-for-lesson/{leid}",
                      headers=auth_header(s_tok))
    assert rec.status_code in (200, 201), f"创建记录失败: {rec.text}"
    rid = rec.json()["id"]

    return {"t_tok": t_tok, "s_tok": s_tok, "rid": rid, "leid": leid, "nbid": nbid}


# ═══════════════════════════════════════════════════════════════
# 1. 幂等：同一 client_request_id 重复提交返回已有记录
# ═══════════════════════════════════════════════════════════════

def test_same_client_request_id_returns_existing_submission(client, db_session_factory):
    """同一 client_request_id 的第二次请求返回第一次的 submission"""
    ctx = _setup_experiment(client, db_session_factory)
    req_id = str(uuid.uuid4())

    # 第一次提交
    r1 = client.post(f"{API}/experiments/records/{ctx['rid']}/submit",
                     headers=auth_header(ctx["s_tok"]),
                     json={"client_request_id": req_id})
    assert r1.status_code == 201, r1.text
    sub1 = r1.json()
    assert sub1["attempt_number"] == 1

    # 第二次提交（相同 client_request_id）→ 幂等返回同一条
    r2 = client.post(f"{API}/experiments/records/{ctx['rid']}/submit",
                     headers=auth_header(ctx["s_tok"]),
                     json={"client_request_id": req_id})
    assert r2.status_code == 201, r2.text
    sub2 = r2.json()
    assert sub2["id"] == sub1["id"], "应返回同一提交"
    assert sub2["attempt_number"] == 1


# ═══════════════════════════════════════════════════════════════
# 2. 不同 client_request_id 产生不同 attempt
# ═══════════════════════════════════════════════════════════════

def test_different_client_request_id_creates_new_attempt(client, db_session_factory):
    """不同 client_request_id 产生递增的 attempt_number"""
    ctx = _setup_experiment(client, db_session_factory)

    r1 = client.post(f"{API}/experiments/records/{ctx['rid']}/submit",
                     headers=auth_header(ctx["s_tok"]),
                     json={"client_request_id": str(uuid.uuid4())})
    assert r1.status_code == 201
    assert r1.json()["attempt_number"] == 1

    r2 = client.post(f"{API}/experiments/records/{ctx['rid']}/submit",
                     headers=auth_header(ctx["s_tok"]),
                     json={"client_request_id": str(uuid.uuid4())})
    assert r2.status_code == 201
    assert r2.json()["attempt_number"] == 2
    assert r2.json()["id"] != r1.json()["id"]


# ═══════════════════════════════════════════════════════════════
# 3. 快照不可变性
# ═══════════════════════════════════════════════════════════════

def test_snapshot_immutable_after_submit(client, db_session_factory):
    """提交后修改 cells_sources 不影响已有快照"""
    ctx = _setup_experiment(client, db_session_factory)
    req_id = str(uuid.uuid4())

    # 先保存一些 cells
    client.put(f"{API}/experiments/records/{ctx['rid']}/cells",
               headers=auth_header(ctx["s_tok"]),
               json={"cells": {"c1": "original code"}, "record_revision": 1})

    # 提交
    r = client.post(f"{API}/experiments/records/{ctx['rid']}/submit",
                    headers=auth_header(ctx["s_tok"]),
                    json={"client_request_id": req_id})
    assert r.status_code == 201, r.text
    sub_id = r.json()["id"]

    # 修改 cells_sources
    client.put(f"{API}/experiments/records/{ctx['rid']}/cells",
               headers=auth_header(ctx["s_tok"]),
               json={"cells": {"c1": "modified code"}, "record_revision": 2})

    # 验证快照不变
    r2 = client.get(f"{API}/experiments/submissions/{sub_id}",
                    headers=auth_header(ctx["s_tok"]))
    snapshot = r2.json()["cells_snapshot"]
    assert snapshot.get("c1") == "original code", f"快照应不变: {snapshot}"


# ═══════════════════════════════════════════════════════════════
# 4. 跨用户越权
# ═══════════════════════════════════════════════════════════════

def test_another_student_cannot_submit(client, db_session_factory):
    """另一学生无法提交他人的实验记录"""
    ctx = _setup_experiment(client, db_session_factory)

    # 创建另一个学生
    create_user(db_session_factory, "esub_s2", "student")
    s2_tok, _ = login(client, "esub_s2")

    r = client.post(f"{API}/experiments/records/{ctx['rid']}/submit",
                    headers=auth_header(s2_tok),
                    json={"client_request_id": str(uuid.uuid4())})
    assert r.status_code == 404, f"越权记录应隐藏: {r.status_code}"


def test_teacher_cannot_submit(client, db_session_factory):
    """教师不能以学生身份提交"""
    ctx = _setup_experiment(client, db_session_factory)

    r = client.post(f"{API}/experiments/records/{ctx['rid']}/submit",
                    headers=auth_header(ctx["t_tok"]),
                    json={"client_request_id": str(uuid.uuid4())})
    assert r.status_code == 403, f"教师不能提交实验: {r.status_code}"


# ═══════════════════════════════════════════════════════════════
# P1-2: 幂等检查不绕过所有权验证
# ═══════════════════════════════════════════════════════════════

def test_p1_2_idempotency_requires_ownership(client, db_session_factory):
    """P1-2: 其他学生即使知道 record_id + client_request_id 也不能获取提交"""
    ctx = _setup_experiment(client, db_session_factory)

    # 学生 A 先提交
    req_id = str(uuid.uuid4())
    r1 = client.post(f"{API}/experiments/records/{ctx['rid']}/submit",
                     headers=auth_header(ctx["s_tok"]),
                     json={"client_request_id": req_id})
    assert r1.status_code == 201, r1.text
    sub_id = r1.json()["id"]

    # 创建学生 B
    create_user(db_session_factory, "esub_s3", "student")
    s3_tok, _ = login(client, "esub_s3")

    # 学生 B 使用学生 A 的 record_id + 相同的 client_request_id → 必须 403
    r2 = client.post(f"{API}/experiments/records/{ctx['rid']}/submit",
                     headers=auth_header(s3_tok),
                     json={"client_request_id": req_id})
    assert r2.status_code == 404, \
        f"P1-2: 越权记录应隐藏，实际: {r2.status_code}"


# ═══════════════════════════════════════════════════════════════
# 5. client_request_id 必填
# ═══════════════════════════════════════════════════════════════

def test_submit_requires_client_request_id(client, db_session_factory):
    """提交时 client_request_id 为必填"""
    ctx = _setup_experiment(client, db_session_factory)

    r = client.post(f"{API}/experiments/records/{ctx['rid']}/submit",
                    headers=auth_header(ctx["s_tok"]),
                    json={})
    assert r.status_code == 422, f"缺少 client_request_id 应 422: {r.status_code}"


# ═══════════════════════════════════════════════════════════════
# P0-2: 并发提交不同 client_request_id 产生不同 attempt_number
# ═══════════════════════════════════════════════════════════════

def test_p0_2_concurrent_different_client_request_id_distinct_attempts(client, db_session_factory):
    """P0-2: 两个不同 client_request_id 并发提交 → 不同的 attempt_number，不报 500"""
    import threading

    ctx = _setup_experiment(client, db_session_factory)
    rid = ctx["rid"]
    s_tok = ctx["s_tok"]

    results = []
    barrier = threading.Barrier(2, timeout=5)
    errors = []

    def do_submit(client_request_id):
        try:
            barrier.wait()  # 同步起点——确保两个线程同时到达提交点
            r = client.post(
                f"{API}/experiments/records/{rid}/submit",
                headers=auth_header(s_tok),
                json={"client_request_id": client_request_id},
            )
            results.append((client_request_id, r.status_code, r.json() if r.status_code == 201 else r.text))
        except Exception as e:
            errors.append(str(e))

    req_a = str(uuid.uuid4())
    req_b = str(uuid.uuid4())
    assert req_a != req_b

    t1 = threading.Thread(target=do_submit, args=(req_a,))
    t2 = threading.Thread(target=do_submit, args=(req_b,))

    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert len(errors) == 0, f"线程异常: {errors}"
    assert len(results) == 2, f"应有 2 个结果: {results}"

    # 两个请求都应成功
    statuses = [s for _, s, _ in results]
    assert all(s == 201 for s in statuses), f"两个请求都应返回 201: {results}"

    # attempt_number 应不同（1 和 2，顺序不固定）
    attempts = sorted([r["attempt_number"] for _, _, r in results])
    assert attempts == [1, 2], f"attempt_number 应为 1 和 2: {results}"

    # id 应不同
    ids = [r["id"] for _, _, r in results]
    assert ids[0] != ids[1], f"两个提交应有不同 id: {ids}"
