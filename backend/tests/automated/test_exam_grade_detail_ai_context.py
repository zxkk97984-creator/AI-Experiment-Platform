"""成绩详情 AI 评分上下文——教师端逐题展示确定性判题与 AI 评分状态

覆盖：
- 编程题答案携带 ai_grading（确定性得分、样例通过数、AI 状态与失败原因）
- 客观题 / 无评分记录的答案 ai_grading 为 None
- 既有字段不受影响（code_answer 等仍正常返回）
"""
from datetime import datetime, timezone

from conftest import grant_teaching_fixture, auth_header, create_user, login, seed_basic_environment

from app.models import (
    CodeGrade, Course, Exam, ExamAnswer, ExamQuestion, ExamSubmission, QuestionRubric,
)

API = "/api/v1"


def _seed_exam_with_grades(db, teacher, student):
    """建一门考试：1 单选 + 1 active 编程题，编程题带确定性结果与失败终止的 AI 评分。"""
    seed_basic_environment(db)
    course = Course(title="C详情AI", status="published", teacher_id=teacher.id)
    db.add(course)
    db.flush()
    exam = Exam(course_id=course.id, title="E期末", status="published", duration_minutes=60)
    db.add(exam)
    db.flush()
    choice = ExamQuestion(
        exam_id=exam.id, question_type="single_choice", prompt="Q单选",
        options={"A": "a", "B": "b"}, correct_answer={"correct": ["A"]},
        points=10, order_index=0,
    )
    code = ExamQuestion(
        exam_id=exam.id, question_type="code", prompt="写 is_balanced",
        correct_answer={}, points=20, order_index=1, grading_mode="active",
        test_groups=[{"id": "F1", "dimension": "F", "max_score": 60, "tests": "x"}],
    )
    db.add_all([choice, code])
    db.flush()
    rubric = QuestionRubric(
        exam_question_id=code.id, version=1, status="locked",
        source_hash="h", source_snapshot={}, rubric_json={}, model_name="m",
        locked_at=datetime.now(timezone.utc),
    )
    db.add(rubric)
    db.flush()
    es = ExamSubmission(exam_id=exam.id, student_id=student.id, status="review_required")
    db.add(es)
    db.flush()
    ans_choice = ExamAnswer(
        submission_id=es.id, question_id=choice.id,
        selected_options=["B"], grading_status="completed", score=0,
    )
    ans_code = ExamAnswer(
        submission_id=es.id, question_id=code.id,
        code_answer="def is_balanced(s):\n    return True",
        grading_status="completed", score=None,
    )
    db.add_all([ans_choice, ans_code])
    db.flush()
    cg = CodeGrade(
        exam_answer_id=ans_code.id, rubric_id=rubric.id, mode="active",
        status="review_required",
        functional_score=60, robustness_score=10,
        deterministic_details={"groups": [
            {"id": "F1", "counts": {"passed": 7, "failed": 0, "errors": 0, "skipped": 0}, "score": 30.0, "max_score": 30.0, "dimension": "F"},
            {"id": "F2", "counts": {"passed": 5, "failed": 1, "errors": 0, "skipped": 0}, "score": 25.0, "max_score": 30.0, "dimension": "F"},
            {"id": "R1", "counts": {"passed": 4, "failed": 0, "errors": 0, "skipped": 0}, "score": 5.0, "max_score": 5.0, "dimension": "R"},
        ], "system_errors": []},
        needs_teacher_review=True,
        review_reason="AI 评分失败（尝试 3 次）: JSON 解析失败",
        last_error="AI 返回 JSON 解析失败",
        attempt_count=3,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(cg)
    grant_teaching_fixture(db, course, [student.id])
    db.commit()
    db.refresh(es)
    return es.id


def _setup(client, db_session_factory):
    teacher = create_user(db_session_factory, "gda_t", "teacher", real_name="王老师")
    student = create_user(db_session_factory, "gda_s", "student", real_name="李同学")
    with db_session_factory() as db:
        sub_id = _seed_exam_with_grades(db, teacher, student)
    tok, _ = login(client, "gda_t")
    return tok, sub_id


def test_grade_detail_ai_context_fields(client, db_session_factory):
    """编程题答案应携带 ai_grading：确定性样例数、FR 分、AI 失败原因。"""
    tok, sub_id = _setup(client, db_session_factory)
    exams = client.get(f"{API}/exams", headers=auth_header(tok))
    exam_id = next(item["id"] for item in exams.json()["items"] if item["title"] == "E期末")

    resp = client.get(f"{API}/exams/{exam_id}/grades/{sub_id}", headers=auth_header(tok))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    by_type = {a["question_type"]: a for a in data["answers"]}

    # 客观题：无 CodeGrade → ai_grading 为 None，既有字段不变
    assert by_type["single_choice"]["ai_grading"] is None
    assert by_type["single_choice"]["score"] == 0
    assert by_type["single_choice"]["selected_options"] == ["B"]

    # 编程题：ai_grading 携带完整上下文
    code_answer = by_type["code"]
    ai = code_answer["ai_grading"]
    assert ai is not None
    assert ai["mode"] == "active"
    assert ai["status"] == "review_required"
    assert ai["needs_teacher_review"] is True
    assert "AI 评分失败" in (ai["review_reason"] or "")
    assert "JSON 解析失败" in (ai["last_error"] or "")
    assert ai["attempt_count"] == 3

    # 确定性判题归一化：passed = 7+5+4 = 16；total = 16+1 = 17
    assert ai["tests_passed"] == 16
    assert ai["tests_total"] == 17
    assert ai["functional_score"] == 60
    assert ai["robustness_score"] == 10

    # AI 终止未定分 → 本题分数保持 None（公平性：不按 0 分结算）
    assert code_answer["score"] is None
    assert code_answer["grading_status"] == "completed"

    # 学生代码原文照常返回（此前“显示为空”仅为前端对比度 bug）
    assert "def is_balanced" in code_answer["code_answer"]


def test_grade_detail_without_code_grade(client, db_session_factory):
    """无 CodeGrade 的编程题（legacy）：ai_grading 为 None，不影响其他字段。"""
    teacher = create_user(db_session_factory, "gdn_t", "teacher")
    student = create_user(db_session_factory, "gdn_s", "student")
    with db_session_factory() as db:
        seed_basic_environment(db)
        course = Course(title="C无AI", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        exam = Exam(course_id=course.id, title="E无AI", status="published", duration_minutes=60)
        db.add(exam)
        db.flush()
        code = ExamQuestion(
            exam_id=exam.id, question_type="code", prompt="Q代码",
            correct_answer={}, points=15, order_index=0, grading_mode="legacy",
        )
        db.add(code)
        db.flush()
        es = ExamSubmission(exam_id=exam.id, student_id=student.id, status="graded", score=10)
        db.add(es)
        db.flush()
        db.add(ExamAnswer(
            submission_id=es.id, question_id=code.id,
            code_answer="print(1)", grading_status="completed", score=10,
        ))
        grant_teaching_fixture(db, course, [student.id])
        db.commit()
        db.refresh(es)
        exam_id, sub_id = exam.id, es.id

    tok, _ = login(client, "gdn_t")
    resp = client.get(f"{API}/exams/{exam_id}/grades/{sub_id}", headers=auth_header(tok))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["answers"][0]["ai_grading"] is None
    assert data["answers"][0]["code_answer"] == "print(1)"
