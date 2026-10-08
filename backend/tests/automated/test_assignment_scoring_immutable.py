"""TASK-009：作业评分事实不可变。

- published 状态：题目新增/修改、环境修改、AI 配置修改全部 409。
- 取消发布但已有提交：同样 409（评分输入与规则不可原地漂移）。
- 标题/描述/截止时间等非评分元数据始终可调。
"""

from conftest import auth_header, create_course_db, create_user, login

from teaching_test_support import grant_course_students
API = "/api/v1"


def _setup(client, db_session_factory, *, with_submission=False, tag=""):
    t_name, s_name = f"imm_t{tag}", f"imm_s{tag}"
    create_user(db_session_factory, t_name, "teacher")
    create_user(db_session_factory, s_name, "student")
    t_tok, _ = login(client, t_name)
    s_tok, _ = login(client, s_name)
    cid = create_course_db(
        db_session_factory, teacher_username=t_name, title="不可变测试课",
        status="published", visibility="public",
    )
    client.post(f"{API}/courses/{cid}/enroll", headers=auth_header(s_tok))

    assignment = client.post(
        f"{API}/assignments",
        headers=auth_header(t_tok),
        json={"course_id": cid, "title": "评分事实作业", "status": "draft"},
    ).json()
    aid = assignment["id"]
    grant_course_students(db_session_factory, cid, [s_name])
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
    assert (
        client.post(
            f"{API}/assignments/{aid}/publish", headers=auth_header(t_tok)
        ).status_code
        == 200
    )

    submission_id = None
    if with_submission:
        sub = client.post(
            f"{API}/judge/submissions",
            headers=auth_header(s_tok),
            json={"question_id": qid, "code": "def add(a, b):\n    return a + b\n"},
        )
        assert sub.status_code == 201, sub.text
        submission_id = sub.json()["id"]

    return {"t_tok": t_tok, "aid": aid, "qid": qid, "submission_id": submission_id}


def _add_question(client, token, aid):
    return client.post(
        f"{API}/assignments/{aid}/questions",
        headers=auth_header(token),
        json={"title": "新题", "function_name": "f", "hidden_tests": "pass", "grading_mode": "legacy"},
    )


def _patch_question(client, token, aid, qid):
    return client.patch(
        f"{API}/assignments/{aid}/questions/{qid}",
        headers=auth_header(token),
        json={"hidden_tests": "def test():\n    assert False\n"},
    )


def _patch_env(client, token, aid):
    return client.patch(
        f"{API}/assignments/{aid}",
        headers=auth_header(token),
        json={"import_policy_mode": "restricted", "allowed_imports": ["os"]},
    )


def _patch_metadata(client, token, aid):
    return client.patch(
        f"{API}/assignments/{aid}",
        headers=auth_header(token),
        json={"title": "改名后的作业", "due_at": "2099-01-01T00:00:00Z"},
    )


def test_published_blocks_scoring_mutations(client, db_session_factory):
    d = _setup(client, db_session_factory)
    assert _add_question(client, d["t_tok"], d["aid"]).status_code == 409
    assert _patch_question(client, d["t_tok"], d["aid"], d["qid"]).status_code == 409
    assert _patch_env(client, d["t_tok"], d["aid"]).status_code == 409
    # 非评分元数据仍可调
    resp = _patch_metadata(client, d["t_tok"], d["aid"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "改名后的作业"


def test_unpublished_without_submissions_allows_edits(client, db_session_factory):
    d = _setup(client, db_session_factory)
    unpub = client.post(f"{API}/assignments/{d['aid']}/unpublish", headers=auth_header(d["t_tok"]))
    assert unpub.status_code == 200, unpub.text
    # 无提交：题目与评分配置可改
    assert _add_question(client, d["t_tok"], d["aid"]).status_code == 201
    assert _patch_question(client, d["t_tok"], d["aid"], d["qid"]).status_code == 200
    assert _patch_env(client, d["t_tok"], d["aid"]).status_code == 200


def test_unpublished_with_submissions_still_blocks_scoring_mutations(client, db_session_factory):
    d = _setup(client, db_session_factory, with_submission=True)
    unpub = client.post(f"{API}/assignments/{d['aid']}/unpublish", headers=auth_header(d["t_tok"]))
    assert unpub.status_code == 200, unpub.text

    resp = _add_question(client, d["t_tok"], d["aid"])
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ASSIGNMENT_HAS_SUBMISSIONS"
    assert _patch_question(client, d["t_tok"], d["aid"], d["qid"]).status_code == 409
    assert _patch_env(client, d["t_tok"], d["aid"]).status_code == 409
    # 非评分元数据仍可调
    assert _patch_metadata(client, d["t_tok"], d["aid"]).status_code == 200


def test_ai_config_blocked_when_published_or_with_submissions(client, db_session_factory):
    for index, with_submission in enumerate((False, True)):
        d = _setup(client, db_session_factory, with_submission=with_submission, tag=str(index))
        if with_submission:
            unpub = client.post(
                f"{API}/assignments/{d['aid']}/unpublish", headers=auth_header(d["t_tok"])
            )
            assert unpub.status_code == 200
        resp = client.put(
            f"{API}/ai-grading/questions/assignment/{d['qid']}/config",
            headers=auth_header(d["t_tok"]),
            json={"grading_mode": "legacy", "teacher_constraints": {}, "reference_solution": "x"},
        )
        assert resp.status_code == 409, f"with_submission={with_submission}: {resp.text}"


def test_ai_config_allowed_when_draft_without_submissions(client, db_session_factory):
    d = _setup(client, db_session_factory)
    client.post(f"{API}/assignments/{d['aid']}/unpublish", headers=auth_header(d["t_tok"]))
    resp = client.put(
        f"{API}/ai-grading/questions/assignment/{d['qid']}/config",
        headers=auth_header(d["t_tok"]),
        json={"grading_mode": "legacy", "teacher_constraints": {}, "reference_solution": "x"},
    )
    assert resp.status_code == 200, resp.text
