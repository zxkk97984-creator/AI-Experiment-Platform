"""批次 2 验收：未决异议只提示不阻止发布；已发布快照不被改写，修订版 revision+1。"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.models import (
    AcademicTerm,
    Assignment,
    CodeGrade,
    Course,
    GradeAppeal,
    JudgeQuestion,
    Submission,
    TeachingClass,
    User,
)
from app.models.gradebook import GradebookSnapshot
from app.models.teaching import CourseOffering, OfferingEnrollment
from tests.automated.conftest import (
    auth_header,
    create_user,
    login,
    make_judge_question,
    make_rubric,
    make_submission,
)

API = "/api/v1/gradebooks"
APPEALS = "/api/v1/appeals"
REASON = "老师您好，这道题的第三组测试我认为我的解法是正确的，请求复核。" * 2


@pytest.fixture()
def book_case(app, client, db_session_factory):
    from app.api import gradebook as api

    if not any(getattr(route, "path", None) == f"{API}/offerings" for route in app.routes):
        app.include_router(api.router, prefix="/api/v1")

    teacher = create_user(db_session_factory, "appeal-book-teacher", "teacher")
    student = create_user(db_session_factory, "appeal-book-student", "student",
                          real_name="Book Student")
    with db_session_factory() as db:
        term = AcademicTerm(code="APPEAL-TERM", name="Term", start_date=date(2026, 9, 1),
                            end_date=date(2027, 1, 1), status="active")
        db.add(term)
        db.flush()
        course = Course(title="Appeal course", teacher_id=teacher.id, status="published")
        db.add(course)
        db.flush()
        klass = TeachingClass(academic_term_id=term.id, code="APPEAL-1", name="Class",
                              status="active")
        db.add(klass)
        db.flush()
        offering = CourseOffering(course_id=course.id, academic_term_id=term.id,
                                  teaching_class_id=klass.id, teacher_id=teacher.id,
                                  status="active")
        db.add(offering)
        db.flush()
        db.add(OfferingEnrollment(offering_id=offering.id, student_id=student.id, status="active"))
        db.commit()
        offering_id, course_id = offering.id, course.id

    assignment_id = _assignment(db_session_factory, course_id=course_id, offering_id=offering_id)
    question_id = make_judge_question(db_session_factory, assignment_id=assignment_id,
                                      function_name="solve")
    submission_id = make_submission(db_session_factory, question_id=question_id,
                                    student_username=student.username,
                                    grading_status="completed", score=60.0)
    rubric_id = make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    with db_session_factory() as db:
        db.add(CodeGrade(submission_id=submission_id, rubric_id=rubric_id, mode="active",
                         status="completed", functional_score=50.0, robustness_score=5.0,
                         final_score_100=60.0, finished_at=datetime.now(timezone.utc)))
        db.commit()
    return {
        "teacher": teacher, "student": student, "offering": offering_id,
        "submission_id": submission_id,
        "teacher_headers": auth_header(login(client, teacher.username)[0]),
        "student_headers": auth_header(login(client, student.username)[0]),
    }


def _assignment(db_session_factory, *, course_id, offering_id):
    """已发布且绑定授课实例的作业——异议目标必须落在该 offering 范围内。"""
    from tests.automated.conftest import create_assignment_db, seed_basic_environment

    seed_basic_environment(db_session_factory)
    assignment_id = create_assignment_db(db_session_factory, course_id=course_id,
                                         teacher_username="appeal-book-teacher",
                                         title="异议作业", status="published")
    with db_session_factory() as db:
        db.get(Assignment, assignment_id).offering_id = offering_id
        db.commit()
    return assignment_id


def _save_config(client, case):
    response = client.put(
        f"{API}/offerings/{case['offering']}/config", headers=case["teacher_headers"],
        json={"expected_revision": 0, "categories": [{
            "key": "regular", "title": "平时", "weight": "100",
            "columns": [{"key": "main", "title": "main", "weight": "100",
                         "source_kind": "manual"}],
        }]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _publish(client, case):
    draft = client.get(f"{API}/offerings/{case['offering']}",
                       headers=case["teacher_headers"]).json()
    return client.post(f"{API}/offerings/{case['offering']}/publish",
                       headers=case["teacher_headers"],
                       json={"expected_revision": draft["revision"],
                             "expected_preview_hash": draft["draft"]["preview_hash"],
                             "reason": "期末确认"})


def test_pending_appeal_only_warns_and_publish_still_succeeds(client, db_session_factory, book_case):
    saved = _save_config(client, book_case)
    edited = client.patch(
        f"{API}/offerings/{book_case['offering']}/entries",
        headers=book_case["teacher_headers"],
        json={"expected_revision": saved["revision"],
              "entries": [{"student_id": book_case["student"].id, "column_key": "main",
                           "state": "scored", "score": "80"}]},
    )
    assert edited.status_code == 200, edited.text
    first = _publish(client, book_case)
    assert first.status_code == 201, first.text
    first_snapshot = first.json()
    first_revision = first_snapshot["revision"]

    created = client.post(APPEALS, headers=book_case["student_headers"],
                          json={"target_kind": "submission",
                                "target_id": book_case["submission_id"], "reason": REASON})
    assert created.status_code == 201, created.text
    appeal_id = created.json()["id"]

    draft = client.get(f"{API}/offerings/{book_case['offering']}",
                       headers=book_case["teacher_headers"]).json()["draft"]
    assert draft["blockers"] == [], "未决异议不得进入 blockers"
    assert [item["kind"] for item in draft["warnings"]] == ["PENDING_APPEALS"]
    assert draft["warnings"][0]["count"] == 1

    # 有未决异议时再发布一次：必须成功（只提示），且产生新修订版
    second = _publish(client, book_case)
    assert second.status_code == 201, second.text
    assert second.json()["revision"] == first_snapshot["revision"] + 1

    # 改分（走既有 override 通道）→ 未决计数归零
    handled = client.post(f"{APPEALS}/{appeal_id}/handle",
                          headers=book_case["teacher_headers"],
                          json={"action": "adjust", "comment": "复核后改分",
                                "final_score_100": 90.0})
    assert handled.status_code == 200, handled.text
    with db_session_factory() as db:
        assert db.get(Submission, book_case["submission_id"]).score == 90.0
        assert db.get(GradeAppeal, appeal_id).status == "adjusted"

    after = client.get(f"{API}/offerings/{book_case['offering']}",
                       headers=book_case["teacher_headers"]).json()["draft"]
    assert after["warnings"] == [], "异议处理完成后不得继续提示"
    assert after["blockers"] == []

    # 已发布快照绝不被改写：revision 1/2 的 payload 与 hash 必须原样保留
    with db_session_factory() as db:
        snapshots = db.scalars(
            select(GradebookSnapshot).order_by(GradebookSnapshot.revision)
        ).all()
        hashes = {row.revision: row.payload_hash for row in snapshots}
        scores = {row.revision: row.payload["rows"][0]["final_score"] for row in snapshots}
    assert hashes[first_revision] == first_snapshot["payload_hash"], "旧快照 payload_hash 不得变化"
    assert hashes[second.json()["revision"]] == second.json()["payload_hash"]
    assert scores[first_revision] == "80.00", "历史发布快照保留当时的成绩"

    third = _publish(client, book_case)
    assert third.status_code == 201, third.text
    assert third.json()["revision"] == second.json()["revision"] + 1, "改分后发布修订版 revision+1"
    with db_session_factory() as db:
        rows = {row.revision: row.payload_hash for row in db.scalars(
            select(GradebookSnapshot)).all()}
    assert rows[first_revision] == first_snapshot["payload_hash"]
    assert rows[second.json()["revision"]] == second.json()["payload_hash"]
    assert rows[third.json()["revision"]] == third.json()["payload_hash"]


def test_no_pending_appeal_means_no_warning(client, db_session_factory, book_case):
    """对照组：没有未决异议时 warnings 必须为空。"""
    saved = _save_config(client, book_case)
    assert client.patch(
        f"{API}/offerings/{book_case['offering']}/entries",
        headers=book_case["teacher_headers"],
        json={"expected_revision": saved["revision"],
              "entries": [{"student_id": book_case["student"].id, "column_key": "main",
                           "state": "scored", "score": "75"}]},
    ).status_code == 200
    draft = client.get(f"{API}/offerings/{book_case['offering']}",
                       headers=book_case["teacher_headers"]).json()["draft"]
    assert draft["warnings"] == []
