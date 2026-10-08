"""Container validation rejects forged metadata before replacing stored bytes."""
from io import BytesIO
import stat
import struct
import zipfile

from fastapi import HTTPException, UploadFile
import pytest

from app.services import course_material_service as service
from app.services import material_upload_validation as checks
from material_test_files import minimal_pdf, minimal_pdf_xref_stream, minimal_pptx, zip_bytes


def validate(settings, filename, body):
    upload = UploadFile(filename=filename, file=BytesIO(body))
    result = service.validate_upload(upload, settings)
    assert upload.file.tell() == 0
    return result


@pytest.mark.parametrize('filename,body', [
    ('lecture.pdf', minimal_pdf()),
    ('stream.pdf', minimal_pdf_xref_stream()),
    ('lesson.zip', zip_bytes([('chapter/notes.txt', b'lecture notes')])),
    ('slides.pptx', minimal_pptx()),
])
def test_accepts_small_structurally_valid_materials(test_settings, filename, body):
    assert validate(test_settings, filename, body)[0] == filename


@pytest.mark.parametrize('body', [
    b'%PDF-1.7\nforged contents',
    minimal_pdf()[:-6],
    minimal_pdf().replace(b'/Root 1 0 R', b'/None 1 0 R'),
    minimal_pdf().replace(b'/Root 1 0 R', b'/Root 3 0 R'),
    minimal_pdf().replace(b'/Root 1 0 R', b'/Root 1 9 R'),
    minimal_pdf().replace(b' obj\n', b' ???\n'),
    minimal_pdf().replace(b'%PDF-1.4', b'%PDF-9.9'),
    minimal_pdf().replace(b'startxref\n', b'startxref\n99999999'),
    minimal_pdf_xref_stream().replace(b'/Length 35', b'/Length 99'),
    minimal_pdf_xref_stream().replace(b'/W [1 4 2]', b'/W [0 0 0]'),
    minimal_pdf_xref_stream().replace(b'/Size 5', b'/Size 6'),
])
def test_rejects_pdf_magic_without_valid_objects_or_cross_reference(test_settings, body):
    with pytest.raises(HTTPException) as raised:
        validate(test_settings, 'forged.pdf', body)
    assert raised.value.status_code == 415


@pytest.mark.parametrize('names', [
    ['../answer.txt'], ['/etc/passwd'], ['C:/answer.txt'], ['chapter\\answer.txt'],
    ['chapter/./answer.txt'], ['chapter//answer.txt'], ['chapter/answer.txt '],
    ['same.txt', 'same.txt'], ['same.txt', 'SAME.txt'], ['A.txt', 'Ａ.txt'],
    ['folder', 'folder/notes.txt'],
])
def test_rejects_unsafe_and_duplicate_archive_paths(test_settings, names):
    with pytest.raises(HTTPException) as raised:
        validate(test_settings, 'unsafe.zip', zip_bytes([(name, b'notes') for name in names]))
    assert raised.value.status_code == 415


def test_rejects_archive_symlink_and_encryption(test_settings):
    info = zipfile.ZipInfo('link')
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    linked = zip_bytes([(info, b'/etc/passwd')])
    encrypted = bytearray(zip_bytes([('notes.txt', b'lecture notes')]))
    local, central = encrypted.index(b'PK\x03\x04'), encrypted.index(b'PK\x01\x02')
    struct.pack_into('<H', encrypted, local + 6, struct.unpack_from('<H', encrypted, local + 6)[0] | 1)
    struct.pack_into('<H', encrypted, central + 8, struct.unpack_from('<H', encrypted, central + 8)[0] | 1)
    for body in [linked, encrypted]:
        with pytest.raises(HTTPException) as raised:
            validate(test_settings, 'unsafe.zip', body)
        assert raised.value.status_code == 415


def test_rejects_bomb_and_expansion_limits_without_unpacking(test_settings, monkeypatch):
    highly_compressed = zip_bytes([('zeros.txt', b'0' * 10000)])
    with pytest.raises(HTTPException):
        validate(test_settings, 'bomb.zip', highly_compressed)
    two_files = zip_bytes([('a.txt', b'1234567890'), ('b.txt', b'1234567890')])
    monkeypatch.setattr(checks, 'MAX_EXPANDED_BYTES', 15)
    with pytest.raises(HTTPException):
        validate(test_settings, 'large.zip', two_files)
    monkeypatch.setattr(checks, 'MAX_EXPANDED_BYTES', 1024)
    monkeypatch.setattr(checks, 'MAX_MEMBER_BYTES', 9)
    with pytest.raises(HTTPException):
        validate(test_settings, 'large-member.zip', two_files)


def test_limits_directory_before_zipfile_parsing(test_settings, monkeypatch):
    body = bytearray(zip_bytes([('notes.txt', b'lecture')]))
    eocd = body.index(b'PK\x05\x06')
    struct.pack_into('<HH', body, eocd + 8, 5001, 5001)
    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError('Oversized directory must be rejected before parsing')
    monkeypatch.setattr(checks.zipfile, 'ZipFile', unexpected_parse)
    with pytest.raises(HTTPException) as raised:
        validate(test_settings, 'many.zip', body)
    assert raised.value.status_code == 415


@pytest.mark.parametrize('body', [
    b'PK\x03\x04forged',
    zip_bytes([('notes.txt', b'lecture')])[:-2],
    zip_bytes([('notes.txt', b'lecture')]).replace(b'PK\x03\x04', b'FAKE', 1),
])
def test_rejects_truncated_or_inconsistent_archive_structure(test_settings, body):
    with pytest.raises(HTTPException) as raised:
        validate(test_settings, 'malformed.zip', body)
    assert raised.value.status_code == 415


def test_pptx_requires_package_members_and_rejects_macro_mismatch(test_settings):
    missing = zip_bytes([('notes.txt', b'lecture')])
    wrong_case = minimal_pptx().replace(b'[Content_Types].xml', b'[content_types].xml')
    macro = zip_bytes([('[Content_Types].xml', b'<Types/>'), ('_rels/.rels', b'<Relationships/>'),
                      ('ppt/presentation.xml', b'<presentation/>'), ('ppt/vbaProject.bin', b'macro')])
    for body in [missing, wrong_case, macro]:
        with pytest.raises(HTTPException) as raised:
            validate(test_settings, 'invalid.pptx', body)
        assert raised.value.status_code == 415


def test_video_ftyp_declared_length_must_fit_file(test_settings):
    for size in [0, 12, 1000]:
        body = struct.pack('>I', size) + b'ftypisom' + b'\x00' * 4 + b'isom'
        with pytest.raises(HTTPException) as raised:
            validate(test_settings, 'invalid.mp4', body)
        assert raised.value.status_code == 415


def test_size_rejected_before_container_parsing(test_settings, monkeypatch):
    test_settings.material_max_upload_bytes = 8
    def unexpected_parse(*_args):
        raise AssertionError('Size check should precede parsing')
    monkeypatch.setattr(service, 'validate_pdf', unexpected_parse)
    with pytest.raises(HTTPException) as raised:
        validate(test_settings, 'large.pdf', minimal_pdf())
    assert raised.value.status_code == 413
