"""Bounded archive handling for untrusted experiment uploads.

No uploaded code or model is imported here. Extraction happens only in a fresh
private temporary directory; workers execute copies later in their sandbox.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile


class LabDataError(ValueError):
    """An upload fails the documented data or artifact contract."""


def safe_relative_path(value: str) -> str:
    if not value or len(value) > 400 or "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise LabDataError("文件路径无效")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise LabDataError("文件路径不能越过资源目录")
    return path.as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def extract_zip(source: Path, destination: Path, settings) -> list[Path]:
    """Reject duplicate paths, links, special files and actual/claimed bombs."""
    destination.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    seen: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > settings.lab_max_files:
                raise LabDataError("压缩包文件数量超出限制")
            if sum(info.file_size for info in infos) > settings.lab_expanded_max_bytes:
                raise LabDataError("压缩包展开大小超出限制")
            for info in infos:
                relative = safe_relative_path(info.filename.rstrip("/") if info.is_dir() else info.filename)
                if relative.casefold() in seen:
                    raise LabDataError("压缩包含重复文件路径")
                seen.add(relative.casefold())
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or info.flag_bits & 1:
                    raise LabDataError("不支持链接、特殊文件或加密压缩包")
                if info.is_dir():
                    if kind not in {0, stat.S_IFDIR}:
                        raise LabDataError("压缩包目录类型不一致")
                    continue
                if kind == stat.S_IFDIR or info.file_size > settings.lab_file_max_bytes:
                    raise LabDataError("压缩包单文件大小或类型无效")
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                actual = 0
                with archive.open(info) as inp, target.open("xb") as out:
                    while chunk := inp.read(1024 * 1024):
                        actual += len(chunk)
                        total += len(chunk)
                        if actual > settings.lab_file_max_bytes or total > settings.lab_expanded_max_bytes:
                            raise LabDataError("压缩包实际展开大小超出限制")
                        out.write(chunk)
                if actual != info.file_size:
                    raise LabDataError("压缩包文件大小校验失败")
                files.append(target)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, FileExistsError, NotADirectoryError, IsADirectoryError) as exc:
        raise LabDataError("无法读取有效的 ZIP 压缩包") from exc
    return files


def regular_files(directory: Path, settings) -> list[Path]:
    """Read a completed sandbox directory only after its container has stopped."""
    if directory.is_symlink() or not directory.is_dir():
        raise LabDataError("成果目录无效")
    files = []
    total = 0
    for root, dirs, names in os.walk(directory, followlinks=False):
        for name in dirs + names:
            path = Path(root) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise LabDataError("成果只能包含普通文件和目录")
            safe_relative_path(path.relative_to(directory).as_posix())
            if stat.S_ISREG(mode):
                size = path.stat().st_size
                total += size
                files.append(path)
                if size > settings.lab_file_max_bytes or total > settings.lab_expanded_max_bytes or len(files) > settings.lab_max_files:
                    raise LabDataError("成果大小或文件数量超出限制")
    return sorted(files)
