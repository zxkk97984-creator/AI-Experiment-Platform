"""批次 3 验收：判断题、两种考试时间模式、乱序冻结、监考事件与换机写入代次。

验收口径（对应提示词第五节 · 批次 3）：
  1. 判断题出题校验 / 评分 / 开考时冻结分值；
  2. unified 下截止前 1 分钟开考 expires_at == exam.end_at（含"不与 end_at 取 min"的修复前对照）；
  3. per_student 仍拿到完整时长；
  4. 发布后改 timing_mode / shuffle_* / duration_minutes 被 422 拒绝（对照组：同值提交成功）；
  5. 乱序刷新 10 次稳定、两学生顺序不同、关掉乱序后同一份答案得分不变；
  6. 换机后旧设备 PUT/submit 全 409 且库中答案未变（对照组：新设备同样请求成功）；
  7. 断网重连事件补报不重复累加（幂等），监考只读且不自动扣分；
  8. 两种时间模式下 scan_expired_exams 都能自动交卷；
  9. 教师延时留痕且累计不超上限。
"""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import (AuditEvent, Exam, ExamAnswer, ExamEvent, ExamQuestion,
                        ExamSubmission, User)
from app.services.exam_service import (retry_exam_submission, scan_expired_exams,
                                       score_choice_answer)
from app.services.time_utils import as_utc, utc_now
from conftest import auth_header, create_course_db, create_user, login

from teaching_test_support import grant_course_students

API = "/api/v1"
PASSWORD = "Passw0rd!"

_TRUE_FALSE = {
    "question_type": "true_false",
    "prompt": "Python 的 list 是可变的。",
    "options": None,
    "correct_answer": {"correct": ["T"]},
    "points": 10,
}
_CHOICE = {
    "question_type": "single_choice",
    "prompt": "2 + 2 = ?",
    "options": {"A": "4", "B": "5", "C": "6", "D": "7"},
    "correct_answer": {"correct": ["A"]},
    "points": 10,
}


def _seed(client, db_sf, *, tag="a", questions=None, duration=60, start_delta=-5,
          end_delta=30, timing_mode="unified", shuffle_questions=False,
          shuffle_options=False, exam_kind="quiz"):
    """建课程 + 教师 + 两名学生 + 已发布前的考试与题目，返回句柄集合。"""
    teacher_name = f"integrity_teacher_{tag}"
    student_names = [f"integrity_student_{tag}", f"integrity_student2_{tag}"]
    create_user(db_sf, teacher_name, "teacher")
    for name in student_names:
        create_user(db_sf, name, "student")
    teacher, _ = login(client, teacher_name)
    students = [login(client, name)[0] for name in student_names]
    course_id = create_course_db(
        db_sf, teacher_username=teacher_name, title=f"Integrity {tag}",
        status="published", visibility="public",
    )
    for token in students:
        client.post(f"{API}/courses/{course_id}/enroll", headers=auth_header(token))
    now = utc_now()
    created = client.post(f"{API}/exams", headers=auth_header(teacher), json={
        "course_id": course_id,
        "title": f"Integrity Exam {tag}",
        "duration_minutes": duration,
        "start_at": (now + timedelta(minutes=start_delta)).isoformat(),
        "end_at": (now + timedelta(minutes=end_delta)).isoformat(),
        "timing_mode": timing_mode,
        "shuffle_questions": shuffle_questions,
        "shuffle_options": shuffle_options,
        "exam_kind": exam_kind,
    })
    assert created.status_code == 201, created.text
    exam_id = created.json()["id"]
    grant_course_students(db_sf, course_id, student_names)
    question_ids = []
    for item in (questions if isinstance(questions, list) else [questions or _CHOICE]):
        response = client.post(
            f"{API}/exams/{exam_id}/questions", headers=auth_header(teacher), json=item,
        )
        assert response.status_code == 201, response.text
        question_ids.append(response.json()["id"])
    return {
        "teacher": teacher, "student": students[0], "student2": students[1],
        "exam_id": exam_id, "question_ids": question_ids, "course_id": course_id,
    }


