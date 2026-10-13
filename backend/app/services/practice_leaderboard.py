"""练习榜单读取（批次 6）：班内榜 / 全平台榜 × 普通测试榜 / AI 综合榜。

三条硬规则：

1. **只读快照**：榜单读 ``practice_student_stats``（由定时重算维护），
   请求路径不实时聚合全表；重算间隔见 ``settings.practice_stats_recalc_seconds``。
2. **排序确定性**：总分 DESC → 通过题数 DESC → 达成时间 ASC → ``student_id``
   （稳定标识，绝不用随机数/时间戳）；并列用 competition-style rank
   （先例 ``services/lab_leaderboard.py:52-57``）。
3. **匿名与 opt-in**：全平台榜只展示 HMAC 化名（同一学生稳定、不同学生不同、
   不可逆推学号），且只收录**主动加入**的学生；退出后立即从全平台榜消失，
   班内榜不受影响。班内榜展示姓名，但同样不返回学号/源码/他人评分明细。

用 Core 表投影而非 ORM 实体查询：与 ``lab_leaderboard`` 同一做法，显式声明
「这里读的是聚合快照，不经过任何行级 scope 策略」。
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import User
from app.models.practice import (
    PracticeBoardPreference, PracticeStudentStat,
)
from app.models.teaching import OfferingEnrollment
from app.services import practice_service

# 每个榜最多返回的名次（与批次 5 的既有接口保持一致）
LEADERBOARD_LIMIT = practice_service.LEADERBOARD_LIMIT
# 「只看我的名次」视图：自己前后各 3 名
NEIGHBOR_WINDOW = 3


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def alias(settings, *, scope_kind: str, scope_id: int, student_id: int) -> str:
    """HMAC 化名：与批次 5 的 ``practice_service._alias`` 同一盐值与截断规则，
    保证同一个学生在旧接口和新榜单页看到的是同一个化名。"""
    digest = hmac.new(
        settings.secret_key.encode(),
        f"practice:{scope_kind}:{scope_id or 0}:{student_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"同学 {digest[:6].upper()}"


def _snapshot_rows(db: Session, *, board: str, scope_kind: str,
                   scope_id: int) -> list[dict]:
    """读统计快照；全平台榜只保留主动加入者，班内榜按在读名单取交集。"""
    stat = PracticeStudentStat.__table__
    base_columns = (
        stat.c.student_id, stat.c.solved_count, stat.c.total_score,
        stat.c.avg_score, stat.c.achieved_at, stat.c.updated_at,
    )
    has_activity = stat.c.avg_score.is_not(None)
    if scope_kind == "platform":
        prefs = PracticeBoardPreference.__table__
        query = (
            select(*base_columns)
            .select_from(stat.outerjoin(
                prefs, prefs.c.student_id == stat.c.student_id))
            .where(
                stat.c.scope_kind == "platform",
                stat.c.scope_id == 0,
                stat.c.board_kind == board,
                has_activity,
                prefs.c.platform_opt_in.is_(True),
            )
        )
        return [dict(row) for row in db.execute(query).mappings().all()]
    users = User.__table__
    enroll = OfferingEnrollment.__table__
    query = (
        select(*base_columns, users.c.real_name.label("real_name"))
        .select_from(stat.join(
            enroll,
            and_(enroll.c.student_id == stat.c.student_id,
                 enroll.c.offering_id == scope_id,
                 enroll.c.status == "active"),
        ).join(users, users.c.id == stat.c.student_id))
        .where(
            stat.c.scope_kind == "offering",
            stat.c.scope_id == scope_id,
            stat.c.board_kind == board,
            has_activity,
        )
    )
    return [dict(row) for row in db.execute(query).mappings().all()]


def _ranked(rows: list[dict], *, settings, scope_kind: str, scope_id: int,
            viewer_id: int | None) -> list[dict]:
    """排序 + competition rank + 化名，输出前端可见的公开投影。"""
    def sort_key(row: dict):
        achieved = _as_utc(row["achieved_at"])
        return (
            -float(row["total_score"] or 0.0),
            -int(row["solved_count"] or 0),
            achieved or datetime.max.replace(tzinfo=timezone.utc),
            int(row["student_id"]),
        )

    ordered = sorted(rows, key=sort_key)
    items: list[dict] = []
    rank = 0
    previous: tuple | None = None
    for index, row in enumerate(ordered, 1):
        achieved = _as_utc(row["achieved_at"])
        ticket = (round(float(row["total_score"] or 0.0), 6),
                  int(row["solved_count"] or 0),
                  achieved.isoformat() if achieved else None)
        if ticket != previous:
            rank = index
            previous = ticket
        student_id = int(row["student_id"])
        if scope_kind == "platform":
            display = alias(settings, scope_kind=scope_kind, scope_id=scope_id,
                            student_id=student_id)
        else:
            display = row.get("real_name") or alias(
                settings, scope_kind=scope_kind, scope_id=scope_id,
                student_id=student_id)
        items.append({
            "rank": rank,
            "display_name": display,
            "solved_count": int(row["solved_count"] or 0),
            "total_score": round(float(row["total_score"] or 0.0), 4),
            "avg_score": (round(float(row["avg_score"]), 4)
                          if row["avg_score"] is not None else None),
            "achieved_at": achieved,
            "is_me": viewer_id is not None and student_id == viewer_id,
        })
    return items


def board(db: Session, *, board_kind: str, scope_kind: str, scope_id: int,
          viewer, settings, view: str = "top") -> dict:
    """榜单主入口：返回公开投影 + 我的名次 + 「自己 ±3」窗口。"""
    rows = _snapshot_rows(db, board=board_kind, scope_kind=scope_kind,
                          scope_id=scope_id)
    viewer_id = int(viewer.id) if getattr(viewer, "role", None) == "student" else None
    items = _ranked(rows, settings=settings, scope_kind=scope_kind,
                    scope_id=scope_id, viewer_id=viewer_id)
    total = len(items)
    me = next((item for item in items if item["is_me"]), None)
    window = None
    if view == "me" and me is not None:
        index = items.index(me)
        start = max(0, index - NEIGHBOR_WINDOW)
        window = items[start:index + NEIGHBOR_WINDOW + 1]
    recalculated_at = None
    for row in rows:
        updated = _as_utc(row.get("updated_at"))
        if updated is not None and (recalculated_at is None or updated > recalculated_at):
            recalculated_at = updated
    opt_in = None
    if scope_kind == "platform" and viewer_id is not None:
        from app.services import practice_stats_service

        opt_in = practice_stats_service.opt_in_state(db, viewer_id)["opt_in"]

    def _public(item: dict) -> dict:
        achieved = item["achieved_at"]
        return {
            "rank": item["rank"],
            "display_name": item["display_name"],
            "solved_count": item["solved_count"],
            "total_score": item["total_score"],
            "avg_score": item["avg_score"],
            "achieved_at": achieved.isoformat() if achieved else None,
            "is_me": item["is_me"],
        }

    return {
        "board": board_kind,
        "scope": scope_kind,
        "scope_id": scope_id,
        "total": total,
        "truncated": total > LEADERBOARD_LIMIT,
        "anonymized": scope_kind == "platform",
        "opt_in": opt_in,
        "items": [_public(item) for item in items[:LEADERBOARD_LIMIT]],
        "me": _public(me) if me is not None else None,
        "window": [_public(item) for item in window] if window is not None else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "recalculated_at": (recalculated_at.isoformat()
                            if recalculated_at is not None else None),
    }
