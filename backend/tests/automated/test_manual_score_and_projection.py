"""批次三 3D：作业人工评分与学生结果白名单投影。

契约：

* 人工评分独立于自动判题与 AI：只产生新的结果版本（``result_revision`` 原子 +1），
  记录评分者/说明/时间，不改写历史；
* 判题在途（``grading_status == "running"``）时拒绝人工分（409 GRADING_IN_PROGRESS）；
* 可选 ``expected_result_revision`` 做乐观并发（409 RESULT_REVISION_CONFLICT）；
* 学生结果只返回白名单字段：**没有** stdout / stderr / 隐藏测试内容 / 教师备注；
  教师与管理员保留完整视图。
"""
from __future__ import annotations

import pytest

from app.models import Submission
from tests.automated.conftest import auth_header, login

STUDENT_WHITELIST = {
    "id", "question_id", "student_id", "code", "status", "grading_status", "score",
    "tests_passed", "tests_total", "execution_time_ms", "grading_breakdown",
    "diagnostic", "manual_review", "processing_terminal", "score_finalized",
    "grading_source", "created_at", "finished_at",
}


def _case(db_session_factory, *, grading_status="completed", stdout="SECRET-HIDDEN-TEST",
          stderr="TRACEBACK", score=10.0, result_details=None):
    """建一条带敏感 stdout/stderr 的提交，并返回 (submission_id, student_token, teacher_token)。"""
    from tests.automated.conftest import (
        grant_teaching_fixture, make_assignment, make_course, make_judge_question,
        make_student, make_teacher,
    )

    from app.models import Assignment, Course, JudgeQuestion

    make_teacher(db_session_factory, username="teacher")
    student = make_student(db_session_factory, username="student")
    question_id = make_judge_question(db_session_factory, hidden_tests="assert secret == 42")
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        assignment = db.get(Assignment, question.assignment_id)
        course = db.get(Course, assignment.course_id)
        grant_teaching_fixture(db, course, [student.id])
        db.commit()
    with db_session_factory() as db:
        submission = Submission(
            question_id=question_id,
            student_id=student.id,
            code="def solve():\n    return 42",
            status="succeeded",
            grading_status=grading_status,
            score=score,
            tests_passed=1,
            tests_total=2,
            stdout=stdout,
            stderr=stderr,
            result_details=result_details if result_details is not None else {"hidden": "secret"},
        )
        db.add(submission)
        db.commit()
        submission_id = submission.id
    return submission_id, question_id


def _tokens(client, db_session_factory):
    student_token, _ = login(client, "student")
    teacher_token, _ = login(client, "teacher")
    return student_token, teacher_token


def _revision(db_session_factory, submission_id):
    with db_session_factory() as db:
        return db.get(Submission, submission_id).result_revision


# ─────────────────────────────────────────────────────────────
# 1. 学生白名单投影
# ─────────────────────────────────────────────────────────────

def test_student_projection_is_exact_whitelist(client, db_session_factory):
    submission_id, _ = _case(db_session_factory)
    student_token, _ = _tokens(client, db_session_factory)

    for path in (f"/api/v1/judge/submissions/{submission_id}",
                 f"/api/v1/judge/submissions/{submission_id}/result"):
        response = client.get(path, headers=auth_header(student_token))
        assert response.status_code == 200, response.text
        payload = response.json()
        assert set(payload.keys()) == STUDENT_WHITELIST, (
            f"{path} 字段集合变化：多出 {set(payload.keys()) - STUDENT_WHITELIST}，"
            f"缺少 {STUDENT_WHITELIST - set(payload.keys())}"
        )
        assert "stdout" not in payload and "stderr" not in payload
        assert "SECRET-HIDDEN-TEST" not in response.text
        assert "result_details" not in payload
        assert "hidden" not in response.text


def test_teacher_and_admin_keep_full_view(client, db_session_factory):
    submission_id, _ = _case(db_session_factory)
    _, teacher_token = _tokens(client, db_session_factory)

    detail = client.get(f"/api/v1/judge/submissions/{submission_id}",
                        headers=auth_header(teacher_token))
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["stdout"] == "SECRET-HIDDEN-TEST" and payload["stderr"] == "TRACEBACK"

    teacher_view = client.get(f"/api/v1/judge/submissions/{submission_id}/teacher",
                              headers=auth_header(teacher_token))
    assert teacher_view.status_code == 200, teacher_view.text
    assert teacher_view.json()["stdout"] == "SECRET-HIDDEN-TEST"


