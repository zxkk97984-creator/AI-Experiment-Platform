"""Immutable, public-only teaching workspaces for interactive kernels.

Files are copied through StorageService (including S3), never by interpreting
an object-store key as a host path. Finished cache directories are atomically
published and retained while kernels may still mount them.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
from tempfile import mkdtemp

from sqlalchemy.orm import sessionmaker

from app.models import EnvironmentVersion, LabDatasetVersion, LabFile, StorageObject
from app.services.lab_data_service import materialize_dataset
from app.services.studio_asset_manifest_service import StudioAssetManifestService, validate_relative_asset_path
from app.storage import StorageArea, StorageService


def prepare_course_workspace(db, settings, *, version=None, template=None, dataset_id=None):
    if (version is None) == (template is None):
        raise ValueError("请选择一个固定模板版本或预览草稿")
    storage = StorageService.from_settings(settings)
    assets_service = StudioAssetManifestService(storage)
    if version is not None:
        manifest_id, assets = assets_service.version_assets(db, version.id, version.assets_dir)
        identity = ["version", version.id, version.sha256, manifest_id]
        legacy_prefix = version.assets_dir
    else:
        manifest_id, assets = assets_service.draft_assets(db, template)
        identity = ["draft", template.id, template.draft_revision, manifest_id]
        legacy_prefix = template.draft_assets_dir
    if manifest_id is None and legacy_prefix and not assets:
        raise RuntimeError("历史教材资源暂不可用，请检查存储配置")
    # Include legacy object's metadata so changing a legacy draft cannot reuse
    # a previous cache merely because its directory key stayed the same.
    asset_fingerprints = []
    for asset in assets:
        if asset.storage_object_id:
            obj = db.get(StorageObject, asset.storage_object_id)
            if obj is None or obj.status != "active" or obj.backend != storage.backend_name:
                raise RuntimeError("教材资源暂不可用")
        relative = validate_relative_asset_path(asset.relative_path)
        if dataset_id and relative.split("/")[0] in {"train", "validation", "test"}:
            raise RuntimeError("启用数据集时教材资源不能占用 train、validation 或 test 目录")
        metadata = storage.head(StorageArea.STUDIO, asset.object_key)
        asset_fingerprints.append([relative, asset.storage_object_id, asset.sha256, metadata.size,
                                   metadata.etag if not asset.sha256 else None])
    dataset = db.get(LabDatasetVersion, dataset_id) if dataset_id is not None else None
    if dataset_id and (dataset is None or dataset.status != "ready"):
        raise RuntimeError("实验数据集尚未通过校验")
    fingerprint = hashlib.sha256(json.dumps([identity, asset_fingerprints,
        [dataset.id, dataset.sha256] if dataset else None], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    root = Path(settings.lab_work_path) / "course-cache"
    root.mkdir(parents=True, exist_ok=True)
    destination = root / fingerprint
    if destination.is_symlink():
        raise RuntimeError("教材缓存目录异常")
    if destination.is_dir():
        return destination
    required_bytes = sum(item[3] for item in asset_fingerprints)
    if dataset:
        from sqlalchemy import select, func
        required_bytes += db.scalar(select(func.coalesce(func.sum(StorageObject.size_bytes), 0))
            .join(LabFile, LabFile.object_id == StorageObject.id).where(
                LabFile.dataset_version_id == dataset.id,
                LabFile.role.in_(["train_input", "train_labels", "validation_input", "validation_labels"])))
    if shutil.disk_usage(root).free < required_bytes + settings.lab_min_free_disk_mb * 1024 * 1024:
        raise RuntimeError("实验磁盘剩余空间不足，请联系管理员")
    staged = Path(mkdtemp(prefix=".pending-", dir=root))
    try:
        for asset in assets:
            relative = validate_relative_asset_path(asset.relative_path)
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, total = hashlib.sha256(), 0
            with closing(storage.open_read(StorageArea.STUDIO, asset.object_key)) as inp, target.open("xb") as out:
                while chunk := inp.read(1024 * 1024):
                    total += len(chunk)
                    if total > (asset.size_bytes if asset.size_bytes is not None else settings.lab_file_max_bytes):
                        raise RuntimeError("教材资源大小完整性校验失败")
                    digest.update(chunk)
                    out.write(chunk)
            if asset.sha256 and digest.hexdigest() != asset.sha256:
                raise RuntimeError("教材资源哈希完整性校验失败")
            if asset.size_bytes is not None and total != asset.size_bytes:
                raise RuntimeError("教材资源大小完整性校验失败")
        if dataset:
            for split in ("train", "validation"):
                materialize_dataset(db, dataset.id, split, staged / split, settings, include_labels=True)
        # Sandbox UID 1000 needs read/execute permission through mkdtemp's
        # otherwise private root. Docker additionally mounts this tree :ro.
        for directory, _, files in os.walk(staged):
            os.chmod(directory, 0o755)
            for name in files:
                os.chmod(Path(directory) / name, 0o444)
        try:
            staged.rename(destination)
        except OSError:
            if not destination.is_dir() or destination.is_symlink():
                raise
        return destination
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def ensure_record_kernel(manager, record, version, db, *, image_ref=None, environment_version_id=None, restart=False):
    """Bind live sessions to the caller's database, never the global DB."""
    from app.services.kernel_manager import KernelManager
    from app.services.lab_assessment_service import record_assessment

    kwargs = ({"image_ref": image_ref, "environment_version_id": environment_version_id}
              if image_ref is not None or environment_version_id is not None else {})
    if isinstance(manager, KernelManager) and environment_version_id is not None:
        assessment = record_assessment(db, record.id)
        config = assessment.config if assessment else {}
        kwargs.update(_kernel_context(manager, db, environment_version_id, config))
        if version is not None:
            kwargs["lesson_storage_dir"] = str(prepare_course_workspace(db, manager.settings,
                version=version, dataset_id=config.get("dataset_id")))
    if restart:
        return manager.restart(record.id, **kwargs) if isinstance(manager, KernelManager) else manager.restart(record.id)
    return manager.get_or_create_session(record.id, **kwargs)


def ensure_preview_kernel(manager, template, session_id, db, *, image_ref=None, environment_version_id=None):
    from app.services.kernel_manager import KernelManager

    kwargs = ({"image_ref": image_ref, "environment_version_id": environment_version_id}
              if image_ref is not None or environment_version_id is not None else {})
    if isinstance(manager, KernelManager) and db is not None:
        kwargs.update(_kernel_context(manager, db, environment_version_id, {}))
        kwargs["lesson_storage_dir"] = str(prepare_course_workspace(db, manager.settings, template=template))
    return manager.get_or_create_session(session_id, **kwargs)


def _kernel_context(manager, db, environment_version_id, config):
    environment = db.get(EnvironmentVersion, environment_version_id) if environment_version_id else None
    resources = config.get("resources") or {}
    return {"memory_mb": max(int(environment.minimum_memory_mb) if environment else manager.settings.judge_memory_limit_mb,
                              int(resources.get("memory_mb", 128))),
            "cpus": float(resources.get("cpus", manager.settings.judge_cpu_limit)),
            "resource_factory": sessionmaker(bind=db.get_bind(), expire_on_commit=False)}
