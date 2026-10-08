"""作业/考试任务级发布范围：班级 + 白名单 + 排除名单。"""

from datetime import datetime, timedelta, timezone

from conftest import auth_header, create_user, grant_teaching_fixture, login, seed_basic_environment
from app.models import (
    AcademicTerm, Assignment, Chapter, Course, CourseEnrollment,
    CourseTeachingClass, Exam, JudgeQuestion, TeachingClass, TeachingClassStudent,
)

NOW = datetime.now(timezone.utc)


def _seed(db_session_factory):
    seed_basic_environment(db_session_factory)
    from app.models import AssignmentAudienceClass, AssignmentAudienceStudent
    teacher = create_user(db_session_factory, "aud-teacher", "teacher")
    in_class = create_user(db_session_factory, "aud-class-student", "student", real_name="班级学生")
    in_class.student_no = "C001"
    extra = create_user(db_session_factory, "aud-extra-student", "student", real_name="额外学生")
    extra.student_no = "E001"
    excluded = create_user(db_session_factory, "aud-excluded-student", "student", real_name="排除学生")
    excluded.student_no = "X001"
    outsider = create_user(db_session_factory, "aud-outsider-student", "student", real_name="外部学生")
    outsider.student_no = "O001"
    with db_session_factory() as db:
        db.add_all([teacher, in_class, extra, excluded, outsider])
        db.commit()
        course = Course(title="范围测试课", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        term = AcademicTerm(code="AUD-TERM", name="范围学期", start_date=NOW.date(), end_date=NOW.date() + timedelta(days=90), status="active")
        db.add(term)
        db.flush()
        cls = TeachingClass(academic_term_id=term.id, code="AUD01", name="范围班", status="active")
        db.add(cls)
        db.flush()
        db.add_all([
            CourseTeachingClass(course_id=course.id, teaching_class_id=cls.id),
            TeachingClassStudent(teaching_class_id=cls.id, student_id=in_class.id, status="active"),
            TeachingClassStudent(teaching_class_id=cls.id, student_id=excluded.id, status="active"),
            CourseEnrollment(course_id=course.id, student_id=in_class.id, status="enrolled", origin="class"),
            CourseEnrollment(course_id=course.id, student_id=excluded.id, status="enrolled", origin="class"),
        ])
        assignment = Assignment(course_id=course.id, title="范围作业", status="draft")
        db.add(assignment)
        db.flush()
        db.add(JudgeQuestion(assignment_id=assignment.id, title="题", function_name="f", hidden_tests="def test():\n    pass\n", grading_mode="legacy"))
        exam = Exam(course_id=course.id, title="范围考试", status="draft", start_at=NOW, end_at=NOW + timedelta(days=1))
        db.add(exam)
        # 管理员分配授课实例；额外白名单成员也必须先属于该实例。
        grant_teaching_fixture(db, course, student_ids=[in_class.id, extra.id, excluded.id])
        db.commit()
        return {
            "teacher": teacher, "in_class": in_class, "extra": extra,
            "excluded": excluded, "outsider": outsider,
            "course": course, "cls": cls, "assignment": assignment, "exam": exam,
        }


def _update_assignment(client, token, aid, payload):
    return client.patch(f"/api/v1/assignments/{aid}", headers=auth_header(token), json=payload)


def test_assignment_audience_visibility(client, db_session_factory):
    g = _seed(db_session_factory)
    token, _ = login(client, "aud-teacher")
    res = _update_assignment(client, token, g["assignment"].id, {
        "audience_mode": "selected_classes",
        "audience_class_ids": [g["cls"].id],
        "whitelist_student_ids": [g["extra"].id],
        "excluded_student_ids": [g["excluded"].id],
    })
    assert res.status_code == 200, res.text
    assert g["extra"].id in res.json()["whitelist_student_ids"]

    pub = client.post(f"/api/v1/assignments/{g['assignment'].id}/publish", headers=auth_header(token))
    assert pub.status_code == 200, pub.text

    for username, expected in [("aud-class-student", 1), ("aud-extra-student", 1), ("aud-outsider-student", 0)]:
        tok, _ = login(client, username)
        listing = client.get("/api/v1/assignments", headers=auth_header(tok)).json()
        assert listing["total"] == expected, (username, listing)
    excluded_tok, _ = login(client, "aud-excluded-student")
    assert client.get("/api/v1/assignments", headers=auth_header(excluded_tok)).json()["total"] == 0
    detail = client.get(f"/api/v1/assignments/{g['assignment'].id}", headers=auth_header(excluded_tok))
    assert detail.status_code == 403


def test_exam_audience_start_gate(client, db_session_factory):
    g = _seed(db_session_factory)
    token, _ = login(client, "aud-teacher")
    from app.models import ExamQuestion
    with db_session_factory() as db:
        db.add(ExamQuestion(exam_id=g["exam"].id, question_type="single_choice", prompt="1+1?", options={"A": "1", "B": "2"}, correct_answer={"correct": ["B"]}, points=10, order_index=0))
        db.commit()
    res = client.patch(f"/api/v1/exams/{g['exam'].id}", headers=auth_header(token), json={
        "audience_mode": "selected_classes",
        "audience_class_ids": [g["cls"].id],
        "whitelist_student_ids": [g["extra"].id],
        "excluded_student_ids": [g["excluded"].id],
        "status": "published",
    })
    assert res.status_code == 200, res.text

    tok, _ = login(client, "aud-extra-student")
    assert client.post(f"/api/v1/exams/{g['exam'].id}/start", headers=auth_header(tok)).status_code == 201
    out_tok, _ = login(client, "aud-outsider-student")
    assert client.post(f"/api/v1/exams/{g['exam'].id}/start", headers=auth_header(out_tok)).status_code == 404


def test_audience_csv_import(client, db_session_factory):
    g = _seed(db_session_factory)
    token, _ = login(client, "aud-teacher")
    csv_data = "学号,姓名,账号\nE001,额外学生,aud-extra-student\n"
    res = client.post(
        f"/api/v1/assignments/{g['assignment'].id}/audience/import",
        params={"kind": "include"},
        files={"file": ("audience.csv", csv_data.encode("utf-8-sig"), "text/csv")},
        headers=auth_header(token),
    )
    assert res.status_code == 200, res.text
    assert res.json()["created"] == 1

    fetched = client.get(f"/api/v1/assignments/{g['assignment'].id}", headers=auth_header(token)).json()
    assert g["extra"].id in fetched["whitelist_student_ids"]
