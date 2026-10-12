"""批次三 3C（后端契约）：pytest→data_v1 AST 预检/应用、版本编辑、AI 草稿。

关键约束：

* 转换只做静态 AST 分析——**从不执行**题目源码、不导入被测模块；
* 无法静态证明等价的用例逐条给出原因，绝不猜测；
* 转换结果与 AI 生成结果都只是**草稿**，必须教师确认后显式启用；
* 启用后新提交绑定新版本（与 3B 绑定契约闭环）。
"""
from __future__ import annotations

import json

import pytest

from app.models import (
    Assignment,
    Course,
    ExamQuestion,
    JudgeGradingVersion,
    JudgeQuestion,
    Submission,
)
from app.services import grading_conversion as conv
from app.services import grading_version_service as versions
from tests.automated.conftest import auth_header, grant_teaching_fixture, login, make_student, make_teacher

API = "/api/v1/judge"


# ─────────────────────────────────────────────────────────────
# 1. AST 预检（纯静态）
# ─────────────────────────────────────────────────────────────

def test_analyze_simple_equality_cases():
    source = (
        "def test_add():\n"
        "    assert solve(1, 2) == 3\n"
        "    assert solve([1, 2]) == [1, 2, 3]\n"
    )
    report = conv.analyze_pytest_source(source, entry_function="solve")
    assert report["convertible_count"] == 2
    assert report["unsupported_count"] == 0
    assert report["fully_convertible"] is True
    first, second = report["cases"]
    assert first["input"] == {"t": "tuple", "v": [{"t": "int", "v": 1}, {"t": "int", "v": 2}]}
    assert first["expected"] == {"t": "int", "v": 3}
    assert first["compare"] == {"kind": "literal"}
    assert second["expected"] == {"t": "list", "v": [{"t": "int", "v": 1},
                                                     {"t": "int", "v": 2},
                                                     {"t": "int", "v": 3}]}


def test_analyze_identity_and_exception_cases():
    source = (
        "import pytest\n"
        "def test_none():\n"
        "    assert solve(0) is None\n"
        "def test_raises():\n"
        "    with pytest.raises(ValueError):\n"
        "        solve(-1)\n"
    )
    report = conv.analyze_pytest_source(source, entry_function="solve")
    assert report["convertible_count"] == 2
    identity, exception = report["cases"]
    assert identity["compare"] == {"kind": "identity"}
    assert identity["expected"] == {"t": "none"}
    assert exception["compare"] == {"kind": "exception", "types": ["ValueError"]}
    assert exception["input"] == {"t": "tuple", "v": [{"t": "int", "v": -1}]}


@pytest.mark.parametrize("source,reason", [
    ("def test_x():\n    assert solve(x) == 3\n", "字面量"),
    ("def test_x():\n    assert other(1) == 3\n", "非入口函数"),
    ("def test_x():\n    assert solve(a=1) == 3\n", "关键字实参"),
    ("def test_x():\n    for i in range(3):\n        assert solve(i) == 3\n", "控制流"),
    ("def test_x():\n    value = solve(1)\n    assert value == 3\n", "赋值"),
    ("def test_x():\n    assert solve(1) != 3\n", "比较运算符"),
    ("def test_x():\n    assert solve(1)\n", "只支持"),
    ("def test_x():\n    with pytest.raises(CustomError):\n        solve(1)\n", "内置异常"),
])
def test_unsupported_forms_report_reasons(source, reason):
    report = conv.analyze_pytest_source(source, entry_function="solve")
    assert report["convertible_count"] == 0
    assert report["unsupported_count"] >= 1
    reasons = " ".join(case["reason"] for case in report["cases"])
    assert reason in reasons, reasons


def test_analysis_never_executes_source(tmp_path):
    """模块级与函数级副作用都不得执行（纯静态分析）。"""
    marker = tmp_path / "pwned.txt"
    source = (
        "import os\n"
        f"open({str(marker)!r}, 'w').write('pwned')\n"
        "def test_x():\n"
        f"    open({str(marker)!r}, 'a').write('pwned')\n"
        "    assert solve(1) == 1\n"
    )
    report = conv.analyze_pytest_source(source, entry_function="solve")
    assert not marker.exists(), "AST 预检绝不能执行题目源码"
    # 副作用语句被逐条标记 unsupported；可静态证明的断言仍可转换
    assert report["unsupported_count"] >= 1
    assert all(case["status"] in ("convertible", "unsupported") for case in report["cases"])


def test_analysis_is_bounded_and_validated():
    with pytest.raises(ValueError):
        conv.analyze_pytest_source("", entry_function="solve")
    with pytest.raises(ValueError):
        conv.analyze_pytest_source("x" * (conv.MAX_SOURCE_BYTES + 1), entry_function="solve")
    with pytest.raises(ValueError):
        conv.analyze_pytest_source("def test_x():\n    assert solve(1) == 1\n",
                                   entry_function="not an identifier")
    with pytest.raises(ValueError):
        conv.analyze_pytest_source("def test_x(:\n", entry_function="solve")


def test_case_count_is_capped():
    body = "".join(f"    assert solve({index}) == {index}\n" for index in range(300))
    report = conv.analyze_pytest_source(f"def test_many():\n{body}", entry_function="solve")
    assert len(report["cases"]) <= conv.MAX_CASES_PER_GROUP


