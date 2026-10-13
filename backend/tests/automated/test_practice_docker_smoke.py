"""批次 5 真实 Docker 判题 smoke（默认 skip，`DAI_RUN_DOCKER_SMOKE=1` 启用）。

与其余练习用例的区别：这里**不替换**容器执行器，真的启动判题容器跑学生代码，
验证「普通判题」端到端计分。对照组：正确解 100 分 / 错误解 0 分（学生自己写错
是合法 0 分，与「平台/AI 故障绝不写 0 分」是两件事）。
"""
from __future__ import annotations

import os

import pytest

from app.config import get_settings
from app.models import CodeGrade
from app.models.practice import PracticeSubmission
from practice_test_support import (
    as_admin, as_student, create_and_publish, expected_value_map, problem_payload,
)

pytestmark = [
    pytest.mark.usefixtures("db_session_factory"),
    pytest.mark.skipif(
        os.environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
        reason="需要真实 Docker 与判题镜像；设置 DAI_RUN_DOCKER_SMOKE=1 启用"),
]

CORRECT = "def solution(a, b):\n    return a + b\n"
WRONG = "def solution(a, b):\n    return a - b\n"


def _submit(client, header, problem_id, code, key):
    response = client.post(f"/api/v1/practice/problems/{problem_id}/submissions",
                           json={"code": code, "client_request_id": key}, headers=header)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def _grade(db_session_factory, submission_id):
    from app.worker import practice_worker

    settings = get_settings()
    practice_worker.grade_submission(db_session_factory, submission_id, settings, None)
    with db_session_factory() as db:
        row = db.get(PracticeSubmission, submission_id)
        assert row.grading_status == "completed", row.last_error
        assert row.code_grade_id is None, "普通判题不应创建 AI 评分记录"
        assert db.query(CodeGrade).count() == 0
        return row.score


def test_real_container_deterministic_grading(client, db_session_factory):
    _, admin_header = as_admin(client, db_session_factory)
    created = client.post("/api/v1/admin/practice/problems", json=problem_payload(),
                          headers=admin_header)
    assert created.status_code == 201, created.text
    problem_id = created.json()["id"]
    assert client.post(f"/api/v1/admin/practice/problems/{problem_id}/publish",
                       headers=admin_header).status_code == 200

    _, student = as_student(client, db_session_factory, "docker-student")
    expected = expected_value_map(db_session_factory, problem_id)
    assert set(expected) == {"c1", "c2"}, "用例必须来自当前评分版本"

    good_id = _submit(client, student, problem_id, CORRECT, "docker-ok")
    assert _grade(db_session_factory, good_id) == 100.0

    bad_id = _submit(client, student, problem_id, WRONG, "docker-bad")
    assert _grade(db_session_factory, bad_id) == 0.0, "学生自己写错是合法 0 分"

    # 最高分口径：榜单只取该题最高分（100），不因后来的 0 分下降
    board = client.get("/api/v1/practice/leaderboard?board=deterministic&scope=platform",
                       headers=student).json()
    assert board["items"][0]["total_score"] == 100.0
    detail = client.get(f"/api/v1/practice/submissions/{bad_id}", headers=student).json()
    assert detail["deterministic"]["passed_count"] == 0
    assert detail["deterministic"]["total_count"] == 2
