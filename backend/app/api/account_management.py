from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.dependencies import get_db, get_redis_client, pagination, PaginationParams, require_roles
from app.errors import api_error
from app.models import AdminBatch, AdminBatchItem, AuditEvent, User
from app.security import validate_password_rules
from app.services import account_service as accounts
from app.services import account_batch_service as batches
from app.services.roster_import_service import parse_xlsx, spreadsheet

router = APIRouter(prefix='/admin', tags=['account-management'])


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    current_password: str
    default_password: str | None = None
    temporary_days: int = Field(default=7, ge=1, le=30)


class BatchConfirm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_digest: str
    current_password: str | None = None
    reason: str | None = Field(default=None, max_length=500)


class ResetSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    student_ids: list[int] = Field(default_factory=list, max_length=5000)
    class_id: int | None = None
    all_matching: bool = False
    q: str | None = Field(default=None, max_length=120)
    status_filter: Literal['active', 'disabled'] | None = None

    @model_validator(mode='after')
    def selection(self):
        if sum([bool(self.student_ids), bool(self.class_id), self.all_matching]) != 1:
            raise ValueError('请选择一种目标范围')
        return self


def batch_read(batch):
    return {key: getattr(batch, key) for key in ('id', 'actor_id', 'kind', 'status', 'scope', 'summary',
            'preview_digest', 'policy_version', 'reason', 'expires_at', 'finished_at', 'created_at', 'error_code')}


def require_batch(db, batch_id):
    batch = db.get(AdminBatch, batch_id)
    if not batch:
        raise api_error(404, 'BATCH_NOT_FOUND', '批次不存在')
    return batch


def verify_admin(request, actor, password, redis, settings):
    from app.api.auth import _client_ip
    from app.services.auth_service import check_login_rate_limits, record_login_failure, reset_login_failures
    key = 'reauth:' + actor.username
    ip = _client_ip(request, settings)
    check_login_rate_limits(redis, settings, key, ip)
    try:
        accounts.verify_current(actor, password)
    except Exception:
        record_login_failure(redis, settings, key, ip)
        raise
    reset_login_failures(redis, key)


@router.get('/account-security')
def read_policy(db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
                actor: User = Depends(require_roles('admin'))):
    policy = db.get(accounts.AccountSecurityPolicy, 1)
    return {'configured': True, 'custom_password': bool(policy and policy.password_ciphertext),
            'version': policy.version if policy else 1, 'temporary_days': policy.temporary_days if policy else 7,
            'encryption_ready': bool(settings.account_security_keys)}


@router.put('/account-security')
def update_policy(payload: PolicyUpdate, request: Request, db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings), actor: User = Depends(require_roles('admin')),
                  redis=Depends(get_redis_client)):
    verify_admin(request, actor, payload.current_password, redis, settings)
    policy = accounts.lock_policy(db)
    password, _, _ = accounts.temporary_policy(db, settings)
    password = payload.default_password if payload.default_password is not None else password
    try:
        validate_password_rules(password)
    except ValueError as exc:
        raise api_error(422, 'PASSWORD_POLICY', str(exc))
    policy.version += 1
    policy.password_ciphertext = accounts.encrypt_secret(settings, password, f'policy:{policy.version}')
    policy.temporary_days, policy.updated_by = payload.temporary_days, actor.id
    accounts.audit(db, actor.id, 'account_policy.update', 'account_policy', 1,
                   request_id=getattr(request.state, 'request_id', None),
                   changes={'version': policy.version, 'temporary_days': policy.temporary_days})
    db.commit()
    return read_policy(db, settings, actor)


