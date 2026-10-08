"""Assignment execution rechecks audience after an InnoDB policy-lock wait."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, local

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from app.api import judge
from app.models import (Assignment, AssignmentAudienceStudent, Course, CourseEnrollment,
                        JudgeQuestion, Submission, User)
from app.schemas import SubmissionCreate
from app.services.audience_service import effective_student_ids, save_audience
from app.services.offering_service import require_task_manager
from conftest import seed_basic_environment
from test_school_write_scope import cache_scope, make_scope_case
from test_teaching_readonly_mysql import (
    _connection_id,
    _sql_listeners,
    _wait,
    mysql_readonly_factory,  # noqa: F401
)

pytestmark = pytest.mark.no_auto_env_seed


def _make_assignment_case(factory, restriction):
    case = make_scope_case(factory)
    seed_basic_environment(factory)
    with factory() as db:
        db.add(CourseEnrollment(course_id=case.course, student_id=case.student, status="enrolled"))
        assignment = Assignment(course_id=case.course, offering_id=case.offering,
                                title="Audience current-read race", status="published",
                                audience_mode="whitelist_only" if restriction == "remove_whitelist" else "all_enrolled")
        db.add(assignment)
        db.flush()
        if restriction == "remove_whitelist":
            db.add(AssignmentAudienceStudent(assignment_id=assignment.id,
                                             student_id=case.student, kind="include"))
        question = JudgeQuestion(assignment_id=assignment.id, title="Public example",
                                 function_name="f", hidden_tests="assert True",
                                 public_cases=[{"args": [], "expected": 1}], grading_mode="legacy")
        db.add(question)
        db.commit()
        case.assignment, case.question = assignment.id, question.id
    return case


@pytest.mark.parametrize("restriction", ["exclude", "remove_whitelist"])
@pytest.mark.parametrize("entrypoint", ["sample", "submit"])
def test_mysql_assignment_write_rejects_audience_revocation_after_wait(
    mysql_readonly_factory, test_settings, redis_client, monkeypatch, restriction, entrypoint,  # noqa: F811
):
    factory = mysql_readonly_factory
    case = _make_assignment_case(factory, restriction)
    cached, held, waiting = Event(), Event(), Event()
    thread = local()
    connections, side_effects = [], []

    def runner(*_args, **_kwargs):
        side_effects.append("runner")
        return "1 passed", "", 0, 1

    def enqueue(*_args, **_kwargs):
        side_effects.append("enqueue")
        return True

    monkeypatch.setattr(judge, "_run_docker_pytest", runner)
    # Keep a regression from ever touching a real daemon or Redis server.
    # Authorization itself and all its database reads remain unpatched.
    monkeypatch.setattr(judge, "resolve_run_image_ref", lambda *_args: "sha256:" + "1" * 64)
    monkeypatch.setattr("app.services.judge_queue.enqueue_job", enqueue)

    def before(_conn, _cursor, statement, _parameters, _context, _many):
        if (getattr(thread, "role", None) == "student"
                and statement.lower().startswith("update account_security_policies")):
            waiting.set()

    def student():
        thread.role = "student"
        with factory() as db:
            connections.append(_connection_id(db))
            actor, cached_scope = cache_scope(db, case, case.student)
            assignment = db.get(Assignment, case.assignment)
            course = db.get(Course, case.course)
            cached_links = list(db.scalars(select(AssignmentAudienceStudent).where(
                AssignmentAudienceStudent.assignment_id == assignment.id)))
            assert case.student in effective_student_ids(
                db, task_type="assignment", task_id=assignment.id, course=course)
            cached.set()
            _wait(held, "teacher did not acquire the teaching policy lock")
            payload = SubmissionCreate(question_id=case.question, code="def f(): return 1")
            try:
                if entrypoint == "sample":
                    judge.sample_run(case.question, payload, db, test_settings, actor)
                else:
                    judge.create_submission(payload, db, test_settings, actor, redis_client)
                return "unexpectedly_allowed"
            except HTTPException as exc:
                db.rollback()
                assert len(cached_scope) == 5
                assert len(cached_links) == (1 if restriction == "remove_whitelist" else 0)
                return exc.status_code, exc.detail["code"]

    def teacher():
        thread.role = "teacher"
        _wait(cached, "student did not establish its prior audience snapshot")
        with factory() as db:
            connections.append(_connection_id(db))
            actor = db.get(User, case.teacher)
            assignment = db.get(Assignment, case.assignment)
            require_task_manager(db, assignment, actor, write=True)
            held.set()
            _wait(waiting, "student did not wait for the teacher's policy transaction")
            save_audience(db, task_type="assignment", task_id=assignment.id,
                          course=db.get(Course, case.course), audience_mode=assignment.audience_mode,
                          audience_class_ids=[], whitelist_student_ids=[],
                          excluded_student_ids=[case.student] if restriction == "exclude" else [],
                          actor_id=actor.id)
            db.commit()

    with _sql_listeners(factory.kw["bind"], before=before), ThreadPoolExecutor(max_workers=2) as pool:
        reading, changing = pool.submit(student), pool.submit(teacher)
        changing.result(timeout=20)
        assert reading.result(timeout=20) == (403, "NOT_IN_ASSIGNMENT_AUDIENCE")
    assert len(set(connections)) == 2
    assert side_effects == []
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Submission)) == 0
        assert case.student not in effective_student_ids(
            db, task_type="assignment", task_id=case.assignment, course=db.get(Course, case.course))
