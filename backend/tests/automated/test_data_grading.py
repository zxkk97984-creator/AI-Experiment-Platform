"""批次三 3E：data_v1 平台侧编排与 Worker 执行路径。

分两层：

1. ``data_grading`` 纯函数层：用例规划（容器输入不含期望值）、帧比较、分组计分、
   平台错误转人工；
2. Worker 路径：用**假的容器执行**替换 Docker，验证端到端写入语义——
   绑定版本 → 计分 → CodeGrade（含 grading_version_id 证据）→ 入队 AI；
   以及各类失败关闭（模式不一致、无版本基线、平台错误、容器失败）。
"""
from __future__ import annotations

import json

import pytest

from app.models import (
    CodeGrade,
    ExamAnswer,
    ExamQuestion,
    JudgeGradingVersion,
    JudgeQuestion,
    Submission,
)
from app.services import data_grading
from app.services.data_grading import DataGradingConfigError
from app.services.grading_runner import RESULT_PREFIX
from app.services.grading_values import encode_value

pytest_plugins = ["tests.automated.conftest"]


def _version_payload(*, f_cases=2, r_cases=1, f_score=60.0, r_score=10.0):
    groups = [{
        "id": "F1", "name": "功能", "dimension": "F", "max_score": f_score,
        "cases": [{"id": f"F1-{index}", "input": encode_value(([index],)),
                   "expected": encode_value(index * 2), "compare": {"kind": "literal"}}
                  for index in range(1, f_cases + 1)],
    }]
    if r_cases:
        groups.append({
            "id": "R1", "name": "边界", "dimension": "R", "max_score": r_score,
            "cases": [{"id": "R1-1", "input": encode_value(([],)),
                       "expected": encode_value(None), "compare": {"kind": "identity"}}],
        })
    return {"schema_version": 1, "entry_function": "solve", "execution_model": "data_v1",
            "groups": groups}


class _FakeVersion:
    def __init__(self, payload, entry_function="solve", version_id=1):
        self.payload = payload
        self.entry_function = entry_function
        self.id = version_id


def _sign(frame: dict) -> str:
    payload = json.dumps(frame, ensure_ascii=False, sort_keys=True)
    return RESULT_PREFIX + payload


def _frame(case_id, value):
    return _sign({"id": case_id, "status": "ok", "value": encode_value(value)})


def _exception_frame(case_id, exc_type="ValueError"):
    return _sign({"id": case_id, "status": "exception",
                  "exception": {"type": exc_type, "message": "boom"}})


# ─────────────────────────────────────────────────────────────
# 1. 平台侧编排
# ─────────────────────────────────────────────────────────────

def test_plan_cases_keeps_expectations_off_the_container():
    version = _FakeVersion(_version_payload())
    container_payload, expectations = data_grading.plan_cases(version)
    assert container_payload["entry_function"] == "solve"
    assert [case["id"] for case in container_payload["cases"]] == ["F1-1", "F1-2", "R1-1"]
    dumped = json.dumps(container_payload, ensure_ascii=False)
    for forbidden in ("expected", "max_score", "weight", "compare"):
        assert forbidden not in dumped, forbidden
    assert len(expectations) == 3
    assert expectations[0]["group_id"] == "F1" and expectations[0]["max_score"] == 60.0
    assert expectations[2]["dimension"] == "R"


@pytest.mark.parametrize("payload,reason", [
    ({"groups": []}, "缺少用例组"),
    ({"groups": [{"id": "F1", "dimension": "X", "max_score": 10, "cases": [{}]}]}, "dimension"),
    ({"groups": [{"id": "F1", "dimension": "F", "max_score": 0, "cases": [{}]}]}, "max_score"),
    ({"groups": [{"id": "F1", "dimension": "F", "max_score": 10, "cases": []}]}, "没有用例"),
])
def test_plan_cases_rejects_invalid_versions(payload, reason):
    with pytest.raises(DataGradingConfigError) as error:
        data_grading.plan_cases(_FakeVersion(payload))
    assert reason in str(error.value)


def test_write_runner_files_never_writes_expectations(tmp_path):
    version = _FakeVersion(_version_payload())
    container_payload, _ = data_grading.plan_cases(version)
    data_grading.write_runner_files(tmp_path, entry_function="solve",
                                    container_payload=container_payload,
                                    student_code="def solve(values):\n    return len(values) * 2\n")
    assert (tmp_path / data_grading.RUNNER_FILENAME).exists()
    cases_text = (tmp_path / data_grading.CASES_FILENAME).read_text(encoding="utf-8")
    assert "expected" not in cases_text
    assert (tmp_path / "user_code.py").read_text(encoding="utf-8").startswith("def solve")


def test_grade_output_scores_full_and_partial():
    _, expectations = data_grading.plan_cases(_FakeVersion(_version_payload()))
    full = data_grading.grade_output(
        "\n".join([_frame("F1-1", 2), _frame("F1-2", 4), _frame("R1-1", None)]), expectations)
    assert full["f_score"] == 60.0 and full["r_score"] == 10.0
    assert full["tests_passed"] == 3 and full["tests_total"] == 3
    assert full["needs_review"] is False

    partial = data_grading.grade_output(
        "\n".join([_frame("F1-1", 2), _frame("F1-2", 999), _frame("R1-1", None)]), expectations)
    assert partial["f_score"] == 30.0, "组内等权：2 例通过 1 例"
    assert partial["r_score"] == 10.0
    assert partial["tests_passed"] == 2 and partial["tests_total"] == 3
    assert partial["needs_review"] is False


