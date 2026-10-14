"""批次 6 必过验收（榜单与隐私）：手算排序、并列 rank、10 次稳定性、版本隔离、
opt-out、化名不可逆推、教师只读 403、非练习域不受影响。

每条断言都带对照组（回滚实现后必须转红）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, update

from app.models import AcademicTerm, Course, TeachingClass, User
from app.models.practice import (
    PracticeBadgeAward, PracticeBoardPreference, PracticeSubmission,
)
from app.models.teaching import CourseOffering, OfferingEnrollment
from app.services import practice_badge_service, practice_leaderboard
from practice_test_support import (
    as_admin, as_student, as_teacher, create_and_publish, expected_value_map, grade_now,
)

UTC = timezone.utc
CODE = "def solution(a, b):\n    return a + b\n"
# 4 个用例 → 通过数决定分数：4/4=100、3/4=75、2/4=50、1/4=25
CASES = [
    {"id": "c1", "args": [1, 2], "expected": 3},
    {"id": "c2", "args": [10, -4], "expected": 6},
    {"id": "c3", "args": [0, 0], "expected": 0},
    {"id": "c4", "args": [-5, -7], "expected": -12},
]
PASS_IDS = {
    100: ["c1", "c2", "c3", "c4"],
    75: ["c1", "c2", "c3"],
    50: ["c1", "c2"],
    25: ["c1"],
}


# ─────────────────────────────────────────────────────────────
# 共用工具
# ─────────────────────────────────────────────────────────────

def _problems(client, admin_header, count, prefix="排序题"):
    return [
        create_and_publish(client, admin_header, cases=CASES, title=f"{prefix}{index}")[0]
        for index in range(1, count + 1)
    ]


def _submit(client, header, problem_id, request_id):
    response = client.post(
        f"/api/v1/practice/problems/{problem_id}/submissions",
        json={"code": CODE, "client_request_id": request_id}, headers=header)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def _grade(db_session_factory, redis_client, monkeypatch, test_settings,
           problem_id, submission_id, pass_ids):
    grade_now(db_session_factory, redis_client, submission_id, monkeypatch,
              expected_value_map(db_session_factory, problem_id), test_settings,
              pass_ids=pass_ids)


def _set_finished(db_session_factory, times: dict[int, datetime]):
    with db_session_factory() as db:
        for submission_id, when in times.items():
            db.execute(
                update(PracticeSubmission).where(PracticeSubmission.id == submission_id)
                .values(finished_at=when, created_at=when, updated_at=when))
        db.commit()


def _opt_in(client, header, value=True):
    response = client.put("/api/v1/practice/social/me/opt-in",
                          json={"opt_in": value}, headers=header)
    assert response.status_code == 200, response.text
    assert response.json()["opt_in"] is value
    return response.json()


def _recalc(client, admin_header):
    response = client.post("/api/v1/practice/social/recalculate", headers=admin_header)
    assert response.status_code == 202, response.text
    return response.json()


def _board(client, header, *, board="deterministic", scope="platform",
           scope_id=None, view="top"):
    url = f"/api/v1/practice/social/leaderboard?scope={scope}&board={board}&view={view}"
    if scope_id is not None:
        url += f"&scope_id={scope_id}"
    response = client.get(url, headers=header)
    assert response.status_code == 200, response.text
    return response.json()


def _make_offering(db_session_factory, *, teacher_id, student_ids, code="board"):
    with db_session_factory() as db:
        term = AcademicTerm(code=f"{code}-term", name="练习榜单学期",
                            start_date=date(2026, 9, 1), end_date=date(2027, 1, 31),
                            status="active")
        db.add(term)
        db.flush()
        course = Course(title="练习榜单课程", teacher_id=teacher_id,
                        academic_term_id=term.id, status="published")
        db.add(course)
        db.flush()
        klass = TeachingClass(code=f"{code}-class", name="练习榜单班",
                              academic_term_id=term.id, status="active")
        db.add(klass)
        db.flush()
        offering = CourseOffering(course_id=course.id, academic_term_id=term.id,
                                  teaching_class_id=klass.id, teacher_id=teacher_id,
                                  status="active")
        db.add(offering)
        db.flush()
        for student_id in student_ids:
            db.add(OfferingEnrollment(
                offering_id=offering.id, student_id=student_id, status="active",
                active_scope_key=f"{course.id}:{term.id}:{student_id}"))
        db.commit()
        return offering.id


# ─────────────────────────────────────────────────────────────
# 验收 1：5 学生 × 4 题 × 多次提交，手算期望排序
# ─────────────────────────────────────────────────────────────

def test_hand_computed_ranking_with_best_score_only(client, db_session_factory,
                                                    test_settings, redis_client,
                                                    monkeypatch):
    """手算期望：S2 400/4 > S3 350/2（10:00）> S4 350/2（11:00）> S1 250/1 > S5 200/0。

    低分在后（S1 的第 2 题、S3 的第 3 题先交高分再交低分）不得覆盖历史最高分。
    """
    _, admin_header = as_admin(client, db_session_factory)
    problems = _problems(client, admin_header, 4)
    students = [as_student(client, db_session_factory, f"board-s{index}")
                for index in range(1, 6)]

    # (学生, 题) -> 期望最高分；order 里的第二个分是「后交的低分」，绝不该被计入
    plan = {
        0: {"scores": [100, 75, 50, 25], "extra_low": {1: 50},
            "base": datetime(2026, 10, 13, 8, 0, tzinfo=UTC)},
        1: {"scores": [100, 100, 100, 100], "extra_low": {},
            "base": datetime(2026, 10, 13, 9, 0, tzinfo=UTC)},
        2: {"scores": [100, 100, 75, 75], "extra_low": {3: 50},
            "base": datetime(2026, 10, 13, 10, 0, tzinfo=UTC)},
        3: {"scores": [100, 100, 75, 75], "extra_low": {},
            "base": datetime(2026, 10, 13, 11, 0, tzinfo=UTC)},
        4: {"scores": [50, 50, 50, 50], "extra_low": {},
            "base": datetime(2026, 10, 13, 12, 0, tzinfo=UTC)},
    }
    finished: dict[int, datetime] = {}
    for index, (user, header) in enumerate(students):
        entry = plan[index]
        submitted = 0
        for problem_index, score in enumerate(entry["scores"]):
            problem = problems[problem_index]
            submitted += 1
            submission_id = _submit(client, header, problem["id"],
                                    f"s{index}-p{problem_index}-hi")
            _grade(db_session_factory, redis_client, monkeypatch, test_settings,
                   problem["id"], submission_id, PASS_IDS[score])
            finished[submission_id] = entry["base"] + timedelta(minutes=submitted)
            if problem_index in entry["extra_low"]:
                submitted += 1
                low_id = _submit(client, header, problem["id"],
                                 f"s{index}-p{problem_index}-lo")
                _grade(db_session_factory, redis_client, monkeypatch, test_settings,
                       problem["id"], low_id, PASS_IDS[entry["extra_low"][problem_index]])
                # 低分**在后**：完成时间更晚
                finished[low_id] = entry["base"] + timedelta(minutes=submitted)
    _set_finished(db_session_factory, finished)
    for _user, header in students:
        _opt_in(client, header)

    result = _recalc(client, admin_header)
    assert result["students"] == 5, result

    board = _board(client, students[0][1])
    aliases = {
        index: practice_leaderboard.alias(test_settings, scope_kind="platform",
                                          scope_id=0, student_id=user.id)
        for index, (user, _header) in enumerate(students)
    }
    expected_order = [1, 2, 3, 0, 4]  # S2, S3, S4, S1, S5
    assert [row["display_name"] for row in board["items"]] == \
        [aliases[index] for index in expected_order]
    assert [row["rank"] for row in board["items"]] == [1, 2, 3, 4, 5]
    assert [row["total_score"] for row in board["items"]] == [400.0, 350.0, 350.0, 250.0, 200.0]
    # 通过线是 60：75 也算通过，所以 S2/S3 各 4 题、S1 2 题、S5 0 题
    assert [row["solved_count"] for row in board["items"]] == [4, 4, 4, 2, 0]
    assert board["total"] == 5

    # S1 的历史里保留全部 5 条提交（含后交的低分），但最高分仍是 75
    mine = client.get("/api/v1/practice/me", headers=students[0][1]).json()
    assert mine["total"] == 5
    assert mine["summary"]["total_score"] == 250.0
    assert mine["summary"]["solved_count"] == 2  # 100 与 75 都过线
    # 两榜隔离：AI 榜里没有任何普通判题成绩
    assert _board(client, students[0][1], board="ai")["items"] == []


# ─────────────────────────────────────────────────────────────
# 验收 2：并列 rank 相同 + 跑 10 次顺序一致
# ─────────────────────────────────────────────────────────────

def test_tied_ranks_and_ten_run_stability(client, db_session_factory, test_settings,
                                          redis_client, monkeypatch):
    """两个学生总分/通过题数/达成时间完全相同 → competition rank 并列（1,1,3），
    且连续 10 次请求（含再重算一次）顺序逐字相同。"""
    _, admin_header = as_admin(client, db_session_factory)
    problems = _problems(client, admin_header, 3, prefix="并列题")
    students = [as_student(client, db_session_factory, f"tie-s{index}")
                for index in range(1, 4)]
    plan = [
        {"scores": [100, 100, 75], "base": datetime(2026, 10, 13, 10, 0, tzinfo=UTC)},
        {"scores": [100, 100, 75], "base": datetime(2026, 10, 13, 10, 0, tzinfo=UTC)},
        {"scores": [100, 50, 50], "base": datetime(2026, 10, 13, 9, 0, tzinfo=UTC)},
    ]
    finished: dict[int, datetime] = {}
    for index, (user, header) in enumerate(students):
        entry = plan[index]
        for problem_index, score in enumerate(entry["scores"]):
            submission_id = _submit(client, header, problems[problem_index]["id"],
                                    f"tie{index}-p{problem_index}")
            _grade(db_session_factory, redis_client, monkeypatch, test_settings,
                   problems[problem_index]["id"], submission_id, PASS_IDS[score])
            finished[submission_id] = entry["base"] + timedelta(minutes=problem_index)
    _set_finished(db_session_factory, finished)
    for _user, header in students:
        _opt_in(client, header)
    _recalc(client, admin_header)

    first = _board(client, students[0][1])
    assert [row["rank"] for row in first["items"]] == [1, 1, 3], first["items"]
    assert [row["total_score"] for row in first["items"]] == [275.0, 275.0, 200.0]

    sequences = []
    for _ in range(10):
        items = _board(client, students[0][1])["items"]
        sequences.append([(row["rank"], row["display_name"]) for row in items])
    assert all(sequence == sequences[0] for sequence in sequences), "10 次顺序必须一致"

    # 对照组：重算再跑一次，顺序与并列关系都不变（幂等可重入）
    _recalc(client, admin_header)
    after = [(row["rank"], row["display_name"])
             for row in _board(client, students[0][1])["items"]]
    assert after == sequences[0]


# ─────────────────────────────────────────────────────────────
# 验收 3：改题后旧提交从当前榜消失（对照组：不改题时在榜、历史仍可查）
# ─────────────────────────────────────────────────────────────

def test_problem_change_removes_stale_submission_from_current_board(
        client, db_session_factory, test_settings, redis_client, monkeypatch):
    _, admin_header = as_admin(client, db_session_factory)
    problem, _version = create_and_publish(client, admin_header, cases=CASES,
                                           title="版本隔离题")
    _user, header = as_student(client, db_session_factory, "version-student")
    submission_id = _submit(client, header, problem["id"], "version-1")
    _grade(db_session_factory, redis_client, monkeypatch, test_settings,
           problem["id"], submission_id, PASS_IDS[100])
    _opt_in(client, header)
    _recalc(client, admin_header)

    # 对照组：不改题时在榜
    before = _board(client, header)
    assert before["total"] == 1 and before["items"][0]["total_score"] == 100.0

    # 改题（换用例）→ 发布生成新评分版本 → 定向重算
    patched = client.patch(
        f"/api/v1/admin/practice/problems/{problem['id']}",
        json={"hidden_tests": [{**case, "expected": case["expected"] + 1} for case in CASES]},
        headers=admin_header)
    assert patched.status_code == 200, patched.text
    republished = client.post(f"/api/v1/admin/practice/problems/{problem['id']}/publish",
                              headers=admin_header)
    assert republished.status_code == 200, republished.text
    assert republished.json()["current_grading_version_id"] != problem["current_grading_version_id"]

    after = _board(client, header)
    assert after["items"] == [], "旧版本的提交必须退出当前榜"
    assert after["total"] == 0
    # 历史仍可查：提交详情与我的练习都还在
    detail = client.get(f"/api/v1/practice/submissions/{submission_id}", headers=header)
    assert detail.status_code == 200, detail.text
    assert detail.json()["score"] == 100.0
    mine = client.get("/api/v1/practice/me", headers=header).json()
    assert mine["total"] == 1


# ─────────────────────────────────────────────────────────────
# 验收 6：opt-out 立刻从全平台榜消失，班内榜不受影响
# ─────────────────────────────────────────────────────────────

def test_opt_out_hides_platform_row_but_class_board_is_unaffected(
        client, db_session_factory, test_settings, redis_client, monkeypatch):
    _, admin_header = as_admin(client, db_session_factory)
    teacher, teacher_header = as_teacher(client, db_session_factory, "board-teacher")
    problems = _problems(client, admin_header, 2, prefix="退榜题")
    s1_user, s1_header = as_student(client, db_session_factory, "optout-s1")
    s2_user, s2_header = as_student(client, db_session_factory, "optout-s2")
    offering_id = _make_offering(db_session_factory, teacher_id=teacher.id,
                                 student_ids=[s1_user.id, s2_user.id])

    for index, (user, header) in enumerate(((s1_user, s1_header), (s2_user, s2_header))):
        submission_id = _submit(client, header, problems[0]["id"], f"optout-{index}")
        _grade(db_session_factory, redis_client, monkeypatch, test_settings,
               problems[0]["id"], submission_id, PASS_IDS[100])
    _opt_in(client, s1_header)
    _opt_in(client, s2_header)
    _recalc(client, admin_header)

    platform = _board(client, s1_header)
    assert platform["total"] == 2 and platform["opt_in"] is True
    assert [row["is_me"] for row in platform["items"]].count(True) == 1
    legacy_platform = client.get("/api/v1/practice/leaderboard?scope=platform&board=deterministic",
                                 headers=s1_header).json()
    assert legacy_platform["total"] == platform["total"]
    assert legacy_platform["items"] == platform["items"]

    class_board = _board(client, s1_header, scope="offering", scope_id=offering_id)
    assert class_board["total"] == 2, class_board
    assert class_board["anonymized"] is False
    legacy_class = client.get(
        f"/api/v1/practice/leaderboard?scope=offering&scope_id={offering_id}&board=deterministic",
        headers=s1_header).json()
    assert legacy_class["items"] == class_board["items"]
    names = {row["display_name"] for row in class_board["items"]}
    assert names == {s1_user.real_name or s1_user.username, s2_user.real_name or s2_user.username}

    # 退出全平台榜：立即消失，班内榜不变
    assert _opt_in(client, s1_header, False)["opt_in"] is False
    after_platform = _board(client, s1_header)
    assert after_platform["total"] == 1
    assert after_platform["opt_in"] is False
    assert all(not row["is_me"] for row in after_platform["items"])
    legacy_after_opt_out = client.get(
        "/api/v1/practice/leaderboard?scope=platform&board=deterministic",
        headers=s1_header).json()
    assert legacy_after_opt_out["total"] == after_platform["total"]
    assert legacy_after_opt_out["items"] == after_platform["items"]
    after_class = _board(client, s1_header, scope="offering", scope_id=offering_id)
    assert after_class["total"] == 2, "班内榜不受 opt-out 影响"
    assert after_class["items"] == class_board["items"]

    # 教师只读：能看到任课班级榜与全平台榜，但全平台榜只有 opt-in 学生
    teacher_class = _board(client, teacher_header, scope="offering",
                           scope_id=offering_id)
    assert teacher_class["total"] == 2
    teacher_platform = _board(client, teacher_header)
    assert teacher_platform["total"] == 1, "退榜学生不得出现在全平台榜"
    assert teacher_platform["opt_in"] is None
    legacy_teacher_platform = client.get(
        "/api/v1/practice/leaderboard?scope=platform&board=deterministic",
        headers=teacher_header).json()
    assert legacy_teacher_platform["items"] == teacher_platform["items"]

    # 对照组：另一个老师看不到别人班级
    _other, other_header = as_teacher(client, db_session_factory, "board-teacher-2")
    forbidden = client.get(
        f"/api/v1/practice/social/leaderboard?scope=offering&scope_id={offering_id}",
        headers=other_header)
    assert forbidden.status_code == 403, forbidden.text


# ─────────────────────────────────────────────────────────────
# 验收 7：化名稳定、互不相同、不可逆推学号
# ─────────────────────────────────────────────────────────────

def test_alias_is_stable_distinct_and_not_reversible(client, db_session_factory,
                                                     test_settings, redis_client,
                                                     monkeypatch):
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header, cases=CASES, title="化名题")
    students = [as_student(client, db_session_factory, f"alias-s{index}")
                for index in range(1, 4)]
    finished = {}
    for index, (user, header) in enumerate(students):
        submission_id = _submit(client, header, problem["id"], f"alias-{index}")
        _grade(db_session_factory, redis_client, monkeypatch, test_settings,
               problem["id"], submission_id, PASS_IDS[100])
        finished[submission_id] = datetime(2026, 10, 13, 8, 0, tzinfo=UTC) + timedelta(minutes=index)
        _opt_in(client, header)
    _set_finished(db_session_factory, finished)
    _recalc(client, admin_header)

    first = practice_leaderboard.alias(test_settings, scope_kind="platform",
                                       scope_id=0, student_id=students[0][0].id)
    assert first == practice_leaderboard.alias(test_settings, scope_kind="platform",
                                               scope_id=0, student_id=students[0][0].id)
    aliases = {practice_leaderboard.alias(test_settings, scope_kind="platform",
                                          scope_id=0, student_id=user.id)
               for user, _header in students}
    assert len(aliases) == 3, "不同学生必须得到不同化名"

    # 不可逆推学号：化名里不含学号/用户名，也不是无密钥哈希
    import hashlib

    for user, _header in students:
        alias = practice_leaderboard.alias(test_settings, scope_kind="platform",
                                           scope_id=0, student_id=user.id)
        assert str(user.id) not in alias
        assert user.username not in alias
        assert alias != hashlib.sha256(str(user.id).encode()).hexdigest()[:6].upper()
        assert alias != f"同学 {str(user.id)}"

    # API 投影绝不带学号/用户名/源码
    body = _board(client, students[0][1])
    item_keys = set(body["items"][0])
    assert item_keys == {"rank", "display_name", "solved_count", "total_score",
                         "avg_score", "achieved_at", "is_me"}
    assert "student_id" not in body["items"][0]
    for user, _header in students:
        assert user.username not in str(body)


# ─────────────────────────────────────────────────────────────
# 验收 8：教师打练习写端点一律 403
# ─────────────────────────────────────────────────────────────

def test_teacher_write_endpoints_are_forbidden(client, db_session_factory):
    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header, cases=CASES, title="403题")
    _teacher, teacher_header = as_teacher(client, db_session_factory, "write-teacher")
    _student, student_header = as_student(client, db_session_factory, "write-student")

    assert client.put("/api/v1/practice/social/me/opt-in", json={"opt_in": True},
                      headers=teacher_header).status_code == 403
    assert client.post("/api/v1/practice/social/recalculate",
                       headers=teacher_header).status_code == 403
    assert client.post("/api/v1/admin/practice/problems", json={},
                       headers=teacher_header).status_code == 403
    assert client.post(f"/api/v1/practice/problems/{problem['id']}/submissions",
                       json={"code": CODE, "client_request_id": "teacher-1"},
                       headers=teacher_header).status_code == 403
    assert client.patch("/api/v1/admin/practice/settings", json={"grading_mode": "ai"},
                        headers=teacher_header).status_code == 403

    # 对照组：学生能选自己的 opt-in，管理员能触发重算
    assert client.put("/api/v1/practice/social/me/opt-in", json={"opt_in": True},
                      headers=student_header).status_code == 200
    assert client.post("/api/v1/practice/social/recalculate",
                       headers=admin_header).status_code == 202


# ─────────────────────────────────────────────────────────────
# 钩子/收口只作用于练习域（作业、考试、成绩册零影响）
# ─────────────────────────────────────────────────────────────

def test_settlement_only_targets_practice_submissions(client, db_session_factory,
                                                      test_settings, redis_client,
                                                      monkeypatch):
    """关联写入里只有 practice_submissions 会被结算；其他模型原样忽略（对照组）。"""
    from app.models import Submission

    calls: list[int] = []
    original = practice_badge_service.settle_after_terminal_write

    def spy(db, submission_id):
        calls.append(submission_id)
        return original(db, submission_id)

    monkeypatch.setattr(practice_badge_service, "settle_after_terminal_write", spy)

    with db_session_factory() as db:
        # 非练习目标（作业提交）：不结算、不产生任何练习域数据
        settled = practice_badge_service.settle_related_updates(
            db, [(Submission, 999, {"score": 88.0})])
        assert settled == [] and calls == []
        db.commit()

    _, admin_header = as_admin(client, db_session_factory)
    problem, _ = create_and_publish(client, admin_header, cases=CASES, title="收口对照组")
    student, student_header = as_student(client, db_session_factory, "hook-student")
    submission_id = _submit(client, student_header, problem["id"], "hook-1")
    _grade(db_session_factory, redis_client, monkeypatch, test_settings,
           problem["id"], submission_id, PASS_IDS[100])
    # 对照组：练习终态写入确实触发结算，并落地一条徽章（与提交同一事务）
    assert calls == [submission_id], calls
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(PracticeBadgeAward)) == 1

    # 非练习域的普通 ORM 写入不产生练习域副作用，也不触发结算
    with db_session_factory() as db:
        db.execute(update(User).where(User.id == student.id).values(real_name="改名"))
        db.commit()
        assert db.scalar(select(func.count()).select_from(PracticeBoardPreference)) == 0
    assert calls == [submission_id]
