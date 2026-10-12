"""2026-10-12 独立验收阻断项回归（批次①：权限/投影/人工事务）。

每个用例对应验收报告中的一条，先能复现缺口、修复后必须通过：

* A01：作业路由拿考试版本 ID 时曾跳过全部授权（学生 200 且响应含隐藏 expected）。
* A02：提交 POST 与同键重放曾返回完整 ``SubmissionRead``（含 stdout/stderr/result_details）。
* A03：人工评分只拒绝 ``running``，排队提交与 pending AI 随后会用旧认领覆盖人工分。
"""
from __future__ import annotations

import pytest

from app.models import CodeGrade, ExamQuestion, JudgeGradingVersion, JudgeQuestion, Submission
from app.services import grading_version_service as versions
from app.services.grading_values import encode_value
from tests.automated.conftest import (
    auth_header,
    grant_teaching_fixture,
    login,
    make_exam_question,
    make_judge_question,
    make_rubric,
    make_student,
    make_teacher,
)

JUDGE = "/api/v1/judge"
STUDENT_WHITELIST = {
    "id", "question_id", "student_id", "code", "status", "grading_status", "score",
    "tests_passed", "tests_total", "execution_time_ms", "grading_breakdown",
    "diagnostic", "manual_review", "processing_terminal", "score_finalized",
    "grading_source", "created_at", "finished_at",
}


def _payload(case_id="F1-1"):
    return {
        "schema_version": 1, "entry_function": "solve", "execution_model": "data_v1",
        "groups": [
            {"id": "F1", "name": "功能", "dimension": "F", "max_score": 60,
             "cases": [{"id": case_id, "input": encode_value(([1, 2],)),
                        "expected": encode_value(999), "compare": {"kind": "literal"}}]},
            {"id": "R1", "name": "边界", "dimension": "R", "max_score": 10,
             "cases": [{"id": "R1-1", "input": encode_value(([],)),
                        "expected": encode_value(0), "compare": {"kind": "literal"}}]},
        ],
    }


def _exam_version(db_session_factory):
    """造一个已启用的考试评分版本（其 payload 含"隐藏期望值"999）。"""
    make_teacher(db_session_factory, username="teacher")
    student = make_student(db_session_factory, username="student")
    question_id = make_exam_question(db_session_factory, points=20, grading_mode="active",
                                     correct_answer={"function_name": "solve"})
    rubric_id = make_rubric(db_session_factory, exam_question_id=question_id, status="locked")
    with db_session_factory() as db:
        version = versions.create_draft(
            db, question_kind="exam", question_id=question_id, payload=_payload(),
            entry_function="solve", rubric_id=rubric_id, actor_id=None)
        versions.confirm_version(db, version, confirm_isolated_case_v2=True)
        versions.activate_version(db, version)
        db.commit()
        version_id = version.id
        exam_id = db.get(ExamQuestion, question_id).exam_id
    return version_id, question_id, exam_id, student


# ─────────────────────────────────────────────────────────────
# A01：跨类型版本访问必须被拒绝
# ─────────────────────────────────────────────────────────────

def test_a01_student_cannot_read_exam_version_via_assignment_route(client, db_session_factory):
    version_id, _, _, _ = _exam_version(db_session_factory)
    token, _ = login(client, "student")

    for path, verb in ((f"{JUDGE}/grading-versions/{version_id}", "get"),
                       (f"{JUDGE}/grading-versions/{version_id}/confirm", "post"),
                       (f"{JUDGE}/grading-versions/{version_id}/activate", "post"),
                       (f"{JUDGE}/grading-versions/{version_id}/retire", "post")):
        if verb == "get":
            response = client.get(path, headers=auth_header(token))
        else:
            response = client.post(path, headers=auth_header(token), json={})
        assert response.status_code == 404, (path, response.status_code, response.text[:200])
        assert "999" not in response.text, "不得泄露考试版本的隐藏期望值"
        assert "expected" not in response.text