def test_grade_output_treats_wrong_answer_as_student_failure():
    _, expectations = data_grading.plan_cases(_FakeVersion(_version_payload()))
    graded = data_grading.grade_output(
        "\n".join([_frame("F1-1", 2), _frame("F1-2", 4), _exception_frame("R1-1", "TypeError")]),
        expectations)
    assert graded["r_score"] == 0.0 and graded["needs_review"] is False
    r_group = next(group for group in graded["groups"] if group["id"] == "R1")
    assert r_group["cases"][0]["outcome"] == "failed"


def test_grade_output_expected_exception_passes():
    payload = {"schema_version": 1, "entry_function": "solve", "execution_model": "data_v1",
               "groups": [{"id": "R1", "dimension": "R", "max_score": 10,
                           "cases": [{"id": "R1-1", "input": encode_value(([0],)),
                                      "expected": encode_value(None),
                                      "compare": {"kind": "exception", "types": ["ValueError"]}}]}]}
    _, expectations = data_grading.plan_cases(_FakeVersion(payload))
    graded = data_grading.grade_output(_exception_frame("R1-1", "ValueError"), expectations)
    assert graded["r_score"] == 10.0 and graded["needs_review"] is False


def test_grade_output_platform_errors_block_scoring():
    _, expectations = data_grading.plan_cases(_FakeVersion(_version_payload()))

    missing = data_grading.grade_output(_frame("F1-1", 2), expectations)
    assert missing["needs_review"] is True
    assert missing["f_score"] is None, "存在平台错误时不能产出任何分数"
    assert any("缺少用例结果" in item for item in missing["platform_errors"])

    malformed = data_grading.grade_output(
        "\n".join([_frame("F1-1", 2), _frame("F1-2", 4),
                   RESULT_PREFIX + "deadbeef " + json.dumps(
                       {"id": "R1-1", "status": "ok", "value": "no"}),
                   ]), expectations)
    assert malformed["needs_review"] is True
    assert any("R1-1" in item for item in malformed["platform_errors"])
    r_group = next(group for group in malformed["groups"] if group["id"] == "R1")
    assert r_group["scored"] is False and r_group["earned"] == 0.0

    duplicate = data_grading.grade_output(
        "\n".join([_frame("F1-1", 2), _frame("F1-1", 2), _frame("F1-2", 4), _frame("R1-1", None)]),
        expectations)
    assert duplicate["needs_review"] is True
    assert any("重复" in item for item in duplicate["platform_errors"])


def test_single_case_preview():
    version = _FakeVersion(_version_payload())
    assert data_grading.single_case_preview(version, "F1-1", 2)["outcome"] == "passed"
    assert data_grading.single_case_preview(version, "F1-1", 3)["outcome"] == "failed"
    with pytest.raises(DataGradingConfigError):
        data_grading.single_case_preview(version, "NOPE", 1)


# ─────────────────────────────────────────────────────────────
# 2. Worker 端到端（假容器）
# ─────────────────────────────────────────────────────────────

def _data_case(db_session_factory, *, grading_mode="active", bind=True, status="queued"):
    """建题目 + 提交 + data_v1 版本；返回 (submission_id, version_id, question_id)。"""
    from tests.automated.conftest import (
        grant_judge_claim, grant_teaching_fixture, make_assignment, make_course,
        make_judge_question, make_student, make_teacher,
    )
    from app.models import Assignment, Course
    from app.services import grading_version_service as versions

    make_teacher(db_session_factory, username="teacher")
    student = make_student(db_session_factory, username="student")
    question_id = make_judge_question(db_session_factory, grading_mode=grading_mode)
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        assignment = db.get(Assignment, question.assignment_id)
        course = db.get(Course, assignment.course_id)
        grant_teaching_fixture(db, course, [student.id])
        db.commit()

    from tests.automated.conftest import make_rubric

    rubric_id = make_rubric(db_session_factory, judge_question_id=question_id, status="locked")
    with db_session_factory() as db:
        version = versions.create_draft(
            db, question_kind="assignment", question_id=question_id,
            payload=_version_payload(), entry_function="solve", rubric_id=rubric_id,
            actor_id=None)
        versions.confirm_version(db, version, confirm_isolated_case_v2=True)
        versions.activate_version(db, version)
        versions.set_question_current_version(db, question_kind="assignment",
                                             question_id=question_id, version_id=version.id)
        db.commit()
        version_id = version.id

    with db_session_factory() as db:
        submission = Submission(question_id=question_id, student_id=student.id,
                                code="def solve(values):\n    return len(values) * 2\n",
                                status=status, grading_status="pending", attempt_count=0,
                                grading_version_id=version_id if bind else None)
        db.add(submission)
        db.commit()
        submission_id = submission.id
    return submission_id, version_id, question_id


