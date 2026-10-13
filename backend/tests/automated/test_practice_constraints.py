"""批次 5 练习域模型不变量：三个 XOR 目标约束扩成三选一（约束名不变）。

对应规格：扩展现有评分标准、评分版本、评分结果的目标关联使其支持练习，
**保持目标唯一与角色隔离**。每条都带对照组：合法三选一成功 / 两个目标或零个目标被拒。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from conftest import (constraint_violation, make_exam_question, make_judge_question,
                      make_student, make_submission)
from app.models import CodeGrade, JudgeGradingVersion, QuestionRubric
from app.models.practice import PracticeProblem

pytestmark = pytest.mark.usefixtures("db_session_factory")


def _make_problem(db_session_factory, admin_id):
    with db_session_factory() as db:
        problem = PracticeProblem(
            title="练习题目", difficulty="easy", tags=[], prompt="p",
            public_cases=[], hidden_tests=[{"id": "c1", "input": {"t": "tuple", "v": []},
                                            "expected": {"t": "none"}}],
            test_groups=[], time_limit_ms=10000, memory_limit_kb=262144,
            grading_mode="deterministic", teacher_constraints={}, reference_solution=None,
            score_cap_rules=[], function_name="solution", source_kind="admin_made",
            status="draft", created_by_id=admin_id,
        )
        db.add(problem)
        db.commit()
        return problem.id


@pytest.fixture()
def practice_targets(db_session_factory):
    from practice_test_support import make_admin

    admin = make_admin(db_session_factory)
    problem_id = _make_problem(db_session_factory, admin.id)
    return admin, problem_id


def test_rubric_xor_accepts_practice_target_only(db_session_factory, practice_targets):
    """合法：只挂练习目标（控制组：原有作业目标仍可用）。"""
    admin, problem_id = practice_targets
    with db_session_factory() as db:
        rubric = QuestionRubric(
            practice_problem_id=problem_id, version=1, status="draft",
            source_hash="a" * 64, source_snapshot={}, rubric_json={},
            model_name="test-model",
        )
        db.add(rubric)
        db.commit()
        assert rubric.id is not None
        assert rubric.judge_question_id is None and rubric.exam_question_id is None


def test_rubric_xor_rejects_practice_plus_assignment(db_session_factory, practice_targets):
    """非法：同时挂练习目标与作业目标（约束名不变仍是 ck_rubric_xor_target）。"""
    _admin, problem_id = practice_targets
    question_id = make_judge_question(db_session_factory)
    with db_session_factory() as db:
        db.add(QuestionRubric(
            judge_question_id=question_id, practice_problem_id=problem_id, version=1,
            status="draft", source_hash="b" * 64, source_snapshot={}, rubric_json={},
            model_name="test-model",
        ))
        with constraint_violation():
            db.commit()


def test_rubric_xor_rejects_no_target(db_session_factory, practice_targets):
    """非法：一个目标都没有。"""
    with db_session_factory() as db:
        db.add(QuestionRubric(
            version=1, status="draft", source_hash="c" * 64, source_snapshot={},
            rubric_json={}, model_name="test-model",
        ))
        with constraint_violation():
            db.commit()


def test_grading_version_xor_accepts_practice_target_only(db_session_factory, practice_targets):
    _admin, problem_id = practice_targets
    with db_session_factory() as db:
        version = JudgeGradingVersion(
            practice_problem_id=problem_id, version=1, status="draft",
            execution_mode="data_v1", grading_mode_snapshot="legacy",
            entry_function="solution", payload={"schema_version": 2}, compare_rules={},
            score_cap_rules=[], context_snapshot={}, source_kind="manual",
            source_digest="d" * 64, spec_digest="e" * 64,
        )
        db.add(version)
        db.commit()
        assert version.id is not None


def test_grading_version_xor_rejects_practice_plus_exam(db_session_factory, practice_targets):
    _admin, problem_id = practice_targets
    exam_question_id = make_exam_question(db_session_factory)
    with db_session_factory() as db:
        db.add(JudgeGradingVersion(
            exam_question_id=exam_question_id, practice_problem_id=problem_id, version=1,
            status="draft", execution_mode="data_v1", grading_mode_snapshot="legacy",
            entry_function="solution", payload={"schema_version": 2}, compare_rules={},
            score_cap_rules=[], context_snapshot={}, source_kind="manual",
            source_digest="f" * 64, spec_digest="g" * 64,
        ))
        with constraint_violation():
            db.commit()


def test_grading_version_xor_rejects_no_target(db_session_factory):
    with db_session_factory() as db:
        db.add(JudgeGradingVersion(
            version=1, status="draft", execution_mode="data_v1",
            grading_mode_snapshot="legacy", entry_function="solution",
            payload={"schema_version": 2}, compare_rules={}, score_cap_rules=[],
            context_snapshot={}, source_kind="manual", source_digest="h" * 64,
            spec_digest="i" * 64,
        ))
        with constraint_violation():
            db.commit()


def test_code_grade_xor_accepts_practice_target_only(db_session_factory, practice_targets):
    """合法：CodeGrade 只挂练习提交（角色隔离的基础：一条评分记录只属一个目标）。"""
    _admin, problem_id = practice_targets
    student = make_student(db_session_factory)
    with db_session_factory() as db:
        rubric = QuestionRubric(
            practice_problem_id=problem_id, version=1, status="locked",
            source_hash="j" * 64, source_snapshot={}, rubric_json={"algorithm_criteria": []},
            model_name="test-model",
        )
        db.add(rubric)
        db.flush()
        from app.models.practice import PracticeSubmission

        submission = PracticeSubmission(
            problem_id=problem_id, student_id=student.id, code="def solution(a,b): return a+b",
            client_request_id="req-1", request_digest="k" * 64,
            grading_mode="ai", grading_status="running", max_score=100.0,
        )
        db.add(submission)
        db.flush()
        grade = CodeGrade(
            practice_submission_id=submission.id, rubric_id=rubric.id, mode="active",
            status="pending",
        )
        db.add(grade)
        db.commit()
        assert grade.id is not None
        assert grade.submission_id is None and grade.exam_answer_id is None


def test_code_grade_xor_rejects_practice_plus_submission(db_session_factory, practice_targets):
    _admin, problem_id = practice_targets
    student = make_student(db_session_factory)
    submission_id = make_submission(db_session_factory, student_username="practice-student")
    with db_session_factory() as db:
        rubric = QuestionRubric(
            practice_problem_id=problem_id, version=2, status="locked",
            source_hash="l" * 64, source_snapshot={}, rubric_json={},
            model_name="test-model",
        )
        db.add(rubric)
        db.flush()
        from app.models.practice import PracticeSubmission

        practice_submission = PracticeSubmission(
            problem_id=problem_id, student_id=student.id, code="x",
            client_request_id="req-2", request_digest="m" * 64,
            grading_mode="ai", grading_status="running", max_score=100.0,
        )
        db.add(practice_submission)
        db.flush()
        db.add(CodeGrade(
            submission_id=submission_id, practice_submission_id=practice_submission.id,
            rubric_id=rubric.id, mode="active", status="pending",
        ))
        with constraint_violation():
            db.commit()


def test_code_grade_xor_rejects_no_target(db_session_factory, practice_targets):
    _admin, problem_id = practice_targets
    with db_session_factory() as db:
        rubric = QuestionRubric(
            practice_problem_id=problem_id, version=3, status="locked",
            source_hash="n" * 64, source_snapshot={}, rubric_json={}, model_name="test-model",
        )
        db.add(rubric)
        db.flush()
        db.add(CodeGrade(rubric_id=rubric.id, mode="active", status="pending"))
        with constraint_violation():
            db.commit()


def test_practice_version_uniqueness_is_per_problem(db_session_factory, practice_targets):
    """唯一约束按题目隔离：同一题同版本号只能有一条（对照组：另一题可复用同版本号）。"""
    from practice_test_support import make_admin

    admin, problem_id = practice_targets
    other_problem_id = _make_problem(db_session_factory, admin.id)
    with db_session_factory() as db:
        db.add(JudgeGradingVersion(
            practice_problem_id=problem_id, version=1, status="draft",
            execution_mode="data_v1", grading_mode_snapshot="legacy",
            entry_function="solution", payload={"schema_version": 2}, compare_rules={},
            score_cap_rules=[], context_snapshot={}, source_kind="manual",
            source_digest="o" * 64, spec_digest="p" * 64,
        ))
        db.commit()
    with db_session_factory() as db:
        db.add(JudgeGradingVersion(
            practice_problem_id=other_problem_id, version=1, status="draft",
            execution_mode="data_v1", grading_mode_snapshot="legacy",
            entry_function="solution", payload={"schema_version": 2}, compare_rules={},
            score_cap_rules=[], context_snapshot={}, source_kind="manual",
            source_digest="q" * 64, spec_digest="r" * 64,
        ))
        db.commit()
        assert db.scalar(select(JudgeGradingVersion).where(
            JudgeGradingVersion.practice_problem_id == other_problem_id)) is not None
    with db_session_factory() as db:
        db.add(JudgeGradingVersion(
            practice_problem_id=problem_id, version=1, status="draft",
            execution_mode="data_v1", grading_mode_snapshot="legacy",
            entry_function="solution", payload={"schema_version": 2}, compare_rules={},
            score_cap_rules=[], context_snapshot={}, source_kind="manual",
            source_digest="s" * 64, spec_digest="t" * 64,
        ))
        with constraint_violation():
            db.commit()
