"""Write admission refreshes cached authority, while reads remain unlocked."""
from datetime import date
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import event, select

from app.models import AcademicTerm, Course, TeachingClass, User
from app.models.teaching import CourseOffering, OfferingEnrollment
from app.services.account_service import lock_policy
from app.services.offering_service import require_offering
from app.services.teaching_query_policy import install_request_scope

pytestmark = pytest.mark.no_auto_env_seed


def make_scope_case(factory):
    with factory() as db:
        users = [User(username=name, real_name=name, role=role, status='active', password_hash='not-login')
                 for name, role in [('scope-teacher', 'teacher'), ('scope-other', 'teacher'), ('scope-student', 'student')]]
        db.add_all(users); db.flush()
        teacher, other, student = users
        term = AcademicTerm(code='scope-term', name='Scope', start_date=date(2026, 9, 1), end_date=date(2027, 1, 1), status='active')
        db.add(term); db.flush()
        klass = TeachingClass(code='scope-class', name='Scope class', academic_term_id=term.id, status='active')
        course = Course(title='Scope course', teacher_id=teacher.id, status='published')
        db.add_all([klass, course]); db.flush()
        offering = CourseOffering(course_id=course.id, academic_term_id=term.id, teaching_class_id=klass.id,
                                  teacher_id=teacher.id, status='active')
        db.add(offering); db.flush()
        member = OfferingEnrollment(offering_id=offering.id, student_id=student.id, status='active',
                                    active_scope_key=f'{course.id}:{term.id}:{student.id}')
        db.add(member); lock_policy(db); db.commit()
        return SimpleNamespace(teacher=teacher.id, other=other.id, student=student.id, term=term.id,
                               klass=klass.id, course=course.id, offering=offering.id, member=member.id)


def cache_scope(db, case, actor_id):
    actor = db.get(User, actor_id)
    install_request_scope(db, actor)
    # Keep strong references: identity-map caching and the RR snapshot both
    # existed before the administrator's authority change.
    cached = [db.get(model, ident) for model, ident in [(CourseOffering, case.offering),
              (AcademicTerm, case.term), (TeachingClass, case.klass), (Course, case.course),
              (OfferingEnrollment, case.member)]]
    return actor, cached


def change_scope(db, case, kind, actor_id):
    lock_policy(db)
    if kind == 'teacher': db.get(CourseOffering, case.offering).teacher_id = case.other
    elif kind == 'member': db.get(OfferingEnrollment, case.member).status = 'transferred'
    elif kind == 'course': db.get(Course, case.course).status = 'draft'
    elif kind == 'term': db.get(AcademicTerm, case.term).status = 'closed'
    elif kind == 'class': db.get(TeachingClass, case.klass).status = 'archived'
    else:
        actor = db.get(User, actor_id)
        if kind == 'disabled': actor.status = 'disabled'
        elif kind == 'role': actor.role = 'admin'
        elif kind == 'session': actor.session_version += 1
        elif kind == 'credential': actor.credential_version += 1


@pytest.mark.parametrize('kind', ['teacher', 'member', 'course', 'term', 'class', 'disabled', 'role', 'session', 'credential'])
def test_write_rechecks_authority_cached_before_admin_commit(db_session_factory, kind):
    case = make_scope_case(db_session_factory)
    actor_id = case.student if kind in ('member', 'course') else case.teacher
    with db_session_factory() as reader:
        actor, cached = cache_scope(reader, case, actor_id)
        with db_session_factory() as writer:
            change_scope(writer, case, kind, actor_id); writer.commit()
        with pytest.raises(HTTPException) as denied:
            require_offering(reader, case.offering, actor, write=True)
        assert denied.value.status_code == (409 if kind in ('term', 'class') else 403)
        assert len(cached) == 5
        reader.rollback()


def test_reading_scope_does_not_lock_policy_or_rows(db_session_factory):
    case = make_scope_case(db_session_factory)
    statements = []
    engine = db_session_factory.kw['bind']
    def before_cursor(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.upper())
    event.listen(engine, 'before_cursor_execute', before_cursor)
    try:
        with db_session_factory() as db:
            actor = db.get(User, case.teacher)
            install_request_scope(db, actor)
            assert require_offering(db, case.offering, actor).id == case.offering
            assert db.scalar(select(CourseOffering.id)) == case.offering
    finally:
        event.remove(engine, 'before_cursor_execute', before_cursor)
    assert not any('FOR UPDATE' in statement or 'UPDATE ACCOUNT_SECURITY_POLICIES' in statement for statement in statements)