def test_other_student_cannot_read_projection(client, db_session_factory):
    """越权读取被拒：教学范围过滤器把范围外提交当作不存在（404，避免存在性泄露）。"""
    submission_id, _ = _case(db_session_factory)
    from tests.automated.conftest import make_student

    make_student(db_session_factory, username="intruder")
    intruder_token, _ = login(client, "intruder")
    response = client.get(f"/api/v1/judge/submissions/{submission_id}",
                          headers=auth_header(intruder_token))
    assert response.status_code in (403, 404), response.text
    assert "SECRET-HIDDEN-TEST" not in response.text
    assert "stdout" not in response.text


# ─────────────────────────────────────────────────────────────
# 2. 人工评分
# ─────────────────────────────────────────────────────────────

def test_manual_score_creates_new_result_version(client, db_session_factory):
    submission_id, _ = _case(db_session_factory, score=10.0)
    _, teacher_token = _tokens(client, db_session_factory)
    with db_session_factory() as db:
        before = db.get(Submission, submission_id).result_revision

    response = client.patch(f"/api/v1/judge/submissions/{submission_id}/manual-score",
                            headers=auth_header(teacher_token),
                            json={"score": 88.5, "reason": "算法正确但未处理空输入",
                                  "expected_result_revision": before})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["score"] == 88.5
    assert body["grading_status"] == "completed"

    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        assert submission.score == 88.5
        assert submission.result_revision == before + 1, "人工评分必须推进结果版本"
        details = submission.result_details
        assert details["manual_review"]["score"] == 88.5
        assert details["manual_review"]["reason"] == "算法正确但未处理空输入"
        assert details["manual_review"]["actor_id"] is not None
        assert details["manual_review_history"][-1]["score"] == 88.5
        # 不覆盖原始判题输出（教师仍可复核）
        assert submission.stdout == "SECRET-HIDDEN-TEST"


def test_manual_score_history_is_bounded(client, db_session_factory):
    submission_id, _ = _case(db_session_factory)
    _, teacher_token = _tokens(client, db_session_factory)
    for index in range(12):
        response = client.patch(f"/api/v1/judge/submissions/{submission_id}/manual-score",
                                headers=auth_header(teacher_token),
                                json={"score": float(index), "reason": f"第 {index} 次",
                                      "expected_result_revision": _revision(db_session_factory,
                                                                             submission_id)})
        assert response.status_code == 200, response.text
    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        history = submission.result_details["manual_review_history"]
        assert len(history) == 10
        assert history[-1]["reason"] == "第 11 次"
        assert submission.result_revision == 12


def test_manual_score_rejected_while_grading_running(client, db_session_factory):
    submission_id, _ = _case(db_session_factory, grading_status="running")
    _, teacher_token = _tokens(client, db_session_factory)
    response = client.patch(f"/api/v1/judge/submissions/{submission_id}/manual-score",
                            headers=auth_header(teacher_token),
                            json={"score": 50, "reason": "抢跑",
                                  "expected_result_revision": _revision(db_session_factory,
                                                                         submission_id)})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GRADING_IN_PROGRESS"
    with db_session_factory() as db:
        assert db.get(Submission, submission_id).score == 10.0, "在途判题期间不得改写分数"


def test_manual_score_optimistic_concurrency(client, db_session_factory):
    submission_id, _ = _case(db_session_factory)
    _, teacher_token = _tokens(client, db_session_factory)
    with db_session_factory() as db:
        revision = db.get(Submission, submission_id).result_revision
    stale = client.patch(f"/api/v1/judge/submissions/{submission_id}/manual-score",
                         headers=auth_header(teacher_token),
                         json={"score": 60, "reason": "旧页面", "expected_result_revision": revision + 5})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "RESULT_REVISION_CONFLICT"
    fresh = client.patch(f"/api/v1/judge/submissions/{submission_id}/manual-score",
                         headers=auth_header(teacher_token),
                         json={"score": 60, "reason": "当前页面", "expected_result_revision": revision})
    assert fresh.status_code == 200, fresh.text


def test_students_and_outsiders_cannot_manual_score(client, db_session_factory):
    submission_id, _ = _case(db_session_factory)
    student_token, teacher_token = _tokens(client, db_session_factory)

    as_student = client.patch(f"/api/v1/judge/submissions/{submission_id}/manual-score",
                              headers=auth_header(student_token),
                              json={"score": 100, "reason": "自评分",
                                    "expected_result_revision": _revision(db_session_factory,
                                                                           submission_id)})
    assert as_student.status_code == 403

    from tests.automated.conftest import make_teacher

    make_teacher(db_session_factory, username="other_teacher")
    other_token, _ = login(client, "other_teacher")
    as_other = client.patch(f"/api/v1/judge/submissions/{submission_id}/manual-score",
                            headers=auth_header(other_token),
                            json={"score": 100, "reason": "越权",
                                  "expected_result_revision": _revision(db_session_factory,
                                                                         submission_id)})
    # 范围外教师同样由教学范围过滤器拒之门外（404）或由端点拒绝（403）
    assert as_other.status_code in (403, 404), as_other.text
    with db_session_factory() as db:
        assert db.get(Submission, submission_id).score == 10.0