def _run_worker(db_session_factory, redis_client, test_settings, submission_id, frames):
    """``frames`` 是当前隔离容器返回的实际值帧；评分仍由宿主 expected 表完成。"""
    from app.worker import judge_worker

    calls = {"runner": 0, "pytest": 0}

    def fake_runner(*args, **kwargs):
        calls["runner"] += 1
        protocol = "\n".join(_sign(frame) for frame in frames)
        return "", protocol, 0, 12

    def fake_pytest(*args, **kwargs):
        calls["pytest"] += 1
        return "1 passed", "", 0, 9

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(judge_worker, "_run_docker_data_runner", fake_runner)
    monkeypatch.setattr(judge_worker, "_run_docker_pytest", fake_pytest)
    monkeypatch.setattr(judge_worker, "_submission_env_context",
                        lambda db, submission: (None, None, set()))
    monkeypatch.setattr(judge_worker, "_diagnose_submission", lambda *a, **k: None)
    monkeypatch.setattr(judge_worker, "_enqueue_ai_after_commit",
                        lambda db, redis, cg: True)
    try:
        # process_submission 走真实认领流程：先置 queued，再 claim（queued → running）
        with db_session_factory() as db:
            row = db.get(Submission, submission_id)
            row.grading_status = "queued"
            db.commit()
        with db_session_factory() as db:
            judge_worker.process_submission(db, redis_client, test_settings, submission_id)
    finally:
        monkeypatch.undo()
    return calls


def test_worker_data_v1_scores_and_creates_code_grade(db_session_factory, redis_client, test_settings):
    submission_id, version_id, _ = _data_case(db_session_factory)
    frames = [{"id": "F1-1", "status": "ok", "value": encode_value(2)},
              {"id": "F1-2", "status": "ok", "value": encode_value(4)},
              {"id": "R1-1", "status": "ok", "value": encode_value(None)}]
    calls = _run_worker(db_session_factory, redis_client, test_settings, submission_id, frames)
    assert calls == {"runner": 1, "pytest": 0}, "data_v1 版本必须走 runner，不跑 pytest"

    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        assert submission.grading_status == "completed"
        assert submission.status == "running", "active 模式等 AI 合分"
        assert submission.tests_passed == 3 and submission.tests_total == 3
        assert submission.result_details["grading_version_id"] == version_id
        assert submission.result_details["f_score"] == 60.0
        grade = db.query(CodeGrade).filter_by(submission_id=submission_id).one()
        assert grade.functional_score == 60.0 and grade.robustness_score == 10.0
        assert grade.deterministic_details["grading_version_id"] == version_id
        assert grade.deterministic_details["source"]["grading_version_id"] == version_id
        assert grade.deterministic_details["source"]["judge_claim_id"]


def test_worker_data_v1_partial_score(db_session_factory, redis_client, test_settings):
    submission_id, _, _ = _data_case(db_session_factory)
    frames = [{"id": "F1-1", "status": "ok", "value": encode_value(2)},
              {"id": "F1-2", "status": "ok", "value": encode_value(12345)},
              {"id": "R1-1", "status": "ok", "value": encode_value(None)}]
    _run_worker(db_session_factory, redis_client, test_settings, submission_id, frames)
    with db_session_factory() as db:
        grade = db.query(CodeGrade).filter_by(submission_id=submission_id).one()
        assert grade.functional_score == 30.0 and grade.robustness_score == 10.0


def test_worker_data_v1_platform_error_is_not_zero_score(db_session_factory, redis_client, test_settings):
    submission_id, _, _ = _data_case(db_session_factory)
    frames = [{"id": "F1-1", "status": "ok", "value": encode_value(2)}]  # 缺两例 → 平台错误
    _run_worker(db_session_factory, redis_client, test_settings, submission_id, frames)
    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        assert submission.status == "system_error"
        assert submission.score is None, "平台问题绝不计 0 分"
        assert submission.result_details["platform_errors"]
        assert db.query(CodeGrade).filter_by(submission_id=submission_id).count() == 0


def test_worker_uses_frozen_grading_mode_not_live_question_mode(
        db_session_factory, redis_client, test_settings):
    submission_id, version_id, question_id = _data_case(db_session_factory, grading_mode="legacy")
    with db_session_factory() as db:
        db.get(JudgeQuestion, question_id).grading_mode = "active"
        db.commit()
    frames = [{"id": "F1-1", "status": "ok", "value": encode_value(2)},
              {"id": "F1-2", "status": "ok", "value": encode_value(4)},
              {"id": "R1-1", "status": "ok", "value": encode_value(None)}]
    calls = _run_worker(db_session_factory, redis_client, test_settings, submission_id, frames)
    assert calls == {"runner": 1, "pytest": 0}
    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        assert submission.status == "graded" and submission.score == 100.0
        assert submission.grading_version_id == version_id
        assert db.query(CodeGrade).filter_by(submission_id=submission_id).count() == 0


def test_worker_missing_baseline_with_active_data_version_fails_closed(
        db_session_factory, redis_client, test_settings):
    submission_id, _, _ = _data_case(db_session_factory, bind=False)
    calls = _run_worker(db_session_factory, redis_client, test_settings, submission_id,
                        [{"id": "F1-1", "status": "ok", "value": encode_value(2)}])
    assert calls == {"runner": 0, "pytest": 0}
    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        assert submission.status == "review_required" and submission.score is None
        assert submission.result_details["manual_review"]["required"] is True