def _publish(client, ctx):
    response = client.patch(
        f"{API}/exams/{ctx['exam_id']}", headers=auth_header(ctx["teacher"]),
        json={"status": "published"},
    )
    assert response.status_code == 200, response.text
    return response


def _start(client, ctx, token):
    response = client.post(f"{API}/exams/{ctx['exam_id']}/start", headers=auth_header(token))
    assert response.status_code == 201, response.text
    return response.json()


def _generation(session_payload):
    return session_payload["submission"]["write_generation"]


def _save_batch(client, ctx, token, generation, answers=None, events=None):
    body = {"expected_write_generation": generation, "answers": answers or []}
    if events is not None:
        body["events"] = events
    return client.put(f"{API}/exams/{ctx['exam_id']}/answers", headers=auth_header(token), json=body)


def _submit(client, ctx, token, generation):
    return client.post(f"{API}/exams/{ctx['exam_id']}/submit", headers=auth_header(token),
                       json={"expected_write_generation": generation})


def _error_code(response):
    return response.json()["detail"]["code"]


# ── 1. 判断题 ────────────────────────────────────────────────


def test_true_false_validation_scoring_and_frozen_points(client, db_session_factory):
    ctx = _seed(client, db_session_factory, questions=_TRUE_FALSE)
    teacher_headers = auth_header(ctx["teacher"])
    student_headers = auth_header(ctx["student"])
    student2_headers = auth_header(ctx["student2"])
    question_id = ctx["question_ids"][0]

    # 出题校验：options 必须为空、正确答案恰好一个 T/F、分值必须大于 0
    rejected = [
        {**_TRUE_FALSE, "options": {"A": "是"}},
        {**_TRUE_FALSE, "correct_answer": {"correct": ["A"]}},
        {**_TRUE_FALSE, "correct_answer": {"correct": ["T", "F"]}},
        {**_TRUE_FALSE, "points": 0},
    ]
    for payload in rejected:
        response = client.post(f"{API}/exams/{ctx['exam_id']}/questions",
                               headers=teacher_headers, json=payload)
        assert response.status_code == 422, payload
        assert _error_code(response) == "QUESTION_INVALID"

    _publish(client, ctx)
    started = _start(client, ctx, ctx["student"])
    question = started["questions"][0]
    assert question["question_type"] == "true_false"
    assert not question["options"]
    assert "correct_answer" not in question
    generation = _generation(started)

    saved = _save_batch(client, ctx, ctx["student"], generation, answers=[
        {"question_id": question_id, "selected_options": ["T"], "expected_version": 0},
    ])
    assert saved.json()["results"][0]["ok"] is True, saved.text

    # 冻结：开考后教师把分值改成 1 分，本场考试仍按 10 分冻结
    with db_session_factory() as db:
        db.get(ExamQuestion, question_id).points = 1
        db.commit()

    assert _submit(client, ctx, ctx["student"], generation).status_code == 201
    with db_session_factory() as db:
        submission = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["exam_id"]))
        answer = db.scalar(select(ExamAnswer).where(ExamAnswer.submission_id == submission.id))
        assert submission.score == 10.0
        assert answer.score == 10.0
        assert answer.selected_options == ["T"]
        assert submission.grading_version_map[str(question_id)]["points"] == 10.0

    # 对照组：答错的另一名学生得 0 分（同一条评分路径，条件不成立时失败）
    started2 = _start(client, ctx, ctx["student2"])
    generation2 = _generation(started2)
    _save_batch(client, ctx, ctx["student2"], generation2, answers=[
        {"question_id": question_id, "selected_options": ["F"], "expected_version": 0},
    ])
    assert _submit(client, ctx, ctx["student2"], generation2).status_code == 201
    with db_session_factory() as db:
        second = db.scalar(select(ExamSubmission).where(
            ExamSubmission.exam_id == ctx["exam_id"],
            ExamSubmission.student_id == db.scalar(
                select(User.id).where(User.username == "integrity_student2_a")),
        ))
        assert second.score == 0.0


