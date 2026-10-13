"""G-15 / G-16 锁现状回归（本批不修语义，只把当前行为写进测试）。

G-15：exam_grading.finalize_if_ready 按 0.1 收口父级总分，
      promote_review_required_if_complete 按 0.01 收口——同一组答案分走两条路径结果不同。
G-16：父级 submission.score 仍是「逐题手动改分后由答案分求和」的普通 ORM 写入，
      并发交错不在本批修复范围；这里锁住串行语义与 CAS 栅栏。
"""
from decimal import Decimal

from sqlalchemy import select

from app.models import (Course, Exam, ExamAnswer, ExamGrade, ExamQuestion,
                        ExamSubmission, User)
from app.security import hash_password
from app.services.exam_grading import finalize_if_ready, promote_review_required_if_complete
from app.services.time_utils import utc_now

API = "/api/v1"


def _submission_with_scores(db_session_factory, *, status, scores, tag):
    """构造一份答案分已定的考试提交（G-15 用，绕过 API 只测汇总收口精度）。"""
    from datetime import timedelta
    with db_session_factory() as db:
        teacher = User(username=f"g_teacher_{tag}", real_name="T", role="teacher",
                       status="active", password_hash=hash_password("Passw0rd!"))
        student = User(username=f"g_student_{tag}", real_name="S", role="student",
                       status="active", password_hash=hash_password("Passw0rd!"))
        db.add_all([teacher, student])
        db.flush()
        course = Course(title=f"G {tag}", status="published", visibility="public",
                        teacher_id=teacher.id)
        db.add(course)
        db.flush()
        now = utc_now()
        exam = Exam(course_id=course.id, title=f"G {tag}", status="published",
                    duration_minutes=60, start_at=now, end_at=now + timedelta(minutes=60))
        db.add(exam)
        db.flush()
        submission = ExamSubmission(
            exam_id=exam.id, student_id=student.id, status=status,
            started_at=now, expires_at=now + timedelta(minutes=60), submitted_at=now,
        )
        db.add(submission)
        db.flush()
        for index, score in enumerate(scores):
            question = ExamQuestion(
                exam_id=exam.id, question_type="single_choice", prompt=f"Q{index}",
                options={"A": "对", "B": "错"}, correct_answer={"correct": ["A"]},
                points=10, order_index=index,
            )
            db.add(question)
            db.flush()
            db.add(ExamAnswer(submission_id=submission.id, question_id=question.id,
                              selected_options=["A"], score=score, grading_status="completed"))
        db.commit()
        return submission.id


def test_g15_parent_total_rounding_differs_between_finalize_and_promote(db_session_factory):
    """G-15 未修：自动汇总按 0.1 收口 → 10.0；受控补救汇总按 0.01 收口 → 10.04。"""
    auto_id = _submission_with_scores(db_session_factory, status="grading",
                                      scores=[4.0, 6.04], tag="auto")
    with db_session_factory() as db:
        result = finalize_if_ready(auto_id, db)
        assert result.outcome.value == "graded"
        assert db.get(ExamSubmission, auto_id).score == 10.0
        assert db.scalar(select(ExamGrade.score).where(ExamGrade.student_id == db.get(
            ExamSubmission, auto_id).student_id)) == 10.0

    promote_id = _submission_with_scores(db_session_factory, status="review_required",
                                         scores=[4.0, 6.04], tag="promote")
    with db_session_factory() as db:
        assert promote_review_required_if_complete(promote_id, db) is True
        assert db.get(ExamSubmission, promote_id).score == float(
            Decimal("10.04").quantize(Decimal("0.01")))


