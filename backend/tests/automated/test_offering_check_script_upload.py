"""Class-owned script uploads validate text without running or publishing it."""
from copy import deepcopy

import pytest

from app.models import AcademicTerm, Chapter, LabAssessment, LabRun, Lesson, TeachingClass, User
from app.models.teaching import CourseOffering, OfferingExperiment
from lab_test_support import headers, make_case


@pytest.fixture()
def upload_case(db_session_factory, test_settings):
    case = make_case(db_session_factory, test_settings)
    # The classroom teacher intentionally does not own the shared module.
    with db_session_factory() as db:
        db.get(CourseOffering, case.offering_id).teacher_id = case.other_teacher.id
        db.commit()
    return case


def endpoint(case, offering_id=None):
    return f'/api/v1/offerings/{offering_id or case.offering_id}/experiments/module/{case.module_id}/assessment'


def upload(client, case, settings, *, actor=None, filename='check.py', content='pass\n', url=None):
    return client.post((url or endpoint(case)) + '/check-script',
        headers=headers(actor or case.other_teacher, settings), files={'file': (filename, content, 'text/plain')})


def test_class_teacher_upload_only_fills_draft_then_explicit_save_round_trips_without_shared_changes(
        client, db_session_factory, test_settings, upload_case, tmp_path):
    case = upload_case
    marker = tmp_path / 'must-not-execute'
    script = f'from pathlib import Path\nPath({str(marker)!r}).write_text("unsafe execution")\n'
    teacher = headers(case.other_teacher, test_settings)
    url = endpoint(case)
    shared_url = f'/api/v1/labs/entries/module/{case.module_id}/assessment'
    before = client.get(url, headers=teacher).json()
    other_before = client.get(endpoint(case, case.other_offering_id), headers=headers(case.teacher, test_settings)).json()
    result = upload(client, case, test_settings, content=script)
    assert result.status_code == 200, result.text
    assert result.json() == {'script': script}
    assert not marker.exists()
    assert client.get(url, headers=teacher).json() == before
    assert upload(client, case, test_settings, content=script, url=shared_url).status_code == 403

    config = deepcopy(before['published']['config'])
    config['criteria'] = [{'id': 'custom', 'name': 'Custom cell', 'description': 'Check the answer',
        'source': 'cell', 'max_score': 100, 'levels': []}]
    config['cell_checks'] = [{'id': 'custom', 'cell_id': 'code', 'kind': 'python', 'script': result.json()['script']}]
    saved = client.put(url, headers=teacher, json=config)
    assert saved.status_code == 200, saved.text
    assert saved.json()['status'] == 'draft'
    assert client.get(url, headers=teacher).json()['draft']['config']['cell_checks'][0]['script'] == script
    assert client.get(endpoint(case, case.other_offering_id), headers=headers(case.teacher, test_settings)).json() == other_before
    with db_session_factory() as db:
        assert db.get(LabAssessment, case.assessment_id).config == before['published']['config']
        assert db.query(LabRun).count() == 0
    assert not marker.exists()


@pytest.mark.parametrize('filename,content', [
    ('check.txt', b'pass\n'), ('check.py', b'bad python {'), ('check.py', b'\xff'),
    ('check.py', b'#' + b'a' * 20000), ('check.py', b'#' + '中'.encode() * 6667),
])
def test_class_upload_rejects_extension_syntax_encoding_and_byte_limit(client, test_settings, upload_case, filename, content):
    response = upload(client, upload_case, test_settings, filename=filename, content=content)
    assert response.status_code == 422, response.text
    assert response.json()['detail']['code'] == 'LAB_CHECK_SCRIPT_INVALID'


@pytest.mark.parametrize('actor', ['other_teacher', 'admin'])
def test_class_upload_accepts_current_teacher_admin_and_exact_byte_limit(client, test_settings, upload_case, actor):
    script = '#' + 'a' * 19999
    result = upload(client, upload_case, test_settings, actor=getattr(upload_case, actor), filename='CHECK.PY', content=script)
    assert result.status_code == 200 and result.json()['script'] == script


