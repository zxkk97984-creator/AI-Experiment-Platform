import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest
import fakeredis
from sqlalchemy import text

# This execution sandbox also cannot wake the asyncio loop from AnyIO's
# worker threads.  FastAPI dispatches synchronous endpoints through that
# pool, so a same-thread ASGI client still deadlocks unless the test backend
# executes those synchronous callables inline.  Production keeps AnyIO's
# normal worker-pool implementation; this is only the deterministic test
# runtime adapter.
from anyio._backends._asyncio import AsyncIOBackend


@classmethod
async def _run_sync_inline(
    cls,
    func,
    args,
    abandon_on_cancel=False,
    limiter=None,
):
    await cls.checkpoint()
    return func(*args)


AsyncIOBackend.run_sync_in_worker_thread = _run_sync_inline

ROOT = Path(__file__).resolve().parents[2]
PYTEST_TEMP_ROOT = ROOT / ".pytest-temp-root"
PYTEST_TEMP_ROOT.mkdir(exist_ok=True)
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(PYTEST_TEMP_ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, get_settings
from app.database import Base, create_engine_from_url, sessionmaker_for_engine
from app.dependencies import get_db, get_redis_client
from app.main import create_app
from app.models import User
from app.security import hash_password


class SynchronousASGIClient:
    """Run ASGI requests without AnyIO's cross-thread blocking portal.

    The sandbox's current AnyIO backend cannot wake an event loop from another
    thread (``BlockingPortal.call()`` waits forever in
    ``loop.call_soon_threadsafe``).  Starlette's TestClient relies on exactly
    that portal.  This adapter keeps the synchronous test API while running
    each request on the calling thread's asyncio loop, so failures and
    exceptions retain normal ASGI/HTTPX semantics.
    """

    def __init__(self, app, *, raise_server_exceptions: bool = True, **kwargs):
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app,
                raise_app_exceptions=raise_server_exceptions,
                root_path=kwargs.pop("root_path", ""),
                client=("testclient", 50000),
            ),
            base_url=kwargs.pop("base_url", "http://testserver"),
            headers=kwargs.pop("headers", None),
            cookies=kwargs.pop("cookies", None),
            follow_redirects=kwargs.pop("follow_redirects", True),
        )

    def request(self, method: str, url: str, **kwargs):
        return asyncio.run(self._client.request(method, url, **kwargs))

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.request("DELETE", url, **kwargs)

    @property
    def cookies(self):
        return self._client.cookies

    def close(self):
        asyncio.run(self._client.aclose())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


# Direct ``from fastapi.testclient import TestClient`` imports in a few
# legacy tests must use the same non-blocking adapter as the shared fixture.
import fastapi.testclient as _fastapi_testclient
import starlette.testclient as _starlette_testclient

_fastapi_testclient.TestClient = SynchronousASGIClient
_starlette_testclient.TestClient = SynchronousASGIClient


@pytest.fixture(autouse=True)
def _isolated_op_metrics_aggregator():
    """指标聚合器是进程级内存状态：每个用例前清空，避免跨用例累加。"""
    from app.services import op_metrics

    op_metrics.reset_pending()
    yield
    op_metrics.reset_pending()


