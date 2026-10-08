"""考试 + Jupyter 旧集成测试（适配 v5 模型）"""
from datetime import timedelta

from conftest import auth_header, create_course_db, create_user, login
from app.services.time_utils import utc_now
from teaching_test_support import grant_course_students


def test_exam_submission_and_grade_visibility(client, db_session_factory):
    create_user(db_session_factory, "teacher", "teacher")
    create_user(db_session_factory, "student", "student")
    teacher_token, _ = login(client, "teacher")
    student_token, _ = login(client, "student")

    course_id = create_course_db(
        db_session_factory, teacher_username="teacher", title="深度学习",
        status="published", visibility="public",
    )
    offering_id = grant_course_students(db_session_factory, course_id, ["student"])

    now = utc_now()
    exam_response = client.post(
        "/api/v1/exams",
        headers=auth_header(teacher_token),
        json={
            "course_id": course_id,
            "offering_id": offering_id,
            "title": "期末考试",
            "duration_minutes": 90,
            "start_at": (now - timedelta(minutes=10)).isoformat(),
            "end_at": (now + timedelta(minutes=60)).isoformat(),
        },
    )
    assert exam_response.status_code == 201
    exam_id = exam_response.json()["id"]
    # The legacy API creates shared content; fixture setup explicitly grants
    # this published teaching instance before students are admitted.
    grant_course_students(db_session_factory, course_id, ["student"])

    # 添加题目并发布（创建强制 draft，需显式发布）
    client.post(
        f"/api/v1/exams/{exam_id}/questions",
        headers=auth_header(teacher_token),
        json={
            "question_type": "single_choice",
            "prompt": "1+1=?",
            "options": {"A": "2", "B": "3"},
            "correct_answer": {"correct": ["A"]},
            "points": 10,
        },
    )
    client.patch(
        f"/api/v1/exams/{exam_id}",
        headers=auth_header(teacher_token),
        json={"status": "published"},
    )

    start_response = client.post(
        f"/api/v1/exams/{exam_id}/start",
        headers=auth_header(student_token),
    )
    assert start_response.status_code == 201

    # v5: ExamSubmission 不再有 answers 字段，ExamAnswer 是唯一事实源
    submit_response = client.post(
        f"/api/v1/exams/{exam_id}/submit",
        headers=auth_header(student_token),
        json={"score": 88},
    )
    assert submit_response.status_code in (200, 201)

    grades_response = client.get(
        f"/api/v1/exams/{exam_id}/grades",
        headers=auth_header(teacher_token),
    )
    assert grades_response.status_code == 200


def test_jupyter_entry_and_experiment_records(client, db_session_factory):
    create_user(db_session_factory, "module_teacher", "teacher")
    create_user(db_session_factory, "student", "student")
    teacher_token, _ = login(client, "module_teacher")
    student_token, _ = login(client, "student")

    entry_response = client.get("/api/v1/jupyter/entry", headers=auth_header(student_token))
    assert entry_response.status_code in (200, 503)

    templates_response = client.get(
        "/api/v1/jupyter/templates",
        headers=auth_header(student_token),
    )
    assert templates_response.status_code == 410
    assert templates_response.json()["detail"]["code"] == "JUPYTER_TEMPLATES_RETIRED"

    module_response = client.post(
        "/api/v1/experiments/modules",
        headers=auth_header(teacher_token),
        json={
            "name": "Swin Transformer 可视化",
            "description": "可视化实验",
        },
    )
    assert module_response.status_code == 201
    module_id = module_response.json()["id"]

    # v5: records 仍可通过 GET 列出
    records_response = client.get(
        "/api/v1/experiments/records",
        headers=auth_header(student_token),
    )
    assert records_response.status_code == 200
