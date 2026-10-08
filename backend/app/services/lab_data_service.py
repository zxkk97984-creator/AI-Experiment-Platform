"""Dataset and artifact ingestion; storage bytes are never executed here.

Transactions belong to callers. Uploads are streamed; validation uses private
temporary files and an on-disk sample index so a dataset is not held in RAM.
``samples.csv`` is the uniform inference contract. Labels are separate objects
and are materialized only by an explicit trusted caller request.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import csv
import hashlib
import hmac
import json
import math
import logging
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import BinaryIO
import uuid
import zipfile

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.models import StorageObject
from app.models.lab_data import LabArtifact, LabDatasetVersion, LabFile
from app.services.lab_archive import LabDataError, extract_zip, regular_files, safe_relative_path, sha256_file
from app.services.storage_object_service import StorageObjectService
from app.storage import StorageArea, StorageLimitExceeded, StorageService

TASK_TYPES = {"tabular_regression", "tabular_classification", "image_classification"}
SPLITS = ("train", "validation", "test")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_DATASET_SAMPLES = 200_000
MAX_ARTIFACTS_PER_RECORD = 20
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class _DigestReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        chunk = self.source.read(size)
        self.digest.update(chunk)
        return chunk


def _put_object(db, settings, area, stream, filename, owner_id, max_bytes):
    storage = StorageService.from_settings(settings)
    key = f"{area.value}/{uuid.uuid4().hex}"
    service = StorageObjectService(db, storage)
    obj = service.create_staging(
        namespace=f"lab-{area.value}", object_key=key, backend=storage.backend_name,
        original_filename=Path(filename).name[:255], created_by_id=owner_id,
        metadata_json={"storage_area": area.value},
    )
    reader = _DigestReader(stream)
    try:
        metadata = storage.put(area, key, reader, max_bytes=max_bytes)
    except Exception:
        service.mark_failed(obj.id)
        raise
    service.activate(obj.id, size_bytes=metadata.size, sha256=reader.digest.hexdigest(), etag=metadata.etag)
    db.flush()
    return obj


def _copy_object(db, object_id, destination: Path, settings, area):
    obj = db.get(StorageObject, object_id)
    if obj is None or obj.status != "active":
        raise LabDataError("资源文件当前不可用")
    if obj.metadata_json.get("storage_area") != area.value:
        raise LabDataError("资源文件区域不匹配")
    storage = StorageService.from_settings(settings)
    if obj.backend != storage.backend_name:
        raise LabDataError("资源文件存储后端不可用")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting a symlink or mutable old output.
    digest = hashlib.sha256()
    total = 0
    with closing(storage.open_read(area, obj.object_key)) as inp, destination.open("xb") as out:
        while chunk := inp.read(1024 * 1024):
            total += len(chunk)
            if total > (obj.size_bytes or 0):
                raise LabDataError("资源大小完整性校验失败")
            digest.update(chunk)
            out.write(chunk)
    if total != obj.size_bytes or digest.hexdigest() != obj.sha256:
        raise LabDataError("资源哈希完整性校验失败")


def register_dataset_upload(db: Session, owner_id: int, name: str, task_type: str,
                            config: dict, filename: str, stream: BinaryIO, settings) -> LabDatasetVersion:
    if task_type not in TASK_TYPES:
        raise LabDataError("不支持的数据集任务类型")
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise LabDataError("数据集名称应为 1 至 160 个字符")
    if not isinstance(config, dict):
        raise LabDataError("数据集配置必须是对象")
    suffix = Path(filename).suffix.lower()
    if suffix not in ({".zip"} if task_type == "image_classification" else {".csv", ".zip"}):
        raise LabDataError("表格数据仅支持 CSV/ZIP，图像数据仅支持 ZIP")
    config = json.loads(_canonical(config))
    # A public fixed split seed must not make image IDs guessable from known
    # class names or the sorted class-directory order.
    config["_sample_id_salt"] = uuid.uuid4().hex
    obj = _put_object(db, settings, StorageArea.DATASETS, stream, filename, owner_id, settings.lab_upload_max_bytes)
    dataset = LabDatasetVersion(owner_id=owner_id, name=name.strip(), task_type=task_type,
                                config=config, summary={}, raw_object_id=obj.id, sha256=obj.sha256, status="queued")
    db.add(dataset)
    db.flush()
    return dataset


def _csv_reader(path):
    stream = path.open("r", encoding="utf-8-sig", newline="")
    try:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        if not fields or any(not f or len(f) > 160 for f in fields) or len(set(fields)) != len(fields):
            raise LabDataError("CSV 列名必须非空且唯一")
        if len(fields) > 2048:
            raise LabDataError("CSV 列数超出限制")
        yield fields
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise LabDataError("CSV 行列数量不一致")
            yield row
    finally:
        stream.close()


def _add_sample(index, sample_id, label, features, split, digest, file_path=None):
    if index.total_changes >= MAX_DATASET_SAMPLES:
        raise LabDataError("数据集样本数量不能超过 200000")
    if not isinstance(sample_id, str) or not sample_id.strip() or len(sample_id) > 200:
        raise LabDataError("样本 ID 必须非空且不超过 200 个字符")
    if not isinstance(label, str) or not label.strip() or len(label) > 200:
        raise LabDataError("标签必须非空且不超过 200 个字符")
    if split is not None and split not in SPLITS:
        raise LabDataError("分区仅支持 train、validation、test")
    try:
        index.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?)",
                      (sample_id, label, _canonical(features), split, digest, file_path, ""))
    except sqlite3.IntegrityError as exc:
        raise LabDataError("数据集存在重复样本 ID 或重复内容，请去重后上传") from exc


def _load_tabular(index, paths, config, regression):
    target = config.get("target_column", "label")
    id_column = config.get("id_column", "sample_id")
    split_column = config.get("split_column")
    if not isinstance(target, str) or not isinstance(id_column, str) or (split_column is not None and not isinstance(split_column, str)):
        raise LabDataError("目标、ID 和分区列必须为列名")
    if target == id_column or target == split_column:
        raise LabDataError("目标列不能同时作为 ID 或分区列")
    expected_features = None
    count = 0
    for path in paths:
        rows = _csv_reader(path)
        fields = next(rows)
        if target not in fields or (split_column and split_column not in fields):
            raise LabDataError("CSV 缺少目标列或配置的分区列")
        if "id_column" in config and id_column not in fields:
            raise LabDataError("CSV 缺少配置的样本 ID 列")
        feature_columns = [f for f in fields if f not in {target, id_column, split_column}]
        if not feature_columns or "sample_id" in feature_columns:
            raise LabDataError("CSV 至少需要一个特征列，sample_id 为保留列名")
        if expected_features is not None and feature_columns != expected_features:
            raise LabDataError("各分区的特征列必须相同且顺序一致")
        expected_features = feature_columns
        explicit_split = path.stem if len(paths) > 1 else None
        for row in rows:
            count += 1
            sample_id = row.get(id_column, f"sample-{count}")
            label = row[target]
            if regression:
                try:
                    value = float(label)
                except (ValueError, TypeError) as exc:
                    raise LabDataError("回归标签必须为有限数值") from exc
                if not math.isfinite(value):
                    raise LabDataError("回归标签必须为有限数值")
            features = {f: row[f] for f in feature_columns}
            # Targets are excluded: identical inputs across splits are leakage
            # even when the supplied labels disagree.
            digest = hashlib.sha256(_canonical(features).encode()).hexdigest()
            split = row.get(split_column) if split_column else explicit_split
            if explicit_split and split != explicit_split:
                raise LabDataError("文件名分区与 CSV 分区列冲突")
            _add_sample(index, sample_id, label, features, split, digest)
    return expected_features or []


def _image_magic(path):
    from app.services.lab_image_validation import inspect_image
    inspect_image(path)


def _load_images(index, root, files, config):
    images = {p.relative_to(root).as_posix(): p for p in files if p.suffix.lower() in IMAGE_EXTENSIONS}
    other = [p for p in files if p.suffix.lower() not in IMAGE_EXTENSIONS and p.relative_to(root).as_posix() != "labels.csv"]
    if other or not images:
        raise LabDataError("图像压缩包只允许图片和根目录 labels.csv")
    labels_path = root / "labels.csv"
    seen = set()
    if labels_path.is_file():
        reader = _csv_reader(labels_path)
        fields = next(reader)
        path_column = "image_path" if "image_path" in fields else "path"
        target = config.get("target_column", "label")
        if path_column not in fields or target not in fields:
            raise LabDataError("labels.csv 必须包含 image_path（或 path）和标签列")
        rows = reader
    else:
        path_column, target = "path", "label"
        rows = []
        for relative in sorted(images):
            parts = relative.split("/")
            split = parts[0] if parts[0] in SPLITS else None
            expected_depth = 3 if split else 2
            if len(parts) != expected_depth:
                raise LabDataError("无 labels.csv 时使用 类别/图片 或 分区/类别/图片 目录")
            rows.append({"path": relative, "label": parts[-2], "split": split})
    for number, row in enumerate(rows, 1):
        relative = safe_relative_path(row[path_column])
        path = images.get(relative)
        if path is None or relative in seen:
            raise LabDataError("图像清单引用不存在或重复的文件")
        seen.add(relative)
        _image_magic(path)
        _add_sample(index, row.get("sample_id", f"sample-{number}"), row[target], {},
                    row.get("split") or None, sha256_file(path), str(path))
    if len(seen) != len(images):
        raise LabDataError("所有图片必须在 labels.csv 中恰好出现一次")
    return ["image_path"]


def _assign_splits(index, config):
    count = index.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    if count < 3:
        raise LabDataError("数据集至少需要 3 个不同样本")
    assigned = index.execute("SELECT COUNT(*) FROM samples WHERE split IS NOT NULL").fetchone()[0]
    if assigned not in {0, count}:
        raise LabDataError("不能混合自动划分与手动分区")
    seed = config.get("seed", 42)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise LabDataError("划分 seed 必须为整数")
    for (sample_id,) in index.execute("SELECT id FROM samples"):
        rank = hashlib.sha256(f"{seed}:{sample_id}".encode()).hexdigest()
        index.execute("UPDATE samples SET rank = ? WHERE id = ?", (rank, sample_id))
    if not assigned:
        train_end = max(1, int(count * .7))
        validation_end = min(count - 1, train_end + max(1, int(count * .15)))
        train_end = min(train_end, validation_end - 1)
        for i, (sample_id,) in enumerate(index.execute("SELECT id FROM samples ORDER BY rank, id")):
            split = "train" if i < train_end else "validation" if i < validation_end else "test"
            index.execute("UPDATE samples SET split = ? WHERE id = ?", (split, sample_id))
    counts = {split: index.execute("SELECT COUNT(*) FROM samples WHERE split = ?", (split,)).fetchone()[0] for split in SPLITS}
    if any(n == 0 for n in counts.values()):
        raise LabDataError("train、validation、test 分区均需至少一个样本")
    return counts


def _store_file(db, settings, path, logical_path, role, *, dataset=None, artifact=None):
    parent = dataset if dataset is not None else artifact
    area = StorageArea.DATASETS if dataset is not None else StorageArea.ARTIFACTS
    with path.open("rb") as stream:
        obj = _put_object(db, settings, area, stream, logical_path, parent.owner_id, settings.lab_file_max_bytes)
    file = LabFile(dataset_version_id=dataset.id if dataset else None, artifact_id=artifact.id if artifact else None,
                   object_id=obj.id, path=logical_path, role=role)
    db.add(file)
    return {"path": logical_path, "sha256": obj.sha256, "size_bytes": obj.size_bytes, "role": role}


def _write_dataset(db, settings, dataset, index, work, features):
    for split in SPLITS:
        manifest_path = work / f"{split}-samples.csv"
        labels_path = work / f"{split}-labels.csv"
        with manifest_path.open("w", encoding="utf-8", newline="") as manifest, labels_path.open("w", encoding="utf-8", newline="") as labels:
            writer = csv.DictWriter(manifest, fieldnames=["sample_id", *features])
            label_writer = csv.writer(labels)
            writer.writeheader()
            label_writer.writerow(["sample_id", "label"])
            for sample_id, label, payload, file_path, rank in index.execute(
                "SELECT id,label,payload,file_path,rank FROM samples WHERE split = ? ORDER BY rank,id", (split,)
            ):
                # All exported IDs are opaque. Original paths/IDs may encode
                # labels, especially for images named after their category.
                public_id = "s-" + hmac.new(dataset.config["_sample_id_salt"].encode(), sample_id.encode(), "sha256").hexdigest()
                values = json.loads(payload)
                if file_path:
                    image_path = Path(file_path)
                    relative = f"images/{public_id}{image_path.suffix.lower()}"
                    values = {"image_path": relative}
                    _store_file(db, settings, image_path, f"{split}/{relative}", f"{split}_input", dataset=dataset)
                writer.writerow({"sample_id": public_id, **values})
                label_writer.writerow([public_id, label])
        _store_file(db, settings, manifest_path, f"{split}/samples.csv", f"{split}_input", dataset=dataset)
        _store_file(db, settings, labels_path, f"{split}/labels.csv", f"{split}_labels", dataset=dataset)
        # Public export is generated once per immutable dataset, not per
        # student's overview/download. Never archive the private test partition.
        if split in ("train", "validation"):
            bundle = work / f"{split}.zip"
            with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                archive.write(manifest_path, "samples.csv")
                archive.write(labels_path, "labels.csv")
                for sample_id, file_path in index.execute("SELECT id,file_path FROM samples WHERE split=? AND file_path IS NOT NULL", (split,)):
                    public_id = "s-" + hmac.new(dataset.config["_sample_id_salt"].encode(), sample_id.encode(), "sha256").hexdigest()
                    archive.write(file_path, f"images/{public_id}{Path(file_path).suffix.lower()}")
            _store_file(db, settings, bundle, f"downloads/{split}.zip", "public_bundle", dataset=dataset)


def validate_dataset(db: Session, dataset_id: int, settings) -> LabDatasetVersion:
    dataset = db.scalar(select(LabDatasetVersion).where(LabDatasetVersion.id == dataset_id).with_for_update())
    if dataset is None:
        raise LabDataError("数据集不存在")
    if dataset.status == "ready":
        return dataset
    if dataset.status not in {"queued", "validating"}:
        raise LabDataError("请重新上传修正后的数据集")
    try:
        with db.begin_nested(), TemporaryDirectory(prefix="dai-dataset-") as temp:
            work = Path(temp)
            raw = work / "raw-upload"
            _copy_object(db, dataset.raw_object_id, raw, settings, StorageArea.DATASETS)
            obj = db.get(StorageObject, dataset.raw_object_id)
            source = work / "source"
            source.mkdir()
            if Path(obj.original_filename or "").suffix.lower() == ".zip":
                paths = extract_zip(raw, source, settings)
            else:
                if raw.stat().st_size > min(settings.lab_file_max_bytes, settings.lab_expanded_max_bytes):
                    raise LabDataError("CSV 文件大小超出限制")
                target = source / "data.csv"
                raw.rename(target)
                paths = [target]
            with closing(sqlite3.connect(work / "samples.sqlite")) as index:
                index.execute("CREATE TABLE samples (id TEXT PRIMARY KEY, label TEXT, payload TEXT, split TEXT, digest TEXT UNIQUE, file_path TEXT, rank TEXT)")
                if dataset.task_type == "image_classification":
                    features = _load_images(index, source, paths, dataset.config)
                else:
                    if len(paths) > 1 and {p.relative_to(source).as_posix() for p in paths} != {f"{s}.csv" for s in SPLITS}:
                        raise LabDataError("表格 ZIP 应包含一个 CSV 或 train.csv、validation.csv、test.csv")
                    if not paths or any(p.suffix.lower() != ".csv" for p in paths):
                        raise LabDataError("表格 ZIP 只能包含 CSV 文件")
                    features = _load_tabular(index, sorted(paths), dataset.config, dataset.task_type == "tabular_regression")
                counts = _assign_splits(index, dataset.config)
                classes = [] if dataset.task_type == "tabular_regression" else [row[0] for row in index.execute("SELECT DISTINCT label FROM samples ORDER BY label LIMIT 10001")]
                if dataset.task_type != "tabular_regression" and (len(classes) < 2 or len(classes) > 10000):
                    raise LabDataError("分类数据需要 2 至 10000 个类别")
                configured_classes = dataset.config.get("class_labels")
                if configured_classes is not None:
                    if not isinstance(configured_classes, list) or any(not isinstance(label, (str, int)) for label in configured_classes):
                        raise LabDataError("class_labels 必须是类别值列表")
                    if set(map(str, configured_classes)) != set(classes):
                        raise LabDataError("配置的类别与数据标签不一致")
                _write_dataset(db, settings, dataset, index, work, features)
                dataset.summary = {"splits": counts, "sample_count": sum(counts.values()), "feature_columns": features,
                                   "class_labels": classes, "seed": dataset.config.get("seed", 42),
                                   "input_manifest": "samples.csv", "labels_manifest": "labels.csv"}
                dataset.status, dataset.error = "ready", None
                db.flush()
    except (LabDataError, UnicodeError, csv.Error, StorageLimitExceeded) as exc:
        dataset.status = "failed"
        dataset.error = str(exc)[:500] if isinstance(exc, LabDataError) else "数据格式、编码或大小校验失败"
        db.flush()
    return dataset


def _safe_destination(destination):
    path = Path(destination)
    if path.is_symlink():
        raise LabDataError("目标目录不能为符号链接")
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise LabDataError("资源只允许写入空目录")
    return path


def materialize_dataset(db: Session, dataset_id: int, split: str, destination, settings,
                        include_labels: bool = False) -> Path:
    """Trusted worker operation, NOT a user-authorized download API.

