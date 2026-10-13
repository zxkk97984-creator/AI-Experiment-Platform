"""AI 评分编排——支持 Submission 和 ExamAnswer，应用上限、防重复、折算"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (CodeGrade, ExamAnswer, ExamQuestion, ExamSubmission, JudgeQuestion,
                        QuestionRubric, Submission)
from app.models.practice import PracticeSubmission
from app.schemas.ai_grading import AIGradeResponse
from app.services.ai_client import DeepSeekClient
from app.services.ai_prompts import build_grading_messages
from app.services.ai_score_validation import detect_cross_dimension_duplicates, validate_ai_output
from app.services.grading_errors import LostClaimError
from app.services.score_merger import merge_scores
from app.services.static_analysis import analyze_python

logger = logging.getLogger("dai.ai_grader")


def frozen_grading_facts(db: Session, *, submission=None, answer=None) -> dict | None:
    """从**绑定的评分版本**读取评分事实（A09）。

    绑定版本存在时，AI 评分不得再读实时题目/context/分值上限：教师之后修改题目或启用新
    版本，旧提交的结果必须保持原基线。返回 None 表示该提交没有版本基线（历史题目），
    此时保持原有实时读取行为。
    """
    from app.models import JudgeGradingVersion

    version_id = None
    if submission is not None:
        version_id = getattr(submission, "grading_version_id", None)
    elif answer is not None:
        version_id = getattr(answer, "grading_version_id", None)
    if not version_id:
        return None
    version = db.get(JudgeGradingVersion, version_id)
    if version is None:
        return None
    context = dict(version.context_snapshot or {})
    exam_points = None
    if answer is not None:
        parent = db.get(ExamSubmission, answer.submission_id)
        mapping = (parent.grading_version_map or {}) if parent is not None else {}
        entry = mapping.get(str(answer.question_id))
        exam_points = entry.get("points") if isinstance(entry, dict) else None
    return {
        "grading_version_id": version.id,
        "prompt": context.get("prompt"),
        "title": context.get("title"),
        "description": context.get("description"),
        "function_name": context.get("function_name"),
        "teacher_constraints": context.get("teacher_constraints"),
        "grading_mode": version.grading_mode_snapshot,
        "execution_mode": version.execution_mode,
        "cap_rules": list(version.score_cap_rules or []),
        "rubric": context.get("rubric_snapshot"),
        "rubric_version": context.get("rubric_version"),
        "exam_points": exam_points,
        "frozen_at": version.activated_at.isoformat() if version.activated_at else None,
    }


def grade_code_submission(
    db: Session,
    client: DeepSeekClient,
    code_grade_id: int,
) -> CodeGrade:
    """编排单份 AI 代码评分——同时支持 Submission 和 ExamAnswer"""
    grade = db.get(CodeGrade, code_grade_id)
    if grade is None:
        raise ValueError(f"CodeGrade {code_grade_id} 不存在")

    rubric = db.get(QuestionRubric, grade.rubric_id)
    if rubric is None:
        raise ValueError(f"Rubric {grade.rubric_id} 不存在")

    # 确定目标类型
    if grade.submission_id:
        submission = db.get(Submission, grade.submission_id)
        question = submission.question if submission else None
        code = submission.code if submission else ""
        target_id = grade.submission_id
        target_type = "submission"
    elif grade.exam_answer_id:
        answer = db.get(ExamAnswer, grade.exam_answer_id)
        question = answer.question if answer else None
        code = answer.code_answer or ""
        target_id = grade.exam_answer_id
        target_type = "exam_answer"
    elif grade.practice_submission_id:
        # 自主练习（批次 5）：独立域，题目是平台级公共题库
        # （PracticeSubmission 已在模块顶部导入，此处不可再局部导入：会让它变成
        #  函数局部名，导致下面的 target_model 映射在前面的分支里 UnboundLocalError）
        from app.models.practice import PracticeProblem

        submission = db.get(PracticeSubmission, grade.practice_submission_id)
        question = (db.get(PracticeProblem, submission.problem_id)
                    if submission is not None else None)
        code = submission.code if submission is not None else ""
        target_id = grade.practice_submission_id
        target_type = "practice_submission"
    else:
        raise ValueError("CodeGrade 未关联任何提交")

    if question is None:
        raise ValueError("题目不存在")

    source = (grade.deterministic_details or {}).get("source") or {}
    source_revision = source.get("result_revision")
    target_model = {
        "submission": Submission,
        "exam_answer": ExamAnswer,
        "practice_submission": PracticeSubmission,
    }[target_type]
    revision_guard = (target_model, target_id, source_revision)
    if grade.mode == "active" and (
        not isinstance(source_revision, int) or isinstance(source_revision, bool)
        or source_revision < 0
    ):
        from app.services.ai_grading_queue import finish_ai_grade

        wrote = finish_ai_grade(db, code_grade_id, {
            "status": "review_required",
            "needs_teacher_review": True,
            "review_reason": "AI 评分缺少可验证的确定性结果修订号，请教师复核",
        }, target_revision_guard=(target_model, target_id, None))
        if not wrote:
            raise LostClaimError(f"AI 评分缺少来源修订号且失去认领: grade={code_grade_id}")
        db.expire_all()
        return grade

    # 静态分析（必需分析器：Ruff + Radon）
    static = analyze_python(code)
    tool_status = static.get("tool_status", {}) or {}
    unavailable_tools = [name for name, state in sorted(tool_status.items()) if state != "ok"]
    if unavailable_tools:
        # 平台工具故障（未安装/超时/输出非法）：**不调用外部 AI**，不生成自动正式总分，
        # 经同一认领栅栏转人工复核；已有成绩与可信测试证据保持不变，工具恢复后可显式重试。
        from app.services.ai_grading_queue import finish_ai_grade

        reason = "静态分析工具不可用（%s），未生成自动评分，请人工复核或稍后重试" % (
            "、".join(f"{name}:{tool_status.get(name)}" for name in unavailable_tools)
        )
        wrote = finish_ai_grade(db, code_grade_id, {
            "status": "review_required",
            "needs_teacher_review": True,
            "review_reason": reason,
            "static_analysis": static,
        }, target_revision_guard=revision_guard if grade.mode == "active" else None)
        if not wrote:
            raise LostClaimError(
                f"静态分析故障转人工时失去认领: grade={code_grade_id}"
            )
        db.expire_all()
        logger.warning("ai_grading_static_unavailable", extra={
            "grade_id": code_grade_id, "tools": unavailable_tools,
        })
        return grade

    # 构建评分请求
    deterministic = {
        "functional_score": grade.functional_score,
        "robustness_score": grade.robustness_score,
        "details": grade.deterministic_details,
    }

    # A09：绑定版本优先——冻结的题面不得被后续题目修改影响
    # （练习提交同样按绑定的评分版本快照评分，与作业提交走同一条冻结路径）
    frozen = frozen_grading_facts(
        db,
        submission=submission if target_type in ("submission", "practice_submission") else None,
        answer=answer if target_type == "exam_answer" else None,
    )
    if grade.mode == "active" and frozen is None:
        from app.services.ai_grading_queue import finish_ai_grade

        wrote = finish_ai_grade(db, code_grade_id, {
            "status": "review_required",
            "needs_teacher_review": True,
            "review_reason": "缺少可信评分版本基线，请教师人工复核",
        }, target_revision_guard=(target_model, target_id, None))
        if not wrote:
            raise LostClaimError(f"无评分版本基线且 AI 认领已失效: grade={code_grade_id}")
        db.expire_all()
        return grade
    if grade.mode == "active" and (
        not frozen.get("prompt") or not frozen.get("function_name")
        or not isinstance(frozen.get("teacher_constraints"), dict)
        or source.get("grading_version_id") != frozen.get("grading_version_id")
        or (target_type == "exam_answer" and frozen.get("exam_points") is None)
    ):
        from app.services.ai_grading_queue import finish_ai_grade

        wrote = finish_ai_grade(db, code_grade_id, {
            "status": "review_required",
            "needs_teacher_review": True,
            "review_reason": "评分版本缺少题面、接口、约束或考试分值快照，请教师复核",
        }, target_revision_guard=(target_model, target_id, None))
        if not wrote:
            raise LostClaimError(f"评分版本基线不完整且 AI 认领已失效: grade={code_grade_id}")
        db.expire_all()
        return grade
    live_title = question.title if hasattr(question, "title") else question.prompt
    # 冻结版本存在时以快照为准，否则读实时 rubric；两处消费共用同一份文档。
    rubric_document = (frozen.get("rubric") if frozen and frozen.get("rubric") is not None
                       else rubric.rubric_json)
    messages = build_grading_messages(
        rubric=rubric_document,
        question={
            "title": frozen["title"] if frozen else live_title,
            "description": frozen["description"] if frozen else getattr(
                question, "description", getattr(question, "prompt", None)),
            "function_name": frozen["function_name"] if frozen else getattr(
                question, "function_name", getattr(question, "prompt", None)),
            "teacher_constraints": frozen["teacher_constraints"] if frozen else {},
        },
        code=code,
        deterministic=deterministic,
        static_analysis=static,
        rubric_version=rubric.version,
    )

    # 调用 AI（TASK-028：ai_grading 预算 1500 completion tokens）
    raw_response = client.chat_json(messages, operation="ai_grading")
    raw_json_str = json.dumps(raw_response, ensure_ascii=False)

    # Pydantic 校验
    ai_result = AIGradeResponse.model_validate(raw_response)

    # 业务校验
    code_lines = list(range(1, len(code.splitlines()) + 1))
    validation_errors = validate_ai_output(
        rubric={
            "rubric_version": (frozen.get("rubric_version") if frozen else None) or rubric.version,
            "algorithm_criteria": rubric_document.get("algorithm_criteria", []),
            "quality_criteria": rubric_document.get("quality_criteria", []),
        },
        ai_result=ai_result.model_dump(),
        code_lines=code_lines,
    )

    # 防重复扣分
    a_items = [item.model_dump() for item in ai_result.algorithm.items]
    q_items = [item.model_dump() for item in ai_result.code_quality.items]
    duplicates = detect_cross_dimension_duplicates(a_items, q_items)
    if duplicates:
        for d in duplicates:
            validation_errors.append(
                f"跨维度重复扣分: A.{d['a_criterion']} 与 Q.{d['q_criterion']} reason={d['reason_code']}"
            )

    # 应用题目上限规则
    cap = None
    if grade.submission_id:
        q = submission.question if submission else None
    else:
        q = question
    caps_from_teacher = (frozen.get("cap_rules") if frozen and frozen.get("cap_rules") is not None
                         else (getattr(q, "score_cap_rules", []) if q else []))
    ai_cap_ids = set(ai_result.triggered_cap_rule_ids)
    for rule in caps_from_teacher:
        if rule.get("id") in ai_cap_ids:
            rule_cap = float(rule.get("cap", 100))
            cap = min(cap, rule_cap) if cap is not None else rule_cap

    # 后端合分
    exam_points = None
    if frozen and frozen.get("exam_points") is not None:
        # 开始考试时冻结的分值：教师之后改分值不影响已绑定答案
        exam_points = float(frozen["exam_points"])
    elif target_type == "exam_answer" and frozen is None and isinstance(question, ExamQuestion):
        exam_points = question.points

    merged = merge_scores(
        f=grade.functional_score,
        a=ai_result.algorithm.dimension_score,
        r=grade.robustness_score,
        q=ai_result.code_quality.dimension_score,
        cap=cap,
        exam_points=exam_points,
    )

    # 保存结果——受认领栅栏保护：只有仍持有本次认领的运行才能写入。
    # 全部字段连同 active 模式的正式分在同一条条件 UPDATE 内落地（原先用 ORM 赋值 +
    # db.flush()，失去认领的旧运行照样能覆盖新结果——AI 客户端最多重试 3 次 × 60–180s，
    # 远超 stale 阈值，这是真实可达的路径）。
    from app.services.ai_grading_queue import finish_ai_grade

    values: dict = {
        # A09：留痕本次评分所依据的冻结版本（无绑定则为 None，表示按实时配置评分）
        "deterministic_details": {
            **(grade.deterministic_details or {}),
            "grading_version_id": frozen["grading_version_id"] if frozen else None,
        },
        "algorithm_score": ai_result.algorithm.dimension_score,
        "quality_score": ai_result.code_quality.dimension_score,
        "raw_total": merged.raw_total,
        "score_cap": cap,
        "final_score_100": merged.final_score_100,
        "scaled_score": merged.scaled_score,
        "ai_result": ai_result.model_dump(),
        "raw_response": raw_json_str,
        "static_analysis": static,
        "needs_teacher_review": ai_result.needs_teacher_review or len(validation_errors) > 0,
    }
    needs_review = values["needs_teacher_review"]
    values["review_reason"] = (
        ("; ".join(validation_errors) if validation_errors else ai_result.review_reason)
        if needs_review else None
    )
    # 终态与评分字段一次写入：合法结果 = completed，需人工复核 = review_required。
    # Worker 不再有第二次 complete_ai_grade —— 那次调用发生在 finish_ai_grade 已经
    # 释放认领之后，必然被栅栏拒绝，使 CodeGrade 永久停在 running（考试随之无法汇总）。
    values["status"] = "review_required" if needs_review else "completed"

    # active 模式更新正式分，shadow 保持旧分；与 CodeGrade 同事务写入
    related: list[tuple] = []
    if grade.mode == "active" and not needs_review:
        if grade.submission_id:
            related.append((Submission, grade.submission_id,
                            {"score": merged.final_score_100, "status": "graded",
                             "result_revision": Submission.result_revision + 1}))
        elif grade.exam_answer_id and isinstance(question, ExamQuestion):
            from app.services.exam_service import round_score
            related.append((ExamAnswer, grade.exam_answer_id,
                            {"score": round_score(merged.scaled_score),
                             "result_revision": ExamAnswer.result_revision + 1}))
        elif grade.practice_submission_id:
            # 练习：与 CodeGrade 同一事务写入正式分；确定性分保持已落库的值不变
            related.append((PracticeSubmission, grade.practice_submission_id,
                            {"score": merged.final_score_100,
                             "grading_status": "completed",
                             "last_error": None,
                             "finished_at": datetime.now(timezone.utc),
                             "result_revision": PracticeSubmission.result_revision + 1}))

    if not finish_ai_grade(
        db, code_grade_id, values, related_updates=related,
        target_revision_guard=revision_guard if grade.mode == "active" else None,
    ):
        # 已失去认领：不得继续推进（正式分与新结果都不得落库）
        raise LostClaimError(
            f"AI 评分结果被认领栅栏拒绝，已丢弃: grade={code_grade_id}"
        )
    db.expire_all()

    logger.info(
        "ai_grade_completed",
        extra={
            "grade_id": code_grade_id,
            "mode": grade.mode,
            "raw_total": merged.raw_total,
            "final_score": merged.final_score_100,
        },
    )
    return grade
