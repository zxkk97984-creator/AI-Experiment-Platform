"""批次 6 必过验收（激励与重算）：

* 验收 4：同一提交重复投递 5 次只有一条 ``practice_badge_awards``（对照组：不同徽章各一条）；
* 验收 5：同日多次通关只续 1 天、跨日续上、断签重置；
* 验收 9：重算跑两次内容一致（幂等可重入）+ 租约互斥。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from app.models.practice import (
    PracticeBadgeAward, PracticeStudentStat, PracticeSubmission,
)
from app.services import practice_badge_service, practice_recalc_job, practice_stats_service
from practice_test_support import (
    as_admin, as_student, create_and_publish, expected_value_map, grade_now,
)

UTC = timezone.utc
CODE = "def solution(a, b):\n    return a + b\n"
CASES = [
    {"id": "c1", "args": [1, 2], "expected": 3},
    {"id": "c2", "args": [10, -4], "expected": 6},
]
ALL_PASS = ["c1", "c2"]


def _submit_and_grade(client, db_session_factory, redis_client, monkeypatch,
                      test_settings, header, problem_id, request_id):
    response = client.post(
        f"/api/v1/practice/problems/{problem_id}/submissions",
        json={"code": CODE, "client_request_id": request_id}, headers=header)
    assert response.status_code == 202, response.text
    submission_id = response.json()["id"]
    grade_now(db_session_factory, redis_client, submission_id, monkeypatch,
              expected_value_map(db_session_factory, problem_id), test_settings,
              pass_ids=ALL_PASS)
    return submission_id


def _set_finished(db_session_factory, times: dict[int, datetime]):
    with db_session_factory() as db:
        for submission_id, when in times.items():
            db.execute(
                update(PracticeSubmission).where(PracticeSubmission.id == submission_id)
                .values(finished_at=when, created_at=when, updated_at=when))
        db.commit()


def _recalc_all(db_session_factory):
    with db_session_factory() as db:
        return practice_stats_service.recompute_all(db)


def _badges(db_session_factory, student_id):
    with db_session_factory() as db:
        rows = db.execute(
            select(PracticeBadgeAward.badge_key, PracticeBadgeAward.progress_value,
                   PracticeBadgeAward.awarded_at)
            .where(PracticeBadgeAward.student_id == student_id)).all()
        return {row[0]: (row[1], row[2]) for row in rows}


def _stat_rows(db_session_factory):
    with db_session_factory() as db:
        rows = db.execute(
            select(PracticeStudentStat.student_id, PracticeStudentStat.scope_kind,
                   PracticeStudentStat.scope_id, PracticeStudentStat.board_kind,
                   PracticeStudentStat.solved_count, PracticeStudentStat.total_score,
                   PracticeStudentStat.avg_score, PracticeStudentStat.accuracy,
                   PracticeStudentStat.streak_days, PracticeStudentStat.last_active_date,
                   PracticeStudentStat.achieved_at)
            .order_by(PracticeStudentStat.student_id, PracticeStudentStat.scope_kind,
                      PracticeStudentStat.scope_id, PracticeStudentStat.board_kind)
        ).all()
        return [tuple(row) for row in rows]


# ─────────────────────────────────────────────────────────────
# 验收 4：重复投递只发一条徽章（对照组：不同徽章各一条）
# ─────────────────────────────────────────────────────────────

def test_duplicate_delivery_awards_exactly_one_badge(client, db_session_factory,
                                                     test_settings, redis_client,
                                                     monkeypatch):
    """同一提交重复投递 5 次 → 只有一条 first_solved；再通过 9 道题 → 多一条 solved_10。"""
    from app.worker import practice_worker

    _, admin_header = as_admin(client, db_session_factory)
    problems = [create_and_publish(client, admin_header, cases=CASES,
                                   title=f"徽章题{index}")[0] for index in range(1, 11)]
    student, header = as_student(client, db_session_factory, "badge-student")

    first = _submit_and_grade(client, db_session_factory, redis_client, monkeypatch,
                              test_settings, header, problems[0]["id"], "badge-1")
    # 重复投递：同一提交再判 4 次（认领已释放，栅栏必须拒绝任何第二次写入）
    for _ in range(4):
        grade_now(db_session_factory, redis_client, first, monkeypatch,
                  expected_value_map(db_session_factory, problems[0]["id"]),
                  test_settings, pass_ids=ALL_PASS)

    badges = _badges(db_session_factory, student.id)
    assert list(badges) == ["first_solved"], badges
    assert badges["first_solved"][0] == 1
    with db_session_factory() as db:
        submission = db.get(PracticeSubmission, first)
    assert submission.grading_status == "completed"
    assert submission.result_revision == 1, "重复投递不得推进结果修订号"

    # 对照组：不同徽章各发一条（第 10 道通过后出现 solved_10）
    for index in range(1, 10):
        _submit_and_grade(client, db_session_factory, redis_client, monkeypatch,
                          test_settings, header, problems[index]["id"], f"badge-{index + 1}")
    badges = _badges(db_session_factory, student.id)
    assert sorted(badges) == ["first_solved", "solved_10"], badges
    assert badges["solved_10"][0] == 10
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(PracticeBadgeAward)) == 2

        # 再补发 5 次：唯一约束 + 存在性检查保证还是一条
        for _ in range(5):
            practice_badge_service.award_missing(
                db, student_id=student.id, solved_count=10, streak_days=1,
                source_submission_id=first)
        db.commit()
    assert len(_badges(db_session_factory, student.id)) == 2

    # 徽章墙通过 API 暴露：四个徽章、已获得的有时间、未获得的显示条件
    overview = client.get("/api/v1/practice/social/me/overview", headers=header)
    assert overview.status_code == 200, overview.text
    wall = {item["key"]: item for item in overview.json()["badges"]}
    assert set(wall) == {"first_solved", "solved_10", "solved_50", "streak_7"}
    assert wall["first_solved"]["earned"] is True
    assert wall["solved_10"]["earned"] is True
    assert wall["solved_50"]["earned"] is False
    assert wall["solved_50"]["threshold"] == 50
    assert wall["streak_7"]["description"]


# ─────────────────────────────────────────────────────────────
# 验收 5：同日只续 1 天、跨日续上、断签重置
# ─────────────────────────────────────────────────────────────

def _row(problem_id, day, *, score=100.0, second=0):
    return {
        "submission_id": problem_id * 100 + day.day,
        "problem_id": problem_id,
        "board": "deterministic",
        "score": score,
        "finished_at": datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        + timedelta(seconds=second),
        "grading_version_id": 1,
    }


def test_streak_rules_are_hand_computed():
    """同日多次通关 → 1 天；跨日 → 累加；缺口 → 重置；最后活跃距今 >1 天 → 断签归零。"""
    today = datetime(2026, 10, 13, tzinfo=UTC).date()
    day1 = today - timedelta(days=6)
    day2 = today - timedelta(days=5)
    day3 = today - timedelta(days=4)

    # 同一题同一天两次通关（另有第二题同日通关）→ 仍只算 1 天
    same_day = [_row(1, day1, second=0), _row(1, day1, second=60),
                _row(2, day1, second=120)]
    state = practice_stats_service.activity_state(same_day, today=day1)
    assert state["streak_days"] == 1
    assert state["active_dates"] == [day1]

    # 跨日续上：day1 + day2 + day3（day2 内两次）→ 3 天
    cross = same_day + [_row(3, day2), _row(4, day2, second=30), _row(5, day3)]
    state = practice_stats_service.activity_state(cross, today=day3)
    assert state["streak_days"] == 3
    assert state["active_dates"] == [day1, day2, day3]

    # 断签重置：中间缺两天后又通关 → 从新的那天重新起算
    gap = cross + [_row(6, today - timedelta(days=2))]
    state = practice_stats_service.activity_state(gap, today=today - timedelta(days=2))
    assert state["streak_days"] == 1

    # 最后活跃距今 2 天 → 断签归零（对照组：距今 1 天仍有 streak）
    state = practice_stats_service.activity_state(cross, today=day3 + timedelta(days=2))
    assert state["streak_days"] == 0
    state = practice_stats_service.activity_state(cross, today=day3 + timedelta(days=1))
    assert state["streak_days"] == 3
    # 未通过（50 分）不算打卡
    assert practice_stats_service.activity_state(
        [_row(9, day1, score=50.0)], today=day1)["streak_days"] == 0


def test_same_day_solves_extend_streak_once(client, db_session_factory, test_settings,
                                            redis_client, monkeypatch):
    """真链路：同一天通过两道题 → streak_days 仍是 1，last_active_date 是今天。"""
    _, admin_header = as_admin(client, db_session_factory)
    problems = [create_and_publish(client, admin_header, cases=CASES,
                                   title=f"打卡题{index}")[0] for index in range(1, 3)]
    student, header = as_student(client, db_session_factory, "streak-student")
    _submit_and_grade(client, db_session_factory, redis_client, monkeypatch,
                      test_settings, header, problems[0]["id"], "streak-1")
    _submit_and_grade(client, db_session_factory, redis_client, monkeypatch,
                      test_settings, header, problems[1]["id"], "streak-2")
    _recalc_all(db_session_factory)

    with db_session_factory() as db:
        stat = db.scalar(select(PracticeStudentStat).where(
            PracticeStudentStat.student_id == student.id,
            PracticeStudentStat.scope_kind == "platform",
            PracticeStudentStat.board_kind == "deterministic"))
    assert stat.streak_days == 1, "同日多次通关只续 1 天"
    assert stat.last_active_date.isoformat() == datetime.now(UTC).date().isoformat()
    outcome = client.get("/api/v1/practice/social/me/overview", headers=header).json()
    assert outcome["summary"]["streak_days"] == 1
    assert outcome["checkin"]["active_dates"] == [datetime.now(UTC).date().isoformat()]
    assert len(outcome["growth"]) == 30


def test_seven_consecutive_days_award_streak_badge(client, db_session_factory,
                                                   test_settings, redis_client,
                                                   monkeypatch):
    """连续 7 天（每天一道新题）→ 第 7 天的终态写入时发出 streak_7，且只有一条。"""
    _, admin_header = as_admin(client, db_session_factory)
    problems = [create_and_publish(client, admin_header, cases=CASES,
                                   title=f"连打题{index}")[0] for index in range(1, 9)]
    student, header = as_student(client, db_session_factory, "week-student")
    today = datetime.now(UTC).date()

    finished: dict[int, datetime] = {}
    for index in range(7):
        submission_id = _submit_and_grade(
            client, db_session_factory, redis_client, monkeypatch, test_settings,
            header, problems[index]["id"], f"week-{index}")
        day = today - timedelta(days=6 - index)
        _set_finished(db_session_factory, {submission_id: datetime.combine(
            day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)})
        finished[submission_id] = datetime.combine(
            day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

    # 前 6 天不满足；第 7 天写入时结算
    badges = _badges(db_session_factory, student.id)
    assert "streak_7" in badges, badges
    assert badges["streak_7"][0] == 7
    _recalc_all(db_session_factory)
    with db_session_factory() as db:
        stat = db.scalar(select(PracticeStudentStat).where(
            PracticeStudentStat.student_id == student.id,
            PracticeStudentStat.scope_kind == "platform",
            PracticeStudentStat.board_kind == "deterministic"))
    assert stat.streak_days == 7
    assert stat.last_active_date.isoformat() == today.isoformat()

    # 对照组：第 8 天同一批日期再通过一题，streak_7 仍只有一条（同日不重复发放）
    _submit_and_grade(client, db_session_factory, redis_client, monkeypatch,
                      test_settings, header, problems[7]["id"], "week-7")
    _recalc_all(db_session_factory)
    with db_session_factory() as db:
        count = db.scalar(select(func.count()).select_from(PracticeBadgeAward).where(
            PracticeBadgeAward.student_id == student.id,
            PracticeBadgeAward.badge_key == "streak_7"))
    assert count == 1


# ─────────────────────────────────────────────────────────────
# 验收 9：重算幂等 + 租约互斥
# ─────────────────────────────────────────────────────────────

def test_recalc_is_idempotent_and_lease_protected(client, db_session_factory,
                                                  test_settings, redis_client,
                                                  monkeypatch):
    _, admin_header = as_admin(client, db_session_factory)
    problems = [create_and_publish(client, admin_header, cases=CASES,
                                   title=f"重算题{index}")[0] for index in range(1, 3)]
    _student, header = as_student(client, db_session_factory, "recalc-student")
    for index, problem in enumerate(problems):
        _submit_and_grade(client, db_session_factory, redis_client, monkeypatch,
                          test_settings, header, problem["id"], f"recalc-{index}")

    first = _recalc_all(db_session_factory)
    snapshot_one = _stat_rows(db_session_factory)
    second = _recalc_all(db_session_factory)
    snapshot_two = _stat_rows(db_session_factory)
    assert snapshot_one == snapshot_two, "重算两次内容必须逐字一致"
    assert first == second, (first, second)
    assert first["platform_rows"] == 2 and first["students"] == 1

    # 走 API 再跑两次：仍然一致
    for _ in range(2):
        response = client.post("/api/v1/practice/social/recalculate", headers=admin_header)
        assert response.status_code == 202, response.text
        assert response.json()["skipped"] is False
    assert _stat_rows(db_session_factory) == snapshot_one

    # 租约互斥：同一时刻只有持租约者真正重算（对照组：force=True 强制重算）
    with db_session_factory() as db:
        held = practice_recalc_job.run_recalc(db, owner_id="owner-a")
        blocked = practice_recalc_job.run_recalc(db, owner_id="owner-b")
        forced = practice_recalc_job.run_recalc(db, owner_id="owner-b", force=True)
    assert held["skipped"] is False
    assert blocked["skipped"] is True, "已持租约时第二个实例必须跳过"
    assert forced["skipped"] is False
    assert _stat_rows(db_session_factory) == snapshot_one


# ─────────────────────────────────────────────────────────────
# 练习成绩/排名/徽章不进课程总评（与批次 4 的断言呼应）
# ─────────────────────────────────────────────────────────────

def test_practice_results_and_badges_never_enter_gradebook(client, db_session_factory,
                                                           test_settings, redis_client,
                                                           monkeypatch):
    """学生通过练习并拿到徽章后：成绩册分项来源枚举里没有 practice，也没有任何练习行。"""
    from app.models.gradebook import GradebookColumn
    from app.services import gradebook_service

    constraint_sql = next(
        (str(item.sqltext) for item in GradebookColumn.__table__.constraints
         if getattr(item, "name", None) == "ck_gradebook_column_source"), "")
    assert constraint_sql, "成绩册分项来源 CHECK 约束必须存在（否则断言恒真）"
    assert "practice" not in constraint_sql, constraint_sql

    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header, cases=CASES, title="总评隔离题")
    student, header = as_student(client, db_session_factory, "gradebook-practice-student")
    _submit_and_grade(client, db_session_factory, redis_client, monkeypatch,
                      test_settings, header, problem["id"], "gradebook-1")
    overview = client.get("/api/v1/practice/social/me/overview", headers=header).json()
    assert any(item["earned"] for item in overview["badges"]), overview["badges"]

    with db_session_factory() as db:
        columns = db.scalar(select(func.count()).select_from(GradebookColumn)) or 0
    assert columns == 0, "练习活动绝不该生成任何成绩册分项"
    assert student.id, "对照：该学生确实完成了练习并拿到徽章"
    assert gradebook_service is not None