@pytest.mark.parametrize('mutation', ['member', 'session', 'revision', 'unpublish', 'logout', None])
def test_cell_execution_releases_metadata_locks_and_rechecks_after_runtime(client, db_session_factory, test_settings, monkeypatch, mutation):
    from app.api.experiments import execute_cell
    from app.models import ExperimentModule, ExperimentRecord
    from app.schemas import ExperimentCellExecuteRequest
    from lab_test_support import make_case, record
    from conftest import seed_basic_environment
    seed_basic_environment(db_session_factory)
    case = make_case(db_session_factory, test_settings)
    ident = record(client, case, test_settings)['id']
    with db_session_factory() as db:
        from fakeredis import FakeRedis
        redis = FakeRedis()
        actor = db.get(User, case.student.id)
        install_request_scope(db, actor)
        db.info['teaching_request_method'] = 'POST'
        db.info['teaching_token_jti'], db.info['teaching_token_redis'] = 'test-logout', redis
        def runtime(_record, _code):
            # The actual runtime may acquire resource leases or an admin may
            # revoke teaching/account authority through another connection.
            assert not db.in_transaction()
            with db_session_factory() as writer:
                lock_policy(writer)
                if mutation == 'member':
                    writer.scalar(select(OfferingEnrollment).where(OfferingEnrollment.offering_id == case.offering_id,
                        OfferingEnrollment.student_id == case.student.id)).status = 'removed'
                elif mutation == 'session': writer.get(User, case.student.id).session_version += 1
                elif mutation == 'revision': writer.get(ExperimentRecord, ident).record_revision += 1
                elif mutation == 'unpublish': writer.get(ExperimentModule, case.module_id).status = 'draft'
                writer.commit()
            if mutation == 'logout': redis.set('blacklist:test-logout', '1')
            return {'outputs': [{'output_type': 'stream', 'text': 'private result'}], 'execution_time_ms': 1}
        manager = SimpleNamespace(execute=runtime)
        monkeypatch.setattr('app.api.experiments.get_kernel_manager', lambda: manager)
        monkeypatch.setattr('app.services.lab_workspace.ensure_record_kernel', lambda *args, **kwargs: None)
        if mutation is None:
            result = execute_cell(ident, 'code', ExperimentCellExecuteRequest(code='print(1)'), db, actor)
            assert result.execution_count == 1
        else:
            with pytest.raises(HTTPException) as denied:
                execute_cell(ident, 'code', ExperimentCellExecuteRequest(code='print(1)'), db, actor)
            assert denied.value.status_code == (401 if mutation == 'logout' else 409 if mutation == 'revision' else 403)
        db.rollback()
    with db_session_factory() as db:
        outputs = db.get(ExperimentRecord, ident).cells_outputs
        assert bool(outputs) is (mutation is None)


@pytest.mark.parametrize('mutation', ['member', 'session', 'revision', 'pending', None])
def test_artifact_copy_releases_policy_then_rechecks_scope_revision_and_quota(client, db_session_factory, test_settings, mutation):
    from io import BytesIO
    from zipfile import ZipFile
    from starlette.datastructures import UploadFile
    from app.api.labs import upload_artifact
    from app.models import ExperimentRecord, LabArtifact, LabRun, StorageObject
    from app.storage import StorageArea, StorageService
    from lab_test_support import make_case, record
    from conftest import seed_basic_environment
    seed_basic_environment(db_session_factory)
    case = make_case(db_session_factory, test_settings)
    initial = record(client, case, test_settings)
    archive = BytesIO()
    with ZipFile(archive, 'w') as zipped:
        zipped.writestr('train.py', 'value=1')
    with db_session_factory() as db:
        actor = db.get(User, case.student.id)
        install_request_scope(db, actor)
        db.info['teaching_request_method'] = 'POST'
        class MutatingStream(BytesIO):
            copied = False
            def read(self, size=-1):
                if not self.copied:
                    self.copied = True
                    assert not db.in_transaction()
                    with db_session_factory() as writer:
                        lock_policy(writer)
                        if mutation == 'member':
                            writer.scalar(select(OfferingEnrollment).where(OfferingEnrollment.offering_id == case.offering_id,
                                OfferingEnrollment.student_id == case.student.id)).status = 'removed'
                        elif mutation == 'session': writer.get(User, actor.id).session_version += 1
                        elif mutation == 'revision': writer.get(ExperimentRecord, initial['id']).record_revision += 1
                        elif mutation == 'pending': writer.add(LabArtifact(owner_id=actor.id, record_id=initial['id'],
                            assessment_id=case.assessment_id, source='external', status='queued', code_snapshot={}))
                        writer.commit()
                return super().read(size)
        file = UploadFile(MutatingStream(archive.getvalue()), filename='artifact.zip')
        if mutation is None:
            result = upload_artifact(initial['id'], file, initial['record_revision'], db, actor, test_settings)
            assert result['status'] == 'queued' and result['validation_run_id']
        else:
            with pytest.raises(HTTPException) as denied:
                upload_artifact(initial['id'], file, initial['record_revision'], db, actor, test_settings)
            assert denied.value.status_code == (422 if mutation == 'pending' else 409 if mutation == 'revision' else 403)
    with db_session_factory() as db:
        assert len(db.scalars(select(StorageObject).where(StorageObject.namespace == 'lab-artifacts')).all()) == (1 if mutation is None else 0)
        assert len(db.scalars(select(LabRun).where(LabRun.kind == 'artifact_validate')).all()) == (1 if mutation is None else 0)
    storage = StorageService.from_settings(test_settings)
    assert bool(list(storage.list_objects(StorageArea.ARTIFACTS, 'artifacts'))) is (mutation is None)