def test_worker_without_any_data_version_routes_to_manual_review(
        db_session_factory, redis_client, test_settings):
    """没有可信版本的正式作业提交不得回退到 pytest 自动正式评分。"""
    from tests.automated.conftest import (
        grant_teaching_fixture, make_assignment, make_course, make_judge_question,
        make_student, make_teacher,
    )
    from app.models import Assignment, Course

    make_teacher(db_session_factory, username="teacher")
    student = make_student(db_session_factory, username="student")
    question_id = make_judge_question(db_session_factory, grading_mode="legacy")
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        assignment = db.get(Assignment, question.assignment_id)
        course = db.get(Course, assignment.course_id)
        grant_teaching_fixture(db, course, [student.id])
        db.commit()
        submission = Submission(question_id=question_id, student_id=student.id,
                                code="def solve():\n    return 1\n", status="queued",
                                grading_status="pending", attempt_count=0)
        db.add(submission)
        db.commit()
        submission_id = submission.id

    calls = _run_worker(db_session_factory, redis_client, test_settings, submission_id, [])
    assert calls["pytest"] == 0 and calls["runner"] == 0
    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        assert submission.status == "review_required" and submission.score is None


# ─────────────────────────────────────────────────────────────
# 3. 真实容器端到端（opt-in）
# ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(__import__("os").environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
                    reason="需要真实 Docker；设置 DAI_RUN_DOCKER_SMOKE=1 启用")
def test_real_container_data_grading_end_to_end(tmp_path, test_settings):
    """真实判题镜像：写入 runner+用例+学生代码 → 沙箱执行 → 平台侧计分。"""
    import os

    from app.worker import judge_worker

    image = os.environ.get("DAI_JUDGE_IMAGE", "dai-judge-python:latest")
    version = _FakeVersion(_version_payload())
    container_payload, expectations = data_grading.plan_cases(version)
    # 用例：([1],)→2、([2],)→4、([])→None（与 _version_payload 的期望一致）
    student_code = (
        "def solve(values):\n"
        "    if not values:\n"
        "        return None\n"
        "    return values[0] * 2\n"
    )
    data_grading.write_runner_files(tmp_path, entry_function="solve",
                                    container_payload=container_payload,
                                    student_code=student_code)

    from app.services.grading_runner import build_case_stream

    settings = test_settings.model_copy(update={"judge_image": image, "judge_cpu_limit": 1})
    stdout, stderr, returncode, elapsed = judge_worker._run_docker_data_runner(
        tmp_path, settings, 120, 256, host_workdir=tmp_path, image_ref=image,
        cases=build_case_stream(container_payload))
    assert returncode == 0, stderr
    graded = data_grading.grade_output(stderr, expectations, entry_function="solve", diagnostic_text=stdout)
    assert graded["needs_review"] is False, graded["platform_errors"]
    assert graded["f_score"] == 60.0 and graded["r_score"] == 10.0
    assert graded["tests_passed"] == 3 and elapsed > 0


@pytest.mark.skipif(__import__("os").environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
                    reason="需要真实 Docker；设置 DAI_RUN_DOCKER_SMOKE=1 启用")
def test_real_container_platform_error_never_scores(tmp_path, test_settings):
    """学生代码返回不支持对象：容器回传异常帧，平台按学生失败处理（不是平台错误）。"""
    import os

    from app.worker import judge_worker

    image = os.environ.get("DAI_JUDGE_IMAGE", "dai-judge-python:latest")
    version = _FakeVersion(_version_payload())
    container_payload, expectations = data_grading.plan_cases(version)
    data_grading.write_runner_files(tmp_path, entry_function="solve",
                                    container_payload=container_payload,
                                    student_code="def solve(values):\n    return object()\n")
    from app.services.grading_runner import build_case_stream

    settings = test_settings.model_copy(update={"judge_image": image, "judge_cpu_limit": 1})
    stdout, stderr, returncode, _ = judge_worker._run_docker_data_runner(
        tmp_path, settings, 120, 256, host_workdir=tmp_path, image_ref=image,
        cases=build_case_stream(container_payload))
    assert returncode == 0, stderr
    graded = data_grading.grade_output(stderr, expectations, entry_function="solve", diagnostic_text=stdout)
    assert graded["needs_review"] is False, "学生返回不支持对象是学生错误，不是平台错误"
    assert graded["f_score"] == 0.0 and graded["r_score"] == 0.0


# ─────────────────────────────────────────────────────────────
# 4. 考试答案的 data_v1 路径
# ─────────────────────────────────────────────────────────────

