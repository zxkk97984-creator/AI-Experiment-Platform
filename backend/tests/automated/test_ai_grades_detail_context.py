"""评分详情上下文扩展——作业/考试两类的新增只读字段契约测试

覆盖：student_name/student_username/question_title/course_title/submitted_at/
finished_at/execution_time_ms 的取值规则；进行中评分 finished_at 为空；
既有字段不受影响。
"""
from datetime import datetime, timezone

from conftest import grant_teaching_fixture, create_user, login, auth_header, seed_basic_environment

from app.models import (
    Assignment, CodeGrade, Course, Exam, ExamAnswer, ExamQuestion,
    ExamSubmission, JudgeQuestion, QuestionRubric, Submission,
)


def _iso(v):
    """可空 datetime → iso 字符串；SQLite 不保留时区后缀，期望值统一 naive 化"""
    if not v:
        return None
    if v.tzinfo is not None:
        v = v.replace(tzinfo=None)
    return v.isoformat()


def _seed_assignment_grade(db, teacher, student, *, status="completed", finished=True,
                           execution_time_ms=120):
    """建作业类评分链，返回 (grade_id, 期望上下文)"""
    seed_basic_environment(db)
    course = Course(title="C上下文", status="published", teacher_id=teacher.id)
    db.add(course)
    db.flush()
    assignment = Assignment(title="A有效括号", course_id=course.id, status="published")
    db.add(assignment)
    db.flush()
    question = JudgeQuestion(
        assignment_id=assignment.id, title="Q有效括号", function_name="is_valid",
        hidden_tests="def test(): pass", grading_mode="active",
        test_groups=[{"id": "F1", "name": "F", "dimension": "F", "max_score": 60,
                      "tests": "def test(): assert True"}],
    )
    db.add(question)
    db.flush()
    rubric = QuestionRubric(
        judge_question_id=question.id, version=1, status="locked",
        source_hash="h", source_snapshot={}, rubric_json={}, model_name="m",
        locked_at=datetime.now(timezone.utc),
    )
    db.add(rubric)
    db.flush()
    sub = Submission(
        question_id=question.id, student_id=student.id, code="def is_valid(s): pass",
        status="graded", grading_status="completed", score=88,
        execution_time_ms=execution_time_ms,
    )
    db.add(sub)
    db.flush()
    cg = CodeGrade(
        submission_id=sub.id, rubric_id=rubric.id, mode="active", status=status,
        functional_score=60, algorithm_score=15, robustness_score=10, quality_score=8,
        raw_total=93, score_cap=None, final_score_100=93, scaled_score=93,
        needs_teacher_review=False, attempt_count=1,
        finished_at=datetime.now(timezone.utc) if finished else None,
    )
    db.add(cg)
    grant_teaching_fixture(db, course, [student.id])
    db.commit()
    # 以库内实际值为准：MySQL DATETIME(0) 截断微秒，API 读到的正是截断值
    db.refresh(cg)
    db.refresh(sub)
    return cg.id, {
        "student_name": student.real_name,
        "student_username": student.username,
        "question_title": "Q有效括号",
        "course_title": "C上下文",
        "submitted_at": _iso(sub.created_at),
        "finished_at": _iso(cg.finished_at),
        "execution_time_ms": 120,
    }


def _seed_exam_grade(db, teacher, student, *, status="completed", finished=True):
    """建考试类评分链，返回 (grade_id, 期望上下文)"""
    seed_basic_environment(db)
    course = Course(title="C考试课", status="published", teacher_id=teacher.id)
    db.add(course)
    db.flush()
    exam = Exam(course_id=course.id, title="E期中", status="published", duration_minutes=60)
    db.add(exam)
    db.flush()
    eq = ExamQuestion(exam_id=exam.id, question_type="code", prompt="写函数",
                      correct_answer={"test_file": ""}, points=10, grading_mode="active")
    db.add(eq)
    db.flush()
    rubric = QuestionRubric(
        exam_question_id=eq.id, version=1, status="locked",
        source_hash="h", source_snapshot={}, rubric_json={}, model_name="m",
        locked_at=datetime.now(timezone.utc),
    )
    db.add(rubric)
    db.flush()
    es = ExamSubmission(exam_id=exam.id, student_id=student.id, status="grading")
    db.add(es)
    db.flush()
    ea = ExamAnswer(submission_id=es.id, question_id=eq.id, code_answer="def f(): pass",
                    grading_status="completed", score=8)
    db.add(ea)
    db.flush()
    cg = CodeGrade(
        exam_answer_id=ea.id, rubric_id=rubric.id, mode="active", status=status,
        functional_score=60, algorithm_score=18, robustness_score=10, quality_score=9,
        raw_total=97, score_cap=None, final_score_100=97, scaled_score=9.7,
        needs_teacher_review=False, attempt_count=1,
        finished_at=datetime.now(timezone.utc) if finished else None,
    )
    db.add(cg)
    grant_teaching_fixture(db, course, [student.id])
    db.commit()
    # 以库内实际值为准：MySQL DATETIME(0) 截断微秒，API 读到的正是截断值
    db.refresh(cg)
    db.refresh(ea)
    return cg.id, {
        "student_name": student.real_name,
        "student_username": student.username,
        "question_title": None,  # ExamQuestion 无 title 列，取 prompt
        "course_title": "C考试课",
        "submitted_at": _iso(ea.created_at),
        "finished_at": _iso(cg.finished_at),
        "execution_time_ms": None,  # 考试类无运行时间
    }