Use include_labels=True only for public training/validation or the trusted
scorer after inference ends. Student inference must never receive test labels.
"""
    if split not in SPLITS:
        raise LabDataError("无效数据分区")
    dataset = db.get(LabDatasetVersion, dataset_id)
    if dataset is None or dataset.status != "ready":
        raise LabDataError("数据集尚未通过校验")
    destination = _safe_destination(destination)
    roles = [f"{split}_input"] + ([f"{split}_labels"] if include_labels else [])
    files = db.scalars(select(LabFile).where(LabFile.dataset_version_id == dataset_id, LabFile.role.in_(roles))).all()
    for file in files:
        prefix = f"{split}/"
        if not file.path.startswith(prefix):
            raise LabDataError("数据清单分区不一致")
        relative = safe_relative_path(file.path[len(prefix):])
        _copy_object(db, file.object_id, destination / relative, settings, StorageArea.DATASETS)
    manifest = destination / "samples.csv"
    if not manifest.is_file():
        raise LabDataError("缺少样本清单")
    return manifest


def dataset_projection(dataset: LabDatasetVersion, *, teacher: bool = False) -> dict:
    result = {"id": dataset.id, "name": dataset.name, "task_type": dataset.task_type,
              "status": dataset.status, "summary": dataset.summary, "created_at": dataset.created_at}
    if teacher:
        result.update(config={key: value for key, value in dataset.config.items() if not key.startswith("_")},
                      error=dataset.error, owner_id=dataset.owner_id, sha256=dataset.sha256)
    return result


def register_artifact_upload(db: Session, owner_id: int, filename: str, stream: BinaryIO,
                             code_snapshot: dict, settings, *, record_id=None, assessment_id=None,
                             source="external", require_predict=True) -> LabArtifact:
    if Path(filename).suffix.lower() != ".zip":
        raise LabDataError("成果上传需要 ZIP 文件")
    if source not in {"external", "reference"} or not isinstance(code_snapshot, dict):
        raise LabDataError("成果来源或代码快照无效")
    remaining = _artifact_budget(db, record_id, settings, creating=True)
    try:
        obj = _put_object(db, settings, StorageArea.ARTIFACTS, stream, filename, owner_id,
                          min(settings.lab_upload_max_bytes, remaining))
    except StorageLimitExceeded as exc:
        raise LabDataError("成果上传超过单次上传限制或本实验剩余存储额度") from exc
    artifact = LabArtifact(owner_id=owner_id, record_id=record_id, assessment_id=assessment_id, source=source,
                           raw_object_id=obj.id, code_snapshot=json.loads(_canonical(code_snapshot)), status="queued")
    db.add(artifact)
    db.flush()
    return artifact


@dataclass(frozen=True)
class PreparedArtifactUpload:
    object_key: str
    backend: str
    filename: str
    size_bytes: int
    sha256: str
    etag: str | None


def discard_prepared_artifact(prepared, settings):
    """Physical cleanup runs after metadata rollback, never under policy locks."""
    try:
        StorageService.from_settings(settings).delete(StorageArea.ARTIFACTS, prepared.object_key)
    except Exception:
        # Reconciliation also discovers these unbound physical objects.
        logging.getLogger(__name__).warning('Artifact staging cleanup deferred: %s', prepared.object_key)


def prepare_artifact_upload(filename, stream, settings, *, max_bytes):
    """Copy and hash untrusted bytes without any application DB transaction."""
    if Path(filename).suffix.lower() != '.zip':
        raise LabDataError('成果上传需要 ZIP 文件')
    storage = StorageService.from_settings(settings)
    key = f'artifacts/{uuid.uuid4().hex}'
    reader = _DigestReader(stream)
    try:
        physical = storage.put(StorageArea.ARTIFACTS, key, reader, max_bytes=max_bytes)
    except Exception:
        discard_prepared_artifact(PreparedArtifactUpload(key, storage.backend_name, '', 0, '', None), settings)
        raise
    return PreparedArtifactUpload(key, storage.backend_name, Path(filename).name[:255],
                                  physical.size, reader.digest.hexdigest(), physical.etag)


def bind_prepared_artifact(db, owner_id, prepared, settings, *, record_id, assessment_id):
    """Bind previously copied bytes only after current scope/quota admission."""
    remaining = _artifact_budget(db, record_id, settings, creating=True)
    if prepared.size_bytes > min(settings.lab_upload_max_bytes, remaining):
        raise LabDataError('成果上传超过单次上传限制或本实验剩余存储额度')
    service = StorageObjectService(db, StorageService.from_settings(settings))
    obj = service.create_staging(namespace='lab-artifacts', object_key=prepared.object_key,
        backend=prepared.backend, original_filename=prepared.filename, created_by_id=owner_id,
        size_bytes=prepared.size_bytes, sha256=prepared.sha256, etag=prepared.etag,
        metadata_json={'storage_area': StorageArea.ARTIFACTS.value})
    service.activate(obj.id, size_bytes=prepared.size_bytes, sha256=prepared.sha256, etag=prepared.etag)
    artifact = LabArtifact(owner_id=owner_id, record_id=record_id, assessment_id=assessment_id,
                           source='external', raw_object_id=obj.id, code_snapshot={}, status='queued')
    db.add(artifact); db.flush()
    return artifact


def _lock_artifact_record(db, record_id):
    """Same record-before-artifact lock order as formal submission admission."""
    if record_id is None:
        return
    from app.models import ExperimentRecord, LabRecord
    locked = db.execute(update(LabRecord).where(LabRecord.record_id == record_id)
                        .values(generation=LabRecord.generation)).rowcount
    if not locked:
        # Existing manual-only records have no LabRecord yet.
        locked = db.execute(update(ExperimentRecord).where(ExperimentRecord.id == record_id)
                            .values(record_revision=ExperimentRecord.record_revision)).rowcount
        if not locked:
            raise LabDataError("成果关联的实验记录不存在")


def _artifact_budget(db, record_id, settings, *, creating=False, check_pending=True):
    """Serialize admission with a no-op row UPDATE; do not change grade generation."""
    quota = getattr(settings, "lab_artifact_quota_bytes", 2 * 1024 * 1024 * 1024)
    if record_id is None:
        return quota
    _lock_artifact_record(db, record_id)
    condition = (LabArtifact.record_id == record_id) & (LabArtifact.status != "deleted")
    artifacts = db.scalars(select(LabArtifact).where(condition).with_for_update()
                           .execution_options(populate_existing=True)).all()
    if creating:
        if len(artifacts) >= MAX_ARTIFACTS_PER_RECORD:
            raise LabDataError("每个实验最多保留 20 份成果，请先删除未被提交引用的成果")
        if check_pending and any(row.status in ('queued', 'validating') for row in artifacts):
            raise LabDataError("已有成果等待校验，请等待完成后再上传或训练")
    # Current locking reads avoid a stale RR count/size after waiting for
    # another upload. Stream file metadata instead of loading large manifests.
    raw_ids = [row.raw_object_id for row in artifacts if row.raw_object_id]
    artifact_ids = [row.id for row in artifacts]
    objects = select(StorageObject.id, StorageObject.size_bytes).outerjoin(LabFile, LabFile.object_id == StorageObject.id).where(
        or_(StorageObject.id.in_(raw_ids), LabFile.artifact_id.in_(artifact_ids))).distinct().with_for_update()
    used = sum(int(size or 0) for _, size in db.execute(objects.execution_options(yield_per=1000)))
    remaining = quota - used
    if remaining <= 0:
        raise LabDataError("本实验成果存储额度已用尽，请先删除未被提交引用的成果")
    return remaining


def _validate_source_files(files):
    sources = [path for path in files if path.suffix.lower() in {".py", ".ipynb"}]
    if sum(path.stat().st_size for path in sources) > MAX_SOURCE_BYTES:
        raise LabDataError("成果中的 Python 与 Notebook 源文件合计不能超过 2 MiB")
    for path in sources:
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise LabDataError("Python 和 Notebook 源文件必须使用 UTF-8 编码") from exc
        if "\x00" in raw:
            raise LabDataError("源文件不能包含空字节")
        if path.suffix.lower() != ".ipynb":
            continue
        try:
            notebook = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise LabDataError("Notebook 必须是有效的 JSON 文档") from exc
        if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list) or len(notebook["cells"]) > 1000:
            raise LabDataError("Notebook 必须包含 cells 列表，且最多 1000 个单元格")
        for cell in notebook["cells"]:
            if not isinstance(cell, dict) or cell.get("cell_type") not in {"code", "markdown", "raw"}:
                raise LabDataError("Notebook 单元格类型无效")
            source = cell.get("source")
            if not isinstance(source, str) and not (isinstance(source, list) and all(isinstance(line, str) for line in source)):
                raise LabDataError("Notebook 单元格 source 必须是文本或文本列表")


def _archive_directory(db, artifact, directory, settings, require_predict):
    files = regular_files(directory, settings)
    if sum(path.stat().st_size for path in files) > _artifact_budget(db, artifact.record_id, settings):
        raise LabDataError("解包后成果超过本实验剩余存储额度（原始包与归档文件均计入）")
    _validate_source_files(files)
    paths = {p.relative_to(directory).as_posix() for p in files}
    if not files or (require_predict and "predict.py" not in paths):
        raise LabDataError("模型成果必须在根目录提供 predict.py")
    if not artifact.code_snapshot and not any(p.endswith(".ipynb") or (p.endswith(".py") and p != "predict.py") for p in paths):
        raise LabDataError("成果必须包含训练代码或 Notebook")
    manifest = []
    for path in files:
        relative = path.relative_to(directory).as_posix()
        role = "code" if path.suffix.lower() in {".py", ".ipynb"} else "output"
        manifest.append(_store_file(db, settings, path, relative, role, artifact=artifact))
    artifact.manifest_hash = hashlib.sha256(_canonical(sorted(manifest, key=lambda item: item["path"])).encode()).hexdigest()
    artifact.status, artifact.error = "ready", None
    db.flush()


def validate_artifact(db: Session, artifact_id: int, settings, *, require_predict=True) -> LabArtifact:
    initial = db.get(LabArtifact, artifact_id)
    if initial is not None:
        _lock_artifact_record(db, initial.record_id)
    artifact = db.scalar(select(LabArtifact).where(LabArtifact.id == artifact_id).with_for_update())
    if artifact is None:
        raise LabDataError("成果不存在")
    if artifact.status == "ready":
        return artifact
    if artifact.status not in {"queued", "validating"} or artifact.raw_object_id is None:
        raise LabDataError("请重新上传修正后的成果")
    try:
        with db.begin_nested(), TemporaryDirectory(prefix="dai-artifact-") as temp:
            work = Path(temp)
            raw = work / "raw.zip"
            _copy_object(db, artifact.raw_object_id, raw, settings, StorageArea.ARTIFACTS)
            extract_zip(raw, work / "files", settings)
            _archive_directory(db, artifact, work / "files", settings, require_predict)
    except (LabDataError, StorageLimitExceeded) as exc:
        artifact.status, artifact.error = "failed", str(exc)[:500]
        db.flush()
    return artifact


def archive_artifact(db: Session, owner_id: int, directory, code_snapshot: dict, settings, *,
                     record_id=None, assessment_id=None, run_id=None, source="platform", require_predict=True) -> LabArtifact:
    if source not in {"platform", "reference"}:
        raise LabDataError("本地归档成果来源无效")
    # Platform outputs become ready within this transaction; a separately
    # queued upload must not discard a completed training run's result.
    _artifact_budget(db, record_id, settings, creating=True, check_pending=False)
    artifact = LabArtifact(owner_id=owner_id, record_id=record_id, assessment_id=assessment_id,
                           run_id=run_id, source=source, code_snapshot=json.loads(_canonical(code_snapshot)), status="queued")
    db.add(artifact)
    db.flush()
    _archive_directory(db, artifact, Path(directory), settings, require_predict)
    return artifact


def materialize_artifact(db: Session, artifact_id: int, destination, settings) -> Path:
    artifact = db.get(LabArtifact, artifact_id)
    if artifact is None or artifact.status != "ready":
        raise LabDataError("成果尚未通过校验")
    destination = _safe_destination(destination)
    manifest = []
    for file in db.scalars(select(LabFile).where(LabFile.artifact_id == artifact_id).order_by(LabFile.path)):
        relative = safe_relative_path(file.path)
        _copy_object(db, file.object_id, destination / relative, settings, StorageArea.ARTIFACTS)
        obj = db.get(StorageObject, file.object_id)
        manifest.append({"path": relative, "sha256": obj.sha256, "size_bytes": obj.size_bytes, "role": file.role})
    if hashlib.sha256(_canonical(sorted(manifest, key=lambda item: item["path"])).encode()).hexdigest() != artifact.manifest_hash:
        raise LabDataError("成果清单完整性校验失败")
    return destination


def artifact_files(db, artifact_id, *, page=1, page_size=50):
    query = select(LabFile, StorageObject).join(StorageObject, LabFile.object_id == StorageObject.id).where(LabFile.artifact_id == artifact_id)
    total = db.scalar(select(func.count()).select_from(LabFile).where(LabFile.artifact_id == artifact_id))
    rows = db.execute(query.order_by(LabFile.path).offset((page-1)*page_size).limit(page_size))
    items = [{"id":file.id,"path":file.path,"size_bytes":obj.size_bytes,"sha256":obj.sha256,"role":file.role} for file,obj in rows]
    return {"items":items,"total":total,"page":page,"page_size":page_size}


def artifact_projection(db, artifact: LabArtifact) -> dict:
    files = artifact_files(db, artifact.id)
    return {"id": artifact.id, "record_id": artifact.record_id, "assessment_id": artifact.assessment_id,
            "run_id": artifact.run_id, "source": artifact.source, "status": artifact.status,
            "manifest_hash": artifact.manifest_hash, "error": artifact.error, "files": files["items"],
            "file_count": files["total"], "files_truncated": files["total"] > len(files["items"]),
            "created_at": artifact.created_at, "log_trust": "platform" if artifact.source == "platform" else "student_provided"}


def delete_unsubmitted_artifact(db: Session, artifact_id: int, user, settings) -> LabArtifact:
    """Unbind an unused artifact; retention-aware storage GC owns byte deletion.

