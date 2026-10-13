"""练习徽章与打卡激励（批次 6）：四个徽章、幂等发放、与提交终态同一事务。

规矩（写错就会出现「重复投递发两条徽章」或「没通关却有徽章」）：

1. 唯一 ``(student_id, badge_key)`` 由数据库约束保证；发放前先查存在性，
   并发下再用 SAVEPOINT + IntegrityError 兜底，绝不让唯一冲突把提交终态一起炸掉。
2. 发放只由**提交终态写入**触发（``settle_after_terminal_write``），与终态写入
   同一事务：事务回滚，徽章一起消失；worker 重试/重复投递因为唯一约束只发一次。
3. 徽章只看「当前评分版本」下的通过题数与连续打卡（与榜单同一口径），
   改题后旧成绩退出当前口径，不追溯撤销已发徽章（激励一旦获得不回收）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.practice import (
    PRACTICE_BADGE_KEYS, PRACTICE_BADGE_THRESHOLDS, PracticeBadgeAward,
    PracticeSubmission,
)
from app.services import practice_service

logger = logging.getLogger("dai.practice")

# 徽章定义（顺序即徽章墙顺序）；metric 说明进度取自通过题数还是连续天数
BADGE_DEFINITIONS = (
    {"key": "first_solved", "label": "首题通关", "metric": "solved_count",
     "description": "通过第一道练习题"},
    {"key": "solved_10", "label": "十题达成", "metric": "solved_count",
     "description": "累计通过 10 道练习题"},
    {"key": "solved_50", "label": "五十题达成", "metric": "solved_count",
     "description": "累计通过 50 道练习题"},
    {"key": "streak_7", "label": "连续打卡 7 天", "metric": "streak_days",
     "description": "连续 7 天每天都有新增通关"},
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def earned_keys(*, solved_count: int, streak_days: int) -> dict[str, int]:
    """按当前进度算出**应当拥有**的徽章及达成数值（不查库）。"""
    progress = {"solved_count": int(solved_count or 0),
                "streak_days": int(streak_days or 0)}
    earned: dict[str, int] = {}
    for key, definition in ((item["key"], item) for item in BADGE_DEFINITIONS):
        value = progress[definition["metric"]]
        if value >= PRACTICE_BADGE_THRESHOLDS[key]:
            earned[key] = value
    return earned


def award_missing(db: Session, *, student_id: int, solved_count: int, streak_days: int,
                  source_submission_id: int | None = None) -> list[str]:
    """补发缺失徽章；已存在的不动，唯一冲突静默忽略（并发重复投递）。"""
    granted: list[str] = []
    for key, value in earned_keys(solved_count=solved_count,
                                  streak_days=streak_days).items():
        exists = db.scalar(
            select(PracticeBadgeAward.id).where(
                PracticeBadgeAward.student_id == student_id,
                PracticeBadgeAward.badge_key == key,
            )
        )
        if exists is not None:
            continue
        try:
            # SAVEPOINT：并发下唯一约束冲突只回滚这一条 INSERT，
            # 不能把调用方的提交终态事务一起回滚（否则学生成绩会丢）
            with db.begin_nested():
                db.execute(insert(PracticeBadgeAward).values(
                    student_id=student_id,
                    badge_key=key,
                    progress_value=int(value),
                    source_submission_id=source_submission_id,
                    awarded_at=utcnow(),
                ))
        except IntegrityError:
            logger.info("徽章并发重复发放已忽略: student=%s badge=%s", student_id, key)
            continue
        granted.append(key)
    return granted


def settle_after_terminal_write(db: Session, submission_id: int) -> None:
    """提交终态写入后的同一事务结算：重算统计 → 补发徽章。

    由 ``app.models.practice`` 的会话钩子在 ``practice_submissions`` UPDATE
    命中后调用，因此确定性判题与 AI 终态两条路径都覆盖。
    """
    from app.services import practice_stats_service

    submission = db.scalar(
        select(PracticeSubmission)
        .where(PracticeSubmission.id == submission_id)
        .execution_options(populate_existing=True)
    )
    if submission is None:
        return
    if submission.grading_status not in ("completed", "review_required"):
        return
    if submission.score is None:
        # AI 未完成的 review_required、或平台问题导致的空分：不算通关，不发徽章
        return
    result = practice_stats_service.recompute_student_stats(
        db, student_id=submission.student_id, commit=False)
    granted = award_missing(
        db, student_id=submission.student_id,
        solved_count=result["solved_count"], streak_days=result["streak_days"],
        source_submission_id=submission_id,
    )
    if granted:
        logger.info("练习徽章发放: student=%s badges=%s", submission.student_id, granted)


def settle_related_updates(db: Session, related_updates) -> list[int]:
    """AI 终态「关联写入」里的练习提交结算；其他目标类型原样忽略。

    这是唯一的跨域收口点：``ai_grading_queue.finish_ai_grade`` 用同一段代码写
    作业 / 考试 / 实验 / 练习四种目标，只有练习需要结算激励（作业与考试的
    成绩不计入练习榜，也不发练习徽章）。
    """
    settled: list[int] = []
    for model, pk, _values in (related_updates or []):
        if getattr(model, "__tablename__", None) != PracticeSubmission.__tablename__:
            continue
        settle_after_terminal_write(db, pk)
        settled.append(pk)
    return settled


def badge_wall(db: Session, *, student_id: int, solved_count: int | None = None,
               streak_days: int | None = None) -> list[dict]:
    """徽章墙数据：四个徽章 + 是否获得 + 获得时间 + 达成条件。

    未显式传入进度时读统计快照（学生自己的行，量级极小）。
    """
    from app.services import practice_stats_service

    if solved_count is None or streak_days is None:
        snapshot = practice_stats_service.stats_snapshot(db, student_id=student_id)
        platform = snapshot["platform"].get("deterministic") or {}
        if solved_count is None:
            solved_count = int(platform.get("solved_count") or 0)
        if streak_days is None:
            streak_days = int(platform.get("streak_days") or 0)
    awards = {
        row.badge_key: row
        for row in db.scalars(
            select(PracticeBadgeAward).where(PracticeBadgeAward.student_id == student_id)
        ).all()
    }
    progress = {"solved_count": int(solved_count or 0), "streak_days": int(streak_days or 0)}
    wall = []
    for definition in BADGE_DEFINITIONS:
        key = definition["key"]
        award = awards.get(key)
        current = progress[definition["metric"]]
        wall.append({
            "key": key,
            "label": definition["label"],
            "description": definition["description"],
            "threshold": PRACTICE_BADGE_THRESHOLDS[key],
            "metric": definition["metric"],
            "progress": min(current, PRACTICE_BADGE_THRESHOLDS[key]),
            "earned": award is not None,
            "awarded_at": (award.awarded_at if award is not None else None),
        })
    return wall


def badge_keys() -> tuple[str, ...]:
    return PRACTICE_BADGE_KEYS


def solved_threshold() -> float:
    return practice_service.PRACTICE_SOLVED_SCORE
