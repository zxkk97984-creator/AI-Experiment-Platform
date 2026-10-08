"""Current-read and lock-order proofs using independent MySQL transactions.

Opt in with DAI_TEST_GRADEBOOK_MYSQL_URL. Only the explicitly reserved empty
schema dai_school_gradebook_verify is accepted; application databases are never used.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
import os
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url

from app.api.offerings import OfferingUpdate, Transfer, transfer, update_offering
from app.database import Base, create_engine_from_url, sessionmaker_for_engine
from app.models import (AcademicTerm, Course, EnvironmentProfile, EnvironmentVersion, ExperimentModule,
    ExperimentRecord, ExperimentSubmission, LabAssessment, LabGradeRevision, LabRecord, LabRun, LabSubmission,
    NotebookTemplate, NotebookTemplateVersion, TeachingClass, User)
from app.models.gradebook import GradebookSnapshot
from app.models.teaching import CourseOffering, OfferingEnrollment, OfferingExperiment
from app.schemas import ExperimentSubmitRequest
from app.schemas.gradebook import AdjustmentUpdate, ConfigUpdate, EntriesUpdate, RevisionAction
from app.schemas.lab import LabAssessmentConfig
from app.services import account_service as accounts, gradebook_service as grades
from app.services.lab_submission_service import submit_lab
from app.services.time_utils import utc_now

pytestmark = pytest.mark.no_auto_env_seed


@pytest.fixture()
def mysql_factory():
    raw = os.environ.get('DAI_TEST_GRADEBOOK_MYSQL_URL')
    if not raw:
        pytest.skip('Requires dedicated empty dai_school_gradebook_verify MySQL schema')
    url = make_url(raw)
    if url.get_backend_name() != 'mysql' or url.database != 'dai_school_gradebook_verify':
        pytest.fail('Refusing gradebook concurrency tests outside reserved disposable schema')
    engine = create_engine_from_url(raw).execution_options(isolation_level='REPEATABLE READ')
    if inspect(engine).get_table_names():
        engine.dispose()
        pytest.fail('Refusing to modify nonempty concurrency verification schema')
    Base.metadata.create_all(engine)
    factory = sessionmaker_for_engine(engine)
    try:
        yield factory
    finally:
        with engine.begin() as connection:
            connection.execute(text('UPDATE notebook_templates SET current_version_id = NULL'))
            connection.execute(text('UPDATE environment_profiles SET current_version_id = NULL'))
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def case(mysql_factory):
    with mysql_factory() as db:
        users = [User(username=name, real_name=name, role=role, status='active', password_hash='non-login-fixture')
            for name, role in [('teacher', 'teacher'), ('replacement', 'teacher'), ('admin', 'admin'), ('student-one', 'student'), ('student-two', 'student')]]
        db.add_all(users); db.flush()
        teacher, other, admin, one, two = users
        term = AcademicTerm(code='TERM', name='Term', start_date=date(2026, 9, 1), end_date=date(2027, 1, 1), status='active')
        db.add(term); db.flush()
        course = Course(title='Shared', teacher_id=teacher.id, status='published')
        db.add(course); db.flush()
        classes = [TeachingClass(academic_term_id=term.id, code=f'C{i}', name=f'Class {i}', status='active') for i in (1, 2)]
        db.add_all(classes); db.flush()
        offerings = [CourseOffering(course_id=course.id, academic_term_id=term.id, teaching_class_id=klass.id,
            teacher_id=teacher.id if index == 0 else other.id, status='active') for index, klass in enumerate(classes)]
        db.add_all(offerings); db.flush()
        for student in (one, two):
            db.add(OfferingEnrollment(offering_id=offerings[0].id, student_id=student.id, status='active',
                active_scope_key=f'{course.id}:{term.id}:{student.id}'))
        db.commit()
        config = [{'key': 'main', 'title': 'Main', 'weight': '100', 'columns': [
            {'key': 'manual', 'title': 'Manual', 'weight': '100', 'source_kind': 'manual'}]}]
        grades.save_config(db, offerings[0].id, teacher, ConfigUpdate(expected_revision=0, categories=config))
        db.commit()
        grades.update_entries(db, offerings[0].id, teacher, EntriesUpdate(expected_revision=1,
            entries=[{'student_id': s.id, 'column_key': 'manual', 'state': 'scored', 'score': '80'} for s in (one, two)]))
        db.commit()
        return SimpleNamespace(teacher=teacher.id, other=other.id, admin=admin.id, one=one.id, two=two.id,
            offering=offerings[0].id, destination=offerings[1].id, term=term.id, klass=classes[0].id, config=config)


def stale_publication(factory, case, mutation, *, expected_revision=2):
    reviewed, changed = Event(), Event()
    connection_ids = []
    def reviewer():
        with factory() as db:
            db.execute(text('SET SESSION innodb_lock_wait_timeout=5'))
            connection_ids.append(db.scalar(text('SELECT CONNECTION_ID()')))
            actor = db.get(User, case.teacher)
            preview = grades.calculate(db, grades.book_for(db, case.offering))
            reviewed.set()
            assert changed.wait(timeout=10)
            try:
                snapshot = grades.publish(db, case.offering, actor, RevisionAction(
                    expected_revision=expected_revision, expected_preview_hash=preview['preview_hash'], reason='Reviewed old preview'))
                db.commit()
                return 'published', snapshot.id
            except HTTPException as exc:
                db.rollback()
                return exc.detail['code'], None
    def writer():
        assert reviewed.wait(timeout=10)
        with factory() as db:
            connection_ids.append(db.scalar(text('SELECT CONNECTION_ID()')))
            mutation(db)
            db.commit()
        changed.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(reviewer), pool.submit(writer)
        second.result(timeout=20)
        result = first.result(timeout=20)
    assert len(set(connection_ids)) == 2
    return result


def test_mysql_publish_detects_transfer_committed_after_preview(mysql_factory, case):
    def move(db):
        transfer(case.offering, case.two, Transfer(destination_offering_id=case.destination, reason='Explicit test transfer'),
            db=db, user=db.get(User, case.admin))
    result, _ = stale_publication(mysql_factory, case, move)
    assert result == 'GRADEBOOK_PREVIEW_CHANGED'
    with mysql_factory() as db:
        assert db.scalar(select(func.count()).select_from(GradebookSnapshot)) == 0
        assert grades.book_for(db, case.offering).revision == 2


@pytest.mark.parametrize('mutation', ['reassign', 'disable', 'close_term', 'archive_class'])
def test_mysql_publish_rechecks_current_authority_after_another_commit(mysql_factory, case, mutation):
    def change(db):
        admin = db.get(User, case.admin)
        if mutation == 'reassign':
            update_offering(case.offering, OfferingUpdate(teacher_id=case.other, reason='Teacher revoked'), db=db, user=admin)
        elif mutation == 'disable':
            accounts.update_identity(db, case.teacher, {'status': 'disabled', 'reason': 'Teacher disabled'}, admin)
        else:
            accounts.lock_policy(db)
            db.get(AcademicTerm if mutation == 'close_term' else TeachingClass,
                   case.term if mutation == 'close_term' else case.klass).status = 'closed' if mutation == 'close_term' else 'archived'
    result, _ = stale_publication(mysql_factory, case, change)
    assert result == ('OFFERING_READ_ONLY' if mutation in ('close_term', 'archive_class') else 'FORBIDDEN')
    with mysql_factory() as db:
        assert db.scalar(select(func.count()).select_from(GradebookSnapshot)) == 0


@pytest.mark.parametrize('mutation', ['config', 'entry', 'adjustment'])
def test_mysql_publish_cannot_reuse_stale_cached_values_with_new_revision(mysql_factory, case, mutation):
    def change(db):
        actor = db.get(User, case.teacher)
        if mutation == 'config':
            config = deepcopy(case.config); config[0]['title'] = 'New reviewed configuration'
            grades.save_config(db, case.offering, actor, ConfigUpdate(expected_revision=2, categories=config))
        elif mutation == 'entry':
            grades.update_entries(db, case.offering, actor, EntriesUpdate(expected_revision=2,
                entries=[{'student_id': case.one, 'column_key': 'manual', 'state': 'scored', 'score': '90'}]))
        else:
            grades.adjust(db, case.offering, actor, AdjustmentUpdate(expected_revision=2,
                student_id=case.one, score='90', reason='Approved correction'))
    result, _ = stale_publication(mysql_factory, case, change, expected_revision=3)
    assert result == 'GRADEBOOK_PREVIEW_CHANGED'
    with mysql_factory() as db:
        assert grades.book_for(db, case.offering).revision == 3
        assert db.scalar(select(func.count()).select_from(GradebookSnapshot)) == 0


def test_mysql_two_simultaneous_publish_requests_only_one_commits(mysql_factory, case):
    barrier = Barrier(2)
    def publish():
        with mysql_factory() as db:
            db.execute(text('SET SESSION innodb_lock_wait_timeout=5'))
            actor = db.get(User, case.teacher)
            preview = grades.calculate(db, grades.book_for(db, case.offering))
            barrier.wait(timeout=10)
            try:
                grades.publish(db, case.offering, actor, RevisionAction(expected_revision=2,
                    expected_preview_hash=preview['preview_hash'], reason='Parallel publish'))
                db.commit()
                return 'published'
            except HTTPException as exc:
                db.rollback()
                return exc.detail['code']
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish), pool.submit(publish)]
        assert sorted(future.result(timeout=20) for future in futures) == ['GRADEBOOK_REVISION_CONFLICT', 'published']
    with mysql_factory() as db:
        assert db.scalar(select(func.count()).select_from(GradebookSnapshot)) == 1
        assert grades.book_for(db, case.offering).revision == 3


def attach_published_notebook(factory, case):
    """Actual queue admission data; no Docker or fake service functions involved."""
    with factory() as db:
        transfer(case.offering, case.two, Transfer(destination_offering_id=case.destination, reason='Single student concurrency fixture'),
            db=db, user=db.get(User, case.admin))
        profile = EnvironmentProfile(slug='db-concurrency', display_name='DB concurrency', status='active')
        db.add(profile); db.flush()
        environment = EnvironmentVersion(profile_id=profile.id, version_number=1, status='available',
            base_image_ref='registry.invalid/fixture@sha256:' + 'a' * 64,
            image_digest='registry.invalid/fixture@sha256:' + 'a' * 64, minimum_memory_mb=512, manifest_sha256='a' * 64)
        db.add(environment); db.flush()
        profile.current_version_id = environment.id
        cells = [{'id': 'cell', 'type': 'code', 'source': 'value = 1', 'student_editable': True}]
        template = NotebookTemplate(name='Frozen notebook', owner_id=case.teacher, status='published', draft_cells=cells,
            draft_environment_version_id=environment.id)
        db.add(template); db.flush()
        version = NotebookTemplateVersion(template_id=template.id, version_number=1, sha256='b' * 64,
            cells=cells, cell_order=['cell'], environment_version_id=environment.id, published_by_id=case.teacher)
        db.add(version); db.flush()
        template.current_version_id = version.id
        module = ExperimentModule(name='Queue concurrency', owner_id=case.teacher, template_id=template.id, status='published')
        db.add(module); db.flush()
        config = LabAssessmentConfig(criteria=[{'id': 'check', 'name': 'Check', 'description': 'Notebook check',
            'source': 'check', 'max_score': 100}], confirmed=True, check_script='print(1)').model_dump()
        assessment = LabAssessment(owner_id=case.teacher, entry_key=f'module:{module.id}', version=1,
            status='published', config=config, template_version_id=version.id, config_hash='c' * 64)
        db.add(assessment); db.flush()
        delivery = OfferingExperiment(offering_id=case.offering, entry_kind='module', entry_id=module.id,
            template_version_id=version.id, assessment_id=assessment.id, status='published')
        db.add(delivery); db.flush()
        record = ExperimentRecord(module_id=module.id, offering_id=case.offering, scope_key=f'offering:{case.offering}',
            student_id=case.one, template_version_id=version.id, environment_version_id=environment.id,
            record_revision=2, cells_sources={'cell': 'value = 2'}, cells_outputs={}, status='submitted')
        db.add(record); db.flush()
        old = ExperimentSubmission(record_id=record.id, attempt_number=1, client_request_id=str(uuid4()),
            cells_snapshot={'cell': 'value = 1'}, outputs_snapshot={}, score=80, submitted_at=utc_now())
        db.add(old); db.flush()
        grade = LabGradeRevision(submission_id=old.id, assessment_id=assessment.id, source='auto', total=80,
            items=[], metrics={}, published_at=utc_now())
        db.add(grade); db.flush()
        db.add(LabSubmission(submission_id=old.id, assessment_id=assessment.id, submission_hash='d' * 64,
            quota_charged=True, published_grade_id=grade.id))
        db.add(LabRecord(record_id=record.id, assessment_id=assessment.id, selected_submission_id=old.id))
        db.flush()
        config = deepcopy(case.config)
        config[0]['columns'][0].update(source_kind='experiment', source_id=delivery.id)
        grades.save_config(db, case.offering, db.get(User, case.teacher), ConfigUpdate(expected_revision=2, categories=config))
        db.commit()
        return record.id


@pytest.mark.parametrize('first', ['submit', 'publish'])
def test_mysql_formal_submission_and_gradebook_publish_share_short_transaction_order(mysql_factory, case, test_settings, first):
    record_id = attach_published_notebook(mysql_factory, case)
    with mysql_factory() as db:
        preview = grades.calculate(db, grades.book_for(db, case.offering))
    barrier, held, contender = Barrier(2), Event(), Event()
    def operation(kind):
        with mysql_factory() as db:
            db.execute(text('SET SESSION innodb_lock_wait_timeout=5'))
            actor = db.get(User, case.one if kind == 'submit' else case.teacher)
            record = db.get(ExperimentRecord, record_id)
            barrier.wait(timeout=10)
            if kind == first:
                accounts.lock_policy(db)
                held.set()
                assert contender.wait(timeout=10)
            else:
                assert held.wait(timeout=10)
                contender.set()
            try:
                if kind == 'submit':
                    submit_lab(db, record, actor, ExperimentSubmitRequest(client_request_id=uuid4(), expected_record_revision=2), test_settings)
                    result = 'submitted'
                else:
                    grades.publish(db, case.offering, actor, RevisionAction(expected_revision=3,
                        expected_preview_hash=preview['preview_hash'], reason='Concurrent class publication'))
                    result = 'published'
                db.commit()
                return result
            except HTTPException as exc:
                db.rollback()
                return exc.detail['code']
    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted, published = pool.submit(operation, 'submit'), pool.submit(operation, 'publish')
        assert submitted.result(timeout=20) == 'submitted'
        assert published.result(timeout=20) == ('GRADEBOOK_PREVIEW_CHANGED' if first == 'submit' else 'published')
    with mysql_factory() as db:
        assert db.scalar(select(func.count()).select_from(ExperimentSubmission).where(ExperimentSubmission.record_id == record_id)) == 2
        assert grades.calculate(db, grades.book_for(db, case.offering))['blockers']
    # Complete the queued DB fixture and verify highest-score selection permits
    # a new publication; the earlier official publication remains immutable.
    from app.services.lab_grading import publish_evaluation
    with mysql_factory() as db:
        state = db.scalar(select(LabSubmission).join(ExperimentSubmission,
            ExperimentSubmission.id == LabSubmission.submission_id)
            .where(ExperimentSubmission.record_id == record_id).order_by(ExperimentSubmission.attempt_number.desc()))
        run = db.get(LabRun, state.current_run_id)
        accounts.lock_policy(db)
        run.status = 'succeeded'
        publish_evaluation(db, run, {'total': 90, 'items': [], 'metrics': {}, 'feedback': 'Completed controlled fixture'})
        db.commit()
    with mysql_factory() as db:
        book = grades.book_for(db, case.offering)
        current = grades.calculate(db, book)
        assert not current['blockers'] and current['rows'][0]['final_score'] == '90.00'
        result = grades.publish(db, case.offering, db.get(User, case.teacher), RevisionAction(expected_revision=book.revision,
            expected_preview_hash=current['preview_hash'], reason='All formal results completed'))
        db.commit()
        assert result.payload['rows'][0]['final_score'] == '90.00'
        snapshots = db.scalars(select(GradebookSnapshot).order_by(GradebookSnapshot.revision)).all()
        assert len(snapshots) == (1 if first == 'submit' else 2)
        if first == 'publish':
            assert snapshots[0].payload['rows'][0]['final_score'] == '80.00'
