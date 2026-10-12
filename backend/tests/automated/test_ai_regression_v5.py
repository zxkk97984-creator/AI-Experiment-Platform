"""Regression coverage for issues found while accepting the sixth review.

A/B/C 分类：B 类（最小父行）——assignment/exam/student 外键经共享工厂建真实父行。
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from conftest import grant_teaching_fixture, make_assignment, make_exam, make_student, grant_judge_claim


def _create_course_and_exam(client, db_session_factory, username: str):
    from conftest import auth_header, create_user, login, seed_basic_environment

    seed_basic_environment(db_session_factory)
    create_user(db_session_factory, username, "teacher")
    token, _ = login(client, username)
    headers = auth_header(token)
    from conftest import create_course_db
    course_id = create_course_db(db_session_factory, teacher_username=username, title=f"{username}-course", status="published")
    now = datetime.now(timezone.utc)
    exam_id = client.post(
        "/api/v1/exams",
        headers=headers,
        json={
            "course_id": course_id,
            "title": f"{username}-exam",
            "duration_minutes": 60,
            "start_at": (now - timedelta(hours=1)).isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        },
    ).json()["id"]
    return headers, course_id, exam_id


def test_assignment_explicit_null_mode_still_defaults_to_active(client, db_session_factory):
    from conftest import auth_header, create_user, login

    create_user(db_session_factory, "null_assignment_teacher", "teacher")
    token, _ = login(client, "null_assignment_teacher")
    headers = auth_header(token)
    from conftest import create_course_db
    course_id = create_course_db(db_session_factory, teacher_username="null_assignment_teacher", title="Null assignment course", status="published")
    assignment_id = client.post(
        "/api/v1/assignments",
        headers=headers,
        json={"title": "Null assignment", "course_id": course_id},
    ).json()["id"]

    response = client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        headers=headers,
        json={
            "title": "Question",
            "function_name": "solve",
            "signature": "def solve():",
            "public_cases": [],
            "hidden_tests": "def test_ok(): assert True",
            "grading_mode": None,
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["grading_mode"] == "active"
    config = client.get(
        f"/api/v1/ai-grading/questions/assignment/{response.json()['id']}/config",
        headers=headers,
    )
    assert config.json()["grading_mode"] == "active"


def test_exam_code_explicit_null_mode_still_defaults_to_active(client, db_session_factory):
    headers, _, exam_id = _create_course_and_exam(
        client, db_session_factory, "null_exam_teacher"
    )

    response = client.post(
        f"/api/v1/exams/{exam_id}/questions",
        headers=headers,
        json={
            "question_type": "code",
            "prompt": "Code question",
            "points": 10,
            "correct_answer": {},
            "hidden_tests": "def test_ok(): assert True",
            "grading_mode": None,
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["grading_mode"] == "active"
    config = client.get(
        f"/api/v1/ai-grading/questions/exam/{response.json()['id']}/config",
        headers=headers,
    )
    assert config.json()["grading_mode"] == "active"


def test_exam_choice_question_rejects_ai_mode(client, db_session_factory):
    headers, _, exam_id = _create_course_and_exam(
        client, db_session_factory, "choice_mode_teacher"
    )

    response = client.post(
        f"/api/v1/exams/{exam_id}/questions",
        headers=headers,
        json={
            "question_type": "single_choice",
            "prompt": "Choice question",
            "points": 5,
            "options": {"A": "one", "B": "two"},
            "correct_answer": {"correct": ["A"]},
            "grading_mode": "active",
        },
    )

    assert response.status_code in (400, 422), response.text


def test_shadow_assignment_docker_exception_does_not_award_zero(
    db_session_factory, tmp_path: Path
):
    from app.config import Settings
    from app.models import CodeGrade, JudgeQuestion, QuestionRubric, Submission
    from app.worker.judge_worker import _v1_judge_submission

    groups = [
        {
            "id": "F1",
            "name": "functional",
            "dimension": "F",
            "max_score": 60,
            "tests": "def test_f(): assert True",
        },
        {
            "id": "R1",
            "name": "robustness",
            "dimension": "R",
            "max_score": 10,
            "tests": "def test_r(): assert True",
        },
    ]
    deterministic = {
        "results": {
            "F1": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0},
            "R1": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0},
        },
        "system_errors": [],
    }

    assignment_id = make_assignment(db_session_factory)
    student = make_student(db_session_factory)
    with db_session_factory() as db:
        question = JudgeQuestion(
            assignment_id=assignment_id,
            title="Shadow question",
            function_name="solve",
            hidden_tests="def test_hidden(): assert True",
            grading_mode="shadow",
            test_groups=groups,
        )
        db.add(question)
        db.flush()
        rubric = QuestionRubric(
            judge_question_id=question.id,
            version=1,
            status="locked",
            source_hash="hash",
            source_snapshot={},
            rubric_json={},
            model_name="model",
            locked_at=datetime.now(timezone.utc),
        )
        db.add(rubric)
        submission = Submission(
            question_id=question.id,
            student_id=student.id,
            code="def solve(): pass",
            status="running",
            grading_status="running",
        )
        db.add(submission)
        db.commit()
        if hasattr(submission, "id"):
            grant_judge_claim(db, Submission, submission.id)

        with patch(
            "app.worker.judge_worker.run_test_groups", return_value=deterministic
        ), patch(
            "app.worker.judge_worker._run_docker_pytest",
            side_effect=RuntimeError("Docker unavailable"),
        ):
            result = _v1_judge_submission(
                db,
                MagicMock(),
                Settings(_env_file=None),
                submission,
                question,
                tmp_path,
                tmp_path,
                5,
                256,
            )

        assert result.score is None
        assert result.status == "system_error"
        assert db.query(CodeGrade).filter(CodeGrade.submission_id == submission.id).count() == 0


def test_shadow_assignment_docker_system_exit_does_not_award_zero(
    db_session_factory, tmp_path: Path
):
    """Docker CLI failures normally return 125 instead of raising Python exceptions."""
    from app.config import Settings
    from app.models import CodeGrade, JudgeQuestion, QuestionRubric, Submission
    from app.worker.judge_worker import _v1_judge_submission

    groups = [
        {"id": "F1", "name": "functional", "dimension": "F", "max_score": 60, "tests": "pass"},
        {"id": "R1", "name": "robustness", "dimension": "R", "max_score": 10, "tests": "pass"},
    ]
    deterministic = {
        "results": {
            "F1": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0},
            "R1": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0},
        },
        "system_errors": [],
    }

    assignment_id = make_assignment(db_session_factory)
    student = make_student(db_session_factory)
    with db_session_factory() as db:
        question = JudgeQuestion(
            assignment_id=assignment_id,
            title="Shadow question",
            function_name="solve",
            hidden_tests="def test_hidden(): assert True",
            grading_mode="shadow",
            test_groups=groups,
        )
        db.add(question)
        db.flush()
        db.add(
            QuestionRubric(
                judge_question_id=question.id,
                version=1,
                status="locked",
                source_hash="hash",
                source_snapshot={},
                rubric_json={},
                model_name="model",
                locked_at=datetime.now(timezone.utc),
            )
        )
        submission = Submission(
            question_id=question.id,
            student_id=student.id,
            code="def solve(): pass",
            status="running",
            grading_status="running",
        )
        db.add(submission)
        db.commit()
        if hasattr(submission, "id"):
            grant_judge_claim(db, Submission, submission.id)

        with patch(
            "app.worker.judge_worker.run_test_groups", return_value=deterministic
        ), patch(
            "app.worker.judge_worker._run_docker_pytest",
            return_value=("", "Cannot connect to the Docker daemon", 125, 1),
        ):
            result = _v1_judge_submission(
                db,
                MagicMock(),
                Settings(_env_file=None),
                submission,
                question,
                tmp_path,
                tmp_path,
                5,
                256,
            )

        assert result.score is None
        assert result.status == "system_error"
        assert db.query(CodeGrade).filter(CodeGrade.submission_id == submission.id).count() == 0


def test_shadow_exam_docker_system_exit_does_not_award_zero(db_session_factory):
    from app.config import Settings
    from app.models import (
        CodeGrade,
        ExamAnswer,
        ExamQuestion,
        ExamSubmission,
        QuestionRubric,
    )
    from app.worker.judge_worker import process_exam_answer

    groups = [
        {"id": "F1", "name": "functional", "dimension": "F", "max_score": 60, "tests": "pass"},
        {"id": "R1", "name": "robustness", "dimension": "R", "max_score": 10, "tests": "pass"},
    ]
    deterministic = {
        "results": {
            "F1": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0},
            "R1": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0},
        },
        "system_errors": [],
    }

    exam_id = make_exam(db_session_factory)
    student = make_student(db_session_factory)
    with db_session_factory() as db:
        question = ExamQuestion(
            exam_id=exam_id,
            question_type="code",
            prompt="Shadow exam question",
            correct_answer={},
            points=10,
            hidden_tests="def test_hidden(): assert True",
            grading_mode="shadow",
            test_groups=groups,
        )
        db.add(question)
        db.flush()
        db.add(
            QuestionRubric(
                exam_question_id=question.id,
                version=1,
                status="locked",
                source_hash="hash",
                source_snapshot={},
                rubric_json={},
                model_name="model",
                locked_at=datetime.now(timezone.utc),
            )
        )
        exam_submission = ExamSubmission(exam_id=exam_id, student_id=student.id, status="grading")
        db.add(exam_submission)
        db.flush()
        answer = ExamAnswer(
            submission_id=exam_submission.id,
            question_id=question.id,
            code_answer="def solve(): pass",
            grading_status="queued",
        )
        db.add(answer)
        db.commit()

        with patch(
            "app.worker.judge_worker.run_test_groups", return_value=deterministic
        ), patch(
            "app.worker.judge_worker._run_docker_pytest",
            return_value=("", "Cannot connect to the Docker daemon", 125, 1),
        ):
            result = process_exam_answer(
                db, MagicMock(), Settings(_env_file=None), answer.id
            )

        assert result.score is None
        assert result.system_error
        assert db.query(CodeGrade).filter(CodeGrade.exam_answer_id == answer.id).count() == 0


def test_invalid_grade_kind_is_rejected(client, db_session_factory):
    from conftest import auth_header, create_user, login

    create_user(db_session_factory, "invalid_kind_teacher", "teacher")
    token, _ = login(client, "invalid_kind_teacher")
    response = client.get(
        "/api/v1/ai-grading/grades?kind=not-a-kind",
        headers=auth_header(token),
    )

    assert response.status_code in (400, 422), response.text


def test_teacher_no_kind_question_filter_has_no_duplicate_joins(db_session_factory):
    from app.api.ai_grading import _build_grade_base_query
    from app.models import (
        Assignment,
        CodeGrade,
        Course,
        JudgeQuestion,
        QuestionRubric,
        Submission,
        User,
    )

    with db_session_factory() as db:
        teacher = User(
            username="no_kind_teacher",
            real_name="No Kind",
            role="teacher",
            status="active",
            password_hash="x",
        )
        db.add(teacher)
        db.flush()
        course = Course(title="No kind course", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        assignment = Assignment(title="No kind assignment", course_id=course.id)
        db.add(assignment)
        db.flush()
        question = JudgeQuestion(
            assignment_id=assignment.id,
            title="Question",
            function_name="solve",
            hidden_tests="def test_ok(): assert True",
            grading_mode="active",
        )
        db.add(question)
        db.flush()
        rubric = QuestionRubric(
            judge_question_id=question.id,
            version=1,
            status="locked",
            source_hash="hash",
            source_snapshot={},
            rubric_json={},
            model_name="model",
            locked_at=datetime.now(timezone.utc),
        )
        db.add(rubric)
        db.flush()
        student = User(username='no_kind_student', real_name='No Kind Student',
                       role='student', status='active', password_hash='fixture')
        db.add(student)
        db.flush()
        submission = Submission(
            question_id=question.id,
            student_id=student.id,
            code="def solve(): pass",
            status="graded",
            grading_status="completed",
            score=100,
        )
        db.add(submission)
        db.flush()
        db.add(
            CodeGrade(
                submission_id=submission.id,
                rubric_id=rubric.id,
                mode="active",
                status="completed",
            )
        )
        grant_teaching_fixture(db, course, student_ids=[student.id])
        db.commit()

        query, count_query = _build_grade_base_query(
            db,
            teacher,
            kind=None,
            question_id=question.id,
            student_id=None,
            status=None,
        )
        grades = db.scalars(query).all()
        total = db.scalar(count_query)

        assert len(grades) == 1
        assert total == 1
