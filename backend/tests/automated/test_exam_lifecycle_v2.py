"""考试全链路 v2：服务器状态、完整个人时长、自动保存、填空与讲评公开。"""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import Exam, ExamAnswer, ExamQuestion, ExamSubmission, User
from app.services.time_utils import as_utc, utc_now
from conftest import auth_header, create_course_db, create_user, login

from teaching_test_support import grant_course_students
API = "/api/v1"


def _seed(client, db_sf, *, question=None, duration=60, start_delta=-10, end_delta=30):
    create_user(db_sf, "life_teacher", "teacher")
    create_user(db_sf, "life_student", "student")
    teacher, _ = login(client, "life_teacher")
    student, _ = login(client, "life_student")
    course_id = create_course_db(
        db_sf, teacher_username="life_teacher", title="Lifecycle",
        status="published", visibility="public",
    )
    client.post(f"{API}/courses/{course_id}/enroll", headers=auth_header(student))
    now = utc_now()
    exam = client.post(f"{API}/exams", headers=auth_header(teacher), json={
        "course_id": course_id,
        "title": "Server Clock Exam",
        "duration_minutes": duration,
        "start_at": (now + timedelta(minutes=start_delta)).isoformat(),
        "end_at": (now + timedelta(minutes=end_delta)).isoformat(),
    })
    exam_id = exam.json()["id"]
    grant_course_students(db_sf, course_id, ["life_student"])
    question = question or {
        "question_type": "single_choice",
        "prompt": "2 + 2 = ?",
        "options": {"A": "4", "B": "5"},
        "correct_answer": {"correct": ["A"]},
        "points": 10,
    }
    created_question = client.post(
        f"{API}/exams/{exam_id}/questions", headers=auth_header(teacher), json=question,
    )
    assert created_question.status_code == 201, created_question.text
    return {
        "teacher": teacher, "student": student, "exam_id": exam_id,
        "question_id": created_question.json()["id"], "course_id": course_id,
    }


def test_draft_hidden_and_future_exam_never_exposes_questions(client, db_session_factory):
    ctx = _seed(client, db_session_factory, start_delta=30, end_delta=60)
    student_headers = auth_header(ctx["student"])
    assert client.get(f"{API}/exams", headers=student_headers).json()["total"] == 0
    assert client.get(f"{API}/exams/{ctx['exam_id']}", headers=student_headers).status_code == 403

    published = client.patch(
        f"{API}/exams/{ctx['exam_id']}", headers=auth_header(ctx["teacher"]), json={"status": "published"},
    )
    assert published.status_code == 200, published.text
    summary = client.get(f"{API}/exams", headers=student_headers).json()["items"][0]
    assert summary["student_status"] == "scheduled"
    assert summary["can_start"] is False
    session = client.get(f"{API}/exams/{ctx['exam_id']}/session", headers=student_headers).json()
    assert session["questions"] == []
    assert client.post(f"{API}/exams/{ctx['exam_id']}/start", headers=student_headers).status_code == 403
    assert client.get(f"{API}/exams/{ctx['exam_id']}/questions", headers=student_headers).status_code == 403


def test_late_entry_receives_full_duration_not_global_window_cap(client, db_session_factory):
    ctx = _seed(client, db_session_factory, duration=60, start_delta=-5, end_delta=5)
    client.patch(f"{API}/exams/{ctx['exam_id']}", headers=auth_header(ctx["teacher"]), json={"status": "published"})
    before = utc_now()
    started = client.post(f"{API}/exams/{ctx['exam_id']}/start", headers=auth_header(ctx["student"]))
    assert started.status_code == 201, started.text
    payload = started.json()
    expires_raw = datetime.fromisoformat(payload["expires_at"])
    nested_expires_raw = datetime.fromisoformat(payload["submission"]["expires_at"])
    assert expires_raw.tzinfo is not None
    assert nested_expires_raw.tzinfo is not None
    expires = as_utc(expires_raw)
    assert expires >= before + timedelta(minutes=59, seconds=50)


def test_future_started_submission_is_scheduled_not_in_progress(client, db_session_factory):
    """回归：异常未来 started 提交不得进入答题态，避免 60 分钟考试倒计时显示 817 小时。"""
    ctx = _seed(client, db_session_factory, start_delta=30, end_delta=60)
    client.patch(f"{API}/exams/{ctx['exam_id']}", headers=auth_header(ctx["teacher"]), json={"status": "published"})
    now = utc_now()
    started_at = now + timedelta(minutes=40)  # 在考试窗口内但尚未到点
    with db_session_factory() as db:
        student = db.scalar(select(User).where(User.username == "life_student"))
        assert student is not None
        sub = ExamSubmission(
            exam_id=ctx["exam_id"], student_id=student.id, status="started",
            started_at=started_at, expires_at=started_at + timedelta(minutes=60),
        )
        db.add(sub)
        db.commit()

    session = client.get(f"{API}/exams/{ctx['exam_id']}/session", headers=auth_header(ctx["student"])).json()
    assert session["exam"]["student_status"] == "scheduled"
    assert session["questions"] == []
    assert session["submission"]["status"] == "started"  # 原始状态不篡改，只控制展示