def test_retry_scores_fill_blank_by_fill_blank_rules_not_choice_rules(client, db_session_factory):
    """分发器锁行为：review_required 重评填空题必须按填空规则算分。

    修复前 retry_exam_submission 对非代码题统一调用 score_choice_answer：填空题的
    correct_answer 没有 "correct" 键、selected_options 为空 → 空集 == 空集 → 直接给满分；
    修复后走 score_non_code_answer → 按空等分，只答对 1/2 空 → 5.0。
    """
    fill = {
        "question_type": "fill_blank",
        "prompt": "函数关键字是 [[blank:first]]，空值是 [[blank:second]]。",
        "correct_answer": {"blanks": [
            {"id": "first", "accepted_answers": ["def"], "case_sensitive": False},
            {"id": "second", "accepted_answers": ["None"], "case_sensitive": False},
        ]},
        "points": 10,
    }
    ctx = _seed(client, db_session_factory, tag="retryfill", questions=fill,
                start_delta=-5, end_delta=60)
    _publish(client, ctx)
    started = _start(client, ctx, ctx["student"])
    generation = _generation(started)
    _save_batch(client, ctx, ctx["student"], generation, answers=[{
        "question_id": ctx["question_ids"][0],
        "text_answers": {"first": "def", "second": "wrong"},
        "expected_version": 0,
    }])

    with db_session_factory() as db:
        submission = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["exam_id"]))
        answer = db.scalar(select(ExamAnswer).where(ExamAnswer.submission_id == submission.id))
        question = db.get(ExamQuestion, ctx["question_ids"][0])
        # 修复前的实际行为（旧分发器调用的就是它）：空集 == 空集 → 错给满分
        assert score_choice_answer(question, answer.selected_options, points=10.0) == 10.0
        answer.grading_status = "system_error"
        answer.score = None
        answer.system_error = "模拟判题基础设施错误"
        submission.status = "review_required"
        submission.review_reason = "存在系统错误答案"
        db.commit()
        submission_id, answer_id = submission.id, answer.id

    with db_session_factory() as db:
        actor = db.scalar(select(User).where(User.username == "integrity_teacher_retryfill"))
        submission = retry_exam_submission(submission_id, [answer_id], actor, db)
        assert submission.status == "graded"
        assert submission.score == 5.0
        answer = db.get(ExamAnswer, answer_id)
        assert answer.grading_status == "completed"
        assert answer.score == 5.0  # 修复后：2 空答对 1 空 → 5.0（不是修复前的 10.0）


# ── 2./3. 两种时间模式 ───────────────────────────────────────


def test_unified_late_entry_expires_at_exam_end_at(client, db_session_factory):
    """截止前 1 分钟开考：到期时刻收敛到考试结束时间（G-2）。"""
    ctx = _seed(client, db_session_factory, tag="unified", duration=60,
                start_delta=-5, end_delta=1, timing_mode="unified")
    _publish(client, ctx)
    started = _start(client, ctx, ctx["student"])
    expires = as_utc(datetime.fromisoformat(started["submission"]["expires_at"]))
    started_at = as_utc(datetime.fromisoformat(started["submission"]["started_at"]))
    with db_session_factory() as db:
        exam_end = as_utc(db.get(Exam, ctx["exam_id"]).end_at)
    assert expires == exam_end
    # 修复前对照：旧公式（入场 + 完整时长，不与 end_at 取 min）会越过考试结束时间。
    naive_before_fix = started_at + timedelta(minutes=60)
    assert naive_before_fix > exam_end
    assert (naive_before_fix - exam_end) > timedelta(minutes=50)