def _exam_data_case(db_session_factory, *, grading_mode="active", freeze=True):
    """建考试 + 编程题 + 答案 + data_v1 版本（含冻结映射）。"""
    from tests.automated.conftest import (
        make_exam, make_exam_answer, make_exam_question, make_exam_submission,
        make_rubric, make_student,
    )
    from app.models import ExamQuestion, ExamSubmission
    from app.services import grading_version_service as versions

    make_student(db_session_factory, username="student")
    exam_id = make_exam(db_session_factory)
    question_id = make_exam_question(db_session_factory, exam_id=exam_id, points=10,
                                     grading_mode=grading_mode)
    rubric_id = make_rubric(db_session_factory, exam_question_id=question_id, status="locked")
    with db_session_factory() as db:
        version = versions.create_draft(
            db, question_kind="exam", question_id=question_id, payload=_version_payload(),
            entry_function="solve", rubric_id=rubric_id, actor_id=None)
        versions.confirm_version(db, version, confirm_isolated_case_v2=True)
        versions.activate_version(db, version)
        versions.set_question_current_version(db, question_kind="exam",
                                             question_id=question_id, version_id=version.id)
        db.commit()
        version_id = version.id

    submission_id = make_exam_submission(db_session_factory, exam_id=exam_id, status="grading")
    with db_session_factory() as db:
        submission = db.get(ExamSubmission, submission_id)
        if freeze:
            versions.freeze_exam_versions(db, submission)
        db.commit()
    answer_id = make_exam_answer(db_session_factory, submission_id=submission_id,
                                 question_id=question_id,
                                 code_answer="def solve(values):\n    return values[0] * 2\n")
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        if freeze:
            versions.bind_exam_answer_version(db, answer)
            db.commit()
    return answer_id, version_id, question_id, submission_id


def _run_exam_worker(db_session_factory, redis_client, test_settings, answer_id, frames):
    from app.worker import judge_worker

    calls = {"runner": 0, "pytest": 0, "finalize": 0}

    def fake_runner(*a, **k):
        calls["runner"] += 1
        return "", "\n".join(_sign(frame) for frame in frames), 0, 11

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(judge_worker, "_run_docker_data_runner", fake_runner)
    monkeypatch.setattr(judge_worker, "_run_docker_pytest",
                        lambda *a, **k: (calls.__setitem__("pytest", calls["pytest"] + 1)
                                         or ("1 passed", "", 0, 9)))
    monkeypatch.setattr(judge_worker, "_enqueue_ai_after_commit", lambda db, redis, cg: True)
    monkeypatch.setattr(judge_worker, "_maybe_finalize_exam",
                        lambda submission_id, db: calls.__setitem__("finalize", calls["finalize"] + 1))
    try:
        with db_session_factory() as db:
            answer = db.get(ExamAnswer, answer_id)
            answer.grading_status = "queued"
            db.commit()
        with db_session_factory() as db:
            judge_worker.process_exam_answer(db, redis_client, test_settings, answer_id)
    finally:
        monkeypatch.undo()
    return calls


def test_exam_data_v1_scores_and_creates_code_grade(db_session_factory, redis_client, test_settings):
    answer_id, version_id, _, _ = _exam_data_case(db_session_factory)
    frames = [{"id": "F1-1", "status": "ok", "value": encode_value(2)},
              {"id": "F1-2", "status": "ok", "value": encode_value(4)},
              {"id": "R1-1", "status": "ok", "value": encode_value(None)}]
    calls = _run_exam_worker(db_session_factory, redis_client, test_settings, answer_id, frames)
    assert calls["runner"] == 1 and calls["pytest"] == 0
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        assert answer.grading_status == "completed"
        assert answer.tests_passed == 3 and answer.tests_total == 3
        assert answer.result_details["grading_version_id"] == version_id
        assert answer.result_details["f_score"] == 60.0
        grade = db.query(CodeGrade).filter_by(exam_answer_id=answer_id).one()
        assert grade.functional_score == 60.0 and grade.robustness_score == 10.0
        assert grade.deterministic_details["source"]["grading_version_id"] == version_id
        assert grade.deterministic_details["source"]["judge_claim_id"]


def test_exam_data_v1_platform_error_is_not_zero_score(db_session_factory, redis_client, test_settings):
    answer_id, _, _, _ = _exam_data_case(db_session_factory)
    calls = _run_exam_worker(db_session_factory, redis_client, test_settings, answer_id,
                             [{"id": "F1-1", "status": "ok", "value": encode_value(2)}])
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        assert answer.grading_status == "system_error"
        assert answer.score is None, "平台问题不得计 0 分"
        assert answer.result_details["platform_errors"]
        assert db.query(CodeGrade).filter_by(exam_answer_id=answer_id).count() == 0
    assert calls["finalize"] >= 1, "终态后仍要推进本场考试汇总"


def test_exam_worker_uses_frozen_grading_mode(db_session_factory, redis_client, test_settings):
    answer_id, version_id, question_id, _ = _exam_data_case(db_session_factory, grading_mode="legacy")
    with db_session_factory() as db:
        db.get(ExamQuestion, question_id).grading_mode = "active"
        db.commit()
    frames = [{"id": "F1-1", "status": "ok", "value": encode_value(2)},
              {"id": "F1-2", "status": "ok", "value": encode_value(4)},
              {"id": "R1-1", "status": "ok", "value": encode_value(None)}]
    calls = _run_exam_worker(db_session_factory, redis_client, test_settings, answer_id, frames)
    assert calls["runner"] == 1 and calls["pytest"] == 0
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        assert answer.grading_status == "completed" and answer.score == 10.0
        assert answer.grading_version_id == version_id


def test_exam_answer_without_binding_but_frozen_data_version_fails_closed(
        db_session_factory, redis_client, test_settings):
    answer_id, _, _, _ = _exam_data_case(db_session_factory, freeze=True)
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        answer.grading_version_id = None  # 模拟旧数据：冻结映射里有版本，答案却没绑定
        db.commit()
    calls = _run_exam_worker(db_session_factory, redis_client, test_settings, answer_id,
                             [{"id": "F1-1", "status": "ok", "value": encode_value(2)}])
    assert calls["runner"] == 0 and calls["pytest"] == 0
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
    assert answer.grading_status == "system_error" and answer.score is None


