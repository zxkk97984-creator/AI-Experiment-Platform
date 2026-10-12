"""S-2 回归：教师覆盖 AI 评分必须与在途判题/AI 写入互斥。

修复前 ``override_grade`` 直接 ``db.get(CodeGrade)`` 后裸赋值——不加行锁、不检查在途
状态、不清 ``claim_token``、不递增 ``result_revision``。shadow 模式下 AI 终态写入的
``target_revision_guard`` 为 None，只剩 CodeGrade 认领栅栏，而它依旧满足
（``status=='running'`` 且 ``claim_token`` 匹配），于是晚到的 AI 结果会静默回退教师改分，
只在 ``grade_overrides`` 留一行审计。

两道防线，与 judge.py 人工最终分（A03）保持同一套不变量：
1. **在途即拒**——target 与 CodeGrade 任一在途都返回 409，这是主防线；
2. **清空认领**——覆盖成功后旧 token 的条件 UPDATE 必然落空，兜住锁窗口外的残留代次。
"""
from __future__ import annotations

import pytest

from app.models import CodeGrade, Submission
from app.services.ai_grading_queue import finish_ai_grade
from tests.automated.conftest import (
    auth_header,
    login,
    make_rubric,
    make_submission,
    make_teacher,
)

OVERRIDE_URL = "/api/v1/ai-grading/grades/{grade_id}/override"


@pytest.fixture
def teacher_headers(client, db_session_factory):
    # make_submission 级联建课程时使用的默认授课教师即 username="teacher"，
    # 必须以它的身份请求，否则先被 _check_grade_permission 挡成 404。
    make_teacher(db_session_factory)
    token, _ = login(client, "teacher")
    return auth_header(token)


def _make_grade(db_session_factory, *, cg_status="completed", grading_status="completed",
                claim_token=None, result_details=None, mode="active"):
    """建一条带确定性分的 CodeGrade（默认 active + 已终态，即正常覆盖场景）。"""
    submission_id = make_submission(
        db_session_factory, grading_status=grading_status, score=10.0,
        result_details=result_details if result_details is not None else {},
    )
    with db_session_factory() as db:
        question_id = db.get(Submission, submission_id).question_id
    rubric_id = make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    with db_session_factory() as db:
        grade = CodeGrade(
            submission_id=submission_id, rubric_id=rubric_id, mode=mode, status=cg_status,
            functional_score=50.0, robustness_score=5.0,
            algorithm_score=15.0, quality_score=8.0, claim_token=claim_token,
            # 有确定性判题结果，override 才允许只改 A/Q 而不显式给 F/R
            deterministic_details={"groups": [{"id": "F1", "dimension": "F"}]},
        )
        db.add(grade)
        db.commit()
        return grade.id, submission_id


def _override(client, teacher_headers, grade_id, payload):
    return client.post(OVERRIDE_URL.format(grade_id=grade_id),
                       headers=teacher_headers, json=payload)


# ── 防线 1：在途即拒 ─────────────────────────────────────────────────