def test_per_student_late_entry_keeps_full_duration(client, db_session_factory):
    """per_student：进入后拿完整时长，此时 end_at 只是「最晚可开考时间」。"""
    ctx = _seed(client, db_session_factory, tag="perstudent", duration=60,
                start_delta=-5, end_delta=1, timing_mode="per_student")
    _publish(client, ctx)
    before = utc_now()
    started = _start(client, ctx, ctx["student"])
    expires = as_utc(datetime.fromisoformat(started["submission"]["expires_at"]))
    with db_session_factory() as db:
        exam_end = as_utc(db.get(Exam, ctx["exam_id"]).end_at)
    assert expires >= before + timedelta(minutes=59, seconds=50)
    assert expires > exam_end


def test_published_exam_locks_timing_mode_shuffle_and_duration(client, db_session_factory):
    ctx = _seed(client, db_session_factory, tag="locked", duration=60, end_delta=120)
    teacher_headers = auth_header(ctx["teacher"])
    _publish(client, ctx)
    for change in ({"timing_mode": "per_student"}, {"shuffle_questions": True},
                   {"shuffle_options": True}, {"duration_minutes": 30}):
        response = client.patch(f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json=change)
        assert response.status_code == 422, (change, response.text)
        assert _error_code(response) == "EXAM_SETTING_LOCKED"
    # 对照组：同值提交（未发生变化）必须放行，否则教师连保存都会失败
    unchanged = client.patch(
        f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers,
        json={"timing_mode": "unified", "shuffle_questions": False,
              "shuffle_options": False, "duration_minutes": 60,
              "show_score_after_grading": True},
    )
    assert unchanged.status_code == 200, unchanged.text


# ── 5. 乱序 ──────────────────────────────────────────────────


def test_shuffle_frozen_persisted_and_stable_across_refreshes(client, db_session_factory):
    questions = [
        {**_CHOICE, "prompt": f"2 + {index} = ?", "order_index": index, "points": 10}
        for index in range(1, 6)
    ]
    ctx = _seed(client, db_session_factory, tag="shuffle", questions=questions,
                start_delta=-5, end_delta=120, shuffle_questions=True, shuffle_options=True)
    _publish(client, ctx)
    first = _start(client, ctx, ctx["student"])
    second = _start(client, ctx, ctx["student2"])

    def order_of(payload):
        return [
            (question["id"], tuple((question["options"] or {}).keys()))
            for question in payload["questions"]
        ]

    order_first = order_of(first)
    order_second = order_of(second)
    assert sorted(item[0] for item in order_first) == sorted(ctx["question_ids"])
    assert order_first != order_second  # 两名学生种子不同，顺序不同

    for _ in range(10):  # 刷新 10 次顺序不变
        refreshed = client.get(f"{API}/exams/{ctx['exam_id']}/session",
                               headers=auth_header(ctx["student"]))
        assert refreshed.status_code == 200, refreshed.text
        assert order_of(refreshed.json()) == order_first

    with db_session_factory() as db:
        submission = db.scalar(select(ExamSubmission).where(
            ExamSubmission.exam_id == ctx["exam_id"],
            ExamSubmission.student_id == db.scalar(
                select(User.id).where(User.username == "integrity_student_shuffle")),
        ))
        assert [int(item) for item in submission.question_order] == [
            item[0] for item in order_first
        ]
        for question_id, keys in order_first:
            assert submission.option_order[str(question_id)] == list(keys)


def test_answer_scoring_is_unchanged_with_option_shuffle(client, db_session_factory):
    """乱序只改展示顺序：答案始终按原选项键提交，得分与关闭乱序时一致。"""
    scores = {}
    for shuffle in (False, True):
        tag = f"score{int(shuffle)}"
        ctx = _seed(client, db_session_factory, tag=tag, questions=_CHOICE,
                    start_delta=-5, end_delta=60, shuffle_options=shuffle)
        _publish(client, ctx)
        started = _start(client, ctx, ctx["student"])
        generation = _generation(started)
        _save_batch(client, ctx, ctx["student"], generation, answers=[
            {"question_id": ctx["question_ids"][0], "selected_options": ["A"], "expected_version": 0},
        ])
        assert _submit(client, ctx, ctx["student"], generation).status_code == 201
        with db_session_factory() as db:
            submission = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["exam_id"]))
            scores[shuffle] = submission.score
    assert scores[False] == scores[True] == 10.0


