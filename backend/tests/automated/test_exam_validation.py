"""Task 4: 考试题目与发布校验——PublicCase迁移、题型校验、发布门禁"""
import json

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.schemas import ExamQuestionCreate, ExamQuestionUpdate, PublicCase
from app.services.exam_service import score_choice_answer, validate_publish, validate_question
from conftest import auth_header, create_course_db, create_user, login
from teaching_test_support import grant_course_students


def _enroll_student_for_publish(db_session_factory, course_id, username):
    """管理员显式分班授权，冻结 all_enrolled audience 的真实班级名单。"""
    return grant_course_students(db_session_factory, course_id, [username])


# ═══════════════════════════════════════════════════════════════
# 1. PublicCase 历史格式迁移
# ═══════════════════════════════════════════════════════════════

def test_public_case_migrates_input_to_args():
    """旧格式 {"input": [1,2], "expected": 3} → {"args": [1,2], "expected": 3}"""
    pc = PublicCase.model_validate({"input": [1, 2], "expected": 3})
    assert pc.args == [1, 2]
    assert pc.expected == 3


def test_public_case_rejects_both_input_and_args():
    """同时传入 input 和 args 应报错"""
    with pytest.raises((PydanticValidationError, ValueError)):
        PublicCase.model_validate({"input": [1, 2], "args": [1, 2], "expected": 3})


def test_p1_4_public_case_rejects_input_and_empty_args():
    """P1-4: 同时传入 input 和空的 args=[] 也应报错（key 存在即冲突，不论值是否为空）"""
    with pytest.raises((PydanticValidationError, ValueError)):
        PublicCase.model_validate({"input": [1], "args": [], "expected": 1})


def test_public_case_expected_required():
    """expected 为必填"""
    with pytest.raises(PydanticValidationError):
        PublicCase.model_validate({"args": [1, 2]})


def test_public_case_args_defaults_to_list():
    """只传 expected 时 args 默认为空列表"""
    pc = PublicCase.model_validate({"expected": 42})
    assert pc.args == []
    assert pc.expected == 42


def test_public_case_allows_unknown_fields():
    """extra=allow：允许未知字段（向后兼容）"""
    pc = PublicCase.model_validate({"args": [1], "expected": 2, "unknown_field": 3})
    assert pc.args == [1]
    assert pc.expected == 2


# ═══════════════════════════════════════════════════════════════
# 2. 题型校验
# ═══════════════════════════════════════════════════════════════

def test_single_choice_validation(db_session_factory):
    """单选题：至少2个选项 + 恰好1个正确答案"""
    from app.models import ExamQuestion

    # 有效的单选题
    q = ExamQuestion(question_type="single_choice", prompt="Q",
                     options={"A": "选项A", "B": "选项B"},
                     correct_answer={"correct": ["A"]}, points=5)
    errors = validate_question(q)
    assert len(errors) == 0, f"应通过: {errors}"

    # 选项不足2个
    q1 = ExamQuestion(question_type="single_choice", prompt="Q",
                      options={"A": "选项A"},
                      correct_answer={"correct": ["A"]}, points=5)
    errors1 = validate_question(q1)
    assert len(errors1) >= 1
    assert "选项" in errors1[0]

    # 正确答案不在选项中
    q2 = ExamQuestion(question_type="single_choice", prompt="Q",
                      options={"A": "选项A", "B": "选项B"},
                      correct_answer={"correct": ["C"]}, points=5)
    errors2 = validate_question(q2)
    assert len(errors2) >= 1
    assert "C" in errors2[0]


def test_multi_choice_validation(db_session_factory):
    """多选题：至少2个选项 + 至少1个正确答案"""
    from app.models import ExamQuestion

    q = ExamQuestion(question_type="multi_choice", prompt="Q",
                     options={"A": "A", "B": "B", "C": "C"},
                     correct_answer={"correct": ["A", "B"]}, points=5)
    errors = validate_question(q)
    assert len(errors) == 0, f"应通过: {errors}"

    # 无正确答案
    q1 = ExamQuestion(question_type="multi_choice", prompt="Q",
                      options={"A": "A", "B": "B"},
                      correct_answer={"correct": []}, points=5)
    errors1 = validate_question(q1)
    assert len(errors1) >= 1


