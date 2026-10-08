"""Regressions found while independently reviewing offering authorization."""
from app import models
from conftest import create_user
from test_school_offerings import school  # noqa: F401


def test_student_history_survives_transfer_without_granting_old_task_access(client, school, db_session_factory):  # noqa: F811
    with db_session_factory() as db:
        ids = []
        for index, who in enumerate(('a', 'b')):
            task = models.Assignment(course_id=school['course'], offering_id=school['offerings'][index],
                title='历史作业-' + who, status='published')
            db.add(task); db.flush()
            question = models.JudgeQuestion(assignment_id=task.id, title='历史题', function_name='f', hidden_tests='')
            db.add(question); db.flush()
            submission = models.Submission(question_id=question.id, student_id=school['users'][who].id,
                code='private-code-' + who, score=80, status='completed', grading_status='completed')
            exam = models.Exam(course_id=school['course'], offering_id=school['offerings'][index],
                title='历史考试-' + who, status='published', show_score_after_grading=True)
            db.add_all([submission, exam]); db.flush()
            exam_submission = models.ExamSubmission(exam_id=exam.id, student_id=school['users'][who].id, status='graded', score=90)
            db.add(exam_submission); db.flush()
            ids.append((task.id, submission.id, exam.id, exam_submission.id))
        db.commit()
    response = client.post(f"/api/v1/offerings/{school['offerings'][0]}/students/{school['users']['a'].id}/transfer",
        json={'destination_offering_id': school['offerings'][1], 'reason': '历史只读回归'}, headers=school['headers']['admin'])
    assert response.status_code == 200, response.text
    header = school['headers']['a']
    response = client.get('/api/v1/users/me/history', headers=header)
    assert response.status_code == 200, response.text
    assert response.json()['total'] == 2
    assert {row['entry_title'] for row in response.json()['items']} == {'历史题', '历史考试-a'}
    detail = client.get(f'/api/v1/users/me/history/assignment/{ids[0][1]}', headers=header)
    assert detail.status_code == 200 and detail.json()['code'] == 'private-code-a'
    assert client.get(f'/api/v1/users/me/history/assignment/{ids[1][1]}', headers=header).status_code == 404
    assert client.get(f'/api/v1/users/me/history/exam/{ids[1][3]}', headers=header).status_code == 404
    assert client.get(f'/api/v1/assignments/{ids[0][0]}', headers=header).status_code in (403, 404)
    assert client.get(f'/api/v1/exams/{ids[0][2]}', headers=header).status_code in (403, 404)


def test_release_rejects_unpublished_and_non_notebook_lessons(client, school, db_session_factory):  # noqa: F811
    with db_session_factory() as db:
        template = models.NotebookTemplate(name='共享实验', owner_id=school['users']['owner'].id, status='published')
        db.add(template); db.flush()
        version = models.NotebookTemplateVersion(template_id=template.id, version_number=1,
            sha256='a' * 64, cells=[], cell_order=[], published_by_id=school['users']['owner'].id)
        db.add(version); db.flush()
        template.current_version_id = version.id
        entry = db.get(models.Lesson, school['lessons'][1])
        entry.content_type = 'notebook'; entry.template_id = template.id
        db.commit()
    payload = {'entry_kind': 'lesson', 'entry_id': school['lessons'][1]}
    path = f"/api/v1/offerings/{school['offerings'][1]}/experiments"
    response = client.post(path, json=payload, headers=school['headers']['other'])
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'EXPERIMENT_ENTRY_UNPUBLISHED'
    with db_session_factory() as db:
        entry = db.get(models.Lesson, school['lessons'][1]); entry.status = 'published'; entry.content_type = 'markdown'; db.commit()
    response = client.post(path, json=payload, headers=school['headers']['other'])
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'EXPERIMENT_ENTRY_INVALID'
    with db_session_factory() as db:
        entry = db.get(models.Lesson, school['lessons'][1]); entry.content_type = 'notebook'; db.commit()
    response = client.post(path, json=payload, headers=school['headers']['other'])
    assert response.status_code == 200, response.text
    response = client.post(path, json={**payload, 'opens_at': '2026-10-03T08:00:00', 'deadline': '2026-10-04T08:00:00+00:00'},
        headers=school['headers']['other'])
    assert response.status_code == 422
    response = client.post(path, json={**payload, 'opens_at': '2026-10-03T08:00:00+00:00', 'deadline': '2026-10-04T08:00:00+00:00'},
        headers=school['headers']['other'])
    assert response.status_code == 200, response.text


