"""A cached authorization snapshot cannot republish withdrawn shared content."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident

from fastapi import HTTPException
import pytest
from sqlalchemy import event, select, text

from app.api.offerings import ExperimentRelease, release_experiment
from app.models import ExperimentModule, ExperimentRecord, LabAssessment, NotebookTemplate, User
from app.models.teaching import CourseOffering, OfferingExperiment
from app.services.account_service import lock_policy
from app.services.teaching_query_policy import install_request_scope
from conftest import seed_basic_environment
from lab_test_support import make_case
from test_school_write_scope_mysql import mysql_scope_factory as reserved_mysql_factory

mysql_scope_factory = reserved_mysql_factory
pytestmark = pytest.mark.no_auto_env_seed
CHANGES = ['module', 'shared', 'template', 'assessment', 'late_record', 'current_delivery']


def prepare(factory, settings):
    seed_basic_environment(factory)
    case = make_case(factory, settings)
    with factory() as db:
        db.get(CourseOffering, case.other_offering_id).teacher_id = case.other_teacher.id
        source = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == case.offering_id))
        target = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == case.other_offering_id))
        case.source_delivery, case.target_delivery = source.id, target.id
        lock_policy(db)
        db.commit()
    return case


def cached_request(db, case, change):
    actor = db.get(User, case.other_teacher.id)
    install_request_scope(db, actor)
    cached = [db.get(model, ident) for model, ident in [
        (ExperimentModule, case.module_id), (NotebookTemplate, case.template_id),
        (LabAssessment, case.assessment_id), (OfferingExperiment, case.source_delivery),
        (OfferingExperiment, case.target_delivery)]]
    payload = ExperimentRelease(entry_kind='module', entry_id=case.module_id,
        assessment_id=None if change == 'late_record' else case.assessment_id)
    return actor, cached, payload


def withdraw(db, case, change):
    lock_policy(db)
    if change == 'module': db.get(ExperimentModule, case.module_id).status = 'draft'
    elif change == 'shared':
        # Remove all course endorsements, including the cached target one.
        for row in db.scalars(select(OfferingExperiment).where(OfferingExperiment.entry_id == case.module_id)):
            row.status = 'draft'
    elif change == 'template': db.get(NotebookTemplate, case.template_id).current_version_id = None
    elif change == 'assessment': db.get(LabAssessment, case.assessment_id).status = 'draft'
    else:
        if change == 'current_delivery':
            db.get(OfferingExperiment, case.target_delivery).assessment_id = None
        db.add(ExperimentRecord(module_id=case.module_id, student_id=case.other_student.id,
            offering_id=case.other_offering_id, scope_key=f'offering:{case.other_offering_id}',
            template_version_id=case.version_id, environment_version_id=case.environment_id, status='started'))


def denied_release(db, case, actor, cached, payload, change):
    with pytest.raises(HTTPException) as failure:
        release_experiment(case.other_offering_id, payload, db, actor)
    assert failure.value.status_code == (403 if change == 'shared' else 409 if change in ('late_record', 'current_delivery') else 422)
    assert len(cached) == 5
    db.rollback()
    target = db.get(OfferingExperiment, case.target_delivery)
    assert target.status == ('draft' if change == 'shared' else 'published')
    assert target.assessment_id == (None if change == 'current_delivery' else case.assessment_id)


@pytest.mark.parametrize('change', CHANGES)
def test_release_refreshes_cached_content_and_delivery_after_other_commit(db_session_factory, test_settings, change):
    case = prepare(db_session_factory, test_settings)
    with db_session_factory() as reader:
        actor, cached, payload = cached_request(reader, case, change)
        with db_session_factory() as writer:
            withdraw(writer, case, change)
            writer.commit()
        denied_release(reader, case, actor, cached, payload, change)


@pytest.mark.parametrize('change', CHANGES)
def test_mysql_release_waits_then_rejects_old_repeatable_read_snapshot(mysql_scope_factory, test_settings, change):
    factory = mysql_scope_factory
    case = prepare(factory, test_settings)
    cached, held, waiting = Event(), Event(), Event()
    reader_thread, connections = [], []
    engine = factory.kw['bind']

    def cursor_started(_connection, _cursor, statement, _parameters, _context, _executemany):
        if reader_thread and get_ident() == reader_thread[0] and statement.lower().startswith('update account_security_policies'):
            waiting.set()

    event.listen(engine, 'before_cursor_execute', cursor_started)

    def reader():
        reader_thread.append(get_ident())
        with factory() as db:
            db.execute(text('SET SESSION innodb_lock_wait_timeout=5'))
            connections.append(db.scalar(text('SELECT CONNECTION_ID()')))
            actor, strong_cache, payload = cached_request(db, case, change)
            cached.set()
            assert held.wait(timeout=10)
            denied_release(db, case, actor, strong_cache, payload, change)

    def writer():
        assert cached.wait(timeout=10)
        with factory() as db:
            connections.append(db.scalar(text('SELECT CONNECTION_ID()')))
            lock_policy(db)
            held.set()
            assert waiting.wait(timeout=10)
            withdraw(db, case, change)
            db.commit()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reading, writing = pool.submit(reader), pool.submit(writer)
            writing.result(timeout=20)
            reading.result(timeout=20)
    finally:
        event.remove(engine, 'before_cursor_execute', cursor_started)
    assert len(set(connections)) == 2