# ── 6. 换机与写入代次 ────────────────────────────────────────


def test_takeover_supersedes_old_device_and_fences_writes(client, db_session_factory):
    ctx = _seed(client, db_session_factory, tag="takeover", start_delta=-5, end_delta=60)
    _publish(client, ctx)
    student_headers = auth_header(ctx["student"])
    started = _start(client, ctx, ctx["student"])
    question_id = ctx["question_ids"][0]
    original_expires = started["submission"]["expires_at"]
    assert _generation(started) == 1

    first_save = client.put(
        f"{API}/exams/{ctx['exam_id']}/answers/{question_id}", headers=student_headers,
        json={"selected_options": ["A"], "expected_write_generation": 1},
    )
    assert first_save.status_code == 201, first_save.text

    takeover = client.post(f"{API}/exams/{ctx['exam_id']}/takeover", headers=student_headers,
                           json={"current_password": PASSWORD, "expected_write_generation": 1,
                                 "device_id": "device-b"})
    assert takeover.status_code == 200, takeover.text
    body = takeover.json()
    assert body["submission"]["write_generation"] == 2
    assert body["submission"]["device_token"]
    # 剩余时间不重置（序列化格式与 session 载荷不同，比较解析后的时刻）
    assert as_utc(datetime.fromisoformat(body["submission"]["expires_at"])) == as_utc(
        datetime.fromisoformat(original_expires))
    assert [item["selected_options"] for item in body["saved_answers"]] == [["A"]]

    def snapshot():
        with db_session_factory() as db:
            submission = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["exam_id"]))
            answer = db.scalar(select(ExamAnswer).where(ExamAnswer.submission_id == submission.id))
            return submission.status, submission.write_generation, answer.selected_options, answer.version

    before = snapshot()

    # 旧设备：单题保存、批量保存、交卷全部 409，且一个字节都没写进库
    old_single = client.put(
        f"{API}/exams/{ctx['exam_id']}/answers/{question_id}", headers=student_headers,
        json={"selected_options": ["B"], "expected_write_generation": 1},
    )
    old_batch = _save_batch(client, ctx, ctx["student"], 1, answers=[
        {"question_id": question_id, "selected_options": ["B"], "expected_version": 1},
    ])
    old_submit = _submit(client, ctx, ctx["student"], 1)
    assert old_single.status_code == 409 and _error_code(old_single) == "DEVICE_SUPERSEDED"
    assert old_batch.status_code == 409 and _error_code(old_batch) == "DEVICE_SUPERSEDED"
    assert old_submit.status_code == 409 and _error_code(old_submit) == "DEVICE_SUPERSEDED"
    assert snapshot() == before
    with db_session_factory() as db:
        rejected = db.scalars(select(ExamEvent).where(ExamEvent.kind == "autosave_rejected")).all()
        assert rejected, "代次不匹配必须落 autosave_rejected 事件"

    # 对照组：同一台新设备带当前代次做同样的请求全部成功
    new_single = client.put(
        f"{API}/exams/{ctx['exam_id']}/answers/{question_id}", headers=student_headers,
        json={"selected_options": ["B"], "expected_write_generation": 2},
    )
    assert new_single.status_code == 201, new_single.text
    assert _save_batch(client, ctx, ctx["student"], 2, answers=[
        {"question_id": question_id, "selected_options": ["B"], "expected_version": 2},
    ]).json()["results"][0]["ok"] is True
    assert _submit(client, ctx, ctx["student"], 2).status_code == 201