This transaction never removes physical data or retires shared objects. Once
committed, the existing reconciler discovers newly unreferenced objects and
the collector rechecks all business references before deleting their bytes.
"""
    from app.errors import api_error
    from app.models import AuditEvent, LabAssessment, LabRun, LabSubmission

    artifact = db.get(LabArtifact, artifact_id)
    if artifact is None:
        raise api_error(404, "LAB_ARTIFACT_NOT_FOUND", "成果不存在")
    if user.role != "admin" and artifact.owner_id != user.id:
        raise api_error(403, "LAB_ARTIFACT_FORBIDDEN", "只能删除本人的未提交成果")
    _lock_artifact_record(db, artifact.record_id)
    # Publishers lock the assessment first. Match their order for references,
    # including generated baseline artifacts that exist only in run.result.
    reference_runs = select(LabRun.id).where(or_(
        LabRun.artifact_id == artifact_id, LabRun.result["artifact_id"].as_integer() == artifact_id))
    assessments = db.scalars(select(LabAssessment).where(or_(
        LabAssessment.config["reference_artifact_id"].as_integer() == artifact_id,
        LabAssessment.validation_run_id.in_(reference_runs)))
        .order_by(LabAssessment.id).with_for_update().execution_options(populate_existing=True)).all()
    # An UPDATE also serializes the subsequent latest-state read on SQLite.
    db.execute(update(LabArtifact).where(LabArtifact.id == artifact_id).values(status=LabArtifact.status))
    db.refresh(artifact)
    if artifact.status == "deleted":
        return artifact
    referenced_submission = db.scalar(select(LabSubmission.submission_id).where(LabSubmission.artifact_id == artifact_id).limit(1))
    active_run = db.scalar(select(LabRun.id).where(LabRun.status.in_(["queued", "running", "recovering"]), or_(
        LabRun.artifact_id == artifact_id,
        LabRun.snapshot["assessment_config"]["reference_artifact_id"].as_integer() == artifact_id)).limit(1))
    if referenced_submission or active_run or any(row.status == "published" for row in assessments):
        raise api_error(409, "LAB_ARTIFACT_IN_USE", "成果已被提交、运行任务或已发布的参考试评引用，不能删除")
    object_ids = set(db.scalars(select(LabFile.object_id).where(LabFile.artifact_id == artifact_id)))
    if artifact.raw_object_id is not None:
        object_ids.add(artifact.raw_object_id)
    previous_status = artifact.status
    db.execute(delete(LabFile).where(LabFile.artifact_id == artifact_id))
    artifact.raw_object_id = None
    artifact.status = "deleted"
    db.add(AuditEvent(actor_id=user.id, action="lab.artifact.delete", target_type="lab_artifact",
        target_id=str(artifact.id), changes={"previous_status": previous_status,
            "manifest_hash": artifact.manifest_hash, "detached_object_ids": sorted(object_ids)}))
    db.flush()
    return artifact