class TestDetailContextAssignment:
    """作业类详情上下文字段"""

    def test_returns_context_fields(self, client, db_session_factory):
        teacher = create_user(db_session_factory, "dca_t", "teacher", real_name="王老师")
        student = create_user(db_session_factory, "dca_s", "student", real_name="李同学")
        with db_session_factory() as db:
            grade_id, expected = _seed_assignment_grade(db, teacher, student)

        tok, _ = login(client, "dca_t")
        resp = client.get(f"/api/v1/ai-grading/grades/{grade_id}", headers=auth_header(tok))
        assert resp.status_code == 200, f"详情应 200: {resp.status_code} {resp.text}"
        data = resp.json()

        assert data["student_name"] == "李同学"
        assert data["student_username"] == "dca_s"
        assert data["question_title"] == "Q有效括号"
        assert data["course_title"] == "C上下文"
        assert data["submitted_at"] == expected["submitted_at"]
        assert data["finished_at"] == expected["finished_at"]
        assert data["execution_time_ms"] == 120

        # 既有字段不受影响
        assert data["functional_score"] == 60
        assert data["algorithm_score"] == 15
        assert data["robustness_score"] == 10
        assert data["quality_score"] == 8
        assert data["raw_total"] == 93
        assert data["final_score_100"] == 93
        assert data["mode"] == "active"
        assert data["status"] == "completed"
        assert data["needs_teacher_review"] is False
        assert data["attempt_count"] == 1
        assert data["student_code"] == "def is_valid(s): pass"
        assert data["overrides"] == []


class TestDetailContextExam:
    """考试类详情上下文字段"""

    def test_returns_context_fields_exam(self, client, db_session_factory):
        teacher = create_user(db_session_factory, "dce_t", "teacher", real_name="王老师")
        student = create_user(db_session_factory, "dce_s", "student", real_name="李同学")
        with db_session_factory() as db:
            grade_id, expected = _seed_exam_grade(db, teacher, student)

        tok, _ = login(client, "dce_t")
        resp = client.get(f"/api/v1/ai-grading/grades/{grade_id}", headers=auth_header(tok))
        assert resp.status_code == 200, f"详情应 200: {resp.status_code} {resp.text}"
        data = resp.json()

        assert data["student_name"] == "李同学"
        assert data["student_username"] == "dce_s"
        assert data["course_title"] == "C考试课"
        assert data["submitted_at"] == expected["submitted_at"]
        assert data["finished_at"] == expected["finished_at"]
        assert data["execution_time_ms"] is None, "考试类不应有运行时间"

        # 考试题无 title 列 → question_title 回退 prompt
        assert data["question_title"] == "写函数"


class TestDetailContextIncomplete:
    """进行中评分：finished_at 等终态字段为 None"""

    def test_pending_grade_has_no_finished_at(self, client, db_session_factory):
        teacher = create_user(db_session_factory, "dci_t", "teacher")
        student = create_user(db_session_factory, "dci_s", "student")
        with db_session_factory() as db:
            grade_id, expected = _seed_assignment_grade(
                db, teacher, student, status="pending", finished=False,
            )

        tok, _ = login(client, "dci_t")
        resp = client.get(f"/api/v1/ai-grading/grades/{grade_id}", headers=auth_header(tok))
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["status"] == "pending"
        assert data["finished_at"] is None
        assert data["submitted_at"] == expected["submitted_at"]
        assert data["execution_time_ms"] == 120
        assert data["student_name"] == student.real_name
