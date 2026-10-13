"""评分版本服务（批次三 3B）：创建草稿、确认、启用、退役与绑定。

不可变性契约：

* ``draft`` 可编辑（编辑即另存新版本）；
* ``confirmed`` 之后 payload/规则/摘要不可修改——再次修改必须创建新版本；
* ``active`` 只影响**新**绑定；``retired`` 只禁止新绑定，历史绑定继续可读；
* 每个题目最多一个 ``active``：启用新版本时自动退役旧版本。

绑定契约：

* 作业提交：创建时绑定题目当前 ``active`` 版本；没有则留空（后续评分转人工，
  绝不静默绑定"最新配置"）；
* 考试：``start_exam`` 时把逐代码题版本冻结进 ``ExamSubmission.grading_version_map``，
  创建/补建答案只从该映射复制，考试中途改题目配置不影响已开始的考试。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    ExamAnswer,
    ExamQuestion,
    ExamSubmission,
    JudgeGradingVersion,
    JudgeQuestion,
    QuestionRubric,
    Submission,
)
from app.services.grading_compare import DEFAULT_ABS_TOL, DEFAULT_REL_TOL

EXECUTION_MODES = ("data_v1", "manual_review")
GRADING_MODES = ("legacy", "shadow", "active")
SOURCE_KINDS = ("manual", "converted_from_pytest", "ai_draft")
PAYLOAD_SCHEMA_VERSION = 2
MAX_CASES_PER_VERSION = 256
MAX_VERSION_JSON_BYTES = 1024 * 1024

# F/R 权重（功能 60 / 边界 10）：数据用例的分值上限
MAX_FUNCTIONAL_SCORE = 60.0
MAX_ROBUSTNESS_SCORE = 10.0
# 题型上限（AI 算法 20 + 代码质量 10 由 AI 侧给出）
MAX_AI_ALGORITHM_SCORE = 20.0
MAX_AI_QUALITY_SCORE = 10.0

DEFAULT_COMPARE_RULE = {"kind": "literal"}
DEFAULT_FLOAT_RULE = {"kind": "float", "abs_tol": DEFAULT_ABS_TOL, "rel_tol": DEFAULT_REL_TOL}


class GradingVersionError(ValueError):
    """评分版本不合法（配置问题）——调用方应转 4xx，不产生自动成绩。"""


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_source_digest(payload: dict, entry_function: str) -> str:
    """来源摘要：只覆盖用例数据与入口函数（不含教师备注/上下文）。"""
    material = _canonical({"payload": payload, "entry_function": entry_function})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def compute_spec_digest(payload: dict, entry_function: str, compare_rules: dict,
                        context_snapshot: dict, rubric_id: int | None,
                        score_cap_rules: list | None = None) -> str:
    """规范摘要：覆盖一切会影响评分结果的内容（确认时重算并核对）。"""
    material = _canonical({
        "payload": payload,
        "entry_function": entry_function,
        "compare_rules": compare_rules,
        "context_snapshot": context_snapshot,
        "rubric_id": rubric_id,
        "score_cap_rules": list(score_cap_rules or []),
    })
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def validate_payload(payload: dict) -> dict:
    """校验数据用例结构并返回归一化结果（F/R 权重与用例格式）。"""
    if not isinstance(payload, dict):
        raise GradingVersionError("payload 必须是对象")
    groups = payload.get("groups", [])
    binary_cases = payload.get("binary_cases", [])
    if not isinstance(groups, list) or not isinstance(binary_cases, list):
        raise GradingVersionError("groups 和 binary_cases 必须是数组")
    if not groups and not binary_cases:
        raise GradingVersionError("payload 必须包含 groups 或 binary_cases")
    total_f = total_r = 0.0
    seen_ids: set[str] = set()
    seen_case_ids: set[str] = set()
    normalized_groups = []

    def validate_case_value(case_id: str, encoded_input, encoded_expected) -> None:
        from app.services.grading_values import ValueEncodingError, decode_value

        for label, value in (("input", encoded_input), ("expected", encoded_expected)):
            if len(_canonical(value).encode("utf-8")) > 64 * 1024:
                raise GradingVersionError(f"case {case_id} 的 {label} 超过 64 KiB")
            try:
                decode_value(value)
            except (ValueEncodingError, TypeError, ValueError) as exc:
                raise GradingVersionError(f"case {case_id} 的 {label} 协议非法: {exc}") from exc

    for group in groups:
        if not isinstance(group, dict):
            raise GradingVersionError("group 必须是对象")
        group_id = group.get("id")
        if not isinstance(group_id, str) or not group_id:
            raise GradingVersionError("group 缺少 id")
        if group_id in seen_ids:
            raise GradingVersionError(f"group id 重复: {group_id}")
        seen_ids.add(group_id)
        dimension = group.get("dimension")
        if dimension not in ("F", "R"):
            raise GradingVersionError(f"group {group_id} 的 dimension 必须是 F 或 R")
        max_score = group.get("max_score")
        if not isinstance(max_score, (int, float)) or isinstance(max_score, bool) or max_score <= 0:
            raise GradingVersionError(f"group {group_id} 的 max_score 必须是正数")
        cases = group.get("cases")
        if not isinstance(cases, list) or not cases:
            raise GradingVersionError(f"group {group_id} 必须包含非空 cases")
        for case in cases:
            if not isinstance(case, dict):
                raise GradingVersionError(f"group {group_id} 的 case 必须是对象")
            case_id = case.get("id")
            if not isinstance(case_id, str) or not case_id:
                raise GradingVersionError(f"group {group_id} 的 case 缺少 id")
            if case_id in seen_case_ids:
                raise GradingVersionError(f"case id 重复: {case_id}")
            seen_case_ids.add(case_id)
            if not isinstance(case.get("input"), dict):
                raise GradingVersionError(f"case {case_id} 缺少协议化的 input")
            if not isinstance(case.get("expected"), dict):
                raise GradingVersionError(f"case {case_id} 缺少协议化的 expected")
            validate_case_value(case_id, case["input"], case["expected"])
            rule = case.get("compare") or DEFAULT_COMPARE_RULE
            if not isinstance(rule, dict) or rule.get("kind") not in (
                    "literal", "identity", "float", "array", "exception"):
                raise GradingVersionError(f"case {case_id} 的比较规则非法")
        normalized_cases = [{**case, "compare": case.get("compare") or DEFAULT_COMPARE_RULE}
                            for case in cases]
        normalized_groups.append({
            "id": group_id,
            "name": str(group.get("name") or group_id),
            "dimension": dimension,
            "max_score": float(max_score),
            "cases": normalized_cases,
        })
        if dimension == "F":
            total_f += float(max_score)
        else:
            total_r += float(max_score)
    if total_f > MAX_FUNCTIONAL_SCORE + 1e-9:
        raise GradingVersionError(f"F 组满分合计 {total_f} 超过 {MAX_FUNCTIONAL_SCORE}")
    if total_r > MAX_ROBUSTNESS_SCORE + 1e-9:
        raise GradingVersionError(f"R 组满分合计 {total_r} 超过 {MAX_ROBUSTNESS_SCORE}")
    normalized_binary = []
    for case in binary_cases:
        if not isinstance(case, dict):
            raise GradingVersionError("binary case 必须是对象")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise GradingVersionError("binary case 缺少 id")
        if case_id in seen_case_ids:
            raise GradingVersionError(f"case id 重复: {case_id}")
        seen_case_ids.add(case_id)
        if not isinstance(case.get("input"), dict) or not isinstance(case.get("expected"), dict):
            raise GradingVersionError(f"binary case {case_id} 缺少 input 或 expected")
        validate_case_value(case_id, case["input"], case["expected"])
        rule = case.get("compare") or DEFAULT_COMPARE_RULE
        if not isinstance(rule, dict) or rule.get("kind") not in (
                "literal", "identity", "float", "array", "exception"):
            raise GradingVersionError(f"binary case {case_id} 的比较规则非法")
        normalized_binary.append({**case, "compare": rule})
    if len(seen_case_ids) > MAX_CASES_PER_VERSION:
        raise GradingVersionError(f"评分版本用例总数超过 {MAX_CASES_PER_VERSION}")
    return {
        "schema_version": int(payload.get("schema_version") or PAYLOAD_SCHEMA_VERSION),
        "entry_function": payload.get("entry_function"),
        "execution_model": payload.get("execution_model") or "data_v1",
        "binary_cases": normalized_binary,
        "groups": normalized_groups,
        "functional_total": total_f,
        "robustness_total": total_r,
    }


def _next_version(db: Session, *, judge_question_id: int | None,
                  exam_question_id: int | None,
                  practice_problem_id: int | None = None) -> int:
    query = select(JudgeGradingVersion)
    if practice_problem_id is not None:
        query = query.where(JudgeGradingVersion.practice_problem_id == practice_problem_id)
    elif judge_question_id is not None:
        query = query.where(JudgeGradingVersion.judge_question_id == judge_question_id)
    else:
        query = query.where(JudgeGradingVersion.exam_question_id == exam_question_id)
    latest = db.scalars(query.order_by(JudgeGradingVersion.version.desc()).limit(1)).first()
    return (latest.version + 1) if latest else 1


QUESTION_KINDS = ("assignment", "exam", "practice_problem")


def question_model_for(question_kind: str):
    """题目模型：练习目标是平台级题库（批次 5），与作业题/考试题三选一。"""
    from app.models import ExamQuestion, JudgeQuestion
    from app.models.practice import PracticeProblem

    if question_kind == "assignment":
        return JudgeQuestion
    if question_kind == "exam":
        return ExamQuestion
    if question_kind == "practice_problem":
        return PracticeProblem
    raise GradingVersionError(f"question_kind 必须是 {QUESTION_KINDS}")


def practice_version_mode(problem) -> str:
    """练习题目口径（deterministic|ai）→ 评分版本口径（legacy|active）。"""
    from app.services.practice_service import version_mode_for

    return version_mode_for(getattr(problem, "grading_mode", "deterministic")
                            or "deterministic")


def create_draft(db: Session, *, question_kind: str, question_id: int, payload: dict,
                 entry_function: str, compare_rules: dict | None = None,
                 score_cap_rules: list | None = None, context_snapshot: dict | None = None,
                 rubric_id: int | None = None, source_kind: str = "manual",
                 execution_mode: str = "data_v1", grading_mode_snapshot: str | None = None,
                 actor_id: int | None = None, commit: bool = False) -> JudgeGradingVersion:
    """创建草稿版本（不激活、不绑定任何提交）。

    ``commit`` 默认 False（与历史行为一致：只 flush，由调用方决定事务边界）；
    「保存并启用」传 ``commit=False`` 与其余步骤共用同一事务。
    """
    if question_kind not in QUESTION_KINDS:
        raise GradingVersionError(f"question_kind 必须是 {QUESTION_KINDS}")
    if execution_mode not in EXECUTION_MODES:
        raise GradingVersionError(f"execution_mode 必须是 {EXECUTION_MODES}")
    if source_kind not in SOURCE_KINDS:
        raise GradingVersionError(f"source_kind 必须是 {SOURCE_KINDS}")

    normalized = validate_payload(payload)
    if not isinstance(entry_function, str) or not entry_function.isidentifier():
        raise GradingVersionError("entry_function 必须是合法标识符")

    question = db.get(question_model_for(question_kind), question_id)
    if question is None:
        raise GradingVersionError("题目不存在")
    if rubric_id is not None and db.get(QuestionRubric, rubric_id) is None:
        # 先校验存在性：否则外键错误会变成 500，而不是可解释的配置错误
        raise GradingVersionError("指定的 Rubric 不存在")

    if grading_mode_snapshot is not None:
        mode_snapshot = grading_mode_snapshot
    elif question_kind == "practice_problem":
        # 练习题目口径是 deterministic|ai，版本口径是 legacy|active
        mode_snapshot = practice_version_mode(question)
    else:
        mode_snapshot = getattr(question, "grading_mode", "legacy") or "legacy"
    if mode_snapshot not in GRADING_MODES:
        raise GradingVersionError(f"grading_mode_snapshot 必须是 {GRADING_MODES}")

    # A newly created version is an explicit migration candidate. Persist v2 semantics
    # on the new row; already confirmed v1 rows are never rewritten or reinterpreted.
    normalized["schema_version"] = PAYLOAD_SCHEMA_VERSION
    normalized["execution_model"] = "isolated_case_v2"
    if mode_snapshot == "legacy" and not normalized["binary_cases"] and normalized["groups"]:
        # A manually authored data-case draft under legacy keeps its trusted cases but
        # uses the documented binary pass rule; F/R group weights are not adopted.
        normalized["binary_cases"] = [case for group in normalized["groups"]
                                      for case in group["cases"]]
        normalized["groups"] = []
        normalized["functional_total"] = 0.0
        normalized["robustness_total"] = 0.0
    if mode_snapshot == "legacy" and not normalized["binary_cases"]:
        raise GradingVersionError("legacy 模式必须包含可信 binary_cases")
    if mode_snapshot == "shadow" and not normalized["binary_cases"]:
        raise GradingVersionError("shadow 模式必须包含可信 binary_cases")
    serialized = _canonical(normalized).encode("utf-8")
    if len(serialized) > MAX_VERSION_JSON_BYTES:
        raise GradingVersionError(f"评分规范超过 {MAX_VERSION_JSON_BYTES} 字节")

    rules = dict(compare_rules or {})
    rules.setdefault("default", DEFAULT_COMPARE_RULE)
    rules.setdefault("float", DEFAULT_FLOAT_RULE)
    context = dict(context_snapshot or {})
    context.setdefault("prompt", getattr(question, "title", None) or getattr(question, "prompt", ""))
    context.setdefault("title", getattr(question, "title", None) or getattr(question, "prompt", ""))
    context.setdefault("description", getattr(question, "description", None)
                       or getattr(question, "prompt", "")
                       or getattr(question, "title", ""))
    context.setdefault("function_name", entry_function)
    context.setdefault("teacher_constraints", dict(getattr(question, "teacher_constraints", {}) or {}))
    context["grading_mode"] = mode_snapshot
    context["execution_mode"] = execution_mode
    context.setdefault("isolated_case_v2_confirmed", False)
    if rubric_id is not None:
        rubric = db.get(QuestionRubric, rubric_id)
        if rubric is not None:
            context.setdefault("rubric_snapshot", dict(rubric.rubric_json or {}))
            context.setdefault("rubric_version", rubric.version)
    cap_rules = list(score_cap_rules or [])
    frozen_spec = {
        "payload": normalized, "entry_function": entry_function,
        "compare_rules": rules, "context_snapshot": context,
        "rubric_id": rubric_id, "score_cap_rules": cap_rules,
    }
    if len(_canonical(frozen_spec).encode("utf-8")) > MAX_VERSION_JSON_BYTES:
        raise GradingVersionError(f"完整评分规范超过 {MAX_VERSION_JSON_BYTES} 字节")

    version = JudgeGradingVersion(
        judge_question_id=question_id if question_kind == "assignment" else None,
        exam_question_id=question_id if question_kind == "exam" else None,
        practice_problem_id=question_id if question_kind == "practice_problem" else None,
        version=_next_version(
            db,
            judge_question_id=question_id if question_kind == "assignment" else None,
            exam_question_id=question_id if question_kind == "exam" else None,
            practice_problem_id=question_id if question_kind == "practice_problem" else None,
        ),
        status="draft",
        execution_mode=execution_mode,
        grading_mode_snapshot=mode_snapshot,
        entry_function=entry_function,
        payload=normalized,
        compare_rules=rules,
        score_cap_rules=cap_rules,
        context_snapshot=context,
        rubric_id=rubric_id,
        source_kind=source_kind,
        source_digest=compute_source_digest(normalized, entry_function),
        spec_digest=compute_spec_digest(normalized, entry_function, rules, context, rubric_id,
                                        cap_rules),
        created_by_id=actor_id,
    )
    db.add(version)
    if commit:
        db.commit()
    else:
        db.flush()
    return version


def confirm_version(db: Session, version: JudgeGradingVersion, *, actor_id: int | None = None,
                    expected_spec_digest: str | None = None,
                    confirm_isolated_case_v2: bool = False,
                    commit: bool = False) -> JudgeGradingVersion:
    """确认草稿：payload 冻结；摘要变化（被改动）时拒绝确认。

    ``commit`` 默认 False（与历史行为一致：只 flush）。
    """
    if version.status != "draft":
        raise GradingVersionError("只有 draft 版本可以确认")
    current = compute_spec_digest(version.payload, version.entry_function, version.compare_rules,
                                 version.context_snapshot, version.rubric_id,
                                 version.score_cap_rules)
    if current != version.spec_digest:
        raise GradingVersionError("版本内容与摘要不一致（草稿被修改），请重新保存")
    if expected_spec_digest and expected_spec_digest != current:
        raise GradingVersionError("候选版本已变化，请重新读取后再确认")
    if (version.payload or {}).get("execution_model") != "isolated_case_v2" \
            or (version.payload or {}).get("schema_version") != 2:
        raise GradingVersionError("旧执行规范不能重新解释，请预检并创建新的逐例隔离版本")
    context = dict(version.context_snapshot or {})
    if not context.get("isolated_case_v2_confirmed"):
        if not confirm_isolated_case_v2:
            raise GradingVersionError(
                "确认前必须明确接受逐例独立执行；依赖跨用例共享状态的旧测试需人工处理"
            )
        context["isolated_case_v2_confirmed"] = True
        version.context_snapshot = context
        version.spec_digest = compute_spec_digest(
            version.payload, version.entry_function, version.compare_rules,
            context, version.rubric_id, version.score_cap_rules,
        )
    version.status = "confirmed"
    version.confirmed_by_id = actor_id
    version.confirmed_at = datetime.now(timezone.utc)
    if commit:
        db.commit()
    else:
        db.flush()
    return version


def activate_version(db: Session, version: JudgeGradingVersion, *,
                     actor_id: int | None = None,
                     commit: bool = False) -> JudgeGradingVersion:
    """启用版本：同题目的旧 active 自动退役（只禁止新绑定，不影响历史绑定）。

    ``commit`` 默认 False（与历史行为一致：只 flush）。
    """
    if version.status not in ("confirmed", "retired"):
        raise GradingVersionError("只有 confirmed 版本可以启用")
    _validate_activation_strategy(db, version)
    query = select(JudgeGradingVersion).where(JudgeGradingVersion.status == "active")
    if version.practice_problem_id is not None:
        # 练习题两种口径（普通判题/AI 综合）各自保留一个 active 版本：
        # 全局开关关掉 AI 后新提交仍要能绑定普通判题版本，历史成绩也按口径分榜。
        query = query.where(
            JudgeGradingVersion.practice_problem_id == version.practice_problem_id,
            JudgeGradingVersion.grading_mode_snapshot == version.grading_mode_snapshot,
        )
    elif version.judge_question_id is not None:
        query = query.where(JudgeGradingVersion.judge_question_id == version.judge_question_id)
    else:
        query = query.where(JudgeGradingVersion.exam_question_id == version.exam_question_id)
    for previous in db.scalars(query).all():
        if previous.id != version.id:
            previous.status = "retired"
    version.status = "active"
    version.activated_at = datetime.now(timezone.utc)
    if version.confirmed_at is None:
        version.confirmed_by_id = actor_id
        version.confirmed_at = datetime.now(timezone.utc)
    if commit:
        db.commit()
    else:
        db.flush()
    return version


def retire_version(db: Session, version: JudgeGradingVersion) -> JudgeGradingVersion:
    if version.status != "active":
        raise GradingVersionError("只有 active 版本可以退役")
    version.status = "retired"
    db.flush()
    return version


def _validate_activation_strategy(db: Session, version: JudgeGradingVersion) -> None:
    payload = version.payload or {}
    if payload.get("schema_version") != 2 or payload.get("execution_model") != "isolated_case_v2":
        raise GradingVersionError("旧执行规范不能新启用；请迁移为逐例隔离版本")
    context = version.context_snapshot or {}
    if context.get("isolated_case_v2_confirmed") is not True:
        raise GradingVersionError("教师尚未确认逐例独立执行规则")
    groups = payload.get("groups") or []
    binary_cases = payload.get("binary_cases") or []
    if version.grading_mode_snapshot == "legacy":
        if not binary_cases:
            raise GradingVersionError("legacy 模式必须包含可信 binary_cases")
        return
    f_total = sum(float(group.get("max_score", 0)) for group in groups
                  if group.get("dimension") == "F")
    r_total = sum(float(group.get("max_score", 0)) for group in groups
                  if group.get("dimension") == "R")
    if abs(f_total - MAX_FUNCTIONAL_SCORE) > 1e-6 \
            or abs(r_total - MAX_ROBUSTNESS_SCORE) > 1e-6:
        raise GradingVersionError(
            f"{version.grading_mode_snapshot} 模式要求 F=60、R=10，当前 F={f_total}、R={r_total}"
        )
    if version.grading_mode_snapshot == "shadow" and not binary_cases:
        raise GradingVersionError("shadow 模式必须包含可信 binary_cases")
    if not rubric_binding_ok(db, version):
        raise GradingVersionError("AI 评分模式必须绑定已锁定且已冻结的 Rubric")


def active_version_for(db: Session, *, question_kind: str,
                       question_id: int) -> JudgeGradingVersion | None:
    query = select(JudgeGradingVersion).where(JudgeGradingVersion.status == "active")
    if question_kind == "assignment":
        query = query.where(JudgeGradingVersion.judge_question_id == question_id)
    elif question_kind == "practice_problem":
        query = query.where(JudgeGradingVersion.practice_problem_id == question_id)
    else:
        query = query.where(JudgeGradingVersion.exam_question_id == question_id)
    return db.scalars(query.order_by(JudgeGradingVersion.version.desc()).limit(1)).first()


def bind_submission_version(db: Session, submission: Submission) -> int | None:
    """提交创建时绑定题目当前 active 版本；没有则留空（后续转人工，不猜最新配置）。"""
    question = db.get(JudgeQuestion, submission.question_id)
    if question is None:
        submission.grading_version_id = None
        return None
    active = active_version_for(db, question_kind="assignment", question_id=question.id)
    submission.grading_version_id = active.id if active else None
    return submission.grading_version_id


def freeze_exam_versions(db: Session, submission: ExamSubmission) -> dict:
    """考试开始时冻结逐代码题的版本映射与分值。

    每道题都记录 ``{"version_id": int|None, "points": float}``：代码题版本与全部题目
    分值都在**开始考试时**冻结，教师之后修改实时题目不会影响本场考试。
    """
    questions = db.scalars(
        select(ExamQuestion).where(ExamQuestion.exam_id == submission.exam_id)
    ).all()
    mapping: dict[str, dict] = {}
    for question in questions:
        active = (active_version_for(db, question_kind="exam", question_id=question.id)
                  if question.question_type == "code" else None)
        mapping[str(question.id)] = {
            "version_id": active.id if active else None,
            "points": float(question.points or 0),
        }
    submission.grading_version_map = mapping
    db.flush()
    return mapping


def frozen_points_for_question(submission: ExamSubmission, question_id: int) -> float | None:
    """从冻结映射读取该题在开始考试时的分值（兼容裸 int 旧数据）。"""
    entry = (submission.grading_version_map or {}).get(str(question_id))
    if isinstance(entry, dict):
        points = entry.get("points")
        return float(points) if points is not None else None
    return None


def version_id_from_map_entry(entry) -> int | None:
    """冻结映射条目 → 版本 ID（兼容旧数据裸 int）。"""
    if isinstance(entry, dict):
        return entry.get("version_id")
    return entry


def version_for_answer(db: Session, answer: ExamAnswer) -> JudgeGradingVersion | None:
    """答案的版本只从父级冻结映射解析；映射缺失/为空一律返回 None（转人工）。"""
    if answer.grading_version_id is None:
        return None
    return db.get(JudgeGradingVersion, answer.grading_version_id)


def apply_frozen_version(db: Session, answer: ExamAnswer,
                         submission: ExamSubmission) -> int | None:
    """创建/补建答案时从父级映射复制版本（禁止读取题目最新配置）。"""
    mapping = submission.grading_version_map or {}
    version_id = version_id_from_map_entry(mapping.get(str(answer.question_id)))
    answer.grading_version_id = version_id
    db.flush()
    return version_id


def bind_exam_answer_version(db: Session, answer: ExamAnswer) -> int | None:
    """答案入库时的绑定入口：父级已有冻结映射则复制；否则保持空并转人工。"""
    submission = db.get(ExamSubmission, answer.submission_id)
    if submission is None:
        answer.grading_version_id = None
        return None
    if submission.grading_version_map is None:
        # 旧会话没有基线：不静默绑定最新版本，交由人工/显式预检处理
        answer.grading_version_id = None
        return None
    return apply_frozen_version(db, answer, submission)


def set_question_current_version(db: Session, *, question_kind: str, question_id: int,
                                 version_id: int | None) -> None:
    """更新题目的 current_grading_version_id（启用/退役时保持一致）。"""
    model = question_model_for(question_kind)
    db.execute(update(model).where(model.id == question_id).values(
        current_grading_version_id=version_id))
    db.flush()


def rubric_binding_ok(db: Session, version: JudgeGradingVersion) -> bool:
    """需要 AI 的模式必须绑定仍存在的 Rubric；否则版本不可启用。"""
    if version.grading_mode_snapshot == "legacy":
        return True
    if version.rubric_id is None:
        return False
    rubric = db.get(QuestionRubric, version.rubric_id)
    context = version.context_snapshot or {}
    return bool(rubric and rubric.status == "locked"
                and isinstance(context.get("rubric_snapshot"), dict)
                and context.get("rubric_version") == rubric.version)


# ─────────────────────────────────────────────────────────────
# 启用前预检（批次三 3E）：不得自行假定"没有教学影响"
# ─────────────────────────────────────────────────────────────

IN_FLIGHT_STATUSES = ("queued", "running")


def enablement_preflight(db: Session, *, question_kind: str, question_id: int) -> dict:
    """启用 data_v1 前的影响面核查。

    返回：

    * ``active_version``：当前启用的版本（若有）；
    * ``in_flight``：该题正在排队/判题中的任务数（**切换前必须等它们结束**）；
    * ``unbound_legacy``：已终态但没有版本基线的历史提交/答案数——启用后这些记录若被
      重新判题会 fail-closed 转人工，教师需要知道数量；
    * ``started_exams``（考试题）：仍在进行中的考试场次数——这些场次使用开考时冻结的
      版本，不受后续切换影响；
    * ``blocking`` / ``warnings``：给调用方直接展示的结论。
    """
    if question_kind not in ("assignment", "exam", "practice_problem"):
        raise GradingVersionError("question_kind 必须是 assignment 或 exam 或 practice_problem")
    report: dict = {"question_kind": question_kind, "question_id": question_id,
                    "in_flight": 0, "unbound_legacy": 0, "started_exams": 0,
                    "active_version": None, "blocking": [], "warnings": []}

    if question_kind == "practice_problem":
        from app.models.practice import PracticeSubmission

        in_flight = db.scalar(
            select(func.count()).select_from(PracticeSubmission).where(
                PracticeSubmission.problem_id == question_id,
                PracticeSubmission.grading_status.in_(IN_FLIGHT_STATUSES))
        ) or 0
        unbound = db.scalar(
            select(func.count()).select_from(PracticeSubmission).where(
                PracticeSubmission.problem_id == question_id,
                PracticeSubmission.grading_version_id.is_(None),
                PracticeSubmission.grading_status.notin_(IN_FLIGHT_STATUSES))
        ) or 0
    elif question_kind == "assignment":
        in_flight = db.scalar(
            select(func.count()).select_from(Submission).where(
                Submission.question_id == question_id,
                Submission.grading_status.in_(IN_FLIGHT_STATUSES))
        ) or 0
        unbound = db.scalar(
            select(func.count()).select_from(Submission).where(
                Submission.question_id == question_id,
                Submission.grading_version_id.is_(None),
                Submission.grading_status.notin_(IN_FLIGHT_STATUSES))
        ) or 0
    else:
        in_flight = db.scalar(
            select(func.count()).select_from(ExamAnswer).where(
                ExamAnswer.question_id == question_id,
                ExamAnswer.grading_status.in_(IN_FLIGHT_STATUSES))
        ) or 0
        unbound = db.scalar(
            select(func.count()).select_from(ExamAnswer).where(
                ExamAnswer.question_id == question_id,
                ExamAnswer.grading_version_id.is_(None),
                ExamAnswer.grading_status.notin_(IN_FLIGHT_STATUSES))
        ) or 0
        report["started_exams"] = db.scalar(
            select(func.count()).select_from(ExamSubmission).join(
                ExamQuestion, ExamQuestion.exam_id == ExamSubmission.exam_id).where(
                ExamQuestion.id == question_id,
                ExamSubmission.status.in_(("started", "submitted", "grading")))
        ) or 0

    active = active_version_for(db, question_kind=question_kind, question_id=question_id)
    if active is not None:
        report["active_version"] = {"id": active.id, "version": active.version,
                                    "execution_mode": active.execution_mode,
                                    "status": active.status}
    report["in_flight"] = int(in_flight)
    report["unbound_legacy"] = int(unbound)
    if in_flight:
        report["blocking"].append(
            f"有 {in_flight} 个任务正在排队或判题中，切换版本前必须先等它们结束")
    if unbound:
        report["warnings"].append(
            f"有 {unbound} 条历史记录没有评分版本基线：启用后若被重新判题将转人工复核")
    if report["started_exams"]:
        report["warnings"].append(
            f"有 {report['started_exams']} 场考试仍在进行：它们使用开考时冻结的版本，不受本次切换影响")
    if not report["blocking"]:
        report["blocking"] = []
    report["ready"] = not report["blocking"]
    return report


def assert_enablement_allowed(db: Session, *, question_kind: str, question_id: int,
                             acknowledge_in_flight: bool = False) -> dict:
    """启用前门禁：在途任务未确认时必须拒绝，避免判到一半切换协议。"""
    report = enablement_preflight(db, question_kind=question_kind, question_id=question_id)
    if report["in_flight"] and not acknowledge_in_flight:
        raise GradingVersionError(
            f"有 {report['in_flight']} 个任务正在排队或判题中，请等待结束后再启用"
            "（如确认无影响，可显式确认在途任务后继续）")
    return report


def build_data_payload_from_generated_groups(groups, *, entry_function: str,
                                             max_score: float = 60.0) -> tuple[dict, dict]:
    """逐组静态转换 AI 生成用例，保留 F/R 边界与每组权重。

    每组独立解析；任一组有未转换断言、无用例或不支持语义时整批拒绝，
    不留下部分候选。源码不执行，返回 ``(payload, 预检报告)``。
    """
    from app.services import grading_conversion

    if not groups:
        raise GradingVersionError("AI 未生成任何测试代码")
    converted_groups = []
    group_reports = []
    combined_cases = []
    combined_functions = []
    f_total = r_total = 0.0
    seen_ids = set()
    for group in groups:
        raw = group.model_dump() if hasattr(group, "model_dump") else dict(group)
        group_id = raw.get("id")
        name = raw.get("name")
        dimension = raw.get("dimension")
        weight = raw.get("max_score")
        source = raw.get("tests")
        if not isinstance(group_id, str) or not group_id or group_id in seen_ids:
            raise GradingVersionError("AI 测试组缺少唯一 ID")
        seen_ids.add(group_id)
        if dimension not in ("F", "R") or not isinstance(weight, (int, float)) \
                or isinstance(weight, bool) or weight <= 0:
            raise GradingVersionError(f"测试组 {group_id} 的维度或权重无效")
        try:
            report = grading_conversion.analyze_pytest_source(
                source, entry_function=entry_function)
        except (TypeError, ValueError) as exc:
            raise GradingVersionError(f"测试组 {group_id} 无法静态分析：{exc}") from exc
        group_report = {
            "id": group_id, "name": str(name or group_id), "dimension": dimension,
            "max_score": float(weight), "convertible_count": report["convertible_count"],
            "unsupported_count": report["unsupported_count"],
            "fully_convertible": report["fully_convertible"],
            "cases": report["cases"],
        }
        group_reports.append(group_report)
        if not report["fully_convertible"] or report["unsupported_count"]:
            reasons = "；".join(sorted({case.get("reason") or "未知原因"
                                        for case in report["cases"]
                                        if case.get("status") == "unsupported"})) or "没有可转换用例"
            raise GradingVersionError(
                f"测试组 {group_id} 未完整转换（unsupported={report['unsupported_count']}）：{reasons}"
            )
        one_group = grading_conversion.build_conversion_payload(
            report, entry_function=entry_function, group_id=group_id,
            group_name=str(name or group_id), max_score=float(weight), dimension=dimension)
        converted_groups.extend(one_group["groups"])
        combined_cases.extend({**case, "case_id": f"{group_id}:{case['case_id']}"}
                              for case in report["cases"])
        combined_functions.extend({**item, "group_id": group_id} for item in report["functions"])
        if dimension == "F":
            f_total += float(weight)
        else:
            r_total += float(weight)
    if abs(f_total - 60.0) > 1e-6 or abs(r_total - 10.0) > 1e-6:
        raise GradingVersionError(f"AI 用例分组权重必须满足 F=60、R=10，当前 F={f_total}、R={r_total}")
    report = {
        "schema_version": 2, "entry_function": entry_function,
        "cases": combined_cases, "functions": combined_functions,
        "convertible_count": sum(item["convertible_count"] for item in group_reports),
        "unsupported_count": 0, "fully_convertible": True,
        "groups": group_reports, "notes": [],
    }
    payload = {
        "schema_version": 2, "entry_function": entry_function,
        "execution_model": "isolated_case_v2", "binary_cases": [],
        "groups": converted_groups,
    }
    return payload, report
