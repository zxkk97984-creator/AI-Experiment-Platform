"""Authorization and immutable assessment publishing for both lab entry types."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.errors import api_error
from app.models import (EnvironmentVersion, ExperimentModule, ExperimentRecord, LabArtifact,
                        LabAssessment, LabDatasetVersion, LabRecord, LabRun, Lesson,
                        NotebookTemplate, NotebookTemplateVersion, StudioAssetManifest)
from app.schemas.lab import LabAssessmentConfig
from app.services.environment_service import resolve_run_image_ref


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).encode()).hexdigest()


def entry_key(kind, ident):
    if kind not in ('lesson', 'module'):
        raise api_error(404, 'LAB_ENTRY_NOT_FOUND', '实验入口不存在')
    return f'{kind}:{ident}'


def managed_entry(db, kind, ident, user):
    model = Lesson if kind == 'lesson' else ExperimentModule if kind == 'module' else None
    entry = db.get(model, ident) if model else None
    if not entry:
        raise api_error(404, 'LAB_ENTRY_NOT_FOUND', '实验入口不存在')
    owner_id = (entry.chapter.course.teacher_id if entry.chapter and entry.chapter.course else None) if kind == 'lesson' else entry.owner_id
    if user.role != 'admin' and (user.role != 'teacher' or owner_id != user.id):
        raise api_error(403, 'FORBIDDEN', '无权管理此实验')
    return entry


def require_record(db, ident, user, *, current_access=True, write=False):
    from app.services.offering_service import record_teaching_access, require_delivery
    record = db.get(ExperimentRecord, ident)
    if not record:
        raise api_error(404, 'RECORD_NOT_FOUND', '实验记录不存在')
    record_teaching_access(db, record, user, write=write)
    if current_access and record.offering_id and user.role == 'student':
        require_delivery(db, record.offering_id, 'lesson' if record.lesson_id else 'module',
            record.lesson_id or record.module_id, user, write=write)
    return record


def assessment_read(row, *, teacher=False):
    if row is None:
        return None
    config = copy.deepcopy(row.config)
    if not teacher:
        for key in ('reference_cells', 'reference_artifact_id', 'check_script', 'benchmark_node_id'):
            config.pop(key, None)
        config['cell_checks'] = [{'id': c['id'], 'cell_id': c['cell_id']} for c in config.get('cell_checks', [])]
    return dict(id=row.id, version=row.version, status=row.status, config=config,
                template_version_id=row.template_version_id,
                validated=bool(row.validated_hash and row.validated_hash == row.config_hash),
                validation_run_id=row.validation_run_id if teacher else None,
                published_at=row.published_at)


def get_assessments(db, kind, ident):
    rows = db.scalars(select(LabAssessment).where(LabAssessment.entry_key == entry_key(kind, ident))
                      .order_by(LabAssessment.version.desc())).all()
    return (next((r for r in rows if r.status == 'draft'), None),
            next((r for r in rows if r.status == 'published'), None))


def template_hash(db, template):
    from app.services.studio_service import _normalize_cells
    manifest = db.scalar(select(StudioAssetManifest).where(StudioAssetManifest.template_id == template.id))
    return digest({'cells': _normalize_cells(template.draft_cells), 'metadata': template.draft_metadata,
                   'environment': template.draft_environment_version_id,
                   'imports': [template.draft_import_policy_mode, template.draft_allowed_imports],
                   'assets': [template.draft_assets_dir, manifest.revision if manifest else None]})


def save_draft(db, kind, ident, user, payload: LabAssessmentConfig, settings):
    entry = managed_entry(db, kind, ident, user)
    # Serialize version allocation on the durable entry, including empty drafts.
    db.execute(select(type(entry)).where(type(entry).id == ident).with_for_update())
    draft, published = get_assessments(db, kind, ident)
    if draft is None:
        version = (db.scalar(select(func.max(LabAssessment.version)).where(
            LabAssessment.entry_key == entry_key(kind, ident))) or 0) + 1
        draft = LabAssessment(entry_key=entry_key(kind, ident), owner_id=user.id,
                              version=version, status='draft')
        db.add(draft)
    config = bind_benchmark_config(payload.model_dump(), settings)
    if config['dataset_id']:
        dataset = db.get(LabDatasetVersion, config['dataset_id'])
        if not dataset or (user.role != 'admin' and dataset.owner_id != user.id):
            raise api_error(403, 'LAB_DATASET_FORBIDDEN', '无权使用此数据集')
    if config['reference_artifact_id']:
        artifact = db.get(LabArtifact, config['reference_artifact_id'])
        if not artifact or (user.role != 'admin' and artifact.owner_id != user.id) or artifact.source != 'reference':
            raise api_error(403, 'LAB_ARTIFACT_FORBIDDEN', '请选择本人的参考成果')
    template = db.get(NotebookTemplate, entry.template_id) if entry.template_id else None
    current_template_hash = template_hash(db, template) if template else None
    if template and not draft.template_version_id and published and published.template_draft_hash == current_template_hash:
        draft.template_version_id = published.template_version_id
        draft.template_draft_revision = published.template_draft_revision
        draft.template_draft_hash = current_template_hash
    if template and (not draft.template_version_id or draft.template_draft_hash != current_template_hash):
        from app.services.studio_service import publish_template
        version = publish_template(db, settings, template.id, user, activate=False, commit=False)
        draft.template_version_id = version.id
        draft.template_draft_revision = template.draft_revision
        draft.template_draft_hash = current_template_hash
    old_hash = draft.config_hash
    draft.config = config
    draft.config_hash = digest({'config': config, 'template_version_id': draft.template_version_id})
    if draft.config_hash != old_hash:
        draft.validated_hash = None
        draft.validation_run_id = None
    db.flush()
    return draft


def bind_benchmark_config(config, settings):
    if any(item.get('source') == 'efficiency' for item in config.get('criteria', [])):
        node = settings.lab_benchmark_node_id
        if not node:
            raise api_error(422, 'LAB_BENCHMARK_NOT_CONFIGURED', '管理员尚未配置专用效率基准节点，请将效率权重设为零')
        if config.get('benchmark_node_id') and config['benchmark_node_id'] != node:
            raise api_error(422, 'LAB_BENCHMARK_NODE_MISMATCH', '评分基准节点与当前管理员配置不一致，请核对后重新试评')
        config = {**config, 'benchmark_node_id': node}
    return config


def validate_configuration(db, row, settings, *, publishing=False):
    cfg = LabAssessmentConfig.model_validate(row.config)
    if any(item.source == 'efficiency' for item in cfg.criteria):
        bind_benchmark_config(row.config, settings)
        if not cfg.benchmark_node_id:
            raise api_error(422, 'LAB_BENCHMARK_NODE_REQUIRED', '效率评分须冻结专用基准节点，请重新保存配置并试评')
    if not row.template_version_id:
        raise api_error(422, 'LAB_TEMPLATE_REQUIRED', '请先保存并发布 Notebook 模板')
    if not cfg.criteria:
        raise api_error(422, 'LAB_CRITERIA_REQUIRED', '请设置满分合计100分的评分量表')
    metric_items = [c for c in cfg.criteria if c.source == 'metric']
    if cfg.task_type != 'notebook':
        dataset = db.get(LabDatasetVersion, cfg.dataset_id) if cfg.dataset_id else None
        if not dataset or dataset.status != 'ready' or dataset.task_type != cfg.task_type:
            raise api_error(422, 'LAB_DATASET_REQUIRED', '请选择校验通过且类型匹配的数据集')
        if len(metric_items) != 1:
            raise api_error(422, 'LAB_METRIC_REQUIRED', '模型实验必须有且仅有一个客观指标评分项')
        if not cfg.reference_artifact_id and not cfg.reference_cells:
            raise api_error(422, 'LAB_REFERENCE_REQUIRED', '请提供完整参考 Notebook 或参考模型包进行试评')
    elif metric_items:
        raise api_error(422, 'LAB_METRIC_NOT_APPLICABLE', '普通 Notebook 请使用代码检查或 AI 评分项')
    if any(c.source == 'check' for c in cfg.criteria) and not cfg.check_script.strip():
        raise api_error(422, 'LAB_CHECK_REQUIRED', '代码检查项需要提供检查脚本')
    if cfg.reference_artifact_id:
        artifact = db.scalar(select(LabArtifact).where(LabArtifact.id == cfg.reference_artifact_id)
            .with_for_update().execution_options(populate_existing=True))
        if not artifact or artifact.status != 'ready':
            raise api_error(422, 'LAB_REFERENCE_NOT_READY', '参考模型包尚未校验通过')
    template = db.get(NotebookTemplateVersion, row.template_version_id)
    cells = {c['id']: c for c in template.cells or []}
    for check in cfg.cell_checks:
        cell = cells.get(check.cell_id)
        if not cell or cell.get('type', cell.get('cell_type')) != 'code' or cell.get('source_hidden'):
            raise api_error(422, 'LAB_CELL_CHECK_TARGET', 'Cell 检查必须绑定当前模板中可见的代码单元格')
    if cfg.environment_version_id is not None and cfg.environment_version_id != template.environment_version_id:
        raise api_error(422, 'LAB_ENVIRONMENT_MISMATCH', '评分环境必须与当前 Notebook 模板环境一致，请先在模板中选择该环境')
    environment_id = cfg.environment_version_id or template.environment_version_id
    image = resolve_run_image_ref(db, environment_id)
    environment = db.get(EnvironmentVersion, environment_id)
    for resources in (cfg.resources, cfg.evaluation_resources):
        if resources.memory_mb < environment.minimum_memory_mb:
            raise api_error(422, 'MEMORY_BELOW_ENV_MIN', '运行内存小于所选软件环境的最低要求')
        if resources.memory_mb > settings.lab_total_memory_mb - settings.lab_interactive_reserved_memory_mb or resources.cpus > settings.lab_total_cpus:
            raise api_error(422, 'LAB_RESOURCE_UNAVAILABLE', '资源申请超过管理员配置的后台任务预算')
    if cfg.gpu_environment_version_id:
        if not settings.lab_gpu_enabled or not settings.lab_gpu_devices.strip():
            raise api_error(422, 'LAB_GPU_UNAVAILABLE', '管理员尚未启用可用 GPU')
        resolve_run_image_ref(db, cfg.gpu_environment_version_id)
    if publishing:
        if not cfg.confirmed:
            raise api_error(422, 'LAB_CONFIRMATION_REQUIRED', '请确认评分量表后发布')
        if row.validated_hash != row.config_hash:
            raise api_error(422, 'LAB_BASELINE_REQUIRED', '请先对当前配置完成参考试评')
        if any(c.source == 'ai' for c in cfg.criteria) and not settings.ai_ready:
            raise api_error(503, 'AI_NOT_READY', '尚未启用 AI 评分或配置模型服务')
    return cfg, environment_id, image


def publish_assessment(db, kind, ident, user, settings):
    entry = managed_entry(db, kind, ident, user)
    draft, _ = get_assessments(db, kind, ident)
    if not draft:
        raise api_error(404, 'LAB_DRAFT_NOT_FOUND', '请先保存实验配置')
    db.execute(select(LabAssessment).where(LabAssessment.id == draft.id).with_for_update())
    template = db.get(NotebookTemplate, entry.template_id)
    if not template or template_hash(db, template) != draft.template_draft_hash:
        raise api_error(409, 'LAB_TEMPLATE_CHANGED', 'Notebook 模板已变化，请重新保存配置并试评')
    validate_configuration(db, draft, settings, publishing=True)
    template.current_version_id = draft.template_version_id
    template.status = 'published'
    entry.status = 'published'
    draft.status = 'published'
    draft.published_at = datetime.now(timezone.utc)
    db.flush()
    return draft


def bind_new_record(db, record):
    kind, ident = ('lesson', record.lesson_id) if record.lesson_id else ('module', record.module_id)
    if record.offering_id:
        from app.models.teaching import OfferingExperiment
        delivery = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == record.offering_id,
            OfferingExperiment.entry_kind == kind, OfferingExperiment.entry_id == ident))
        published = db.get(LabAssessment, delivery.assessment_id) if delivery and delivery.assessment_id else None
    else:
        _, published = get_assessments(db, kind, ident)
    if published and published.template_version_id == record.template_version_id:
        db.add(LabRecord(record_id=record.id, assessment_id=published.id))
        db.flush()


def record_assessment(db, record_id):
    binding = db.get(LabRecord, record_id)
    return db.get(LabAssessment, binding.assessment_id) if binding else None


def record_snapshot(db, record):
    version = db.get(NotebookTemplateVersion, record.template_version_id)
    cells = copy.deepcopy(version.cells or [])
    for cell in cells:
        if not cell.get('source_hidden'):
            cell['source'] = record.cells_sources.get(cell['id'], cell.get('source', ''))
    return {'cells': cells, 'record_revision': record.record_revision,
            'template_version_id': record.template_version_id,
            'environment_version_id': record.environment_version_id}


def create_run(db, *, owner_id, kind, assessment=None, record=None, snapshot=None,
               artifact_id=None, dataset_id=None, submission_id=None, request_key=None, device='cpu'):
    run = LabRun(owner_id=owner_id, kind=kind, status='queued',
                 assessment_id=assessment.id if assessment else None,
                 record_id=record.id if record else None, snapshot=copy.deepcopy(snapshot or {}),
                 artifact_id=artifact_id, dataset_id=dataset_id, submission_id=submission_id,
                 request_key=request_key, device=device)
    if assessment:
        config = assessment.config
        resource_key = 'resources' if kind in ('train', 'baseline') else 'evaluation_resources'
        run.resources = copy.deepcopy(config[resource_key])
        if kind == 'baseline' or (kind in ('evaluate', 'public_validate', 'cell_check') and config.get('cell_checks')):
            # A baseline can train and then run CPU inference/checks. Reserve
            # the largest stage budget so a larger evaluator cannot oversubscribe.
            for key in ('cpus', 'memory_mb', 'disk_mb'):
                run.resources[key] = max(config['resources'][key], config['evaluation_resources'][key])
        version = db.get(NotebookTemplateVersion, assessment.template_version_id)
        env_id = config.get('environment_version_id') or version.environment_version_id
        if device == 'gpu':
            env_id = config.get('gpu_environment_version_id')
            if not env_id:
                raise api_error(422, 'LAB_GPU_UNAVAILABLE', '该实验尚未配置 GPU 环境')
        run.snapshot = {**run.snapshot, 'assessment_config': copy.deepcopy(config), 'image_ref': resolve_run_image_ref(db, env_id),
                        'template_version_id': assessment.template_version_id,
                        'environment_version_id': env_id,
                        'assessment_hash': assessment.config_hash}
        if kind in ('baseline', 'evaluate') and any(item.get('source') == 'efficiency' for item in config.get('criteria', [])):
            if not config.get('benchmark_node_id'):
                raise api_error(409, 'LAB_BENCHMARK_NODE_REQUIRED', '效率评分版本未冻结基准节点，请管理员核对')
            run.snapshot['benchmark_node_id'] = config['benchmark_node_id']
        run.dataset_id = config.get('dataset_id')
    if kind == 'train' and record:
        run.active_student_id = record.student_id
    db.add(run)
    db.flush()
    return run


def run_read(run, *, teacher=False):
    if run is None:
        return None
    # Validation/evaluation may also execute private teacher checks. Their mixed
    # logs can echo scripts or reference values, even on public input datasets.
    result = {k: copy.deepcopy(v) for k, v in (run.result or {}).items()
              if k in ('metrics', 'curves', 'artifact_id', 'elapsed_seconds', 'program_elapsed_seconds',
                       'efficiency_samples_seconds', 'items', 'checks', 'score', 'cell_checks', 'record_revision', 'cell_id')}
    if not teacher and run.kind in ('evaluate', 'baseline'):
        result = {}
    return dict(id=run.id, kind=run.kind, status=run.status, device=run.device,
                result=result, logs=run.logs if teacher or run.kind == 'train' else '',
                error_code=run.error_code, error_message=run.error_message,
                created_at=run.created_at, started_at=run.started_at, finished_at=run.finished_at)
