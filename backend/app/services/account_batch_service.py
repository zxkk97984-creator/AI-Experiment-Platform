"""Durable account operations. Database states, not Redis messages, are authoritative."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.errors import api_error
from app.models import (AdminBatch, AdminBatchItem, AuditEvent, User,
                        TeachingClass, TeachingClassStudent, ExamSubmission)
from app.security import hash_password
from app.services import account_service as accounts
from app.services import credential_delivery_service as credentials
from app.models.credential_delivery import CredentialDelivery
from app.services.roster_import_service import preflight
from app.services.time_utils import as_utc, utc_now

TERMINAL = {'succeeded', 'partially_succeeded', 'failed', 'conflict', 'expired'}


def now_db(db):
    return as_utc(db.scalar(select(func.current_timestamp())))


def in_exam(db, user_id):
    return db.scalar(select(ExamSubmission.id).where(ExamSubmission.student_id == user_id,
                                                    ExamSubmission.status == 'started').limit(1)) is not None


def create_import_preview(db, actor, kind, scope, inputs, settings):
    results, summary, digest = preflight(db, kind, scope, inputs)
    _, _, version = accounts.temporary_policy(db, settings)
    batch = AdminBatch(id=str(uuid4()), actor_id=actor.id, kind=kind, scope=scope,
                       summary=summary, preview_digest=digest, policy_version=version,
                       expires_at=utc_now() + timedelta(hours=24))
    db.add(batch)
    db.flush()
    for (number, data), result in zip(inputs, results):
        db.add(AdminBatchItem(batch_id=batch.id, row_number=number, data=data, result=result,
                             user_id=result.get('user_id')))
    db.commit()
    return batch


def create_reset_preview(db, actor, payload, settings):
    query = select(User).where(User.role == 'student')
    if payload.student_ids:
        query = query.where(User.id.in_(set(payload.student_ids)))
    elif payload.class_id:
        query = query.join(TeachingClassStudent).where(
            TeachingClassStudent.teaching_class_id == payload.class_id, TeachingClassStudent.status == 'active')
    elif payload.all_matching:
        if payload.q:
            query = query.where(or_(User.username == payload.q, User.student_no == payload.q, User.real_name == payload.q))
        if payload.status_filter:
            query = query.where(User.status == payload.status_filter)
    else:
        raise api_error(422, 'EMPTY_SELECTION', '请选择学生或明确选择全部筛选结果')
    users = db.scalars(query.order_by(User.id).limit(5001)).all()
    if not users or len(users) > 5000:
        raise api_error(422, 'INVALID_SELECTION_SIZE', '请选择 1～5000 名学生')
    if payload.student_ids and len(users) != len(set(payload.student_ids)):
        raise api_error(422, 'INVALID_STUDENT', '所选名单包含不存在或非学生账号')
    _, _, version = accounts.temporary_policy(db, settings)
    batch = AdminBatch(id=str(uuid4()), actor_id=actor.id, kind='password_reset', scope={},
                       summary={}, preview_digest=str(uuid4()), policy_version=version,
                       expires_at=utc_now() + timedelta(hours=24))
    db.add(batch)
    db.flush()
    skipped = 0
    exam_students = set(db.scalars(select(ExamSubmission.student_id).where(ExamSubmission.status == 'started')).all())
    for number, user in enumerate(users, 1):
        reason = 'disabled' if user.status != 'active' else 'exam_in_progress' if user.id in exam_students else None
        skipped += bool(reason)
        db.add(AdminBatchItem(batch_id=batch.id, row_number=number, user_id=user.id,
                             expected_version=user.credential_version,
                             data={'student_no': user.student_no, 'real_name': user.real_name},
                             result={'message': {'disabled': '账号已禁用，跳过', 'exam_in_progress': '学生正在考试，跳过'}.get(reason, '重置后旧登录失效，必须改密')},
                             status='skipped' if reason else 'pending'))
    batch.summary = {'selected': len(users), 'eligible': len(users) - skipped, 'skipped': skipped}
    db.commit()
    return batch


def confirm_batch(db, batch_id, actor, payload, settings):
    accounts.lock_policy(db)
    batch = db.scalar(select(AdminBatch).where(AdminBatch.id == batch_id).with_for_update()
                      .execution_options(populate_existing=True))
    if not batch or batch.actor_id != actor.id:
        raise api_error(404, 'BATCH_NOT_FOUND', '批次不存在或不属于当前管理员')
    if batch.preview_digest != payload.preview_digest:
        raise api_error(409, 'PREVIEW_CHANGED', '预览已变化，请重新预检')
    if batch.status != 'preview':
        return batch  # Batch ID is the confirmation idempotency key.
    if as_utc(batch.expires_at) <= utc_now():
        raise api_error(409, 'PREVIEW_EXPIRED', '预检已过期，请重新上传')
    if batch.summary.get('errors'):
        raise api_error(409, 'PREVIEW_ERRORS', '请修复所有错误后重新上传')
    _legacy_password, days, version = accounts.temporary_policy(db, settings)
    if version != batch.policy_version:
        raise api_error(409, 'POLICY_CHANGED', '临时凭据策略已变化，请重新预检')
    if batch.kind == 'password_reset':
        accounts.verify_current(actor, payload.current_password)
        if not payload.reason or not payload.reason.strip():
            raise api_error(422, 'REASON_REQUIRED', '请填写重置原因')
        rows = db.scalars(select(AdminBatchItem).where(AdminBatchItem.batch_id == batch.id,
                                                       AdminBatchItem.status == 'pending').order_by(AdminBatchItem.user_id)).all()
        for item in rows:
            user = db.scalar(select(User).where(User.id == item.user_id).with_for_update()
                             .execution_options(populate_existing=True))
            if not user or user.role != 'student' or user.status != 'active' or in_exam(db, user.id):
                item.status, item.result = 'skipped', {'message': '账号状态改变或正在考试'}
            elif user.credential_version != item.expected_version:
                item.status, item.result = 'skipped', {'message': '凭据已变化，请重新预览'}
            else:
                user.credential_version += 1
                credentials.invalidate(db, user.id)
                item.expected_version = user.credential_version
    if batch.kind != 'classes':
        accounts.cipher(settings)  # Fail closed before queueing credential preparation.
    batch.password_ciphertext = None
    batch.scope = {**batch.scope, 'temporary_days': days}
    batch.reason, batch.status = payload.reason, 'queued'
    accounts.audit(db, actor.id, 'batch.confirm', 'batch', batch.id, batch_id=batch.id, reason=payload.reason)
    db.commit()
    return batch


def claim_batch(db):
    now = now_db(db)
    eligible = or_(AdminBatch.status == 'queued',
                   (AdminBatch.status.in_(['preparing', 'applying'])) & (AdminBatch.lease_until < now))
    batch_id = db.scalar(select(AdminBatch.id).where(eligible).order_by(AdminBatch.created_at).limit(1))
    if not batch_id:
        return None
    token = str(uuid4())
    result = db.execute(update(AdminBatch).where(AdminBatch.id == batch_id, eligible).values(
        status='preparing', lease_token=token, lease_until=now + timedelta(seconds=120),
        attempts=AdminBatch.attempts + 1))
    db.commit()
    return (batch_id, token) if result.rowcount == 1 else None


def owned_batch(db, batch_id, token):
    batch = db.scalar(select(AdminBatch).where(AdminBatch.id == batch_id).with_for_update()
                      .execution_options(populate_existing=True))
    if not batch or batch.lease_token != token or batch.status in TERMINAL:
        raise api_error(409, 'LEASE_LOST', '任务已由其他执行进程接管')
    return batch


def heartbeat(db, batch_id, token):
    result = db.execute(update(AdminBatch).where(AdminBatch.id == batch_id, AdminBatch.lease_token == token,
                                               AdminBatch.status.in_(['preparing', 'applying']))
                        .values(lease_until=now_db(db) + timedelta(seconds=120)))
    db.commit()
    if result.rowcount != 1:
        raise api_error(409, 'LEASE_LOST', '执行租约已失效')


def finish(db, batch, status, error=None):
    batch.status, batch.error_code, batch.finished_at = status, error, utc_now()
    batch.password_ciphertext = None
    batch.lease_token = None
    batch.lease_until = None
    db.execute(update(AdminBatchItem).where(AdminBatchItem.batch_id == batch.id).values(prepared_hash=None))
    db.execute(update(CredentialDelivery).where(CredentialDelivery.batch_id == batch.id,
               CredentialDelivery.state == 'prepared').values(ciphertext=None, state='cleared'))


def prepare_hashes(factory, batch_id, token, settings):
    with factory() as db:
        batch = owned_batch(db, batch_id, token)
        if batch.kind == 'classes':
            return
        rows = db.scalars(select(AdminBatchItem).where(AdminBatchItem.batch_id == batch_id).order_by(AdminBatchItem.row_number)).all()
        used = {accounts.decrypt_secret(settings, d.ciphertext, f'credential:{d.id}')
                for d in db.scalars(select(CredentialDelivery).where(
                    CredentialDelivery.batch_id == batch_id, CredentialDelivery.ciphertext.is_not(None)))}
        needed = []
        seen = set()
        for item in rows:
            key = item.user_id if batch.kind == 'password_reset' else item.data.get('student_no')
            requires = batch.kind == 'password_reset' or item.result.get('action') == 'create_user'
            if requires and item.status == 'pending' and key not in seen:
                seen.add(key)
                if not item.prepared_hash:
                    needed.append((item.id, f'user:{key}' if batch.kind == 'password_reset' else f'student:{key}'))
        db.commit()
    with ThreadPoolExecutor(max_workers=settings.account_worker_hash_concurrency) as pool:
        for offset in range(0, len(needed), 40):
            chunk = needed[offset:offset + 40]
            with factory() as db:
                batch = owned_batch(db, batch_id, token)
                secrets_to_hash = []
                for _, key in chunk:
                    target = db.get(User, int(key.split(':', 1)[1])) if key.startswith('user:') else None
                    secrets_to_hash.append(credentials.prepare(db, batch.actor_id, settings, key,
                                           batch_id=batch_id, used=used,
                                           identity=target.username if target else None)[1])
                db.commit()
            hashes = list(pool.map(hash_password, secrets_to_hash))
            with factory() as db:
                owned_batch(db, batch_id, token)
                for (item_id, _), hashed in zip(chunk, hashes):
                    db.get(AdminBatchItem, item_id).prepared_hash = hashed
                heartbeat(db, batch_id, token)


def apply_import(factory, batch_id, token, settings):
    with factory() as db:
        # Same global academic mutation lock as interactive class changes.
        accounts.lock_policy(db)
        batch = owned_batch(db, batch_id, token)
        actor = db.scalar(select(User).where(User.id == batch.actor_id).with_for_update()
                          .execution_options(populate_existing=True))
        if not actor or actor.role != 'admin' or actor.status != 'active' or actor.must_change_password:
            raise api_error(409, 'ACTOR_UNAVAILABLE', '操作人不再具有执行权限')
        items = db.scalars(select(AdminBatchItem).where(AdminBatchItem.batch_id == batch_id)
                           .order_by(AdminBatchItem.row_number)).all()
        results, summary, digest = preflight(db, batch.kind, batch.scope, [(i.row_number, i.data) for i in items], locking=True)
        if digest != batch.preview_digest or summary.get('errors'):
            for item, result in zip(items, results):
                item.result = result
            finish(db, batch, 'conflict', 'PREVIEW_CHANGED')
            db.commit()
            return
        batch.status = 'applying'
        cache = {}
        hashes = {i.data.get('student_no'): i.prepared_hash for i in items if i.prepared_hash}
        affected = set()
        for item, result in zip(items, results):
            if result.get('duplicate'):
                item.status = 'skipped'
                continue
            data = item.data
            if batch.kind == 'classes':
                if result['action'] == 'create_class':
                    cls = TeachingClass(academic_term_id=batch.scope['term_id'], code=data['class_code'], name=data['class_name'])
                    db.add(cls)
                    db.flush()
                    accounts.audit(db, actor.id, 'class.create', 'teaching_class', cls.id, batch_id=batch.id)
                item.status = 'succeeded'
                continue
            no = data['student_no']
            user = cache.get(no) or (db.get(User, result['user_id']) if result['user_id'] else None)
            if user is None:
                user = User(username=no, student_no=no, real_name=data['real_name'], department=data.get('department') or None,
                            role='student', status='active', password_hash=hashes[no], must_change_password=True,
                            temporary_password_expires_at=utc_now() + timedelta(days=batch.scope['temporary_days']))
                db.add(user)
                db.flush()
                delivery = db.scalar(select(CredentialDelivery).where(
                    CredentialDelivery.batch_id == batch_id, CredentialDelivery.recipient_key == f'student:{no}'))
                if not delivery or delivery.state != 'prepared':
                    raise api_error(409, 'CREDENTIAL_UNAVAILABLE', '临时凭据准备状态异常')
                credentials.issue(db, delivery, user, settings)
                accounts.audit(db, actor.id, 'user.create', 'user', user.id, batch_id=batch.id)
            cache[no] = user
            if not user.department and data.get('department'):
                user.department = data['department']
                accounts.audit(db, actor.id, 'user.department_fill', 'user', user.id, batch_id=batch.id,
                               changes={'department': data['department']})
            class_id = result.get('class_id')
            if class_id:
                member = db.scalar(select(TeachingClassStudent).where(TeachingClassStudent.teaching_class_id == class_id,
                                                                      TeachingClassStudent.student_id == user.id))
                if member is None:
                    member = TeachingClassStudent(teaching_class_id=class_id, student_id=user.id)
                    db.add(member)
                if member.status != 'active':
                    member.status, member.joined_at, member.removed_at = 'active', utc_now(), None
                    accounts.audit(db, actor.id, 'class.member_add', 'user', user.id,
                                   batch_id=batch.id, changes={'class_id': class_id})
                affected.add(class_id)
            item.user_id, item.status = user.id, 'succeeded'
            db.flush()
        # Sync once per course even when several imported classes link to it.
        from app.models import Course, CourseTeachingClass
        from app.services.roster_service import sync_course_class_enrollments
        course_ids = db.scalars(select(CourseTeachingClass.course_id).where(
            CourseTeachingClass.teaching_class_id.in_(affected)).distinct()).all() if affected else []
        for course_id in sorted(course_ids):
            sync_course_class_enrollments(db, db.get(Course, course_id))
            db.flush()
        batch.summary = summary
        accounts.audit(db, actor.id, 'batch.complete', 'batch', batch.id, batch_id=batch.id, changes=summary)
        finish(db, batch, 'succeeded')
        db.commit()


def apply_resets(factory, batch_id, token, settings):
    with factory() as db:
        ids = list(db.scalars(select(AdminBatchItem.id).where(AdminBatchItem.batch_id == batch_id,
                                                            AdminBatchItem.status == 'pending').order_by(AdminBatchItem.id)))
    for item_id in ids:
        with factory() as db:
            batch = owned_batch(db, batch_id, token)
            actor = db.scalar(select(User).where(User.id == batch.actor_id).with_for_update()
                          .execution_options(populate_existing=True))
            if not actor or actor.role != 'admin' or actor.status != 'active' or actor.must_change_password:
                raise api_error(409, 'ACTOR_UNAVAILABLE', '操作人不再具有执行权限')
            item = db.get(AdminBatchItem, item_id)
            if item.status != 'pending':
                continue
            user = db.scalar(select(User).where(User.id == item.user_id).with_for_update())
            if (not user or user.status != 'active' or user.role != 'student'
                    or user.credential_version != item.expected_version or in_exam(db, user.id)):
                item.status, item.result = 'skipped', {'message': '凭据/账号状态改变或正在考试，未覆盖'}
            else:
                accounts.change_credentials(db, user, item.prepared_hash, temporary=True,
                                            days=batch.scope['temporary_days'])
                delivery = db.scalar(select(CredentialDelivery).where(
                    CredentialDelivery.batch_id == batch_id, CredentialDelivery.recipient_key == f'user:{user.id}'))
                if not delivery or delivery.state != 'prepared':
                    raise api_error(409, 'CREDENTIAL_UNAVAILABLE', '临时凭据准备状态异常')
                credentials.issue(db, delivery, user, settings)
                item.status, item.result = 'succeeded', {'message': '密码已重置，下次登录必须改密'}
                accounts.audit(db, batch.actor_id, 'password.reset', 'user', user.id,
                               batch_id=batch.id, reason=batch.reason)
            item.prepared_hash = None
            heartbeat(db, batch_id, token)
    with factory() as db:
        batch = owned_batch(db, batch_id, token)
        counts = dict(db.execute(select(AdminBatchItem.status, func.count()).where(AdminBatchItem.batch_id == batch_id)
                                 .group_by(AdminBatchItem.status)).all())
        batch.summary = {**batch.summary, **counts}
        finish(db, batch, 'partially_succeeded' if counts.get('skipped') else 'succeeded')
        db.commit()


def process_batch(factory, batch_id, token, settings):
    try:
        with factory() as db:
            batch = owned_batch(db, batch_id, token)
            kind, attempts = batch.kind, batch.attempts
        if attempts > 3:
            raise api_error(409, 'RETRY_EXHAUSTED', '任务重试次数已达上限')
        prepare_hashes(factory, batch_id, token, settings)
        if kind == 'password_reset':
            apply_resets(factory, batch_id, token, settings)
        else:
            apply_import(factory, batch_id, token, settings)
    except (HTTPException, IntegrityError, OperationalError) as exc:
        code = exc.detail.get('code') if isinstance(exc, HTTPException) else 'DATABASE_CONFLICT'
        if code == 'LEASE_LOST':
            return
        with factory() as db:
            batch = owned_batch(db, batch_id, token)
            if isinstance(exc, OperationalError) and batch.attempts < 3:
                batch.status, batch.lease_token, batch.lease_until = 'queued', None, None
            else:
                counts = dict(db.execute(select(AdminBatchItem.status, func.count()).where(AdminBatchItem.batch_id == batch_id)
                                         .group_by(AdminBatchItem.status)).all())
                batch.summary = {**batch.summary, **counts}
                finish(db, batch, 'partially_succeeded' if counts.get('succeeded') else 'failed', code)
            db.commit()


def cleanup_batches(db, settings):
    now = utc_now()
    credentials.cleanup(db)
    for batch in db.scalars(select(AdminBatch).where(AdminBatch.status == 'preview', AdminBatch.expires_at < now)).all():
        finish(db, batch, 'expired')
    old = select(AdminBatch.id).where(AdminBatch.finished_at < now - timedelta(days=settings.account_batch_retention_days))
    db.execute(delete(AdminBatchItem).where(AdminBatchItem.batch_id.in_(old)))
    db.execute(delete(AuditEvent).where(AuditEvent.created_at < now - timedelta(days=settings.account_audit_retention_days)))
    db.execute(delete(AdminBatch).where(AdminBatch.finished_at < now - timedelta(days=settings.account_audit_retention_days)))
    db.commit()