def _graded_choice_submission(client, db_session_factory, tag):
    """走 API 造一份 graded 的考试提交，返回 (teacher_headers, exam_id, submission_id, answer_ids)。"""
    from datetime import timedelta

    from conftest import auth_header, create_course_db, create_user, login

    from teaching_test_support import grant_course_students

    teacher_name = f"g16_teacher_{tag}"
    student_name = f"g16_student_{tag}"
    create_user(db_session_factory, teacher_name, "teacher")
    create_user(db_session_factory, student_name, "student")
    teacher, _ = login(client, teacher_name)
    student_token, _ = login(client, student_name)
    course_id = create_course_db(db_session_factory, teacher_username=teacher_name,
                                 title=f"G16 {tag}", status="published", visibility="public")
    client.post(f"{API}/courses/{course_id}/enroll", headers=auth_header(student_token))
    now = utc_now()
    created = client.post(f"{API}/exams", headers=auth_header(teacher), json={
        "course_id": course_id, "title": f"G16 {tag}", "duration_minutes": 60,
        "start_at": (now - timedelta(minutes=5)).isoformat(),
        "end_at": (now + timedelta(minutes=60)).isoformat(),
    })
    exam_id = created.json()["id"]
    grant_course_students(db_session_factory, course_id, [student_name])
    question_ids = []
    for index in range(2):
        response = client.post(f"{API}/exams/{exam_id}/questions", headers=auth_header(teacher), json={
            "question_type": "single_choice", "prompt": f"Q{index}",
            "options": {"A": "对", "B": "错"}, "correct_answer": {"correct": ["A"]},
            "points": 10, "order_index": index,
        })
        question_ids.append(response.json()["id"])
    client.patch(f"{API}/exams/{exam_id}", headers=auth_header(teacher), json={"status": "published"})
    client.post(f"{API}/exams/{exam_id}/start", headers=auth_header(student_token))
    for question_id in question_ids:
        client.put(f"{API}/exams/{exam_id}/answers/{question_id}",
                   headers=auth_header(student_token),
                   json={"expected_write_generation": 1, "selected_options": ["A"]})
    client.post(f"{API}/exams/{exam_id}/submit", headers=auth_header(student_token),
                json={"expected_write_generation": 1})
    with db_session_factory() as db:
        submission = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == exam_id))
        answers = db.scalars(select(ExamAnswer).where(
            ExamAnswer.submission_id == submission.id)).all()
        answer_ids = [answer.id for answer in answers]
    return auth_header(teacher), exam_id, submission.id, answer_ids


def test_g16_parent_score_is_recomputed_from_answer_scores_with_revision_cas(
        client, db_session_factory):
    """G-16 未修：父级总分 = 逐题答案分之和（普通 ORM 写）；修订号 CAS 仍是唯一并发栅栏。"""
    teacher_headers, exam_id, submission_id, answer_ids = _graded_choice_submission(
        client, db_session_factory, "sum")
    detail_url = f"{API}/exams/{exam_id}/grades/{submission_id}"

    first = client.patch(f"{detail_url}/answers/{answer_ids[0]}/score", headers=teacher_headers,
                         json={"score": 3.0, "reason": "第一题部分正确", "expected_result_revision": 1})
    assert first.status_code == 200, first.text
    assert first.json()["submission"]["score"] == 13.0  # 3 + 10

    # 同一题用过期修订号重发（旧页面）被拒：这是当前唯一的并发栅栏
    stale = client.patch(f"{detail_url}/answers/{answer_ids[0]}/score", headers=teacher_headers,
                         json={"score": 5.0, "reason": "过期页面覆盖", "expected_result_revision": 1})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "RESULT_REVISION_CONFLICT"

    updated = client.patch(f"{detail_url}/answers/{answer_ids[0]}/score", headers=teacher_headers,
                           json={"score": 4.0, "reason": "再次调整第一题", "expected_result_revision": 2})
    assert updated.status_code == 200, updated.text

    # 另一题有各自的修订号：父级总分是全部答案分之和
    second = client.patch(f"{detail_url}/answers/{answer_ids[1]}/score", headers=teacher_headers,
                          json={"score": 7.5, "reason": "第二题部分正确", "expected_result_revision": 1})
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["submission"]["score"] == 11.5  # 4 + 7.5
    with db_session_factory() as db:
        assert db.get(ExamSubmission, submission_id).score == 11.5
        grade = db.scalar(select(ExamGrade).where(
            ExamGrade.exam_id == exam_id,
            ExamGrade.student_id == db.get(ExamSubmission, submission_id).student_id,
        ))
        assert grade is not None and grade.score == 11.5
