"""Frozen submissions, quotas and learner-safe projections."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select, update

from app.errors import api_error
from app.models import (ExperimentModule, ExperimentRecord, ExperimentSubmission, LabArtifact,
                        LabAssessment, LabFile, LabGradeRevision, LabRecord, LabRegradeBatch,
                        LabRun, LabSubmission, Lesson, StorageObject)
from app.services import lab_assessment_service as assessments
from app.services import lab_data_service as data
from app.services.lab_grading import grade_read, refresh_selected
from app.storage import StorageArea, StorageService


def artifact_snapshot(db, artifact, settings, report_cells=None):
    result = copy.deepcopy(artifact.code_snapshot or {})
    result['source'] = artifact.source
    result['source_files'] = {}
    actual_cells = []
    storage = StorageService.from_settings(settings)
    total = 0
    for file in db.scalars(select(LabFile).where(LabFile.artifact_id == artifact.id).order_by(LabFile.path)):
        if not file.path.lower().endswith(('.py', '.ipynb')):
            continue
        obj = db.get(StorageObject, file.object_id)
        # Do not silently truncate code and then let AI publish a score.
        total += obj.size_bytes or 0
        if total > 2 * 1024 * 1024:
            result['evidence_too_large'] = True
            break
        raw = storage.read(StorageArea.ARTIFACTS, obj.object_key).decode('utf-8')
        if file.path.lower().endswith('.ipynb'):
            notebook = json.loads(raw)
            for i, cell in enumerate(notebook.get('cells', [])):
                source = cell.get('source', '')
                actual_cells.append({'id': f'upload-{file.id}-{i}',
                    'type': cell.get('cell_type', 'code'),
                    'source': ''.join(source) if isinstance(source, list) else str(source)})
        else:
            result['source_files'][file.path] = raw
    if artifact.source in ('external', 'reference'):
        result['cells'] = actual_cells
    if report_cells:
        # The current written analysis is a separate frozen part of the submission.
        result['cells'] = [c for c in result.get('cells', []) if c.get('type') != 'markdown'] + copy.deepcopy(report_cells)
    return result


def ensure_revision(record, expected):
    if expected is None or record.record_revision != expected:
        raise api_error(409, 'RECORD_REVISION_CONFLICT', '作答已变化，请保存并刷新后重试')


def enqueue_cell_check(db, record, cell_id, user, payload):
    from app.services.lab_resources import lock_resources
    lock_resources(db)
    assessment = assessments.record_assessment(db, record.id)
    if not assessment:
        raise api_error(409, 'LAB_CELL_CHECK_UNAVAILABLE', '此实验尚未配置单元格检查')
    rules = [r for r in assessment.config.get('cell_checks', []) if r['cell_id'] == cell_id]
    if not rules:
        raise api_error(404, 'LAB_CELL_CHECK_NOT_FOUND', '此单元格没有评分检查')
    request_key = 'cell:' + str(payload.client_request_id)
    existing = db.scalar(select(LabRun).where(LabRun.owner_id == user.id, LabRun.request_key == request_key).with_for_update())
    if existing:
        if (existing.record_id != record.id or existing.snapshot.get('cell_id') != cell_id
                or existing.snapshot.get('record_revision') != payload.expected_record_revision
                or bool(existing.snapshot.get('check_all')) != payload.all_cells):
            raise api_error(409, 'LAB_REQUEST_CONFLICT', '请求标识已用于其他检查')
        return existing
    record = db.scalar(select(ExperimentRecord).where(ExperimentRecord.id == record.id)
        .with_for_update().execution_options(populate_existing=True))
    ensure_revision(record, payload.expected_record_revision)
    if db.scalar(select(LabRun.id).where(LabRun.active_student_id == user.id).with_for_update()):
        raise api_error(409, 'LAB_CHECK_ACTIVE', '已有后台训练或单元格检查，请等待完成')
    snapshot = assessments.record_snapshot(db, record)
    cells = sorted(snapshot['cells'], key=lambda c: c.get('order', 0))
    index = next((i for i, c in enumerate(cells) if c['id'] == cell_id and not c.get('source_hidden')), None)
    if index is None:
        raise api_error(404, 'CELL_NOT_FOUND', '评分单元格不存在')
    snapshot.update(cells=cells if payload.all_cells else cells[:index + 1], cell_id=cell_id, check_all=payload.all_cells)
    run = assessments.create_run(db, owner_id=user.id, kind='cell_check', assessment=assessment,
        record=record, snapshot=snapshot, request_key=request_key)
    run.active_student_id = user.id
    db.flush()
    return run


def enqueue_training(db, record, user, payload, settings):
    from app.services.account_service import lock_policy
    lock_policy(db)
    from app.services.lab_resources import lock_resources
    lock_resources(db)
    assessment = assessments.record_assessment(db, record.id)
    if not assessment:
        raise api_error(409, 'LAB_NOT_ENABLED', '此实验尚未启用训练功能')
    request_key = 'train:' + str(payload.client_request_id)
    existing = db.scalar(select(LabRun).where(LabRun.owner_id == user.id, LabRun.request_key == request_key).with_for_update())
    if existing:
        if existing.record_id != record.id:
            raise api_error(409, 'LAB_REQUEST_CONFLICT', '请求标识已用于其他实验')
        return existing
    record = db.scalar(select(ExperimentRecord).where(ExperimentRecord.id == record.id).with_for_update().execution_options(populate_existing=True))
    ensure_revision(record, payload.expected_record_revision)
    if db.scalar(select(LabRun.id).where(LabRun.active_student_id == user.id).with_for_update()):
        raise api_error(409, 'LAB_TRAINING_ACTIVE', '已有训练任务，请等待完成或先取消')
    if payload.device == 'gpu' and not settings.lab_gpu_enabled:
        raise api_error(422, 'LAB_GPU_UNAVAILABLE', '当前未启用 GPU 训练')
    snapshot = assessments.record_snapshot(db, record)
    return assessments.create_run(db, owner_id=user.id, kind='train', assessment=assessment,
        record=record, snapshot=snapshot, request_key=request_key, device=payload.device)


def require_artifact(db, ident, record, assessment, user):
    artifact = db.scalar(select(LabArtifact).where(LabArtifact.id == ident)
        .with_for_update().execution_options(populate_existing=True)) if ident else None
    if not artifact or artifact.owner_id != user.id or artifact.record_id != record.id:
        raise api_error(403, 'LAB_ARTIFACT_FORBIDDEN', '无权使用此实验成果')
    if artifact.status != 'ready' or artifact.assessment_id != assessment.id:
        raise api_error(409, 'LAB_ARTIFACT_NOT_READY', '请选择当前实验版本已完成校验的成果')
    if assessment.config.get('require_platform_training') and artifact.source != 'platform':
        raise api_error(422, 'LAB_PLATFORM_TRAINING_REQUIRED', '该实验要求使用平台训练成果')
    return artifact


def submit_lab(db, record, user, payload, settings):
    from app.services.account_service import lock_policy
    lock_policy(db)
    # An UPDATE locks SQLite as well as MySQL before quota and attempt allocation.
    db.execute(update(LabRecord).where(LabRecord.record_id == record.id)
               .values(generation=LabRecord.generation + 1))
    existing = db.scalar(select(ExperimentSubmission).where(
        ExperimentSubmission.record_id == record.id,
        ExperimentSubmission.client_request_id == str(payload.client_request_id)).with_for_update())
    if existing:
        return existing
    record = db.scalar(select(ExperimentRecord).where(ExperimentRecord.id == record.id).with_for_update().execution_options(populate_existing=True))
    if record.offering_id:
        from app.services.offering_service import require_delivery
        # Keep the same record -> delivery lock order as class regrading. Once
        # a replacement is pending no new old-version formal score may appear,
        # including after this student's staged regrade run has finished.
        _, delivery = require_delivery(db, record.offering_id,
            'lesson' if record.lesson_id else 'module', record.lesson_id or record.module_id,
            user, lock=True)
        if delivery.pending_assessment_id:
            raise api_error(409, 'ASSESSMENT_SWITCH_PENDING', '本班评测版本正在整批核对，请稍后正式提交')
    ensure_revision(record, payload.expected_record_revision)
    binding = db.scalar(select(LabRecord).where(LabRecord.record_id == record.id).with_for_update().execution_options(populate_existing=True))
    assessment = db.get(LabAssessment, binding.assessment_id)
    cfg = assessment.config
    entry = db.get(Lesson, record.lesson_id) if record.lesson_id else db.get(ExperimentModule, record.module_id)
    now = datetime.now(timezone.utc)
    deadline = getattr(entry, 'due_at', None)
    if deadline and now > deadline.replace(tzinfo=timezone.utc):
        raise api_error(403, 'LAB_DEADLINE_PASSED', '实验提交已截止')
    # Freeze a record while its entire comparable score history is being migrated.
    if db.scalar(select(LabRun.id).where(LabRun.record_id == record.id,
        LabRun.kind == 'evaluate', LabRun.status.in_(['queued','running','recovering']))):
        raise api_error(409, 'LAB_EVALUATION_ACTIVE', '上一份提交正在评测，请等待完成后再提交')
    attempts = len(db.scalars(select(LabSubmission.submission_id)
        .join(ExperimentSubmission, ExperimentSubmission.id == LabSubmission.submission_id)
        .where(ExperimentSubmission.record_id == record.id, LabSubmission.quota_charged.is_(True))
        .with_for_update()).all())
    if attempts >= cfg['max_submissions']:
        raise api_error(409, 'LAB_ATTEMPTS_EXHAUSTED', '已达到正式提交次数上限')
    current_snapshot = assessments.record_snapshot(db, record)
    snapshot, artifact = current_snapshot, None
    if cfg['task_type'] != 'notebook':
        artifact = require_artifact(db, payload.artifact_id, record, assessment, user)
        reports = [c for c in current_snapshot['cells'] if c.get('type') == 'markdown'
                   and c.get('student_editable') and not c.get('source_hidden')]
        snapshot = artifact_snapshot(db, artifact, settings, reports)
    snapshot['assessment_config'] = copy.deepcopy(cfg)
    digest = assessments.digest({'snapshot': snapshot, 'artifact': artifact.manifest_hash if artifact else None,
                                  'assessment': assessment.config_hash})
    prior = db.scalar(select(LabSubmission).join(ExperimentSubmission,
        ExperimentSubmission.id == LabSubmission.submission_id).where(
        ExperimentSubmission.record_id == record.id, LabSubmission.submission_hash == digest,
        LabSubmission.quota_charged.is_(True)))
    if prior:
        raise api_error(409, 'LAB_IDENTICAL_SUBMISSION', '相同成果已提交，可查看原结果或请求教师复核')
    attempt = (db.scalar(select(ExperimentSubmission.attempt_number).where(
        ExperimentSubmission.record_id == record.id).order_by(ExperimentSubmission.attempt_number.desc()).limit(1).with_for_update()) or 0) + 1
    submission = ExperimentSubmission(record_id=record.id, attempt_number=attempt,
        client_request_id=str(payload.client_request_id),
        cells_snapshot={c['id']: c.get('source','') for c in snapshot.get('cells', []) if not c.get('source_hidden')},
        outputs_snapshot={}, submitted_at=now)
    db.add(submission)
    db.flush()
    state = LabSubmission(submission_id=submission.id, assessment_id=assessment.id,
        artifact_id=artifact.id if artifact else None, submission_hash=digest, quota_charged=True)
    db.add(state)
    run = assessments.create_run(db, owner_id=user.id, kind='evaluate', assessment=assessment,
        record=record, snapshot=snapshot, artifact_id=state.artifact_id, submission_id=submission.id)
    state.current_run_id = run.id
    record.status, record.submitted_at = 'submitted', now
    record.record_revision += 1
    db.flush()
    return submission


def submission_lab_read(db, submission, *, teacher=False):
    state = db.get(LabSubmission, submission.id)
    if not state:
        return {'enabled': False}
    assessment = db.get(LabAssessment, state.assessment_id)
    grade = db.get(LabGradeRevision, state.published_grade_id) if state.published_grade_id else None
    history = db.scalars(select(LabGradeRevision).where(
        LabGradeRevision.submission_id == submission.id, LabGradeRevision.published_at.is_not(None))
        .order_by(LabGradeRevision.id.desc())).all()
    return {'enabled': True, 'record_id': submission.record_id, 'assessment': assessments.assessment_read(assessment, teacher=teacher),
        'run': assessments.run_read(db.get(LabRun,state.current_run_id) if state.current_run_id else None, teacher=teacher),
        'grade': grade_read(grade), 'history': [grade_read(g) for g in history],
        'review_requested': state.review_requested, 'review_reason': state.review_reason}


def record_overview(db, record, *, teacher=False):
    assessment = assessments.record_assessment(db, record.id)
    if not assessment:
        return {'enabled': False}
    artifacts = db.scalars(select(LabArtifact).where(LabArtifact.record_id == record.id, LabArtifact.status != 'deleted')
                          .order_by(LabArtifact.id.desc()).limit(20)).all()
    run_rows = db.execute(select(LabRun.id, LabRun.kind, LabRun.status, LabRun.device,
        LabRun.created_at, LabRun.started_at, LabRun.finished_at, LabRun.error_code, LabRun.error_message,
        LabRun.result['metrics'].label('metrics'), LabRun.result['artifact_id'].label('artifact_id'),
        LabRun.result['elapsed_seconds'].label('elapsed_seconds'))
        .where(LabRun.record_id == record.id).order_by(LabRun.id.desc()).limit(20)).mappings().all()
    run_summaries = []
    for row in run_rows:
        summary = {k: row[k] for k in ('id','kind','status','device','created_at','started_at','finished_at','error_code','error_message')}
        public_result = {k: row[k] for k in ('metrics','artifact_id','elapsed_seconds') if row[k] is not None}
        summary.update(logs='', result=public_result if teacher or row['kind'] not in ('evaluate','baseline') else {})
        run_summaries.append(summary)
    submissions = db.scalars(select(ExperimentSubmission).where(ExperimentSubmission.record_id == record.id)
                            .order_by(ExperimentSubmission.id.desc())).all()
    attempts = db.scalar(select(func.count()).select_from(LabSubmission).join(ExperimentSubmission,
        ExperimentSubmission.id == LabSubmission.submission_id).where(
        ExperimentSubmission.record_id == record.id, LabSubmission.quota_charged.is_(True))) or 0
    binding = db.get(LabRecord,record.id)
    selected = db.get(ExperimentSubmission,binding.selected_submission_id) if binding.selected_submission_id else None
    dataset = None
    if assessment.config.get('dataset_id'):
        from app.models import LabDatasetVersion
        obj = db.get(LabDatasetVersion,assessment.config['dataset_id'])
        dataset = data.dataset_projection(obj,teacher=False)
        dataset['files'] = []
        public_paths = ['train/samples.csv', 'train/labels.csv', 'validation/samples.csv', 'validation/labels.csv',
                        'downloads/train.zip', 'downloads/validation.zip']
        for file, stored in db.execute(select(LabFile, StorageObject).join(StorageObject, StorageObject.id == LabFile.object_id)
                .where(LabFile.dataset_version_id == obj.id, LabFile.path.in_(public_paths))):
            dataset['files'].append({'id':file.id,'path':file.path,'role':file.role,'size_bytes':stored.size_bytes})
    return {'enabled': True, 'assessment': assessments.assessment_read(assessment,teacher=teacher),
        'attempts_used': attempts, 'max_submissions':assessment.config['max_submissions'],
        'selected_submission_id': binding.selected_submission_id, 'score': selected.score if selected else None,
        'runs':run_summaries,
        'artifacts':[{**data.artifact_projection(db,a), 'compatible': a.assessment_id == assessment.id} for a in artifacts], 'dataset':dataset,
        'submissions':[{'id':s.id,'attempt_number':s.attempt_number,'submitted_at':s.submitted_at,
                        **submission_lab_read(db,s,teacher=teacher)} for s in submissions]}


def enqueue_regrade(db, submission, user, *, reason, assessment=None, batch_id=None):
    from app.services.lab_grading import lock_lab_records
    lock_lab_records(db, [submission.record_id])
    state = db.scalar(select(LabSubmission).where(LabSubmission.submission_id == submission.id)
        .with_for_update().execution_options(populate_existing=True))
    if not state:
        raise api_error(409,'LAB_NOT_ENABLED','旧人工实验无需自动重评')
    assessment = assessment or db.get(LabAssessment,state.assessment_id)
    old = db.get(LabRun,state.current_run_id) if state.current_run_id else db.scalar(
        select(LabRun).where(LabRun.submission_id == submission.id).order_by(LabRun.id.desc()))
    if not old:
        raise api_error(409,'LAB_EVIDENCE_MISSING','找不到冻结评测证据')
    snapshot = copy.deepcopy(old.snapshot)
    snapshot.pop('batch_id',None)
    snapshot['reason'] = reason
    if batch_id:
        snapshot['batch_id'] = batch_id
    run = assessments.create_run(db,owner_id=user.id,kind='evaluate',assessment=assessment,
        record=db.get(ExperimentRecord,submission.record_id),snapshot=snapshot,
        artifact_id=state.artifact_id,submission_id=submission.id,request_key='regrade:'+str(uuid4()))
    state.current_run_id = run.id
    return run