@pytest.mark.parametrize("payload", [
    {"score": -1, "reason": "负数"},
    {"score": 10, "reason": ""},
    {"score": 10},
    {"score": 10, "reason": "x", "expected_result_revision": -1},
    {"score": 10, "reason": "缺少修订号"},
    {"score": 10, "reason": "x", "extra": 1},
])
def test_manual_score_request_validation(client, db_session_factory, payload):
    submission_id, _ = _case(db_session_factory)
    _, teacher_token = _tokens(client, db_session_factory)
    response = client.patch(f"/api/v1/judge/submissions/{submission_id}/manual-score",
                            headers=auth_header(teacher_token), json=payload)
    assert response.status_code == 422


def test_manual_score_does_not_touch_ai_grade(client, db_session_factory):
    """AI 已终态时人工评分成功且不改写 AI 证据；AI 仍在途时先被 A03 门禁拒绝。"""
    from tests.automated.conftest import make_code_grade, make_rubric

    submission_id, question_id = _case(db_session_factory)
    rubric_id = make_rubric(db_session_factory, judge_question_id=question_id)
    grade_id = make_code_grade(db_session_factory, submission_id=submission_id,
                               rubric_id=rubric_id, final_score_100=73.0)
    _, teacher_token = _tokens(client, db_session_factory)

    # 默认 make_code_grade 是 pending：属于在途 AI，必须先被拒绝（A03）
    in_flight = client.patch(f"/api/v1/judge/submissions/{submission_id}/manual-score",
                             headers=auth_header(teacher_token),
                             json={"score": 12.0, "reason": "AI 在途",
                                   "expected_result_revision": _revision(db_session_factory,
                                                                          submission_id)})
    assert in_flight.status_code == 409
    assert in_flight.json()["detail"]["code"] == "AI_GRADING_IN_PROGRESS"

    from app.models import CodeGrade

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        grade.status = "completed"
        db.commit()

    response = client.patch(f"/api/v1/judge/submissions/{submission_id}/manual-score",
                            headers=auth_header(teacher_token),
                            json={"score": 12.0, "reason": "人工覆盖",
                                  "expected_result_revision": _revision(db_session_factory,
                                                                         submission_id)})
    assert response.status_code == 200, response.text

    from app.models import CodeGrade

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.final_score_100 == 73.0, "人工评分不得改写 AI 评分记录"
        assert db.get(Submission, submission_id).score == 12.0


# ─────────────────────────────────────────────────────────────
# 3. 人工分进入成绩簿来源（快照不被改写）
# ─────────────────────────────────────────────────────────────

def test_manual_score_flows_into_gradebook_source(client, db_session_factory):
    """人工评分后，成绩簿的作业来源分读取新分数；不触碰任何已确认快照。"""
    from types import SimpleNamespace

    from tests.automated.conftest import grant_teaching_fixture, make_student, make_teacher
    from app.models import Assignment, Course, JudgeQuestion, Submission
    from app.services.gradebook_service import source_score

    make_teacher(db_session_factory, username="teacher")
    student = make_student(db_session_factory, username="student")
    submission_id, question_id = _case(db_session_factory)
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        assignment = db.get(Assignment, question.assignment_id)
        course = db.get(Course, assignment.course_id)
        grant_teaching_fixture(db, course, [student.id])
        assignment.status = "published"
        db.commit()
        offering_id = assignment.offering_id
        assignment_id = assignment.id

    column = SimpleNamespace(source_kind="assignment", source_id=assignment_id, selection="latest")
    with db_session_factory() as db:
        before = source_score(db, column, offering_id, student.id)
    assert before["state"] == "scored" and before["score"] == "10.00", before

    _, teacher_token = _tokens(client, db_session_factory)
    response = client.patch(f"/api/v1/judge/submissions/{submission_id}/manual-score",
                            headers=auth_header(teacher_token),
                            json={"score": 95.0, "reason": "人工复核",
                                  "expected_result_revision": _revision(db_session_factory,
                                                                         submission_id)})
    assert response.status_code == 200, response.text

    with db_session_factory() as db:
        after = source_score(db, column, offering_id, student.id)
    assert after["state"] == "scored"
    assert after["score"] == "95.00", after
    assert after["source"]["kind"] == "assignment"
    assert after["source"]["questions"][0]["score"] == "95.0"
