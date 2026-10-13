"""练习统计重算任务（批次 6）：多实例互斥 + 幂等可重入。

* 由 ``main.py`` 的后台协程按 ``settings.practice_stats_recalc_seconds`` 调用；
  多副本下用 ``scheduler_lease`` 的 ``practice-stats-recalc`` 租约保证同一时刻
  只有一个实例真正重算（先例 ``main.py`` 的 ``exam-expiry`` / ``kernel-cleanup``）。
* 重算是幂等的：``recompute_all`` 按 ``(student_id, scope_kind, scope_id,
  board_kind)`` 就地更新，跑两次得到完全一致的统计行内容。
* Redis 不参与：重算的权威状态全在 MySQL，租约也在库内。

改题/归档会在管理端请求内做一次**定向**重算（只覆盖该题的提交者），不在请求
路径上聚合全表——这是为了让「改题后旧成绩立刻退出当前榜」不必等下一个周期。
"""
from __future__ import annotations

import logging
import os
import socket
import time
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.practice import PracticeSubmission
from app.services import practice_stats_service
from app.services.scheduler_lease import try_acquire_lease

logger = logging.getLogger("dai.practice")

RECALC_TASK_NAME = "practice-stats-recalc"
# 租约要覆盖一次全量重算；崩溃后自动释放，不会永久阻塞后续周期
RECALC_LEASE_SECONDS = 240


def process_owner_id() -> str:
    """owner 每进程唯一（hostname + pid + 随机实例号），同机多进程不会共享租约。"""
    return f"api:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def run_recalc(db: Session, *, owner_id: str, ttl_seconds: int = RECALC_LEASE_SECONDS,
               force: bool = False) -> dict:
    """一次重算；拿不到租约就跳过（返回 skipped=True，绝不重复劳动）。"""
    started = time.monotonic()
    if not force and not try_acquire_lease(db, RECALC_TASK_NAME, owner_id, ttl_seconds):
        return {"skipped": True, "owner_id": owner_id, "duration_ms": 0}
    counts = practice_stats_service.recompute_all(db, commit=True)
    counts.update({
        "skipped": False,
        "owner_id": owner_id,
        "duration_ms": int((time.monotonic() - started) * 1000),
    })
    return counts


def recalc_after_problem_change(db: Session, *, problem_id: int) -> dict:
    """改题/归档后的定向重算：只覆盖对该题提交过的学生。"""
    student_ids = sorted(set(int(value) for value in db.scalars(
        select(PracticeSubmission.student_id)
        .where(PracticeSubmission.problem_id == problem_id).distinct()
    ).all()))
    recomputed = 0
    for student_id in student_ids:
        practice_stats_service.recompute_student_stats(db, student_id=student_id,
                                                       commit=False)
        recomputed += 1
    db.commit()
    return {"problem_id": problem_id, "students": recomputed}