def test_exam_without_data_version_fails_closed_to_manual_review(
        db_session_factory, redis_client, test_settings):
    """没有开考版本快照的历史考试必须转人工，不能回退到 pytest 自动正式评分。"""
    from tests.automated.conftest import make_exam_answer, make_exam_question, make_exam_submission

    make_exam_question(db_session_factory, grading_mode="legacy",
                       hidden_tests="def test_x():\n    assert True\n")
    question_id = None
    with db_session_factory() as db:
        question_id = db.query(ExamQuestion).order_by(ExamQuestion.id.desc()).first().id
        exam_id = db.get(ExamQuestion, question_id).exam_id
    submission_id = make_exam_submission(db_session_factory, exam_id=exam_id, status="grading")
    answer_id = make_exam_answer(db_session_factory, submission_id=submission_id,
                                 question_id=question_id, code_answer="def solve():\n    return 1\n")
    calls = _run_exam_worker(db_session_factory, redis_client, test_settings, answer_id, [])
    assert calls["pytest"] == 0 and calls["runner"] == 0
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        assert answer.grading_status == "system_error" and answer.score is None


@pytest.mark.skipif(__import__("os").environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
                    reason="需要真实 Docker；设置 DAI_RUN_DOCKER_SMOKE=1 启用")
def test_real_container_bytes_input_and_dict_output(tmp_path, test_settings):
    """A11：bytes 输入与普通 dict 输出必须能穿过真实 runner 并完成可信比较。"""
    import os

    from app.worker import judge_worker

    from app.services.grading_values import encode_value

    payload = {
        "schema_version": 1, "entry_function": "summarize", "execution_model": "data_v1",
        "groups": [{"id": "F1", "name": "功能", "dimension": "F", "max_score": 60,
                    "cases": [
                        {"id": "F1-1", "input": encode_value((b"abc",)),
                         "expected": encode_value({"length": 3}), "compare": {"kind": "literal"}},
                    ]}],
    }
    version = _FakeVersion(payload, entry_function="summarize")
    container_payload, expectations = data_grading.plan_cases(version)
    student_code = (
        "def summarize(raw):\n"
        "    return {'length': len(raw)}\n"
    )
    data_grading.write_runner_files(tmp_path, entry_function="summarize",
                                    container_payload=container_payload,
                                    student_code=student_code)
    image = os.environ.get("DAI_JUDGE_IMAGE", "dai-judge-python:latest")
    settings = test_settings.model_copy(update={"judge_image": image, "judge_cpu_limit": 1})
    from app.services.grading_runner import build_case_stream

    stdout, stderr, returncode, _ = judge_worker._run_docker_data_runner(
        tmp_path, settings, 120, 256, host_workdir=tmp_path, image_ref=image,
        cases=build_case_stream(container_payload))
    assert returncode == 0, stderr
    graded = data_grading.grade_output(stderr, expectations, entry_function="summarize", diagnostic_text=stdout)
    assert graded["needs_review"] is False, graded["platform_errors"]
    assert graded["f_score"] == 60.0, (graded["f_score"], stdout)


# ─────────────────────────────────────────────────────────────
# A08：安全降级——manual_review 绑定不得落到旧 pytest 评分器
# ─────────────────────────────────────────────────────────────

def test_a08_manual_review_version_fails_closed(db_session_factory, redis_client, test_settings):
    submission_id, version_id, _ = _data_case(db_session_factory)
    with db_session_factory() as db:
        version = db.get(JudgeGradingVersion, version_id)
        version.execution_mode = "manual_review"
        db.commit()
    calls = _run_worker(db_session_factory, redis_client, test_settings, submission_id, [])
    assert calls == {"runner": 0, "pytest": 0}, "人工复核版本不得执行任何自动评分器"
    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        assert submission.status == "review_required"
        assert submission.score is None, "不得产生旧的不可信自动正式分"
        assert submission.result_details["diagnostic"]["code"] == "MANUAL_REVIEW_REQUIRED"
        assert submission.result_details["grading_version_id"] == version_id
        assert db.query(CodeGrade).filter_by(submission_id=submission_id).count() == 0


def test_a08_dangling_version_binding_fails_closed(db_session_factory, redis_client, test_settings):
    """绑定指向不存在的版本：不能静默回退到 pytest。"""
    submission_id, _, _ = _data_case(db_session_factory)
    with db_session_factory() as db:
        db.get(Submission, submission_id).grading_version_id = 999999
        db.commit()
    calls = _run_worker(db_session_factory, redis_client, test_settings, submission_id, [])
    assert calls == {"runner": 0, "pytest": 0}
    with db_session_factory() as db:
        assert db.get(Submission, submission_id).status == "review_required"


