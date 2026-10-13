"""练习个人统计与重算（批次 6）：platform / offering × deterministic / ai。

口径（与 ``practice_service`` 的榜单口径一致，避免两处各算一套）：

* 只有**当前评分版本**下的提交计入：``grading_version_id`` 必须等于该榜口径
  （deterministic=legacy / ai=active）下该题的最新版本，改题后旧成绩自动退出当前榜；
* 每题只取历史最高分；得分 ≥ ``PRACTICE_SOLVED_SCORE`` 记为「通过」；
* ``achieved_at`` 取计入提交里最早的完成时间，作为排序第三关键字；
* 打卡：以「某题首次通过」的日期为活跃日，同日多次通关只算一天；
  ``streak_days`` 是截至最后一次活跃日的连续天数，距今超过 1 天即断签归零。

请求路径只读 ``practice_student_stats`` 快照；写路径（重算）由
``practice_recalc_job`` 在 ``scheduler_lease`` 保护下按间隔执行，幂等可重入。
"""
from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.practice import (
    PracticeBoardPreference, PracticeStudentStat, PracticeSubmission,
)
from app.models.teaching import OfferingEnrollment
from app.services import practice_service

BOARD_KINDS = ("deterministic", "ai")
PLATFORM_SCOPE_ID = 0
GROWTH_DAYS = 30
# 断签判定：最后一次活跃日距今天超过该天数即视为断签
STREAK_GRACE_DAYS = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _today(now: datetime | None = None) -> date_cls:
    return (now or utcnow()).date()


def active_offering_ids(db: Session, student_id: int) -> list[int]:
    """学生当前在读的教学班（班内榜统计行按这些班级展开）。"""
    return sorted(set(int(oid) for oid in db.scalars(
        select(OfferingEnrollment.offering_id).where(
            OfferingEnrollment.student_id == student_id,
            OfferingEnrollment.status == "active",
        )
    ).all()))


def counted_rows(db: Session, *, student_id: int) -> list[dict]:
    """该学生当前口径下计入榜单的提交（每题每个榜可多条，取最高分在调用方）。"""
    eligible = {board: practice_service.eligible_version_map(db, board=board)
                for board in BOARD_KINDS}
    rows = db.execute(
        select(
            PracticeSubmission.id,
            PracticeSubmission.problem_id,
            PracticeSubmission.grading_mode,
            PracticeSubmission.score,
            PracticeSubmission.finished_at,
            PracticeSubmission.grading_version_id,
        )
        .where(
            PracticeSubmission.student_id == student_id,
            PracticeSubmission.score.is_not(None),
            PracticeSubmission.counted_for_rank.is_(True),
        )
        .order_by(PracticeSubmission.id)
    ).all()
    counted: list[dict] = []
    for submission_id, problem_id, board, score, finished, version_id in rows:
        board_map = eligible.get(board)
        if not board_map or board_map.get(int(problem_id)) != version_id:
            continue
        counted.append({
            "submission_id": int(submission_id),
            "problem_id": int(problem_id),
            "board": board,
            "score": float(score),
            "finished_at": _as_utc(finished),
            "grading_version_id": version_id,
        })
    return counted


def board_metrics(rows: list[dict], board: str) -> dict:
    """单个榜的聚合：每题最高分 → 总分 / 通过题数 / 平均分 / 正确率 / 达成时间。"""
    best: dict[int, float] = {}
    achieved: datetime | None = None
    for row in rows:
        if row["board"] != board:
            continue
        problem_id = row["problem_id"]
        best[problem_id] = max(best.get(problem_id, 0.0), row["score"])
        finished = row["finished_at"]
        if finished is not None and (achieved is None or finished < achieved):
            achieved = finished
    solved = sum(1 for value in best.values()
                 if value >= practice_service.PRACTICE_SOLVED_SCORE)
    total = round(sum(best.values()), 4)
    attempted = len(best)
    return {
        "solved_count": solved,
        "total_score": total,
        "avg_score": round(total / attempted, 4) if attempted else None,
        "accuracy": round(solved / attempted, 4) if attempted else None,
        "achieved_at": achieved,
        "attempted": attempted,
    }


