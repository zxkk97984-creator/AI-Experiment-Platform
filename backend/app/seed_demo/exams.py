# -*- coding: utf-8 -*-
"""考试全链：考试、题目、考试提交、答案、成绩。

状态机（当前代码为准）：exam.status draft/published；exam_submissions.status
started/submitted/grading/graded/review_required。期中全量 graded 且复核已发布；
期末已发布未开始；章节测验混合状态。
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Exam,
    ExamAnswer,
    ExamQuestion,
    ExamSubmission,
    User,
)

from .constants import ARCHETYPES, BACKGROUND_ARCHETYPE, FIXED_STUDENT_DEFS, FLAGSHIP_COURSE_TITLE
from .marks import mark
from .rng import make_rng
from .tasks import BASIC_TASKS
from .timeline import DemoClock

logger = logging.getLogger("dai.seed_demo.exams")


def create_exams(
    db: Session, clock: DemoClock, users: dict, courses: dict,
) -> dict:
    """创建 3 场考试及题目；返回 {'midterm': Exam, 'final': Exam, 'quiz': Exam}。"""
    flagship = courses[FLAGSHIP_COURSE_TITLE]
    ml_course = courses.get("机器学习基础", flagship)
    teacher_zhang = users["teacher_zhang"]
    teacher_chen = users["teacher_chen"]

    exams = {
        "midterm": _ensure_exam(
            db, flagship, "期中测验：Python 函数与数据结构",
            clock.midterm_start(), clock.midterm_end(), teacher_zhang.id,
            show_score=True, show_questions=True, show_answers=True,
            review_released_at=clock.midterm_review_released(),
        ),
        "final": _ensure_exam(
            db, flagship, "期末上机考试：Python 与 AI 综合",
            clock.final_start(), clock.final_end(), teacher_zhang.id,
        ),
        "quiz": _ensure_exam(
            db, ml_course, "章节测验：线性模型",
            clock.quiz_start(), clock.quiz_end(), teacher_chen.id,
        ),
    }

    # 题目（幂等：按 exam + order_index + question_type 查重）
    _ensure_questions(db, exams["midterm"], "midterm")
    _ensure_questions(db, exams["final"], "final")
    _ensure_questions(db, exams["quiz"], "quiz")
    db.flush()
    return exams


def _ensure_exam(db, course, title, start_at, end_at, teacher_id, *,
                 show_score=False, show_questions=False, show_answers=False,
                 review_released_at=None) -> Exam:
    exam = db.scalar(select(Exam).where(Exam.course_id == course.id, Exam.title == title))
    if exam is None:
        exam = Exam(
            course_id=course.id,
            title=title,
            status="published",
            duration_minutes=60,
            start_at=start_at,
            end_at=end_at,
            created_by_id=teacher_id,
            show_score_after_grading=show_score,
            show_questions_after_review=show_questions,
            show_answers_after_review=show_answers,
            review_released_at=review_released_at,
            review_released_by_id=teacher_id if review_released_at else None,
        )
        db.add(exam)
        db.flush()
        logger.info("[创建] 考试 %s", title)
    else:
        exam.status = "published"
        exam.start_at = start_at
        exam.end_at = end_at
        exam.show_score_after_grading = show_score
        exam.show_questions_after_review = show_questions
        exam.show_answers_after_review = show_answers
        exam.review_released_at = review_released_at
        db.flush()
        logger.info("[更新] 考试 %s", title)
    mark(db, "exams", exam.id)
    return exam


def _ensure_questions(db, exam: Exam, kind: str) -> None:
    """按考试类型定义题目（order_index 递增，幂等）。"""
    if kind == "midterm":
        spec = [
            ("single_choice", "下列关于 Python 列表的说法，正确的是？", 20,
             {"A": "列表元素类型必须一致", "B": "列表可以通过下标访问元素", "C": "列表不可变", "D": "列表只能存数字"},
             {"correct": ["B"]}),
            ("single_choice", "调用 len('Python') 的返回值是？", 20,
             {"A": "5", "B": "6", "C": "7", "D": "报错"}, {"correct": ["B"]}),
            ("multi_choice", "以下哪些是 Python 内置数据结构？（多选）", 20,
             {"A": "list", "B": "dict", "C": "set", "D": "tuple"}, {"correct": ["A", "B", "C", "D"]}),
            ("fill_blank", "Python 中定义函数使用关键字 ____。", 20, None,
             {"blanks": [{"id": "b1", "accepted_answers": ["def"], "case_sensitive": False}]}),
            ("code", "编程题：实现正数求和函数（AI 评分）", 20, None, {}),
        ]
    elif kind == "final":
        spec = [
            ("single_choice", "下列关于 Python 字典的说法，正确的是？", 20,
             {"A": "键必须唯一", "B": "值必须唯一", "C": "键可以是列表", "D": "字典不可遍历"},
             {"correct": ["A"]}),
            ("single_choice", "import numpy as np 中 np 是 numpy 的？", 20,
             {"A": "函数", "B": "别名", "C": "模块", "D": "包"}, {"correct": ["B"]}),
            ("multi_choice", "以下哪些操作会修改原列表？（多选）", 20,
             {"A": "append", "B": "extend", "C": "sort", "D": "sorted"}, {"correct": ["A", "B", "C"]}),
            ("fill_blank", "Python 中生成 1 到 5 的整数序列使用 range(____)。", 20, None,
             {"blanks": [{"id": "b1", "accepted_answers": ["1, 6", "1,6"], "case_sensitive": False}]}),
            ("code", "编程题：实现括号匹配（AI 评分）", 20, None, {}),
        ]
    else:  # quiz
        spec = [
            ("single_choice", "线性回归模型的最常见损失函数是？", 20,
             {"A": "交叉熵", "B": "均方误差", "C": "Hinge Loss", "D": "KL 散度"}, {"correct": ["B"]}),
            ("single_choice", "过拟合通常表现为？", 20,
             {"A": "训练误差低、验证误差高", "B": "训练误差高、验证误差低",
              "C": "训练与验证误差都低", "D": "训练与验证误差都高"}, {"correct": ["A"]}),
            ("multi_choice", "以下哪些属于模型评估指标？（多选）", 20,
             {"A": "准确率", "B": "召回率", "C": "F1", "D": "学习率"}, {"correct": ["A", "B", "C"]}),
            ("fill_blank", "train_test_split 用于划分训练集与____集。", 20, None,
             {"blanks": [{"id": "b1", "accepted_answers": ["验证"], "case_sensitive": False}]}),
            ("code", "编程题：实现简单线性预测（AI 评分）", 20, None, {}),
        ]
    for order_index, (qtype, prompt, points, options, correct) in enumerate(spec):
        question = db.scalar(
            select(ExamQuestion).where(
                ExamQuestion.exam_id == exam.id,
                ExamQuestion.order_index == order_index,
            )
        )
        if question is None:
            question = ExamQuestion(
                exam_id=exam.id,
                question_type=qtype,
                prompt=prompt,
                points=points,
                options=options,
                correct_answer=correct,
                order_index=order_index,
                starter_code=("def sum_positive(values):\n    pass" if qtype == "code" and kind == "midterm" else
                              "def is_balanced(text):\n    pass" if qtype == "code" and kind == "final" else
                              "def predict(x, y, target):\n    pass" if qtype == "code" else None),
                grading_mode="active" if qtype == "code" else "legacy",
                time_limit_ms=10000 if qtype == "code" else None,
                memory_limit_mb=256 if qtype == "code" else None,
            )
            db.add(question)
            db.flush()
            logger.info("[创建] 考试题目 %s/%s", exam.title, prompt[:20])
        else:
            # 幂等修复：同步字段（尤其历史 correct_answer=None 的编程题）
            changed = False
            for field, value in (
                ("question_type", qtype),
                ("prompt", prompt),
                ("points", points),
                ("options", options),
                ("correct_answer", correct),
                ("grading_mode", "active" if qtype == "code" else "legacy"),
                ("starter_code", "def sum_positive(values):\n    pass" if qtype == "code" and kind == "midterm" else
                                 "def is_balanced(text):\n    pass" if qtype == "code" and kind == "final" else
                                 "def predict(x, y, target):\n    pass" if qtype == "code" else None),
            ):
                if getattr(question, field) != value:
                    setattr(question, field, value)
                    changed = True
            if changed:
                db.flush()
                logger.info("[更新] 考试题目 %s/%s（字段同步）", exam.title, prompt[:20])
        mark(db, "exam_questions", question.id)
    db.flush()


def create_exam_submissions(
    db: Session, clock: DemoClock, users: dict, exams: dict,
) -> None:
    """创建考试提交与答案。

    - 期中：全部选课学生 graded（成绩已发布）；
    - 期末：已发布未开始，不创建 started 提交；
    - 测验：混合状态（graded/submitted/review_required/missed）。
    """
    students: list[User] = users["students"]
    archetype_map = {uname: a for uname, _n, a in FIXED_STUDENT_DEFS}

    _fill_midterm(db, clock, users, exams["midterm"], students, archetype_map)
    _fill_final(db, clock, users, exams["final"], students, archetype_map)
    _fill_quiz(db, clock, users, exams["quiz"], students, archetype_map)
    db.flush()


def sync_exam_grades(db: Session, exams: dict) -> int:
    """通过正式考试 finalizer 汇总 seed 构造的历史时间线。

    Seed 仍然可以预先构造历史提交，但必须把可评分提交留在 ``grading``，
    由生产链路共用的 ``finalize_if_ready`` 负责状态转换、求和和 ExamGrade
    幂等 upsert。这样演示数据不会再维护一份可能与线上规则漂移的汇总实现。
    """
    from app.services.exam_grading import FinalizeOutcome, finalize_if_ready

    exam_ids = [exam.id for exam in exams.values()]
    submission_ids = list(
        db.scalars(
            select(ExamSubmission.id).where(
                ExamSubmission.exam_id.in_(exam_ids),
                ExamSubmission.status == "grading",
            )
        ).all()
    )
    finalized = 0
    for submission_id in submission_ids:
        result = finalize_if_ready(submission_id, db)
        if result.outcome == FinalizeOutcome.GRADED:
            finalized += 1
    return finalized


def _exam_questions(db, exam: Exam) -> list[ExamQuestion]:
    return list(
        db.scalars(
            select(ExamQuestion).where(ExamQuestion.exam_id == exam.id)
            .order_by(ExamQuestion.order_index)
        ).all()
    )


def _fill_midterm(db, clock, users, exam, students, archetype_map):
    """期中：60 名学生全部 graded。"""
    questions = _exam_questions(db, exam)
    teacher = users["teacher_zhang"]
    for student in students:
        archetype = archetype_map.get(student.username, BACKGROUND_ARCHETYPE)
        profile = ARCHETYPES[archetype]
        rng = make_rng("exam_midterm", student.username, exam.title)

        sub = db.scalar(
            select(ExamSubmission).where(
                ExamSubmission.exam_id == exam.id,
                ExamSubmission.student_id == student.id,
            )
        )
        if sub is not None:
            mark(db, "exam_submissions", sub.id)
            for ans in sub.answers:
                mark(db, "exam_answers", ans.id)
            continue

        started_at = clock.midterm_start() + timedelta(minutes=rng.randint(5, 40))
        expires_at = started_at + timedelta(minutes=exam.duration_minutes)
        submitted_at = started_at + timedelta(minutes=rng.randint(20, 55))
        sub = ExamSubmission(
            exam_id=exam.id,
            student_id=student.id,
            status="grading",
            score=None,
            started_at=started_at,
            expires_at=expires_at,
            last_saved_at=submitted_at,
            submission_reason="time_up" if rng.random() < 0.2 else "submit",
            submitted_at=submitted_at,
            graded_at=None,
        )
        db.add(sub)
        db.flush()
        mark(db, "exam_submissions", sub.id)

        for question in questions:
            # 选择题：按画像正确率决定对错；编程题：AI 评分（CodeGrade 单独处理）
            ans = db.scalar(
                select(ExamAnswer).where(
                    ExamAnswer.submission_id == sub.id,
                    ExamAnswer.question_id == question.id,
                )
            )
            if ans is not None:
                mark(db, "exam_answers", ans.id)
                continue
            if question.question_type == "single_choice":
                correct_prob = (profile["score_lo"] + profile["score_hi"]) / 200.0
                right = rng.random() < correct_prob
                selected = [question.options and list(question.options.keys())[0]]
                score = question.points if right else 0
                grading = "completed"
                ans = ExamAnswer(
                    submission_id=sub.id, question_id=question.id,
                    selected_options=selected, score=score, grading_status=grading,
                )
            elif question.question_type == "multi_choice":
                # 部分给分：正确选项全部选中得满分；部分正确按比例；选错选项 0 分
                correct_set = set((question.correct_answer or {}).get("correct", []))
                all_options = list(question.options.keys())
                correct_prob = (profile["score_lo"] + profile["score_hi"]) / 200.0
                rng_sel = rng.random()
                if rng_sel < correct_prob:
                    selected = sorted(correct_set)
                    score = question.points
                elif rng_sel < correct_prob + 0.2:
                    # 漏选：按选中正确项比例给分
                    chosen_correct = rng.sample(sorted(correct_set), max(1, len(correct_set) - 1))
                    selected = sorted(chosen_correct)
                    score = round(question.points * len(chosen_correct) / max(1, len(correct_set)), 2)
                else:
                    # 错选：0 分（全部选项都正确时退化为漏选分支）
                    wrong_pool = [o for o in all_options if o not in correct_set]
                    if wrong_pool:
                        selected = sorted(list(correct_set)[:1] + [rng.choice(wrong_pool)])
                    else:
                        chosen = rng.sample(sorted(correct_set), max(1, len(correct_set) - 1))
                        selected = sorted(chosen)
                    score = 0
                ans = ExamAnswer(
                    submission_id=sub.id, question_id=question.id,
                    selected_options=selected, score=score, grading_status="completed",
                )
            elif question.question_type == "fill_blank":
                # 学生答案格式：{blank_id: 填写内容}（与 score_fill_blank_answer 一致）
                right = rng.random() < 0.8 if archetype != "struggling" else rng.random() < 0.4
                blanks = (question.correct_answer or {}).get("blanks", [])
                filled = {
                    str(blank.get("id", "")): (blank.get("accepted_answers") or [""])[0]
                    if right else "（未作答）"
                    for blank in blanks
                }
                score = question.points if right else 0
                ans = ExamAnswer(
                    submission_id=sub.id, question_id=question.id,
                    text_answers=filled, score=score, grading_status="completed",
                )
            else:  # code：AI 评分题目，答案先记录，CodeGrade 在 ai_grading 模块创建
                task = BASIC_TASKS[0]
                score = None
                grading = "pending"
                ans = ExamAnswer(
                    submission_id=sub.id, question_id=question.id,
                    code_answer=task["solution"], score=score, grading_status=grading,
                )
            db.add(ans)
            db.flush()
            mark(db, "exam_answers", ans.id)

        # The formal finalizer computes the total after the code answer is
        # completed.  Do not persist a partial score while the parent is
        # still in grading.
        sub.score = None
        db.flush()


def _fill_final(db, clock, users, exam, students, archetype_map):
    """期末：已发布未开始，不创建 started 提交。

    历史版本曾用 clock.final_start() 创建未来 started 提交，导致学生端把
    60 分钟的考试倒计时显示为 817 小时（expires_at 远晚于服务器当前时间）。
    """
    return


def _fill_quiz(db, clock, users, exam, students, archetype_map):
    """章节测验：混合状态（graded / submitted / review_required / missed）。"""
    questions = _exam_questions(db, exam)
    teacher = users["teacher_chen"]
    enrolled = [u for u in students if u.username.startswith("student_")][:12]
    for student in enrolled:
        rng = make_rng("exam_quiz", student.username, exam.title)
        existing = db.scalar(
            select(ExamSubmission).where(
                ExamSubmission.exam_id == exam.id,
                ExamSubmission.student_id == student.id,
            )
        )
        if existing is not None:
            mark(db, "exam_submissions", existing.id)
            for ans in existing.answers:
                mark(db, "exam_answers", ans.id)
            continue
        # 状态分布：graded / submitted / review_required；missed 不建提交行
        # （考试成绩视图的 missed 是"已选课但无提交"的派生状态，不是存储状态）
        state_roll = rng.random()
        if state_roll < 0.55:
            # Leave publishable submissions in the same intermediate state as
            # the production submit -> grading -> finalize path.
            status, score = "grading", None
        elif state_roll < 0.75:
            status, score = "submitted", None
        else:
            status, score = "review_required", None

        started_at = clock.quiz_start() + timedelta(minutes=rng.randint(5, 40))
        sub = ExamSubmission(
            exam_id=exam.id, student_id=student.id,
            status=status, score=score,
            started_at=started_at,
            expires_at=started_at + timedelta(minutes=exam.duration_minutes),
            last_saved_at=started_at + timedelta(minutes=20) if status != "review_required" else None,
            submitted_at=started_at + timedelta(minutes=rng.randint(20, 55)) if status in ("grading", "submitted", "review_required") else None,
            graded_at=None,
            review_reason="编程题需要教师复核" if status == "review_required" else None,
            review_required_at=clock.quiz_end() if status == "review_required" else None,
        )
        db.add(sub)
        db.flush()
        mark(db, "exam_submissions", sub.id)
        for question in questions:
            ans = ExamAnswer(
                submission_id=sub.id, question_id=question.id,
                selected_options=(list(question.options.keys())[:1] if question.options else None),
                code_answer=(BASIC_TASKS[0]["solution"] if question.question_type == "code" else None),
                score=(question.points if question.question_type != "code" and status == "grading" else None),
                grading_status=("completed" if question.question_type != "code" and status == "grading" else "pending"),
            )
            db.add(ans)
            db.flush()
            mark(db, "exam_answers", ans.id)
        # ``grading`` is finalized after AI code grading below; ``submitted``
        # and ``review_required`` intentionally have no aggregate score.
