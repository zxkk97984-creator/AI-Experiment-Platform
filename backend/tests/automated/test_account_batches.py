"""End-to-end account batches using real spreadsheets and isolated databases."""
from datetime import timedelta
from io import BytesIO

from cryptography.fernet import Fernet
from openpyxl import Workbook
import pytest
from sqlalchemy import func, select

from app.models import AdminBatch, AdminBatchItem, User
from app.security import verify_password
from app.services import account_batch_service as worker
from app.services.time_utils import utc_now
from conftest import auth_header, create_user, login

API = '/api/v1'


@pytest.fixture()
def admin_headers(client, db_session_factory, test_settings):
    test_settings.account_security_keys = Fernet.generate_key().decode()
    admin = create_user(db_session_factory, 'batch-admin', 'admin')
    return auth_header(login(client, admin.username)[0])


def workbook(rows):
    book = Workbook()
    book.active.append(['学号', '姓名', '院系', '班级编码'])
    for row in rows:
        book.active.append(row)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def preview(client, headers, rows):
    response = client.post(f'{API}/admin/roster-imports/preview', headers=headers,
                           data={'kind': 'students'},
                           files={'file': ('students.xlsx', workbook(rows),
                                          'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, headers, batch, **extra):
    return client.post(f"{API}/admin/batches/{batch['id']}/confirm", headers=headers,
                       json={'preview_digest': batch['preview_digest'], **extra})


def run_worker(factory, settings):
    with factory() as db:
        claim = worker.claim_batch(db)
    assert claim is not None
    worker.process_batch(factory, *claim, settings)
    return claim


def test_import_requires_password_change_then_grants_normal_access(client, db_session_factory, test_settings, admin_headers):
    batch = preview(client, admin_headers, [['00001234', 'Student One']])
    assert batch['summary']['new_accounts'] == 1
    assert confirm(client, admin_headers, batch).status_code == 202
    run_worker(db_session_factory, test_settings)
    with db_session_factory() as db:
        student = db.scalar(select(User).where(User.student_no == '00001234'))
        assert student.username == '00001234'
        assert student.must_change_password is True
        assert student.temporary_password_expires_at is not None
        assert verify_password('tzcaicode', student.password_hash)
    token, _ = login(client, '00001234', 'tzcaicode')
    headers = auth_header(token)
    assert client.get(f'{API}/auth/me', headers=headers).json()['must_change_password'] is True
    assert client.get(f'{API}/courses', headers=headers).status_code == 403
    changed = client.patch(f'{API}/users/me/password', headers=headers,
                           json={'current_password': 'tzcaicode', 'password': 'PersonalPassword!123'})
    assert changed.status_code == 204, changed.text
    assert client.get(f'{API}/auth/me', headers=headers).status_code == 401
    fresh, _ = login(client, '00001234', 'PersonalPassword!123')
    assert client.get(f'{API}/courses', headers=auth_header(fresh)).status_code == 200


def test_reimport_and_duplicate_confirmation_never_reset_existing_credentials(client, db_session_factory, test_settings, admin_headers):
    rows = [['00001235', 'Student Two'], ['00001236', 'Student Three']]
    first = preview(client, admin_headers, rows)
    assert confirm(client, admin_headers, first).status_code == 202
    claim = run_worker(db_session_factory, test_settings)
    with db_session_factory() as db:
        users = db.scalars(select(User).where(User.role == 'student').order_by(User.id)).all()
        before = [(u.id, u.password_hash, u.session_version, u.credential_version, u.must_change_password) for u in users]
        assert users[0].password_hash != users[1].password_hash
    assert confirm(client, admin_headers, first).status_code == 202
    worker.process_batch(db_session_factory, *claim, test_settings)
    second = preview(client, admin_headers, rows)
    assert second['summary'].get('new_accounts', 0) == 0
    assert second['summary']['reused_accounts'] == 2
    assert confirm(client, admin_headers, second).status_code == 202
    run_worker(db_session_factory, test_settings)
    with db_session_factory() as db:
        users = db.scalars(select(User).where(User.role == 'student').order_by(User.id)).all()
        assert [(u.id, u.password_hash, u.session_version, u.credential_version, u.must_change_password) for u in users] == before
        assert worker.claim_batch(db) is None
        assert db.get(AdminBatch, first['id']).password_ciphertext is None
        assert not any(db.scalars(select(AdminBatchItem.prepared_hash)))


@pytest.mark.parametrize('rows', [
    [[1234, 'Numeric identifier']],
    [['0001', '=1+1']],
    [['0001', 'First name'], ['0001', 'Conflicting name']],
    [['0001', 'Student', '', 'UNKNOWN']],
])
def test_invalid_preview_cannot_be_confirmed(client, db_session_factory, admin_headers, rows):
    batch = preview(client, admin_headers, rows)
    assert batch['summary']['errors'] > 0
    response = confirm(client, admin_headers, batch)
    assert response.status_code == 409, response.text
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.role == 'student')) == 0
        assert worker.claim_batch(db) is None


def test_prepared_import_recovers_expired_lease_without_regenerating_hashes(client, db_session_factory, test_settings, admin_headers, monkeypatch):
    batch = preview(client, admin_headers, [['00001237', 'Recoverable']])
    assert confirm(client, admin_headers, batch).status_code == 202
    with db_session_factory() as db:
        old_claim = worker.claim_batch(db)
    worker.prepare_hashes(db_session_factory, *old_claim, test_settings)
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.role == 'student')) == 0
        prepared = db.scalar(select(AdminBatchItem.prepared_hash).where(AdminBatchItem.batch_id == batch['id']))
        assert prepared
        db.get(AdminBatch, batch['id']).lease_until = utc_now() - timedelta(minutes=3)
        db.commit()
    def unexpected_hash(_):
        pytest.fail('Recovered preparation must reuse its persisted per-user digest')
    monkeypatch.setattr(worker, 'hash_password', unexpected_hash)
    with db_session_factory() as db:
        new_claim = worker.claim_batch(db)
    assert new_claim[1] != old_claim[1]
    worker.process_batch(db_session_factory, *old_claim, test_settings)
    worker.process_batch(db_session_factory, *new_claim, test_settings)
    with db_session_factory() as db:
        student = db.scalar(select(User).where(User.student_no == '00001237'))
        assert student.password_hash == prepared
        assert db.get(AdminBatch, batch['id']).status == 'succeeded'


