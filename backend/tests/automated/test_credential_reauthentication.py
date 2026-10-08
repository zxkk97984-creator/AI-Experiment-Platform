"""An authenticated request may not export after its administrator is revoked."""
from datetime import timedelta
from uuid import uuid4

import pytest

from app.api import account_management as api
from app.models import AdminBatch, AuditEvent, User
from app.models.credential_delivery import CredentialDelivery
from app.security import decode_token
from app.services import account_service as accounts, credential_delivery_service as credentials
from app.services.time_utils import utc_now
from test_credential_delivery import admin as credential_admin, create_student, delivery

admin = credential_admin


@pytest.mark.parametrize('mode', ['single', 'batch'])
@pytest.mark.parametrize('change', ['role', 'status', 'session', 'credential', 'jti_before', 'jti_during'])
def test_credential_export_rechecks_account_and_access_token_before_secret_decryption(
        client, admin, db_session_factory, test_settings, redis_client, monkeypatch, mode, change):
    actor, headers = admin
    student = create_student(client, headers)
    meta = delivery(client, headers, student['id'])
    endpoint = f"/api/v1/admin/credential-deliveries/{meta['id']}/export"
    if mode == 'batch':
        batch_id = str(uuid4())
        with db_session_factory() as db:
            db.add(AdminBatch(id=batch_id, actor_id=actor.id, kind='student_import',
                status='completed', preview_digest='fixture', policy_version=1,
                expires_at=utc_now() + timedelta(days=7), finished_at=utc_now()))
            db.flush()
            db.get(CredentialDelivery, meta['id']).batch_id = batch_id
            db.commit()
        endpoint = f"/api/v1/admin/batches/{batch_id}/credentials"
    token = headers['Authorization'].split(' ', 1)[1]
    jti = decode_token(token, test_settings.secret_key, test_settings.algorithm)['jti']
    original_reauth, original_verify = api.verify_admin, accounts.verify_current

    def revoke():
        if change.startswith('jti_'):
            redis_client.set(f'blacklist:{jti}', '1', ex=3600)
            return
        with db_session_factory() as other:
            accounts.lock_policy(other)
            current = other.get(User, actor.id)
            if change in ('session', 'credential'):
                field = change + '_version'
                setattr(current, field, getattr(current, field) + 1)
            else:
                setattr(current, change, 'teacher' if change == 'role' else 'disabled')
            other.commit()

    def reauth_after_revoke(*args, **kwargs):
        if change != 'jti_during':
            revoke()
        return original_reauth(*args, **kwargs)

    def password_verification_with_logout(*args, **kwargs):
        result = original_verify(*args, **kwargs)
        if change == 'jti_during':
            revoke()
        return result

    def never_decrypt(*args, **kwargs):
        raise AssertionError('revoked administrator must never reach credential decryption')

    monkeypatch.setattr(api, 'verify_admin', reauth_after_revoke)
    monkeypatch.setattr(accounts, 'verify_current', password_verification_with_logout)
    monkeypatch.setattr(credentials, 'workbook', never_decrypt)
    result = client.post(endpoint, headers=headers, json={'current_password': 'Passw0rd!'})
    assert result.status_code == (401 if change.startswith('jti_') else 403), result.text
    assert result.headers['content-type'].startswith('application/json')
    assert 'ciphertext' not in result.text and '临时凭据' not in result.text
    with db_session_factory() as db:
        assert db.query(AuditEvent).filter_by(action='credential.export').count() == 0
        assert db.get(CredentialDelivery, meta['id']).ciphertext is not None
