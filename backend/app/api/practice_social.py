"""练习榜单与个人统计 API（批次 6）。

端点划分：

* ``GET /practice/social/leaderboard``：班内榜/全平台榜 × 普通测试榜/AI 综合榜。
  三种角色都可读（教师只读任课班级 + 全平台），返回的只有聚合与化名。
* ``GET /practice/social/me/overview``：学生个人统计、打卡日历、徽章墙、成长曲线。
* ``GET|PUT /practice/social/me/opt-in``：全平台榜主动加入/退出（学生专属；
  教师与管理员调用一律 403——练习写权限只属于学生本人，教师只读）。
* ``POST /practice/social/recalculate``：管理员手动触发统计重算（幂等）。

隐私：全平台榜只收录 opt-in 学生并以 HMAC 化名展示；任何端点都不返回学号、
姓名（班内榜除外）、提交源码或他人评分明细。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.practice import _offering_scope
from app.config import Settings, get_settings
from app.dependencies import get_db, require_roles
from app.models import User
from app.models.practice import PracticeUsageLog
from app.schemas.practice_social import PracticeOptInUpdate
from app.services import (
    practice_badge_service, practice_leaderboard, practice_queue,
    practice_recalc_job, practice_service, practice_stats_service,
)

router = APIRouter(prefix="/practice/social", tags=["练习榜单与统计"])

GROWTH_WINDOW_DAYS = 30


def _board_meta(db: Session, *, viewer: User) -> dict:
    """榜单附带元信息：判题队列积压与额度状态（UI 据此提示「排队中」「额度已用完」）。

    学生看自己的每日提交额度；教师/管理员看平台 AI 预算——两者都只做提示，
    绝不把未完成判题的提交计入榜单。
    """
    settings_row = practice_service.settings_row(db)
    today = datetime.now(timezone.utc).date()
    if viewer.role == "student":
        used = float(db.scalar(
            select(func.coalesce(func.sum(PracticeUsageLog.amount), 0.0)).where(
                PracticeUsageLog.date == today,
                PracticeUsageLog.metric == "submissions",
                PracticeUsageLog.scope == "student",
                PracticeUsageLog.student_id == viewer.id,
            )
        ) or 0.0)
        limit = int(settings_row.daily_submission_cap_per_student or 0)
        exhausted = bool(limit > 0 and used >= limit)
    else:
        used = float(db.scalar(
            select(func.coalesce(func.sum(PracticeUsageLog.amount), 0.0)).where(
                PracticeUsageLog.date == today,
                PracticeUsageLog.metric == "ai_tokens",
                PracticeUsageLog.scope == "platform",
            )
        ) or 0.0)
        limit = int(settings_row.daily_ai_budget_tokens or 0)
        exhausted = bool(limit > 0 and used >= limit)
    return {
        "quota_exhausted": exhausted,
        "queue": {
            "queued": practice_queue.queued_count(db),
            "running": practice_queue.running_count(db),
            "max_concurrent": int(settings_row.max_concurrent_practice_jobs or 1),
        },
    }


@router.get("/leaderboard")
def leaderboard(
    scope: str = Query(default="platform", pattern="^(platform|offering)$"),
    scope_id: int | None = Query(default=None),
    board: str = Query(default="deterministic", pattern="^(deterministic|ai)$"),
    view: str = Query(default="top", pattern="^(top|me)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student", "teacher", "admin")),
    settings: Settings = Depends(get_settings),
):
    """只读榜单。班内榜的授权（任课教师 / 在读学生）与批次 5 同一实现。"""
    _student_ids, resolved_scope = _offering_scope(
        db, scope_kind=scope, scope_id=scope_id, viewer=current_user)
    payload = practice_leaderboard.board(
        db, board_kind=board, scope_kind=scope, scope_id=resolved_scope,
        viewer=current_user, settings=settings, view=view,
    )
    payload["meta"] = _board_meta(db, viewer=current_user)
    return payload


@router.get("/me/overview")
def my_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
    settings: Settings = Depends(get_settings),
):
    """学生个人面板：统计卡 + 打卡日历 + 徽章墙 + 近 30 天成长曲线。"""
    settings_row = practice_service.settings_row(db)
    summary = practice_service.student_summary(
        db, student_id=current_user.id, settings=settings_row)
    snapshot = practice_stats_service.stats_snapshot(db, student_id=current_user.id)
    platform = snapshot["platform"]
    if not platform:
        # 还没有统计快照（例如从未触发过重算）：按同一口径即时算一次**本人**的数据，
        # 不写库、不聚合全表；下一次重算会补齐快照。
        computed = practice_stats_service.compute_student_snapshot(
            db, student_id=current_user.id)
        platform = {
            board: {
                "solved_count": computed[board]["solved_count"],
                "total_score": computed[board]["total_score"],
                "avg_score": computed[board]["avg_score"],
                "accuracy": computed[board]["accuracy"],
                "achieved_at": computed[board]["achieved_at"],
                "streak_days": computed["activity"]["streak_days"],
                "last_active_date": computed["activity"]["last_active_date"],
            }
            for board in practice_stats_service.BOARD_KINDS
        }
    activity = practice_stats_service.activity_state(
        practice_stats_service.counted_rows(db, student_id=current_user.id))
    deterministic = platform.get("deterministic") or {}
    streak_days = int(deterministic.get("streak_days") or activity["streak_days"] or 0)
    summary = {
        **summary,
        # 统计卡与徽章用同一口径的连续天数（快照优先，快照缺失时用即时值）
        "streak_days": streak_days,
        "last_active_date": (
            deterministic.get("last_active_date") or activity["last_active_date"]
        ),
    }
    return {
        "summary": summary,
        "boards": {
            board: {
                "solved_count": int((platform.get(board) or {}).get("solved_count") or 0),
                "total_score": float((platform.get(board) or {}).get("total_score") or 0.0),
                "avg_score": (platform.get(board) or {}).get("avg_score"),
                "accuracy": (platform.get(board) or {}).get("accuracy"),
            }
            for board in practice_stats_service.BOARD_KINDS
        },
        "badges": practice_badge_service.badge_wall(
            db, student_id=current_user.id,
            solved_count=int(deterministic.get("solved_count")
                             or summary.get("solved_count") or 0),
            streak_days=streak_days,
        ),
        "checkin": {
            "days": GROWTH_WINDOW_DAYS,
            "active_dates": [day.isoformat() for day in activity["active_dates"]],
            "streak_days": streak_days,
            "last_active_date": (activity["last_active_date"].isoformat()
                                 if activity["last_active_date"] else None),
        },
        "growth": [
            {**point, "date": point["date"]}
            for point in practice_stats_service.growth_series(
                db, student_id=current_user.id, days=GROWTH_WINDOW_DAYS)
        ],
        "opt_in": practice_stats_service.opt_in_state(db, current_user.id)["opt_in"],
        "recalculated_at": (snapshot["recalculated_at"].isoformat()
                            if snapshot["recalculated_at"] else None),
    }


@router.get("/me/opt-in")
def get_my_opt_in(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
):
    return practice_stats_service.opt_in_state(db, current_user.id)


@router.put("/me/opt-in")
def update_my_opt_in(
    data: PracticeOptInUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("student")),
):
    """加入/退出全平台榜：退出后立即从全平台榜消失，班内榜不受影响。"""
    return practice_stats_service.set_opt_in(
        db, student_id=current_user.id, opt_in=data.opt_in)


@router.post("/recalculate", status_code=202)
def recalculate(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    """手动全量重算（管理员）：幂等——连续两次跑出的统计行内容一致。"""
    return practice_recalc_job.run_recalc(
        db, owner_id=practice_recalc_job.process_owner_id(), force=True)