@pytest.fixture()
def test_settings(tmp_path):
    # 可通过 DAI_DATABASE_URL 环境变量切换到 MySQL（CI job 设置此变量）
    # 仅在未设置环境变量时创建临时 SQLite 数据库
    db_url = os.environ.get("DAI_DATABASE_URL", "")
    if not db_url:
        db_path = tmp_path / "test.db"
        db_url = f"sqlite:///{db_path}"
    return Settings(
        # Do not let a developer's backend/.env (for example V2=true) change
        # the default SQLite test contract.  V2-specific tests opt in below.
        environment="development",
        environment_editor_v2_enabled=False,
        env_base_image="python:3.12-slim",
        env_registry_repository=None,
        env_registry_docker_config="/run/secrets/config.json",
        database_url=db_url,
        redis_url=os.environ.get("DAI_REDIS_URL", "redis://localhost:6379/15"),
        secret_key="test-secret-key",
        account_security_keys="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        access_token_expire_minutes=30,
        refresh_token_expire_days=7,
        jupyter_base_url="http://localhost:8888",
        judge_use_docker=False,
        judge_timeout_seconds=5,
        studio_storage_dir=str(tmp_path / "studio"),
        # 测试视频目录指向临时目录，绝不写入真实 backend/storage/videos/
        video_storage_dir=str(tmp_path / "videos"),
        video_max_upload_bytes=500 * 1024 * 1024,
        video_playback_url_ttl_seconds=3600,
        # 测试封面目录指向临时目录，绝不写入真实 backend/storage/covers/
        cover_storage_dir=str(tmp_path / "covers"),
        cover_max_upload_bytes=5 * 1024 * 1024,
        # TASK-020：默认关闭后，测试环境显式选择启用（等价生产 DAI_AI_ENABLED=true 的显式审批后配置）；
        # 需要验证禁用语义的测试自行构造 Settings
        ai_enabled=True,
        ai_api_key="test-ai-key-not-real",
    )


@pytest.fixture()
def db_session_factory(test_settings):
    engine = create_engine_from_url(test_settings.database_url)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker_for_engine(engine)
    try:
        yield SessionLocal
    finally:
        # notebook_templates.current_version_id ↔ notebook_template_versions 是
        # use_alter 循环外键：外键开启后 drop_all 按依赖序先删 versions，会被
        # templates 的引用卡死（SQLite/MySQL 同病）。先清空引用再删表（方言无关）。
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE notebook_templates SET current_version_id = NULL")
            )
            # environment_profiles.current_version_id ↔ environment_versions
            # is the V2 publication pointer cycle; clear it before dropping
            # versions so SQLite/MySQL enforce the FK consistently.
            conn.execute(
                text("UPDATE environment_profiles SET current_version_id = NULL")
            )
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def redis_client(monkeypatch):
    from app.services import judge_queue
    client = fakeredis.FakeRedis(decode_responses=True)
    client.flushall()
    # Queue service creates clients outside FastAPI dependency injection.
    monkeypatch.setattr(judge_queue, "_get_redis", lambda: client)
    return client


@pytest.fixture()
def app(test_settings, db_session_factory, redis_client):
    os.environ["DAI_SECRET_KEY"] = test_settings.secret_key
    app = create_app(test_settings)

    def override_db():
        db = db_session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis_client] = lambda: redis_client
    app.dependency_overrides[get_settings] = lambda: test_settings
    return app


@pytest.fixture()
def client(app):
    test_client = SynchronousASGIClient(app)
    try:
        yield test_client
    finally:
        # TestClient creates an AnyIO portal thread on request.  Explicitly
        # close it so pytest can terminate without waiting on epoll forever.
        test_client.close()


