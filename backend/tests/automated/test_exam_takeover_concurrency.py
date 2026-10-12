"""两设备并发换机接管：只有一个能提升写入代次。

必须在真 MySQL（InnoDB 行锁）上跑，SQLite 的行锁语义证明不了竞争：
本用例在 SQLite 下 skip，用 `mysql-env.sh export shared` 的 DAI_DATABASE_URL 跑真库。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models import Exam, ExamEvent, ExamSubmission, User
from app.services.exam_service import takeover_exam_session
from app.services.time_utils import utc_now
from conftest import auth_header, create_course_db, create_user, login

from teaching_test_support import grant_course_students

API = "/api/v1"


@pytest.fixture()
def mysql_engine(db_session_factory):
    engine = db_session_factory.kw["bind"]
    if engine.dialect.name != "mysql":
        pytest.skip("并发接管依赖 InnoDB 行锁；SQLite 下运行无证明力（真库复核见报告）")
    return engine


def _published_started_exam(client, db_sf):
    create_user(db_sf, "takeover_teacher", "teacher")
    create_user(db_sf, "takeover_student", "student")
    teacher, _ = login(client, "takeover_teacher")
    student_token, _ = login(client, "takeover_student")
    course_id = create_course_db(db_sf, teacher_username="takeover_teacher", title="Takeover",
                                 status="published", visibility="public")
    client.post(f"{API}/courses/{course_id}/enroll", headers=auth_header(student_token))
    now = utc_now()
    created = client.post(f"{API}/exams", headers=auth_header(teacher), json={
        "course_id": course_id,
        "title": "Takeover Exam",
        "duration_minutes": 60,
        "start_at": (now - timedelta(minutes=5)).isoformat(),
        "end_at": (now + timedelta(minutes=60)).isoformat(),
        "timing_mode": "per_student",
    })
    assert created.status_code == 201, created.text
    exam_id = created.json()["id"]
    grant_course_students(db_sf, course_id, ["takeover_student"])
    question = client.post(f"{API}/exams/{exam_id}/questions", headers=auth_header(teacher), json={
        "question_type": "single_choice", "prompt": "2 + 2 = ?",
        "options": {"A": "4", "B": "5"}, "correct_answer": {"correct": ["A"]}, "points": 10,
    })
    assert question.status_code == 201, question.text
    assert client.patch(f"{API}/exams/{exam_id}", headers=auth_header(teacher),
                        json={"status": "published"}).status_code == 200
    started = client.post(f"{API}/exams/{exam_id}/start", headers=auth_header(student_token))
    assert started.status_code == 201, started.text
    return exam_id


def test_two_devices_concurrent_takeover_only_one_promotes(client, db_session_factory, mysql_engine):
    exam_id = _published_started_exam(client, db_session_factory)
    with db_session_factory() as db:
        assert db.scalar(select(ExamSubmission.write_generation).where(
            ExamSubmission.exam_id == exam_id)) == 1

    barrier = Barrier(2, timeout=20)

    def device(name):
        with db_session_factory() as db:
            exam = db.get(Exam, exam_id)
            student = db.scalar(select(User).where(User.username == "takeover_student"))
            barrier.wait()
            try:
                result = takeover_exam_session(
                    db, exam, student, expected_write_generation=1, device_id=name,
                )
                return ("promoted", result["submission"]["write_generation"])
            except HTTPException as exc:
                db.rollback()
                return ("denied", exc.status_code, exc.detail["code"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(device, name) for name in ("device-a", "device-b")]
        results = [future.result(timeout=60) for future in futures]

    promoted = [item for item in results if item[0] == "promoted"]
    denied = [item for item in results if item[0] == "denied"]
    assert len(promoted) == 1, f"并发接管必须只有一个成功: {results}"
    assert promoted[0][1] == 2
    assert len(denied) == 1, results
    assert denied[0][1:] == (409, "DEVICE_SUPERSEDED")

    with db_session_factory() as db:
        submission = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == exam_id))
        assert submission.write_generation == 2
        assert submission.device_switch_count == 1
        assert submission.active_device_id in ("device-a", "device-b")
        assert len(db.scalars(select(ExamEvent).where(ExamEvent.kind == "device_switch")).all()) == 1
        rejected = db.scalars(select(ExamEvent).where(ExamEvent.kind == "autosave_rejected")).all()
        assert len(rejected) == 1
