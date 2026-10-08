"""教师上传视频：上传 / 权限 / 校验 / 生命周期 / 签名播放 端到端测试

- 存储目录由 conftest 指向 tmp_path，绝不写入真实 backend/storage/videos/
- 媒体端点直接使用 TestClient（签名 URL 为同源相对路径）
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app import models
from conftest import auth_header, create_course_db, create_user, grant_teaching_fixture, login

API = "/api/v1"


# ── 合法视频文件头 ──────────────────────────────────────────────────
def mp4_bytes(size: int = 200) -> bytes:
    head = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41"
    return head + b"x" * max(0, size - len(head))


def webm_bytes(size: int = 200) -> bytes:
    head = b"\x1a\x45\xdf\xa3" + b"\x00" * 24
    return head + b"x" * max(0, size - len(head))


def mov_bytes(size: int = 200) -> bytes:
    head = b"\x00\x00\x00\x18ftypqt  \x00\x00\x02\x00qt  "
    return head + b"x" * max(0, size - len(head))


def _setup(client, db_session_factory, visibility="public", enroll="s_yes"):
    """教师 owner + 其他教师 + 已选课学生 + 未选课学生 + 管理员"""
    create_user(db_session_factory, "t_own", "teacher")
    create_user(db_session_factory, "t_other", "teacher")
    create_user(db_session_factory, "s_yes", "student")
    create_user(db_session_factory, "s_no", "student")
    create_user(db_session_factory, "admin", "admin")
    tok = {u: login(client, u)[0] for u in
           ["t_own", "t_other", "s_yes", "s_no", "admin"]}

    # 领域 fixture 直接创建 published 课程（发布门禁由 course_publish 测试覆盖）
    cid = create_course_db(
        db_session_factory,
        teacher_username="t_own",
        title="视频课程",
        status="published",
        visibility=visibility,
    )
    if enroll == "s_yes":
        with db_session_factory() as db:
            student = db.query(models.User).filter_by(username="s_yes").one()
            grant_teaching_fixture(db, db.get(models.Course, cid), student_ids=[student.id])
            db.commit()
        response = client.post(f"{API}/courses/{cid}/enroll", headers=auth_header(tok["s_yes"]))
        assert response.status_code == 201, response.text
    ch = client.post(f"{API}/courses/{cid}/chapters", headers=auth_header(tok["t_own"]), json={"title": "Ch"})
    chid = ch.json()["id"]
    le = client.post(f"{API}/chapters/{chid}/lessons", headers=auth_header(tok["t_own"]), json={
        "title": "视频课", "content_type": "video",
        "video_url": "https://v.example.com/old.mp4",
        "status": "published",
    })
    lid = le.json()["id"]
    # 一个非视频课时（用于 409 场景）
    md = client.post(f"{API}/chapters/{chid}/lessons", headers=auth_header(tok["t_own"]), json={
        "title": "讲义课", "content_type": "markdown", "content": "x",
    })
    return {"tok": tok, "cid": cid, "chid": chid, "lid": lid, "md_lid": md.json()["id"]}


def _upload(client, tok, lid, filename, content, mime):
    return client.put(
        f"{API}/lessons/{lid}/video-file",
        headers=auth_header(tok),
        files={"file": (filename, content, mime)},
    )


def _video_keys(test_settings):
    """视频根目录下所有最终文件 key（不含 .staging）"""
    root = test_settings.video_storage_path
    if not root.exists():
        return []
    return sorted(
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file() and ".staging" not in p.parts
    )


def _staging_files(test_settings):
    staging = test_settings.video_storage_path / ".staging"
    if not staging.exists():
        return []
    return list(staging.iterdir())


# ══════════════════════════════════════════════════════════════════
# 上传权限
# ══════════════════════════════════════════════════════════════════

def test_owner_teacher_uploads_mp4_webm_mov(client, db_session_factory, test_settings):
    d = _setup(client, db_session_factory)
    # 三个课时分别上传三种格式（同一课时连续上传会互相替换）
    for i, (name, content, mime) in enumerate([
        ("demo.mp4", mp4_bytes(), "video/mp4"),
        ("demo.webm", webm_bytes(), "video/webm"),
        ("demo.mov", mov_bytes(), "video/quicktime"),
    ]):
        ch = client.post(f"{API}/courses/{d['cid']}/chapters", headers=auth_header(d["tok"]["t_own"]), json={"title": f"Ch{i}"})
        le = client.post(f"{API}/chapters/{ch.json()['id']}/lessons", headers=auth_header(d["tok"]["t_own"]), json={
            "title": f"视频课{i}", "content_type": "video",
        })
        lid = le.json()["id"]
        r = _upload(client, d["tok"]["t_own"], lid, name, content, mime)
        assert r.status_code == 200, f"{name}: {r.text}"
        data = r.json()
        lesson = data["lesson"]
        assert lesson["video_source"] == "upload"
        assert lesson["video_url"] is None
        assert lesson["video_filename"] == name
        assert lesson["video_content_type"] == mime
        assert lesson["video_size"] == len(content)
        assert "video_storage_key" not in lesson, "不得向客户端暴露 storage key"
        assert data["playback_url"].startswith("/api/v1/media/lesson-videos/")
        assert data["expires_at"]
    assert len(_video_keys(test_settings)) == 3


def test_admin_uploads_success(client, db_session_factory):
    d = _setup(client, db_session_factory)
    r = _upload(client, d["tok"]["admin"], d["lid"], "a.mp4", mp4_bytes(), "video/mp4")
    assert r.status_code == 200, r.text


def test_other_teacher_and_students_forbidden(client, db_session_factory):
    d = _setup(client, db_session_factory)
    for role in ("t_other", "s_yes", "s_no"):
        r = _upload(client, d["tok"][role], d["lid"], "a.mp4", mp4_bytes(), "video/mp4")
        assert r.status_code == 403, f"{role}: {r.status_code}"
        assert r.json()["detail"]["code"] == "FORBIDDEN"


def test_upload_missing_lesson_404(client, db_session_factory):
    d = _setup(client, db_session_factory)
    r = _upload(client, d["tok"]["t_own"], 99999, "a.mp4", mp4_bytes(), "video/mp4")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "LESSON_NOT_FOUND"


def test_upload_non_video_lesson_409(client, db_session_factory):
    d = _setup(client, db_session_factory)
    r = _upload(client, d["tok"]["t_own"], d["md_lid"], "a.mp4", mp4_bytes(), "video/mp4")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "LESSON_NOT_VIDEO"


def test_upload_requires_login(client, db_session_factory):
    d = _setup(client, db_session_factory)
    r = client.put(
        f"{API}/lessons/{d['lid']}/video-file",
        files={"file": ("a.mp4", mp4_bytes(), "video/mp4")},
    )
    assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════
# 文件校验
# ══════════════════════════════════════════════════════════════════

def test_upload_bad_extension_415(client, db_session_factory):
    d = _setup(client, db_session_factory)
    r = _upload(client, d["tok"]["t_own"], d["lid"], "evil.exe", b"MZ" + b"x" * 100, "application/octet-stream")
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "VIDEO_TYPE_UNSUPPORTED"


def test_upload_bad_mime_415(client, db_session_factory):
    d = _setup(client, db_session_factory)
    r = _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(), "application/pdf")
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "VIDEO_TYPE_UNSUPPORTED"


def test_upload_extension_mime_mismatch_415(client, db_session_factory):
    d = _setup(client, db_session_factory)
    r = _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(), "video/webm")
    assert r.status_code == 415


def test_upload_fake_magic_415(client, db_session_factory):
    d = _setup(client, db_session_factory)
    r = _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", b"GIF89a" + b"x" * 100, "video/mp4")
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "VIDEO_CONTENT_INVALID"


def test_upload_empty_file_400(client, db_session_factory):
    d = _setup(client, db_session_factory)
    r = _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", b"", "video/mp4")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "VIDEO_FILE_EMPTY"


def test_upload_too_large_413_no_residue(client, db_session_factory, test_settings):
    d = _setup(client, db_session_factory)
    test_settings.video_max_upload_bytes = 512
    r = _upload(client, d["tok"]["t_own"], d["lid"], "big.mp4", mp4_bytes(2048), "video/mp4")
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "VIDEO_TOO_LARGE"
    # staging 与最终目录均无残留
    assert _staging_files(test_settings) == []
    assert _video_keys(test_settings) == []


def test_upload_bad_filename_rejected(client, db_session_factory, test_settings):
    d = _setup(client, db_session_factory)
    # 注：NUL/控制字符无法经 httpx multipart 编码传输（会被剥离），
    # 该类校验在 service 层单独单测（见 test_service_rejects_control_char_filenames）
    cases = [
        ("../escape.mp4", "VIDEO_FILENAME_INVALID"),
        ("a\\b.mp4", "VIDEO_FILENAME_INVALID"),
        ("a/b.mp4", "VIDEO_FILENAME_INVALID"),
        ("a" * 300 + ".mp4", "VIDEO_FILENAME_INVALID"),
        ("..mp4", "VIDEO_FILENAME_INVALID"),
    ]
    for name, err_code in cases:
        r = _upload(client, d["tok"]["t_own"], d["lid"], name, mp4_bytes(), "video/mp4")
        assert r.status_code == 400, f"{name}: {r.status_code}"
        assert r.json()["detail"]["code"] == err_code, name
    assert _video_keys(test_settings) == []


def test_service_rejects_control_char_filenames():
    """service 层单测：NUL 与控制字符文件名被拒绝（HTTP 层无法传输此类名称）"""
    from fastapi import UploadFile
    from io import BytesIO

    from app.config import Settings
    from app.services import lesson_video_service as svc

    settings = Settings(video_storage_dir="C:/unused-videos", secret_key="test")
    for name in ("x\x00y.mp4", "x\x01y.mp4", "x\x1fy.mp4"):
        upload = UploadFile(filename=name, file=BytesIO(mp4_bytes()), headers={"content-type": "video/mp4"})
        try:
            import asyncio
            asyncio.run(svc.store_upload(upload, 1, settings))
            raise AssertionError(f"控制字符文件名 {name!r} 应被拒绝")
        except Exception as exc:
            assert getattr(exc, "detail", {}).get("code") == "VIDEO_FILENAME_INVALID", name


def test_stored_key_is_uuid_without_original_name(client, db_session_factory, test_settings):
    d = _setup(client, db_session_factory)
    r = _upload(client, d["tok"]["t_own"], d["lid"], "秘密文件.mp4", mp4_bytes(), "video/mp4")
    assert r.status_code == 200
    keys = _video_keys(test_settings)
    assert len(keys) == 1
    key = keys[0]
    # key = lessons/{lesson_id}/{uuid}.{ext}，不含原文件名
    assert key.startswith(f"lessons/{d['lid']}/")
    assert key.endswith(".mp4")
    assert "秘密文件" not in key
    name_part = key.rsplit("/", 1)[1].split(".")[0]
    assert len(name_part) == 32 and all(c in "0123456789abcdef" for c in name_part)
    # 解析后位于视频根目录内
    resolved = (test_settings.video_storage_path / key).resolve()
    assert resolved.is_relative_to(test_settings.video_storage_path.resolve())


# ══════════════════════════════════════════════════════════════════
# 生命周期
# ══════════════════════════════════════════════════════════════════

def test_upload_clears_video_url_and_sets_metadata(client, db_session_factory):
    d = _setup(client, db_session_factory)
    r = _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(300), "video/mp4")
    assert r.status_code == 200
    lesson = r.json()["lesson"]
    assert lesson["video_source"] == "upload"
    assert lesson["video_url"] is None
    assert lesson["video_filename"] == "a.mp4"
    assert lesson["video_content_type"] == "video/mp4"
    assert lesson["video_size"] == 300


def test_second_upload_replaces_old_file(client, db_session_factory, test_settings):
    d = _setup(client, db_session_factory)
    assert _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(100), "video/mp4").status_code == 200
    first_key = _video_keys(test_settings)[0]
    assert _upload(client, d["tok"]["t_own"], d["lid"], "b.webm", webm_bytes(200), "video/webm").status_code == 200
    keys = _video_keys(test_settings)
    assert len(keys) == 1, "替换后旧文件应被删除"
    assert keys[0] != first_key
    assert keys[0].endswith(".webm")


def test_failed_second_upload_keeps_old_record_and_file(client, db_session_factory, test_settings):
    d = _setup(client, db_session_factory)
    assert _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(100), "video/mp4").status_code == 200
    first_key = _video_keys(test_settings)[0]

    # 第二次上传校验失败（伪造魔数）
    r = _upload(client, d["tok"]["t_own"], d["lid"], "fake.mp4", b"GIF89a" + b"x" * 50, "video/mp4")
    assert r.status_code == 415

    # 旧文件仍在，数据库记录未被破坏
    assert _video_keys(test_settings) == [first_key]
    resp = client.get(f"{API}/courses/{d['cid']}/chapters", headers=auth_header(d["tok"]["t_own"]))
    lesson = next(l for ch in resp.json()["items"] for l in ch["lessons"] if l["id"] == d["lid"])
    assert lesson["video_source"] == "upload"
    assert lesson["video_filename"] == "a.mp4"


def test_switch_to_external_clears_metadata_and_file(client, db_session_factory, test_settings):
    d = _setup(client, db_session_factory)
    assert _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(), "video/mp4").status_code == 200
    assert len(_video_keys(test_settings)) == 1

    r = client.patch(f"{API}/lessons/{d['lid']}", headers=auth_header(d["tok"]["t_own"]), json={
        "video_url": "https://v.example.com/new.mp4",
    })
    assert r.status_code == 200, r.text
    lesson = r.json()
    assert lesson["video_source"] == "external"
    assert lesson["video_url"] == "https://v.example.com/new.mp4"
    assert lesson["video_filename"] is None
    assert lesson["video_size"] is None
    assert _video_keys(test_settings) == [], "切换外链后本地文件应被删除"


def test_video_url_none_keeps_uploaded_file(client, db_session_factory, test_settings):
    """video_url=None 且当前来源为 upload：不得隐式删除本地文件"""
    d = _setup(client, db_session_factory)
    assert _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(), "video/mp4").status_code == 200
    r = client.patch(f"{API}/lessons/{d['lid']}", headers=auth_header(d["tok"]["t_own"]), json={
        "video_url": None,
    })
    assert r.status_code == 200
    assert len(_video_keys(test_settings)) == 1, "upload 来源传 video_url=None 不应删除文件"


def test_delete_video_file_restores_external(client, db_session_factory, test_settings):
    d = _setup(client, db_session_factory)
    assert _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(), "video/mp4").status_code == 200

    r = client.delete(f"{API}/lessons/{d['lid']}/video-file", headers=auth_header(d["tok"]["t_own"]))
    assert r.status_code == 204
    resp = client.get(f"{API}/courses/{d['cid']}/chapters", headers=auth_header(d["tok"]["t_own"]))
    lesson = next(l for ch in resp.json()["items"] for l in ch["lessons"] if l["id"] == d["lid"])
    assert lesson["video_source"] == "external"
    assert lesson["video_url"] is None
    assert lesson["video_filename"] is None
    assert _video_keys(test_settings) == []


def test_delete_lesson_cleans_file(client, db_session_factory, test_settings):
    d = _setup(client, db_session_factory)
    assert _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(), "video/mp4").status_code == 200
    r = client.delete(f"{API}/lessons/{d['lid']}", headers=auth_header(d["tok"]["t_own"]))
    assert r.status_code == 204
    assert _video_keys(test_settings) == []


def test_delete_chapter_cleans_all_files(client, db_session_factory, test_settings):
    d = _setup(client, db_session_factory)
    assert _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(), "video/mp4").status_code == 200
    ch2 = client.post(f"{API}/courses/{d['cid']}/chapters", headers=auth_header(d["tok"]["t_own"]), json={"title": "Ch2"})
    ch2id = ch2.json()["id"]
    le2 = client.post(f"{API}/chapters/{ch2id}/lessons", headers=auth_header(d["tok"]["t_own"]), json={
        "title": "视频课2", "content_type": "video",
    })
    assert _upload(client, d["tok"]["t_own"], le2.json()["id"], "b.webm", webm_bytes(), "video/webm").status_code == 200
    assert len(_video_keys(test_settings)) == 2

    r = client.delete(f"{API}/chapters/{ch2id}", headers=auth_header(d["tok"]["t_own"]))
    assert r.status_code == 204
    keys = _video_keys(test_settings)
    assert len(keys) == 1 and keys[0].endswith(".mp4")


def test_db_commit_failure_removes_new_file(client, app, db_session_factory, test_settings, monkeypatch):
    d = _setup(client, db_session_factory)
    # 使用不抛出异常的 TestClient 以接收 500 响应体
    from fastapi.testclient import TestClient

    quiet_client = TestClient(app, raise_server_exceptions=False)

    def _fail_commit(*args, **kwargs):
        raise RuntimeError("db down")

    # 让 Session.commit 抛异常（仅本次上传请求生效）
    from sqlalchemy.orm import Session as ORMSession

    monkeypatch.setattr(ORMSession, "commit", _fail_commit)
    try:
        r = _upload(quiet_client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(), "video/mp4")
    finally:
        quiet_client.close()
    assert r.status_code == 500
    # 新文件被清理，数据库记录未变
    assert _video_keys(test_settings) == []
    resp = client.get(f"{API}/courses/{d['cid']}/chapters", headers=auth_header(d["tok"]["t_own"]))
    lesson = next(l for ch in resp.json()["items"] for l in ch["lessons"] if l["id"] == d["lid"])
    assert lesson["video_source"] == "external"
    assert lesson["video_url"] == "https://v.example.com/old.mp4"


# ══════════════════════════════════════════════════════════════════
# 签名播放
# ══════════════════════════════════════════════════════════════════

def _uploaded_setup(client, db_session_factory):
    d = _setup(client, db_session_factory)
    r = _upload(client, d["tok"]["t_own"], d["lid"], "a.mp4", mp4_bytes(500), "video/mp4")
    assert r.status_code == 200
    d["playback_url"] = r.json()["playback_url"]
    return d


def test_playback_url_grants_for_enrolled_owner_admin(client, db_session_factory):
    d = _uploaded_setup(client, db_session_factory)
    for role in ("s_yes", "t_own", "admin"):
        r = client.get(f"{API}/lessons/{d['lid']}/video-playback-url", headers=auth_header(d["tok"][role]))
        assert r.status_code == 200, f"{role}: {r.status_code}"
        assert r.json()["url"].startswith("/api/v1/media/lesson-videos/")
        assert "sig=" in r.json()["url"]


def test_playback_url_is_same_origin_relative_path(client, db_session_factory):
    d = _uploaded_setup(client, db_session_factory)
    response = client.get(
        f"{API}/lessons/{d['lid']}/video-playback-url",
        headers=auth_header(d["tok"]["t_own"]),
    )

    assert response.status_code == 200, response.text
    url = response.json()["url"]
    assert url.startswith("/api/v1/media/lesson-videos/")
    assert not url.startswith(("http://", "https://"))


def test_playback_url_denied_for_unenrolled_nonowner(client, db_session_factory):
    d = _uploaded_setup(client, db_session_factory)
    for role in ("s_no", "t_other"):
        r = client.get(f"{API}/lessons/{d['lid']}/video-playback-url", headers=auth_header(d["tok"][role]))
        assert r.status_code == 403, f"{role}: {r.status_code}"


def test_playback_url_404_for_external_or_no_file(client, db_session_factory):
    d = _setup(client, db_session_factory)  # 未上传：external 来源
    r = client.get(f"{API}/lessons/{d['lid']}/video-playback-url", headers=auth_header(d["tok"]["t_own"]))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "VIDEO_NOT_FOUND"


def test_media_stream_returns_video_with_mime(client, db_session_factory):
    d = _uploaded_setup(client, db_session_factory)
    r = client.get(d["playback_url"])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("video/mp4")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"
    assert len(r.content) == 500


def test_media_stream_supports_range(client, db_session_factory):
    d = _uploaded_setup(client, db_session_factory)
    r = client.get(d["playback_url"], headers={"Range": "bytes=0-9"})
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 0-9/500"
    assert len(r.content) == 10
    assert r.headers["accept-ranges"] == "bytes"


def test_media_stream_rejects_tampered_signature(client, db_session_factory):
    d = _uploaded_setup(client, db_session_factory)
    base, query = d["playback_url"].split("?", 1)
    params = dict(p.split("=", 1) for p in query.split("&"))
    # 篡改 lesson id（URL 路径）
    tampered_path = base.replace(f"/{d['lid']}", "/99999")
    r = client.get(f"{tampered_path}?{query}")
    assert r.status_code == 404  # 课时不存在
    # 篡改 uid
    r = client.get(f"{base}?uid={int(params['uid']) + 1}&expires={params['expires']}&sig={params['sig']}")
    assert r.status_code == 401
    # 篡改 expires
    r = client.get(f"{base}?uid={params['uid']}&expires={int(params['expires']) + 60}&sig={params['sig']}")
    assert r.status_code == 401
    # 篡改 sig
    bad_sig = "0" * 64
    r = client.get(f"{base}?uid={params['uid']}&expires={params['expires']}&sig={bad_sig}")
    assert r.status_code == 401


def test_media_stream_rejects_expired_signature(client, db_session_factory, test_settings):
    d = _uploaded_setup(client, db_session_factory)
    base, query = d["playback_url"].split("?", 1)
    params = dict(p.split("=", 1) for p in query.split("&"))
    old = int(time.time()) - 100
    params["expires"] = str(old)
    from app.services.lesson_video_service import create_playback_signature

    with db_session_factory() as db:
        lesson = db.get(models.Lesson, d["lid"])
        key = lesson.video_storage_key
    sig, _ = create_playback_signature(
        d["lid"], int(params["uid"]), key, test_settings,
        now=datetime.fromtimestamp(old, tz=timezone.utc),
    )
    r = client.get(f"{base}?uid={params['uid']}&expires={old}&sig={sig}")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "VIDEO_SIGNATURE_EXPIRED"


def test_old_signature_invalid_after_replacement(client, db_session_factory):
    d = _uploaded_setup(client, db_session_factory)
    old_url = d["playback_url"]
    # 替换视频 → storage key 变化 → 旧签名立即失效
    assert _upload(client, d["tok"]["t_own"], d["lid"], "b.webm", webm_bytes(300), "video/webm").status_code == 200
    r = client.get(old_url)
    assert r.status_code == 401


def test_media_denied_after_drop_enrollment(client, db_session_factory):
    d = _uploaded_setup(client, db_session_factory)
    # 学生先获取自己的签名 URL（签名 uid = 学生 id）
    url = client.get(
        f"{API}/lessons/{d['lid']}/video-playback-url",
        headers=auth_header(d["tok"]["s_yes"]),
    ).json()["url"]
    # 管理员撤销授课名单，已经签发的学生媒体 URL 也立即失效。
    with db_session_factory() as db:
        student = db.query(models.User).filter_by(username="s_yes").one()
    r = client.delete(f"{API}/courses/{d['cid']}/students/{student.id}", headers=auth_header(d["tok"]["admin"]))
    assert r.status_code == 204
    resp = client.get(url)
    assert resp.status_code == 403, "退课后媒体端点必须拒绝"


def test_media_authority_comes_from_offering_roster_not_whitelist(client, db_session_factory):
    d = _uploaded_setup(client, db_session_factory)
    # 课程改为白名单可见，加白名单学生后重新签发，再移出白名单
    client.patch(f"{API}/courses/{d['cid']}", headers=auth_header(d["tok"]["t_own"]), json={"visibility": "whitelist"})
    with db_session_factory() as db:
        student = db.query(models.User).filter_by(username="s_yes").first()
    client.post(
        f"{API}/courses/{d['cid']}/whitelist",
        headers=auth_header(d["tok"]["t_own"]),
        json={"student_id": student.id},
    )
    # 重新签发后移出白名单
    url = client.get(f"{API}/lessons/{d['lid']}/video-playback-url", headers=auth_header(d["tok"]["s_yes"])).json()["url"]
    client.delete(f"{API}/courses/{d['cid']}/whitelist/{student.id}", headers=auth_header(d["tok"]["t_own"]))
    resp = client.get(url)
    assert resp.status_code == 200, "内容白名单变更不撤销管理员授课授权"
    revoked = client.delete(f"{API}/courses/{d['cid']}/students/{student.id}", headers=auth_header(d["tok"]["admin"]))
    assert revoked.status_code == 204
    resp = client.get(url)
    assert resp.status_code == 403


def test_media_missing_file_404_with_log(client, db_session_factory, test_settings, caplog):
    d = _uploaded_setup(client, db_session_factory)
    # 磁盘文件被外部删除
    key = _video_keys(test_settings)[0]
    (test_settings.video_storage_path / key).unlink()

    import logging
    with caplog.at_level(logging.ERROR, logger="lesson_video"):
        r = client.get(d["playback_url"])
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "VIDEO_NOT_FOUND"
    assert any("lesson video file missing" in rec.getMessage() for rec in caplog.records)
