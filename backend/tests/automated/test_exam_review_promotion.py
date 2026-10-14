"""review_required 受控补救——promote_review_required_if_complete

覆盖：
- 全部答案有分 + 无在途 AI 评分 → 提升 graded、清复核标记、upsert ExamGrade
- 有答案缺分 / active CodeGrade 未完成 → 拒绝提升，父状态不变
- 幂等：已 graded 后再次调用为 False
- API 级：工作台覆盖评分（override）触发补救，父级收口
"""
from datetime import datetime, timezone

from conftest import grant_teaching_fixture, auth_header, create_user, login, seed_basic_environment

from app.models import (
    CodeGrade, Course, Exam, ExamAnswer, ExamGrade, ExamQuestion, ExamSubmission, QuestionRubric,
)
from app.services.exam_grading import promote_review_required_if_complete

API = "/api/v1"


def _seed_review_required(db, teacher, student):
    """建 review_required 提交：单选(10分, 已得10) + active 编程题(20分, 无分)。

    返回 (submission_id, code_answer_id, code_question_id)。
    """
    seed_basic_environment(db)
    course = Course(title="C补救", status="published", teacher_id=teacher.id)
    db.add(course)
    db.flush()
    exam = Exam(course_id=course.id, title="E补救", status="published", duration_minutes=60)
    db.add(exam)
    db.flush()
    choice = ExamQuestion(
        exam_id=exam.id, question_type="single_choice", prompt="Q单选",
        options={"A": "a", "B": "b"}, correct_answer={"correct": ["A"]},
        points=10, order_index=0,
    )
    code = ExamQuestion(
        exam_id=exam.id, question_type="code", prompt="Q代码", correct_answer={},
        points=20, order_index=1, grading_mode="active",
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
    es = ExamSubmission(
        exam_id=exam.id, student_id=student.id, status="review_required",
        review_reason="AI 评分终止需人工复核",
        review_required_at=datetime.now(timezone.utc),
        grading_version_map={
            str(choice.id): {"version_id": None, "points": float(choice.points)},
            str(code.id): {"version_id": None, "points": float(code.points)},
        },
    )
    db.add(es)
    db.flush()
    ans_choice = ExamAnswer(
        submission_id=es.id, question_id=choice.id,
        selected_options=["A"], grading_status="completed", score=10,
    )
    ans_code = ExamAnswer(
        submission_id=es.id, question_id=code.id,
        code_answer="def f(): pass", grading_status="completed", score=None,
    )
    db.add_all([ans_choice, ans_code])
    grant_teaching_fixture(db, course, [student.id])
    db.commit()
    return es.id, ans_code.id, code.id


def _add_active_cg(db, answer_id, rubric_question_id, *, status, scaled=None, needs_review=False):
    rubric = db.query(QuestionRubric).filter_by(
        exam_question_id=rubric_question_id, status="locked").one()
    cg = CodeGrade(
        exam_answer_id=answer_id, rubric_id=rubric.id, mode="active", status=status,
        functional_score=60, robustness_score=10, scaled_score=scaled,
        needs_teacher_review=needs_review,
    )
    db.add(cg)
    db.commit()
    return cg.id


def _setup(client, db_session_factory):
    teacher = create_user(db_session_factory, "prm_t", "teacher")
    student = create_user(db_session_factory, "prm_s", "student")
    with db_session_factory() as db:
        sub_id, ans_code_id, q_code_id = _seed_review_required(db, teacher, student)
    tok, _ = login(client, "prm_t")
    return tok, sub_id, ans_code_id, q_code_id


def test_promote_blocked_while_codegrade_open(db_session_factory):
    """active CodeGrade 仍在 review_required → 拒绝提升。"""
    teacher = create_user(db_session_factory, "prb_t", "teacher")
    student = create_user(db_session_factory, "prb_s", "student")
    with db_session_factory() as db:
        sub_id, ans_code_id, q_code_id = _seed_review_required(db, teacher, student)
        _add_active_cg(db, ans_code_id, q_code_id, status="review_required")

    with db_session_factory() as db:
        assert promote_review_required_if_complete(sub_id, db) is False
        sub = db.get(ExamSubmission, sub_id)
        assert sub.status == "review_required"
        assert sub.review_reason == "AI 评分终止需人工复核"


def test_promote_blocked_when_answer_unscored(db_session_factory):
    """编程题答案仍无分（即使无 CodeGrade）→ 拒绝提升。"""
    teacher = create_user(db_session_factory, "pru_t", "teacher")
    student = create_user(db_session_factory, "pru_s", "student")
    with db_session_factory() as db:
        sub_id, _ans_id, _q_id = _seed_review_required(db, teacher, student)

    with db_session_factory() as db:
        assert promote_review_required_if_complete(sub_id, db) is False
        assert db.get(ExamSubmission, sub_id).status == "review_required"


def test_promote_finalizes_and_upserts_grade(db_session_factory):
    """全部收齐后提升：graded + 总分 + 清复核字段 + ExamGrade upsert；幂等。"""
    teacher = create_user(db_session_factory, "pro_t", "teacher")
    student = create_user(db_session_factory, "pro_s", "student")
    with db_session_factory() as db:
        sub_id, ans_code_id, q_code_id = _seed_review_required(db, teacher, student)
        _add_active_cg(db, ans_code_id, q_code_id, status="completed", scaled=17.6)
        ans = db.get(ExamAnswer, ans_code_id)
        ans.score = 17.6
        db.commit()

    with db_session_factory() as db:
        assert promote_review_required_if_complete(sub_id, db) is True
        sub = db.get(ExamSubmission, sub_id)
        assert sub.status == "graded"
        assert sub.score == 27.6
        assert sub.review_reason is None
        assert sub.review_required_at is None
        assert sub.graded_at is not None
        grade = db.query(ExamGrade).filter_by(exam_id=sub.exam_id, student_id=sub.student_id).one()
        assert grade.score == 27.6

    # 幂等：已 graded 后再调返回 False，不重复写
    with db_session_factory() as db:
        assert promote_review_required_if_complete(sub_id, db) is False
    with db_session_factory() as db:
        grades = db.query(ExamGrade).filter_by(student_id=student.id).all()
        assert len(grades) == 1


def test_override_endpoint_promotes_parent(client, db_session_factory):
    """API：教师覆盖确认后，卡在 review_required 的提交自动收口。"""
    tok, sub_id, ans_code_id, q_code_id = _setup(client, db_session_factory)
    with db_session_factory() as db:
        cg_id = _add_active_cg(db, ans_code_id, q_code_id, status="review_required",
                               needs_review=True)
        # 覆盖前先把答案补上分（模拟工作台重试已成功的场景）
        db.get(ExamAnswer, ans_code_id).score = 18.4
        db.get(CodeGrade, cg_id).status = "completed"
        db.get(CodeGrade, cg_id).needs_teacher_review = False
        db.commit()

    resp = client.post(
        f"{API}/ai-grading/grades/{cg_id}/override",
        headers=auth_header(tok),
        json={"final_score_100": 92, "reason": "确认 AI 折算分"},
    )
    assert resp.status_code == 200, resp.text

    with db_session_factory() as db:
        sub = db.get(ExamSubmission, sub_id)
        assert sub.status == "graded"
        # 覆盖按题目分值重算折算分：92% × 20 = 18.4；总分 18.4 + 10
        assert sub.score == 28.4
        assert sub.review_reason is None
