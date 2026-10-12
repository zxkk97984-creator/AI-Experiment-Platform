"""批次三 3B：评分版本的不可变性、启用/退役与绑定冻结。

覆盖契约（未启用新判题路径前也必须成立）：

* 版本严格绑定"作业题 XOR 考试题"，同题版本号单调递增；
* 草稿可改（新版本另存），确认后内容不可改，摘要变化拒绝确认；
* 同题最多一个 active，启用新版本自动退役旧的，retired 只禁止新绑定；
* 作业提交创建时绑定 active 版本；无 active 一律留空（不猜最新配置）；
* 考试开始冻结逐题版本映射，答案只从映射复制，考试中途改配置不影响已开始考试。
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models import (
    ExamAnswer,
    ExamQuestion,
    ExamSubmission,
    JudgeGradingVersion,
    JudgeQuestion,
    Submission,
)
from app.services import grading_version_service as versions
from app.services.grading_values import encode_value

pytest_plugins = ["tests.automated.conftest"]


def _confirm_version(db, version, **kwargs):
    return versions.confirm_version(
        db, version, confirm_isolated_case_v2=True, **kwargs)


def _grant_student_access(db_session_factory, question_id: int, username: str = "student") -> int:
    """给学生在题目所属课程上授予教学上下文（提交接口按受众校验）。"""
    from tests.automated.conftest import grant_teaching_fixture, make_student

    from app.models import Assignment, Course, JudgeQuestion

    student = make_student(db_session_factory, username=username)
    student_id = getattr(student, "id", student)
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        assignment = db.get(Assignment, question.assignment_id)
        course = db.get(Course, assignment.course_id)
        grant_teaching_fixture(db, course, [student_id])
        db.commit()
    return student_id


def _payload(case_ids=("F1-1",), *, entry="solve", f_score=30.0, r_score=5.0):
    groups = [{
        "id": "F1", "name": "功能", "dimension": "F", "max_score": f_score,
        "cases": [{"id": case_id, "input": encode_value(([1, 2],)),
                   "expected": encode_value(3), "compare": {"kind": "literal"}}
                  for case_id in case_ids],
    }]
    if r_score:
        groups.append({
            "id": "R1", "name": "边界", "dimension": "R", "max_score": r_score,
            "cases": [{"id": "R1-1", "input": encode_value(([],)),
                       "expected": encode_value(0), "compare": {"kind": "literal"}}],
        })
    return {"schema_version": 1, "entry_function": entry, "execution_model": "data_v1",
            "groups": groups}


def _draft(db_session_factory, *, question_kind="assignment", question_id=None,
           payload=None, entry_function="solve", **kwargs):
    from tests.automated.conftest import make_exam_question, make_judge_question

    if question_id is None:
        question_id = (make_judge_question(db_session_factory) if question_kind == "assignment"
                       else make_exam_question(db_session_factory))
    with db_session_factory() as db:
        version = versions.create_draft(
            db, question_kind=question_kind, question_id=question_id,
            payload=payload if payload is not None else _payload(),
            entry_function=entry_function, **kwargs)
        db.commit()
        db.refresh(version)
        return version.id, question_id


# ─────────────────────────────────────────────────────────────
# 1. 创建与校验
# ─────────────────────────────────────────────────────────────

def test_create_draft_validates_and_normalizes(db_session_factory):
    version_id, question_id = _draft(db_session_factory)
    with db_session_factory() as db:
        version = db.get(JudgeGradingVersion, version_id)
        assert version.status == "draft"
        assert version.judge_question_id == question_id and version.exam_question_id is None
        assert version.version == 1
        assert version.payload["functional_total"] == 0.0
        assert version.payload["robustness_total"] == 0.0
        assert version.payload["groups"] == []
        assert len(version.payload["binary_cases"]) == 2
        assert version.source_digest and version.spec_digest
        assert version.spec_digest != version.source_digest


def test_versions_increment_per_question(db_session_factory):
    version_id, question_id = _draft(db_session_factory)
    second_id, _ = _draft(db_session_factory, question_id=question_id)
    third_id, _ = _draft(db_session_factory, question_id=question_id)
    with db_session_factory() as db:
        numbers = [db.get(JudgeGradingVersion, item).version
                   for item in (version_id, second_id, third_id)]
        assert numbers == [1, 2, 3]


def test_exam_question_versions_are_separate_namespace(db_session_factory):
    exam_version, exam_question_id = _draft(db_session_factory, question_kind="exam")
    with db_session_factory() as db:
        version = db.get(JudgeGradingVersion, exam_version)
        assert version.exam_question_id == exam_question_id
        assert version.judge_question_id is None


@pytest.mark.parametrize("payload,reason", [
    ({}, "groups"),
    ({"groups": []}, "payload"),
    ({"groups": [{"id": "F1", "dimension": "X", "max_score": 10, "cases": [{}]}]}, "dimension"),
    ({"groups": [{"id": "F1", "dimension": "F", "max_score": 0, "cases": [{}]}]}, "正数"),
    ({"groups": [{"id": "F1", "dimension": "F", "max_score": 10, "cases": []}]}, "cases"),
    ({"groups": [{"id": "F1", "dimension": "F", "max_score": 10,
                  "cases": [{"id": "F1-1", "input": encode_value(1)}]}]}, "expected"),
    ({"groups": [{"id": "F1", "dimension": "F", "max_score": 61, "cases": [
        {"id": "F1-1", "input": encode_value(1), "expected": encode_value(1)}]}]}, "60"),
    ({"groups": [
        {"id": "F1", "dimension": "F", "max_score": 10,
         "cases": [{"id": "X", "input": encode_value(1), "expected": encode_value(1)}]},
        {"id": "R1", "dimension": "R", "max_score": 11,
         "cases": [{"id": "Y", "input": encode_value(1), "expected": encode_value(1)}]},
    ]}, "10"),
])
def test_payload_validation_rejects_bad_shapes(db_session_factory, payload, reason):
    with pytest.raises(versions.GradingVersionError) as error:
        _draft(db_session_factory, payload=payload)
    assert reason in str(error.value)


def test_duplicate_case_ids_rejected(db_session_factory):
    payload = _payload(case_ids=("F1-1", "F1-1"))
    with pytest.raises(versions.GradingVersionError) as error:
        _draft(db_session_factory, payload=payload)
    assert "重复" in str(error.value)


def test_entry_function_must_be_identifier(db_session_factory):
    with pytest.raises(versions.GradingVersionError):
        _draft(db_session_factory, entry_function="import os")


# ─────────────────────────────────────────────────────────────
# 2. 确认 / 启用 / 退役
# ─────────────────────────────────────────────────────────────

def test_confirm_then_activate_retires_previous(db_session_factory):
    first_id, question_id = _draft(db_session_factory)
    second_id, _ = _draft(db_session_factory, question_id=question_id)
    with db_session_factory() as db:
        first = db.get(JudgeGradingVersion, first_id)
        second = db.get(JudgeGradingVersion, second_id)
        _confirm_version(db, first, actor_id=None)
        versions.activate_version(db, first)
        assert first.status == "active"
        _confirm_version(db, second)
        versions.activate_version(db, second)
        db.commit()
        assert second.status == "active"
        assert first.status == "retired", "启用新版本必须退役同题旧 active"

    with db_session_factory() as db:
        active = versions.active_version_for(db, question_kind="assignment",
                                             question_id=question_id)
        assert active.id == second_id
        # 历史绑定不受影响：退役版本仍可读取
        assert db.get(JudgeGradingVersion, first_id).status == "retired"


def test_confirm_rejects_modified_payload(db_session_factory):
    version_id, _ = _draft(db_session_factory)
    with db_session_factory() as db:
        version = db.get(JudgeGradingVersion, version_id)
        version.payload = dict(version.payload, binary_cases=[])
        with pytest.raises(versions.GradingVersionError) as error:
            _confirm_version(db, version)
        assert "摘要" in str(error.value)


def test_confirm_rejects_stale_candidate_digest(db_session_factory):
    version_id, _ = _draft(db_session_factory)
    with db_session_factory() as db:
        version = db.get(JudgeGradingVersion, version_id)
        with pytest.raises(versions.GradingVersionError):
            _confirm_version(db, version, expected_spec_digest="0" * 64)


def test_only_confirmed_can_be_activated_and_only_active_retired(db_session_factory):
    draft_id, _ = _draft(db_session_factory)
    with db_session_factory() as db:
        draft = db.get(JudgeGradingVersion, draft_id)
        with pytest.raises(versions.GradingVersionError):
            versions.activate_version(db, draft)
        with pytest.raises(versions.GradingVersionError):
            versions.retire_version(db, draft)
        _confirm_version(db, draft)
        versions.activate_version(db, draft)
        versions.retire_version(db, draft)
        db.commit()
        assert draft.status == "retired"
        # 退役后仍可重新启用（不改变历史绑定）
        versions.activate_version(db, draft)
        db.commit()
        assert draft.status == "active"


def test_ai_modes_require_existing_rubric(db_session_factory):
    version_id, _ = _draft(db_session_factory, grading_mode_snapshot="active")
    with db_session_factory() as db:
        version = db.get(JudgeGradingVersion, version_id)
        assert versions.rubric_binding_ok(db, version) is False
        version.rubric_id = 10 ** 9
        assert versions.rubric_binding_ok(db, version) is False
        legacy_id, _ = _draft(db_session_factory, grading_mode_snapshot="legacy")
        assert versions.rubric_binding_ok(db, db.get(JudgeGradingVersion, legacy_id)) is True


def test_spec_digest_detects_context_change(db_session_factory):
    first_id, question_id = _draft(db_session_factory, context_snapshot={"prompt": "A"})
    second_id, _ = _draft(db_session_factory, question_id=question_id,
                          context_snapshot={"prompt": "B"})
    with db_session_factory() as db:
        first = db.get(JudgeGradingVersion, first_id)
        second = db.get(JudgeGradingVersion, second_id)
        assert first.spec_digest != second.spec_digest, "上下文变化必须改变规范摘要"
        assert first.source_digest == second.source_digest, "来源摘要只覆盖用例与入口"


# ─────────────────────────────────────────────────────────────
# 3. 绑定：作业提交
# ─────────────────────────────────────────────────────────────

def test_submission_binds_active_version(db_session_factory, client):
    """真实 API 路径：提交创建时绑定 active 版本（无 active 时保持 NULL）。"""
    from tests.automated.conftest import login

    version_id, question_id = _draft(db_session_factory)
    with db_session_factory() as db:
        version = db.get(JudgeGradingVersion, version_id)
        _confirm_version(db, version)
        versions.activate_version(db, version)
        versions.set_question_current_version(db, question_kind="assignment",
                                             question_id=question_id, version_id=version_id)
        db.commit()

    _grant_student_access(db_session_factory, question_id)
    token, _ = login(client, "student")
    response = client.post("/api/v1/judge/submissions",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"question_id": question_id, "code": "def solve():\n    return 0"})
    assert response.status_code in (200, 201), response.text
    submission_id = response.json()["id"]

    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        assert submission.grading_version_id == version_id
        assert db.get(JudgeQuestion, question_id).current_grading_version_id == version_id


def test_submission_without_active_version_stays_unbound(db_session_factory, client):
    from tests.automated.conftest import login, make_student

    _version_id, question_id = _draft(db_session_factory)  # 只有草稿，没有 active
    _grant_student_access(db_session_factory, question_id)
    token, _ = login(client, "student")
    response = client.post("/api/v1/judge/submissions",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"question_id": question_id, "code": "def solve():\n    return 0"})
    assert response.status_code in (200, 201), response.text
    with db_session_factory() as db:
        submission = db.get(Submission, response.json()["id"])
        assert submission.grading_version_id is None, "没有 active 版本时不得绑定任何版本"


def test_new_submission_after_activation_binds_new_version(db_session_factory, client):
    from tests.automated.conftest import login

    first_id, question_id = _draft(db_session_factory)
    second_id, _ = _draft(db_session_factory, question_id=question_id)
    with db_session_factory() as db:
        for version_id in (first_id, second_id):
            version = db.get(JudgeGradingVersion, version_id)
            _confirm_version(db, version)
            versions.activate_version(db, version)
        db.commit()

    _grant_student_access(db_session_factory, question_id)
    token, _ = login(client, "student")
    response = client.post("/api/v1/judge/submissions",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"question_id": question_id, "code": "def solve():\n    return 0"})
    assert response.status_code in (200, 201), response.text
    with db_session_factory() as db:
        assert db.get(Submission, response.json()["id"]).grading_version_id == second_id


# ─────────────────────────────────────────────────────────────
# 4. 绑定：考试冻结映射
# ─────────────────────────────────────────────────────────────

def test_exam_freeze_map_and_answer_copy(db_session_factory):
    from tests.automated.conftest import make_exam_answer, make_exam_submission

    version_id, question_id = _draft(db_session_factory, question_kind="exam")
    with db_session_factory() as db:
        version = db.get(JudgeGradingVersion, version_id)
        _confirm_version(db, version)
        versions.activate_version(db, version)
        versions.set_question_current_version(db, question_kind="exam",
                                             question_id=question_id, version_id=version_id)
        db.commit()
        exam_id = db.get(ExamQuestion, question_id).exam_id

    submission_id = make_exam_submission(db_session_factory, exam_id=exam_id)
    with db_session_factory() as db:
        submission = db.get(ExamSubmission, submission_id)
        mapping = versions.freeze_exam_versions(db, submission)
        db.commit()
        # A09：冻结映射同时记录开始考试时的分值（兼容旧裸 int 数据）
        assert mapping == {str(question_id): {"version_id": version_id, "points": 10.0}}

    # 考试中途：题目启用新版本，已开始考试的答案仍使用冻结版本
    later_id, _ = _draft(db_session_factory, question_kind="exam", question_id=question_id)
    with db_session_factory() as db:
        later = db.get(JudgeGradingVersion, later_id)
        _confirm_version(db, later)
        versions.activate_version(db, later)
        versions.set_question_current_version(db, question_kind="exam",
                                             question_id=question_id, version_id=later_id)
        db.commit()

    answer_id = make_exam_answer(db_session_factory, submission_id=submission_id,
                                 question_id=question_id)
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        # 生产路径在创建答案后调用 bind_exam_answer_version（save_answer / 人工评分入口）
        versions.bind_exam_answer_version(db, answer)
        db.commit()
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        assert answer.grading_version_id == version_id, "必须使用冻结版本而非最新配置"
        assert versions.version_for_answer(db, answer).id == version_id
        # 新会话使用新版本
        fresh_id = make_exam_submission(db_session_factory, exam_id=exam_id,
                                        student_username="student2")
        fresh = db.get(ExamSubmission, fresh_id)
        assert versions.freeze_exam_versions(db, fresh) == {
            str(question_id): {"version_id": later_id, "points": 10.0}}


def test_exam_answer_without_frozen_map_stays_unbound(db_session_factory):
    from tests.automated.conftest import make_exam_answer, make_exam_submission

    version_id, question_id = _draft(db_session_factory, question_kind="exam")
    with db_session_factory() as db:
        version = db.get(JudgeGradingVersion, version_id)
        _confirm_version(db, version)
        versions.activate_version(db, version)
        db.commit()
        exam_id = db.get(ExamQuestion, question_id).exam_id

    submission_id = make_exam_submission(db_session_factory, exam_id=exam_id)
    answer_id = make_exam_answer(db_session_factory, submission_id=submission_id,
                                 question_id=question_id)
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        versions.bind_exam_answer_version(db, answer)
        db.commit()
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        assert answer.grading_version_id is None, "旧会话无基线：不静默绑定最新版本"
        assert versions.version_for_answer(db, answer) is None


def test_non_code_exam_questions_freeze_points_without_a_code_version(db_session_factory):
    from tests.automated.conftest import make_exam_question, make_exam_submission

    with db_session_factory() as db:
        exam_question_id = make_exam_question(db_session_factory, question_type="single_choice")
        exam_id = db.get(ExamQuestion, exam_question_id).exam_id
    submission_id = make_exam_submission(db_session_factory, exam_id=exam_id)
    with db_session_factory() as db:
        submission = db.get(ExamSubmission, submission_id)
        assert versions.freeze_exam_versions(db, submission) == {
            str(exam_question_id): {"version_id": None, "points": 10.0}
        }


def test_version_table_xor_constraint_enforced(db_session_factory):
    """数据库层保证"作业题 XOR 考试题"：两边同时给或同时为空都不可写入。"""
    from tests.automated.conftest import constraint_violation
    with db_session_factory() as db:
        db.add(JudgeGradingVersion(
            judge_question_id=None, exam_question_id=None, version=1, status="draft",
            execution_mode="data_v1", grading_mode_snapshot="legacy", entry_function="solve",
            payload={}, compare_rules={}, score_cap_rules=[], context_snapshot={},
            source_kind="manual", source_digest="a" * 64, spec_digest="b" * 64))
        with constraint_violation():
            db.commit()
        db.rollback()
        remaining = db.scalars(select(JudgeGradingVersion)).all()
        assert remaining == []


# ─────────────────────────────────────────────────────────────
# 5. 迁移建库路径（不是 create_all）：时间戳列必须有 server_default
# ─────────────────────────────────────────────────────────────

def test_migrated_schema_supports_version_insert(tmp_path):
    """经 alembic 迁移到 head 的库必须能用 ORM 插入评分版本。

    迁移建表与 ``Base.metadata.create_all`` 建表是两条路径：迁移里漏掉
    ``server_default`` 时，INSERT 会省略 created_at/updated_at 并撞上 NOT NULL
    （``alembic check`` 默认不比较 server_default，所以这个门禁必须单独存在）。
    """
    from sqlalchemy.orm import sessionmaker

    from tests.automated import migration_stage_support as support

    from app.models import Assignment, Course, JudgeGradingVersion, JudgeQuestion, User

    database = tmp_path / "migrated.db"
    support.historical_schema(database, "20260926_0001")
    support.upgrade_to_current_head(database, stamped_at="20260926_0001")

    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        columns = {row[1]: row for row in connection.execute(
            sa.text("PRAGMA table_info(judge_grading_versions)"))}
        assert columns["created_at"][4] is not None, "created_at 缺少 server_default"
        assert columns["updated_at"][4] is not None, "updated_at 缺少 server_default"

    Session = sessionmaker(bind=engine)
    with Session() as db:
        teacher = User(username="mig_teacher", password_hash="x", real_name="迁移教师",
                       role="teacher", status="active")
        db.add(teacher)
        db.flush()
        course = Course(title="迁移课程", status="published", teacher_id=teacher.id)
        db.add(course)
        db.flush()
        assignment = Assignment(course_id=course.id, title="迁移作业", status="published")
        db.add(assignment)
        db.flush()
        question = JudgeQuestion(assignment_id=assignment.id, title="迁移题目",
                                 function_name="solve", hidden_tests="assert True")
        db.add(question)
        db.flush()
        version = JudgeGradingVersion(
            judge_question_id=question.id, version=1, status="draft",
            execution_mode="data_v1", grading_mode_snapshot="legacy", entry_function="solve",
            payload={"groups": []}, compare_rules={}, score_cap_rules=[], context_snapshot={},
            source_kind="manual", source_digest="a" * 64, spec_digest="b" * 64)
        db.add(version)
        db.commit()
        assert version.created_at is not None and version.updated_at is not None
    engine.dispose()