@pytest.mark.parametrize("cg_status", ["pending", "queued", "running"])
def test_rejected_while_ai_grade_in_flight(client, db_session_factory, teacher_headers, cg_status):
    """AI 评分在途时覆盖必须 409——否则它持有的认领会晚到并覆盖教师改分。"""
    grade_id, _ = _make_grade(db_session_factory, cg_status=cg_status, claim_token="in-flight")

    resp = _override(client, teacher_headers, grade_id,
                     {"final_score_100": 90, "reason": "教师复核"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "AI_GRADING_IN_PROGRESS"
    with db_session_factory() as db:
        assert db.get(CodeGrade, grade_id).final_score_100 is None, "被拒时不得写入任何分数"


@pytest.mark.parametrize("grading_status", ["queued", "running"])
def test_rejected_while_judge_in_flight(client, db_session_factory, teacher_headers, grading_status):
    """主判题在途时覆盖必须 409（对齐 judge.py 的 A03 不变量）。"""
    grade_id, submission_id = _make_grade(db_session_factory, grading_status=grading_status)

    resp = _override(client, teacher_headers, grade_id,
                     {"final_score_100": 90, "reason": "教师复核"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "GRADING_IN_PROGRESS"
    with db_session_factory() as db:
        assert db.get(Submission, submission_id).score == 10.0, "被拒时正式分不得变动"


def test_rejected_after_manual_score_final(client, db_session_factory, teacher_headers):
    """人工最终分已生效后不得再走覆盖通道——它才是权威且带 CAS 审计的路径。"""
    grade_id, submission_id = _make_grade(
        db_session_factory,
        result_details={"manual_review": {"score": 88.0, "reason": "教师人工核定"}},
    )

    resp = _override(client, teacher_headers, grade_id,
                     {"final_score_100": 90, "reason": "教师复核"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MANUAL_SCORE_FINAL"
    with db_session_factory() as db:
        assert db.get(Submission, submission_id).score == 10.0


def test_rejected_after_exam_answer_manual_score(client, db_session_factory):
    """考试侧以 manual_score_at/manual_score_reason 为人工最终分判据。"""
    from datetime import datetime, timezone

    from app.models import (
        Course, Exam, ExamAnswer, ExamQuestion, ExamSubmission, QuestionRubric,
    )
    from tests.automated.conftest import create_user, grant_teaching_fixture

    with db_session_factory() as db:
        teacher = create_user(db_session_factory, "ovr_exam_t", "teacher")
        student = create_user(db_session_factory, "ovr_exam_s", "student")
        course = Course(title="OVR course", status="published", teacher_id=teacher.id)
        db.add(course); db.flush()
        exam = Exam(course_id=course.id, title="OVR exam", status="published", duration_minutes=60)
        db.add(exam); db.flush()
        eq = ExamQuestion(exam_id=exam.id, question_type="code", prompt="t",
                          correct_answer={"test_file": ""}, points=10, grading_mode="active")
        db.add(eq); db.flush()
        es = ExamSubmission(exam_id=exam.id, student_id=student.id, status="grading")
        db.add(es); db.flush()
        ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="x",
                        grading_status="completed", score=5,
                        manual_score_at=datetime.now(timezone.utc),
                        manual_score_reason="教师人工核定")
        db.add(ea); db.flush()
        rub = QuestionRubric(exam_question_id=eq.id, version=1, status="locked",
                             source_hash="a", source_snapshot={}, rubric_json={},
                             model_name="m")
        db.add(rub); db.flush()
        cg = CodeGrade(exam_answer_id=ea.id, rubric_id=rub.id, mode="active",
                       status="review_required", functional_score=60, robustness_score=10,
                       algorithm_score=15, quality_score=5)
        db.add(cg); db.flush()
        grant_teaching_fixture(db, course, [student.id])
        db.commit()
        grade_id = cg.id

    token, _ = login(client, "ovr_exam_t")
    resp = _override(client, auth_header(token), grade_id,
                     {"final_score_100": 90, "reason": "教师复核"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MANUAL_SCORE_FINAL"


# ── 防线 2：清空认领，兜住晚到写入 ───────────────────────────────────


def test_override_clears_claim_and_bumps_revision(client, db_session_factory, teacher_headers):
    """覆盖成功后不得残留认领，且 result_revision 递增以便客户端识别过时结果。"""
    grade_id, _ = _make_grade(db_session_factory, claim_token="stale-claim")
    with db_session_factory() as db:
        before = db.get(CodeGrade, grade_id).result_revision

    resp = _override(client, teacher_headers, grade_id,
                     {"final_score_100": 90, "reason": "教师复核"})
    assert resp.status_code == 200, resp.text

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.claim_token is None
        assert grade.result_revision == (before or 0) + 1


def test_override_syncs_official_score(client, db_session_factory, teacher_headers):
    """覆盖成功后 CodeGrade 与正式分一致，且旧认领不残留。"""
    grade_id, submission_id = _make_grade(db_session_factory, claim_token="stale-claim")

    resp = _override(client, teacher_headers, grade_id,
                     {"final_score_100": 90, "reason": "教师复核"})
    assert resp.status_code == 200, resp.text

    with db_session_factory() as db:
        assert db.get(CodeGrade, grade_id).final_score_100 == 90
        assert db.get(Submission, submission_id).score == 90


def test_cleared_claim_fences_off_late_ai_write(db_session_factory):
    """防线 2 单元验证：认领被清空后，晚到的 AI 终态写入必须落空。

    用两条记录对照，证明断言不是空过——
    * 仍持有认领 → 写入成功（栅栏本身放行合法 worker）；
    * 认领已被覆盖动作清空 → 同一条写入被拒。
    """
    held_id, _ = _make_grade(db_session_factory, cg_status="running", claim_token="stale-claim")
    cleared_id, _ = _make_grade(db_session_factory, cg_status="running", claim_token=None)

    values = {"status": "completed", "final_score_100": 10.0,
              "algorithm_score": 0.0, "quality_score": 0.0}

    with db_session_factory() as db:
        db.info.setdefault("ai_claims", {})[held_id] = "stale-claim"
        assert finish_ai_grade(db, held_id, dict(values)) is True, \
            "持有认领的写入应当成功，否则本对照无意义"

    with db_session_factory() as db:
        db.info.setdefault("ai_claims", {})[cleared_id] = "stale-claim"
        assert finish_ai_grade(db, cleared_id, dict(values)) is False, \
            "认领已清空，旧代次写入必须被栅栏拒绝"

    with db_session_factory() as db:
        cleared = db.get(CodeGrade, cleared_id)
        assert cleared.status == "running", "被拒的写入不得留下任何终态痕迹"
        assert cleared.final_score_100 is None
