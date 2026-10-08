"""Real API regressions: sharing content never grants another class's data."""
from datetime import date, timedelta

import pytest
from conftest import auth_header, create_user, login, seed_basic_environment


@pytest.fixture()
def school(client, db_session_factory):
    from app.models import AcademicTerm, Chapter, Course, Lesson, TeachingClass, TeachingClassStudent
    seed_basic_environment(db_session_factory)
    users = {role: create_user(db_session_factory, 'school-' + role, actual)
             for role, actual in [('admin', 'admin'), ('owner', 'teacher'), ('other', 'teacher'), ('a', 'student'), ('b', 'student')]}
    with db_session_factory() as db:
        term = AcademicTerm(code='school-term', name='秋季', start_date=date.today(), end_date=date.today() + timedelta(days=100), status='active')
        course = Course(title='共享课程', description='课程', status='published', teacher_id=users['owner'].id)
        db.add_all([term, course]); db.flush()
        classes = [TeachingClass(academic_term_id=term.id, code=k, name=k, status='active') for k in ['A班', 'B班']]
        db.add_all(classes); db.flush()
        for klass, student in zip(classes, ['a', 'b']):
            db.add(TeachingClassStudent(teaching_class_id=klass.id, student_id=users[student].id, status='active'))
        chapter = Chapter(course_id=course.id, title='章节'); db.add(chapter); db.flush()
        lessons = [Lesson(chapter_id=chapter.id, title=state, content='secret-' + state, status=state) for state in ['published', 'draft', 'pending']]
        db.add_all(lessons); db.commit()
        ids = {'course': course.id, 'term': term.id, 'classes': [c.id for c in classes], 'lessons': [l.id for l in lessons], 'chapter': chapter.id}
    headers = {name: auth_header(login(client, user.username)[0]) for name, user in users.items()}
    offerings = []
    for klass, teacher in zip(ids['classes'], ['owner', 'other']):
        response = client.post('/api/v1/offerings', json={'course_id': ids['course'], 'academic_term_id': ids['term'], 'teaching_class_id': klass, 'teacher_id': users[teacher].id}, headers=headers['admin'])
        assert response.status_code == 201, response.text
        offerings.append(response.json()['id'])
    return {**ids, 'users': users, 'headers': headers, 'offerings': offerings}


def test_teacher_cannot_create_authority_by_binding_course(client, school):
    result = client.patch(f"/api/v1/courses/{school['course']}", json={'teaching_class_ids': school['classes']}, headers=school['headers']['owner'])
    assert result.status_code == 403
    for scope in ['selectable', 'linked']:
        body = client.get('/api/v1/teaching-classes', params={'scope': scope}, headers=school['headers']['owner']).json()
        assert [c['id'] for c in body['items']] == [school['classes'][0]]


def test_same_course_content_owner_cannot_read_other_class(client, school):
    headers = school['headers']['owner']
    assert client.get(f"/api/v1/courses/{school['course']}/chapters", headers=school['headers']['other']).status_code == 200
    assert client.get(f"/api/v1/offerings/{school['offerings'][1]}/students", headers=headers).status_code == 403
    students = client.get('/api/v1/users/students', headers=headers).json()['items']
    assert [s['id'] for s in students] == [school['users']['a'].id]
    assert client.get(f"/api/v1/teaching-classes/{school['classes'][1]}/students", headers=headers).status_code == 403


def test_student_chapter_response_does_not_contain_drafts(client, school):
    response = client.get(f"/api/v1/courses/{school['course']}/chapters", headers=school['headers']['a'])
    assert response.status_code == 200, response.text
    lessons = response.json()['items'][0]['lessons']
    assert [lesson['status'] for lesson in lessons] == ['published']
    assert 'secret-draft' not in response.text
    body = client.get('/api/v1/teaching-classes', headers=school['headers']['a']).json()
    assert [c['id'] for c in body['items']] == [school['classes'][0]]


def test_authority_reassignment_immediately_revokes_teacher(client, school):
    offering = school['offerings'][0]
    response = client.patch(f'/api/v1/offerings/{offering}', json={'teacher_id': school['users']['other'].id, 'reason': '换老师'}, headers=school['headers']['admin'])
    assert response.status_code == 200, response.text
    assert client.get(f'/api/v1/offerings/{offering}/students', headers=school['headers']['owner']).status_code == 403
    assert client.get(f'/api/v1/offerings/{offering}/students', headers=school['headers']['other']).status_code == 200


def test_unique_grade_class_and_transfer_preserve_identity(client, school):
    member = school['users']['a'].id
    target = school['offerings'][1]
    response = client.post(f'/api/v1/offerings/{target}/students', json={'student_ids': [member], 'reason': '冲突'}, headers=school['headers']['admin'])
    assert response.status_code == 409
    response = client.post(f"/api/v1/offerings/{school['offerings'][0]}/students/{member}/transfer", json={'destination_offering_id': target, 'reason': '转班'}, headers=school['headers']['admin'])
    assert response.status_code == 200, response.text
    assert [r['id'] for r in client.get('/api/v1/offerings', headers=school['headers']['a']).json()['items']] == [target]


def test_shared_course_submission_aggregation_is_scoped_before_count(client, school, db_session_factory):
    from app.models import Assignment, JudgeQuestion, Submission
    with db_session_factory() as db:
        for index, who in enumerate(['a', 'b']):
            task = Assignment(course_id=school['course'], offering_id=school['offerings'][index], title='作业' + who, status='published')
            db.add(task); db.flush()
            question = JudgeQuestion(assignment_id=task.id, title='题', function_name='f', hidden_tests='')
            db.add(question); db.flush()
            db.add(Submission(question_id=question.id, student_id=school['users'][who].id, code='pass', score=80, status='completed', grading_status='completed'))
        db.commit()
    response = client.get('/api/v1/submissions/unified', headers=school['headers']['owner'])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['total'] == 1
    assert all(r['student_name'] == 'school-a' for r in body['items'])
    response = client.get('/api/v1/search', params={'q': 'school-'}, headers=school['headers']['owner'])
    assert response.status_code == 200, response.text
    assert 'school-b' not in response.text


def test_archived_instance_is_read_only(client, school):
    offering = school['offerings'][0]
    response = client.patch(f'/api/v1/offerings/{offering}', json={'status': 'archived', 'reason': '归档'}, headers=school['headers']['admin'])
    assert response.status_code == 200
    assert client.get(f'/api/v1/offerings/{offering}/students', headers=school['headers']['owner']).status_code == 200
    response = client.post(f'/api/v1/offerings/{offering}/tasks/copy', json={'kind': 'assignment', 'source_id': 999}, headers=school['headers']['owner'])
    assert response.status_code == 409
