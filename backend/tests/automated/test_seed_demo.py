"""Demo Seed 隔离测试：临时 SQLite + 自造 available 环境版本。

与 CI/E2E/既有测试完全隔离：
- 使用 conftest 的 db_session_factory（临时 SQLite 库）；
- 测试内自造 basic 环境版本（available + digest），仅供本测试运行，
  不触碰真实环境控制面；
- 验证：播种成功、二次运行幂等（计数一致、无重复）、reset 后再次播种计数一致。
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select


def _seed_basic_env(db_session_factory):
    """在测试库内自造 basic 可用环境版本（测试 Fixture，非生产伪造）。"""
    from sqlalchemy import select

    from app.models import EnvironmentProfile, EnvironmentVersion

    with db_session_factory() as db:
        existing = db.scalar(select(EnvironmentProfile).where(EnvironmentProfile.slug == "basic"))
        if existing is not None:
            return existing.id
        profile = EnvironmentProfile(slug="basic", display_name="Basic", status="active")
        db.add(profile)
        db.flush()
        version = EnvironmentVersion(
            profile_id=profile.id,
            version_number=1,
            status="available",
            base_image_ref="python:3.12-slim",
            image_digest="sha256:test-demo-seed-digest-0000000000000000000000000000000000000000000000000000000000000000",
            python_version="3.12",
            minimum_memory_mb=256,
            manifest_sha256="c" * 64,
            available_at=datetime.now(timezone.utc),
        )
        db.add(version)
        db.commit()
        return profile.id


def _run_seed(db_session_factory, *, reset=False):
    from app.seed_demo import run_demo_seed

    with db_session_factory() as db:
        return run_demo_seed(
            db,
            reference_date="2026-12-07",
            reset=reset,
            skip_env_check=True,
            force_fixture=True,  # 测试环境无 Docker，全部 Fixture
        )


def _counts(db_session_factory):
    from sqlalchemy import func, select

    from app.models import (
        AcademicTerm, Announcement, Assignment, Chapter, CodeGrade, Course,
        CourseEnrollment, CourseWhitelistStudent, Exam, ExamAnswer, ExamQuestion, ExamSubmission,
        ExamGrade, ExperimentModule, ExperimentRecord, ExperimentSubmission, JudgeQuestion,
        Lesson, LessonProgress, NotebookTemplate, NotebookTemplateVersion,
        QuestionRubric, Submission, TeachingClass, TeachingClassStudent, User,
        StorageObject, StorageQuarantine,
    )

    models = [
        User, AcademicTerm, TeachingClass, TeachingClassStudent, Course, Chapter,
        Lesson, CourseEnrollment, CourseWhitelistStudent, LessonProgress, Assignment, JudgeQuestion,
        Submission, Exam, ExamQuestion, ExamSubmission, ExamAnswer,
        ExamGrade,
        NotebookTemplate, NotebookTemplateVersion, ExperimentModule,
        ExperimentRecord, ExperimentSubmission, QuestionRubric, CodeGrade,
        Announcement, StorageObject, StorageQuarantine,
    ]
    with db_session_factory() as db:
        return {
            m.__tablename__: int(db.scalar(select(func.count()).select_from(m)) or 0)
            for m in models
        }


def _semantic_outcomes(db_session_factory):
    """Compare demo facts by names/order, independent of retained SQL IDs."""
    import json
    from app.models import (Assignment, CodeGrade, Exam, ExamAnswer, ExamQuestion,
                            ExamSubmission, JudgeQuestion, Submission, User)
    def rows(db, query):
        return sorted(json.dumps(list(row), ensure_ascii=False, sort_keys=True, default=str)
                      for row in db.execute(query))
    grade_values = [CodeGrade.status, CodeGrade.functional_score, CodeGrade.algorithm_score,
                    CodeGrade.robustness_score, CodeGrade.quality_score, CodeGrade.final_score_100,
                    CodeGrade.scaled_score, CodeGrade.needs_teacher_review, CodeGrade.finished_at]
    with db_session_factory() as db:
        return {
            'exam_submissions': rows(db, select(Exam.title, User.username, ExamSubmission.status,
                ExamSubmission.score, ExamSubmission.submission_reason, ExamSubmission.started_at,
                ExamSubmission.submitted_at).join(ExamSubmission, ExamSubmission.exam_id == Exam.id)
                .join(User, User.id == ExamSubmission.student_id)),
            'exam_answers': rows(db, select(Exam.title, User.username, ExamQuestion.order_index,
                ExamQuestion.question_type, ExamAnswer.selected_options, ExamAnswer.text_answers,
                ExamAnswer.code_answer, ExamAnswer.score, ExamAnswer.grading_status)
                .select_from(ExamAnswer).join(ExamSubmission, ExamSubmission.id == ExamAnswer.submission_id)
                .join(Exam, Exam.id == ExamSubmission.exam_id).join(User, User.id == ExamSubmission.student_id)
                .join(ExamQuestion, ExamQuestion.id == ExamAnswer.question_id)),
            'assignment_ai': rows(db, select(Assignment.title, JudgeQuestion.title, User.username,
                Submission.attempt_count, *grade_values).select_from(CodeGrade)
                .join(Submission, Submission.id == CodeGrade.submission_id)
                .join(User, User.id == Submission.student_id).join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
                .join(Assignment, Assignment.id == JudgeQuestion.assignment_id)),
            'exam_ai': rows(db, select(Exam.title, ExamQuestion.order_index, User.username, *grade_values)
                .select_from(CodeGrade).join(ExamAnswer, ExamAnswer.id == CodeGrade.exam_answer_id)
                .join(ExamQuestion, ExamQuestion.id == ExamAnswer.question_id)
                .join(ExamSubmission, ExamSubmission.id == ExamAnswer.submission_id)
                .join(Exam, Exam.id == ExamSubmission.exam_id).join(User, User.id == ExamSubmission.student_id)),
        }


def test_seed_demo_runs_and_is_idempotent(db_session_factory):
    _seed_basic_env(db_session_factory)
    first = _run_seed(db_session_factory)
    second = _run_seed(db_session_factory)

    c1 = _counts(db_session_factory)
    c2 = _counts(db_session_factory)
    assert c1 == c2, f"二次播种计数漂移: {c1} vs {c2}"

    # 核心数量断言
    assert c1["users"] >= 64
    assert c1["courses"] >= 8
    assert c1["course_whitelist_students"] >= 3
    assert c1["assignments"] >= 9
    assert c1["submissions"] >= 200
    assert c1["code_grades"] >= 100
    assert c1["exams"] >= 3
    assert c1["storage_objects"] == 0
    assert c1["storage_quarantines"] == 0

    from app.models import Exam, ExamQuestion
    with db_session_factory() as db:
        midterm = db.scalar(select(Exam).where(Exam.title == "期中测验：Python 函数与数据结构"))
        code_question = db.scalar(select(ExamQuestion).where(
            ExamQuestion.exam_id == midterm.id,
            ExamQuestion.question_type == "code",
        ))
        assert code_question.reference_solution.startswith("def sum_positive")

    from sqlalchemy import text
    from app.models import Assignment, CodeGrade, JudgeQuestion, Submission, User
    from app.models import EnvironmentBuildJob, EnvironmentVersion
    with db_session_factory() as db:
        roles = dict(db.execute(
            select(User.role, func.count()).group_by(User.role)
        ).all())
        assert roles == {"admin": 1, "teacher": 3, "student": 60}
        assert db.scalar(select(func.count()).select_from(EnvironmentBuildJob)) == 0
        assert db.scalar(
            select(func.count()).select_from(EnvironmentVersion).where(
                EnvironmentVersion.status == "available",
                EnvironmentVersion.image_digest.is_not(None),
            )
        ) == 1
        assert db.scalar(text("SELECT COUNT(*) FROM courses WHERE cover_object_id IS NOT NULL")) == 0
        assert db.scalar(text("SELECT COUNT(*) FROM lessons WHERE video_object_id IS NOT NULL")) == 0
        assert db.scalar(text("SELECT COUNT(*) FROM notebook_templates WHERE draft_assets_dir IS NOT NULL")) == 0
        assert db.scalar(text("SELECT COUNT(*) FROM notebook_template_versions WHERE assets_dir IS NOT NULL")) == 0
        # notebook 课时必须绑定已发布版本的模板，否则学生端打开即 404
        assert db.scalar(
            text(
                "SELECT COUNT(*) FROM lessons l "
                "JOIN notebook_templates t ON t.id = l.template_id "
                "WHERE l.content_type = 'notebook' AND (t.current_version_id IS NULL)"
            )
        ) == 0
        assert db.scalar(
            text("SELECT COUNT(*) FROM lessons WHERE content_type = 'notebook' AND template_id IS NULL")
        ) == 0

        from app.seed_demo.marks import all_marks, validate_mark_tables
        from app.seed_demo.verify import MARKED_MODELS

        marks = all_marks(db)
        validate_mark_tables(marks)
        for table_name, row_ids in marks.items():
            if table_name == "announcement_reads":
                placeholders = ",".join(f":id{i}" for i in range(len(row_ids)))
                existing = {
                    row[0] for row in db.execute(
                        text(f"SELECT id FROM announcement_reads WHERE id IN ({placeholders})"),
                        {f"id{i}": row_id for i, row_id in enumerate(row_ids)},
                    ).all()
                }
            else:
                model = MARKED_MODELS[table_name]
                existing = set(db.scalars(select(model.id).where(model.id.in_(row_ids))).all())
            assert existing == set(row_ids), f"stale/missing mark: {table_name}"

    # 期中/章节测验的可发布 submission 在 seed 完成 CodeGrade 后，必须同步生成
    # ExamGrade；review_required/system_error 不得被按 0 分强行汇总。
    from app.models import ExamAnswer, ExamGrade, ExamSubmission
    with db_session_factory() as db:
        submissions = db.scalars(select(ExamSubmission).where(ExamSubmission.status == "graded")).all()
        expected = set()
        for submission in submissions:
            answers = db.scalars(select(ExamAnswer).where(ExamAnswer.submission_id == submission.id)).all()
            if answers and all(answer.grading_status == "completed" and answer.score is not None for answer in answers):
                expected.add((submission.exam_id, submission.student_id, round(submission.score, 2)))
        actual = {
            (grade.exam_id, grade.student_id, round(grade.score, 2))
            for grade in db.scalars(select(ExamGrade)).all()
        }
        assert actual == expected

    # 固定画像必须传递到 AI 作业评分：不能因为用 student_id 查用户名映射失败，
    # 把 elite / struggling 都错误地按 background average 评分。
    with db_session_factory() as db:
        def _feedback_for(username):
            return db.scalars(
                select(CodeGrade)
                .join(Submission, Submission.id == CodeGrade.submission_id)
                .join(JudgeQuestion, JudgeQuestion.id == Submission.question_id)
                .join(Assignment, Assignment.id == JudgeQuestion.assignment_id)
                .join(User, User.id == Submission.student_id)
                .where(User.username == username, JudgeQuestion.grading_mode == "active")
            ).all()

        elite_grades = _feedback_for("demo_student_elite")
        struggling_grades = _feedback_for("demo_student_struggling")
        assert elite_grades, "elite 应至少有一条 AI 作业评分"
        assert struggling_grades, "struggling 应至少有一条 AI 作业评分"
        assert all(
            not (grade.ai_result or {}).get("student_feedback", {}).get("issues")
            for grade in elite_grades
        )
        assert any(
            "核心功能未完全实现" in (grade.ai_result or {}).get("student_feedback", {}).get("issues", [])
            for grade in struggling_grades
        )

    # 唯一键抽查：用户名无重复
    with db_session_factory() as db:
        dup = db.execute(
            text("SELECT username, COUNT(*) c FROM users GROUP BY username HAVING c > 1")
        ).all()
        assert not dup, f"用户名重复: {dup}"

    # 白名单课程权限：elite 可见且已选课，struggling 不可见
    from app.models import Course, CourseWhitelistStudent, CourseEnrollment

    with db_session_factory() as db:
        whitelist = db.scalar(select(Course).where(Course.title == "AI 创新实践（白名单）"))
        assert whitelist is not None
        assert whitelist.visibility == "whitelist"
        elite = db.scalar(select(User).where(User.username == "demo_student_elite"))
        struggling = db.scalar(select(User).where(User.username == "demo_student_struggling"))
        assert elite is not None and struggling is not None
        assert db.scalar(select(CourseWhitelistStudent).where(
            CourseWhitelistStudent.course_id == whitelist.id,
            CourseWhitelistStudent.student_id == elite.id,
        )) is not None
        assert db.scalar(select(CourseWhitelistStudent).where(
            CourseWhitelistStudent.course_id == whitelist.id,
            CourseWhitelistStudent.student_id == struggling.id,
        )) is None
        assert db.scalar(select(CourseEnrollment).where(
            CourseEnrollment.course_id == whitelist.id,
            CourseEnrollment.student_id == elite.id,
            CourseEnrollment.origin == "manual",
        )) is not None

    # 回归：期末已发布未开始，不得出现未来 started 提交（曾导致倒计时显示 817 小时）
    from app.models import Exam, ExamSubmission

    with db_session_factory() as db:
        final = db.scalar(select(Exam).where(Exam.title == "期末上机考试：Python 与 AI 综合"))
        assert final is not None
        started_count = db.scalar(
            select(func.count()).select_from(ExamSubmission).where(
                ExamSubmission.exam_id == final.id,
                ExamSubmission.status == "started",
            )
        ) or 0
        assert started_count == 0, f"期末未开始却存在 started 提交: {started_count}"


def test_seed_exam_grades_use_formal_finalizer(db_session_factory, monkeypatch):
    """Demo seed 必须复用正式考试 finalizer，而不是复制一套汇总逻辑。"""
    _seed_basic_env(db_session_factory)

    from app.services import exam_grading

    calls = []
    original = exam_grading.finalize_if_ready

    def tracked_finalize(submission_id, db):
        result = original(submission_id, db)
        calls.append(result)
        return result

    monkeypatch.setattr(exam_grading, "finalize_if_ready", tracked_finalize)
    summary = _run_seed(db_session_factory)

    assert calls, "seed 应调用正式 Exam finalizer"
    assert all(result.outcome.value in {"graded", "review_required"} for result in calls)
    assert summary["exam_grades"] == sum(result.outcome.value == "graded" for result in calls)


def test_seed_demo_reset_then_reseed_matches(db_session_factory):
    """--reset-demo 语义 = 先清 Demo 数据再播种；本测试分两步验证：
    1) reset_demo_data 只清登记数据（业务表归零、环境控制面保留）；
    2) 清空后再次播种，计数与首次一致。
    """
    from app.seed_demo.cleanup import reset_demo_data

    _seed_basic_env(db_session_factory)
    first = _run_seed(db_session_factory)
    c_first = _counts(db_session_factory)
    outcomes_first = _semantic_outcomes(db_session_factory)

    # 模拟 API/教师操作产生的运行态与审计数据：通知、已读、偏好、改分
    from app.models import CodeGrade, GradeOverride, Notification, NotificationRead, User, UserPreference

    with db_session_factory() as db:
        demo_user = db.scalar(select(User).where(User.username == "demo_student_elite"))
        code_grade = db.scalars(select(CodeGrade).limit(1)).first()
        assert demo_user is not None
        notification = Notification(
            recipient_id=demo_user.id,
            type="work",
            title="测试通知",
            content="用于验证 reset 会清理 API 运行态",
            dedupe_key="test-reset-derived",
            visible=True,
        )
        db.add(notification)
        db.flush()
        db.add(NotificationRead(notification_id=notification.id, user_id=demo_user.id))
        db.add(UserPreference(user_id=demo_user.id, preferences={"sidebar_collapsed": True}))
        if code_grade is not None:
            db.add(GradeOverride(
                code_grade_id=code_grade.id,
                original_snapshot={"score": 1},
                replacement_snapshot={"score": 2},
                reason="reset 测试",
                reviewer_id=demo_user.id,
            ))
        db.commit()

    # 单独执行清理步骤（不立即重播）
    with db_session_factory() as db:
        reset_demo_data(db)
    c_after_reset = _counts(db_session_factory)
    # reset 后业务表应为 0（保留环境控制面）
    assert c_after_reset["users"] == 0
    assert c_after_reset["courses"] == 0

    # API 运行态 / 审计行也应被清理
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 0
        assert db.scalar(select(func.count()).select_from(NotificationRead)) == 0
        assert db.scalar(select(func.count()).select_from(UserPreference)) == 0
        assert db.scalar(select(func.count()).select_from(GradeOverride)) == 0

    # 清空后再次播种：计数与首次一致（可复现）
    second = _run_seed(db_session_factory)
    c_second = _counts(db_session_factory)
    assert c_first == c_second, f"reset 后重播计数不一致: {c_first} vs {c_second}"
    assert outcomes_first == _semantic_outcomes(db_session_factory), '重播后演示答案、成绩或复核状态变化'


def test_reset_demo_rejects_unknown_mark_table(db_session_factory):
    """reset 不应静默忽略未来/历史未支持的所有权标记。"""
    from app.seed_demo.cleanup import reset_demo_data
    from app.seed_demo.marks import ensure_marks_table
    from sqlalchemy import text

    with db_session_factory() as db:
        ensure_marks_table(db)
        db.execute(
            text(
                "INSERT INTO demo_seed_marks (mark_key, table_name, row_id, created_at) "
                "VALUES ('unsupported:1', 'unsupported_table', 1, :created_at)"
            ),
            {"created_at": datetime.now(timezone.utc)},
        )
        db.commit()

    with db_session_factory() as db:
        with pytest.raises(RuntimeError, match="不支持的表"):
            reset_demo_data(db)

    with db_session_factory() as db:
        assert db.scalar(
            text("SELECT COUNT(*) FROM demo_seed_marks WHERE table_name = 'unsupported_table'")
        ) == 1