def test_code_question_validation(db_session_factory):
    """编程题草稿允许不完整，发布时按评分模式检查。"""
    from app.models import ExamQuestion

    q = ExamQuestion(question_type="code", prompt="Q",
                     correct_answer={}, points=10,
                     hidden_tests="assert True")
    errors = validate_question(q)
    assert len(errors) == 0, f"应通过: {errors}"

    # active 草稿允许先保存，发布时必须有 AI 测试组
    q1 = ExamQuestion(question_type="code", prompt="Q",
                      correct_answer={}, points=10, grading_mode="active")
    errors1 = validate_question(q1)
    assert errors1 == []
    publish_errors = validate_question(q1, publish=True)
    assert any("AI 测试组" in error for error in publish_errors)

    # legacy 发布仍必须有隐藏测试
    q1.grading_mode = "legacy"
    publish_errors = validate_question(q1, publish=True)
    assert any("隐藏测试" in error for error in publish_errors)


def test_multi_choice_partial_scoring_no_wrong_answers():
    """部分得分：无错选按比例，任一错选即零分，并保留一位小数。"""
    from app.models import ExamQuestion

    q = ExamQuestion(question_type="multi_choice", prompt="Q", points=10,
                     options={"A": "A", "B": "B", "C": "C", "D": "D"},
                     correct_answer={"correct": ["A", "B", "C"], "scoring_mode": "partial_no_wrong"})
    assert score_choice_answer(q, ["A"]) == 3.3
    assert score_choice_answer(q, ["A", "B"]) == 6.7
    assert score_choice_answer(q, ["A", "D"]) == 0.0


def test_legacy_choice_scoring_defaults_to_all_or_nothing():
    """历史 correct_answer 没有 scoring_mode 时继续完全匹配。"""
    from app.models import ExamQuestion

    q = ExamQuestion(question_type="multi_choice", prompt="Q", points=5,
                     options={"A": "A", "B": "B"}, correct_answer={"correct": ["A", "B"]})
    assert score_choice_answer(q, ["A"]) == 0.0
    assert score_choice_answer(q, ["A", "B"]) == 5.0


def test_points_must_be_positive(db_session_factory):
    """points <= 0 应报错"""
    from app.models import ExamQuestion

    q = ExamQuestion(question_type="single_choice", prompt="Q",
                     options={"A": "A", "B": "B"},
                     correct_answer={"correct": ["A"]}, points=0)
    errors = validate_question(q)
    assert len(errors) >= 1
    assert "分值" in errors[0]


# ═══════════════════════════════════════════════════════════════
# 3. 发布校验
# ═══════════════════════════════════════════════════════════════

def test_publish_fails_with_invalid_questions(client, db_session_factory):
    """发布时若题目有校验错误，应拒绝发布"""
    create_user(db_session_factory, "pv_t", "teacher")
    create_user(db_session_factory, "pv_s", "student")
    t_tok, _ = login(client, "pv_t")
    cid = create_course_db(db_session_factory, teacher_username="pv_t", title="VC", status="published")
    _enroll_student_for_publish(db_session_factory, cid, "pv_s")
    import datetime
    from datetime import timezone, timedelta
    now = datetime.datetime.now(timezone.utc)
    e = client.post("/api/v1/exams", headers=auth_header(t_tok), json={
        "course_id": cid, "title": "VE", "duration_minutes": 60,
        "start_at": (now - timedelta(hours=1)).isoformat(),
        "end_at": (now + timedelta(hours=1)).isoformat(),
    })
    eid = e.json()["id"]
    _enroll_student_for_publish(db_session_factory, cid, "pv_s")

    # 创建一道无效题目（只有1个选项的单选题）
    r = client.post(f"/api/v1/exams/{eid}/questions", headers=auth_header(t_tok), json={
        "question_type": "single_choice", "prompt": "Q",
        "options": {"A": "Only"}, "correct_answer": {"correct": ["A"]}, "points": 10,
    })
    # 创建时校验应该拦截
    assert r.status_code == 422, f"无效题目应返回 422: {r.status_code}"

    # 创建有效题目
    client.post(f"/api/v1/exams/{eid}/questions", headers=auth_header(t_tok), json={
        "question_type": "single_choice", "prompt": "Q",
        "options": {"A": "A", "B": "B"}, "correct_answer": {"correct": ["A"]}, "points": 10,
    })
    # 发布应成功
    r = client.patch(f"/api/v1/exams/{eid}", headers=auth_header(t_tok),
                     json={"status": "published"})
    assert r.status_code == 200, f"有效考试发布应成功: {r.text}"