def test_build_payload_only_includes_convertible_cases():
    source = (
        "def test_x():\n"
        "    assert solve(1) == 1\n"
        "    assert solve(y) == 2\n"
    )
    report = conv.analyze_pytest_source(source, entry_function="solve")
    payload = conv.build_conversion_payload(report, entry_function="solve", max_score=30.0)
    cases = payload["groups"][0]["cases"]
    assert len(cases) == 1 and cases[0]["id"].startswith("F1:X-")
    assert payload["groups"][0]["max_score"] == 30.0
    assert payload["execution_model"] == "isolated_case_v2"
    assert payload["schema_version"] == 2
    assert cases[0]["expected"] == {"t": "int", "v": 1}

    with pytest.raises(ValueError):
        conv.build_conversion_payload(
            conv.analyze_pytest_source("def test_x():\n    assert solve(y) == 2\n",
                                       entry_function="solve"),
            entry_function="solve")


def test_summary_hides_source_details():
    report = conv.analyze_pytest_source(
        "def test_x():\n    assert solve(y) == 2\n", entry_function="solve")
    summary = conv.conversion_summary(report)
    assert summary["convertible_count"] == 0 and summary["unsupported_count"] == 1
    assert summary["reasons"] and "notes" in summary
    assert "solve" not in json.dumps(summary)


# ─────────────────────────────────────────────────────────────
# 2. 编辑端点（真实 HTTP）
# ─────────────────────────────────────────────────────────────

def _question_with_scope(db_session_factory, *, hidden_tests=None, grading_mode="legacy"):
    from tests.automated.conftest import make_judge_question

    make_teacher(db_session_factory, username="teacher")
    student = make_student(db_session_factory, username="student")
    question_id = make_judge_question(
        db_session_factory, function_name="solve", grading_mode=grading_mode,
        hidden_tests=hidden_tests or "def test_x():\n    assert solve(1, 2) == 3\n")
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        assignment = db.get(Assignment, question.assignment_id)
        course = db.get(Course, assignment.course_id)
        grant_teaching_fixture(db, course, [student.id])
        db.commit()
    return question_id


def _teacher_token(client):
    token, _ = login(client, "teacher")
    return token


def _cases_payload(case_id="F1-1", *, value=3):
    from app.services.grading_values import encode_value

    return {
        "schema_version": 1, "entry_function": "solve", "execution_model": "data_v1",
        "groups": [
            {"id": "F1", "name": "功能", "dimension": "F", "max_score": 60,
             "cases": [{"id": case_id, "input": encode_value(([1, 2],)),
                        "expected": encode_value(value), "compare": {"kind": "literal"}}]},
            {"id": "R1", "name": "边界", "dimension": "R", "max_score": 10,
             "cases": [{"id": "R1-1", "input": encode_value(([],)),
                        "expected": encode_value(0), "compare": {"kind": "literal"}}]},
        ],
    }


def test_conversion_preview_then_apply_and_activate(client, db_session_factory):
    question_id = _question_with_scope(db_session_factory)
    token = _teacher_token(client)

    preview = client.post(f"{API}/questions/{question_id}/grading-versions/convert-preview",
                          headers=auth_header(token), json={})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["summary"]["convertible_count"] == 1
    assert body["cases"][0]["status"] == "convertible"

    with db_session_factory() as db:
        assert db.query(JudgeGradingVersion).count() == 0, "预检不得写库"

    applied = client.post(f"{API}/questions/{question_id}/grading-versions/convert-apply",
                          headers=auth_header(token),
                          json={"max_score": 30, "confirm_isolated_case_v2": True})
    assert applied.status_code == 201, applied.text
    version = applied.json()["version"]
    assert version["status"] == "draft"
    assert version["source_kind"] == "converted_from_pytest"
    assert version["case_count"] == 1

    # 草稿不能直接启用
    early = client.post(f"{API}/grading-versions/{version['id']}/activate",
                        headers=auth_header(token))
    assert early.status_code == 409

    confirmed = client.post(f"{API}/grading-versions/{version['id']}/confirm",
                            headers=auth_header(token),
                            json={"expected_spec_digest": version["spec_digest"],
                                  "confirm_isolated_case_v2": True})
    assert confirmed.status_code == 200, confirmed.text
    stale = client.post(f"{API}/grading-versions/{version['id']}/confirm",
                        headers=auth_header(token),
                        json={"expected_spec_digest": "0" * 64,
                              "confirm_isolated_case_v2": True})
    assert stale.status_code == 409

    activated = client.post(f"{API}/grading-versions/{version['id']}/activate",
                            headers=auth_header(token))
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "active"

    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        assert question.current_grading_version_id == version["id"]

    # 闭环：启用后新提交绑定该版本（3B 绑定契约）
    student_token, _ = login(client, "student")
    submitted = client.post("/api/v1/judge/submissions", headers=auth_header(student_token),
                            json={"question_id": question_id, "code": "def solve(a, b):\n    return a + b"})
    assert submitted.status_code in (200, 201), submitted.text
    with db_session_factory() as db:
        assert db.get(Submission, submitted.json()["id"]).grading_version_id == version["id"]

    listing = client.get(f"{API}/questions/{question_id}/grading-versions",
                         headers=auth_header(token))
    assert listing.status_code == 200
    assert listing.json()["current_grading_version_id"] == version["id"]
    assert listing.json()["items"][0]["status"] == "active"


