"""Lab orchestration: immutable inputs, isolated execution, fenced publication.

This module only parses bounded CSV/JSON on the host. Uploaded Python and model
files are executed exclusively by lab_runtime's offline Docker sandboxes.
"""
from __future__ import annotations

import copy
import json
import math
import statistics
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from sqlalchemy import func, select, update

from app.models import NotebookTemplateVersion
from app.models.lab import LabAssessment, LabResourceLease, LabRun, LabSubmission
from app.models.lab_data import LabArtifact, LabDatasetVersion
from app.services.lab_archive import LabDataError
from app.services.lab_data_service import (archive_artifact, materialize_artifact,
    materialize_dataset, validate_artifact, validate_dataset)
from app.services.lab_queue import (LeaseGuard, LeaseLost, RunCancelled, append_logs,
    assert_run_owned, finish_run, get_run_guard)
from app.services.lab_resources import release_resources
from app.services.lab_runtime import (RuntimeFailure, execute_check,
    execute_prediction, execute_training, stop_run_containers)
from app.services.lab_cell_checks import observation_requests, read_observations, template_marks


def normalise_cells(cells):
    """Persisted Notebook cells use type; ipynb/Jupyter uses cell_type."""
    result = []
    for cell in cells or []:
        kind = cell.get("cell_type", cell.get("type", "code"))
        if kind not in {"code", "markdown", "raw"}:
            raise LabDataError("Notebook 单元格类型不受支持")
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        if not isinstance(source, str):
            raise LabDataError("Notebook 单元格内容必须为文字")
        result.append({**cell, "cell_type": kind, "source": source})
    return result


def read_curves(directory):
    """Student-provided learning telemetry, never input to objective scoring."""
    path = Path(directory) / "metrics.json"
    if not path.is_file() or path.stat().st_size > 1048576:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        return []
    if not isinstance(data, list) or len(data) > 10000:
        return []
    allowed = {"epoch", "train_loss", "val_loss", "train_accuracy", "val_accuracy", "learning_rate"}
    result = []
    for item in data:
        if not isinstance(item, dict):
            return []
        values = {key: value for key, value in item.items() if key in allowed}
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in values.values()):
            return []
        if "epoch" not in values or values["epoch"] < 0:
            return []
        result.append(values)
    return result