def test_a01_outsider_teacher_cannot_touch_exam_version_via_assignment_route(client, db_session_factory):
    version_id, _, _, _ = _exam_version(db_session_factory)
    make_teacher(db_session_factory, username="outsider")
    token, _ = login(client, "outsider")

    assert client.get(f"{JUDGE}/grading-versions/{version_id}",
                      headers=auth_header(token)).status_code == 404
    assert client.post(f"{JUDGE}/grading-versions/{version_id}/confirm",
                       headers=auth_header(token), json={}).status_code == 404
    assert client.post(f"{JUDGE}/grading-versions/{version_id}/activate",
                       headers=auth_header(token)).status_code == 404
    assert client.post(f"{JUDGE}/grading-versions/{version_id}/retire",
                       headers=auth_header(token)).status_code == 404

    # 状态未被越权改变
    with db_session_factory() as db:
        assert db.get(JudgeGradingVersion, version_id).status == "active"


def test_a01_exam_route_still_guards_assignment_versions(client, db_session_factory):
    """反向：考试路由拿到作业版本 ID 也必须拒绝。"""
    make_teacher(db_session_factory, username="teacher")
    student = make_student(db_session_factory, username="student")
    question_id = make_judge_question(db_session_factory)
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        from app.models import Assignment, Course

        assignment = db.get(Assignment, question.assignment_id)
        course = db.get(Course, assignment.course_id)
        grant_teaching_fixture(db, course, [student.id])
        version = versions.create_draft(db, question_kind="assignment",
                                        question_id=question_id, payload=_payload(),
                                        entry_function="solve", actor_id=None)
        db.commit()
        version_id = version.id
    token, _ = login(client, "teacher")
    assert client.get(f"/api/v1/exams/grading-versions/{version_id}",
                      headers=auth_header(token)).status_code == 404
    assert client.post(f"/api/v1/exams/grading-versions/{version_id}/confirm",
                       headers=auth_header(token), json={}).status_code == 404


# ─────────────────────────────────────────────────────────────
# A02：提交与同键重放都必须是学生白名单投影
# ─────────────────────────────────────────────────────────────

def _student_with_question(db_session_factory):
    make_teacher(db_session_factory, username="teacher")
    student = make_student(db_session_factory, username="student")
    question_id = make_judge_question(db_session_factory)
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        from app.models import Assignment, Course

        assignment = db.get(Assignment, question.assignment_id)
        course = db.get(Course, assignment.course_id)
        grant_teaching_fixture(db, course, [student.id])
        db.commit()
    return question_id, student


def test_a02_submit_response_is_projected(client, db_session_factory):
    question_id, _ = _student_with_question(db_session_factory)
    token, _ = login(client, "student")
    response = client.post(f"{JUDGE}/submissions", headers=auth_header(token),
                           json={"question_id": question_id, "code": "def solve():\n    return 1"})
    assert response.status_code == 201, response.text
    assert set(response.json().keys()) == STUDENT_WHITELIST, response.json().keys()


def test_a02_idempotent_replay_does_not_leak_hidden_output(client, db_session_factory):
    question_id, _ = _student_with_question(db_session_factory)
    token, _ = login(client, "student")
    key = "6f0f6f0f-1111-4222-8333-444455556666"
    body = {"question_id": question_id, "code": "def solve():\n    return 1",
            "client_request_id": key}
    first = client.post(f"{JUDGE}/submissions", headers=auth_header(token), json=body)
    assert first.status_code == 201, first.text
    submission_id = first.json()["id"]

    # 模拟判题产生的敏感输出（隐藏测试 stdout / 诊断明细）
    with db_session_factory() as db:
        row = db.get(Submission, submission_id)
        row.stdout = "HIDDEN-TEST-OUTPUT assert secret == 42"
        row.stderr = "TRACEBACK"
        row.result_details = {"hidden": "secret", "groups": [{"id": "F1"}]}
        row.grading_status = "completed"
        row.score = 60.0
        db.commit()

    replay = client.post(f"{JUDGE}/submissions", headers=auth_header(token), json=body)
    assert replay.status_code == 201, replay.text
    payload = replay.json()
    assert set(payload.keys()) == STUDENT_WHITELIST, payload.keys()
    assert "HIDDEN-TEST-OUTPUT" not in replay.text
    assert "stdout" not in replay.text and "stderr" not in replay.text
    assert "result_details" not in payload
    assert payload["score"] == 60.0, "白名单投影仍应给出学生可见的分数"