def test_manual_draft_and_ai_draft_require_teacher_confirmation(client, db_session_factory):
    question_id = _question_with_scope(db_session_factory)
    token = _teacher_token(client)

    manual = client.post(f"{API}/questions/{question_id}/grading-versions",
                         headers=auth_header(token),
                         json={"payload": _cases_payload(), "entry_function": "solve"})
    assert manual.status_code == 201, manual.text
    assert manual.json()["source_kind"] == "manual"

    ai = client.post(f"{API}/questions/{question_id}/grading-versions/ai-draft",
                     headers=auth_header(token),
                     json={"payload": _cases_payload("F1-2", value=5), "entry_function": "solve",
                           "model_name": "draft-model"})
    assert ai.status_code == 201, ai.text
    ai_version = ai.json()
    assert ai_version["source_kind"] == "ai_draft" and ai_version["status"] == "draft"
    assert ai_version["version"] == 2, "同题版本号递增"

    # AI 草稿不能直接激活，必须先由教师确认
    blocked = client.post(f"{API}/grading-versions/{ai_version['id']}/activate",
                          headers=auth_header(token))
    assert blocked.status_code == 409
    client.post(f"{API}/grading-versions/{ai_version['id']}/confirm",
                headers=auth_header(token), json={"confirm_isolated_case_v2": True})
    activated = client.post(f"{API}/grading-versions/{ai_version['id']}/activate",
                            headers=auth_header(token))
    assert activated.status_code == 200
    with db_session_factory() as db:
        assert db.get(JudgeGradingVersion, manual.json()["id"]).status in ("draft", "retired")


def test_activate_requires_rubric_for_ai_modes(client, db_session_factory):
    """AI 模式（active）必须绑定有效 Rubric 才能启用；不存在的 Rubric 直接被拒。"""
    from tests.automated.conftest import make_rubric

    question_id = _question_with_scope(db_session_factory, grading_mode="active")
    token = _teacher_token(client)

    missing = client.post(f"{API}/questions/{question_id}/grading-versions",
                          headers=auth_header(token),
                          json={"payload": _cases_payload(), "entry_function": "solve",
                                "rubric_id": 10 ** 9})
    assert missing.status_code == 422, missing.text
    assert missing.json()["detail"]["code"] == "INVALID_GRADING_VERSION"

    without_rubric = client.post(f"{API}/questions/{question_id}/grading-versions",
                                 headers=auth_header(token),
                                 json={"payload": _cases_payload(), "entry_function": "solve"})
    version_id = without_rubric.json()["id"]
    client.post(f"{API}/grading-versions/{version_id}/confirm", headers=auth_header(token),
                json={"confirm_isolated_case_v2": True})
    blocked = client.post(f"{API}/grading-versions/{version_id}/activate",
                          headers=auth_header(token))
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "RUBRIC_REQUIRED"

    rubric_id = make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    with_rubric = client.post(f"{API}/questions/{question_id}/grading-versions",
                              headers=auth_header(token),
                              json={"payload": _cases_payload("F1-9"), "entry_function": "solve",
                                    "rubric_id": rubric_id})
    assert with_rubric.status_code == 201, with_rubric.text
    good_id = with_rubric.json()["id"]
    client.post(f"{API}/grading-versions/{good_id}/confirm", headers=auth_header(token),
                json={"confirm_isolated_case_v2": True})
    activated = client.post(f"{API}/grading-versions/{good_id}/activate",
                            headers=auth_header(token))
    assert activated.status_code == 200, activated.text
    assert activated.json()["rubric_binding_ok"] is True