def activity_state(rows: list[dict], *, today: date_cls | None = None) -> dict:
    """打卡状态：活跃日 = 某题**首次通过**的日期（同日多次通关只算一天）。"""
    today = today or _today()
    first_solve: dict[int, datetime] = {}
    for row in rows:
        if row["score"] < practice_service.PRACTICE_SOLVED_SCORE:
            continue
        finished = row["finished_at"]
        if finished is None:
            continue
        problem_id = row["problem_id"]
        if problem_id not in first_solve or finished < first_solve[problem_id]:
            first_solve[problem_id] = finished
    active_dates = sorted({value.date() for value in first_solve.values()})
    streak = 0
    if active_dates and (today - active_dates[-1]).days <= STREAK_GRACE_DAYS:
        streak = 1
        cursor = active_dates[-1]
        for day in reversed(active_dates[:-1]):
            if (cursor - day).days == 1:
                streak += 1
                cursor = day
            else:
                break
    return {
        "active_dates": active_dates,
        "streak_days": streak,
        "last_active_date": active_dates[-1] if active_dates else None,
        "solved_problem_ids": sorted(first_solve),
    }


def compute_student_snapshot(db: Session, *, student_id: int,
                             today: date_cls | None = None) -> dict:
    """一次算全：两个榜的指标 + 打卡状态（重算与终态结算共用同一实现）。"""
    rows = counted_rows(db, student_id=student_id)
    snapshot = {board: board_metrics(rows, board) for board in BOARD_KINDS}
    snapshot["boards"] = {board: snapshot[board] for board in BOARD_KINDS}
    snapshot["activity"] = activity_state(rows, today=today)
    snapshot["counted_submissions"] = len(rows)
    return snapshot


def _write_scope_rows(db: Session, *, student_id: int, scope_kind: str, scope_id: int,
                      snapshot: dict) -> int:
    activity = snapshot["activity"]
    written = 0
    for board in BOARD_KINDS:
        metrics = snapshot[board]
        stat = db.scalar(
            select(PracticeStudentStat).where(
                PracticeStudentStat.student_id == student_id,
                PracticeStudentStat.scope_kind == scope_kind,
                PracticeStudentStat.scope_id == scope_id,
                PracticeStudentStat.board_kind == board,
            )
        )
        if stat is None:
            stat = PracticeStudentStat(student_id=student_id, scope_kind=scope_kind,
                                       scope_id=scope_id, board_kind=board)
            db.add(stat)
        stat.solved_count = metrics["solved_count"]
        stat.total_score = metrics["total_score"]
        stat.avg_score = metrics["avg_score"]
        stat.accuracy = metrics["accuracy"]
        stat.achieved_at = metrics["achieved_at"]
        stat.streak_days = activity["streak_days"]
        stat.last_active_date = activity["last_active_date"]
        written += 1
    return written


def recompute_student_stats(db: Session, *, student_id: int, commit: bool = False,
                            today: date_cls | None = None) -> dict:
    """重算单个学生的统计行（platform + 每个在读教学班）。

    ``commit=False`` 时只 flush、留在调用方事务里——终态写入的激励结算依赖这一点
    （徽章与提交终态同生共死）。
    """
    snapshot = compute_student_snapshot(db, student_id=student_id, today=today)
    platform_rows = _write_scope_rows(db, student_id=student_id, scope_kind="platform",
                                      scope_id=PLATFORM_SCOPE_ID, snapshot=snapshot)
    offering_rows = 0
    for offering_id in active_offering_ids(db, student_id):
        offering_rows += _write_scope_rows(db, student_id=student_id,
                                           scope_kind="offering", scope_id=offering_id,
                                           snapshot=snapshot)
    if commit:
        db.commit()
    return {
        "student_id": student_id,
        "rows": platform_rows + offering_rows,
        "platform_rows": platform_rows,
        "offering_rows": offering_rows,
        "solved_count": snapshot["deterministic"]["solved_count"],
        "streak_days": snapshot["activity"]["streak_days"],
        "last_active_date": snapshot["activity"]["last_active_date"],
        "active_dates": snapshot["activity"]["active_dates"],
    }


def _known_student_ids(db: Session) -> list[int]:
    """重算范围：有过练习提交的学生 + 已有统计行的学生（后者覆盖改题后清零）。"""
    submitted = set(int(value) for value in db.scalars(
        select(PracticeSubmission.student_id).distinct()
    ).all())
    tracked = set(int(value) for value in db.scalars(
        select(PracticeStudentStat.student_id).distinct()
    ).all())
    return sorted(submitted | tracked)