def test_pending_reset_does_not_overwrite_student_password_change(client, db_session_factory, test_settings, admin_headers):
    student = create_user(db_session_factory, 'existing-student', 'student')
    token, _ = login(client, student.username)
    response = client.post(f'{API}/admin/password-resets/preview', headers=admin_headers,
                           json={'student_ids': [student.id]})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert confirm(client, admin_headers, batch, current_password='Passw0rd!', reason='Requested reset').status_code == 202
    # Confirmation reserves the operation but must leave the current session valid.
    assert client.get(f'{API}/auth/me', headers=auth_header(token)).status_code == 200
    changed = client.patch(f'{API}/users/me/password', headers=auth_header(token),
                           json={'current_password': 'Passw0rd!', 'password': 'StudentChosen!123'})
    assert changed.status_code == 204, changed.text
    run_worker(db_session_factory, test_settings)
    with db_session_factory() as db:
        saved = db.get(User, student.id)
        assert verify_password('StudentChosen!123', saved.password_hash)
        assert saved.must_change_password is False
        item = db.scalar(select(AdminBatchItem).where(AdminBatchItem.batch_id == batch['id']))
        assert item.status == 'skipped'
        assert db.get(AdminBatch, batch['id']).summary['skipped'] == 1


def test_5000_row_preview_counts_distinct_students_and_rejects_5001(client, db_session_factory, admin_headers):
    rows = [[f'{i:08d}', f'Student {i}'] for i in range(5000)]
    batch = preview(client, admin_headers, rows)
    assert batch['summary']['new_accounts'] == 5000
    assert batch['summary'].get('errors', 0) == 0
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AdminBatchItem).where(AdminBatchItem.batch_id == batch['id'])) == 5000
        assert db.scalar(select(func.count()).select_from(User).where(User.role == 'student')) == 0
    response = client.post(f'{API}/admin/roster-imports/preview', headers=admin_headers,
                           data={'kind': 'students'}, files={'file': ('students.xlsx', workbook(rows + [['99999999', 'Over limit']]))})
    assert response.status_code == 422, response.text


def test_identity_conflict_after_preparation_keeps_entire_import_unapplied(client, db_session_factory, test_settings, admin_headers):
    batch = preview(client, admin_headers, [['00002000', 'Expected name'], ['00002001', 'Unaffected row']])
    assert confirm(client, admin_headers, batch).status_code == 202
    with db_session_factory() as db:
        claim = worker.claim_batch(db)
    worker.prepare_hashes(db_session_factory, *claim, test_settings)
    rival = create_user(db_session_factory, '00002000', 'student', real_name='Different name')
    with db_session_factory() as db:
        db.get(User, rival.id).student_no = '00002000'
        db.commit()
    worker.process_batch(db_session_factory, *claim, test_settings)
    with db_session_factory() as db:
        assert db.get(AdminBatch, batch['id']).status == 'conflict'
        assert db.scalar(select(User).where(User.student_no == '00002001')) is None
        assert db.get(User, rival.id).real_name == 'Different name'
        assert db.get(AdminBatch, batch['id']).password_ciphertext is None


def test_policy_change_requires_fresh_preview(client, db_session_factory, admin_headers):
    batch = preview(client, admin_headers, [['00002002', 'Policy student']])
    response = client.put(f'{API}/admin/account-security', headers=admin_headers,
                          json={'current_password': 'Passw0rd!', 'default_password': 'NewTemporary!123', 'temporary_days': 10})
    assert response.status_code == 200, response.text
    response = confirm(client, admin_headers, batch)
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'POLICY_CHANGED'
    with db_session_factory() as db:
        assert worker.claim_batch(db) is None
        assert db.scalar(select(User).where(User.student_no == '00002002')) is None
