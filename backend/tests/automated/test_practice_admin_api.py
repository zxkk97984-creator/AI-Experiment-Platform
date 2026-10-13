"""批次 5：管理员题库接口权限与完整录题流程。

必过验收：
* 教师打**全部**管理员端点逐个 403（不是 404）——读端点同样拒绝；
* 管理员完整录题流程：新建 → 配 AI（见 test_practice_ai_setup.py）→ 发布 → 学生可见，
  归档后学生不可见但历史提交仍可查；
* `copy-from-assignment` 集合级入口一步生成草稿题。
"""
from __future__ import annotations

import pytest

from conftest import auth_header, login
from practice_test_support import (
    as_admin, as_student, as_teacher, create_and_publish, problem_payload,
)

pytestmark = pytest.mark.usefixtures("db_session_factory")

ADMIN_WRITE_ENDPOINTS = [
    ("POST", "/api/v1/admin/practice/problems",
     {"title": "教师不该能建", "hidden_tests": [{"id": "c1", "args": [1], "expected": 1}]}),
    ("PATCH", "/api/v1/admin/practice/problems/{problem_id}", {"title": "改标题"}),
    ("DELETE", "/api/v1/admin/practice/problems/{problem_id}", None),
    ("POST", "/api/v1/admin/practice/problems/{problem_id}/publish", None),
    ("POST", "/api/v1/admin/practice/problems/{problem_id}/archive", None),
    ("POST", "/api/v1/admin/practice/problems/copy-from-assignment",
     {"assignment_id": 1, "question_id": 1}),
    ("PATCH", "/api/v1/admin/practice/settings", {"grading_mode": "ai"}),
    ("POST", "/api/v1/admin/practice/appeals/{appeal_id}/handle",
     {"action": "uphold", "comment": "教师不该能处理练习异议"}),
]

ADMIN_READ_ENDPOINTS = [
    ("GET", "/api/v1/admin/practice/problems", None),
    ("GET", "/api/v1/admin/practice/problems/{problem_id}", None),
    ("GET", "/api/v1/admin/practice/settings", None),
    ("GET", "/api/v1/admin/practice/ai-preflight", None),
    ("GET", "/api/v1/admin/practice/usage", None),
    ("GET", "/api/v1/admin/practice/appeals", None),
    ("GET", "/api/v1/admin/practice/submissions/{submission_id}", None),
]


@pytest.mark.parametrize("method,path,body", ADMIN_WRITE_ENDPOINTS)
def test_teacher_is_forbidden_on_every_admin_write_endpoint(
        client, db_session_factory, method, path, body):
    """教师写接口必须 403（不是 404）：逐个端点验证，杜绝"看起来不存在"的掩蔽。"""
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header)
    _, teacher_header = as_teacher(client, db_session_factory)
    url = path.format(problem_id=problem["id"], appeal_id=1)
    response = client.request(method, url, json=body, headers=teacher_header)
    assert response.status_code == 403, f"{method} {url} -> {response.status_code} {response.text}"
    assert response.json()["detail"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("method,path,body", ADMIN_READ_ENDPOINTS)
def test_teacher_is_forbidden_on_admin_read_endpoints(
        client, db_session_factory, method, path, body):
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header)
    _, teacher_header = as_teacher(client, db_session_factory)
    url = path.format(problem_id=problem["id"], submission_id=1)
    response = client.request(method, url, json=body, headers=teacher_header)
    assert response.status_code == 403, f"{method} {url} -> {response.status_code}"


def test_admin_single_problem_detail_matches_list_item(client, db_session_factory):
    """编辑页深链接用的单题详情：形状与列表项一致；不存在 404（教师 403 见上）。"""
    _, admin_header = as_admin(client, db_session_factory)
    problem, published = create_and_publish(client, admin_header)

    listed = client.get("/api/v1/admin/practice/problems", headers=admin_header).json()
    list_item = next(item for item in listed["items"] if item["id"] == problem["id"])
    detail = client.get(f"/api/v1/admin/practice/problems/{problem['id']}",
                        headers=admin_header)
    assert detail.status_code == 200, detail.text
    assert detail.json() == list_item
    assert detail.json()["hidden_tests"] and detail.json()["reference_solution"]
    assert detail.json()["grading_version_id"] == published["grading_version_id"]

    missing = client.get("/api/v1/admin/practice/problems/999999", headers=admin_header)
    assert missing.status_code == 404


def test_student_cannot_write_practice_bank(client, db_session_factory):
    """对照组：学生同样 403（题库只对管理员开放），管理员 201 成功。"""
    _, student_header = as_student(client, db_session_factory)
    response = client.post("/api/v1/admin/practice/problems",
                           json=problem_payload(), headers=student_header)
    assert response.status_code == 403
    _, admin_header = as_admin(client, db_session_factory)
    ok = client.post("/api/v1/admin/practice/problems",
                     json=problem_payload(), headers=admin_header)
    assert ok.status_code == 201


