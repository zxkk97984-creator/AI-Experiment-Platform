"""Real parallel MySQL checks; opt in only with a dedicated disposable database.

DAI_TEST_MYSQL_URL must name an EMPTY database beginning ``dai_test_account_``.
Never uses DAI_DATABASE_URL or the developer's application database.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url

from app.api.account_management import BatchConfirm
from app.database import Base, create_engine_from_url, sessionmaker_for_engine
from app.models import AdminBatch, AuditEvent, TeachingClass, User, AcademicTerm
from app.services import account_batch_service as batches, account_service as accounts
from conftest import create_user


@pytest.fixture()
def db_session_factory():
    raw = os.environ.get('DAI_TEST_MYSQL_URL')
    if not raw:
        pytest.skip('Requires DAI_TEST_MYSQL_URL pointing to a dedicated empty dai_test_account_* MySQL database')
    url = make_url(raw)
    if url.get_backend_name() != 'mysql' or not (url.database or '').startswith('dai_test_account_'):
        pytest.fail('Refusing MySQL tests outside a dedicated dai_test_account_* database')
    engine = create_engine_from_url(raw)
    if inspect(engine).get_table_names():
        engine.dispose()
        pytest.fail('Refusing to modify a nonempty database; supply a newly created disposable MySQL database')
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


def test_mysql_simultaneous_admin_disables_preserve_one_active_admin(db_session_factory):
    first = create_user(db_session_factory, 'parallel-admin-a', 'admin')
    second = create_user(db_session_factory, 'parallel-admin-b', 'admin')
    with db_session_factory() as db:
        accounts.lock_policy(db)
        db.commit()
    barrier = Barrier(2)
    def disable(actor_id, target_id):
        with db_session_factory() as db:
            actor = db.get(User, actor_id)
            barrier.wait(timeout=10)
            try:
                accounts.update_identity(db, target_id, {'status': 'disabled'}, actor)
                db.commit()
                return 'success'
            except HTTPException as exc:
                db.rollback()
                return exc.detail['code']
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(disable, first.id, second.id), pool.submit(disable, second.id, first.id)]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(results) == ['LAST_ADMIN', 'success']
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.role == 'admin', User.status == 'active')) == 1


def test_mysql_two_workers_claim_and_apply_batch_once(db_session_factory, test_settings):
    from datetime import date
    test_settings.account_security_keys = Fernet.generate_key().decode()
    admin = create_user(db_session_factory, 'parallel-batch-admin', 'admin')
    with db_session_factory() as db:
        term = AcademicTerm(code='PARALLEL', name='Parallel', start_date=date(2026, 9, 1),
                            end_date=date(2027, 1, 1), status='active')
        db.add(term)
        db.commit()
        actor = db.get(User, admin.id)
        preview = batches.create_import_preview(db, actor, 'classes', {'term_id': term.id},
                  [(2, {'class_code': 'ONCE', 'class_name': 'Only once'})], test_settings)
        batch_id = preview.id
        batches.confirm_batch(db, preview.id, actor, BatchConfirm(preview_digest=preview.preview_digest), test_settings)
    barrier = Barrier(2)
    def run():
        with db_session_factory() as db:
            barrier.wait(timeout=10)
            claim = batches.claim_batch(db)
        if claim:
            batches.process_batch(db_session_factory, *claim, test_settings)
        return claim
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run), pool.submit(run)]
        claims = [future.result(timeout=30) for future in futures]
    assert sum(claim is not None for claim in claims) == 1
    with db_session_factory() as db:
        assert db.get(AdminBatch, batch_id).status == 'succeeded'
        assert db.scalar(select(func.count()).select_from(TeachingClass).where(TeachingClass.code == 'ONCE')) == 1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.batch_id == batch_id, AuditEvent.action == 'class.create')) == 1


def test_mysql_import_detects_identity_change_committed_by_another_session(db_session_factory, test_settings):
    test_settings.account_security_keys = Fernet.generate_key().decode()
    admin = create_user(db_session_factory, 'conflict-batch-admin', 'admin')
    student = create_user(db_session_factory, '00009000', 'student', real_name='Original name')
    with db_session_factory() as db:
        db.get(User, student.id).student_no = '00009000'
        db.commit()
        actor = db.get(User, admin.id)
        preview = batches.create_import_preview(db, actor, 'students', {}, [
            (2, {'student_no': '00009000', 'real_name': 'Original name'}),
            (3, {'student_no': '00009001', 'real_name': 'Must not be created'}),
        ], test_settings)
        batch_id = preview.id
        batches.confirm_batch(db, batch_id, actor, BatchConfirm(preview_digest=preview.preview_digest), test_settings)
        claim = batches.claim_batch(db)
    batches.prepare_hashes(db_session_factory, *claim, test_settings)
    with db_session_factory() as other:
        other.get(User, student.id).real_name = 'Corrected name'
        other.commit()
    batches.process_batch(db_session_factory, *claim, test_settings)
    with db_session_factory() as db:
        assert db.get(AdminBatch, batch_id).status == 'conflict'
        assert db.get(User, student.id).real_name == 'Corrected name'
        assert db.scalar(select(User).where(User.student_no == '00009001')) is None