def test_a08_exam_manual_review_fails_closed(db_session_factory, redis_client, test_settings):
    answer_id, version_id, _, _ = _exam_data_case(db_session_factory)
    with db_session_factory() as db:
        db.get(JudgeGradingVersion, version_id).execution_mode = "manual_review"
        db.commit()
    calls = _run_exam_worker(db_session_factory, redis_client, test_settings, answer_id, [])
    assert calls["runner"] == 0 and calls["pytest"] == 0
    with db_session_factory() as db:
        answer = db.get(ExamAnswer, answer_id)
        assert answer.grading_status == "system_error" and answer.score is None
        assert answer.result_details["diagnostic"]["code"] == "MANUAL_REVIEW_REQUIRED"


def test_a08_no_version_system_fails_closed_to_manual_review(
        db_session_factory, redis_client, test_settings):
    """无版本历史题正式重评不得再使用 pytest 生成新分数。"""
    from tests.automated.conftest import (
        grant_teaching_fixture, make_judge_question, make_student, make_teacher,
    )
    from app.models import Assignment, Course

    make_teacher(db_session_factory, username="teacher")
    student = make_student(db_session_factory, username="student")
    question_id = make_judge_question(db_session_factory, grading_mode="legacy")
    with db_session_factory() as db:
        question = db.get(JudgeQuestion, question_id)
        assignment = db.get(Assignment, question.assignment_id)
        course = db.get(Course, assignment.course_id)
        grant_teaching_fixture(db, course, [student.id])
        db.commit()
        submission = Submission(question_id=question_id, student_id=student.id,
                                code="def solve():\n    return 1\n", status="queued",
                                grading_status="pending", attempt_count=0)
        db.add(submission)
        db.commit()
        submission_id = submission.id
    calls = _run_worker(db_session_factory, redis_client, test_settings, submission_id, [])
    assert calls["pytest"] == 0 and calls["runner"] == 0
    with db_session_factory() as db:
        submission = db.get(Submission, submission_id)
        assert submission.grading_status == "completed"
        assert submission.status == "review_required" and submission.score is None
        assert submission.result_details["manual_review"]["required"] is True


# ─────────────────────────────────────────────────────────────
# A12：交互输入、组间隔离、结果通道与有界输出
# ─────────────────────────────────────────────────────────────

def test_a12_duplicate_student_result_frame_fails_closed():
    """学生往 stderr 写当前用例的伪造帧会造成重复帧，必须失败关闭。"""
    _, expectations = data_grading.plan_cases(_FakeVersion(_version_payload()))
    protocol = "\n".join([_frame("F1-1", 2), _frame("F1-1", 2),
                          _frame("F1-2", 4), _frame("R1-1", None)])
    graded = data_grading.grade_output(protocol, expectations)
    assert graded["needs_review"] is True
    assert any("重复" in item for item in graded["platform_errors"])
    assert graded["f_score"] is None


def test_a12_protocol_rejects_untrusted_score_fields():
    _, expectations = data_grading.plan_cases(_FakeVersion(_version_payload()))
    untrusted = RESULT_PREFIX + json.dumps({"id": "F1-1", "status": "ok",
                                            "value": encode_value(2),
                                            "score": 100, "passed": True})
    graded = data_grading.grade_output(untrusted, expectations)
    assert graded["needs_review"] is True
    assert any("非结果字段" in item for item in graded["platform_errors"])
    assert graded["f_score"] is None


def test_a12_student_stdout_cannot_inject_frames():
    """协议帧只认 stderr 通道：stdout 里的同名内容完全被忽略。"""
    _, expectations = data_grading.plan_cases(_FakeVersion(_version_payload()))
    fake_stdout = "\n".join([_frame("F1-1", 2), _frame("F1-2", 4), _frame("R1-1", None)])
    graded = data_grading.grade_output("", expectations,
                                       diagnostic_text=fake_stdout)
    assert graded["needs_review"] is True and graded["f_score"] is None


def _settings_stub():
    from app.config import get_settings
    return get_settings().model_copy(update={"judge_image": "x", "judge_cpu_limit": 1})


