"""教师上传视频的存储与签名服务。

首期采用本地磁盘存储（backend/storage/videos/）：
- 上传文件先写入 .staging 目录，校验完成后由统一 Storage 层原子移动到最终位置；
- 磁盘文件名使用 uuid4().hex，原文件名只作展示元数据；
- 播放地址为 HMAC 签名短期 URL，媒体端点再次执行权限检查。
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import UploadFile

from app.config import Settings
from app.errors import api_error
from app.storage import StorageArea, StorageError, StorageLimitExceeded, StorageService

# 扩展名 -> 标准 MIME 白名单（大小写不敏感）
ALLOWED_VIDEO_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}

# WebM EBML 头
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"
# ISO-BMFF（MP4/MOV）容器头：前 4 字节大端长度 + "ftyp"
_FTYP_MAGIC = b"ftyp"

@dataclass(frozen=True)
class StoredVideo:
    storage_key: str
    filename: str
    content_type: str
    size: int


def _validate_filename(filename: str | None) -> str:
    """校验并返回安全化的原文件名（仅作展示元数据）。"""
    if not filename:
        raise api_error(400, "VIDEO_FILE_EMPTY", "缺少文件名")
    if len(filename) > 255:
        raise api_error(400, "VIDEO_FILENAME_INVALID", "文件名长度不能超过 255 字符")
    # 拒绝 NUL 与全部控制字符
    for ch in filename:
        if ch == "\x00" or (ord(ch) < 0x20 and ch not in "\t\n\r"):
            raise api_error(400, "VIDEO_FILENAME_INVALID", "文件名包含非法控制字符")
    if "/" in filename or "\\" in filename:
        raise api_error(400, "VIDEO_FILENAME_INVALID", "文件名不能包含路径分隔符")
    # 拒绝 "." / ".." 路径片段（含任意位置的 ".." 组合）
    if filename in (".", "..") or ".." in filename:
        raise api_error(400, "VIDEO_FILENAME_INVALID", "文件名不能包含 .. 路径片段")
    return filename


def _check_magic_number(filename: str, head: bytes) -> str:
    """校验文件头魔数，返回对应 MIME；不匹配返回空字符串。"""
    ext = Path(filename).suffix.lower()
    if ext in (".mp4", ".mov"):
        # ISO-BMFF：文件开头为 box size(4) + "ftyp" type(4)
        if head[4:8] == _FTYP_MAGIC:
            return ALLOWED_VIDEO_TYPES[ext]
        return ""
    if ext == ".webm":
        if head.startswith(_EBML_MAGIC):
            return ALLOWED_VIDEO_TYPES[ext]
        return ""
    return ""


def _validate_upload(upload: UploadFile, settings: Settings) -> tuple[str, str]:
    """校验文件名、MIME、扩展名与魔数，返回 (安全文件名, 标准 MIME)。"""
    filename = _validate_filename(upload.filename)

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_VIDEO_TYPES:
        raise api_error(415, "VIDEO_TYPE_UNSUPPORTED", "仅支持 .mp4 / .webm / .mov 视频文件")

    declared_mime = (upload.content_type or "").split(";")[0].strip().lower()
    if declared_mime not in ALLOWED_VIDEO_TYPES.values():
        raise api_error(415, "VIDEO_TYPE_UNSUPPORTED", "视频 MIME 类型不受支持")

    expected_mime = ALLOWED_VIDEO_TYPES[ext]
    if declared_mime != expected_mime:
        raise api_error(415, "VIDEO_TYPE_UNSUPPORTED", "视频扩展名与 MIME 类型不匹配")

    # 读取文件头用于空文件与魔数校验（最多 16 字节）
    head = upload.file.read(16)
    upload.file.seek(0)
    if not head:
        raise api_error(400, "VIDEO_FILE_EMPTY", "视频文件为空")
    if _check_magic_number(filename, head) != expected_mime:
        raise api_error(415, "VIDEO_CONTENT_INVALID", "视频文件内容格式校验失败")

    return filename, expected_mime


def remove_storage_key(settings: Settings, storage_key: str | None) -> None:
    """删除磁盘文件；文件不存在或删除失败只记录，不抛异常（生命周期尽力而为）。"""
    if not storage_key:
        return
    try:
        StorageService.from_settings(settings).delete(StorageArea.VIDEOS, storage_key)
    except StorageError:
        # 旧文件删除失败不能回滚已成功的新视频，只记录结构化错误
        import logging

        logging.getLogger("lesson_video").error(
            "remove storage key failed: %s", storage_key, exc_info=True
        )


async def store_upload(
    upload: UploadFile,
    lesson_id: int,
    settings: Settings,
) -> StoredVideo:
    """校验并原子保存上传文件，返回元数据。任何失败都会清理 staging 文件。"""
    filename, content_type = _validate_upload(upload, settings)

    ext = Path(filename).suffix.lower()
    storage_key = f"lessons/{lesson_id}/{uuid.uuid4().hex}{ext}"

    try:
        stored = StorageService.from_settings(settings).put(
            StorageArea.VIDEOS,
            storage_key,
            upload.file,
            max_bytes=settings.video_max_upload_bytes,
        )
        if stored.size == 0:
            raise api_error(400, "VIDEO_FILE_EMPTY", "视频文件为空")
        return StoredVideo(
            storage_key=storage_key,
            filename=filename,
            content_type=content_type,
            size=stored.size,
        )
    except StorageLimitExceeded as exc:
        raise api_error(413, "VIDEO_TOO_LARGE", "视频文件超过 500 MiB 大小限制") from exc


def create_playback_signature(
    lesson_id: int,
    user_id: int,
    storage_key: str,
    settings: Settings,
    now: datetime | None = None,
    session_version: int = 1,
) -> tuple[str, datetime]:
    """生成短期签名播放参数 (sig, expires)。签名内容包含当前 storage key，
    替换/移除视频后已签发的旧地址立即失效。"""
    now = now or datetime.now(timezone.utc)
    expires = int((now + timedelta(seconds=settings.video_playback_url_ttl_seconds)).timestamp())
    message = f"{lesson_id}:{user_id}:{expires}:{storage_key}:{session_version}"
    digest = hmac.new(
        settings.secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest, datetime.fromtimestamp(expires, tz=timezone.utc)


def verify_playback_signature(
    lesson_id: int,
    user_id: int,
    expires: int,
    storage_key: str,
    sig: str,
    settings: Settings,
    session_version: int = 1,
) -> None:
    """校验签名与过期时间；任何不匹配即抛 401，防止被当作普通 404 探测。"""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if expires <= now_ts:
        raise api_error(401, "VIDEO_SIGNATURE_EXPIRED", "播放链接已过期")
    message = f"{lesson_id}:{user_id}:{expires}:{storage_key}:{session_version}"
    expected = hmac.new(
        settings.secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise api_error(401, "VIDEO_SIGNATURE_INVALID", "播放链接签名无效")