def test_active_code_draft_can_save_but_publish_requires_test_groups(client, db_session_factory):
    """active 编程题可先保存草稿，但配置不完整时不能发布。"""
    create_user(db_session_factory, "pv2_t", "teacher")
    create_user(db_session_factory, "pv2_s", "student")
    t_tok, _ = login(client, "pv2_t")
    cid = create_course_db(db_session_factory, teacher_username="pv2_t", title="VC2", status="published")
    _enroll_student_for_publish(db_session_factory, cid, "pv2_s")
    import datetime
    from datetime import timezone, timedelta
    now = datetime.datetime.now(timezone.utc)
    e = client.post("/api/v1/exams", headers=auth_header(t_tok), json={
        "course_id": cid, "title": "VE2", "duration_minutes": 60,
        "start_at": (now - timedelta(hours=1)).isoformat(),
        "end_at": (now + timedelta(hours=1)).isoformat(),
    })
    eid = e.json()["id"]
    _enroll_student_for_publish(db_session_factory, cid, "pv2_s")

    # 创建 active 编程题草稿，不要求 hidden_tests
    r = client.post(f"/api/v1/exams/{eid}/questions", headers=auth_header(t_tok), json={
        "question_type": "code", "prompt": "Q", "points": 10, "correct_answer": {}, "grading_mode": "active",
    })
    assert r.status_code == 201, r.text

    published = client.patch(f"/api/v1/exams/{eid}", headers=auth_header(t_tok), json={"status": "published"})
    assert published.status_code == 422
    assert "AI 测试组" in published.text


# ═══════════════════════════════════════════════════════════════
# 4. ExamQuestionUpdate schema
# ═══════════════════════════════════════════════════════════════

def test_exam_question_update_schema():
    """ExamQuestionUpdate 接受部分字段更新"""
    update = ExamQuestionUpdate(prompt="新的题目描述")
    assert update.prompt == "新的题目描述"
    assert update.points is None  # 未传入

    update2 = ExamQuestionUpdate(points=20, hidden_tests="assert True")
    assert update2.points == 20
    assert update2.prompt is None


def test_exam_question_patch_uses_schema(client, db_session_factory):
    """PATCH 端点使用 ExamQuestionUpdate 而非裸 dict"""
    create_user(db_session_factory, "pu_t", "teacher")
    t_tok, _ = login(client, "pu_t")
    cid = create_course_db(db_session_factory, teacher_username="pu_t", title="VU", status="published")
    import datetime
    from datetime import timezone, timedelta
    now = datetime.datetime.now(timezone.utc)
    e = client.post("/api/v1/exams", headers=auth_header(t_tok), json={
        "course_id": cid, "title": "VU", "duration_minutes": 60,
        "start_at": (now - timedelta(hours=1)).isoformat(),
        "end_at": (now + timedelta(hours=1)).isoformat(),
    })
    eid = e.json()["id"]
    q = client.post(f"/api/v1/exams/{eid}/questions", headers=auth_header(t_tok), json={
        "question_type": "single_choice", "prompt": "Q",
        "options": {"A": "A", "B": "B"}, "correct_answer": {"correct": ["A"]}, "points": 10,
    })
    qid = q.json()["id"]

    # 更新 prompt
    r = client.patch(f"/api/v1/exams/{eid}/questions/{qid}", headers=auth_header(t_tok), json={
        "prompt": "新的题目",
    })
    assert r.status_code == 200, f"PATCH 应成功: {r.text}"
    assert r.json()["prompt"] == "新的题目"

    # 尝试更新为无效数据（points 设为 0）
    r = client.patch(f"/api/v1/exams/{eid}/questions/{qid}", headers=auth_header(t_tok), json={
        "points": 0,
    })
    assert r.status_code == 422, f"无效更新应返回 422: {r.status_code}"