def _read_checks(directory, config, *, required=None):
    path = Path(directory) / "checks.json"
    if not path.is_file() or path.stat().st_size > 65536:
        raise LabDataError("检查脚本须输出不超过64KiB的 checks.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise LabDataError("检查脚本结果不是有效的 JSON") from exc
    if required is None:
        required = {item["id"] for item in config.get("criteria", []) if item.get("source") == "check"}
    if not isinstance(value, dict) or set(value) != required:
        raise LabDataError("检查脚本结果须完整对应已确认的检查评分项")
    for item in value.values():
        if not isinstance(item, dict) or not isinstance(item.get("passed"), bool):
            raise LabDataError("检查脚本结果须包含布尔型 passed")
        if set(item) - {"passed", "message"} or not isinstance(item.get("message", ""), str) or len(item.get("message", "")) > 2000:
            raise LabDataError("检查脚本结果字段或长度无效")
    return value


def _public_course(db, dataset_id, destination, settings):
    destination.mkdir(parents=True, exist_ok=True, mode=0o755)
    if dataset_id:
        for split in ("train", "validation"):
            materialize_dataset(db, dataset_id, split, destination / split, settings, include_labels=True)
    return destination


def _require_success(result):
    if result.exit_code != 0:
        raise RuntimeFailure("代码未能在实验环境中完成运行，请检查运行日志。", "STUDENT_EXECUTION_FAILED")


def _identity(run):
    return dict(run_id=run.id, lease_token=run.lease_token)


def _make_log_sink(session_factory, run, settings):
    def on_log(text):
        with session_factory() as db:
            append_logs(db, run.id, run.lease_token, text, settings)
    return on_log


def _load_context(session_factory, run_id, token, settings):
    with session_factory() as db:
        assert_run_owned(db, run_id, token)
        run = db.get(LabRun, run_id)
        from app.services.lab_node import benchmark_daemon_ready, is_benchmark, matches_worker
        if is_benchmark(run.kind, run.snapshot) and (not matches_worker(run.kind, run.snapshot, settings)
                or not benchmark_daemon_ready(settings)):
            raise RuntimeFailure('基准节点身份或隔离条件无法确认，暂停效率评分。',
                'LAB_BENCHMARK_NODE_UNAVAILABLE', infrastructure=True)
        assessment = db.get(LabAssessment, run.assessment_id) if run.assessment_id else None
        config = copy.deepcopy(run.snapshot.get("assessment_config") or (assessment.config if assessment else {}))
        version_id = run.snapshot.get("template_version_id") or (assessment.template_version_id if assessment else None)
        version = db.get(NotebookTemplateVersion, version_id) if version_id else None
        template_cells = copy.deepcopy(version.cells or []) if version else []
        from app.services.lab_resources import resource_key
        resource_lease = db.get(LabResourceLease, resource_key(f"run:{run_id}", settings))
        gpu = resource_lease.gpu if resource_lease else None
        if assessment:
            frozen = SimpleNamespace(id=assessment.id, config=config, owner_id=assessment.owner_id,
                template_version_id=assessment.template_version_id, config_hash=run.snapshot.get("assessment_hash"),
                entry_key=assessment.entry_key, status=assessment.status)
        else:
            frozen = None
        db.expunge(run)
        return run, frozen, config, template_cells, gpu


def _training(session_factory, run, config, cells, root, settings, guard, on_log, gpu):
    from app.services.lab_workspace import prepare_course_workspace
    with session_factory() as db:
        version_id = run.snapshot.get("template_version_id")
        version = db.get(NotebookTemplateVersion, version_id) if version_id else None
        if version is not None:
            course = prepare_course_workspace(db, settings, version=version, dataset_id=run.dataset_id)
        else:
            course = _public_course(db, run.dataset_id, root / "course", settings)
    guard.assert_owned()
    result = execute_training(**_identity(run), image_ref=run.snapshot["image_ref"],
        cells=normalise_cells(cells), course_dir=course, work_dir=root / "trained",
        resources=run.resources or config.get("resources", {}), settings=settings,
        device=run.device, gpu=gpu, should_continue=guard.assert_owned, on_log=on_log,
        observations=observation_requests(config.get('cell_checks', [])))
    _require_success(result)
    return result


def _archive_training(db, run, cells, output_dir, settings, *, reference=False, require_predict=True):
    # Even an introductory Notebook that only prints has a useful immutable
    # source artifact. The authoritative snapshot is stored alongside the file.
    notebook = Path(output_dir) / "platform-training.ipynb"
    notebook.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {},
        "cells": [{"cell_type": cell["cell_type"], "source": cell["source"], "metadata": {},
            **({"outputs": [], "execution_count": None} if cell["cell_type"] == "code" else {})}
            for cell in normalise_cells(cells) if not cell.get("source_hidden")]}, ensure_ascii=False), encoding="utf-8")
    return archive_artifact(db, run.owner_id, output_dir,
        {**run.snapshot, "cells": copy.deepcopy(cells)}, settings,
        record_id=run.record_id, assessment_id=run.assessment_id, run_id=run.id,
        source="reference" if reference else "platform", require_predict=require_predict)


def _objective_metrics(session_factory, run, config, artifact_dir, root, settings, guard, on_log):
    from app.services.lab_grading import compute_metrics

    split = "validation" if run.kind == "public_validate" else "test"
    with session_factory() as db:
        materialize_dataset(db, run.dataset_id, split, root / "input", settings, include_labels=False)
        dataset = db.get(LabDatasetVersion, run.dataset_id)
        labels = list(dataset.summary.get("class_labels", []))
    guard.assert_owned()
    result = execute_prediction(**_identity(run), image_ref=run.snapshot["image_ref"],
        artifact_dir=artifact_dir, input_dir=root / "input", output_dir=root / "predicted",
        resources=config.get("evaluation_resources", run.resources), settings=settings,
        should_continue=guard.assert_owned, on_log=on_log)
    _require_success(result)
    # Labels do not even exist in this run's workspace until inference stopped.
    # The scorer directory is a sibling and is NEVER a container mount.
    with session_factory() as db:
        materialize_dataset(db, run.dataset_id, split, root / "scorer", settings, include_labels=True)
    try:
        metrics = compute_metrics(config["task_type"], root / "scorer" / "labels.csv",
            root / "predicted" / "predictions.csv", class_labels=labels or None)
    except (ValueError, OSError) as exc:
        raise LabDataError("模型预测未满足样本清单、数值或类别协议，请检查 predict.py。") from exc
    return metrics, result


