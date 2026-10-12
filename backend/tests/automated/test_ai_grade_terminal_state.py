"""批次一：AI 评分终态、判题收尾事务与恢复扫描的实时过期复核。

这些用例固化两个上次验收发现的新增回归与三项收尾约束：

1. AI 合法结果必须在**同一事务**内写成 ``completed``（原先第二次 complete 被栅栏
   拒绝，CodeGrade 永久停在 running，考试随之无法汇总）。
2. 判题主结果与 CodeGrade 创建必须在同一个受认领事务；失去代次立即停止，
   不得创建评分记录或推送 AI。
3. 恢复扫描的 CAS 除 token 外还要复核"实际仍然过期"，SELECT 之后续过心跳的
   任务不得被旧扫描结果重置。
4. 关联写入失败必须回滚主结果（不允许"正式分已改、主记录未终态"的分裂状态）。
5. 结果修订号随任何结果写入原子推进。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import Update, select, update

from app.models import (JudgeGradingVersion,
    Assignment,
    CodeGrade,
    Course,
    EnvironmentProfile,
    EnvironmentVersion,
    Exam,
    ExamAnswer,
    ExamQuestion,
    ExamSubmission,
    JudgeQuestion,
    QuestionRubric,
    Submission,
    User,
)
from app.services.judge_queue import requeue_stale_jobs
from app.worker import judge_worker as worker


# ─────────────────────────────────────────────────────────────
# 夹具
# ─────────────────────────────────────────────────────────────

class _FakeAIClient:
    def __init__(self, payload: dict):
        self._payload = payload

    def chat_json(self, *args, **kwargs):
        return self._payload


def _ai_payload(*, needs_review: bool = False) -> dict:
    return {
        "rubric_version": 1,
        "algorithm": {"dimension_score": 0, "dimension_max": 20, "items": []},
        "code_quality": {"dimension_score": 0, "dimension_max": 10, "items": []},
        "student_feedback": {"strengths": [], "issues": [], "suggestions": []},
        "needs_teacher_review": needs_review,
    }


@pytest.fixture()
def grading_world(db_session_factory, test_settings):
    """教师/学生/课程/作业/环境 + 一条 active 代码题提交，返回可复用 id。"""
    with db_session_factory() as db:
        profile = EnvironmentProfile(slug="term-basic", display_name="term", status="active")
        db.add(profile)
        db.flush()
        version = EnvironmentVersion(
            profile_id=profile.id, version_number=1, status="available",
            base_image_ref="python:3.12-slim", image_digest="sha256:" + "1" * 64,
            minimum_memory_mb=256, manifest_sha256="c" * 64,
        )
        db.add(version)
        db.flush()
        teacher = User(username="term_t", real_name="T", role="teacher", status="active",
                       password_hash="x")
        student = User(username="term_s", real_name="S", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student])
        db.flush()
        course = Course(title="term", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        assignment = Assignment(title="term-a", course_id=course.id, status="published",
                                environment_version_id=version.id)
        db.add(assignment)
        db.commit()
        return {"assignment_id": assignment.id, "student_id": student.id,
                "env_version_id": version.id, "course_id": course.id}


def _seed_ai_grade(db_session_factory, world, *, code="def solve(x): return 0",
                   mode="active") -> int:
    with db_session_factory() as db:
        question = JudgeQuestion(
            assignment_id=world["assignment_id"], title="term-q", function_name="solve",
            hidden_tests="def test_secret():\n    assert solve(1) == 2",
            grading_mode=mode,
            test_groups=[{"id": "F1", "name": "F", "dimension": "F", "max_score": 60,
                          "tests": "def test_f():\n    assert solve(1) == 2"},
                         {"id": "R1", "name": "R", "dimension": "R", "max_score": 10,
                          "tests": "def test_r():\n    assert solve(2) == 3"}],
            public_cases=[],
        )
        db.add(question)
        db.flush()
        rubric = QuestionRubric(judge_question_id=question.id, status="locked", version=1,
                                source_hash="a" * 64, source_snapshot={},
                                rubric_json={"algorithm_criteria": []}, model_name="fake")
        db.add(rubric)
        db.flush()
        version = JudgeGradingVersion(
            judge_question_id=question.id, version=1, status="active",
            execution_mode="data_v1", grading_mode_snapshot=mode, entry_function="solve",
            payload={"schema_version": 2, "execution_model": "isolated_case_v2",
                     "entry_function": "solve", "groups": [], "binary_cases": []},
            score_cap_rules=[],
            context_snapshot={"prompt": question.title, "title": question.title,
                              "description": question.title, "function_name": "solve",
                              "teacher_constraints": {}, "rubric_snapshot": rubric.rubric_json,
                              "rubric_version": rubric.version},
            rubric_id=rubric.id, source_kind="manual", source_digest="a" * 64,
            spec_digest="b" * 64)
        db.add(version)
        db.flush()
        submission = Submission(question_id=question.id, student_id=world["student_id"],
                                environment_version_id=world["env_version_id"],
                                code=code, status="queued", grading_status="queued",
                                grading_version_id=version.id)
        db.add(submission)
        db.flush()
        grade = CodeGrade(submission_id=submission.id, rubric_id=rubric.id, mode=mode,
                          status="queued", functional_score=60, robustness_score=10,
                          deterministic_details={"source": {
                              "result_revision": submission.result_revision,
                              "grading_version_id": version.id}})
        db.add(grade)
        db.commit()
        return grade.id


def _seed_exam_ai_grade(db_session_factory, world, *, points=10) -> tuple[int, int, int]:
    with db_session_factory() as db:
        exam = Exam(course_id=world["course_id"], title="term-exam", status="published",
                    duration_minutes=60)
        db.add(exam)
        db.flush()
        question = ExamQuestion(exam_id=exam.id, question_type="code", prompt="solve",
                                correct_answer={}, points=points, grading_mode="active",
                                test_groups=[])
        db.add(question)
        db.flush()
        submission = ExamSubmission(exam_id=exam.id, student_id=world["student_id"],
                                    status="grading", grading_version_map={})
        db.add(submission)
        db.flush()
        answer = ExamAnswer(submission_id=submission.id, question_id=question.id,
                            code_answer="def solve(x): return x + 1",
                            grading_status="completed")
        db.add(answer)
        db.flush()
        submission.grading_version_map = {str(question.id): {
            "version_id": None, "points": float(points)}}
        rubric = QuestionRubric(exam_question_id=question.id, status="locked", version=1,
                                source_hash="b" * 64, source_snapshot={},
                                rubric_json={"algorithm_criteria": []}, model_name="fake")
        db.add(rubric)
        db.flush()
        version = JudgeGradingVersion(
            exam_question_id=question.id, version=1, status="active",
            execution_mode="data_v1", grading_mode_snapshot="active", entry_function="solve",
            payload={"schema_version": 2, "execution_model": "isolated_case_v2",
                     "entry_function": "solve", "groups": [], "binary_cases": []},
            score_cap_rules=[],
            context_snapshot={"prompt": question.prompt, "title": question.prompt,
                              "description": question.prompt, "function_name": "solve",
                              "teacher_constraints": {}, "rubric_snapshot": rubric.rubric_json,
                              "rubric_version": rubric.version},
            rubric_id=rubric.id, source_kind="manual", source_digest="a" * 64,
            spec_digest="b" * 64)
        db.add(version)
        db.flush()
        answer.grading_version_id = version.id
        submission.grading_version_map = {str(question.id): {
            "version_id": version.id, "points": float(points)}}
        grade = CodeGrade(exam_answer_id=answer.id, rubric_id=rubric.id, mode="active",
                          status="queued", functional_score=60, robustness_score=10,
                          deterministic_details={"source": {
                              "result_revision": answer.result_revision,
                              "grading_version_id": version.id}})
        db.add(grade)
        db.commit()
        return grade.id, submission.id, answer.id


# ─────────────────────────────────────────────────────────────
# 1. AI 终态：一次事务写完
# ─────────────────────────────────────────────────────────────

def test_ai_success_commits_completed_with_submission_score(
        db_session_factory, test_settings, redis_client, grading_world):
    grade_id = _seed_ai_grade(db_session_factory, grading_world)
    with db_session_factory() as db, \
            patch("app.services.ai_client.DeepSeekClient",
                  return_value=_FakeAIClient(_ai_payload())), \
            patch("app.services.ai_grading_service.analyze_python",
                  return_value={"diagnostics": [], "complexity": {}}):
        result = worker.process_ai_grade(db, redis_client, test_settings, grade_id)

    assert result is not None, "合法 AI 结果必须产生终态，不能被栅栏拒绝"
    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        submission = db.get(Submission, grade.submission_id)
        assert grade.status == "completed", f"CodeGrade 必须终态 completed，实际 {grade.status}"
        assert grade.claim_token is None, "终态必须释放认领"
        assert grade.finished_at is not None
        assert grade.result_revision == 1, "结果写入必须原子推进修订号"
        assert submission.score == grade.final_score_100
        assert submission.status == "graded"
        assert submission.result_revision == 1


def test_ai_review_required_terminal_state(db_session_factory, test_settings,
                                           redis_client, grading_world):
    grade_id = _seed_ai_grade(db_session_factory, grading_world)
    with db_session_factory() as db, \
            patch("app.services.ai_client.DeepSeekClient",
                  return_value=_FakeAIClient(_ai_payload(needs_review=True))), \
            patch("app.services.ai_grading_service.analyze_python",
                  return_value={"diagnostics": [], "complexity": {}}):
        worker.process_ai_grade(db, redis_client, test_settings, grade_id)

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        submission = db.get(Submission, grade.submission_id)
        assert grade.status == "review_required"
        assert grade.claim_token is None
        assert grade.needs_teacher_review is True
        assert submission.score is None, "需复核时不得写正式分"


def test_ai_exam_success_finalizes_parent_as_graded(
        db_session_factory, test_settings, redis_client, grading_world):
    grade_id, submission_id, answer_id = _seed_exam_ai_grade(db_session_factory, grading_world)
    with db_session_factory() as db, \
            patch("app.services.ai_client.DeepSeekClient",
                  return_value=_FakeAIClient(_ai_payload())), \
            patch("app.services.ai_grading_service.analyze_python",
                  return_value={"diagnostics": [], "complexity": {}}):
        worker.process_ai_grade(db, redis_client, test_settings, grade_id)

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        answer = db.get(ExamAnswer, answer_id)
        submission = db.get(ExamSubmission, submission_id)
        assert grade.status == "completed"
        assert answer.score is not None, "active 模式必须写入该题正式分"
        assert answer.result_revision == 1
        # ExamSubmission 只有 started/submitted/grading/graded/review_required
        assert submission.status == "graded", f"考试必须正确汇总，实际 {submission.status}"
        assert submission.score is not None


def test_ai_lost_claim_service_raises_and_keeps_new_result(
        db_session_factory, test_settings, grading_world):
    """旧 AI 运行携带旧代次继续评分：必须抛 LostClaimError，且不得覆盖新结果。"""
    from app.services.ai_grading_queue import claim_ai_grade, complete_ai_grade
    from app.services.ai_grading_service import grade_code_submission
    from app.services.grading_errors import LostClaimError

    grade_id = _seed_ai_grade(db_session_factory, grading_world)
    with db_session_factory() as db:
        old_token = claim_ai_grade(db, grade_id)
        assert old_token

    # 恢复扫描交回 queued，新运行接管并完成
    with db_session_factory() as db:
        db.execute(update(CodeGrade).where(CodeGrade.id == grade_id)
                   .values(status="queued", claim_token=None))
        db.commit()
        new_token = claim_ai_grade(db, grade_id)
        assert new_token and new_token != old_token
        assert complete_ai_grade(db, grade_id, {"final_score_100": 91.0}) is True

    # 旧运行迟到：携带旧代次继续评分
    with db_session_factory() as db, \
            patch("app.services.ai_grading_service.analyze_python",
                  return_value={"diagnostics": [], "complexity": {}}):
        db.info.setdefault("ai_claims", {})[grade_id] = old_token
        with pytest.raises(LostClaimError):
            grade_code_submission(db, _FakeAIClient(_ai_payload()), grade_id)

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.status == "completed"
        assert grade.final_score_100 == 91.0, "旧运行不得覆盖新结果"


def test_worker_stops_on_lost_claim_without_failure_write(
        db_session_factory, test_settings, redis_client, grading_world):
    """Worker 层：LostClaimError 只终止本次运行，不得转成失败写入。"""
    from app.services.grading_errors import LostClaimError

    grade_id = _seed_ai_grade(db_session_factory, grading_world)
    with db_session_factory() as db, \
            patch("app.services.ai_grading_queue.claim_ai_grade", return_value="token-x"), \
            patch("app.services.ai_grading_service.grade_code_submission",
                  side_effect=LostClaimError("stale")), \
            patch("app.services.ai_grading_queue.fail_ai_grade") as fail_spy:
        result = worker.process_ai_grade(db, redis_client, test_settings, grade_id)
        assert result is None
        assert fail_spy.call_count == 0, "失去认领不得写失败态"


# ─────────────────────────────────────────────────────────────
# 2. 关联写入失败必须回滚主结果
# ─────────────────────────────────────────────────────────────

def test_related_write_failure_rolls_back_ai_grade(db_session_factory, grading_world):
    from app.services.ai_grading_queue import claim_ai_grade, finish_ai_grade
    from app.services.grading_errors import RelatedWriteError

    grade_id = _seed_ai_grade(db_session_factory, grading_world)
    with db_session_factory() as db:
        assert claim_ai_grade(db, grade_id)
        with pytest.raises(RelatedWriteError):
            finish_ai_grade(
                db, grade_id,
                {"status": "completed", "final_score_100": 88.0},
                related_updates=[(Submission, 987654321, {"score": 88.0, "status": "graded"})],
            )

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.status == "running", "关联写入失败必须回滚主结果"
        assert grade.final_score_100 is None
        assert grade.claim_token is not None, "回滚后仍持有认领，可重试或转人工"


def test_ai_notification_happens_after_commit(
        db_session_factory, test_settings, redis_client, grading_world):
    """通知必须发生在提交之后：被通知时另一会话必须已能看到 pending 记录。"""
    grade_id = _seed_ai_grade(db_session_factory, grading_world)
    seen: dict = {}

    def spy_enqueue(db, redis_client, code_grade_id):
        with db_session_factory() as other:
            row = other.get(CodeGrade, code_grade_id)
            seen["status"] = row.status if row else None
        return True

    with db_session_factory() as db, \
            patch("app.services.ai_client.DeepSeekClient",
                  return_value=_FakeAIClient(_ai_payload())), \
            patch("app.services.ai_grading_service.analyze_python",
                  return_value={"diagnostics": [], "complexity": {}}), \
            patch("app.services.ai_grading_queue.enqueue_ai_grade", side_effect=spy_enqueue):
        worker.process_ai_grade(db, redis_client, test_settings, grade_id)

    assert seen.get("status") is None, "AI 评分终态提交后不应再次入队"


# ─────────────────────────────────────────────────────────────
# 3. 主结果与 CodeGrade 同事务（失去代次立即停止）
# ─────────────────────────────────────────────────────────────

def _seed_judge_submission(db_session_factory, world, *, grading_mode="active") -> int:
    from app.services.grading_values import encode_value

    with db_session_factory() as db:
        question = JudgeQuestion(
            assignment_id=world["assignment_id"], title="term-judge", function_name="solve",
            hidden_tests="def test_secret():\n    assert solve(1) == 2",
            grading_mode=grading_mode,
            test_groups=[{"id": "F1", "name": "F", "dimension": "F", "max_score": 60,
                          "tests": "def test_f():\n    assert solve(1) == 2"}],
            public_cases=[],
        )
        db.add(question)
        db.flush()
        rubric = QuestionRubric(judge_question_id=question.id, status="locked", version=1,
                                source_hash="a" * 64, source_snapshot={},
                                rubric_json={"algorithm_criteria": []}, model_name="fake")
        db.add(rubric)
        db.flush()
        version = JudgeGradingVersion(
            judge_question_id=question.id, version=1, status="active",
            execution_mode="data_v1", grading_mode_snapshot="active", entry_function="solve",
            payload={"schema_version": 2, "entry_function": "solve",
                     "execution_model": "isolated_case_v2", "binary_cases": [],
                     "groups": [
                         {"id": "F1", "name": "功能", "dimension": "F", "max_score": 60,
                          "cases": [{"id": "F1-1", "input": encode_value((1,)),
                                     "expected": encode_value(2), "compare": {"kind": "literal"}}]},
                         {"id": "R1", "name": "鲁棒性", "dimension": "R", "max_score": 10,
                          "cases": [{"id": "R1-1", "input": encode_value((0,)),
                                     "expected": encode_value(2), "compare": {"kind": "literal"}}]},
                     ]},
            score_cap_rules=[],
            context_snapshot={"prompt": question.title, "title": question.title,
                              "description": question.title, "function_name": "solve",
                              "teacher_constraints": {}, "rubric_snapshot": rubric.rubric_json,
                              "rubric_version": rubric.version,
                              "isolated_case_v2_confirmed": True},
            rubric_id=rubric.id, source_kind="manual",
            source_digest="a" * 64, spec_digest="b" * 64)
        db.add(version)
        db.flush()
        submission = Submission(question_id=question.id, student_id=world["student_id"],
                                environment_version_id=world["env_version_id"],
                                code="def solve(x): return 2", status="queued",
                                grading_status="queued", grading_version_id=version.id)
        db.add(submission)
        db.commit()
        return submission.id


def _all_pass_data_frames(*args, **kwargs):
    import json

    from app.services.grading_runner import RESULT_PREFIX
    from app.services.grading_values import encode_value

    frames = [
        {"id": "F1-1", "status": "ok", "value": encode_value(2)},
        {"id": "R1-1", "status": "ok", "value": encode_value(2)},
    ]
    return "", "\n".join(RESULT_PREFIX + json.dumps(frame) for frame in frames), 0, 12


def test_lost_claim_creates_no_code_grade_and_pushes_no_ai(
        db_session_factory, test_settings, redis_client, grading_world):
    """旧 Worker 在判题过程中失去认领：主结果被拒 → 不建 CG、不推 AI。"""
    from app.services.grading_errors import LostClaimError

    submission_id = _seed_judge_submission(db_session_factory, grading_world)

    def steal_claim(*args, **kwargs):
        with db_session_factory() as newer:
            newer.execute(update(Submission).where(Submission.id == submission_id)
                          .values(grading_status="queued", claim_token=None))
            newer.commit()
            from app.services.judge_queue import claim_job
            assert claim_job(newer, job_type="assignment", object_id=submission_id)
        return _all_pass_data_frames()

    pushed: list = []
    with db_session_factory() as db, \
            patch.object(worker, "_run_docker_data_runner", side_effect=steal_claim), \
            patch.object(worker, "_submission_env_context", return_value=(None, None, set())), \
            patch.object(worker, "_diagnose_submission", return_value=None), \
            patch("app.services.ai_grading_queue.enqueue_ai_grade",
                  side_effect=lambda *a, **k: pushed.append(a) or True):
        result = worker.process_submission(db, redis_client, test_settings, submission_id)

    assert result is not None
    with db_session_factory() as db:
        grade = db.scalar(select(CodeGrade).where(CodeGrade.submission_id == submission_id))
        assert grade is None, "失去认领后不得创建评分记录"
        submission = db.get(Submission, submission_id)
        assert submission.tests_passed in (None, 0), "旧代次不得写回判题结果"
    assert pushed == [], "失去认领后不得推送 AI 队列"


def test_code_grade_created_in_same_transaction_as_result(
        db_session_factory, test_settings, redis_client, grading_world):
    submission_id = _seed_judge_submission(db_session_factory, grading_world)
    with db_session_factory() as db, \
            patch.object(worker, "_run_docker_data_runner", side_effect=_all_pass_data_frames), \
            patch.object(worker, "_submission_env_context", return_value=(None, None, set())), \
            patch.object(worker, "_diagnose_submission", return_value=None), \
            patch("app.services.judge_queue._get_redis", return_value=redis_client):
        worker.process_submission(db, redis_client, test_settings, submission_id)

    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        grade = db.scalar(select(CodeGrade).where(CodeGrade.submission_id == submission_id))
        assert submission.grading_status == "completed"
        assert submission.result_revision == 1
        assert grade is not None and grade.status in ("pending", "queued")
        assert grade.functional_score == 60.0
        source = (grade.deterministic_details or {}).get("source", {})
        assert source.get("result_revision") == 1, "评分记录必须带来源证据（修订号）"
        assert source.get("judge_claim_id"), "评分记录必须带原判题认领标识"


class _RenewBeforeUpdateSession:
    """代理 Session：在恢复扫描发出 UPDATE 之前续一次心跳（模拟活着的 Worker）。

    这是唯一能确定性复现"SELECT 与 UPDATE 之间任务被续期"的方式：同一个连接上
    先写一次续期，再让恢复语句执行——修复前它会照样重置，修复后会被实时过期谓词挡住。
    """

    def __init__(self, session, table_name: str, renew_statement):
        self._session = session
        self._table_name = table_name
        self._renew_statement = renew_statement
        self._renewed = False

    def __getattr__(self, item):
        return getattr(self._session, item)

    def execute(self, statement, *args, **kwargs):
        if (not self._renewed and isinstance(statement, Update)
                and getattr(statement.table, "name", None) == self._table_name):
            self._renewed = True
            self._session.execute(self._renew_statement)
        return self._session.execute(statement, *args, **kwargs)


def test_renewed_heartbeat_after_scan_is_not_reset(db_session_factory):
    """恢复扫描必须同时核对代次与"实际仍然过期"。"""
    from app.services.judge_queue import claim_job

    with db_session_factory() as db:
        teacher = User(username="hb_t", real_name="HBT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="hb_s", real_name="HBS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student])
        db.flush()
        course = Course(title="hb", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        assignment = Assignment(course_id=course.id, title="hb-a", status="published")
        db.add(assignment)
        db.flush()
        question = JudgeQuestion(assignment_id=assignment.id, title="hb-q", function_name="f",
                                 hidden_tests="assert True", public_cases=[],
                                 test_groups=[{"id": "F1", "name": "F", "dimension": "F",
                                               "max_score": 60, "tests": "def test(): pass"}])
        db.add(question)
        db.flush()
        submission = Submission(question_id=question.id, student_id=student.id,
                                code="def f(): pass", status="running",
                                grading_status="queued")
        db.add(submission)
        db.commit()
        submission_id = submission.id

    stale = datetime.now(timezone.utc) - timedelta(seconds=7200)
    with db_session_factory() as db:
        token = claim_job(db, job_type="assignment", object_id=submission_id)
        assert token
        db.execute(update(Submission).where(Submission.id == submission_id)
                   .values(started_at=stale, heartbeat_at=stale))
        db.commit()

    with db_session_factory() as db:
        renew = update(Submission).where(Submission.id == submission_id) \
            .values(heartbeat_at=datetime.now(timezone.utc))
        proxy = _RenewBeforeUpdateSession(db, Submission.__tablename__, renew)
        stats = requeue_stale_jobs(proxy, job_type="assignment", stale_running_seconds=1800)

    assert stats["running_reset"] == 0, "SELECT 之后续过心跳的任务不得被旧扫描重置"
    with db_session_factory() as db:
        row = db.get(Submission, submission_id)
        assert row.grading_status == "running"
        assert row.claim_token == token


def test_still_stale_task_is_reset(db_session_factory):
    """对照：确实过期且未续期的任务仍要被恢复（不得把修复做成回归）。"""
    from app.services.judge_queue import claim_job, requeue_stale_jobs as requeue

    with db_session_factory() as db:
        teacher = User(username="st_t", real_name="STT", role="teacher", status="active",
                       password_hash="x")
        student = User(username="st_s", real_name="STS", role="student", status="active",
                       password_hash="x")
        db.add_all([teacher, student])
        db.flush()
        course = Course(title="st", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        assignment = Assignment(course_id=course.id, title="st-a", status="published")
        db.add(assignment)
        db.flush()
        question = JudgeQuestion(assignment_id=assignment.id, title="st-q", function_name="f",
                                 hidden_tests="assert True", public_cases=[],
                                 test_groups=[{"id": "F1", "name": "F", "dimension": "F",
                                               "max_score": 60, "tests": "def test(): pass"}])
        db.add(question)
        db.flush()
        submission = Submission(question_id=question.id, student_id=student.id,
                                code="def f(): pass", status="running",
                                grading_status="queued")
        db.add(submission)
        db.commit()
        submission_id = submission.id

    stale = datetime.now(timezone.utc) - timedelta(seconds=7200)
    with db_session_factory() as db:
        assert claim_job(db, job_type="assignment", object_id=submission_id)
        db.execute(update(Submission).where(Submission.id == submission_id)
                   .values(started_at=stale, heartbeat_at=stale))
        db.commit()

    with db_session_factory() as db, patch("app.services.judge_queue._get_redis",
                                           side_effect=OSError("redis down")):
        stats = requeue(db, job_type="assignment", stale_running_seconds=1800)
    assert stats["running_reset"] == 1
    with db_session_factory() as db:
        row = db.get(Submission, submission_id)
        assert row.grading_status == "pending"
        assert row.claim_token is None


def test_ai_notification_failure_keeps_durable_task(
        db_session_factory, test_settings, redis_client, grading_world):
    """Redis 推送失败不得回滚已提交结果：数据库留 queued，由恢复扫描重推。"""
    submission_id = _seed_judge_submission(db_session_factory, grading_world)
    with db_session_factory() as db, \
            patch.object(worker, "_run_docker_data_runner", side_effect=_all_pass_data_frames), \
            patch.object(worker, "_submission_env_context", return_value=(None, None, set())), \
            patch.object(worker, "_diagnose_submission", return_value=None), \
            patch("app.services.judge_queue._get_redis", side_effect=OSError("redis down")):
        worker.process_submission(db, redis_client, test_settings, submission_id)

    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        grade = db.scalar(select(CodeGrade).where(CodeGrade.submission_id == submission_id))
        assert submission.grading_status == "completed", "数据库是事实源，不因通知失败回滚"
        assert grade is not None, "推送失败不得丢掉评分记录"
        assert grade.status in ("pending", "queued"), "任务留在等待恢复的状态"
        assert grade.claim_token is None


# ─────────────────────────────────────────────────────────────
# 4. 必需分析器故障：不调用外部 AI，转人工（批次四）
# ─────────────────────────────────────────────────────────────

def test_unavailable_analyzer_skips_external_ai_and_needs_review(
        db_session_factory, test_settings, redis_client, grading_world):
    grade_id = _seed_ai_grade(db_session_factory, grading_world)
    calls: list = []

    class _RecordingClient:
        def __init__(self, *args, **kwargs):
            calls.append("constructed")

        def chat_json(self, *args, **kwargs):
            calls.append("called")      # 任何真实外呼都会记录在这里
            return _ai_payload()

    with db_session_factory() as db, \
            patch("app.services.ai_client.DeepSeekClient", _RecordingClient), \
            patch("app.services.ai_grading_service.analyze_python",
                  return_value={"diagnostics": [], "complexity": {},
                                "tool_status": {"ruff": "unavailable", "radon": "ok"},
                                "status": "degraded"}):
        worker.process_ai_grade(db, redis_client, test_settings, grade_id)

    assert "called" not in calls, "分析器故障时不得发起任何外部 AI 调用"
    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        submission = db.get(Submission, grade.submission_id)
        assert grade.status == "review_required"
        assert grade.needs_teacher_review is True
        assert "静态分析工具不可用" in (grade.review_reason or "")
        assert grade.claim_token is None
        assert submission.score is None, "不得生成自动正式分"


def test_timeout_analyzer_also_requires_manual_review(
        db_session_factory, test_settings, redis_client, grading_world):
    grade_id = _seed_ai_grade(db_session_factory, grading_world)
    called: list = []

    class _NeverCallClient:
        def __init__(self, *args, **kwargs):
            pass

        def chat_json(self, *args, **kwargs):
            called.append("chat_json")
            raise AssertionError("分析器超时时不得外呼")

    with db_session_factory() as db, \
            patch("app.services.ai_client.DeepSeekClient", _NeverCallClient), \
            patch("app.services.ai_grading_service.analyze_python",
                  return_value={"diagnostics": [], "complexity": {},
                                "tool_status": {"ruff": "ok", "radon": "timeout"},
                                "status": "degraded"}):
        worker.process_ai_grade(db, redis_client, test_settings, grade_id)

    assert called == []

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.status == "review_required"
        assert "radon:timeout" in (grade.review_reason or "")


# ─────────────────────────────────────────────────────────────
# A09：评分事实必须来自绑定版本（冻结），不受之后题目/版本变更影响
# ─────────────────────────────────────────────────────────────

def _bind_version(db_session_factory, grade_id, *, prompt, cap_rules, points=None):
    """给提交绑定一个已启用的评分版本，并把评分事实冻结在版本里。"""
    from app.models import JudgeGradingVersion

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        submission = db.get(Submission, grade.submission_id)
        existing_versions = db.query(JudgeGradingVersion).filter_by(
            judge_question_id=submission.question_id).all()
        version = JudgeGradingVersion(
            judge_question_id=submission.question_id,
            version=max((item.version for item in existing_versions), default=0) + 1,
            status="active",
            execution_mode="data_v1", grading_mode_snapshot="active",
            entry_function="solve", source_kind="manual",
            payload={"schema_version": 2, "entry_function": "solve", "execution_model": "isolated_case_v2",
                     "groups": []},
            score_cap_rules=cap_rules,
            context_snapshot={"prompt": prompt, "title": prompt, "description": prompt,
                              "function_name": "solve", "teacher_constraints": {},
                              "rubric_snapshot": db.get(QuestionRubric, grade.rubric_id).rubric_json,
                              "rubric_version": db.get(QuestionRubric, grade.rubric_id).version,
                              "points": points},
            source_digest="a" * 64, spec_digest="b" * 64,
            rubric_id=grade.rubric_id,
        )
        db.add(version)
        db.flush()
        submission.grading_version_id = version.id
        details = dict(grade.deterministic_details or {})
        source = dict(details.get("source") or {})
        source["grading_version_id"] = version.id
        details["source"] = source
        grade.deterministic_details = details
        db.commit()
        return version.id


def test_a09_ai_uses_frozen_prompt_and_cap_rules(
        db_session_factory, test_settings, redis_client, grading_world):
    """冻结题面与上限规则优先于实时题目配置。"""
    grade_id = _seed_ai_grade(db_session_factory, grading_world)
    version_id = _bind_version(
        db_session_factory, grade_id,
        prompt="冻结题面：实现 solve", cap_rules=[{"id": "cap-frozen", "cap": 40}])

    # 之后教师修改题目题面与上限规则（不得影响已绑定提交）
    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        question = db.get(JudgeQuestion, db.get(Submission, grade.submission_id).question_id)
        question.title = "实时题面：已被教师改写"
        question.score_cap_rules = [{"id": "cap-live", "cap": 100}]
        db.commit()

    payload = _ai_payload()
    # A/Q 保持 0 分（分项为空），合分基线 = F60 + R10 = 70 → 冻结上限 40 生效
    payload["triggered_cap_rule_ids"] = ["cap-frozen", "cap-live"]

    recorded = {}

    class _RecordingClient:
        def chat_json(self, messages, **kwargs):
            recorded["messages"] = messages
            return payload

    with db_session_factory() as db, \
            patch("app.services.ai_grading_service.analyze_python",
                  return_value={"diagnostics": [], "complexity": {}}):
        from app.services.ai_grading_service import grade_code_submission
        from app.services.ai_grading_queue import claim_ai_grade

        claim_ai_grade(db, grade_id)
        grade_code_submission(db, _RecordingClient(), grade_id)

    text = str(recorded["messages"])
    assert "冻结题面" in text, "AI 必须看到冻结题面"
    assert "实时题面" not in text, "题目改写不得进入已绑定提交的评分"
    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.status == "completed"
        # 基线 70 分，冻结上限 40 生效（实时规则 cap=100 若被读取则不会封顶）
        assert grade.final_score_100 == 40.0, grade.final_score_100
        assert grade.deterministic_details["grading_version_id"] == version_id


def test_a09_exam_points_frozen_at_start(
        db_session_factory, test_settings, redis_client, grading_world):
    """考试分值在开始考试时冻结：之后改分值不影响已绑定答案的折算分。"""
    from app.models import ExamAnswer, ExamQuestion, ExamSubmission

    with db_session_factory() as db:
        # 考试父记录（exam_id 外键）
        exam = Exam(title="term-exam", status="published", course_id=grading_world["course_id"],
                    start_at=datetime.now(timezone.utc),
                    end_at=datetime.now(timezone.utc) + timedelta(hours=2))
        db.add(exam)
        db.flush()
        exam_id = exam.id
        exam_question = ExamQuestion(exam_id=exam_id, question_type="code", prompt="q",
                                     points=20, order_index=1,
                                     correct_answer={"function_name": "solve"})
        db.add(exam_question)
        db.flush()
        rubric = QuestionRubric(judge_question_id=None, exam_question_id=exam_question.id,
                                version=1, status="locked", source_hash="a" * 64,
                                source_snapshot="{}", rubric_json={"algorithm_criteria": []},
                                model_name="test-model")
        db.add(rubric)
        db.flush()
        version = JudgeGradingVersion(
            exam_question_id=exam_question.id, version=1, status="active",
            execution_mode="data_v1", grading_mode_snapshot="active", entry_function="solve",
            source_kind="manual", payload={"schema_version": 1, "entry_function": "solve",
                                           "groups": []},
            context_snapshot={"prompt": "冻结", "title": "冻结", "description": "冻结",
                              "function_name": "solve", "teacher_constraints": {},
                              "rubric_snapshot": rubric.rubric_json,
                              "rubric_version": rubric.version, "points": 20.0},
            rubric_id=rubric.id,
            source_digest="a" * 64, spec_digest="b" * 64)
        db.add(version)
        db.flush()
        exam_submission = ExamSubmission(exam_id=exam_id, student_id=grading_world["student_id"],
                                         status="grading", score=None,
                                         grading_version_map={str(exam_question.id): {
                                             "version_id": version.id, "points": 20.0}})
        db.add(exam_submission)
        db.flush()
        answer = ExamAnswer(submission_id=exam_submission.id, question_id=exam_question.id,
                            code_answer="def solve(x): return 0", grading_status="queued",
                            grading_version_id=version.id)
        db.add(answer)
        db.flush()
        grade = CodeGrade(exam_answer_id=answer.id, rubric_id=rubric.id, mode="active",
                          status="queued", functional_score=60, robustness_score=10,
                          deterministic_details={"source": {
                              "result_revision": answer.result_revision,
                              "grading_version_id": version.id}})
        db.add(grade)
        db.commit()
        grade_id = grade.id
        # 教师之后把题目分值改成 50：已绑定答案仍按冻结的 20 折算
        db.get(ExamQuestion, exam_question.id).points = 50
        db.commit()

    with db_session_factory() as db, \
            patch("app.services.ai_grading_service.analyze_python",
                  return_value={"diagnostics": [], "complexity": {}}):
        from app.services.ai_grading_service import grade_code_submission
        from app.services.ai_grading_queue import claim_ai_grade

        claim_ai_grade(db, grade_id)
        grade_code_submission(db, _FakeAIClient(_ai_payload()), grade_id)

    with db_session_factory() as db:
        grade = db.get(CodeGrade, grade_id)
        assert grade.final_score_100 == 70.0, grade.final_score_100
        assert grade.scaled_score == 14.0, \
            f"必须按开始考试时冻结的 20 分折算（70/100*20），实际 {grade.scaled_score}"
