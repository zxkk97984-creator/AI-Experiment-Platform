"""Account mutations. Callers own transactions; secrets never enter audit payloads."""
import json
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.errors import api_error
from app.models import AccountSecurityPolicy, AuditEvent, User
from app.security import hash_password, validate_password_rules, verify_password
from app.services.time_utils import utc_now, as_utc

DEFAULT_TEMPORARY_PASSWORD = "tzcaicode"


def audit(db, actor_id, action, target_type, target_id, *, reason=None, changes=None,
          batch_id=None, request_id=None):
    if request_id is None:
        from app.logging_config import get_request_id
        request_id = get_request_id()
    db.add(AuditEvent(actor_id=actor_id, action=action, target_type=target_type,
                      target_id=str(target_id), reason=reason, changes=json.loads(json.dumps(changes or {}, default=str)),
                      batch_id=batch_id, request_id=request_id))


def lock_policy(db):
    # Singleton also serializes administrator-count checks, including SQLite.
    if db.get(AccountSecurityPolicy, 1) is None:
        try:
            with db.begin_nested():
                db.add(AccountSecurityPolicy(id=1))
                db.flush()
        except IntegrityError:
            pass
    db.execute(update(AccountSecurityPolicy).where(AccountSecurityPolicy.id == 1)
               .values(version=AccountSecurityPolicy.version))
    return db.scalar(select(AccountSecurityPolicy).where(AccountSecurityPolicy.id == 1)
                     .execution_options(populate_existing=True).with_for_update())


def cipher(settings):
    try:
        keys = [Fernet(k.strip().encode()) for k in settings.account_security_keys.split(',') if k.strip()]
        if not keys:
            raise ValueError('missing key')
        return MultiFernet(keys)
    except (ValueError, TypeError):
        raise api_error(503, "ACCOUNT_KEY_NOT_CONFIGURED", "请配置独立账号加密密钥")


def encrypt_secret(settings, value, context):
    return cipher(settings).encrypt(json.dumps({'context': context, 'value': value}).encode()).decode()


def decrypt_secret(settings, value, context):
    try:
        payload = json.loads(cipher(settings).decrypt(value.encode()))
        if payload['context'] != context:
            raise ValueError('context mismatch')
        return payload['value']
    except (InvalidToken, ValueError, KeyError, TypeError):
        raise api_error(503, "ACCOUNT_SECRET_INVALID", "账号配置密钥或密文无效")


def temporary_policy(db, settings):
    policy = db.get(AccountSecurityPolicy, 1)
    if policy is None:
        return DEFAULT_TEMPORARY_PASSWORD, 7, 1
    password = (decrypt_secret(settings, policy.password_ciphertext, f'policy:{policy.version}')
                if policy.password_ciphertext else DEFAULT_TEMPORARY_PASSWORD)
    return password, policy.temporary_days, policy.version


def ensure_password_session(user):
    if user.must_change_password and user.temporary_password_expires_at:
        if as_utc(user.temporary_password_expires_at) <= utc_now():
            raise api_error(403, 'TEMPORARY_PASSWORD_EXPIRED', '临时密码已过期，请联系管理员重置')


def ensure_full_account(user):
    ensure_password_session(user)
    if user.must_change_password:
        raise api_error(403, 'PASSWORD_CHANGE_REQUIRED', '请先设置个人密码')


def verify_current(user, password):
    if not password or not verify_password(password, user.password_hash):
        raise api_error(422, 'CURRENT_PASSWORD_INVALID', '当前密码不正确')


def change_credentials(db, user, password_hash, *, temporary=False, days=7):
    from app.services.credential_delivery_service import invalidate
    invalidate(db, user.id)
    user.password_hash = password_hash
    user.must_change_password = temporary
    user.password_changed_at = utc_now()
    user.temporary_password_expires_at = utc_now() + timedelta(days=days) if temporary else None
    user.session_version += 1
    user.credential_version += 1
    db.flush()


def change_own_password(db, user, password, current_password, settings):
    user = db.scalar(select(User).where(User.id == user.id).with_for_update()
                     .execution_options(populate_existing=True))
    ensure_password_session(user)
    verify_current(user, current_password)
    default, _, _ = temporary_policy(db, settings)
    try:
        validate_password_rules(password, user.username)
    except ValueError as exc:
        raise api_error(422, 'PASSWORD_EQUALS_USERNAME' if password.strip().casefold() == user.username.strip().casefold() else 'PASSWORD_POLICY', str(exc))
    forbidden = {DEFAULT_TEMPORARY_PASSWORD.casefold(), default.casefold(),
                 user.username.strip().casefold(), (user.student_no or '').strip().casefold()}
    if password.strip().casefold() in forbidden or verify_password(password, user.password_hash):
        raise api_error(422, 'PASSWORD_REUSED', '请使用不同于当前密码、共享密码及学号的个人密码')
    change_credentials(db, user, hash_password(password))
    audit(db, user.id, 'password.change', 'user', user.id)
    return user


def update_identity(db, user_id, updates, actor, *, request_id=None):
    if actor.role != 'admin':
        raise api_error(403, 'FORBIDDEN', '教务身份只能由管理员修改')
    lock_policy(db)
    user = db.scalar(select(User).where(User.id == user_id).with_for_update()
                     .execution_options(populate_existing=True))
    if not user:
        raise api_error(404, 'USER_NOT_FOUND', '用户不存在')
    reason = updates.pop('reason', None)
    for field, maximum in (('real_name', 120), ('department', 120), ('student_no', 80)):
        if updates.get(field) is not None and len(updates[field]) > maximum:
            raise api_error(422, 'INVALID_USER_FIELD', f'{field} 长度超过限制')
    for key in ('real_name', 'role', 'status'):
        if key in updates and (updates[key] is None or not str(updates[key]).strip()):
            raise api_error(422, 'INVALID_USER_FIELD', f'{key} 不能为空')
    if updates.get('role', user.role) not in {'student', 'teacher', 'admin'}:
        raise api_error(422, 'INVALID_ROLE', '角色无效')
    if updates.get('status', user.status) not in {'active', 'disabled'}:
        raise api_error(422, 'INVALID_STATUS', '状态无效')
    student_no = updates.get('student_no', user.student_no)
    student_no = student_no.strip() if student_no else None
    if updates.get('role', user.role) == 'student' and not student_no and ('role' in updates or 'student_no' in updates):
        raise api_error(422, 'STUDENT_NO_REQUIRED', '学生必须填写学号')
    if 'student_no' in updates:
        updates['student_no'] = student_no
        if user.student_no and user.username == user.student_no and student_no:
            updates['username'] = student_no
    if user.role == 'admin' and user.status == 'active' and (
        updates.get('role', user.role) != 'admin' or updates.get('status', user.status) != 'active'
    ):
        count = len(db.scalars(select(User.id).where(User.role == 'admin', User.status == 'active').with_for_update()).all())
        if count <= 1:
            raise api_error(409, 'LAST_ADMIN', '不能禁用或降级最后一个有效管理员')
    changes = {key: {'before': getattr(user, key), 'after': value}
               for key, value in updates.items() if getattr(user, key) != value}
    for key, value in updates.items():
        setattr(user, key, value)
    if {'role', 'status', 'username'} & changes.keys():
        user.session_version += 1
        user.credential_version += 1
        from app.services.credential_delivery_service import invalidate
        invalidate(db, user.id)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise api_error(409, 'USER_IDENTITY_CONFLICT', '学号或登录账号已被占用')
    audit(db, actor.id, 'user.update', 'user', user.id, reason=reason, changes=changes, request_id=request_id)
    return user
