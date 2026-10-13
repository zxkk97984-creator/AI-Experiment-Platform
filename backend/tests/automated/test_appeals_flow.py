"""批次 2 验收：单次成绩异议（窗口 / 幂等 / 撤回 / 限流 / 权限 / 改分 / 重评）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import (
    Assignment,
    AuditEvent,
    CodeGrade,
    Course,
    ExamAnswer,
    ExamQuestion,
    ExamSubmission,
    GradeAppeal,
    GradeOverride,
    JudgeGradingVersion,
    QuestionRubric,
    Submission,
)
from app.services.ai_grading_queue import finish_ai_grade
from app.services import grading_version_service as versions
from tests.automated.conftest import (
    auth_header,
    create_user,
    grant_teaching_fixture,
    login,
    make_assignment,
    make_exam_chain,
    make_judge_question,
    make_rubric,
    make_submission,
    make_teacher,
)

API = "/api/v1/appeals"

REASON = "老师您好，这道题的第三组测试我认为我的解法是正确的，请求复核。" * 2


def _headers(client, username):
    token, _ = login(client, username)
    return auth_header(token)


def _simple_grade(
    db_session_factory, *, student_username="student", finished_days_ago=0.0,
    mode="active", cg_status="completed", claim_token=None, needs_review=False,
    score=55.0, extra_result_details=None,
):
    """一条可申请异议的 CodeGrade（默认 active + 已终态）。"""
    submission_id = make_submission(
        db_session_factory, student_username=student_username, grading_status="completed",
        score=score, result_details=extra_result_details or {},
    )
    with db_session_factory() as db:
        question_id = db.get(Submission, submission_id).question_id
    rubric_id = make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    finished_at = datetime.now(timezone.utc) - timedelta(days=finished_days_ago)
    with db_session_factory() as db:
        grade = CodeGrade(
            submission_id=submission_id, rubric_id=rubric_id, mode=mode, status=cg_status,
            functional_score=40.0, robustness_score=5.0, algorithm_score=5.0, quality_score=5.0,
            final_score_100=score, claim_token=claim_token, needs_teacher_review=needs_review,
            finished_at=finished_at,
        )
        db.add(grade)
        db.commit()
        return grade.id, submission_id


def _versioned_grade(db_session_factory, *, student_username="student", score=55.0):
    """带可信评分版本基线的 CodeGrade——重评（retry 通道）要求来源可验证。"""
    grade_id, submission_id = _simple_grade(db_session_factory, student_username=student_username,
                                            score=score)
    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        question_id = submission.question_id
        rubric_id = db.get(CodeGrade, grade_id).rubric_id
        version = JudgeGradingVersion(
            judge_question_id=question_id, version=1, status="active",
            execution_mode="data_v1", grading_mode_snapshot="active", entry_function="solve",
            payload={"schema_version": 2, "execution_model": "isolated_case_v2",
                     "binary_cases": [], "groups": [{"id": "F1", "dimension": "F",
                                                     "max_score": 60.0}]},
            compare_rules={}, score_cap_rules=[], context_snapshot={"prompt": "测试题"},
            rubric_id=rubric_id, source_kind="manual",
            source_digest="a" * 64, spec_digest="b" * 64,
        )
        db.add(version)
        db.flush()
        submission.grading_version_id = version.id
        grade = db.get(CodeGrade, grade_id)
        grade.deterministic_details = {
            "groups": [{"id": "F1", "dimension": "F", "max_score": 60.0}],
            "grading_version_id": version.id,
            "source": {"result_revision": submission.result_revision or 0,
                       "grading_version_id": version.id},
        }
        db.commit()
        return grade_id, submission_id


@pytest.fixture()
def student_headers(client, db_session_factory):
    make_teacher(db_session_factory)
    make_teacher(db_session_factory, "appeal_other_t")
    make_student_fixture(db_session_factory)
    return _headers(client, "student")


def make_student_fixture(db_session_factory, username="student"):
    from tests.automated.conftest import make_student

    return make_student(db_session_factory, username)


# ── 异议窗口 ────────────────────────────────────────────────────


def test_window_open_on_day_seven_and_closed_on_day_eight(client, db_session_factory, student_headers):
    open_grade_id, open_submission_id = _simple_grade(db_session_factory, finished_days_ago=6.96)
    closed_grade_id, closed_submission_id = _simple_grade(
        db_session_factory, finished_days_ago=7.04)

    opened = client.post(API, headers=student_headers,
                         json={"target_kind": "submission", "target_id": open_submission_id,
                               "reason": REASON})
    assert opened.status_code == 201, opened.text
    assert opened.json()["status"] == "pending"
    assert opened.json()["window_expires_at"] is not None

    closed = client.post(API, headers=student_headers,
                         json={"target_kind": "submission", "target_id": closed_submission_id,
                               "reason": REASON})
    assert closed.status_code == 403, closed.text
    assert closed.json()["detail"]["code"] == "APPEAL_WINDOW_CLOSED"
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GradeAppeal)) == 1, \
            "窗口外的申请不得落库"
        assert db.get(CodeGrade, closed_grade_id).status == "completed"


def test_reason_shorter_than_twenty_chars_is_rejected(client, db_session_factory, student_headers):
    _, submission_id = _simple_grade(db_session_factory)
    response = client.post(API, headers=student_headers,
                           json={"target_kind": "submission", "target_id": submission_id,
                                 "reason": "太短了"})
    assert response.status_code == 422


# ── 同版本一条 + 撤回后可再申请 ─────────────────────────────────


def test_duplicate_then_withdraw_allows_reapply(client, db_session_factory, student_headers):
    _, submission_id = _simple_grade(db_session_factory)
    payload = {"target_kind": "submission", "target_id": submission_id, "reason": REASON}

    first = client.post(API, headers=student_headers, json=payload)
    assert first.status_code == 201, first.text
    appeal_id = first.json()["id"]

    duplicate = client.post(API, headers=student_headers, json=payload)
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["detail"]["code"] == "APPEAL_DUPLICATE"
    assert duplicate.json()["detail"]["fields"]["existing_appeal_id"] == appeal_id

    withdrawn = client.post(f"{API}/{appeal_id}/withdraw", headers=student_headers)
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "withdrawn"
    with db_session_factory() as db:
        assert db.get(GradeAppeal, appeal_id).dedupe_key is None

    again = client.post(API, headers=student_headers, json=payload)
    assert again.status_code == 201, again.text
    assert again.json()["id"] != appeal_id
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GradeAppeal)) == 2


def test_five_appeals_per_day_then_rate_limited(client, db_session_factory, student_headers):
    for index in range(6):
        _, submission_id = _simple_grade(db_session_factory)
        response = client.post(API, headers=student_headers,
                               json={"target_kind": "submission", "target_id": submission_id,
                                     "reason": REASON})
        if index < 5:
            assert response.status_code == 201, response.text
        else:
            assert response.status_code == 429, response.text
            assert response.json()["detail"]["code"] == "APPEAL_DAILY_LIMITED"
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GradeAppeal)) == 5


# ── 归属与作用域隔离 ────────────────────────────────────────────


def test_student_cannot_appeal_another_students_result(client, db_session_factory, student_headers):
    make_student_fixture(db_session_factory, "other_student")
    _, submission_id = _simple_grade(db_session_factory, student_username="other_student")
    response = client.post(API, headers=student_headers,
                           json={"target_kind": "submission", "target_id": submission_id,
                                 "reason": REASON})
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "APPEAL_TARGET_NOT_FOUND"


def test_teacher_scope_isolation_and_own_appeals_visible(client, db_session_factory, student_headers):
    """教师 A 看不到也处理不了教师 B 的授课班的异议（对照组：B 能看到自己的）。"""
    _, own_submission = _simple_grade(db_session_factory, student_username="student")
    own = client.post(API, headers=student_headers,
                      json={"target_kind": "submission", "target_id": own_submission,
                            "reason": REASON})
    assert own.status_code == 201, own.text
    own_appeal_id = own.json()["id"]

    # 教师 B 的独立课程链：B 是 course.teacher_id，他人课程不共享授课关系
    make_teacher(db_session_factory, "appeal_other_t")
    make_student_fixture(db_session_factory, "b_student")
    other_assignment = make_assignment(db_session_factory, teacher_username="appeal_other_t",
                                       title="B 的作业", status="draft")
    other_question = make_judge_question(db_session_factory, assignment_id=other_assignment)
    other_submission = make_submission(db_session_factory, question_id=other_question,
                                       student_username="b_student", grading_status="completed")
    _rubric = make_rubric(db_session_factory, judge_question_id=other_question, status="locked")
    with db_session_factory() as db:
        grade = CodeGrade(submission_id=other_submission, rubric_id=_rubric, mode="active",
                          status="completed", functional_score=40.0, robustness_score=5.0,
                          final_score_100=45.0, finished_at=datetime.now(timezone.utc))
        db.add(grade)
        db.commit()

    other_student_headers = _headers(client, "b_student")
    created = client.post(API, headers=other_student_headers,
                          json={"target_kind": "submission", "target_id": other_submission,
                                "reason": REASON})
    assert created.status_code == 201, created.text
    other_appeal_id = created.json()["id"]

    teacher_a_headers = _headers(client, "teacher")
    listed = client.get(API, headers=teacher_a_headers, params={"status": "pending"})
    assert listed.status_code == 200, listed.text
    ids = {item["id"] for item in listed.json()["items"]}
    assert own_appeal_id in ids
    assert other_appeal_id not in ids, "教师 A 不得看到教师 B 授课班的异议"

    forbidden = client.post(f"{API}/{other_appeal_id}/handle", headers=teacher_a_headers,
                            json={"action": "uphold", "comment": "越权处理"})
    assert forbidden.status_code == 403, forbidden.text
    assert forbidden.json()["detail"]["code"] == "FORBIDDEN"
    with db_session_factory() as db:
        assert db.get(GradeAppeal, other_appeal_id).status == "pending"

    # 对照组：教师 B 能看到并处理自己的那条
    teacher_b_headers = _headers(client, "appeal_other_t")
    listed_b = client.get(API, headers=teacher_b_headers, params={"status": "pending"})
    assert {item["id"] for item in listed_b.json()["items"]} == {other_appeal_id}

    handled = client.post(f"{API}/{other_appeal_id}/handle", headers=teacher_b_headers,
                          json={"action": "uphold", "comment": "已复核，原分无误"})
    assert handled.status_code == 200, handled.text
    assert handled.json()["status"] == "upheld"


# ── 改分：必须复用既有 override 通道 ─────────────────────────────


def test_adjust_reuses_override_path_and_fences_stale_ai_write(
    client, db_session_factory, student_headers
):
    grade_id, submission_id = _simple_grade(db_session_factory, claim_token="old-claim")
    control_grade_id, _ = _simple_grade(db_session_factory, student_username="student2",
                                        cg_status="running", claim_token="old-claim")

    created = client.post(API, headers=student_headers,
                          json={"target_kind": "submission", "target_id": submission_id,
                                "reason": REASON})
    assert created.status_code == 201, created.text
    appeal_id = created.json()["id"]
    with db_session_factory() as db:
        revision_before = db.get(CodeGrade, grade_id).result_revision

    headers = _headers(client, "teacher")
    handled = client.post(f"{API}/{appeal_id}/handle", headers=headers,
                          json={"action": "adjust", "comment": "复核后调整总分",
                                "final_score_100": 88.0})
    assert handled.status_code == 200, handled.text
    assert handled.json()["status"] == "adjusted"
    assert handled.json()["score_after"] == 88.0

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.final_score_100 == 88.0
        assert grade.claim_token is None, "改分必须清空认领"
        assert grade.result_revision == (revision_before or 0) + 1, \
            "改分必须递增 CodeGrade.result_revision"
        assert db.get(Submission, submission_id).score == 88.0
        assert db.scalar(select(func.count()).select_from(GradeOverride).where(
            GradeOverride.code_grade_id == grade_id)) == 1
        assert db.get(GradeAppeal, appeal_id).score_before == 55.0

    # 对照组：认领未被清空时同一条 AI 写入必须成功（证明栅栏本身放行合法 worker）
    values = {"status": "completed", "final_score_100": 99.0}
    with db_session_factory() as db:
        db.info.setdefault("ai_claims", {})[control_grade_id] = "old-claim"
        assert finish_ai_grade(db, control_grade_id, dict(values)) is True
    # 被改分清空认领后，晚到的旧 worker 写入必须被拒
    with db_session_factory() as db:
        db.info.setdefault("ai_claims", {})[grade_id] = "old-claim"
        assert finish_ai_grade(db, grade_id, dict(values)) is False
    with db_session_factory() as db:
        assert db.get(CodeGrade, grade_id).final_score_100 == 88.0, \
            "旧 worker 不得覆盖人工改分"


def test_adjust_requires_score_and_comment(client, db_session_factory, student_headers):
    _, submission_id = _simple_grade(db_session_factory)
    appeal_id = client.post(API, headers=student_headers,
                            json={"target_kind": "submission", "target_id": submission_id,
                                  "reason": REASON}).json()["id"]
    headers = _headers(client, "teacher")
    no_score = client.post(f"{API}/{appeal_id}/handle", headers=headers,
                           json={"action": "adjust", "comment": "改分"})
    assert no_score.status_code == 422
    no_comment = client.post(f"{API}/{appeal_id}/handle", headers=headers,
                             json={"action": "adjust", "final_score_100": 70})
    assert no_comment.status_code == 422
    assert no_comment.json()["detail"]["code"] == "APPEAL_COMMENT_REQUIRED"
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GradeOverride)) == 0


# ── 重评：完成前不关闭，终态按分数变化转 adjusted / upheld ───────


def test_regrade_stays_open_then_resolves_by_score_change(
    client, db_session_factory, student_headers
):
    grade_id, submission_id = _versioned_grade(db_session_factory)
    appeal_id = client.post(API, headers=student_headers,
                            json={"target_kind": "submission", "target_id": submission_id,
                                  "reason": REASON}).json()["id"]
    headers = _headers(client, "teacher")

    started = client.post(f"{API}/{appeal_id}/handle", headers=headers,
                          json={"action": "regrade", "comment": "同意重评，重新跑一次 AI"})
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "regrading"

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.status in ("pending", "queued"), "重评必须走既有 retry 重置通道"
        assert grade.claim_token is None
    # 重评完成之前不得关闭处理流程（学生端仍看到处理中）
    mine = client.get(f"{API}/mine", headers=student_headers)
    assert mine.json()["items"][0]["status"] == "regrading"

    # 模拟 AI 重评完成：分数变化 → adjusted
    with db_session_factory() as db:
        db.info.setdefault("ai_claims", {})[grade_id] = "regrade-claim"
        grade = db.get(CodeGrade, grade_id)
        grade.status = "running"
        grade.claim_token = "regrade-claim"
        db.commit()
    with db_session_factory() as db:
        db.info.setdefault("ai_claims", {})[grade_id] = "regrade-claim"
        assert finish_ai_grade(db, grade_id, {"status": "completed", "final_score_100": 70.0}) is True

    with db_session_factory() as db:
        appeal = db.get(GradeAppeal, appeal_id)
        assert appeal.status == "adjusted", "分数变化必须转 adjusted"
        assert appeal.score_after == 70.0
        assert appeal.resolved_at is not None


def test_regrade_unchanged_score_resolves_as_upheld(
    client, db_session_factory, student_headers
):
    grade_id, submission_id = _versioned_grade(db_session_factory)
    created = client.post(API, headers=student_headers,
                          json={"target_kind": "submission", "target_id": submission_id,
                                "reason": REASON})
    assert created.status_code == 201, created.text
    appeal_id = created.json()["id"]
    headers = _headers(client, "teacher")
    assert client.post(f"{API}/{appeal_id}/handle", headers=headers,
                       json={"action": "regrade", "comment": "重评一次确认"}).status_code == 200

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        grade.status = "running"
        grade.claim_token = "regrade-claim-2"
        db.commit()
    with db_session_factory() as db:
        db.info.setdefault("ai_claims", {})[grade_id] = "regrade-claim-2"
        assert finish_ai_grade(db, grade_id, {"status": "completed", "final_score_100": 55.0}) is True

    with db_session_factory() as db:
        appeal = db.get(GradeAppeal, appeal_id)
        assert appeal.status == "upheld", "分数未变必须转 upheld"
        assert appeal.score_after == 55.0


def test_regrade_does_not_close_when_ai_write_is_review_required(
    client, db_session_factory, student_headers
):
    """重评失败（review_required）时保持 regrading，绝不提前关闭处理流程。"""
    grade_id, submission_id = _versioned_grade(db_session_factory)
    created = client.post(API, headers=student_headers,
                          json={"target_kind": "submission", "target_id": submission_id,
                                "reason": REASON})
    assert created.status_code == 201, created.text
    appeal_id = created.json()["id"]
    headers = _headers(client, "teacher")
    assert client.post(f"{API}/{appeal_id}/handle", headers=headers,
                       json={"action": "regrade", "comment": "重评"}).status_code == 200

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        grade.status = "running"
        grade.claim_token = "regrade-claim-3"
        db.commit()
    with db_session_factory() as db:
        db.info.setdefault("ai_claims", {})[grade_id] = "regrade-claim-3"
        assert finish_ai_grade(db, grade_id, {
            "status": "review_required", "needs_teacher_review": True,
            "review_reason": "AI 输出不可用",
        }) is True

    with db_session_factory() as db:
        assert db.get(GradeAppeal, appeal_id).status == "regrading"


# ── 考试编程题与实验目标 ────────────────────────────────────────


def test_exam_answer_appeal_can_be_created_and_upheld(client, db_session_factory, student_headers):
    make_teacher(db_session_factory)
    chain = make_exam_chain(db_session_factory)
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, chain["answer_id"])
        answer.grading_status = "completed"
        answer.score = 6.0
        rubric = QuestionRubric(exam_question_id=chain["question_id"], version=1, status="locked",
                                source_hash="a" * 64, source_snapshot={}, rubric_json={},
                                model_name="m")
        db.add(rubric)
        db.flush()
        grade = CodeGrade(exam_answer_id=answer.id, rubric_id=rubric.id, mode="active",
                          status="completed", functional_score=50.0, robustness_score=5.0,
                          final_score_100=60.0, scaled_score=6.0,
                          finished_at=datetime.now(timezone.utc))
        db.add(grade)
        db.commit()
        grade_id = grade.id

    created = client.post(API, headers=student_headers,
                          json={"target_kind": "exam_answer", "target_id": chain["answer_id"],
                                "reason": REASON})
    assert created.status_code == 201, created.text
    assert created.json()["target_kind"] == "exam_answer"
    assert created.json()["route"].startswith("/student/exams/")

    headers = _headers(client, "teacher")
    handled = client.post(f"{API}/{created.json()['id']}/handle", headers=headers,
                          json={"action": "adjust", "comment": "复核考试编程题",
                                "final_score_100": 90.0})
    assert handled.status_code == 200, handled.text
    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.final_score_100 == 90.0
        assert grade.scaled_score == 9.0, "考试改分必须按题目分值同步折算"
        assert db.get(ExamAnswer, chain["answer_id"]).score == 9.0


def test_unscored_result_cannot_be_appealed(client, db_session_factory, student_headers):
    _, submission_id = _simple_grade(db_session_factory, mode="shadow", cg_status="completed")
    response = client.post(API, headers=student_headers,
                           json={"target_kind": "submission", "target_id": submission_id,
                                 "reason": REASON})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "APPEAL_NOT_SCORED"


# ── 审计 ────────────────────────────────────────────────────────


def test_each_transition_writes_audit_event(client, db_session_factory, student_headers):
    _, submission_id = _simple_grade(db_session_factory)
    appeal_id = client.post(API, headers=student_headers,
                            json={"target_kind": "submission", "target_id": submission_id,
                                  "reason": REASON}).json()["id"]
    headers = _headers(client, "teacher")
    assert client.post(f"{API}/{appeal_id}/handle", headers=headers,
                       json={"action": "uphold", "comment": "已复核，维持原分"}).status_code == 200
    with db_session_factory() as db:
        actions = set(db.scalars(select(AuditEvent.action).where(
            AuditEvent.target_type == "grade_appeal", AuditEvent.target_id == str(appeal_id))).all())
    assert {"appeal.created", "appeal.upheld"} <= actions


def test_version_functions_keep_existing_commit_behaviour(db_session_factory):
    """commit 参数默认值必须与历史行为一致：三个版本函数默认只 flush，不提交。"""
    make_teacher(db_session_factory)
    question_id = make_judge_question(db_session_factory)
    rubric_id = make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    with db_session_factory() as db:
        version = versions.create_draft(
            db, question_kind="assignment", question_id=question_id,
            payload={"binary_cases": [{"id": "c1", "input": {"t": "int", "v": 1},
                                       "expected": {"t": "int", "v": 1}}]},
            entry_function="solve", rubric_id=rubric_id, grading_mode_snapshot="legacy",
        )
        version_id = version.id
        db.rollback()
    with db_session_factory() as db:
        assert db.get(JudgeGradingVersion, version_id) is None, \
            "默认 commit=False 时调用方不提交，草稿不得落库（与历史行为一致）"
