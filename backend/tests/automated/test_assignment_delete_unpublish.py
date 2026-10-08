"""作业统一管理定向测试：删除草稿作业（级联清理）+ 取消发布（published → draft）

覆盖计划验收项：
- 删除：教师权限 204；非教师 403；published 409；有提交草稿 409；级联清理；删除后列表不含
- unpublish：published → draft 状态流转；draft 409；学生端列表不可见；再 publish 正常（legacy 无 rubric）
"""
from conftest import auth_header, create_course_db, create_user, login
from teaching_test_support import grant_course_students
from app.models import Assignment, CodeGrade, JudgeQuestion, QuestionRubric, Submission


def _setup(client, db_session_factory):
    """创建教师/学生/已发布课程，返回 (teacher_token, student_token, course_id, student_id)"""
    create_user(db_session_factory, "teacher", "teacher")
    student = create_user(db_session_factory, "student", "student")
    teacher_token, _ = login(client, "teacher")
    student_token, _ = login(client, "student")
    course_id = create_course_db(
        db_session_factory,
        teacher_username="teacher",
        title="作业管理测试课",
        status="published",
        visibility="public",
    )
    client.post(f"/api/v1/courses/{course_id}/enroll", headers=auth_header(student_token))
    return teacher_token, student_token, course_id, student.id


def _create_assignment(client, db_sf, teacher_token, course_id, title="草稿作业", status="draft"):
    """创建作业；status="published" 时走 draft → 加题 → /publish 合法链路"""
    resp = client.post(
        "/api/v1/assignments",
        headers=auth_header(teacher_token),
        json={"course_id": course_id, "title": title, "status": "draft"},
    )
    assert resp.status_code == 201, resp.text
    assignment_id = resp.json()["id"]
    grant_course_students(db_sf, course_id, ["student"])
    if status == "published":
        _create_question(client, teacher_token, assignment_id)
        pub = client.post(
            f"/api/v1/assignments/{assignment_id}/publish",
            headers=auth_header(teacher_token),
        )
        assert pub.status_code == 200, pub.text
    return assignment_id


