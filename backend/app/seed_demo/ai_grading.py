# -*- coding: utf-8 -*-
"""AI 评分（评审 5）：Rubric 锁定 + CodeGrade 确定性 Fixture。

依据（以当前代码为准）：
- CodeGrade 字段语义：functional/robustness 来自判题确定性分数，algorithm/quality
  为 AI 维度，raw_total/final_score_100 经 app.services.score_merger.merge_scores
  计算（F60 + A20 + R10 + Q10）；
- ai_result 结构遵循 app.schemas.ai_grading.AIGradeResponse
  （algorithm/code_quality/student_feedback，A 维度 20 分、Q 维度 10 分）；
- static_analysis 复用 app.services.static_analysis.analyze_python；
- 所有生成结果带 seed_fixture 标记（真实 AI 需 DAI_AI_ENABLED + Key，默认关闭，
  因此 Demo 以确定性 Fixture 呈现，明确标注不冒充真实 AI 输出）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CodeGrade,
    JudgeQuestion,
    QuestionRubric,
    Submission,
    User,
)
from app.services.score_merger import merge_scores
from app.services.static_analysis import analyze_python

from .constants import ARCHETYPES, demo_archetype
from .marks import mark
from .rng import make_rng
from .timeline import DemoClock

logger = logging.getLogger("dai.seed_demo.ai_grading")

_MODEL_NAME = "demo-deterministic-fixture"


def _rubric_document(task: dict, *, is_exam: bool) -> dict:
    """生成符合 RubricDocument schema 的 rubric_json（A=20, Q1-Q4=10）。"""
    return {
        "rubric_version": 1,
        "question_type": "考试编程题" if is_exam else "课程作业编程题",
        "learning_objective": task["description"],
        "explicit_requirements": [
            f"实现 {task['function_name']} 函数并保持题目给出的函数签名",
            "通过公开样例和隐藏测试，正确处理正常输入与边界输入",
        ],
        "teacher_constraints": [
            "不得修改评测入口或绕过测试",
            "优先使用清晰、可维护且与题目环境匹配的实现",
        ],
        "accepted_strategies": [
            "允许使用等价的算法实现，只要输入输出契约和边界行为一致",
            "允许合理的辅助变量、辅助函数和标准库/题目环境白名单内的包",
        ],
        "algorithm_criteria": [
            {"id": "A1", "name": "核心功能实现", "points": 10, "description": "正常输入下得到正确结果"},
            {"id": "A2", "name": "算法思路与实现", "points": 6, "description": "实现逻辑与数据处理过程合理"},
            {"id": "A3", "name": "边界处理与复杂度", "points": 4, "description": "覆盖边界情况并避免明显低效实现"},
        ],
        "quality_criteria": [
            {"id": "Q1", "name": "可读性与命名", "points": 3, "description": "命名清晰，代码易于理解"},
            {"id": "Q2", "name": "代码结构", "points": 3, "description": "结构清晰，职责合理"},
            {"id": "Q3", "name": "重复与冗余", "points": 2, "description": "没有明显重复或无效代码"},
            {"id": "Q4", "name": "接口、规范与安全", "points": 2, "description": "遵守函数接口和运行环境约束"},
        ],
        "uncertain_items": [],
    }


def _ensure_rubric(
    db: Session, question: JudgeQuestion, task: dict, *, is_exam: bool, clock: DemoClock,
) -> QuestionRubric:
    """为 AI 题目确保锁定 Rubric（幂等）。"""
    if not getattr(question, "reference_solution", None):
        question.reference_solution = task["solution"]
    snapshot = {
        "title": getattr(question, "title", None) or getattr(question, "prompt", ""),
        "description": task["description"],
        "function_name": task["function_name"],
        "is_exam": is_exam,
        "teacher_constraints": question.teacher_constraints or {},
        "test_groups": question.test_groups or [],
        "reference_solution": task["solution"],
    }
    hash_snapshot = {k: v for k, v in snapshot.items() if k != "reference_solution"}
    source_hash = hashlib.sha256(
        json.dumps(hash_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    rubric_json = _rubric_document(task, is_exam=is_exam)

    target = QuestionRubric.judge_question_id if not is_exam else QuestionRubric.exam_question_id
    existing = db.scalar(
        select(QuestionRubric).where(target == question.id, QuestionRubric.status == "locked")
        .order_by(QuestionRubric.version.desc()).limit(1)
    )
    if existing is not None:
        mark(db, "question_rubrics", existing.id)
        return existing
    max_version = db.scalar(select(func.max(QuestionRubric.version)).where(target == question.id)) or 0
    rubric = QuestionRubric(
        judge_question_id=question.id if not is_exam else None,
        exam_question_id=None if not is_exam else question.id,
        version=int(max_version) + 1,
        status="locked",
        source_hash=source_hash,
        source_snapshot=snapshot,
        rubric_json=rubric_json,
        model_name=_MODEL_NAME,
        raw_response=json.dumps(rubric_json, ensure_ascii=False),
        locked_at=clock.day(-20, 9),
    )
    db.add(rubric)
    db.flush()
    mark(db, "question_rubrics", rubric.id)
    logger.info("[创建] 锁定 Rubric %s（%s）",
                 getattr(question, "title", None) or getattr(question, "prompt", ""),
                 "exam" if is_exam else "assignment")
    return rubric


def create_assignment_ai_grades(
    db: Session, clock: DemoClock, users: dict, ai_questions: list[JudgeQuestion],
) -> int:
    """为 AI 作业的提交创建 CodeGrade（确定性 Fixture），返回创建条数。"""
    students: list[User] = users["students"]
    count = 0

    for question in ai_questions:
        task = _task_for_ai_question(question)
        rubric = _ensure_rubric(db, question, task, is_exam=False, clock=clock)

        submissions = db.scalars(
            select(Submission).where(
                Submission.question_id == question.id,
                Submission.grading_status == "completed",
            )
        ).all()
        for sub in submissions:
            student = db.get(User, sub.student_id)
            archetype = demo_archetype(student.username if student else None)
            profile = ARCHETYPES[archetype]
            rng = make_rng("ai_grade", student.username if student else "missing-demo-student",
                           getattr(question, "title", None) or getattr(question, "prompt", ""),
                           sub.attempt_count)

            existing = db.scalar(
                select(CodeGrade).where(CodeGrade.submission_id == sub.id)
            )
            if existing is not None:
                mark(db, "code_grades", existing.id)
                count += 1
                continue

            # 确定性分项：基于画像与固定种子
            f = float(sub.result_details.get("f_score", 0) or 0) if sub.result_details else 0.0
            r = float(sub.result_details.get("r_score", 0) or 0) if sub.result_details else 0.0
            a = _dimension_score(profile, rng, 20, archetype)
            q = _dimension_score(profile, rng, 10, archetype)
            merged = merge_scores(f=f, a=a, r=r, q=q, cap=None, exam_points=None)

            needs_review = rng.random() < profile["review_prob"]
            ai_result = _build_ai_result(task, f, a, r, q, archetype, needs_review)

            cg = CodeGrade(
                submission_id=sub.id,
                rubric_id=rubric.id,
                mode="active",
                status="review_required" if needs_review else "completed",
                functional_score=f,
                algorithm_score=a,
                robustness_score=r,
                quality_score=q,
                raw_total=merged.raw_total,
                score_cap=None,
                final_score_100=merged.final_score_100,
                scaled_score=merged.scaled_score,
                deterministic_details={
                    "groups": (sub.result_details or {}).get("groups", []),
                    "system_errors": [],
                    "seed_fixture": True,
                },
                static_analysis=analyze_python(sub.code),
                ai_result=ai_result,
                raw_response=json.dumps(ai_result, ensure_ascii=False),
                needs_teacher_review=needs_review,
                review_reason=("AI 置信度低，建议教师复核" if needs_review else None),
                attempt_count=1,
                queued_at=sub.finished_at,
                started_at=sub.finished_at,
                finished_at=sub.finished_at + timedelta(seconds=rng.randint(3, 20)),
            )
            db.add(cg)
            db.flush()
            mark(db, "code_grades", cg.id)
            # 正式分回写 Submission（与 worker/override 语义一致）
            if not needs_review:
                sub.score = merged.final_score_100
                sub.status = "graded"
            else:
                sub.score = None
                sub.status = "running"
            count += 1
    db.flush()
    return count


def _task_for_ai_question(question: JudgeQuestion) -> dict:
    from .tasks import BASIC_TASKS, DATA_TASKS
    for task in BASIC_TASKS + DATA_TASKS:
        if task["function_name"] == question.function_name:
            return task
    return BASIC_TASKS[0]


def _dimension_score(profile: dict, rng, max_score: int, archetype: str) -> float:
    """AI 维度得分：按画像区间 + 固定种子。"""
    lo_ratio = profile["score_lo"] / 100.0
    hi_ratio = profile["score_hi"] / 100.0
    # 困难学生 AI 维度压低
    if archetype == "struggling":
        hi_ratio = min(hi_ratio, 0.55)
    ratio = rng.uniform(lo_ratio, hi_ratio)
    return round(max_score * ratio, 2)


def _build_ai_result(task: dict, f: float, a: float, r: float, q: float,
                     archetype: str, needs_review: bool) -> dict:
    """构造符合 AIGradeResponse 的 ai_result（student_feedback 按画像）。"""
    from .tasks import AI_ROBUSTNESS_TESTS

    a_items = [
        {"criterion_id": "A1", "criterion": "核心功能实现", "level": "complete" if a >= 8 else "partial",
         "score": min(a, 10.0), "max_score": 10, "code_lines": [1, 2, 3], "evidence": "功能测试通过",
         "reason_code": None, "deduction_reason": None},
        {"criterion_id": "A2", "criterion": "算法思路与实现", "level": "complete" if a >= 5 else "partial",
         "score": max(0.0, min(a - 10, 6.0)), "max_score": 6, "code_lines": [3, 4],
         "evidence": "实现逻辑清晰", "reason_code": None, "deduction_reason": None},
        {"criterion_id": "A3", "criterion": "边界处理与复杂度", "level": "partial" if a < 3 else "complete",
         "score": max(0.0, min(a - 16, 4.0)), "max_score": 4, "code_lines": [5],
         "evidence": "边界情况已覆盖", "reason_code": None, "deduction_reason": None},
    ]
    q_items = [
        {"criterion_id": "Q1", "criterion": "可读性与命名", "level": "complete" if q >= 2.4 else "partial",
         "score": min(q, 3.0), "max_score": 3, "code_lines": [1], "evidence": "命名清晰",
         "reason_code": None, "deduction_reason": None},
        {"criterion_id": "Q2", "criterion": "代码结构", "level": "complete" if q >= 2.4 else "partial",
         "score": min(max(q - 3, 0), 3.0), "max_score": 3, "code_lines": [2, 3],
         "evidence": "结构合理", "reason_code": None, "deduction_reason": None},
        {"criterion_id": "Q3", "criterion": "重复与冗余", "level": "complete" if q >= 1.6 else "partial",
         "score": min(max(q - 6, 0), 2.0), "max_score": 2, "code_lines": [], "evidence": "无明显重复",
         "reason_code": None, "deduction_reason": None},
        {"criterion_id": "Q4", "criterion": "接口、规范与安全", "level": "complete" if q >= 1.6 else "partial",
         "score": min(max(q - 8, 0), 2.0), "max_score": 2, "code_lines": [1],
         "evidence": "遵守接口约定", "reason_code": None, "deduction_reason": None},
    ]
    feedback = {
        "strengths": ["实现了核心函数", "通过功能测试"] if archetype != "struggling"
        else ["完成了基本结构"],
        "issues": [] if archetype == "elite" else
        (["边界情况处理不足", "存在冗余代码"] if archetype == "average" else
         ["核心功能未完全实现", "缺少异常处理", "建议复习函数定义与返回值"]),
        "suggestions": [] if archetype == "elite" else
        (["补充边界测试", "简化重复分支"] if archetype == "average" else
         ["从最简单用例开始逐步调试", "先实现功能再优化结构"]),
        "code_suggestions": [] if archetype == "elite" else
        [{"title": "改进返回值处理", "diff": "-    return None\n+    return total"}],
    }
    return {
        "rubric_version": 1,
        "algorithm": {"dimension_score": round(a, 2), "dimension_max": 20.0, "items": a_items},
        "code_quality": {"dimension_score": round(q, 2), "dimension_max": 10.0, "items": q_items},
        "triggered_cap_rule_ids": [],
        "uncertainties": ["seed_fixture"] ,
        "needs_teacher_review": needs_review,
        "review_reason": "AI 置信度低，建议教师复核" if needs_review else None,
        "student_feedback": feedback,
        "seed_fixture": True,
    }

def create_exam_ai_grades(
    db: Session, clock: DemoClock, users: dict, exams: dict,
) -> int:
    """为考试编程题答案创建 CodeGrade（确定性 Fixture），返回创建条数。"""
    from app.models import ExamAnswer, ExamQuestion, ExamSubmission

    count = 0
    for exam in exams.values():
        code_questions = list(
            db.scalars(
                select(ExamQuestion).where(
                    ExamQuestion.exam_id == exam.id,
                    ExamQuestion.question_type == "code",
                )
            ).all()
        )
        for question in code_questions:
            task = _task_for_exam_question(question)
            rubric = _ensure_rubric(db, question, task, is_exam=True, clock=clock)
            answers = db.scalars(
                select(ExamAnswer).where(
                    ExamAnswer.question_id == question.id,
                    ExamAnswer.grading_status == "pending",
                )
            ).all()
            for ans in answers:
                sub = db.get(ExamSubmission, ans.submission_id)
                if sub is None:
                    continue
                student = db.get(User, sub.student_id)
                archetype = "average"
                if student is not None:
                    archetype = demo_archetype(student.username)
                profile = ARCHETYPES[archetype]
                rng = make_rng("exam_ai_grade", student.username if student else "missing-demo-student",
                               exam.title, question.order_index,
                               getattr(question, "title", None) or getattr(question, "prompt", ""))

                existing = db.scalar(
                    select(CodeGrade).where(CodeGrade.exam_answer_id == ans.id)
                )
                if existing is not None:
                    mark(db, "code_grades", existing.id)
                    count += 1
                    continue

                f = round(60 * rng.uniform(0.3 if archetype == "struggling" else 0.7, 1.0), 2)
                r = round(10 * rng.uniform(0.3 if archetype == "struggling" else 0.7, 1.0), 2)
                a = _dimension_score(profile, rng, 20, archetype)
                q = _dimension_score(profile, rng, 10, archetype)
                merged = merge_scores(f=f, a=a, r=r, q=q, cap=None, exam_points=question.points)
                needs_review = rng.random() < profile["review_prob"]

                released_at = (
                    clock.midterm_review_released() if exam.title.startswith("期中")
                    else clock.quiz_end() if exam.title.startswith("章节")
                    else clock.final_published()
                )
                cg = CodeGrade(
                    exam_answer_id=ans.id,
                    rubric_id=rubric.id,
                    mode="active",
                    status="review_required" if needs_review else "completed",
                    functional_score=f,
                    algorithm_score=a,
                    robustness_score=r,
                    quality_score=q,
                    raw_total=merged.raw_total,
                    score_cap=None,
                    final_score_100=merged.final_score_100,
                    scaled_score=merged.scaled_score,
                    deterministic_details={
                        "groups": [], "system_errors": [], "seed_fixture": True,
                    },
                    static_analysis=analyze_python(ans.code_answer or ""),
                    ai_result=_build_ai_result(task, f, a, r, q, archetype, needs_review),
                    raw_response=json.dumps(_build_ai_result(task, f, a, r, q, archetype, needs_review), ensure_ascii=False),
                    needs_teacher_review=needs_review,
                    review_reason=("AI 置信度低，建议教师复核" if needs_review else None),
                    attempt_count=1,
                    finished_at=released_at + timedelta(hours=rng.randint(1, 8)),
                )
                db.add(cg)
                db.flush()
                mark(db, "code_grades", cg.id)
                if not needs_review:
                    ans.score = round(merged.scaled_score, 2)
                    ans.grading_status = "completed"
                    _recompute_exam_submission_score(db, sub.id)
                else:
                    ans.score = None
                    ans.grading_status = "completed"
                    # 需复核：父提交保持非终态，等待教师处理
                    if sub.status in ("graded",):
                        sub.status = "review_required"
                        sub.review_reason = "编程题需要教师复核"
                        sub.review_required_at = released_at
                count += 1
    db.flush()
    return count


def _recompute_exam_submission_score(db: Session, submission_id: int) -> None:
    """重算考试提交总分：所有已完成答案的分数之和（编程题 AI 分计入）。"""
    from app.models import ExamAnswer, ExamSubmission as _ES

    # 会话 autoflush=False：先 flush 让刚写入的答案分数/状态对查询可见
    db.flush()
    sub = db.get(_ES, submission_id)
    if sub is None:
        return
    answers = db.scalars(
        select(ExamAnswer).where(
            ExamAnswer.submission_id == submission_id,
            ExamAnswer.grading_status == "completed",
        )
    ).all()
    total = round(sum(a.score or 0 for a in answers), 2)
    if sub.status in ("graded", "review_required"):
        sub.score = total if sub.status == "graded" else None
        db.flush()


def _task_for_exam_question(question) -> dict:  # noqa: ANN001 —— ExamQuestion 局部导入，避免模块级循环
    from .tasks import BASIC_TASKS
    prompt = question.prompt or ""
    if "括号匹配" in prompt or "is_balanced" in prompt:
        return BASIC_TASKS[1]
    if "线性预测" in prompt or "predict" in prompt:
        return {
            "title": "线性拟合预测", "function_name": "predict_linear",
            "description": "对样本进行一次线性拟合，并预测目标值。",
            "solution": "def predict_linear(x, y, target):\n    import numpy as np\n    slope, intercept = np.polyfit(np.asarray(x), np.asarray(y), 1)\n    return float(slope * target + intercept)",
        }
    return BASIC_TASKS[0]