def test_a12_oversized_frame_is_platform_error(monkeypatch, tmp_path):
    """超长用例帧不写入容器，直接平台错误（有界终止）。"""
    from app.worker import judge_worker
    from app.services.grading_runner import CASE_FRAME_MAX_BYTES

    huge = {"id": "F1-1", "input": {"t": "str", "v": "x" * (CASE_FRAME_MAX_BYTES + 10)}}
    calls = {"stdin_writes": 0}

    class _FakeStdin:
        def write(self, data):
            calls["stdin_writes"] += 1

        def flush(self):
            pass

        def close(self):
            pass

    class _FakeProcess:
        stdin = _FakeStdin()
        stdout = None
        stderr = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 125

    monkeypatch.setattr(judge_worker.subprocess, "Popen",
                        lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(judge_worker.subprocess, "run", lambda *a, **k: None)
    stdout, stderr, returncode, _ = judge_worker._run_docker_data_runner(
        tmp_path, _settings_stub(), 5, 128, cases=[huge])
    assert calls["stdin_writes"] == 0, "超长帧绝不能进入容器"
    assert returncode == 125


def _payload_with(cases, *, f_score=60.0):
    return {"schema_version": 1, "entry_function": "solve", "execution_model": "data_v1",
            "groups": [{"id": "F1", "name": "功能", "dimension": "F", "max_score": f_score,
                        "cases": cases}]}


def _run_real(work, payload, student_code, image):
    from app.worker import judge_worker
    from app.services.grading_runner import build_case_stream

    version = _FakeVersion(payload)
    container_payload, expectations = data_grading.plan_cases(version)
    data_grading.write_runner_files(work, entry_function="solve",
                                    container_payload=container_payload,
                                    student_code=student_code)
    settings = _settings_stub().model_copy(update={"judge_image": image})
    stdout, stderr, returncode, _ = judge_worker._run_docker_data_runner(
        work, settings, 60, 256, host_workdir=work, image_ref=image,
        cases=build_case_stream(container_payload))
    graded = data_grading.grade_output(stderr, expectations, entry_function="solve", diagnostic_text=stdout)
    return stdout, stderr, returncode, graded


@pytest.mark.skipif(__import__("os").environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
                    reason="需要真实 Docker；设置 DAI_RUN_DOCKER_SMOKE=1 启用")
def test_a12_real_container_later_inputs_invisible(tmp_path):
    """学生代码只能读到当前容器 manifest 中的一个用例，不能读后续输入。"""
    import os

    image = os.environ.get("DAI_JUDGE_IMAGE", "dai-judge-python:latest")
    payload = _payload_with([
        {"id": "F1-1", "input": encode_value((1,)), "expected": encode_value(1),
         "compare": {"kind": "literal"}},
        {"id": "F1-2", "input": encode_value((2,)), "expected": encode_value(1),
         "compare": {"kind": "literal"}},
    ])
    student = (
        "import json\n"
        "def solve(value):\n"
        "    with open('dai_cases.json', encoding='utf-8') as source:\n"
        "        cases = json.load(source)['cases']\n"
        "    return int(len(cases) == 1 and cases[0]['id'] in ('F1-1', 'F1-2'))\n"
    )
    _, _, returncode, graded = _run_real(tmp_path, payload, student, image)
    assert returncode == 0
    assert graded["needs_review"] is False, graded["platform_errors"]
    assert graded["f_score"] == 60.0, "学生代码执行期间不得看到后续输入"


@pytest.mark.skipif(__import__("os").environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
                    reason="需要真实 Docker；设置 DAI_RUN_DOCKER_SMOKE=1 启用")
def test_a12_real_container_case_isolation(tmp_path):
    """每个用例以全新容器运行；共享模块状态不会跨用例保留。"""
    import os

    image = os.environ.get("DAI_JUDGE_IMAGE", "dai-judge-python:latest")
    payload = _payload_with([
        {"id": "F1-1", "input": encode_value((1,)), "expected": encode_value(1),
         "compare": {"kind": "literal"}},
        {"id": "F1-2", "input": encode_value((2,)), "expected": encode_value(1),
         "compare": {"kind": "literal"}},
    ])
    student = (
        "calls = 0\n"
        "def solve(values):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    return calls\n"
    )
    _, _, returncode, graded = _run_real(tmp_path, payload, student, image)
    assert returncode == 0
    assert graded["needs_review"] is False, graded["platform_errors"]
    assert graded["f_score"] == 60.0, "每个用例都应以初始模块状态开始"


@pytest.mark.skipif(__import__("os").environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
                    reason="需要真实 Docker；设置 DAI_RUN_DOCKER_SMOKE=1 启用")
def test_a12_real_container_result_prefix_cannot_feed_later_case(tmp_path):
    """学生伪造 DAI_RESULT 前缀不能推进输入；重复 ID 让本次评分失败关闭。"""
    import os

    image = os.environ.get("DAI_JUDGE_IMAGE", "dai-judge-python:latest")
    payload = _payload_with([
        {"id": "F1-1", "input": encode_value((1,)), "expected": encode_value(1),
         "compare": {"kind": "literal"}},
        {"id": "F1-2", "input": encode_value((2,)), "expected": encode_value(2),
         "compare": {"kind": "literal"}},
    ])
    student = (
        "import json, sys\n"
        "def solve(value):\n"
        "    fake = {'id':'F1-1','status':'ok','value':{'t':'int','v':1}}\n"
        "    sys.stderr.write('DAI_RESULT=' + json.dumps(fake) + '\\n')\n"
        "    return value\n"
    )
    _, stderr, returncode, graded = _run_real(tmp_path, payload, student, image)
    assert returncode == 0, stderr
    assert graded["needs_review"] is True
    assert any("重复" in item for item in graded["platform_errors"])


@pytest.mark.skipif(__import__("os").environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
                    reason="需要真实 Docker；设置 DAI_RUN_DOCKER_SMOKE=1 启用")
def test_a12_real_container_output_flood_is_bounded(tmp_path):
    """持续输出洪泛：主机在字节上限处终止容器，按平台错误处理，绝不计 0 分。"""
    import os

    image = os.environ.get("DAI_JUDGE_IMAGE", "dai-judge-python:latest")
    payload = _payload_with([
        {"id": "F1-1", "input": encode_value((1,)), "expected": encode_value(1),
         "compare": {"kind": "literal"}},
    ])
    student = (
        "def solve(values):\n"
        "    for _ in range(100000):\n"
        "        print('x' * 200)\n"
        "    return 1\n"
    )
    _, _, returncode, graded = _run_real(tmp_path, payload, student, image)
    assert returncode == 125, f"输出超限必须终止容器，实际 rc={returncode}"
    assert graded["needs_review"] is True
    assert graded["f_score"] is None
