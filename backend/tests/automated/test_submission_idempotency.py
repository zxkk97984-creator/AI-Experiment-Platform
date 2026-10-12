"""批次四：作业提交幂等请求键。

契约：
- ``client_request_id`` 可选；唯一范围是 (student_id, client_request_id)；
- 同键同正文 → 返回原提交（201 + 原 id，不新增行）；
- 同键不同正文 → 409 IDEMPOTENCY_KEY_REUSED；
- 幂等查询先于次数与截止限制（重放已接收的请求不因之后用完次数/到点而失败）；
- 省略请求键的旧客户端保持原行为。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.api.judge import submission_request_digest
from conftest import auth_header, create_user, login

from test_judge_attempt_quota import _setup_question

API = "/api/v1/judge/submissions"
CODE = "def add(a, b):\n    return a + b\n"


def _submit(client, token, question_id, *, code=CODE, key=None):
    body = {"question_id": question_id, "code": code}
    if key is not None:
        body["client_request_id"] = key
    return client.post(API, headers=auth_header(token), json=body)


def _count_submissions(db_session_factory, question_id) -> int:
    from app.models import Submission

    with db_session_factory() as db:
        return len(db.scalars(select(Submission).where(Submission.question_id == question_id)).all())


def test_same_key_same_body_returns_original_submission(client, db_session_factory, redis_client):
    token, question_id = _setup_question(client, db_session_factory, max_attempts=5)
    key = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"

    first = _submit(client, token, question_id, key=key)
    assert first.status_code == 201, first.text
    replay = _submit(client, token, question_id, key=key)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"], "同键同正文必须返回原提交"
    assert _count_submissions(db_session_factory, question_id) == 1, "重放不得新增提交"


def test_same_key_different_body_conflicts(client, db_session_factory, redis_client):
    token, question_id = _setup_question(client, db_session_factory, max_attempts=5)
    key = "3f2504e0-4f89-41d3-9a0c-0305e82c3302"

    assert _submit(client, token, question_id, key=key).status_code == 201
    conflict = _submit(client, token, question_id, code="def add(a, b):\n    return 0\n", key=key)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert _count_submissions(db_session_factory, question_id) == 1


def test_different_students_same_key_are_independent(client, db_session_factory, redis_client):
    from app.models import Course, JudgeQuestion
    from conftest import grant_teaching_fixture

    token, question_id = _setup_question(client, db_session_factory, max_attempts=5)
    student2 = create_user(db_session_factory, "student2", "student")
    # 第二个学生必须在同一授课名单里，否则访问检查先返回 404
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        course = db.get(Course, question.assignment.course_id)
        grant_teaching_fixture(db, course, student_ids=[student2.id])
        db.commit()
    token2, _ = login(client, "student2")
    key = "3f2504e0-4f89-41d3-9a0c-0305e82c3303"

    first = _submit(client, token, question_id, key=key)
    second = _submit(client, token2, question_id, key=key)
    assert first.status_code == 201 and second.status_code == 201, (first.text, second.text)
    assert first.json()["id"] != second.json()["id"]
    assert _count_submissions(db_session_factory, question_id) == 2


def test_omitted_key_keeps_legacy_behaviour(client, db_session_factory, redis_client):
    token, question_id = _setup_question(client, db_session_factory, max_attempts=5)
    first = _submit(client, token, question_id)
    second = _submit(client, token, question_id)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] != second.json()["id"], "省略请求键时每次都是新提交"


def test_replay_precedes_attempt_limit(client, db_session_factory, redis_client):
    """次数用满后，同键同正文的重放仍返回原提交（幂等先于限制）。"""
    token, question_id = _setup_question(client, db_session_factory, max_attempts=1)
    key = "3f2504e0-4f89-41d3-9a0c-0305e82c3304"

    first = _submit(client, token, question_id, key=key)
    assert first.status_code == 201, first.text

    replay = _submit(client, token, question_id, key=key)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]

    # 新请求键仍然消耗次数限制（限制本身没有被削弱）
    blocked = _submit(client, token, question_id, key="3f2504e0-4f89-41d3-9a0c-0305e82c3305")
    assert blocked.status_code == 400, blocked.text
    assert blocked.json()["detail"]["code"] == "MAX_ATTEMPTS_REACHED"


def test_replay_precedes_deadline(client, db_session_factory, redis_client):
    """截止之后再重放原请求仍返回原提交；真正的新提交被截止拦下。"""
    from app.models import Assignment, JudgeQuestion

    token, question_id = _setup_question(client, db_session_factory, max_attempts=5)
    key = "3f2504e0-4f89-41d3-9a0c-0305e82c3306"
    first = _submit(client, token, question_id, key=key)
    assert first.status_code == 201, first.text

    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        assignment = db.get(Assignment, question.assignment_id)
        assignment.due_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()

    replay = _submit(client, token, question_id, key=key)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]

    fresh = _submit(client, token, question_id, key="3f2504e0-4f89-41d3-9a0c-0305e82c3307")
    assert fresh.status_code == 403, fresh.text
    assert fresh.json()["detail"]["code"] == "ASSIGNMENT_DEADLINE_PASSED"


def test_digest_is_stable_and_content_sensitive():
    """摘要只取决于题目与**原样**代码：空白变化即视为不同正文。"""
    a = submission_request_digest(7, "def f():\n    return 1\n")
    b = submission_request_digest(7, "def f():\n    return 1\n")
    c = submission_request_digest(7, "def f():\n    return 1")     # 去掉了末尾换行
    d = submission_request_digest(8, "def f():\n    return 1\n")   # 换了题目
    assert a == b and len(a) == 64
    assert a != c and a != d


# ── 名单导入：后端有界读取（2 MiB + 1 字节） ─────────────────────


def test_audience_import_rejects_oversize_after_bounded_read(client, db_session_factory):
    """上传 2 MiB + 1 字节 → 422 CSV_TOO_LARGE；读取本身有界（不会无界读入）。"""
    from conftest import create_assignment_db, create_course_db, grant_teaching_fixture

    from app.models import Course

    create_user(db_session_factory, "import_t", "teacher")
    teacher_token, _ = login(client, "import_t")
    course_id = create_course_db(db_session_factory, teacher_username="import_t",
                                 title="导入课", status="published", visibility="public")
    assignment_id = create_assignment_db(db_session_factory, course_id=course_id, status="draft")
    with db_session_factory() as db:
        grant_teaching_fixture(db, db.get(Course, course_id), student_ids=[])
        db.commit()
    # 名单导入不需要先发布（受众在发布时解析）
    oversize = b"student_no,real_name\n" + b"x" * (2 * 1024 * 1024)
    response = client.post(
        f"/api/v1/assignments/{assignment_id}/audience/import?kind=include",
        headers=auth_header(teacher_token),
        files={"file": ("roster.csv", oversize, "text/csv")},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "CSV_TOO_LARGE"


def test_audience_import_accepts_within_limit(client, db_session_factory):
    """限额内的合法 CSV 正常导入（有界读取没有把正常路径一起拦掉）。"""
    from conftest import create_assignment_db, create_course_db, grant_teaching_fixture

    from app.models import Course, User

    create_user(db_session_factory, "import_t2", "teacher")
    create_user(db_session_factory, "import_s", "student")
    teacher_token, _ = login(client, "import_t2")
    course_id = create_course_db(db_session_factory, teacher_username="import_t2",
                                 title="导入课2", status="published", visibility="public")
    assignment_id = create_assignment_db(db_session_factory, course_id=course_id, status="draft")
    with db_session_factory() as db:
        student = db.scalar(select(User).where(User.username == "import_s"))
        grant_teaching_fixture(db, db.get(Course, course_id), student_ids=[student.id])
        db.commit()
    # 只给账号列：解析器会校验 real_name 与库内一致，留空即可
    csv = "username\nimport_s\n".encode()
    response = client.post(
        f"/api/v1/assignments/{assignment_id}/audience/import?kind=include",
        headers=auth_header(teacher_token),
        files={"file": ("roster.csv", csv, "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 1, body