def _checks(run, config, snapshot, metrics, root, settings, guard, on_log, *, observations=None):
    script = config.get("check_script")
    if not script:
        return {}
    evidence = root / "evidence"
    evidence.mkdir(mode=0o755)
    # Output/log text is untrusted; hidden per-sample labels and platform
    # credentials never enter the checker or the AI evidence context.
    (evidence / "evidence.json").write_text(json.dumps({"cells": snapshot.get("cells", []),
        "metrics": metrics, "observations": observations or {}}, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    predictions = root / "predicted" / "predictions.csv"
    if predictions.is_file():
        import shutil
        shutil.copyfile(predictions, evidence / "predictions.csv")
    result = execute_check(**_identity(run), image_ref=run.snapshot["image_ref"], script=script,
        evidence_dir=evidence, output_dir=root / "checks", resources=config.get("evaluation_resources", run.resources),
        settings=settings, should_continue=guard.assert_owned, on_log=on_log)
    _require_success(result)
    return _read_checks(root / "checks", config)


def _cell_checks(run, config, snapshot, execution, root, settings, guard, on_log):
    rules = config.get('cell_checks', [])
    if run.kind == 'cell_check' and not run.snapshot.get('check_all'):
        rules = [r for r in rules if r['cell_id'] == run.snapshot['cell_id']]
    if not rules:
        return {}, {}
    observations = read_observations(execution.output_dir)
    marks = template_marks(rules, observations)
    for rule in rules:
        if rule['kind'] != 'python':
            continue
        directory = root / f"cell-check-{rule['id']}"
        evidence = directory / 'evidence'
        evidence.mkdir(parents=True, mode=0o755)
        (evidence / 'evidence.json').write_text(json.dumps({'cells': snapshot.get('cells', []),
            'cell_id': rule['cell_id'], 'observations': observations}, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        result = execute_check(**_identity(run), image_ref=run.snapshot['image_ref'], script=rule['script'],
            evidence_dir=evidence, output_dir=directory / 'output',
            resources=config.get('evaluation_resources', run.resources), settings=settings,
            should_continue=guard.assert_owned, on_log=on_log)
        _require_success(result)
        value = _read_checks(directory / 'output', config, required={rule['id']})[rule['id']]
        # Check scripts are private; only a generic outcome reaches students.
        marks[rule['id']] = {'passed': value['passed'],
            'message': '检查通过' if value['passed'] else '尚未满足本单元格要求，请检查实现'}
    for rule in rules:
        marks[rule['id']]['cell_id'] = rule['cell_id']
    return marks, observations


def _program_time(result):
    value = getattr(result, 'program_elapsed_seconds', None)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise LabDataError('未获得执行节点测量的程序耗时，需重新评测')
    return value


def _commit_completion(db, run, status, result=None, **kwargs):
    from app.services.account_service import lock_policy
    lock_policy(db)
    if run.submission_id:
        from app.services.lab_grading import lock_evaluation_records
        lock_evaluation_records(db, run)
    if not finish_run(db, run.id, run.lease_token, status, result=result, **kwargs):
        db.rollback()
        raise LeaseLost("The lab run changed before publication")


def _validate_upload(session_factory, run, settings, guard):
    with session_factory() as db:
        assert_run_owned(db, run.id, run.lease_token)
        if run.kind == "dataset_validate":
            obj = validate_dataset(db, run.dataset_id, settings)
            result = {"dataset_id": obj.id}
        else:
            obj = validate_artifact(db, run.artifact_id, settings,
                require_predict=run.snapshot.get("require_predict", True))
            result = {"artifact_id": obj.id}
        _commit_completion(db, run, "succeeded" if obj.status == "ready" else "failed", result,
            error_code=None if obj.status == "ready" else "INVALID_UPLOAD",
            error_message=obj.error)
        db.commit()


def _process_owned(session_factory, run_id, token, settings, guard):
    from app.services.lab_grading import grade_evidence, publish_evaluation, refresh_regrade_batch

    run, assessment, config, template_cells, gpu = _load_context(session_factory, run_id, token, settings)
    if run.kind in {"dataset_validate", "artifact_validate"}:
        _validate_upload(session_factory, run, settings, guard)
        return
    if assessment is None:
        raise LabDataError("实验评分版本不存在")
    root = Path(settings.lab_work_path)
    root.mkdir(parents=True, exist_ok=True)
    on_log = _make_log_sink(session_factory, run, settings)
    with TemporaryDirectory(prefix=f"run-{run.id}-{token}-", dir=root) as work:
        work = Path(work)
        task_type = config.get("task_type", "notebook")
        is_baseline = run.kind == "baseline"
        cells = (config.get("reference_cells") or template_cells) if is_baseline else run.snapshot.get("cells", [])
        grade_snapshot = {**run.snapshot, "cells": copy.deepcopy(cells)}
        if run.kind == "train":
            trained = _training(session_factory, run, config, cells, work, settings, guard, on_log, gpu)
            guard.assert_owned()
            with session_factory() as db:
                from app.services.account_service import lock_policy
                lock_policy(db)
                artifact = _archive_training(db, run, cells, trained.output_dir, settings,
                    require_predict=task_type != "notebook")
                result = {"artifact_id": artifact.id, "curves": read_curves(trained.output_dir),
                    "elapsed_seconds": trained.elapsed_seconds}
                _commit_completion(db, run, "succeeded", result)
                db.execute(update(LabRun).where(LabRun.id == run.id, LabRun.lease_token == token).values(artifact_id=artifact.id))
                db.commit()
            return
        if run.kind not in {"baseline", "evaluate", "public_validate", "cell_check"}:
            raise LabDataError("不支持的实验任务类型")
        artifact_id = config.get("reference_artifact_id") if is_baseline else run.artifact_id
        reference_output = None
        metrics = {}
        if task_type == "notebook" or run.kind == 'cell_check':
            execution = _training(session_factory, run, config, cells, work, settings, guard, on_log, gpu)
        else:
            if artifact_id:
                with session_factory() as db:
                    artifact_dir = materialize_artifact(db, artifact_id, work / "artifact", settings)
                    artifact = db.get(LabArtifact, artifact_id)
                    if is_baseline:
                        from app.services.lab_submission_service import artifact_snapshot
                        grade_snapshot = {**grade_snapshot, **artifact_snapshot(db, artifact, settings)}
            elif is_baseline:
                trained = _training(session_factory, run, config, cells, work, settings, guard, on_log, gpu)
                artifact_dir = reference_output = trained.output_dir
                if not (artifact_dir / "predict.py").is_file():
                    raise LabDataError("参考实验必须生成 predict.py 预测入口")
            else:
                raise LabDataError("请选择已归档的模型成果")
            metrics, execution = _objective_metrics(session_factory, run, config, artifact_dir, work, settings, guard, on_log)
        cell_execution = execution
        if task_type != 'notebook' and run.kind != 'cell_check' and config.get('cell_checks'):
            replay_cells = cells if is_baseline else grade_snapshot.get('cells', [])
            wanted = {r['cell_id'] for r in config['cell_checks']}
            if not wanted <= {c.get('id') for c in replay_cells}:
                raise LabDataError('模型成果缺少完整且稳定编号的 Notebook 单元格；请在平台重新训练后提交')
            cell_execution = _training(session_factory, run, config, replay_cells,
                work / 'notebook-cell-replay', settings, guard, on_log, None)
            grade_snapshot['cells'] = copy.deepcopy(replay_cells)
        cell_marks, observations = _cell_checks(run, config, grade_snapshot, cell_execution, work, settings, guard, on_log)
        if is_baseline and any(not mark['passed'] for mark in cell_marks.values()):
            raise LabDataError('参考实现未通过全部 Cell 检查，请修正参考代码或评分规则后重新试评')
        if run.kind == 'cell_check':
            with session_factory() as db:
                _commit_completion(db, run, 'succeeded', {'cell_checks': cell_marks,
                    'cell_id': run.snapshot['cell_id'], 'record_revision': run.snapshot['record_revision']})
                db.commit()
            return
        checks = _checks(run, config, grade_snapshot, metrics, work, settings, guard, on_log, observations=observations)
        samples = []
        program_seconds = getattr(execution, 'program_elapsed_seconds', None)
        if run.kind != 'public_validate' and any(c.get('source') == 'efficiency' for c in config.get('criteria', [])):
            samples.append(_program_time(execution))
            for index in range(2):
                repeat = work / f'efficiency-{index}'
                if task_type == 'notebook':
                    measured = _training(session_factory, run, config, cells, repeat, settings, guard, on_log, None)
                    repeated_marks, _ = _cell_checks(run, config, grade_snapshot, measured, repeat, settings, guard, on_log)
                    if repeated_marks != cell_marks:
                        raise LabDataError('三次重放的 Cell 检查结果不一致，请教师复核')
                else:
                    repeated_metrics, measured = _objective_metrics(session_factory, run, config, artifact_dir, repeat, settings, guard, on_log)
                    if repeated_metrics != metrics:
                        raise LabDataError('三次推理指标不一致，请检查确定性并申请教师复核')
                samples.append(_program_time(measured))
            program_seconds = statistics.median(samples)
        guard.assert_owned()
        if run.kind == "public_validate":
            with session_factory() as db:
                _commit_completion(db, run, "succeeded", {"metrics": metrics, "checks": checks,
                    "elapsed_seconds": execution.elapsed_seconds})
                db.commit()
            return
        with session_factory() as db:
            # AI sees only code/report, trusted aggregate metrics and sandboxed
            # checks. A successful process exit by itself never earns a grade.
            grade = grade_evidence(db, assessment, grade_snapshot, metrics, checks, settings,
                cell_checks=cell_marks, program_seconds=program_seconds)
        guard.assert_owned()
        with session_factory() as db:
            from app.services.account_service import lock_policy
            lock_policy(db)
            result = {**grade, "score": grade["total"], "checks": checks,
                "cell_checks": cell_marks, 'program_elapsed_seconds': program_seconds,
                'efficiency_samples_seconds': samples, "elapsed_seconds": execution.elapsed_seconds}
            if reference_output is not None:
                artifact = _archive_training(db, run, cells, reference_output, settings, reference=True)
                result["artifact_id"] = artifact.id
            _commit_completion(db, run, "succeeded", result)
            if is_baseline:
                changed = db.execute(update(LabAssessment).where(
                    LabAssessment.id == run.assessment_id,
                    LabAssessment.config_hash == run.snapshot.get("assessment_hash"),
                    LabAssessment.status == "draft",
                ).values(validated_hash=run.snapshot.get("assessment_hash"), validation_run_id=run.id)).rowcount
                if not changed:
                    db.rollback()
                    raise LabDataError("实验配置已变更，请按当前版本重新试跑")
            else:
                live_run = db.get(LabRun, run.id, populate_existing=True)
                publish_evaluation(db, live_run, grade)
                if run.snapshot.get("batch_id"):
                    refresh_regrade_batch(db, run.snapshot["batch_id"])
            db.commit()


def _failure(session_factory, run_id, token, settings, exc):
    from app.services.lab_grading import refresh_regrade_batch

    if isinstance(exc, RuntimeFailure) and exc.code == "SANDBOX_CLEANUP_FAILED":
        raise exc
    infrastructure = isinstance(exc, RuntimeFailure) and exc.infrastructure
    with session_factory() as db:
        run = db.get(LabRun, run_id)
        if not run or run.lease_token != token or run.status != "running":
            return
        if isinstance(exc, RunCancelled) or run.cancel_requested:
            if not stop_run_containers(run_id, token):
                raise RuntimeFailure("取消清理待恢复", "SANDBOX_CLEANUP_FAILED", infrastructure=True)
            _commit_completion(db, run, "cancelled")
        elif infrastructure and run.attempts < settings.lab_max_attempts:
            # Runtime guarantees sandbox teardown before raising any other
            # infrastructure error; retry with a fresh fencing token.
            if run.submission_id:
                from app.services.lab_grading import lock_evaluation_records
                lock_evaluation_records(db, run)
            assert_run_owned(db, run_id, token)
            now = db.scalar(select(func.now()))
            changed = db.execute(update(LabRun).where(LabRun.id == run_id,
                LabRun.lease_token == token, LabRun.status == "running", LabRun.lease_until > now,
                LabRun.cancel_requested.is_(False),
            ).values(status="queued", lease_token=None, lease_until=None,
                error_code=exc.code, error_message="运行服务暂时不可用，系统正在重试。")).rowcount
            if not changed:
                raise LeaseLost("The lab run changed before retry")
            release_resources(db, f"run:{run_id}", token)
        else:
            status = "failed" if infrastructure or run.kind in {"train", "dataset_validate", "artifact_validate"} else "needs_review"
            code = getattr(exc, "code", "REVIEW_REQUIRED")
            message = str(exc)[:500] if isinstance(exc, (LabDataError, RuntimeFailure)) else "评分证据不足或评分服务异常，已转交教师复核。"
            _commit_completion(db, run, status, error_code=code, error_message=message)
            if infrastructure and run.submission_id:
                db.execute(update(LabSubmission).where(LabSubmission.submission_id == run.submission_id,
                    LabSubmission.current_run_id == run.id).values(quota_charged=False))
        if run.snapshot.get("batch_id"):
            refresh_regrade_batch(db, run.snapshot["batch_id"])
        db.commit()


def process_run(session_factory, run_id, lease_token, settings):
    from app.services.lab_grading import LabGradingNeedsReview

    guard = get_run_guard(run_id, lease_token)
    context = nullcontext(guard) if guard else LeaseGuard(session_factory, run_id, lease_token, settings)
    with context as active_guard:
        try:
            active_guard.assert_owned()
            _process_owned(session_factory, run_id, lease_token, settings, active_guard)
        except LeaseLost:
            raise
        except (RunCancelled, RuntimeFailure, LabDataError, LabGradingNeedsReview) as exc:
            _failure(session_factory, run_id, lease_token, settings, exc)