@pytest.mark.parametrize('actor', ['teacher', 'student', 'other_student'])
def test_class_upload_denies_other_class_shared_owner_and_students(client, test_settings, upload_case, actor):
    assert upload(client, upload_case, test_settings, actor=getattr(upload_case, actor)).status_code == 403


@pytest.mark.parametrize('withdrawal', ['offering', 'class', 'term', 'reassigned', 'disabled', 'unreleased', 'removed'])
def test_class_upload_checks_current_assignment_readonly_account_and_binding(
        client, db_session_factory, test_settings, upload_case, withdrawal):
    case = upload_case
    with db_session_factory() as db:
        offering = db.get(CourseOffering, case.offering_id)
        delivery = db.query(OfferingExperiment).filter_by(offering_id=offering.id).one()
        if withdrawal == 'offering': offering.status = 'archived'
        if withdrawal == 'class': db.get(TeachingClass, offering.teaching_class_id).status = 'archived'
        if withdrawal == 'term': db.get(AcademicTerm, offering.academic_term_id).status = 'closed'
        if withdrawal == 'reassigned': offering.teacher_id = case.teacher.id
        if withdrawal == 'disabled': db.get(User, case.other_teacher.id).status = 'disabled'
        if withdrawal == 'unreleased': delivery.status = 'draft'
        if withdrawal == 'removed': db.delete(delivery)
        db.commit()
    result = upload(client, case, test_settings)
    assert result.status_code in (401, 403, 409), result.text
    with db_session_factory() as db:
        assert db.query(LabAssessment).count() == 1
        assert db.query(LabRun).count() == 0


def test_class_upload_rejects_disabled_lab_and_unbound_entry(client, test_settings, upload_case):
    test_settings.lab_enabled = False
    result = upload(client, upload_case, test_settings)
    assert result.status_code == 503 and result.json()['detail']['code'] == 'LAB_DISABLED'
    test_settings.lab_enabled = True
    result = upload(client, upload_case, test_settings, url=endpoint(upload_case).replace('/module/', '/lesson/'))
    assert result.status_code == 409 and result.json()['detail']['code'] == 'EXPERIMENT_RELEASE_REQUIRED'


def test_published_lesson_binding_accepts_class_upload(client, db_session_factory, test_settings, upload_case):
    with db_session_factory() as db:
        offering = db.get(CourseOffering, upload_case.offering_id)
        chapter = Chapter(course_id=offering.course_id, title='Chapter')
        db.add(chapter)
        db.flush()
        lesson = Lesson(chapter_id=chapter.id, title='Notebook lesson', status='published', template_id=upload_case.template_id)
        db.add(lesson)
        db.flush()
        db.add(OfferingExperiment(offering_id=offering.id, entry_kind='lesson', entry_id=lesson.id,
            template_version_id=upload_case.version_id, status='published'))
        db.commit()
        url = f'/api/v1/offerings/{offering.id}/experiments/lesson/{lesson.id}/assessment'
    result = upload(client, upload_case, test_settings, url=url)
    assert result.status_code == 200 and result.json() == {'script': 'pass\n'}


@pytest.mark.parametrize('change', ['reassigned', 'closed', 'revoked_session'])
def test_class_upload_rechecks_permission_after_reading_the_file(
        client, db_session_factory, test_settings, upload_case, monkeypatch, change):
    from starlette.datastructures import UploadFile
    original_read = UploadFile.read

    async def read_after_change(file, size=-1):
        result = await original_read(file, size)
        with db_session_factory() as db:
            offering = db.get(CourseOffering, upload_case.offering_id)
            if change == 'reassigned': offering.teacher_id = upload_case.teacher.id
            if change == 'closed': db.get(AcademicTerm, offering.academic_term_id).status = 'closed'
            if change == 'revoked_session': db.get(User, upload_case.other_teacher.id).session_version += 1
            db.commit()
        return result

    monkeypatch.setattr(UploadFile, 'read', read_after_change)
    response = upload(client, upload_case, test_settings)
    assert response.status_code == (409 if change == 'closed' else 403), response.text