def test_whitelist_never_exposes_or_changes_another_instructors_students(client, school, db_session_factory):  # noqa: F811
    with db_session_factory() as db:
        db.add_all([models.CourseWhitelistStudent(course_id=school['course'], student_id=school['users'][who].id)
                    for who in ('a', 'b')]); db.commit()
    path = f"/api/v1/courses/{school['course']}/whitelist"
    response = client.get(path, headers=school['headers']['owner'])
    assert response.status_code == 200, response.text
    assert response.json()['total'] == 1
    assert response.json()['items'][0]['student']['id'] == school['users']['a'].id
    assert 'school-b' not in response.text
    assert client.post(path, json={'student_id': school['users']['b'].id}, headers=school['headers']['owner']).status_code == 403
    assert client.delete(path + f"/{school['users']['b'].id}", headers=school['headers']['owner']).status_code == 403
    assert client.delete(path + f"/{school['users']['a'].id}", headers=school['headers']['owner']).status_code == 204
    assert client.get(path, headers=school['headers']['admin']).json()['total'] == 1


def test_course_roster_count_uses_the_same_student_scope_as_rows(client, school, db_session_factory):  # noqa: F811
    from app.services.offering_service import enroll_student
    from app.models.teaching import CourseOffering
    extra = create_user(db_session_factory, 'school-extra', 'student')
    with db_session_factory() as db:
        enroll_student(db, db.get(CourseOffering, school['offerings'][0]), extra.id)
        db.commit()
    response = client.get(f"/api/v1/courses/{school['course']}/students", headers=school['headers']['owner'])
    assert response.status_code == 200, response.text
    assert response.json()['total'] == 2
    assert {row['id'] for row in response.json()['items']} == {school['users']['a'].id, extra.id}
    response = client.get(f"/api/v1/courses/{school['course']}/students", headers=school['headers']['admin'])
    assert response.json()['total'] == 3


def test_shared_content_deletion_protects_other_class_history(client, school, db_session_factory):  # noqa: F811
    from datetime import datetime, timezone
    with db_session_factory() as db:
        template = models.NotebookTemplate(name='保留历史模板', owner_id=school['users']['owner'].id, status='published')
        db.add(template); db.flush()
        version = models.NotebookTemplateVersion(template_id=template.id, version_number=1, sha256='b' * 64,
            cells=[], cell_order=[], published_by_id=school['users']['owner'].id)
        db.add(version); db.flush()
        record = models.ExperimentRecord(lesson_id=school['lessons'][0], offering_id=school['offerings'][1],
            scope_key=f"offering:{school['offerings'][1]}", template_version_id=version.id, student_id=school['users']['b'].id, status='graded')
        db.add(record); db.flush()
        submission = models.ExperimentSubmission(record_id=record.id, attempt_number=1, client_request_id='history-preserved',
            cells_snapshot={}, score=88, reviewed_at=datetime.now(timezone.utc))
        progress = models.LessonProgress(lesson_id=school['lessons'][2], offering_id=school['offerings'][1],
            student_id=school['users']['b'].id, status='completed')
        db.add_all([submission, progress]); db.commit()
        record_id, submission_id, progress_id = record.id, submission.id, progress.id
    header = school['headers']['owner']
    for lesson in (school['lessons'][0], school['lessons'][2]):
        response = client.delete(f'/api/v1/lessons/{lesson}', headers=header)
        assert response.status_code == 409, response.text
        assert 'school-b' not in response.text
    assert client.delete(f"/api/v1/chapters/{school['chapter']}", headers=header).status_code == 409
    assert client.delete(f"/api/v1/courses/{school['course']}", headers=header).status_code == 204
    with db_session_factory() as db:
        assert db.get(models.ExperimentRecord, record_id) is not None
        assert db.get(models.ExperimentSubmission, submission_id).score == 88
        assert db.get(models.LessonProgress, progress_id).status == 'completed'
