from app.models import Assignment, JudgeQuestion, Submission
from test_school_offerings import school  # noqa: F401


def test_legacy_list_and_count_follow_offering_teacher_not_content_owner(client, school, db_session_factory):
    ids = []
    with db_session_factory() as db:
        for index, student in enumerate(['a', 'b']):
            task = Assignment(course_id=school['course'], offering_id=school['offerings'][index], title='Scoped', status='published')
            db.add(task); db.flush()
            question = JudgeQuestion(assignment_id=task.id, title='Question', function_name='f', hidden_tests='')
            db.add(question); db.flush()
            submission = Submission(question_id=question.id, student_id=school['users'][student].id,
                                    code='pass', status='accepted', grading_status='completed', score=100)
            db.add(submission); db.flush()
            ids.append(submission.id)
        db.commit()
    for index, teacher in enumerate(['owner', 'other']):
        headers = school['headers'][teacher]
        response = client.get('/api/v1/judge/submissions', params={'page_size': 1}, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['total'] == 1
        assert [row['id'] for row in body['items']] == [ids[index]]
        assert client.get('/api/v1/judge/submissions', params={'page_size': 1, 'page': 2}, headers=headers).json()['items'] == []
        unified = client.get('/api/v1/submissions/unified', params={'kind': 'assignment'}, headers=headers)
        assert unified.status_code == 200, unified.text
        assert unified.json()['total'] == body['total']
        assert client.get(f'/api/v1/judge/submissions/{ids[index]}', headers=headers).status_code == 200
        assert client.get(f'/api/v1/judge/submissions/{ids[1-index]}', headers=headers).status_code in (403, 404)
