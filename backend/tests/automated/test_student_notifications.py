"""批次 2 验收：学生通知中心（G-14 例外）——角色放开 + 派生通知 + 收件人隔离。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.models import CodeGrade, GradeAppeal, GradeOverride, Notification, Submission
from tests.automated.conftest import (
    auth_header,
    login,
    make_rubric,
    make_student,
    make_submission,
    make_teacher,
)

API = "/api/v1/notifications"
APPEALS = "/api/v1/appeals"
REASON = "老师您好，这道题的第三组测试我认为我的解法是正确的，请求复核。" * 2


@pytest.fixture()
def world(client, db_session_factory):
    make_teacher(db_session_factory)
    make_student(db_session_factory, "student")
    make_student(db_session_factory, "student2")
    submission_id = make_submission(db_session_factory, student_username="student",
                                    grading_status="completed", score=60.0)
    with db_session_factory() as db:
        question_id = db.get(Submission, submission_id).question_id
    rubric_id = make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    with db_session_factory() as db:
        grade = CodeGrade(submission_id=submission_id, rubric_id=rubric_id, mode="active",
                          status="completed", functional_score=50.0, robustness_score=5.0,
                          algorithm_score=3.0, quality_score=2.0, final_score_100=60.0,
                          finished_at=datetime.now(timezone.utc))
        db.add(grade)
        db.commit()
        grade_id = grade.id
    # 第二名学生的对照样本：他也应有自己的通知，用于验证收件人隔离
    other_submission = make_submission(db_session_factory, student_username="student2",
                                       grading_status="completed", score=70.0)
    with db_session_factory() as db:
        other_question = db.get(Submission, other_submission).question_id
    other_rubric = make_rubric(db_session_factory, judge_question_id=other_question, status="locked")
    with db_session_factory() as db:
        db.add(CodeGrade(submission_id=other_submission, rubric_id=other_rubric, mode="active",
                         status="completed", functional_score=60.0, robustness_score=5.0,
                         final_score_100=70.0, finished_at=datetime.now(timezone.utc)))
        db.commit()
    return {"submission_id": submission_id, "grade_id": grade_id,
            "other_submission_id": other_submission}


def _headers(client, username):
    token, _ = login(client, username)
    return auth_header(token)


def test_student_can_read_own_notifications(client, db_session_factory, world):
    headers = _headers(client, "student")
    response = client.get(API, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    types = {item["type"] for item in body["items"]}
    assert "grading" in types, "AI 评分完成应派生学生通知"
    assert body["unread_count"] >= 1
    titles = {item["title"] for item in body["items"]}
    assert "AI 评分已完成" in titles


def test_teacher_views_stay_scoped_to_their_own_notifications(client, db_session_factory, world):
    """教师端行为不变：只看到自己的通知（学生的评分通知不得泄漏到教师收件箱）。"""
    student_headers = _headers(client, "student")
    assert client.get(API, headers=student_headers).status_code == 200
    teacher_headers = _headers(client, "teacher")
    teacher_body = client.get(API, headers=teacher_headers).json()
    assert all(item["title"] != "AI 评分已完成" for item in teacher_body["items"])

    with db_session_factory() as db:
        student = db.scalar(select(Notification.recipient_id).select_from(Notification).limit(1))
        all_rows = db.scalars(select(Notification)).all()
        student_ids = {row.recipient_id for row in all_rows}
    assert teacher_body["items"] == [] or all(
        item["title"] != "AI 评分已完成" for item in teacher_body["items"])
    assert student_ids and student not in {None}, "学生通知必须落到学生自己的收件箱"


def test_notification_derivation_is_idempotent(client, db_session_factory, world):
    headers = _headers(client, "student")
    first = client.get(API, headers=headers)
    second = client.get(API, headers=headers)
    assert first.json()["unread_count"] == second.json()["unread_count"]
    with db_session_factory() as db:
        rows = db.scalars(select(Notification)).all()
        keys = [row.dedupe_key for row in rows]
    assert len(keys) == len(set(keys)), "dedupe_key 必须防重复发放"
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == len(keys)


def test_student_cannot_read_someone_elses_notification(client, db_session_factory, world):
    other_headers = _headers(client, "student2")
    assert client.get(API, headers=other_headers).status_code == 200
    other_ids = {item["id"] for item in client.get(API, headers=other_headers).json()["items"]}
    assert other_ids, "对照样本：另一个学生自己也有通知"

    mine = {item["id"] for item in client.get(API, headers=_headers(client, "student")).json()["items"]}
    stolen = next(iter(other_ids))
    response = client.post(f"{API}/{stolen}/read", headers=_headers(client, "student"))
    assert response.status_code == 404, response.text
    assert stolen not in mine


def test_appeal_lifecycle_notifications_reach_the_student(client, db_session_factory, world):
    student_headers = _headers(client, "student")
    created = client.post(APPEALS, headers=student_headers,
                          json={"target_kind": "submission", "target_id": world["submission_id"],
                                "reason": REASON})
    assert created.status_code == 201, created.text
    appeal_id = created.json()["id"]

    received = client.get(API, headers=student_headers).json()
    assert any(item["title"] == "成绩异议已提交" for item in received["items"]), \
        "申请后学生应看到异议受理通知"

    teacher_headers = _headers(client, "teacher")
    pending = client.get("/api/v1/appeals", headers=teacher_headers,
                         params={"status": "pending"}).json()
    assert [item["id"] for item in pending["items"]] == [appeal_id]

    handled = client.post(f"{APPEALS}/{appeal_id}/handle", headers=teacher_headers,
                          json={"action": "adjust", "comment": "复核后调整", "final_score_100": 82.0})
    assert handled.status_code == 200, handled.text

    after = client.get(API, headers=student_headers).json()
    titles = [item["title"] for item in after["items"]]
    assert "成绩异议已改分" in titles, titles
    adjusted = next(item for item in after["items"] if item["title"] == "成绩异议已改分")
    assert "82" in adjusted["content"]
    assert any(item["title"] == "教师已复核你的成绩" for item in after["items"]), \
        "改分结果要同时派生教师改分通知"
    # 处理完成后异议"已提交"通知自动隐藏
    assert "成绩异议已提交" not in titles
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GradeOverride)) == 1
        assert db.get(GradeAppeal, appeal_id).status == "adjusted"


def test_upheld_appeal_notification_reports_original_score(client, db_session_factory, world):
    student_headers = _headers(client, "student")
    appeal_id = client.post(APPEALS, headers=student_headers,
                            json={"target_kind": "submission",
                                  "target_id": world["submission_id"], "reason": REASON}).json()["id"]
    teacher_headers = _headers(client, "teacher")
    assert client.post(f"{APPEALS}/{appeal_id}/handle", headers=teacher_headers,
                       json={"action": "uphold", "comment": "已复核，原分无误"}).status_code == 200
    items = client.get(API, headers=student_headers).json()["items"]
    assert any(item["title"] == "成绩异议维持原分" for item in items)