def test_autosave_versions_restore_and_detect_conflicts(client, db_session_factory):
    ctx = _seed(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['exam_id']}", headers=auth_header(ctx["teacher"]), json={"status": "published"})
    headers = auth_header(ctx["student"])
    client.post(f"{API}/exams/{ctx['exam_id']}/start", headers=headers)
    first = client.put(f"{API}/exams/{ctx['exam_id']}/answers", headers=headers, json={"answers": [{
        "question_id": ctx["question_id"], "selected_options": ["A"], "expected_version": 0,
    }]})
    assert first.json()["results"][0]["version"] == 1
    restored = client.get(f"{API}/exams/{ctx['exam_id']}/session", headers=headers).json()["saved_answers"][0]
    assert restored["selected_options"] == ["A"]
    assert restored["version"] == 1
    conflict = client.put(f"{API}/exams/{ctx['exam_id']}/answers", headers=headers, json={"answers": [{
        "question_id": ctx["question_id"], "selected_options": ["B"], "expected_version": 0,
    }]})
    assert conflict.json()["results"][0]["ok"] is False
    assert conflict.json()["results"][0]["code"] == "ANSWER_VERSION_CONFLICT"


def test_deadline_submit_is_idempotent_and_start_retry_restores_existing_attempt(client, db_session_factory):
    ctx = _seed(client, db_session_factory)
    client.patch(f"{API}/exams/{ctx['exam_id']}", headers=auth_header(ctx["teacher"]), json={"status": "published"})
    started = client.post(f"{API}/exams/{ctx['exam_id']}/start", headers=auth_header(ctx["student"]))
    assert started.status_code == 201

    with db_session_factory() as db:
        submission = db.scalar(select(ExamSubmission).where(ExamSubmission.exam_id == ctx["exam_id"]))
        exam = db.get(Exam, ctx["exam_id"])
        # 已经开始后，即使全局最晚进入时间过去，start 重试也应恢复同一记录。
        exam.end_at = utc_now() - timedelta(seconds=1)
        db.commit()
    retried = client.post(f"{API}/exams/{ctx['exam_id']}/start", headers=auth_header(ctx["student"]))
    assert retried.status_code == 201
    assert retried.json()["id"] == started.json()["id"]

    with db_session_factory() as db:
        submission = db.get(ExamSubmission, started.json()["id"])
        submission.expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
    first = client.post(f"{API}/exams/{ctx['exam_id']}/submit", headers=auth_header(ctx["student"]))
    second = client.post(f"{API}/exams/{ctx['exam_id']}/submit", headers=auth_header(ctx["student"]))
    assert first.status_code == second.status_code == 201
    assert first.json()["submission"]["submission_reason"] == "time_expired"
    assert second.json()["id"] == first.json()["id"]


def test_started_exam_allows_public_policy_changes_but_never_shortens_entry_window(client, db_session_factory):
    ctx = _seed(client, db_session_factory)
    teacher_headers = auth_header(ctx["teacher"])
    published = client.patch(f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json={"status": "published"})
    client.post(f"{API}/exams/{ctx['exam_id']}/start", headers=auth_header(ctx["student"]))

    policy = client.patch(f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json={
        "duration_minutes": published.json()["duration_minutes"],
        "start_at": published.json()["start_at"],
        "end_at": published.json()["end_at"],
        "show_score_after_grading": True,
    })
    assert policy.status_code == 200, policy.text
    assert policy.json()["show_score_after_grading"] is True

    shortened = client.patch(f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json={
        "end_at": (utc_now() + timedelta(minutes=1)).isoformat(),
    })
    cleared = client.patch(f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json={"end_at": None})
    assert shortened.status_code == cleared.status_code == 409