def _create_question(client, teacher_token, assignment_id, title="两数相加"):
    resp = client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        headers=auth_header(teacher_token),
        json={
            "title": title,
            "function_name": "add",
            "signature": "def add(a: int, b: int) -> int",
            "public_cases": [{"args": [1, 2], "expected": 3}],
            "hidden_tests": "def test_add():\n    assert user_code.add(1, 2) == 3\n",
            "grading_mode": "legacy",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _teacher_ids(client, teacher_token):
    resp = client.get("/api/v1/assignments", headers=auth_header(teacher_token))
    assert resp.status_code == 200, resp.text
    return [it["id"] for it in resp.json()["items"]]


# ═══════════════════════════════════════════════════════════════════════
# 删除草稿作业
# ═══════════════════════════════════════════════════════════════════════


def test_delete_draft_assignment_returns_204_and_removes_from_list(client, db_session_factory):
    teacher_token, _, course_id, _ = _setup(client, db_session_factory)
    assignment_id = _create_assignment(client, db_session_factory, teacher_token, course_id)
    question_id = _create_question(client, teacher_token, assignment_id)

    resp = client.delete(
        f"/api/v1/assignments/{assignment_id}", headers=auth_header(teacher_token)
    )
    assert resp.status_code == 204, resp.text
    assert resp.content == b""

    # 删除后列表不含该作业
    assert assignment_id not in _teacher_ids(client, teacher_token)
    # DB 中作业与题目均已删净
    with db_session_factory() as db:
        assert db.get(Assignment, assignment_id) is None
        assert db.get(JudgeQuestion, question_id) is None


def test_delete_assignment_forbidden_for_non_manager(client, db_session_factory):
    teacher_token, student_token, course_id, _ = _setup(client, db_session_factory)
    assignment_id = _create_assignment(client, db_session_factory, teacher_token, course_id)

    # 学生（非教师/管理员/课程负责人）删除 → 403
    resp = client.delete(
        f"/api/v1/assignments/{assignment_id}", headers=auth_header(student_token)
    )
    assert resp.status_code == 403, resp.text
    # 作业仍在
    assert assignment_id in _teacher_ids(client, teacher_token)


def test_delete_published_assignment_conflict(client, db_session_factory):
    teacher_token, _, course_id, _ = _setup(client, db_session_factory)
    assignment_id = _create_assignment(client, db_session_factory, teacher_token, course_id, status="published")

    resp = client.delete(
        f"/api/v1/assignments/{assignment_id}", headers=auth_header(teacher_token)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ASSIGNMENT_NOT_DRAFT"
    assert assignment_id in _teacher_ids(client, teacher_token)


def test_delete_draft_with_submissions_conflict(client, db_session_factory):
    """已发布→学生提交→取消发布回草稿→删除被拒（有提交记录防边界），且拒绝时数据零删除"""
    teacher_token, student_token, course_id, student_id = _setup(client, db_session_factory)
    # 评分事实不可变：题必须在发布前建好（发布后不再能新增题）
    assignment_id = _create_assignment(client, db_session_factory, teacher_token, course_id, status="draft")
    question_id = _create_question(client, teacher_token, assignment_id)
    publish = client.post(
        f"/api/v1/assignments/{assignment_id}/publish", headers=auth_header(teacher_token)
    )
    assert publish.status_code == 200, publish.text

    submit = client.post(
        "/api/v1/judge/submissions",
        headers=auth_header(student_token),
        json={"question_id": question_id, "code": "def add(a, b):\n    return a + b\n"},
    )
    assert submit.status_code == 201, submit.text
    submission_id = submit.json()["id"]
    # 直插 rubric + code grade（模拟 AI 评分产物），验证拒绝场景下连同 submissions 一并保留
    with db_session_factory() as db:
        rubric = QuestionRubric(
            judge_question_id=question_id,
            version=1,
            status="locked",
            source_hash="hash",
            source_snapshot={},
            rubric_json={},
            model_name="test-model",
        )
        db.add(rubric)
        db.commit()
        db.refresh(rubric)
        rubric_id = rubric.id
        grade = CodeGrade(submission_id=submission_id, rubric_id=rubric_id, mode="ai", status="pending")
        db.add(grade)
        db.commit()
        db.refresh(grade)
        grade_id = grade.id

    # 回到草稿后删除 → 409，且所有关联数据均未被触碰
    client.post(f"/api/v1/assignments/{assignment_id}/unpublish", headers=auth_header(teacher_token))
    resp = client.delete(
        f"/api/v1/assignments/{assignment_id}", headers=auth_header(teacher_token)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ASSIGNMENT_HAS_SUBMISSIONS"
    assert assignment_id in _teacher_ids(client, teacher_token)
    with db_session_factory() as db:
        assert db.get(Assignment, assignment_id) is not None
        assert db.get(JudgeQuestion, question_id) is not None
        assert db.get(Submission, submission_id) is not None
        assert db.get(QuestionRubric, rubric_id) is not None
        assert db.get(CodeGrade, grade_id) is not None


def test_delete_draft_cascades_children(client, db_session_factory):
    """级联清理：rubrics / judge_questions / assignments 均删净。

    有提交的作业由门禁 409 拦截（见 test_delete_draft_with_submissions_conflict），
    submissions / code_grades 的级联删除是门禁之外的防御路径（防竞态 IntegrityError）。
    """
    teacher_token, _, course_id, _ = _setup(client, db_session_factory)
    assignment_id = _create_assignment(client, db_session_factory, teacher_token, course_id)
    question_id = _create_question(client, teacher_token, assignment_id)

    # 直插关联数据（测试库无 AI 密钥，不走评分链路）：locked rubric
    with db_session_factory() as db:
        rubric = QuestionRubric(
            judge_question_id=question_id,
            version=1,
            status="locked",
            source_hash="hash",
            source_snapshot={},
            rubric_json={},
            model_name="test-model",
        )
        db.add(rubric)
        db.commit()
        db.refresh(rubric)
        rubric_id = rubric.id

    resp = client.delete(
        f"/api/v1/assignments/{assignment_id}", headers=auth_header(teacher_token)
    )
    assert resp.status_code == 204, resp.text

    with db_session_factory() as db:
        assert db.get(Assignment, assignment_id) is None
        assert db.get(JudgeQuestion, question_id) is None
        assert db.get(QuestionRubric, rubric_id) is None


def test_delete_missing_assignment_404(client, db_session_factory):
    teacher_token, _, _, _ = _setup(client, db_session_factory)
    resp = client.delete("/api/v1/assignments/999999", headers=auth_header(teacher_token))
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "ASSIGNMENT_NOT_FOUND"


# ═══════════════════════════════════════════════════════════════════════
# 取消发布
# ═══════════════════════════════════════════════════════════════════════


def test_unpublish_published_to_draft(client, db_session_factory):
    teacher_token, _, course_id, _ = _setup(client, db_session_factory)
    assignment_id = _create_assignment(client, db_session_factory, teacher_token, course_id, status="published")

    resp = client.post(
        f"/api/v1/assignments/{assignment_id}/unpublish", headers=auth_header(teacher_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == assignment_id
    assert resp.json()["status"] == "draft"

    with db_session_factory() as db:
        assert db.get(Assignment, assignment_id).status == "draft"


def test_unpublish_draft_conflict(client, db_session_factory):
    teacher_token, _, course_id, _ = _setup(client, db_session_factory)
    assignment_id = _create_assignment(client, db_session_factory, teacher_token, course_id, status="draft")

    resp = client.post(
        f"/api/v1/assignments/{assignment_id}/unpublish", headers=auth_header(teacher_token)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ASSIGNMENT_NOT_PUBLISHED"


def test_unpublish_forbidden_for_non_manager(client, db_session_factory):
    teacher_token, student_token, course_id, _ = _setup(client, db_session_factory)
    assignment_id = _create_assignment(client, db_session_factory, teacher_token, course_id, status="published")

    resp = client.post(
        f"/api/v1/assignments/{assignment_id}/unpublish", headers=auth_header(student_token)
    )
    assert resp.status_code == 403, resp.text
    with db_session_factory() as db:
        assert db.get(Assignment, assignment_id).status == "published"


def test_unpublish_hides_from_student_list(client, db_session_factory):
    """学生端列表只显示 published：取消发布后立即不可见，对照组作业仍可见"""
    teacher_token, student_token, course_id, _ = _setup(client, db_session_factory)
    target_id = _create_assignment(client, db_session_factory, teacher_token, course_id, title="目标作业", status="published")
    keep_id = _create_assignment(client, db_session_factory, teacher_token, course_id, title="保留作业", status="published")

    def student_ids():
        resp = client.get("/api/v1/assignments", headers=auth_header(student_token))
        assert resp.status_code == 200, resp.text
        return [it["id"] for it in resp.json()["items"]]

    assert target_id in student_ids()
    client.post(f"/api/v1/assignments/{target_id}/unpublish", headers=auth_header(teacher_token))
    after = student_ids()
    assert target_id not in after
    assert keep_id in after


def test_unpublish_then_republish_legacy(client, db_session_factory):
    """取消发布后可重新发布（legacy 无 rubric 题目走现有发布门禁直接通过）"""
    teacher_token, _, course_id, _ = _setup(client, db_session_factory)
    assignment_id = _create_assignment(client, db_session_factory, teacher_token, course_id, status="published")

    client.post(f"/api/v1/assignments/{assignment_id}/unpublish", headers=auth_header(teacher_token))
    resp = client.post(
        f"/api/v1/assignments/{assignment_id}/publish", headers=auth_header(teacher_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "published"