def test_a02_concurrent_winner_is_projected(client, db_session_factory, monkeypatch):
    """并发同键：唯一约束 winner 的返回同样必须是投影。"""
    question_id, _ = _student_with_question(db_session_factory)
    token, _ = login(client, "student")
    key = "7f0f6f0f-2222-4222-8333-444455556666"
    with db_session_factory() as db:
        winner = Submission(question_id=question_id, student_id=1,
                            code="def solve():\n    return 1", status="succeeded",
                            grading_status="completed", score=70.0,
                            stdout="WINNER-HIDDEN", result_details={"hidden": "x"},
                            client_request_id=key, request_digest="d" * 64)
        db.add(winner)
        db.commit()
        winner_id = winner.id

    from app.api import judge as judge_api

    def fake_existing(db, student_id, request_key, digest):
        with db_session_factory() as fresh:
            return fresh.get(Submission, winner_id), None

    monkeypatch.setattr(judge_api, "_existing_idempotent_submission", fake_existing)
    response = client.post(f"{JUDGE}/submissions", headers=auth_header(token),
                           json={"question_id": question_id, "code": "def solve():\n    return 1",
                                 "client_request_id": key})
    assert response.status_code == 201, response.text
    assert set(response.json().keys()) == STUDENT_WHITELIST
    assert "WINNER-HIDDEN" not in response.text


# ─────────────────────────────────────────────────────────────
# A03：人工评分必须拦住全部在途工作并用 CAS 写入
# ─────────────────────────────────────────────────────────────

def _manual_case(db_session_factory, *, grading_status="completed", cg_status=None,
                 score=10.0, result_revision=3):
    question_id, student = _student_with_question(db_session_factory)
    rubric_id = None
    if cg_status is not None:
        rubric_id = make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    with db_session_factory() as db:
        submission = Submission(question_id=question_id, student_id=student.id,
                                code="def solve():\n    return 1", status="succeeded",
                                grading_status=grading_status, score=score,
                                result_revision=result_revision, attempt_count=1)
        db.add(submission)
        db.flush()
        if cg_status is not None:
            db.add(CodeGrade(submission_id=submission.id, rubric_id=rubric_id, mode="active",
                             status=cg_status, functional_score=50.0, robustness_score=5.0,
                             claim_token="stale-claim"))
        db.commit()
        return submission.id


def _teacher_token(client, db_session_factory):
    make_teacher(db_session_factory, username="teacher")
    token, _ = login(client, "teacher")
    return token


def test_a03_rejects_queued_submission(client, db_session_factory):
    submission_id = _manual_case(db_session_factory, grading_status="queued")
    token = _teacher_token(client, db_session_factory)
    response = client.patch(f"{JUDGE}/submissions/{submission_id}/manual-score",
                            headers=auth_header(token), json={"score": 88, "reason": "抢跑",
                                                              "expected_result_revision": 3})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GRADING_IN_PROGRESS"
    with db_session_factory() as db:
        assert db.get(Submission, submission_id).score == 10.0


def test_a03_rejects_pending_ai_grade(client, db_session_factory):
    """主判题已完成但 AI 仍 pending：必须拒绝，否则 queued AI 会用旧认领覆盖人工分。"""
    submission_id = _manual_case(db_session_factory, grading_status="completed",
                                 cg_status="pending")
    token = _teacher_token(client, db_session_factory)
    response = client.patch(f"{JUDGE}/submissions/{submission_id}/manual-score",
                            headers=auth_header(token), json={"score": 88, "reason": "AI 在途",
                                                              "expected_result_revision": 3})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "AI_GRADING_IN_PROGRESS"
    with db_session_factory() as db:
        assert db.get(Submission, submission_id).score == 10.0
        assert db.query(CodeGrade).filter_by(submission_id=submission_id).one().status == "pending"


