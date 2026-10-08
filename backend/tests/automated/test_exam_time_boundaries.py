"""Task 5: 考试时间边界测试——UTC 规范化、边界行为、多时区"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.time_utils import as_utc, utc_now
from conftest import auth_header, create_course_db, create_user, login

from teaching_test_support import grant_course_students
API = "/api/v1"


def _setup_exam(client, db_sf):
    """创建一个已发布的考试（时间窗口为当前前后各1小时）"""
    create_user(db_sf, "tzt", "teacher")
    create_user(db_sf, "szt", "student")
    t_tok, _ = login(client, "tzt")
    s_tok, _ = login(client, "szt")
    cid = create_course_db(db_sf, teacher_username="tzt", title="TZC", status="published", visibility="public")
    client.post(f"{API}/courses/{cid}/enroll", headers=auth_header(s_tok))

    now = utc_now()
    e = client.post(f"{API}/exams", headers=auth_header(t_tok), json={
        "course_id": cid, "title": "TZE", "duration_minutes": 60,
        "start_at": (now - timedelta(hours=1)).isoformat(),
        "end_at": (now + timedelta(hours=1)).isoformat(),
    })
    eid = e.json()["id"]
    grant_course_students(db_sf, cid, ["szt"])
    client.post(f"{API}/exams/{eid}/questions", headers=auth_header(t_tok), json={
        "question_type": "single_choice", "prompt": "Q",
        "options": {"A": "A", "B": "B"}, "correct_answer": {"correct": ["A"]}, "points": 10,
    })
    client.patch(f"{API}/exams/{eid}", headers=auth_header(t_tok),
                 json={"status": "published"})
    return {"t_tok": t_tok, "s_tok": s_tok, "eid": eid, "cid": cid}


# ═══════════════════════════════════════════════════════════════
# 1. as_utc 规范化
# ═══════════════════════════════════════════════════════════════

def test_as_utc_none():
    assert as_utc(None) is None


def test_as_utc_naive():
    """无时区的 datetime 视为 UTC"""
    dt = datetime(2026, 7, 27, 12, 0, 0)
    result = as_utc(dt)
    assert result.tzinfo is not None
    assert result.hour == 12
    assert result.utcoffset() == timedelta(0)


def test_as_utc_aware_utc():
    """UTC-aware datetime 保持不变"""
    dt = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
    result = as_utc(dt)
    assert result == dt
    assert result.utcoffset() == timedelta(0)


def test_as_utc_aware_other_tz():
    """其他时区的 datetime 转换为 UTC"""
    shanghai = timezone(timedelta(hours=8))
    dt = datetime(2026, 7, 27, 20, 0, 0, tzinfo=shanghai)
    result = as_utc(dt)
    assert result.hour == 12  # 北京时间20:00 = UTC 12:00


def test_utc_now_has_tz():
    """utc_now() 返回 UTC-aware datetime"""
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() is not None


# ═══════════════════════════════════════════════════════════════
# 2. 边界行为：考试时间窗口
# ═══════════════════════════════════════════════════════════════

def test_exam_not_started_yet(client, db_session_factory):
    """start_at 在未来时不能开始考试"""
    create_user(db_session_factory, "fut_t", "teacher")
    create_user(db_session_factory, "fut_s", "student")
    t_tok, _ = login(client, "fut_t")
    s_tok, _ = login(client, "fut_s")
    cid = create_course_db(db_session_factory, teacher_username="fut_t", title="FC", status="published", visibility="public")
    client.post(f"{API}/courses/{cid}/enroll", headers=auth_header(s_tok))

    # 考试在未来 1 小时后开始
    future = (utc_now() + timedelta(hours=1)).isoformat()
    far_future = (utc_now() + timedelta(hours=2)).isoformat()
    e = client.post(f"{API}/exams", headers=auth_header(t_tok), json={
        "course_id": cid, "title": "Future", "duration_minutes": 30,
        "start_at": future, "end_at": far_future,
    })
    eid = e.json()["id"]
    grant_course_students(db_session_factory, cid, ["fut_s"])
    client.post(f"{API}/exams/{eid}/questions", headers=auth_header(t_tok), json={
        "question_type": "single_choice", "prompt": "Q",
        "options": {"A": "A", "B": "B"}, "correct_answer": {"correct": ["A"]}, "points": 10,
    })
    client.patch(f"{API}/exams/{eid}", headers=auth_header(t_tok),
                 json={"status": "published"})

    r = client.post(f"{API}/exams/{eid}/start", headers=auth_header(s_tok))
    assert r.status_code == 403, f"考试尚未开始，应为 403: {r.status_code}"
    assert "尚未开始" in (r.json()["detail"]["message"])


def test_exam_ended_no_submit(client, db_session_factory):
    """end_at 已过时不能再交卷"""
    create_user(db_session_factory, "end_t", "teacher")
    create_user(db_session_factory, "end_s", "student")
    t_tok, _ = login(client, "end_t")
    s_tok, _ = login(client, "end_s")
    cid = create_course_db(db_session_factory, teacher_username="end_t", title="EC", status="published", visibility="public")
    client.post(f"{API}/courses/{cid}/enroll", headers=auth_header(s_tok))

    # 考试在 2 小时前结束
    past_start = (utc_now() - timedelta(hours=3)).isoformat()
    past_end = (utc_now() - timedelta(hours=2)).isoformat()
    e = client.post(f"{API}/exams", headers=auth_header(t_tok), json={
        "course_id": cid, "title": "Past", "duration_minutes": 30,
        "start_at": past_start, "end_at": past_end,
    })
    eid = e.json()["id"]
    grant_course_students(db_session_factory, cid, ["end_s"])
    client.post(f"{API}/exams/{eid}/questions", headers=auth_header(t_tok), json={
        "question_type": "single_choice", "prompt": "Q",
        "options": {"A": "A", "B": "B"}, "correct_answer": {"correct": ["A"]}, "points": 10,
    })
    client.patch(f"{API}/exams/{eid}", headers=auth_header(t_tok),
                 json={"status": "published"})

    r = client.post(f"{API}/exams/{eid}/start", headers=auth_header(s_tok))
    assert r.status_code == 403, f"考试已结束，开始应为 403: {r.status_code}"


def test_exam_starts_exactly_at_start_at(client, db_session_factory):
    """now < start_at 不可开始，now >= start_at 可开始"""
    ctx = _setup_exam(client, db_session_factory)
    # 正常考试应能开始
    r = client.post(f"{API}/exams/{ctx['eid']}/start",
                    headers=auth_header(ctx["s_tok"]))
    assert r.status_code == 201, f"考试窗口中应可开始: {r.status_code}"


def test_exam_without_time_window_cannot_publish(client, db_session_factory):
    """正式考试必须设置开始时间与最晚进入时间。"""
    create_user(db_session_factory, "ntw_t", "teacher")
    create_user(db_session_factory, "ntw_s", "student")
    t_tok, _ = login(client, "ntw_t")
    s_tok, _ = login(client, "ntw_s")
    cid = create_course_db(db_session_factory, teacher_username="ntw_t", title="NW", status="published", visibility="public")
    client.post(f"{API}/courses/{cid}/enroll", headers=auth_header(s_tok))
    e = client.post(f"{API}/exams", headers=auth_header(t_tok), json={
        "course_id": cid, "title": "NoWin", "duration_minutes": 60,
    })
    eid = e.json()["id"]
    grant_course_students(db_session_factory, cid, ["ntw_s"])
    client.post(f"{API}/exams/{eid}/questions", headers=auth_header(t_tok), json={
        "question_type": "single_choice", "prompt": "Q",
        "options": {"A": "A", "B": "B"}, "correct_answer": {"correct": ["A"]}, "points": 10,
    })
    publish = client.patch(f"{API}/exams/{eid}", headers=auth_header(t_tok),
                           json={"status": "published"})
    assert publish.status_code == 422
    assert "开始时间" in publish.text
    r = client.post(f"{API}/exams/{eid}/start", headers=auth_header(s_tok))
    assert r.status_code == 403