def test_write_generation_is_required_on_every_write_endpoint(client, db_session_factory):
    ctx = _seed(client, db_session_factory, tag="required", start_delta=-5, end_delta=60)
    _publish(client, ctx)
    student_headers = auth_header(ctx["student"])
    _start(client, ctx, ctx["student"])
    question_id = ctx["question_ids"][0]

    missing_single = client.put(f"{API}/exams/{ctx['exam_id']}/answers/{question_id}",
                                headers=student_headers, json={"selected_options": ["A"]})
    missing_batch = client.put(f"{API}/exams/{ctx['exam_id']}/answers", headers=student_headers,
                               json={"answers": [{"question_id": question_id,
                                                  "selected_options": ["A"]}]})
    missing_submit = client.post(f"{API}/exams/{ctx['exam_id']}/submit", headers=student_headers,
                                 json={})
    assert missing_single.status_code == 422
    assert _error_code(missing_single) == "WRITE_GENERATION_REQUIRED"
    assert missing_batch.status_code == 422
    assert missing_submit.status_code == 422
    with db_session_factory() as db:
        submission = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["exam_id"]))
        assert submission.status == "started"
        assert submission.write_generation == 1
        assert db.scalars(select(ExamAnswer)).all() == []


# ── 7. 监考事件（幂等补报 + 只读监考视图）────────────────────


def _event(kind, when, duration_ms=None):
    return {"kind": kind, "client_ts": when.isoformat(), "duration_ms": duration_ms}


def test_reconnect_event_replay_does_not_double_count_and_proctor_is_read_only(
        client, db_session_factory):
    ctx = _seed(client, db_session_factory, tag="events", start_delta=-5, end_delta=60)
    _publish(client, ctx)
    student_headers = auth_header(ctx["student"])
    teacher_headers = auth_header(ctx["teacher"])
    started = _start(client, ctx, ctx["student"])
    generation = _generation(started)
    question_id = ctx["question_ids"][0]
    _save_batch(client, ctx, ctx["student"], generation, answers=[
        {"question_id": question_id, "selected_options": ["A"], "expected_version": 0},
    ])

    base = utc_now() - timedelta(seconds=60)
    events = [
        _event("blur", base),
        _event("focus", base + timedelta(seconds=5), 5000),
        _event("blur", base + timedelta(seconds=20)),
        _event("focus", base + timedelta(seconds=27), 7000),
        _event("reconnect", base + timedelta(seconds=30)),
    ]
    first = _save_batch(client, ctx, ctx["student"], generation, events=events)
    assert first.status_code == 200, first.text
    assert first.json()["events_recorded"] == 5

    def counters():
        with db_session_factory() as db:
            submission = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["exam_id"]))
            return (submission.blur_count, submission.away_total_ms,
                    submission.device_switch_count, submission.last_saved_at)

    after_first = counters()
    assert after_first[:3] == (2, 12000, 0)

    # 断网重连：整批原样补报（client_ts 不变）→ 全部命中幂等去重，计数不重复累加
    replay = _save_batch(client, ctx, ctx["student"], generation, events=events)
    assert replay.status_code == 200
    assert replay.json()["events_recorded"] == 0
    assert counters()[:3] == (2, 12000, 0)
    with db_session_factory() as db:
        assert len(db.scalars(select(ExamEvent)).all()) == 5
        assert db.scalars(select(ExamEvent.ip_hash)).first() is not None  # IP/UA 只存 HMAC 摘要

    # 只读监考视图：不清屏、不扣分、不自动交卷
    proctor = client.get(f"{API}/exams/{ctx['exam_id']}/proctor", headers=teacher_headers)
    assert proctor.status_code == 200, proctor.text
    overview = proctor.json()
    assert overview["totals"] == {"students": 1, "in_progress": 1, "blur_count": 2,
                                  "away_total_ms": 12000, "device_switch_count": 0}
    row = overview["students"][0]
    assert (row["blur_count"], row["away_total_ms"], row["status"]) == (2, 12000, "started")
    assert {event["kind"] for event in overview["events"]} >= {"blur", "focus", "reconnect"}
    assert all("ip" not in event or event.get("ip") is None for event in overview["events"])
    with db_session_factory() as db:
        submission = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["exam_id"]))
        assert submission.status == "started"
        answer = db.scalar(select(ExamAnswer).where(ExamAnswer.submission_id == submission.id))
        assert answer.score is None and answer.grading_status == "pending"

    # 学生看不了监考视图（权限对照）
    forbidden = client.get(f"{API}/exams/{ctx['exam_id']}/proctor", headers=student_headers)
    assert forbidden.status_code == 403