def test_invalid_payload_and_permissions(client, db_session_factory):
    question_id = _question_with_scope(db_session_factory)
    token = _teacher_token(client)

    bad = client.post(f"{API}/questions/{question_id}/grading-versions",
                      headers=auth_header(token),
                      json={"payload": {"groups": []}, "entry_function": "solve"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "INVALID_GRADING_VERSION"

    student_token, _ = login(client, "student")
    denied = client.get(f"{API}/questions/{question_id}/grading-versions",
                        headers=auth_header(student_token))
    assert denied.status_code == 403

    make_teacher(db_session_factory, username="other_teacher")
    other_token, _ = login(client, "other_teacher")
    out_of_scope = client.get(f"{API}/questions/{question_id}/grading-versions",
                              headers=auth_header(other_token))
    assert out_of_scope.status_code == 403

    missing = client.get(f"{API}/questions/999999/grading-versions", headers=auth_header(token))
    assert missing.status_code == 404


def test_conversion_preview_reports_syntax_error(client, db_session_factory):
    question_id = _question_with_scope(db_session_factory)
    token = _teacher_token(client)
    response = client.post(f"{API}/questions/{question_id}/grading-versions/convert-preview",
                           headers=auth_header(token), json={"source": "def test_x(:\n"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "CONVERSION_PREVIEW_FAILED"


def test_retire_clears_current_binding(client, db_session_factory):
    question_id = _question_with_scope(db_session_factory)
    token = _teacher_token(client)
    created = client.post(f"{API}/questions/{question_id}/grading-versions",
                          headers=auth_header(token),
                          json={"payload": _cases_payload(), "entry_function": "solve"})
    version_id = created.json()["id"]
    client.post(f"{API}/grading-versions/{version_id}/confirm", headers=auth_header(token),
                json={"confirm_isolated_case_v2": True})
    client.post(f"{API}/grading-versions/{version_id}/activate", headers=auth_header(token))
    retired = client.post(f"{API}/grading-versions/{version_id}/retire", headers=auth_header(token))
    assert retired.status_code == 200 and retired.json()["status"] == "retired"
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        assert question.current_grading_version_id is None
        # 历史版本仍可读
        assert db.get(JudgeGradingVersion, version_id).status == "retired"
    # 退役后新提交不再绑定（转人工），而不是绑定"最新版本"
    student_token, _ = login(client, "student")
    submitted = client.post("/api/v1/judge/submissions", headers=auth_header(student_token),
                            json={"question_id": question_id, "code": "def solve(a, b):\n    return a + b"})
    with db_session_factory() as db:
        assert db.get(Submission, submitted.json()["id"]).grading_version_id is None


def test_ai_draft_payload_still_validated(client, db_session_factory):
    """AI 输出同样要过用例结构校验，不能绕过。"""
    question_id = _question_with_scope(db_session_factory)
    token = _teacher_token(client)
    bad_payload = {"schema_version": 1, "groups": [{"id": "F1", "dimension": "F", "max_score": 10,
                                                    "cases": [{"id": "F1-1"}]}]}
    response = client.post(f"{API}/questions/{question_id}/grading-versions/ai-draft",
                           headers=auth_header(token),
                           json={"payload": bad_payload, "entry_function": "solve"})
    assert response.status_code == 422
    with db_session_factory() as db:
        assert db.query(JudgeGradingVersion).count() == 0


def test_version_detail_exposes_digests_and_rules(client, db_session_factory):
    question_id = _question_with_scope(db_session_factory)
    token = _teacher_token(client)
    created = client.post(f"{API}/questions/{question_id}/grading-versions",
                          headers=auth_header(token),
                          json={"payload": _cases_payload(), "entry_function": "solve",
                                "context_snapshot": {"prompt": "两数之和"}})
    version_id = created.json()["id"]
    detail = client.get(f"{API}/grading-versions/{version_id}", headers=auth_header(token))
    assert detail.status_code == 200
    body = detail.json()
    assert body["source_digest"] and body["spec_digest"]
    assert body["payload"]["binary_cases"][0]["expected"]["v"] == 3
    assert body["compare_rules"]["default"] == {"kind": "literal"}
    assert body["context_snapshot"]["prompt"] == "两数之和"
    assert body["context_snapshot"]["isolated_case_v2_confirmed"] is False
    assert body["rubric_binding_ok"] is True  # legacy 模式不要求 Rubric
    assert versions.compute_spec_digest(body["payload"], body["entry_function"],
                                        body["compare_rules"], body["context_snapshot"],
                                        body["rubric_id"], body["score_cap_rules"]) == body["spec_digest"]


# ─────────────────────────────────────────────────────────────
# 5. 启用前预检与在途门禁（3E）
# ─────────────────────────────────────────────────────────────

def _queued_submission(db_session_factory, question_id, *, status="queued"):
    from tests.automated.conftest import make_student

    from app.models import Submission

    student = make_student(db_session_factory, username=f"cap_{status}")
    student_id = getattr(student, "id", student)
    with db_session_factory() as db:
        submission = Submission(question_id=question_id, student_id=student_id,
                                code="def solve():\n    return 1\n", status="running",
                                grading_status=status, attempt_count=1)
        db.add(submission)
        db.commit()
        return submission.id


def test_preflight_reports_in_flight_and_legacy_records(client, db_session_factory):
    question_id = _question_with_scope(db_session_factory)
    token = _teacher_token(client)
    _queued_submission(db_session_factory, question_id, status="queued")
    _queued_submission(db_session_factory, question_id, status="completed")

    response = client.get(f"{API}/questions/{question_id}/grading-versions/preflight",
                          headers=auth_header(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["in_flight"] == 1
    assert body["unbound_legacy"] == 1, "已完成但没有版本基线的历史提交必须被统计"
    assert body["ready"] is False and body["blocking"]
    assert any("正在排队或判题中" in item for item in body["blocking"])
    assert any("没有评分版本基线" in item for item in body["warnings"])


def test_activation_blocked_by_in_flight_unless_acknowledged(client, db_session_factory):
    question_id = _question_with_scope(db_session_factory)
    token = _teacher_token(client)
    created = client.post(f"{API}/questions/{question_id}/grading-versions",
                          headers=auth_header(token),
                          json={"payload": _cases_payload(), "entry_function": "solve"})
    version_id = created.json()["id"]
    client.post(f"{API}/grading-versions/{version_id}/confirm", headers=auth_header(token),
                json={"confirm_isolated_case_v2": True})
    _queued_submission(db_session_factory, question_id, status="running")

    blocked = client.post(f"{API}/grading-versions/{version_id}/activate",
                          headers=auth_header(token))
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "IN_FLIGHT_GRADING"
    with db_session_factory() as db:
        assert db.get(JudgeQuestion, question_id).current_grading_version_id is None

    forced = client.post(f"{API}/grading-versions/{version_id}/activate?acknowledge_in_flight=true",
                         headers=auth_header(token))
    assert forced.status_code == 200, forced.text
    with db_session_factory() as db:
        assert db.get(JudgeQuestion, question_id).current_grading_version_id == version_id


def test_preflight_requires_teacher_scope(client, db_session_factory):
    question_id = _question_with_scope(db_session_factory)
    student_token, _ = login(client, "student")
    denied = client.get(f"{API}/questions/{question_id}/grading-versions/preflight",
                        headers=auth_header(student_token))
    assert denied.status_code == 403

    make_teacher(db_session_factory, username="other_teacher2")
    other_token, _ = login(client, "other_teacher2")
    out_of_scope = client.get(f"{API}/questions/{question_id}/grading-versions/preflight",
                              headers=auth_header(other_token))
    assert out_of_scope.status_code == 403


# ─────────────────────────────────────────────────────────────
# 6. 考试题版本端点（批次三 3C/3E）
# ─────────────────────────────────────────────────────────────

EXAM_API = "/api/v1/exams"


def _exam_question_with_scope(db_session_factory, *, grading_mode="active",
                              hidden_tests="def test_x():\n    assert solve(1, 2) == 3\n"):
    from tests.automated.conftest import (
        grant_teaching_fixture, make_exam, make_exam_question, make_rubric, make_student,
        make_teacher,
    )

    from app.models import Exam

    make_teacher(db_session_factory, username="teacher")
    student = make_student(db_session_factory, username="student")
    exam_id = make_exam(db_session_factory)
    # exam_questions 没有 function_name 列：入口函数放在 correct_answer 里
    question_id = make_exam_question(db_session_factory, exam_id=exam_id, points=20,
                                     grading_mode=grading_mode,
                                     correct_answer={"function_name": "solve"},
                                     hidden_tests=hidden_tests)
    with db_session_factory() as db:
        exam = db.get(Exam, exam_id)
        course = db.get(Course, exam.course_id)
        grant_teaching_fixture(db, course, [student.id])
        db.commit()
    rubric_id = make_rubric(db_session_factory, exam_question_id=question_id, status="locked")
    return question_id, rubric_id, exam_id


def test_exam_version_lifecycle_and_binding(client, db_session_factory):
    from tests.automated.conftest import make_exam_answer, make_exam_submission

    from app.models import ExamAnswer, ExamQuestion, ExamSubmission
    from app.services import grading_version_service as versions

    question_id, rubric_id, exam_id = _exam_question_with_scope(db_session_factory)
    token = _teacher_token(client)

    created = client.post(f"{EXAM_API}/questions/{question_id}/grading-versions",
                          headers=auth_header(token),
                          json={"payload": _cases_payload(), "entry_function": "solve",
                                "rubric_id": rubric_id})
    assert created.status_code == 201, created.text
    version = created.json()
    assert version["exam_question_id"] == question_id and version["judge_question_id"] is None
    assert version["status"] == "draft"

    blocked = client.post(f"{EXAM_API}/grading-versions/{version['id']}/activate",
                          headers=auth_header(token))
    assert blocked.status_code == 409, "草稿不能直接启用"

    client.post(f"{EXAM_API}/grading-versions/{version['id']}/confirm",
                headers=auth_header(token), json={"expected_spec_digest": version["spec_digest"],
                                                  "confirm_isolated_case_v2": True})
    activated = client.post(f"{EXAM_API}/grading-versions/{version['id']}/activate",
                            headers=auth_header(token))
    assert activated.status_code == 200, activated.text

    with db_session_factory() as db:
        assert db.get(ExamQuestion, question_id).current_grading_version_id == version["id"]

    # 开考冻结映射 → 答案绑定（与 3B/3E 执行路径闭环）
    submission_id = make_exam_submission(db_session_factory, exam_id=exam_id, status="started")
    with db_session_factory() as db:
        submission = db.get(ExamSubmission, submission_id)
        # A09：冻结映射记录版本与开考时的分值（旧裸 int 数据仍兼容）
        assert versions.freeze_exam_versions(db, submission) == {
            str(question_id): {"version_id": version["id"], "points": 20.0}}
        db.commit()
    answer_id = make_exam_answer(db_session_factory, submission_id=submission_id,
                                 question_id=question_id, code_answer="def solve(a, b):\n    return a + b\n")
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        versions.bind_exam_answer_version(db, answer)
        db.commit()
    with db_session_factory() as db:
        assert db.get(ExamAnswer, answer_id).grading_version_id == version["id"]

    listing = client.get(f"{EXAM_API}/questions/{question_id}/grading-versions",
                         headers=auth_header(token))
    assert listing.status_code == 200
    assert listing.json()["current_grading_version_id"] == version["id"]

    retired = client.post(f"{EXAM_API}/grading-versions/{version['id']}/retire",
                          headers=auth_header(token))
    assert retired.status_code == 200 and retired.json()["status"] == "retired"
    with db_session_factory() as db:
        assert db.get(ExamQuestion, question_id).current_grading_version_id is None


def test_exam_conversion_preview_and_apply(client, db_session_factory):
    question_id, rubric_id, _ = _exam_question_with_scope(db_session_factory)
    token = _teacher_token(client)

    preview = client.post(f"{EXAM_API}/questions/{question_id}/grading-versions/convert-preview",
                          headers=auth_header(token), json={"entry_function": "solve"})
    assert preview.status_code == 200, preview.text
    assert preview.json()["summary"]["convertible_count"] == 1

    applied = client.post(f"{EXAM_API}/questions/{question_id}/grading-versions/convert-apply",
                          headers=auth_header(token),
                          json={"max_score": 20, "entry_function": "solve",
                                "confirm_isolated_case_v2": True})
    assert applied.status_code == 201, applied.text
    body = applied.json()
    assert body["version"]["source_kind"] == "converted_from_pytest"
    assert body["version"]["exam_question_id"] == question_id
    assert body["summary"]["convertible_count"] == 1


def test_exam_preflight_and_in_flight_gate(client, db_session_factory):
    from tests.automated.conftest import make_exam_answer, make_exam_submission

    from app.models import ExamAnswer

    question_id, rubric_id, exam_id = _exam_question_with_scope(db_session_factory)
    token = _teacher_token(client)

    created = client.post(f"{EXAM_API}/questions/{question_id}/grading-versions",
                          headers=auth_header(token),
                          json={"payload": _cases_payload(), "entry_function": "solve",
                                "rubric_id": rubric_id})
    version_id = created.json()["id"]
    client.post(f"{EXAM_API}/grading-versions/{version_id}/confirm",
                headers=auth_header(token), json={"confirm_isolated_case_v2": True})

    submission_id = make_exam_submission(db_session_factory, exam_id=exam_id, status="started")
    answer_id = make_exam_answer(db_session_factory, submission_id=submission_id,
                                 question_id=question_id, code_answer="def solve(a, b):\n    return a + b\n")
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        answer.grading_status = "running"
        db.commit()

    preflight = client.get(f"{EXAM_API}/questions/{question_id}/grading-versions/preflight",
                           headers=auth_header(token))
    assert preflight.status_code == 200, preflight.text
    report = preflight.json()
    assert report["in_flight"] == 1
    assert report["started_exams"] >= 1, "进行中的考试场次必须被统计"
    assert report["ready"] is False

    blocked = client.post(f"{EXAM_API}/grading-versions/{version_id}/activate",
                          headers=auth_header(token))
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "IN_FLIGHT_GRADING"
    forced = client.post(
        f"{EXAM_API}/grading-versions/{version_id}/activate?acknowledge_in_flight=true",
        headers=auth_header(token))
    assert forced.status_code == 200, forced.text


def test_exam_version_permissions(client, db_session_factory):
    question_id, _, _ = _exam_question_with_scope(db_session_factory)
    student_token, _ = login(client, "student")
    assert client.get(f"{EXAM_API}/questions/{question_id}/grading-versions",
                      headers=auth_header(student_token)).status_code == 403

    make_teacher(db_session_factory, username="exam_outsider")
    outsider_token, _ = login(client, "exam_outsider")
    assert client.get(f"{EXAM_API}/questions/{question_id}/grading-versions",
                      headers=auth_header(outsider_token)).status_code == 403
    assert client.get(f"{EXAM_API}/questions/999999/grading-versions",
                      headers=auth_header(_teacher_token(client))).status_code == 404


def test_exam_entry_function_must_be_explicit(client, db_session_factory):
    """考试题既没传入口函数、correct_answer 也没有时，必须 422 而不是猜一个默认值。"""
    from tests.automated.conftest import make_exam_question

    question_id, _, _ = _exam_question_with_scope(db_session_factory)
    with db_session_factory() as db:
        db.get(ExamQuestion, question_id).correct_answer = {}
        db.commit()
    token = _teacher_token(client)
    response = client.post(f"{EXAM_API}/questions/{question_id}/grading-versions/convert-preview",
                           headers=auth_header(token), json={})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ENTRY_FUNCTION_REQUIRED"


# ─────────────────────────────────────────────────────────────
# 7. 字面量编解码（逐例表单后端）
# ─────────────────────────────────────────────────────────────

def _codec_teacher(db_session_factory, client):
    make_teacher(db_session_factory, username="teacher")
    return _teacher_token(client)


def test_literal_encode_endpoint(client, db_session_factory):
    token = _codec_teacher(db_session_factory, client)
    cases = [
        ("3", {"t": "int", "v": 3}),
        ("-2.5", {"t": "float", "v": -2.5}),
        ('"文本"', {"t": "str", "v": "文本"}),
        ("None", {"t": "none"}),
        ("True", {"t": "bool", "v": True}),
        ("[1, 2]", {"t": "list", "v": [{"t": "int", "v": 1}, {"t": "int", "v": 2}]}),
        ("(1, 2)", {"t": "tuple", "v": [{"t": "int", "v": 1}, {"t": "int", "v": 2}]}),
        ("{'a': 1}", {"t": "dict", "v": [[{"t": "str", "v": "a"}, {"t": "int", "v": 1}]]}),
    ]
    for literal, expected in cases:
        response = client.post(f"{API}/grading-versions/literal",
                               headers=auth_header(token), json={"literal": literal})
        assert response.status_code == 200, (literal, response.text)
        assert response.json()["value"] == expected, literal


@pytest.mark.parametrize("literal", [
    "open('/tmp/x','w')", "solve(1)", "__import__('os')", "1 + 2", "x", "[1, 2", "",
])
def test_literal_encode_rejects_non_literals(client, db_session_factory, literal):
    token = _codec_teacher(db_session_factory, client)
    response = client.post(f"{API}/grading-versions/literal",
                           headers=auth_header(token), json={"literal": literal})
    assert response.status_code in (422,), (literal, response.status_code)
    assert response.json()["detail"]["code"] in ("INVALID_LITERAL",) or literal == ""


def test_literal_encode_rejects_unsupported_value(client, db_session_factory):
    token = _codec_teacher(db_session_factory, client)
    response = client.post(f"{API}/grading-versions/literal",
                           headers=auth_header(token), json={"literal": "float('nan')"})
    # float('nan') 是调用，不是字面量 → 422
    assert response.status_code == 422


def test_literal_decode_endpoint_roundtrip(client, db_session_factory):
    token = _codec_teacher(db_session_factory, client)
    encoded = client.post(f"{API}/grading-versions/literal",
                          headers=auth_header(token), json={"literal": "[1, 'a']"}).json()["value"]
    decoded = client.post(f"{API}/grading-versions/literal/decode",
                          headers=auth_header(token), json={"value": encoded})
    assert decoded.status_code == 200, decoded.text
    assert decoded.json()["literal"] == repr([1, "a"])

    array = client.post(f"{API}/grading-versions/literal/decode",
                        headers=auth_header(token),
                        json={"value": {"t": "array", "dtype": "float64", "shape": [2],
                                        "data": [1.0, 2.0]}})
    assert array.status_code == 200 and array.json()["array"] is True

    bad = client.post(f"{API}/grading-versions/literal/decode",
                      headers=auth_header(token), json={"value": {"t": "nope"}})
    assert bad.status_code == 422


def test_literal_tools_require_teacher(client, db_session_factory):
    from tests.automated.conftest import make_student

    make_student(db_session_factory, username="student")
    student_token, _ = login(client, "student")
    response = client.post(f"{API}/grading-versions/literal",
                           headers=auth_header(student_token), json={"literal": "1"})
    assert response.status_code == 403


# ─────────────────────────────────────────────────────────────
# 8. AI 生成入口（生成 → AST 预检 → 草稿）
# ─────────────────────────────────────────────────────────────

def _generation_response(tests_sources, warnings=None, r_sources=None):
    """用**真实响应模型**构造 AI 生成结果（字段名与生产一致：test_groups）。"""
    from app.schemas.ai_grading import (
        TestGroup,
        TestGroupsGenerateResponse,
        TestGroupsValidationSummary,
    )

    groups = [
        TestGroup(id=f"F{index + 1}", name=f"功能{index + 1}", dimension="F",
                  max_score=60.0 / len(tests_sources), tests=source)
        for index, source in enumerate(tests_sources)
    ]
    groups.extend(
        TestGroup(id=f"R{index + 1}", name=f"边界{index + 1}", dimension="R",
                  max_score=10.0 / len(r_sources), tests=source)
        for index, source in enumerate(r_sources or [])
    )
    return TestGroupsGenerateResponse(
        test_groups=groups,
        validation=TestGroupsValidationSummary(
            f_total=60.0, r_total=10.0 if r_sources else 0.0, group_count=len(groups),
            f_group_count=len(tests_sources), r_group_count=len(r_sources or [])),
        warnings=list(warnings or []),
        generation_id="gen-test-1",
    )


def _patch_generator(monkeypatch, result=None, error=None):
    from app.services import test_group_generator

    def fake(client, snapshot, settings, **kwargs):
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(test_group_generator, "generate_test_groups", fake)


def test_ai_generate_creates_draft_from_convertible_groups(client, db_session_factory, monkeypatch):
    from tests.automated.conftest import make_rubric

    question_id = _question_with_scope(db_session_factory, grading_mode="active")
    make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    token = _teacher_token(client)
    _patch_generator(monkeypatch, _generation_response([
        "def test_add():\n    assert solve(1, 2) == 3\n",
    ], r_sources=["def test_empty():\n    assert solve() == 0\n"]))

    response = client.post(f"{API}/questions/{question_id}/grading-versions/ai-generate",
                           headers=auth_header(token), json={"entry_function": "solve"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version"]["source_kind"] == "ai_draft"
    assert body["version"]["status"] == "draft", "AI 只能产生草稿"
    assert body["version"]["case_count"] == 2
    assert body["summary"]["convertible_count"] == 2
    assert [(group["id"], group["dimension"], group["max_score"])
            for group in body["version"]["payload"]["groups"]] == [
                ("F1", "F", 60.0), ("R1", "R", 10.0)]
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        assert question.current_grading_version_id is None, "AI 生成不得自动启用"
        assert db.query(JudgeGradingVersion).count() == 1


def test_ai_generate_rejects_non_convertible_groups(client, db_session_factory, monkeypatch):
    from tests.automated.conftest import make_rubric

    question_id = _question_with_scope(db_session_factory, grading_mode="active")
    make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    token = _teacher_token(client)
    _patch_generator(monkeypatch, _generation_response([
        "def test_x():\n    for i in range(3):\n        assert solve(i) == i\n",
    ]))
    response = client.post(f"{API}/questions/{question_id}/grading-versions/ai-generate",
                           headers=auth_header(token), json={"entry_function": "solve"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "AI_GENERATION_NOT_CONVERTIBLE"
    with db_session_factory() as db:
        assert db.query(JudgeGradingVersion).count() == 0, "不可转换时不得留下任何版本"


def test_ai_generate_rejects_missing_robustness_dimension(client, db_session_factory, monkeypatch):
    from tests.automated.conftest import make_rubric

    question_id = _question_with_scope(db_session_factory, grading_mode="active")
    make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    token = _teacher_token(client)
    _patch_generator(monkeypatch, _generation_response([
        "def test_add():\n    assert solve(1, 2) == 3\n",
    ]))
    response = client.post(f"{API}/questions/{question_id}/grading-versions/ai-generate",
                           headers=auth_header(token), json={"entry_function": "solve"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "AI_GENERATION_NOT_CONVERTIBLE"
    with db_session_factory() as db:
        assert db.query(JudgeGradingVersion).count() == 0


def test_ai_generate_maps_service_errors(client, db_session_factory, monkeypatch):
    from app.services.ai_client import AIServiceError
    from tests.automated.conftest import make_rubric

    question_id = _question_with_scope(db_session_factory, grading_mode="active")
    make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    token = _teacher_token(client)
    _patch_generator(monkeypatch, error=AIServiceError("timeout", "timeout", retryable=True))
    response = client.post(f"{API}/questions/{question_id}/grading-versions/ai-generate",
                           headers=auth_header(token), json={"entry_function": "solve"})
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "AI_GENERATION_TIMEOUT"


def test_ai_generate_requires_ready_ai(client, db_session_factory, monkeypatch, app):
    from app.config import Settings, get_settings

    question_id = _question_with_scope(db_session_factory)
    token = _teacher_token(client)
    override = app.dependency_overrides.get(get_settings)
    base = override() if callable(override) else (override or get_settings())
    disabled = base.model_copy(update={"ai_enabled": False, "ai_api_key": ""})
    assert isinstance(disabled, Settings)
    app.dependency_overrides[get_settings] = lambda: disabled
    try:
        response = client.post(f"{API}/questions/{question_id}/grading-versions/ai-generate",
                               headers=auth_header(token), json={"entry_function": "solve"})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "AI_NOT_READY"
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_ai_generate_permissions_and_entry_function(client, db_session_factory, monkeypatch):
    question_id = _question_with_scope(db_session_factory)
    _patch_generator(monkeypatch, _generation_response([
        "def test_x():\n    assert solve(1) == 1\n"]))
    student_token, _ = login(client, "student")
    assert client.post(f"{API}/questions/{question_id}/grading-versions/ai-generate",
                       headers=auth_header(student_token),
                       json={"entry_function": "solve"}).status_code == 403

    token = _teacher_token(client)
    bad = client.post(f"{API}/questions/{question_id}/grading-versions/ai-generate",
                      headers=auth_header(token), json={"entry_function": "not an identifier"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "ENTRY_FUNCTION_REQUIRED"


# ─────────────────────────────────────────────────────────────
# A10：不完整转换不得当作等价迁移
# ─────────────────────────────────────────────────────────────

PARTIAL_SOURCE = (
    "def test_ok():\n"
    "    assert solve(1, 2) == 3\n"
    "def test_loop():\n"
    "    for i in range(3):\n"
    "        assert solve(i) == i\n"
)


def test_a10_partial_conversion_requires_explicit_confirmation(client, db_session_factory):
    question_id = _question_with_scope(db_session_factory, hidden_tests=PARTIAL_SOURCE)
    token = _teacher_token(client)

    preview = client.post(f"{API}/questions/{question_id}/grading-versions/convert-preview",
                          headers=auth_header(token), json={})
    assert preview.status_code == 200
    assert preview.json()["summary"]["convertible_count"] == 1
    assert preview.json()["summary"]["unsupported_count"] >= 1

    blocked = client.post(f"{API}/questions/{question_id}/grading-versions/convert-apply",
                          headers=auth_header(token), json={"max_score": 30})
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["detail"]["code"] == "CONVERSION_INCOMPLETE"
    assert blocked.json()["detail"]["fields"]["unsupported_count"] >= 1
    with db_session_factory() as db:
        assert db.query(JudgeGradingVersion).count() == 0, "未确认时不得留下任何版本"

    allowed = client.post(f"{API}/questions/{question_id}/grading-versions/convert-apply",
                          headers=auth_header(token),
                          json={"max_score": 30, "allow_partial": True,
                                "confirm_isolated_case_v2": True})
    assert allowed.status_code == 201, allowed.text
    version = allowed.json()["version"]
    assert version["status"] == "draft"
    assert version["case_count"] == 1, "只落可转换用例"
    assert version["context_snapshot"]["partial_conversion"] is True
    assert version["context_snapshot"]["unsupported_count"] >= 1


def test_a10_complete_conversion_needs_no_flag(client, db_session_factory):
    question_id = _question_with_scope(
        db_session_factory,
        hidden_tests="def test_x():\n    assert solve(1, 2) == 3\n")
    token = _teacher_token(client)
    applied = client.post(f"{API}/questions/{question_id}/grading-versions/convert-apply",
                          headers=auth_header(token),
                          json={"max_score": 30, "confirm_isolated_case_v2": True})
    assert applied.status_code == 201, applied.text
    assert applied.json()["version"]["context_snapshot"].get("partial_conversion") is None


def test_a10_exam_partial_conversion_is_gated(client, db_session_factory):
    question_id, _, _ = _exam_question_with_scope(db_session_factory, hidden_tests=PARTIAL_SOURCE)
    token = _teacher_token(client)
    blocked = client.post(f"{EXAM_API}/questions/{question_id}/grading-versions/convert-apply",
                          headers=auth_header(token),
                          json={"max_score": 20, "entry_function": "solve"})
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "CONVERSION_INCOMPLETE"