def test_fill_blank_partial_scoring_visibility_and_manual_review(client, db_session_factory):
    fill = {
        "question_type": "fill_blank",
        "prompt": "函数关键字是 [[blank:first]]，空值是 [[blank:second]]。",
        "correct_answer": {"blanks": [
            {"id": "first", "accepted_answers": ["def"], "case_sensitive": False},
            {"id": "second", "accepted_answers": ["None", "null"], "case_sensitive": False},
        ]},
        "points": 10,
    }
    ctx = _seed(client, db_session_factory, question=fill)
    teacher_headers = auth_header(ctx["teacher"])
    student_headers = auth_header(ctx["student"])
    assert client.patch(f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json={"status": "published"}).status_code == 200
    started = client.post(f"{API}/exams/{ctx['exam_id']}/start", headers=student_headers).json()
    assert "correct_answer" not in started["questions"][0]
    saved = client.put(f"{API}/exams/{ctx['exam_id']}/answers", headers=student_headers, json={"answers": [{
        "question_id": ctx["question_id"],
        "text_answers": {"first": " DEF ", "second": "wrong"},
        "expected_version": 0,
    }]})
    assert saved.json()["results"][0]["ok"] is True
    submitted = client.post(f"{API}/exams/{ctx['exam_id']}/submit", headers=student_headers).json()
    assert submitted["status"] == "graded"
    assert submitted["score"] is None
    assert submitted["exam"]["max_score"] == 10

    client.patch(f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json={
        "show_score_after_grading": True,
        "show_questions_after_review": True,
        "show_answers_after_review": True,
    })
    score_session = client.get(f"{API}/exams/{ctx['exam_id']}/session", headers=student_headers).json()
    assert score_session["submission"]["score"] == 5.0
    assert score_session["questions"] == []
    assert client.post(f"{API}/exams/{ctx['exam_id']}/review-release", headers=teacher_headers).status_code == 409

    with db_session_factory() as db:
        exam = db.get(Exam, ctx["exam_id"])
        exam.end_at = utc_now() - timedelta(seconds=1)
        db.commit()
    released = client.post(f"{API}/exams/{ctx['exam_id']}/review-release", headers=teacher_headers)
    assert released.status_code == 200, released.text
    review = client.get(f"{API}/exams/{ctx['exam_id']}/session", headers=student_headers).json()
    assert review["visibility"] == {"score": True, "questions": True, "answers": True, "review_released": True}
    assert review["questions"][0]["correct_answer"]["blanks"][0]["accepted_answers"] == ["def"]
    assert review["saved_answers"][0]["text_answers"]["first"] == " DEF "
    assert review["saved_answers"][0]["score"] == 5.0


def test_review_session_exposes_code_reference_solution_after_answers_are_released(client, db_session_factory):
    code = {
        "question_type": "code",
        "prompt": "实现 add(a, b)",
        "correct_answer": {"test_file": "secret evaluator code"},
        "points": 10,
        "starter_code": "def add(a, b):\n    pass",
        "hidden_tests": "from user_code import add\n\ndef test_hidden():\n    assert add(2, 3) == 5",
        "grading_mode": "legacy",
    }
    ctx = _seed(client, db_session_factory, question=code)
    teacher_headers = auth_header(ctx["teacher"])
    student_headers = auth_header(ctx["student"])
    assert client.patch(
        f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json={"status": "published"},
    ).status_code == 200

    before_release = client.get(f"{API}/exams/{ctx['exam_id']}/session", headers=student_headers).json()
    assert before_release["questions"] == []

    with db_session_factory() as db:
        exam = db.get(Exam, ctx["exam_id"])
        question = db.get(ExamQuestion, ctx["question_id"])
        question.reference_solution = "def add(a, b):\n    return a + b"
        exam.review_released_at = utc_now()
        exam.show_questions_after_review = True
        exam.show_answers_after_review = True
        db.commit()

    review = client.get(f"{API}/exams/{ctx['exam_id']}/session", headers=student_headers).json()
    assert review["questions"][0]["reference_solution"] == "def add(a, b):\n    return a + b"
    assert "correct_answer" not in review["questions"][0]
    assert "test_file" not in review["questions"][0]

    with db_session_factory() as db:
        question = db.get(ExamQuestion, ctx["question_id"])
        question.reference_solution = None
        db.commit()

    without_reference_response = client.get(f"{API}/exams/{ctx['exam_id']}/session", headers=student_headers)
    without_reference = without_reference_response.json()
    assert without_reference["questions"][0]["reference_solution"] is None
    assert "secret evaluator code" not in without_reference_response.text