# ── 8. 两种模式到时自动交卷 ──────────────────────────────────


def test_scan_expired_exams_auto_submits_in_both_timing_modes(client, db_session_factory):
    for mode in ("unified", "per_student"):
        ctx = _seed(client, db_session_factory, tag=f"scan{mode}", start_delta=-5, end_delta=120,
                    timing_mode=mode)
        _publish(client, ctx)
        started = _start(client, ctx, ctx["student"])
        generation = _generation(started)
        _save_batch(client, ctx, ctx["student"], generation, answers=[
            {"question_id": ctx["question_ids"][0], "selected_options": ["A"], "expected_version": 0},
        ])
        with db_session_factory() as db:
            submission = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["exam_id"]))
            submission.expires_at = utc_now() - timedelta(seconds=1)
            submission_id = submission.id
            db.commit()
            metrics = scan_expired_exams(db, utc_now())
            assert metrics["expired_claimed"] >= 1
            assert metrics["auto_submitted"] >= 1
            db.refresh(submission)
            assert submission.status in ("submitted", "grading", "graded")
            assert submission.submission_reason == "time_expired"
            answer = db.scalar(select(ExamAnswer).where(ExamAnswer.submission_id == submission_id))
            assert answer.grading_status == "completed" and answer.score == 10.0
        # 下一轮扫描（模拟 5 分钟后的收口）把 grading 汇总为 graded
        with db_session_factory() as db:
            scan_expired_exams(db, utc_now() + timedelta(minutes=6))
            graded = db.get(ExamSubmission, submission_id)
            assert graded.status == "graded"
            assert graded.score == 10.0


# ── 9. 教师延时：留痕且累计不超上限 ──────────────────────────


def test_teacher_extension_is_capped_and_audited(client, db_session_factory):
    ctx = _seed(client, db_session_factory, tag="extend", start_delta=-5, end_delta=60)
    _publish(client, ctx)
    teacher_headers = auth_header(ctx["teacher"])
    started = _start(client, ctx, ctx["student"])
    submission_id = started["id"]
    before = as_utc(datetime.fromisoformat(started["submission"]["expires_at"]))

    extended = client.patch(
        f"{API}/exams/{ctx['exam_id']}/submissions/{submission_id}/extend",
        headers=teacher_headers, json={"minutes": 60},
    )
    assert extended.status_code == 200, extended.text
    after = as_utc(datetime.fromisoformat(extended.json()["expires_at"]))
    assert after == before + timedelta(minutes=60)

    over = client.patch(
        f"{API}/exams/{ctx['exam_id']}/submissions/{submission_id}/extend",
        headers=teacher_headers, json={"minutes": 1},
    )
    assert over.status_code == 422, over.text
    assert _error_code(over) == "EXTENSION_LIMIT_EXCEEDED"

    with db_session_factory() as db:
        audits = db.scalars(select(AuditEvent).where(
            AuditEvent.action == "exam.extend_submission").execution_options(no_scope=True)).all()
        assert len(audits) == 1 and audits[0].target_id == str(submission_id)
        event = db.scalar(select(ExamEvent).where(ExamEvent.kind == "warning"))
        assert event is not None
        assert event.payload["action"] == "extend" and event.payload["minutes"] == 60