def test_admin_full_publish_flow_and_archive_history(client, db_session_factory):
    """新建 → 发布 → 学生可见；归档后不可见，但历史提交仍可查。"""
    _, admin_header = as_admin(client, db_session_factory)
    problem, published = create_and_publish(client, admin_header)
    assert published["status"] == "published"
    assert published["grading_version_id"], "发布必须生成并绑定评分版本"
    assert published["current_grading_version_id"] == published["grading_version_id"]

    _, student_header = as_student(client, db_session_factory)
    listed = client.get("/api/v1/practice/problems", headers=student_header)
    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()["items"]]
    assert problem["id"] in ids

    detail = client.get(f"/api/v1/practice/problems/{problem['id']}", headers=student_header)
    assert detail.status_code == 200
    body = detail.json()
    # 学生视角绝不泄漏隐藏用例与参考解法
    assert "hidden_tests" not in body and "reference_solution" not in body
    assert body["my_status"] == "not_started"
    assert body["latest_submission_id"] is None

    # 学生提交后归档：列表与详情不可见，历史提交仍可读
    submitted = client.post(
        f"/api/v1/practice/problems/{problem['id']}/submissions",
        json={"code": "def solution(a, b):\n    return a + b\n",
              "client_request_id": "archive-flow-1"},
        headers=student_header)
    assert submitted.status_code == 202, submitted.text
    submission_id = submitted.json()["id"]

    archived = client.post(f"/api/v1/admin/practice/problems/{problem['id']}/archive",
                           headers=admin_header)
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"

    after = client.get("/api/v1/practice/problems", headers=student_header)
    assert problem["id"] not in [item["id"] for item in after.json()["items"]]
    hidden = client.get(f"/api/v1/practice/problems/{problem['id']}", headers=student_header)
    assert hidden.status_code == 404
    history = client.get(f"/api/v1/practice/submissions/{submission_id}",
                         headers=student_header)
    assert history.status_code == 200, "归档不得删除历史提交"
    assert history.json()["id"] == submission_id


def test_publish_requires_hidden_cases(client, db_session_factory):
    """没有隐藏用例的题目不能发布（fail-closed），对照组：补上用例后发布成功。"""
    _, admin_header = as_admin(client, db_session_factory)
    created = client.post("/api/v1/admin/practice/problems",
                          json=problem_payload(hidden_tests=[]), headers=admin_header)
    assert created.status_code == 201
    problem_id = created.json()["id"]
    rejected = client.post(f"/api/v1/admin/practice/problems/{problem_id}/publish",
                           headers=admin_header)
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "PRACTICE_CASES_REQUIRED"

    patched = client.patch(f"/api/v1/admin/practice/problems/{problem_id}",
                           json={"hidden_tests": [{"id": "c1", "args": [2, 3], "expected": 5}]},
                           headers=admin_header)
    assert patched.status_code == 200, patched.text
    ok = client.post(f"/api/v1/admin/practice/problems/{problem_id}/publish",
                     headers=admin_header)
    assert ok.status_code == 200
    assert ok.json()["status"] == "published"


def test_delete_only_drafts_without_submissions(client, db_session_factory):
    """DELETE 只允许草稿且无提交；对照组：草稿可删、发布后 409。"""
    _, admin_header = as_admin(client, db_session_factory)
    draft = client.post("/api/v1/admin/practice/problems", json=problem_payload(),
                        headers=admin_header).json()
    assert client.delete(f"/api/v1/admin/practice/problems/{draft['id']}",
                         headers=admin_header).status_code == 200

    problem, _ = create_and_publish(client, admin_header)
    blocked = client.delete(f"/api/v1/admin/practice/problems/{problem['id']}",
                            headers=admin_header)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "PRACTICE_PROBLEM_IN_USE"


def test_copy_from_assignment_creates_draft(client, db_session_factory):
    """从已有作业复制：集合级入口 201，source_kind=from_assignment 且带来源题 id。"""
    from conftest import make_assignment, make_judge_question

    _, admin_header = as_admin(client, db_session_factory)
    assignment_id = make_assignment(db_session_factory)
    question_id = make_judge_question(
        db_session_factory, assignment_id=assignment_id, title="作业原题",
        function_name="solve_it",
        public_cases=[{"args": [1, 2], "expected": 3}],
    )
    response = client.post("/api/v1/admin/practice/problems/copy-from-assignment",
                           json={"assignment_id": assignment_id, "question_id": question_id,
                                 "title": "复制自作业"},
                           headers=admin_header)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_kind"] == "from_assignment"
    assert body["source_judge_question_id"] == question_id
    assert body["status"] == "draft"
    assert body["function_name"] == "solve_it"
    assert body["title"] == "复制自作业"
    assert len(body["public_cases"]) == 1
    # 作业题的 hidden_tests 是 pytest 源码，不能当练习用例，必须为空（由管理员补齐）
    assert body["hidden_tests"] == []

    missing = client.post("/api/v1/admin/practice/problems/copy-from-assignment",
                          json={"assignment_id": assignment_id, "question_id": 999999},
                          headers=admin_header)
    assert missing.status_code == 404


def test_settings_preflight_and_usage_endpoints(client, db_session_factory):
    """设置/预检/用量三个只读写端点的基本契约。"""
    _, admin_header = as_admin(client, db_session_factory)
    settings = client.get("/api/v1/admin/practice/settings", headers=admin_header)
    assert settings.status_code == 200
    assert settings.json()["grading_mode"] == "deterministic"
    assert settings.json()["max_concurrent_practice_jobs"] >= 1

    patched = client.patch("/api/v1/admin/practice/settings",
                           json={"daily_submission_cap_per_student": 7,
                                 "daily_ai_budget_tokens": 1234},
                           headers=admin_header)
    assert patched.status_code == 200
    assert patched.json()["daily_submission_cap_per_student"] == 7
    assert patched.json()["daily_ai_budget_tokens"] == 1234

    preflight = client.get("/api/v1/admin/practice/ai-preflight", headers=admin_header)
    assert preflight.status_code == 200
    assert preflight.json()["ready"] is True
    assert "queue" in preflight.json() and "ai_budget" in preflight.json()

    usage = client.get("/api/v1/admin/practice/usage", headers=admin_header)
    assert usage.status_code == 200
    body = usage.json()
    assert body["ai_budget_tokens"] == 1234
    assert len(body["daily"]) == 7
    assert body["queue"]["max_concurrent"] >= 1
