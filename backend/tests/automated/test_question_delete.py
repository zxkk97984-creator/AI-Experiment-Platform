"""TASK-017：题目删除端点 DELETE /assignments/{id}/questions/{qid}。

- 草稿作业无提交：教师/admin 可删，级联清理提交/Rubric/CodeGrade。
- 已发布或已有提交：409（与评分事实不可变守卫一致）。
- 权限：非所有者 403；题目不属于作业 404；题目不存在 404。
"""

from conftest import auth_header, create_course_db, create_user, login
from sqlalchemy import select

from app.models import JudgeQuestion, QuestionRubric

API = "/api/v1"


def _setup(client, db_session_factory, *, with_submission=False, tag=""):
    t_name, s_name = f"del_t{tag}", f"del_s{tag}"
    create_user(db_session_factory, t_name, "teacher")
    create_user(db_session_factory, s_name, "student")
    t_tok, _ = login(client, t_name)
    s_tok, _ = login(client, s_name)
    cid = create_course_db(
        db_session_factory, teacher_username=t_name, title="删除测试课",
        status="published", visibility="public",
    )
    client.post(f"{API}/courses/{cid}/enroll", headers=auth_header(s_tok))
    assignment = client.post(
        f"{API}/assignments",
        headers=auth_header(t_tok),
        json={"course_id": cid, "title": "删除题目作业", "status": "draft"},
    ).json()
    aid = assignment["id"]
    question = client.post(
        f"{API}/assignments/{aid}/questions",
        headers=auth_header(t_tok),
        json={
            "title": "两数相加",
            "function_name": "add",
            "hidden_tests": "def test():\n    pass\n",
            "grading_mode": "legacy",
        },
    ).json()
    qid = question["id"]

    from teaching_test_support import grant_course_students
    grant_course_students(db_session_factory, cid, [s_name])

    submission_id = None
    if with_submission:
        assert (
            client.post(f"{API}/assignments/{aid}/publish", headers=auth_header(t_tok)).status_code
            == 200
        )
        sub = client.post(
            f"{API}/judge/submissions",
            headers=auth_header(s_tok),
            json={"question_id": qid, "code": "def add(a, b):\n    return a + b\n"},
        )
        assert sub.status_code == 201, sub.text
        submission_id = sub.json()["id"]
        assert (
            client.post(f"{API}/assignments/{aid}/unpublish", headers=auth_header(t_tok)).status_code
            == 200
        )

    return {
        "t_name": t_name,
        "t_tok": t_tok,
        "s_tok": s_tok,
        "s_name": s_name,
        "aid": aid,
        "qid": qid,
        "submission_id": submission_id,
    }


def test_delete_question_cascades(client, db_session_factory):
    d = _setup(client, db_session_factory)
    # 直插 rubric（版本化事实）：删除题目时 Rubric 必须先于/随题目清理，
    # 否则外键 RESTRICT 会失败；无提交场景下 CodeGrade 提交侧清理分支天然为空。
    with db_session_factory() as session:
        rubric = QuestionRubric(
            judge_question_id=d["qid"],
            version=1,
            status="draft",
            source_hash="a" * 64,
            source_snapshot={},
            rubric_json={},
            model_name="smoke",
        )
        session.add(rubric)
        session.commit()
        rubric_id = rubric.id

    resp = client.delete(
        f"{API}/assignments/{d['aid']}/questions/{d['qid']}",
        headers=auth_header(d["t_tok"]),
    )
    assert resp.status_code == 204, resp.text
    with db_session_factory() as session:
        assert session.get(JudgeQuestion, d["qid"]) is None
        assert (
            session.scalar(
                select(QuestionRubric.id).where(QuestionRubric.judge_question_id == d["qid"])
            )
            is None
        )
        assert session.get(QuestionRubric, rubric_id) is None
    # 列表为空
    listing = client.get(
        f"{API}/assignments/{d['aid']}/questions", headers=auth_header(d["t_tok"])
    ).json()
    assert listing["total"] == 0


def test_delete_question_with_submissions_cascades(client, db_session_factory):
    d = _setup(client, db_session_factory, with_submission=True)
    # 有提交时守卫 409：不可删除
    resp = client.delete(
        f"{API}/assignments/{d['aid']}/questions/{d['qid']}",
        headers=auth_header(d["t_tok"]),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ASSIGNMENT_HAS_SUBMISSIONS"


def test_delete_question_blocked_when_published(client, db_session_factory):
    d = _setup(client, db_session_factory)
    assert (
        client.post(f"{API}/assignments/{d['aid']}/publish", headers=auth_header(d["t_tok"])).status_code
        == 200
    )
    resp = client.delete(
        f"{API}/assignments/{d['aid']}/questions/{d['qid']}",
        headers=auth_header(d["t_tok"]),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ASSIGNMENT_NOT_EDITABLE"


def test_delete_question_permissions(client, db_session_factory):
    d = _setup(client, db_session_factory)
    # 学生 403
    assert (
        client.delete(
            f"{API}/assignments/{d['aid']}/questions/{d['qid']}",
            headers=auth_header(d["s_tok"]),
        ).status_code
        == 403
    )
    # Other teachers cannot enumerate tasks outside their teaching scope.
    other_name = f"del_other_{d['t_name']}"
    create_user(db_session_factory, other_name, "teacher")
    other_tok, _ = login(client, other_name)
    assert (
        client.delete(
            f"{API}/assignments/{d['aid']}/questions/{d['qid']}",
            headers=auth_header(other_tok),
        ).status_code
        == 404
    )
    with db_session_factory() as db:
        assert db.get(JudgeQuestion, d['qid']) is not None
    # 404：题目不存在
    assert (
        client.delete(
            f"{API}/assignments/{d['aid']}/questions/999999",
            headers=auth_header(d["t_tok"]),
        ).status_code
        == 404
    )
    # 404：题目属于其他作业
    other_q = client.post(
        f"{API}/assignments/{d['aid']}/questions",
        headers=auth_header(d["t_tok"]),
        json={"title": "别题", "function_name": "f", "hidden_tests": "pass", "grading_mode": "legacy"},
    ).json()
    d2 = _setup(client, db_session_factory, tag="2")
    assert (
        client.delete(
            f"{API}/assignments/{d2['aid']}/questions/{other_q['id']}",
            headers=auth_header(d2["t_tok"]),
        ).status_code
        == 404
    )


def test_delete_question_by_admin(client, db_session_factory):
    d = _setup(client, db_session_factory)
    create_user(db_session_factory, f"del_admin_{d['t_name']}", "admin")
    admin_tok, _ = login(client, f"del_admin_{d['t_name']}")
    resp = client.delete(
        f"{API}/assignments/{d['aid']}/questions/{d['qid']}",
        headers=auth_header(admin_tok),
    )
    assert resp.status_code == 204, resp.text