@router.get('/roster-imports/template')
def import_template(kind: Literal['classes', 'students', 'class_students'] = 'students',
                    actor: User = Depends(require_roles('admin'))):
    return Response(spreadsheet(kind), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{kind}.xlsx"'})


@router.post('/roster-imports/preview')
def preview_import(file: UploadFile = File(...), kind: Literal['classes', 'students', 'class_students'] = Form(...),
                   term_id: int | None = Form(None), class_id: int | None = Form(None),
                   db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
                   actor: User = Depends(require_roles('admin'))):
    if kind == 'class_students' and class_id is None:
        raise api_error(422, 'CLASS_REQUIRED', '请选择目标教学班')
    content = file.file.read(5 * 1024 * 1024 + 1)
    inputs = parse_xlsx(content, file.filename, kind)
    batch = batches.create_import_preview(db, actor, kind, {'term_id': term_id, 'class_id': class_id}, inputs, settings)
    return batch_read(batch)


@router.post('/password-resets/preview')
def preview_reset(payload: ResetSelection, db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
                  actor: User = Depends(require_roles('admin'))):
    return batch_read(batches.create_reset_preview(db, actor, payload, settings))


@router.post('/batches/{batch_id}/confirm', status_code=202)
def confirm(batch_id: str, payload: BatchConfirm, request: Request, db: Session = Depends(get_db),
            settings: Settings = Depends(get_settings), actor: User = Depends(require_roles('admin')),
            redis=Depends(get_redis_client)):
    batch = require_batch(db, batch_id)
    if batch.kind == 'password_reset' and batch.status == 'preview':
        verify_admin(request, actor, payload.current_password, redis, settings)
    result = batches.confirm_batch(db, batch_id, actor, payload, settings)
    try:
        redis.publish('account-batches', batch_id)
    except Exception:
        pass  # DB polling recovers a lost wakeup.
    return batch_read(result)


@router.get('/batches')
def list_batches(page: PaginationParams = Depends(pagination), db: Session = Depends(get_db),
                 actor: User = Depends(require_roles('admin'))):
    total = db.scalar(select(func.count()).select_from(AdminBatch))
    rows = db.scalars(select(AdminBatch).order_by(AdminBatch.created_at.desc(), AdminBatch.id)
                      .offset((page.page - 1) * page.page_size).limit(page.page_size)).all()
    return {'items': [batch_read(b) for b in rows], 'total': total, 'page': page.page, 'page_size': page.page_size}


@router.get('/batches/{batch_id}')
def get_batch(batch_id: str, page: PaginationParams = Depends(pagination), db: Session = Depends(get_db),
              actor: User = Depends(require_roles('admin'))):
    batch = require_batch(db, batch_id)
    query = select(AdminBatchItem).where(AdminBatchItem.batch_id == batch.id)
    total = db.scalar(select(func.count()).select_from(AdminBatchItem).where(AdminBatchItem.batch_id == batch.id))
    rows = db.scalars(query.order_by(AdminBatchItem.row_number).offset((page.page - 1) * page.page_size).limit(page.page_size)).all()
    prepared = db.scalar(select(func.count()).select_from(AdminBatchItem).where(
        AdminBatchItem.batch_id == batch.id, AdminBatchItem.prepared_hash.is_not(None)))
    return {**batch_read(batch), 'prepared': prepared, 'total': total, 'page': page.page, 'page_size': page.page_size,
            'items': [{'row_number': i.row_number, 'status': i.status, 'data': {k:v for k,v in i.data.items() if not k.startswith('_')},
                       'result': i.result} for i in rows]}


@router.get('/batches/{batch_id}/report')
def report(batch_id: str, db: Session = Depends(get_db), actor: User = Depends(require_roles('admin'))):
    require_batch(db, batch_id)
    rows = db.scalars(select(AdminBatchItem).where(AdminBatchItem.batch_id == batch_id).order_by(AdminBatchItem.row_number)).all()
    values = [[i.row_number, i.status, '; '.join(i.result.get('errors', [])) or i.result.get('message', i.result.get('action', '')),
               i.data.get('student_no'), i.data.get('real_name'), i.data.get('class_code')] for i in rows]
    return Response(spreadsheet('report', values), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="result.xlsx"', 'Cache-Control': 'private, no-store'})


@router.get('/audit-events')
def list_audit(action: str | None = None, target_id: str | None = None,
               page: PaginationParams = Depends(pagination), db: Session = Depends(get_db),
               actor: User = Depends(require_roles('admin'))):
    filters = []
    if action:
        filters.append(AuditEvent.action == action)
    if target_id:
        filters.append(AuditEvent.target_id == target_id)
    total = db.scalar(select(func.count()).select_from(AuditEvent).where(*filters))
    rows = db.scalars(select(AuditEvent).where(*filters).order_by(AuditEvent.id.desc())
                     .offset((page.page - 1) * page.page_size).limit(page.page_size)).all()
    return {'items': [{k: getattr(r, k) for k in ('id', 'actor_id', 'action', 'target_type', 'target_id',
                     'batch_id', 'reason', 'changes', 'created_at')} for r in rows],
            'total': total, 'page': page.page, 'page_size': page.page_size}