def create_user(db_session_factory, username, role, password="Passw0rd!", real_name=None):
    with db_session_factory() as db:
        user = User(
            username=username,
            real_name=real_name or username,
            role=role,
            status="active",
            password_hash=hash_password(password),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def create_course_db(
    db_session_factory,
    *,
    teacher_username="teacher",
    title="测试课程",
    description="测试课程描述",
    status="draft",
    visibility="class",
    default_score=100.0,
    cover=None,
    start_time=None,
    academic_term_id=None,
):
    """领域 fixture：直接创建课程行（绕过 API 发布门禁）。

    用于与「课程发布」无关的测试；发布门禁本身由
    test_course_publish_requirements.py 走 API 覆盖。
    """
    from app.models import Course

    seed_basic_environment(db_session_factory)
    with db_session_factory() as db:
        teacher = db.query(User).filter(User.username == teacher_username).first()
        course = Course(
            title=title,
            description=description,
            status=status,
            visibility=visibility,
            default_score=default_score,
            teacher_id=teacher.id if teacher else None,
            cover=cover,
            start_time=start_time,
            academic_term_id=academic_term_id,
        )
        db.add(course)
        db.commit()
        db.refresh(course)
        return course.id


def create_assignment_db(
    db_session_factory,
    *,
    course_id,
    teacher_username="teacher",
    title="测试作业",
    status="draft",
    due_at=None,
    environment_version_id=None,
):
    """领域 fixture：直接创建作业行（绕过 API 发布门禁）。

    用于与「作业发布」无关的测试；发布门禁本身由
    test_assignment_publish_gate.py 走 API 覆盖。
    """
    from app.models import Assignment

    seed_basic_environment(db_session_factory)
    with db_session_factory() as db:
        teacher = db.query(User).filter(User.username == teacher_username).first()
        kwargs = dict(
            course_id=course_id,
            title=title,
            status=status,
            due_at=due_at,
            created_by_id=teacher.id if teacher else None,
        )
        # 环境版本 NOT NULL：显式传 None 会绕过模型 default，仅非 None 时传参
        if environment_version_id is not None:
            kwargs["environment_version_id"] = environment_version_id
        assignment = Assignment(**kwargs)
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return assignment.id


def seed_basic_environment(db_or_factory):
    """幂等 seed：basic 档位 available 版本（带 digest）。

    模型层 environment_version_id 已与迁移 B 对齐为 NOT NULL，
    未显式绑定环境的测试记录依赖 resolve_basic_env_version_id 惰性默认，
    需要库中存在 basic 可用版本；环境控制面自身的测试不要调用本 helper。
    参数可以是 session factory，也可以是一个已打开的 Session。
    """
    from app.models import EnvironmentProfile, EnvironmentVersion

    def _seed(session):
        if (
            session.query(EnvironmentProfile)
            .filter(EnvironmentProfile.slug == "basic")
            .first()
            is not None
        ):
            return
        profile = EnvironmentProfile(
            slug="basic", display_name="Python 基础", status="active"
        )
        session.add(profile)
        session.flush()
        version = EnvironmentVersion(
            profile_id=profile.id,
            version_number=1,
            status="available",
            base_image_ref="python:3.12-slim@sha256:" + "0" * 64,
            image_digest="sha256:" + "1" * 64,
            python_version="3.12",
            minimum_memory_mb=256,
            manifest_sha256="c" * 64,
        )
        session.add(version)
        session.commit()

    if hasattr(db_or_factory, "query") and hasattr(db_or_factory, "add"):
        _seed(db_or_factory)
    else:
        with db_or_factory() as session:
            _seed(session)


@pytest.fixture(autouse=True)
def _auto_seed_basic_environment(request, db_session_factory):
    """所有测试默认预置 basic 可用环境版本（模型层 environment_version_id 已 NOT NULL）。

    直接操作环境控制面表的测试（environment_* 等）自行管理种子数据，
    通过 pytestmark = pytest.mark.no_auto_env_seed 关闭本 fixture。
    """
    marker = request.node.get_closest_marker("no_auto_env_seed")
    if marker is None:
        seed_basic_environment(db_session_factory)


def login(client, username, password="Passw0rd!"):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    # refresh_token 在 HttpOnly Cookie 中，TestClient 不返回 Cookie 的值
    # 从 Set-Cookie 头中提取（测试需要）
    refresh_token = ""
    for cookie_str in response.headers.get_list("set-cookie"):
        if "dai_refresh_token=" in cookie_str:
            refresh_token = cookie_str.split("dai_refresh_token=")[1].split(";")[0]
            break
    return data["access_token"], refresh_token


def constraint_violation():
    """跨方言捕获 CHECK 约束违反。

    SQLite 抛 sqlalchemy.exc.IntegrityError；MySQL 8 对 CHECK 违反抛
    OperationalError(3819)。约束语义测试统一用本 helper 断言。
    """
    from sqlalchemy.exc import IntegrityError, OperationalError

    return pytest.raises((IntegrityError, OperationalError))


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def grant_teaching_fixture(db, course, student_ids=(), *, bind_tasks=True):
    """Explicit administrator grant in setup; never installed as an auto hook.

    Older domain fixtures need a real teaching context after ownership and data
    authority were separated. Security tests still construct unassigned data.
    """
    from datetime import date, timedelta
    from sqlalchemy import select
    from app.models import (AcademicTerm, Assignment, Chapter, CourseEnrollment, CourseTeachingClass, Exam,
        ExperimentRecord, Lesson, TeachingClass)
    from app.models.teaching import CourseOffering
    from app.services.offering_service import enroll_student
    db.flush()
    offering = db.scalar(select(CourseOffering).where(CourseOffering.course_id == course.id,
        CourseOffering.teacher_id == course.teacher_id, CourseOffering.status == 'active'))
    if not offering:
        klass = db.scalar(select(TeachingClass).join(CourseTeachingClass).where(CourseTeachingClass.course_id == course.id).order_by(TeachingClass.id))
        if not klass:
            term = db.get(AcademicTerm, course.academic_term_id) if course.academic_term_id else None
            if not term:
                term = AcademicTerm(code=f'fixture-{course.id}', name='测试学期', start_date=date.today(), end_date=date.today() + timedelta(days=120), status='active')
                db.add(term); db.flush()
            klass = TeachingClass(academic_term_id=term.id, code=f'fixture-{course.id}', name='测试教学班', status='active')
            db.add(klass); db.flush()
            db.add(CourseTeachingClass(course_id=course.id, teaching_class_id=klass.id))
        offering = CourseOffering(course_id=course.id, academic_term_id=klass.academic_term_id,
            teaching_class_id=klass.id, teacher_id=course.teacher_id, status='active')
        db.add(offering); db.flush()
    for student_id in student_ids:
        projection = db.scalar(select(CourseEnrollment).where(
            CourseEnrollment.course_id == course.id, CourseEnrollment.student_id == student_id))
        enroll_student(db, offering, student_id,
                       origin='class' if projection and projection.origin == 'class' else 'manual')
    if bind_tasks:
        for model in (Assignment, Exam):
            for task in db.scalars(select(model).where(model.course_id == course.id, model.offering_id.is_(None))):
                task.offering_id = offering.id
        for record in db.scalars(select(ExperimentRecord).join(Lesson).join(Chapter).where(Chapter.course_id == course.id, ExperimentRecord.offering_id.is_(None))):
            record.offering_id, record.scope_key = offering.id, f'offering:{offering.id}'
    db.flush()
    return offering


# ═══════════════════════════════════════════════════════════
# 共享领域工厂（A/B/C 分类法）
#
#  SQLite 测试引擎已开启外键（PRAGMA foreign_keys=ON，与 MySQL 对齐），
#  任何插入都必须有真实父行。测试按需分三类：
#   · C 类：纯校验/无 DB——不使用工厂；
#   · B 类：最小父行——单个工厂（如 make_judge_question）；
#   · A 类：完整领域图——组合工厂（如 make_submission 级联建全链）。
#  修复外键违规时一律使用本层工厂，禁止在测试里散装硬编码父 ID。
# ═══════════════════════════════════════════════════════════


def get_or_create_user(db_session_factory, username, role, password="Passw0rd!"):
    """幂等建用户：存在即复用。工厂组合时避免默认用户名重复创建冲突。"""
    with db_session_factory() as db:
        existing = db.query(User).filter(User.username == username).first()
        if existing is not None:
            return existing
        user = User(
            username=username,
            real_name=username,
            role=role,
            status="active",
            password_hash=hash_password(password),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def make_teacher(db_session_factory, username="teacher"):
    """B 类：教师用户（幂等）"""
    return get_or_create_user(db_session_factory, username, "teacher")


def make_student(db_session_factory, username="student"):
    """B 类：学生用户（幂等）"""
    return get_or_create_user(db_session_factory, username, "student")


def make_course(db_session_factory, *, teacher_username="teacher",
                title="测试课程", status="published", **kwargs):
    """B 类：课程（含教师，幂等）"""
    get_or_create_user(db_session_factory, teacher_username, "teacher")
    return create_course_db(
        db_session_factory,
        teacher_username=teacher_username,
        title=title,
        status=status,
        **kwargs,
    )


def make_assignment(db_session_factory, *, course_id=None,
                    teacher_username="teacher", title="测试作业",
                    status="published", **kwargs):
    """A 类：作业（无 course_id 时级联建课程）"""
    if course_id is None:
        course_id = make_course(db_session_factory, teacher_username=teacher_username)
    get_or_create_user(db_session_factory, teacher_username, "teacher")
    ident = create_assignment_db(
        db_session_factory,
        course_id=course_id,
        teacher_username=teacher_username,
        title=title,
        status=status,
        **kwargs,
    )

    from app.models import Assignment, Course
    with db_session_factory() as db:
        task = db.get(Assignment, ident)
        grant_teaching_fixture(db, db.get(Course, task.course_id))
        db.commit()
    return ident


def make_judge_question(db_session_factory, *, assignment_id=None,
                        title="测试题目", function_name="solve", **kwargs):
    """A 类：判题题目（无 assignment_id 时级联建作业→课程→教师）"""
    from app.models import JudgeQuestion

    if assignment_id is None:
        assignment_id = make_assignment(db_session_factory)
    with db_session_factory() as db:
        question = JudgeQuestion(
            assignment_id=assignment_id,
            title=title,
            function_name=function_name,
            hidden_tests=kwargs.pop("hidden_tests", "assert True"),
            **kwargs,
        )
        db.add(question)
        db.commit()
        db.refresh(question)
        return question.id


def make_submission(db_session_factory, *, question_id=None,
                    student_username="student", code="def solve():\n    return 0",
                    **kwargs):
    """A 类：作业提交（级联建题目→作业→课程→教师 + 学生）"""
    from app.models import Submission

    if question_id is None:
        question_id = make_judge_question(db_session_factory)
    student = get_or_create_user(db_session_factory, student_username, "student")
    with db_session_factory() as db:
        submission = Submission(
            question_id=question_id,
            student_id=student.id,
            code=code,
            **kwargs,
        )
        db.add(submission); db.flush()
        from app.models import Course, JudgeQuestion
        grant_teaching_fixture(db, db.get(Course, db.get(JudgeQuestion, question_id).assignment.course_id), [student.id])
        db.commit()
        db.refresh(submission)
        return submission.id


def make_rubric(db_session_factory, *, judge_question_id=None,
                exam_question_id=None, version=1, status="draft",
                source_hash="a" * 64, **kwargs):
    """A 类：题目 Rubric（judge_question_id/exam_question_id 二选一）"""
    from app.models import QuestionRubric

    if judge_question_id is None and exam_question_id is None:
        judge_question_id = make_judge_question(db_session_factory)
    with db_session_factory() as db:
        rubric = QuestionRubric(
            judge_question_id=judge_question_id,
            exam_question_id=exam_question_id,
            version=version,
            status=status,
            source_hash=source_hash,
            source_snapshot=kwargs.pop("source_snapshot", {}),
            rubric_json=kwargs.pop("rubric_json", {}),
            model_name=kwargs.pop("model_name", "test-model"),
            **kwargs,
        )
        db.add(rubric)
        db.commit()
        db.refresh(rubric)
        return rubric.id


def make_code_grade(db_session_factory, *, submission_id=None, rubric_id=None,
                    mode="shadow", **kwargs):
    """A 类：AI 评分记录（级联建提交链 + rubric）"""
    from app.models import CodeGrade

    if submission_id is None:
        submission_id = make_submission(db_session_factory)
    if rubric_id is None:
        rubric_id = make_rubric(db_session_factory)
    with db_session_factory() as db:
        grade = CodeGrade(
            submission_id=submission_id,
            rubric_id=rubric_id,
            mode=mode,
            **kwargs,
        )
        db.add(grade)
        db.commit()
        db.refresh(grade)
        return grade.id


def make_exam(db_session_factory, *, course_id=None, title="测试考试",
              status="published", **kwargs):
    """A 类：考试（无 course_id 时级联建课程）"""
    from app.models import Exam

    if course_id is None:
        course_id = make_course(db_session_factory)
    with db_session_factory() as db:
        exam = Exam(course_id=course_id, title=title, status=status, **kwargs)
        db.add(exam); db.flush()
        from app.models import Course
        grant_teaching_fixture(db, db.get(Course, course_id))
        db.commit()
        db.refresh(exam)
        return exam.id


def make_exam_question(db_session_factory, *, exam_id=None, prompt="测试题",
                       points=10, **kwargs):
    """A 类：考试题（无 exam_id 时级联建考试→课程）"""
    from app.models import ExamQuestion

    if exam_id is None:
        exam_id = make_exam(db_session_factory)
    with db_session_factory() as db:
        question = ExamQuestion(
            exam_id=exam_id,
            question_type=kwargs.pop("question_type", "code"),
            prompt=prompt,
            correct_answer=kwargs.pop("correct_answer", {}),
            points=points,
            **kwargs,
        )
        db.add(question)
        db.commit()
        db.refresh(question)
        return question.id


def make_exam_submission(db_session_factory, *, exam_id=None,
                         student_username="student", status="grading", **kwargs):
    """A 类：考试提交（级联建考试→课程 + 学生）"""
    from app.models import ExamSubmission

    if exam_id is None:
        exam_id = make_exam(db_session_factory)
    student = get_or_create_user(db_session_factory, student_username, "student")
    with db_session_factory() as db:
        submission = ExamSubmission(
            exam_id=exam_id,
            student_id=student.id,
            status=status,
            **kwargs,
        )
        db.add(submission)
        db.commit()
        db.refresh(submission)
        return submission.id


def make_exam_answer(db_session_factory, *, submission_id=None, question_id=None,
                     code_answer="def solve():\n    return 0", **kwargs):
    """A 类：考试答题（级联建提交/题目链 + 学生）"""
    from app.models import ExamAnswer

    if submission_id is None:
        submission_id = make_exam_submission(db_session_factory)
    if question_id is None:
        question_id = make_exam_question(db_session_factory)
    with db_session_factory() as db:
        answer = ExamAnswer(
            submission_id=submission_id,
            question_id=question_id,
            code_answer=code_answer,
            **kwargs,
        )
        db.add(answer)
        db.commit()
        db.refresh(answer)
        return answer.id


def make_exam_chain(db_session_factory, *, student_username="student"):
    """A 类：考试全链路（exam→question→submission→answer），返回各 id 字典"""
    exam_id = make_exam(db_session_factory)
    question_id = make_exam_question(db_session_factory, exam_id=exam_id)
    submission_id = make_exam_submission(
        db_session_factory, exam_id=exam_id, student_username=student_username
    )
    answer_id = make_exam_answer(
        db_session_factory, submission_id=submission_id, question_id=question_id
    )
    return {
        "exam_id": exam_id,
        "question_id": question_id,
        "submission_id": submission_id,
        "answer_id": answer_id,
    }


def make_chapter(db_session_factory, *, course_id=None, title="测试章节", **kwargs):
    """A 类：章节（无 course_id 时级联建课程）"""
    from app.models import Chapter

    if course_id is None:
        course_id = make_course(db_session_factory)
    with db_session_factory() as db:
        chapter = Chapter(course_id=course_id, title=title, **kwargs)
        db.add(chapter)
        db.commit()
        db.refresh(chapter)
        return chapter.id


def make_lesson(db_session_factory, *, chapter_id=None, title="测试课时", **kwargs):
    """A 类：课时（无 chapter_id 时级联建章节→课程）"""
    from app.models import Lesson

    if chapter_id is None:
        chapter_id = make_chapter(db_session_factory)
    with db_session_factory() as db:
        lesson = Lesson(chapter_id=chapter_id, title=title, **kwargs)
        db.add(lesson)
        db.commit()
        db.refresh(lesson)
        return lesson.id


def make_experiment_module(db_session_factory, *, name="测试实验模块",
                           owner_username="teacher", **kwargs):
    """B 类：实验模块（owner 教师可空但默认建教师）"""
    from app.models import ExperimentModule

    owner = get_or_create_user(db_session_factory, owner_username, "teacher")
    with db_session_factory() as db:
        module = ExperimentModule(
            name=name,
            owner_id=owner.id,
            status=kwargs.pop("status", "published"),
            **kwargs,
        )
        db.add(module)
        db.commit()
        db.refresh(module)
        return module.id


def make_notebook_template(db_session_factory, *, name="测试模板",
                           owner_username="teacher", **kwargs):
    """B 类：实验模板（owner 教师）"""
    from app.models import NotebookTemplate

    owner = get_or_create_user(db_session_factory, owner_username, "teacher")
    with db_session_factory() as db:
        template = NotebookTemplate(
            name=name,
            owner_id=owner.id,
            **kwargs,
        )
        db.add(template)
        db.commit()
        db.refresh(template)
        return template.id


def make_template_version(db_session_factory, *, template_id=None,
                          published_by_username="teacher", version_number=1,
                          **kwargs):
    """A 类：模板不可变版本（级联建模板 + 发布者；依赖 auto-seed 的 basic 环境）"""
    from app.models import NotebookTemplateVersion

    if template_id is None:
        template_id = make_notebook_template(db_session_factory)
    publisher = get_or_create_user(db_session_factory, published_by_username, "teacher")
    with db_session_factory() as db:
        version = NotebookTemplateVersion(
            template_id=template_id,
            version_number=version_number,
            sha256=kwargs.pop("sha256", "b" * 64),
            published_by_id=publisher.id,
            **kwargs,
        )
        db.add(version)
        db.commit()
        db.refresh(version)
        return version.id


def grant_judge_claim(db, model, object_id, job_type="assignment"):
    """测试前置：让当前会话看起来已经认领该判题任务。

    生产序列是 claim_job()（写 claim_token + 记入 db.info）之后才进入判题函数；
    直接调用判题函数的测试必须先把这两个前置补齐，否则结果写入会被认领栅栏拒绝。
    """
    token = "test-claim-token"
    row = db.get(model, object_id)
    row.grading_status = "running"
    row.claim_token = token
    db.commit()
    db.info.setdefault("judge_claims", {})[(job_type, object_id)] = token
    return token


def executed_update_values(mock_db, model):
    """收集 MagicMock 会话上针对某模型执行过的 UPDATE 绑定参数。

    结果写入已从"ORM 赋值"改为"条件 UPDATE"（认领栅栏），
    全 mock 的 db 无法反映内存对象的副作用，因此这类测试改为断言发出的 SQL 值。
    """
    from sqlalchemy import Update

    collected = []
    for call in mock_db.execute.call_args_list:
        stmt = call.args[0] if call.args else None
        if isinstance(stmt, Update) and getattr(stmt.table, "name", None) == model.__tablename__:
            try:
                collected.append(stmt.compile().params)
            except Exception:  # pragma: no cover - 仅测试辅助
                continue
    return collected