def recompute_all(db: Session, *, commit: bool = True,
                  today: date_cls | None = None) -> dict:
    """全量重算（定时任务入口）：幂等——跑两次得到完全一致的统计行内容。"""
    student_ids = _known_student_ids(db)
    platform_rows = offering_rows = 0
    for student_id in student_ids:
        result = recompute_student_stats(db, student_id=student_id, commit=False,
                                         today=today)
        platform_rows += result["platform_rows"]
        offering_rows += result["offering_rows"]
    if commit:
        db.commit()
    return {
        "students": len(student_ids),
        "platform_rows": platform_rows,
        "offering_rows": offering_rows,
    }


def stats_snapshot(db: Session, *, student_id: int) -> dict:
    """已落库的快照（请求路径读这个，不重新聚合全表）。"""
    rows = db.scalars(
        select(PracticeStudentStat).where(PracticeStudentStat.student_id == student_id)
        .order_by(PracticeStudentStat.scope_kind, PracticeStudentStat.scope_id,
                  PracticeStudentStat.board_kind)
    ).all()
    payload: dict = {"platform": {}, "offerings": {}, "recalculated_at": None}
    latest: datetime | None = None
    for row in rows:
        if row.scope_kind == "platform":
            payload["platform"][row.board_kind] = {
                "solved_count": int(row.solved_count or 0),
                "total_score": float(row.total_score or 0.0),
                "avg_score": row.avg_score,
                "accuracy": row.accuracy,
                "achieved_at": row.achieved_at,
                "streak_days": int(row.streak_days or 0),
                "last_active_date": row.last_active_date,
            }
        else:
            payload["offerings"].setdefault(str(row.scope_id), {})[row.board_kind] = {
                "solved_count": int(row.solved_count or 0),
                "total_score": float(row.total_score or 0.0),
            }
        updated = _as_utc(row.updated_at)
        if updated is not None and (latest is None or updated > latest):
            latest = updated
    payload["recalculated_at"] = latest
    return payload


def growth_series(db: Session, *, student_id: int, days: int = GROWTH_DAYS,
                  today: date_cls | None = None) -> list[dict]:
    """近 N 天成长曲线：每天的新增通过题数与当天计入提交的平均分。

    只查**单个学生**自己的提交（与 ``/practice/me`` 同一量级），不是全表聚合；
    窄屏由前端降级为列表展示。
    """
    days = max(1, min(int(days), 90))
    today = today or _today()
    start = today - timedelta(days=days - 1)
    rows = counted_rows(db, student_id=student_id)
    series = {
        start + timedelta(days=offset): {
            "date": (start + timedelta(days=offset)).isoformat(),
            "passed_count": 0, "submissions": 0, "avg_score": None,
        }
        for offset in range(days)
    }
    first_solve: dict[int, date_cls] = {}
    score_sum = {key: 0.0 for key in series}
    score_count = {key: 0 for key in series}
    for row in rows:
        finished = row["finished_at"]
        if finished is None:
            continue
        day = finished.date()
        if day in series:
            series[day]["submissions"] += 1
            score_sum[day] += row["score"]
            score_count[day] += 1
        if row["score"] >= practice_service.PRACTICE_SOLVED_SCORE:
            problem_id = row["problem_id"]
            if problem_id not in first_solve or day < first_solve[problem_id]:
                first_solve[problem_id] = day
    for day in first_solve.values():
        if day in series:
            series[day]["passed_count"] += 1
    for day, bucket in series.items():
        bucket["avg_score"] = (round(score_sum[day] / score_count[day], 4)
                               if score_count[day] else None)
    return [series[start + timedelta(days=offset)] for offset in range(days)]


# ─────────────────────────────────────────────────────────────
# 全平台榜 opt-in（默认不公开，学生主动加入）
# ─────────────────────────────────────────────────────────────

def opt_in_state(db: Session, student_id: int) -> dict:
    row = db.scalar(
        select(PracticeBoardPreference).where(
            PracticeBoardPreference.student_id == student_id)
    )
    if row is None:
        return {"opt_in": False, "decided_at": None}
    return {
        "opt_in": bool(row.platform_opt_in),
        "decided_at": _as_utc(row.decided_at),
    }


def set_opt_in(db: Session, *, student_id: int, opt_in: bool) -> dict:
    row = db.scalar(
        select(PracticeBoardPreference).where(
            PracticeBoardPreference.student_id == student_id)
    )
    if row is None:
        row = PracticeBoardPreference(student_id=student_id)
        db.add(row)
    row.platform_opt_in = bool(opt_in)
    row.decided_at = utcnow()
    db.commit()
    db.refresh(row)
    return opt_in_state(db, student_id)