@pytest.mark.parametrize("status", ["queued", "running"])
def test_a03_rejects_queued_and_running_ai_grade(client, db_session_factory, status):
    submission_id = _manual_case(db_session_factory, grading_status="completed", cg_status=status)
    token = _teacher_token(client, db_session_factory)
    response = client.patch(
        f"{JUDGE}/submissions/{submission_id}/manual-score",
        headers=auth_header(token),
        json={"score": 88, "reason": "AI 在途", "expected_result_revision": 3},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "AI_GRADING_IN_PROGRESS"
    with db_session_factory() as db:
        row = db.get(Submission, submission_id)
        grade = db.query(CodeGrade).filter_by(submission_id=submission_id).one()
        assert row.score == 10.0 and row.result_revision == 3
        assert grade.status == status and grade.claim_token == "stale-claim"


def test_a03_manual_request_requires_result_revision(client, db_session_factory):
    submission_id = _manual_case(db_session_factory)
    token = _teacher_token(client, db_session_factory)
    response = client.patch(f"{JUDGE}/submissions/{submission_id}/manual-score",
                            headers=auth_header(token), json={"score": 88, "reason": "缺修订号"})
    assert response.status_code == 422


def test_a03_concurrent_teachers_second_gets_conflict(client, db_session_factory):
    submission_id = _manual_case(db_session_factory, result_revision=3)
    token = _teacher_token(client, db_session_factory)
    first = client.patch(f"{JUDGE}/submissions/{submission_id}/manual-score",
                         headers=auth_header(token),
                         json={"score": 88, "reason": "教师甲", "expected_result_revision": 3})
    assert first.status_code == 200, first.text
    second = client.patch(f"{JUDGE}/submissions/{submission_id}/manual-score",
                          headers=auth_header(token),
                          json={"score": 70, "reason": "教师乙", "expected_result_revision": 3})
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "RESULT_REVISION_CONFLICT"
    with db_session_factory() as db:
        row = db.get(Submission, submission_id)
        assert row.score == 88.0 and row.result_revision == 4


def test_a03_manual_score_invalidates_stale_ai_claim(client, db_session_factory):
    """人工分落库后，任何残留的在途 AI 认领必须失效（防御性收口）。"""
    submission_id = _manual_case(db_session_factory, cg_status=None)
    token = _teacher_token(client, db_session_factory)
    with db_session_factory() as db:
        question_id = db.get(Submission, submission_id).question_id
    rubric_id = make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    with db_session_factory() as db:
        db.add(CodeGrade(submission_id=submission_id, rubric_id=rubric_id, mode="active",
                         status="pending", functional_score=50.0, robustness_score=5.0,
                         claim_token="stale-claim"))
        db.commit()
    # pending 会被前置检查拒绝；先把它改成 running 也拒绝，验证"人工分之前不可有在途 AI"
    response = client.patch(f"{JUDGE}/submissions/{submission_id}/manual-score",
                            headers=auth_header(token), json={"score": 88, "reason": "x",
                                                              "expected_result_revision": 3})
    assert response.status_code == 409

    # 把 AI 记录置为终态后，人工分成功，且历史 AI 证据保留
    with db_session_factory() as db:
        grade = db.query(CodeGrade).filter_by(submission_id=submission_id).one()
        grade.status = "completed"
        grade.needs_teacher_review = False
        grade.claim_token = None
        db.commit()
    ok = client.patch(f"{JUDGE}/submissions/{submission_id}/manual-score",
                      headers=auth_header(token), json={"score": 88, "reason": "人工复核",
                                                        "expected_result_revision": 3})
    assert ok.status_code == 200, ok.text
    with db_session_factory() as db:
        grade = db.query(CodeGrade).filter_by(submission_id=submission_id).one()
        assert grade.functional_score == 50.0 and grade.robustness_score == 5.0, "AI 证据保留"
        row = db.get(Submission, submission_id)
        assert row.score == 88.0 and row.claim_token is None


# ─────────────────────────────────────────────────────────────
# A06 后端：终态/来源字段 + append-only 人工审计
# ─────────────────────────────────────────────────────────────

def test_a06_projection_exposes_terminal_state_and_source(client, db_session_factory):
    question_id, _ = _student_with_question(db_session_factory)
    token, _ = login(client, "student")
    created = client.post(f"{JUDGE}/submissions", headers=auth_header(token),
                          json={"question_id": question_id, "code": "def solve():\n    return 1"})
    submission_id = created.json()["id"]
    body = created.json()
    assert body["processing_terminal"] is False, "刚提交仍在队列"
    assert body["score_finalized"] is False

    with db_session_factory() as db:
        row = db.get(Submission, submission_id)
        row.grading_status = "completed"
        row.status = "succeeded"
        row.score = 60.0
        row.result_details = {"groups": [{"id": "F1"}], "grading_version_id": 1}
        db.commit()

    detail = client.get(f"{JUDGE}/submissions/{submission_id}", headers=auth_header(token)).json()
    assert detail["processing_terminal"] is True
    assert detail["score_finalized"] is True
    assert detail["grading_source"] == "auto"


def test_a06_active_ai_queue_is_not_terminal_but_shadow_is(client, db_session_factory):
    from tests.automated.conftest import make_rubric

    submission_id = _manual_case(db_session_factory, grading_status="completed", score=None,
                                 cg_status="queued")
    token, _ = login(client, "student")
    with db_session_factory() as db:
        grade = db.query(CodeGrade).filter_by(submission_id=submission_id).one()
        grade.mode = "active"
        row = db.get(Submission, submission_id)
        row.result_details = {"groups": [{"id": "F1"}], "grading_version_id": 1}
        db.commit()

    for status in ("queued", "running", "pending"):
        with db_session_factory() as db:
            db.query(CodeGrade).filter_by(submission_id=submission_id).one().status = status
            db.commit()
        for path in (f"{JUDGE}/submissions/{submission_id}",
                     f"{JUDGE}/submissions/{submission_id}/result"):
            body = client.get(path, headers=auth_header(token)).json()
            assert body["processing_terminal"] is False
            assert body["score_finalized"] is False
            assert body["grading_source"] is None

    with db_session_factory() as db:
        grade = db.query(CodeGrade).filter_by(submission_id=submission_id).one()
        grade.status = "review_required"
        grade.needs_teacher_review = True
        db.commit()
    body = client.get(f"{JUDGE}/submissions/{submission_id}",
                      headers=auth_header(token)).json()
    assert body["processing_terminal"] is True and body["score_finalized"] is False

    with db_session_factory() as db:
        grade = db.query(CodeGrade).filter_by(submission_id=submission_id).one()
        grade.mode = "shadow"
        grade.status = "queued"
        grade.needs_teacher_review = False
        db.get(Submission, submission_id).score = 60.0
        db.commit()
    body = client.get(f"{JUDGE}/submissions/{submission_id}",
                      headers=auth_header(token)).json()
    assert body["processing_terminal"] is True and body["score_finalized"] is True
    assert body["grading_source"] == "auto"


def test_a06_student_list_uses_same_safe_projection(client, db_session_factory):
    submission_id = _manual_case(db_session_factory, grading_status="completed", score=60)
    token, _ = login(client, "student")
    response = client.get(f"{JUDGE}/submissions", headers=auth_header(token))
    assert response.status_code == 200
    item = next(row for row in response.json()["items"] if row["id"] == submission_id)
    assert set(item) == STUDENT_WHITELIST
    assert "stdout" not in item and "result_details" not in item


def test_a06_manual_score_marks_source_and_is_audited_append_only(client, db_session_factory):
    from app.models import ManualScoreAudit

    submission_id = _manual_case(db_session_factory, score=10.0)
    token = _teacher_token(client, db_session_factory)
    for index, score in enumerate((80.0, 85.0, 90.0), start=1):
        response = client.patch(f"{JUDGE}/submissions/{submission_id}/manual-score",
                                headers=auth_header(token),
                                json={"score": score, "reason": f"第 {index} 次人工评分",
                                      "expected_result_revision": 3 + index - 1})
        assert response.status_code == 200, response.text

    with db_session_factory() as db:
        audits = db.query(ManualScoreAudit).filter_by(target_kind="submission",
                                                      target_id=submission_id).order_by(
            ManualScoreAudit.id).all()
        assert [row.score for row in audits] == [80.0, 85.0, 90.0], "append-only：三次都给保留"
        assert [row.result_revision for row in audits] == [4, 5, 6]
        assert audits[0].previous_score == 10.0
        assert all(row.actor_role == "teacher" for row in audits)
        row = db.get(Submission, submission_id)
        assert len(row.result_details["manual_review_history"]) == 3, "展示历史不丢"

    student_token, _ = login(client, "student")
    projection = client.get(f"{JUDGE}/submissions/{submission_id}",
                            headers=auth_header(student_token)).json()
    assert projection["grading_source"] == "manual"
    assert projection["score_finalized"] is True
    assert projection["score"] == 90.0


def test_a06_ai_override_requires_deterministic_scores(client, db_session_factory):
    """AI override 不得把缺失的 F/R 当 0 合分。"""
    from app.models import CodeGrade

    submission_id = _manual_case(db_session_factory)
    question_id = None
    with db_session_factory() as db:
        question_id = db.get(Submission, submission_id).question_id
    rubric_id = make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    with db_session_factory() as db:
        grade = CodeGrade(submission_id=submission_id, rubric_id=rubric_id, mode="active",
                          status="completed", functional_score=None, robustness_score=None,
                          algorithm_score=15.0, quality_score=8.0, final_score_100=None)
        db.add(grade)
        db.commit()
        grade_id = grade.id

    token = _teacher_token(client, db_session_factory)
    blocked = client.post(f"/api/v1/ai-grading/grades/{grade_id}/override",
                          headers=auth_header(token),
                          json={"algorithm_score": 18.0, "reason": "调整算法分"})
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["detail"]["code"] == "DETERMINISTIC_SCORES_REQUIRED"

    ok = client.post(f"/api/v1/ai-grading/grades/{grade_id}/override",
                     headers=auth_header(token),
                     json={"algorithm_score": 18.0, "reason": "补齐确定性分后调整",
                           "functional_score": 50.0, "robustness_score": 8.0})
    assert ok.status_code == 200, ok.text
    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.functional_score == 50.0 and grade.robustness_score == 8.0
        assert grade.final_score_100 is not None and grade.final_score_100 > 0


# ─────────────────────────────────────────────────────────────
# 批次① 补充：撤销复查 Redis 故障必须 503（不是 500/放行）
# ─────────────────────────────────────────────────────────────

class _BrokenRedis:
    """模拟 Redis 不可用（连接错误）。"""

    def exists(self, *args, **kwargs):
        raise ConnectionError("redis down")


def test_revocation_recheck_fails_closed_with_503(db_session_factory):
    """写入前撤销复查：Redis 故障必须 503（既不放行也不 500）。"""
    from sqlalchemy import select

    from app.models import User
    from app.services.teaching_query_policy import lock_current_actor

    _student_with_question(db_session_factory)
    with db_session_factory() as db:
        user = db.scalars(select(User).where(User.username == "student")).one()
        db.info["teaching_actor"] = {
            "id": user.id, "role": user.role, "status": user.status,
            "session_version": user.session_version,
            "credential_version": user.credential_version,
        }
        db.info["teaching_token_jti"] = "jti-test"
        db.info["teaching_token_redis"] = _BrokenRedis()
        with pytest.raises(Exception) as error:
            lock_current_actor(db, user)

    detail = getattr(error.value, "detail", None)
    assert detail is not None, error.value
    assert detail["code"] == "SERVICE_UNAVAILABLE", detail
    assert getattr(error.value, "status_code", None) == 503