def test_review_session_hides_question_scores_until_score_is_released(client, db_session_factory):
    ctx = _seed(client, db_session_factory)
    teacher_headers = auth_header(ctx["teacher"])
    student_headers = auth_header(ctx["student"])
    assert client.patch(
        f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json={"status": "published"},
    ).status_code == 200
    assert client.post(f"{API}/exams/{ctx['exam_id']}/start", headers=student_headers).status_code == 201
    assert client.put(
        f"{API}/exams/{ctx['exam_id']}/answers/{ctx['question_id']}",
        headers=student_headers,
        json={"selected_options": ["A"]},
    ).status_code == 201
    assert client.post(f"{API}/exams/{ctx['exam_id']}/submit", headers=student_headers).status_code == 201

    with db_session_factory() as db:
        exam = db.get(Exam, ctx["exam_id"])
        answer = db.scalar(select(ExamAnswer).where(ExamAnswer.question_id == ctx["question_id"]))
        answer.score = 7.0
        answer.manual_score_reason = "教师内部改分说明"
        exam.end_at = utc_now() - timedelta(seconds=1)
        exam.show_questions_after_review = True
        exam.show_answers_after_review = True
        exam.show_score_after_grading = False
        db.commit()

    assert client.post(f"{API}/exams/{ctx['exam_id']}/review-release", headers=teacher_headers).status_code == 200
    review = client.get(f"{API}/exams/{ctx['exam_id']}/session", headers=student_headers).json()
    assert review["visibility"] == {"score": False, "questions": True, "answers": True, "review_released": True}
    saved = review["saved_answers"][0]
    assert "score" not in saved
    assert "manual_score_reason" not in saved


def test_teacher_force_submit_records_reason(client, db_session_factory):
    ctx = _seed(client, db_session_factory)
    teacher_headers = auth_header(ctx["teacher"])
    student_headers = auth_header(ctx["student"])
    client.patch(f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json={"status": "published"})
    started = client.post(f"{API}/exams/{ctx['exam_id']}/start", headers=student_headers).json()
    forced = client.post(
        f"{API}/exams/{ctx['exam_id']}/submissions/{started['id']}/force-submit", headers=teacher_headers,
    )
    assert forced.status_code == 200
    assert forced.json()["submission_reason"] == "teacher_forced"
    with db_session_factory() as db:
        submission = db.scalar(select(ExamSubmission).where(ExamSubmission.id == started["id"]))
        assert submission.status == "graded"


def test_active_student_sample_run_executes_only_public_cases(
    client, db_session_factory, monkeypatch,
):
    code_question = {
        "question_type": "code",
        "prompt": "实现 add(a, b)",
        "correct_answer": {"test_file": "def add(a, b):\n    return a + b"},
        "points": 10,
        "starter_code": "def add(a, b):\n    pass",
        "public_cases": [{"args": [1, 2], "expected": 3}],
        "hidden_tests": "from user_code import add\n\ndef test_hidden():\n    assert add(2, 3) == 5",
        "grading_mode": "legacy",
    }
    ctx = _seed(client, db_session_factory, question=code_question)
    teacher_headers = auth_header(ctx["teacher"])
    student_headers = auth_header(ctx["student"])
    assert client.patch(
        f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json={"status": "published"},
    ).status_code == 200
    assert client.post(f"{API}/exams/{ctx['exam_id']}/start", headers=student_headers).status_code == 201

    def fake_runner(workdir, *_args, **_kwargs):
        rendered_tests = (workdir / "test_public_cases.py").read_text(encoding="utf-8")
        assert "assert add(1, 2) == 3" in rendered_tests
        assert "test_hidden" not in rendered_tests
        return "1 passed", "", 0, 12

    monkeypatch.setattr("app.api.exams._run_docker_pytest", fake_runner)
    response = client.post(
        f"{API}/exams/{ctx['exam_id']}/questions/{ctx['question_id']}/sample-run",
        headers=student_headers,
        json={"code": "def add(a, b):\n    return a + b"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "output": "1 passed\n", "status": "accepted", "execution_time_ms": 12,
        "diagnostic": None,
    }


def test_exam_sample_run_requires_an_active_owned_attempt(client, db_session_factory):
    code_question = {
        "question_type": "code", "prompt": "实现 add(a, b)", "correct_answer": {},
        "points": 10, "public_cases": [], "hidden_tests": "assert True", "grading_mode": "legacy",
    }
    ctx = _seed(client, db_session_factory, question=code_question)
    teacher_headers = auth_header(ctx["teacher"])
    student_headers = auth_header(ctx["student"])
    assert client.patch(
        f"{API}/exams/{ctx['exam_id']}", headers=teacher_headers, json={"status": "published"},
    ).status_code == 200

    not_started = client.post(
        f"{API}/exams/{ctx['exam_id']}/questions/{ctx['question_id']}/sample-run",
        headers=student_headers, json={"code": "pass"},
    )
    forbidden_teacher = client.post(
        f"{API}/exams/{ctx['exam_id']}/questions/{ctx['question_id']}/sample-run",
        headers=teacher_headers, json={"code": "pass"},
    )
    assert not_started.status_code == 403
    assert forbidden_teacher.status_code == 403
