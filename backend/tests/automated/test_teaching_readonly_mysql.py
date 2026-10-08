"""Real InnoDB races at teaching closure and exam answer boundaries.

Only DAI_TEST_READONLY_MYSQL_URL pointing at the reserved, empty local schema
is accepted. Application URLs are never used to select the test database.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import os
from threading import Event, local

from fastapi import HTTPException
import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.engine import make_url

from app.api.academics import close_term
from app.api.exams import put_answers_batch
from app.database import Base, create_engine_from_url, sessionmaker_for_engine
from app.models import AcademicTerm, Exam, ExamAnswer, ExamGrade, ExamQuestion, ExamSubmission, User
from app.schemas import ExamAnswerBatchRequest
from app.services.account_service import lock_policy
from app.services.exam_service import _auto_submit, save_answer, start_exam
from app.services.teaching_query_policy import install_request_scope
from app.services.time_utils import utc_now
from test_school_write_scope import cache_scope, make_scope_case

pytestmark = pytest.mark.no_auto_env_seed


@pytest.fixture()
def mysql_readonly_factory():
    raw = os.environ.get("DAI_TEST_READONLY_MYSQL_URL")
    if not raw:
        pytest.skip("Requires reserved disposable dai_school_readonly_verify MySQL schema")
    url = make_url(raw)
    if (url.get_backend_name() != "mysql"
            or url.database != "dai_school_readonly_verify"
            or url.host not in {"127.0.0.1", "localhost", "::1"}):
        pytest.fail("Refusing readonly concurrency tests outside the reserved local schema")
    engine = create_engine_from_url(raw).execution_options(isolation_level="REPEATABLE READ")
    if inspect(engine).get_table_names():
        engine.dispose()
        pytest.fail("Refusing to modify a nonempty readonly concurrency schema")
    Base.metadata.create_all(engine)
    factory = sessionmaker_for_engine(engine)
    try:
        yield factory
    finally:
        with engine.begin() as conn:
            conn.execute(text("UPDATE notebook_templates SET current_version_id = NULL"))
            conn.execute(text("UPDATE environment_profiles SET current_version_id = NULL"))
        Base.metadata.drop_all(engine)
        engine.dispose()


def _make_exam_case(factory, *, started=False):
    case = make_scope_case(factory)
    with factory() as db:
        admin = User(username="readonly-admin", real_name="Administrator", role="admin",
                     status="active", password_hash="not-login")
        exam = Exam(course_id=case.course, offering_id=case.offering, title="Readonly race",
                    status="published", audience_mode="all_enrolled", duration_minutes=60)
        db.add_all([admin, exam])
        db.flush()
        questions = [ExamQuestion(exam_id=exam.id, question_type="single_choice", prompt=f"Question {i}",
                                 options={"A": "correct", "B": "incorrect"},
                                 correct_answer={"correct": ["A"]}, points=10, order_index=i)
                     for i in range(2)]
        db.add_all(questions)
        db.flush()
        case.admin, case.exam = admin.id, exam.id
        case.questions = [question.id for question in questions]
        if started:
            now = utc_now()
            submission = ExamSubmission(exam_id=exam.id, student_id=case.student, status="started",
                                        started_at=now, expires_at=now + timedelta(hours=1))
            db.add(submission)
            db.flush()
            answers = [ExamAnswer(submission_id=submission.id, question_id=question.id,
                                  selected_options=["B"], version=1) for question in questions]
            db.add_all(answers)
            db.flush()
            case.submission = submission.id
            case.answers = [answer.id for answer in answers]
            case.expired_now = now + timedelta(hours=2)
        db.commit()
    return case


def _wait(signal, description):
    assert signal.wait(timeout=8), description


def _connection_id(db):
    db.execute(text("SET SESSION innodb_lock_wait_timeout=10"))
    return db.scalar(text("SELECT CONNECTION_ID()"))


@contextmanager
def _sql_listeners(engine, *, before=None, after=None):
    listeners = [("before_cursor_execute", before), ("after_cursor_execute", after)]
    for name, callback in listeners:
        if callback is not None:
            event.listen(engine, name, callback)
    try:
        yield
    finally:
        for name, callback in listeners:
            if callback is not None:
                event.remove(engine, name, callback)


@pytest.mark.parametrize("first", ["close", "start"])
def test_mysql_close_and_start_serialize_after_old_snapshot(mysql_readonly_factory, first):
    factory = mysql_readonly_factory
    case = _make_exam_case(factory)
    primed, held, waiting = Event(), Event(), Event()
    thread = local()
    connections = []

    def before(_conn, _cursor, statement, _parameters, _context, _many):
        if (getattr(thread, "role", None) == "follower"
                and statement.lower().startswith("update account_security_policies")):
            waiting.set()

    def follower():
        thread.role = "follower"
        with factory() as db:
            connections.append(_connection_id(db))
            actor_id = case.student if first == "close" else case.admin
            actor, cached = cache_scope(db, case, actor_id)
            exam = db.get(Exam, case.exam)
            assert db.scalar(select(ExamSubmission.id).where(ExamSubmission.exam_id == case.exam)) is None
            primed.set()
            _wait(held, "first transaction did not acquire policy lock")
            try:
                if first == "close":
                    start_exam(exam, actor, db)
                else:
                    close_term(case.term, db, actor)
                return "unexpectedly_allowed"
            except HTTPException as exc:
                db.rollback()
                assert len(cached) == 5
                return exc.status_code, exc.detail["code"]

    def leader():
        thread.role = "leader"
        _wait(primed, "follower did not establish its RR snapshot")
        with factory() as db:
            connections.append(_connection_id(db))
            actor = db.get(User, case.admin if first == "close" else case.student)
            install_request_scope(db, actor)
            lock_policy(db)
            held.set()
            _wait(waiting, "follower did not wait for the teaching policy lock")
            if first == "close":
                close_term(case.term, db, actor)
            else:
                start_exam(db.get(Exam, case.exam), actor, db)

    with _sql_listeners(factory.kw["bind"], before=before), ThreadPoolExecutor(max_workers=2) as pool:
        following, leading = pool.submit(follower), pool.submit(leader)
        leading.result(timeout=20)
        expected = "OFFERING_READ_ONLY" if first == "close" else "EXAMS_IN_PROGRESS"
        assert following.result(timeout=20) == (409, expected)
    assert len(set(connections)) == 2
    with factory() as db:
        assert db.get(AcademicTerm, case.term).status == ("closed" if first == "close" else "active")
        submissions = list(db.scalars(select(ExamSubmission).where(ExamSubmission.exam_id == case.exam)))
        assert len(submissions) == (0 if first == "close" else 1)
        if submissions:
            assert submissions[0].status == "started"


@pytest.mark.parametrize("first", ["save", "auto_submit"])
def test_mysql_save_and_auto_submit_preserve_answer_and_grade(mysql_readonly_factory, first):
    factory = mysql_readonly_factory
    case = _make_exam_case(factory, started=True)
    primed, held, waiting = Event(), Event(), Event()
    thread = local()
    connections = []

    def before(_conn, _cursor, statement, _parameters, _context, _many):
        sql = statement.lower()
        if (getattr(thread, "role", None) == "follower" and "exam_submissions" in sql
                and ("for update" in sql or sql.startswith("update exam_submissions"))):
            waiting.set()

    def after(_conn, _cursor, statement, _parameters, _context, _many):
        sql = statement.lower()
        if (getattr(thread, "role", None) == "leader" and not held.is_set()
                and "exam_submissions" in sql and "for update" in sql):
            held.set()
            _wait(waiting, "competing operation did not reach the locked submission")

    def operation(kind, role):
        thread.role = role
        with factory() as db:
            connections.append(_connection_id(db))
            actor = db.get(User, case.student)
            if kind == "save":
                install_request_scope(db, actor)
            submission = db.get(ExamSubmission, case.submission)
            cached_answer = db.get(ExamAnswer, case.answers[0])
            assert cached_answer.selected_options == ["B"]
            if role == "follower":
                primed.set()
                _wait(held, "first operation did not lock the submission")
            else:
                _wait(primed, "competing operation did not establish its old snapshot")
                if kind == "auto_submit":
                    db.scalar(select(ExamSubmission).where(ExamSubmission.id == case.submission).with_for_update())
            if kind == "auto_submit":
                _auto_submit(submission, db, case.expired_now)
                return "submitted"
            try:
                answer = save_answer(db, case.exam, case.questions[0], actor,
                                     {"selected_options": ["A"], "expected_version": 1})
                return "saved", answer.version
            except HTTPException as exc:
                db.rollback()
                return "denied", exc.detail["code"]

    other = "auto_submit" if first == "save" else "save"
    with _sql_listeners(factory.kw["bind"], before=before, after=after), ThreadPoolExecutor(max_workers=2) as pool:
        following = pool.submit(operation, other, "follower")
        leading = pool.submit(operation, first, "leader")
        results = {first: leading.result(timeout=20), other: following.result(timeout=20)}
    assert len(set(connections)) == 2
    assert results["auto_submit"] == "submitted"
    assert results["save"] == (("saved", 2) if first == "save" else ("denied", "EXAM_NOT_STARTED"))
    with factory() as db:
        answer = db.get(ExamAnswer, case.answers[0])
        expected_score = 10 if first == "save" else 0
        assert answer.selected_options == (["A"] if first == "save" else ["B"])
        assert answer.version == (2 if first == "save" else 1)
        assert answer.score == expected_score
        submission = db.get(ExamSubmission, case.submission)
        assert submission.status == "graded"
        assert submission.score == expected_score
        grades = list(db.scalars(select(ExamGrade).where(ExamGrade.exam_id == case.exam)))
        assert len(grades) == 1 and grades[0].score == expected_score


@pytest.mark.parametrize("close_after_submit", [False, True])
def test_mysql_batch_relocks_after_each_answer_commit(mysql_readonly_factory, close_after_submit):
    factory = mysql_readonly_factory
    case = _make_exam_case(factory, started=True)
    first_saved, intervened = Event(), Event()
    connections = []

    def saver():
        with factory() as db:
            connections.append(_connection_id(db))
            actor = db.get(User, case.student)
            install_request_scope(db, actor)
            db.info["teaching_request_method"] = "PUT"
            # The session event runs after MySQL has released the first answer's
            # locks but before the batch is allowed to attempt its second item.
            def after_commit(_session):
                if not first_saved.is_set():
                    first_saved.set()
                    _wait(intervened, "automatic submission did not finish between batch items")
            event.listen(db, "after_commit", after_commit)
            try:
                payload = ExamAnswerBatchRequest(answers=[
                    {"question_id": ident, "selected_options": ["A"], "expected_version": 1}
                    for ident in case.questions])
                return put_answers_batch(case.exam, payload, db, actor)
            finally:
                event.remove(db, "after_commit", after_commit)

    def submitter():
        _wait(first_saved, "batch did not commit its first answer")
        with factory() as db:
            connections.append(_connection_id(db))
            _auto_submit(db.get(ExamSubmission, case.submission), db, case.expired_now)
            if close_after_submit:
                close_term(case.term, db, db.get(User, case.admin))
        intervened.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        saving, submitting = pool.submit(saver), pool.submit(submitter)
        submitting.result(timeout=20)
        result = saving.result(timeout=20)
    assert len(set(connections)) == 2
    assert result["results"][0]["ok"] is True
    assert result["results"][1]["ok"] is False
    assert result["results"][1]["code"] == ("OFFERING_READ_ONLY" if close_after_submit else "EXAM_NOT_STARTED")
    with factory() as db:
        first_answer, second_answer = [db.get(ExamAnswer, ident) for ident in case.answers]
        assert first_answer.selected_options == ["A"] and first_answer.version == 2
        assert second_answer.selected_options == ["B"] and second_answer.version == 1
        submission = db.get(ExamSubmission, case.submission)
        assert submission.status == "graded" and submission.score == 10
        assert db.get(AcademicTerm, case.term).status == ("closed" if close_after_submit else "active")
