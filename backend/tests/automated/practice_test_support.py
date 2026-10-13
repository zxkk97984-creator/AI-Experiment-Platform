"""批次 5 练习域测试支撑：建管理员/学生、录题、假容器判题器。

假容器判题器只回传**实际值帧**（与真实 runner 同一协议），期望值留在宿主侧——
所以它不需要 docker，也不改变被测的宿主比较逻辑。
"""
from __future__ import annotations

import json

from conftest import auth_header, create_user, login


def make_admin(db_session_factory, username="practice-admin"):
    return create_user(db_session_factory, username, "admin")


def make_student(db_session_factory, username="practice-student"):
    return create_user(db_session_factory, username, "student")


def make_teacher(db_session_factory, username="practice-teacher"):
    return create_user(db_session_factory, username, "teacher")


def as_admin(client, db_session_factory, username="practice-admin"):
    user = make_admin(db_session_factory, username)
    token, _ = login(client, username)
    return user, auth_header(token)


def as_student(client, db_session_factory, username="practice-student"):
    user = make_student(db_session_factory, username)
    token, _ = login(client, username)
    return user, auth_header(token)


def as_teacher(client, db_session_factory, username="practice-teacher"):
    user = make_teacher(db_session_factory, username)
    token, _ = login(client, username)
    return user, auth_header(token)


def problem_payload(*, title="两数之和", cases=None, function_name="solution",
                    hidden_tests=None, difficulty="easy", tags=None, **kwargs):
    """管理员录题 payload：args 是原始 JSON 位置参数，expected 是原始期望值。"""
    case_list = cases if cases is not None else [
        {"id": "c1", "args": [1, 2], "expected": 3},
        {"id": "c2", "args": [10, -4], "expected": 6},
    ]
    payload = {
        "title": title,
        "difficulty": difficulty,
        "tags": list(tags or ["基础"]),
        "prompt": "实现 solution(a, b) 返回两数之和。",
        "starter_code": "def solution(a, b):\n    pass\n",
        "public_cases": [{"id": "p1", "args": [1, 1], "expected": 2}],
        "hidden_tests": hidden_tests if hidden_tests is not None else case_list,
        "function_name": function_name,
        "reference_solution": "def solution(a, b):\n    return a + b\n",
    }
    payload.update(kwargs)
    return payload


def create_and_publish(client, admin_header, *, cases=None, **kwargs):
    response = client.post("/api/v1/admin/practice/problems",
                           json=problem_payload(cases=cases, **kwargs), headers=admin_header)
    assert response.status_code == 201, response.text
    problem = response.json()
    published = client.post(f"/api/v1/admin/practice/problems/{problem['id']}/publish",
                            headers=admin_header)
    assert published.status_code == 200, published.text
    return problem, published.json()


def expected_value_map(db_session_factory, problem_id, *, mode="deterministic"):
    """从当前评分版本 payload 读出期望值，供假容器判题器构造"通过"的帧。"""
    from app.models import JudgeGradingVersion
    from app.models.practice import PracticeProblem
    from app.services import practice_service

    with db_session_factory() as db:
        problem = db.get(PracticeProblem, problem_id)
        version = None
        if problem is not None and problem.current_grading_version_id:
            candidate = db.get(JudgeGradingVersion, problem.current_grading_version_id)
            if candidate is not None and candidate.practice_problem_id == problem_id:
                version = candidate
        if version is None:
            version = practice_service.latest_version_for_mode(
                db, problem_id=problem_id, mode=mode)
        if version is None:
            return {}
        payload = version.payload or {}
        cases = (payload.get("binary_cases") or [])
        if not cases:
            cases = [case for group in (payload.get("groups") or [])
                     for case in (group.get("cases") or [])]
        return {str(case["id"]): case["expected"] for case in cases}


def fake_docker_runner(expected_by_id, *, pass_ids=None, fail_ids=None,
                       returncode=0, stderr_text=None):
    """假容器：按帧协议回传实际值；``pass_ids`` 之外的用例回传一个错误值。"""
    passing = set(expected_by_id) if pass_ids is None else set(pass_ids)
    if fail_ids:
        passing -= set(fail_ids)

    def _runner(workdir, settings, timeout_seconds, memory_limit_mb, *,
                host_workdir=None, image_ref=None, cases=None, heartbeat_callback=None):
        if returncode != 0:
            return "", stderr_text or "boom", returncode, 7
        lines = []
        for case in (cases or []):
            case_id = str(case.get("id"))
            if heartbeat_callback is not None:
                heartbeat_callback()
            value = (expected_by_id.get(case_id) if case_id in passing
                     else {"t": "int", "v": -123456})
            lines.append("DAI_RESULT=" + json.dumps(
                {"id": case_id, "status": "ok", "value": value}, ensure_ascii=False))
        return "student stdout", "\n".join(lines) + "\n", 0, 12

    return _runner


def install_fake_runner(monkeypatch, runner, redis_client=None):
    from app.worker import practice_worker

    monkeypatch.setattr(practice_worker, "_docker_runner", lambda: runner)
    if redis_client is not None:
        # 入队推送必须走测试 Redis，绝不碰真实实例
        monkeypatch.setattr(practice_worker, "_get_redis", lambda: redis_client)


def grade_now(db_session_factory, redis_client, submission_id, monkeypatch,
              expected_by_id, settings, **runner_kwargs):
    """同步跑一次练习判题（跳过 Redis 唤醒，Redis 客户端由用例注入）。"""
    from app.worker import practice_worker

    install_fake_runner(monkeypatch, fake_docker_runner(expected_by_id, **runner_kwargs),
                        redis_client=redis_client)
    practice_worker.grade_submission(db_session_factory, submission_id, settings, redis_client)
    return submission_id
