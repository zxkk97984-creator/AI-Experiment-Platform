"""批次 4：四类成绩模板、任务自动关联与总评汇总（原始需求 Q7）。

覆盖：四类 20/30/20/30 手算总评 / 课程模板复制与不回溯 / 三类任务发布自动建列 /
重复发布不新增列 / exam_kind=other 未选分类被拒 / 自定义比例下新列待分配与发布门禁 /
旧快照 payload_hash 不变 / 成绩册无 practice 来源。
"""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (AcademicTerm, Assignment, Course, ExperimentModule, JudgeQuestion,
                        NotebookTemplate, NotebookTemplateVersion, Submission, TeachingClass, User)
from app.models.gradebook import Gradebook, GradebookCategory, GradebookColumn
from app.models.teaching import CourseOffering
from app.services.offering_service import enroll_student
from app.services import gradebook_service as grades
from app.services.time_utils import utc_now
from conftest import auth_header, create_user, login, make_template_version

API = '/api/v1/gradebooks'
COLUMN_KEYS = ('regular-main', 'labs-main', 'midterm-main', 'final-main')

# 五名学生四类原始分（平时 / 实验 / 期中 / 期末），总分按 20/30/20/30 手算。
SCORES = {
    '20260001': (100, 100, 100, 100),
    '20260002': (80, 90, 70, 60),
    '20260003': (0, 100, 50, 0),
    '20260004': (60, 0, 0, 100),
    '20260005': (33.33, 66.67, 12.5, 87.5),
}
# 各一级栏目加权分 = 原始分 * 栏目占比（20/30/20/30）。
EXPECTED_CATEGORY_POINTS = {
    '20260001': ['20.00', '30.00', '20.00', '30.00'],
    '20260002': ['16.00', '27.00', '14.00', '18.00'],
    '20260003': ['0.00', '30.00', '10.00', '0.00'],
    '20260004': ['12.00', '0.00', '0.00', '30.00'],
    '20260005': ['6.67', '20.00', '2.50', '26.25'],
}
EXPECTED_TOTAL = {'20260001': '100.00', '20260002': '75.00', '20260003': '40.00',
                  '20260004': '42.00', '20260005': '55.42'}


@pytest.fixture()
def book(app, client, db_session_factory):
    from app.api import gradebook as api

    if not any(getattr(route, 'path', None) == f'{API}/offerings' for route in app.routes):
        app.include_router(api.router, prefix='/api/v1')
    teacher = create_user(db_session_factory, 'gb4-teacher', 'teacher')
    students = [create_user(db_session_factory, f'gb4-student-{index}', 'student', real_name=f'学生{index}')
                for index in range(1, 6)]
    with db_session_factory() as db:
        for index, student in enumerate(students, start=1):
            db.get(User, student.id).student_no = f'2026000{index}'
        term = AcademicTerm(code='GB4-TERM', name='学期', start_date=date(2026, 9, 1),
                            end_date=date(2027, 1, 1), status='active')
        db.add(term); db.flush()
        course = Course(title='四类模板课程', teacher_id=teacher.id, status='published')
        db.add(course); db.flush()
        klass = TeachingClass(academic_term_id=term.id, code='GB4-1', name='班级一', status='active')
        db.add(klass); db.flush()
        offering = CourseOffering(course_id=course.id, academic_term_id=term.id, teaching_class_id=klass.id,
                                  teacher_id=teacher.id, status='active')
        db.add(offering); db.flush()
        db.flush()
        for student in students:
            enroll_student(db, offering, student.id)
        db.commit()
        values = {'offering': offering.id, 'course': course.id, 'term': term.id, 'klass': klass.id,
                  'student_nos': [f'2026000{index}' for index in range(1, 6)]}
    return SimpleNamespace(teacher=teacher, students=students,
                           headers=auth_header(login(client, teacher.username)[0]), **values)


def read(client, book):
    response = client.get(f'{API}/offerings/{book.offering}', headers=book.headers)
    assert response.status_code == 200, response.text
    return response.json()


def save(client, book, config, revision):
    response = client.put(f'{API}/offerings/{book.offering}/config', headers=book.headers,
                          json={'expected_revision': revision, 'categories': config})
    assert response.status_code == 200, response.text
    return response.json()


def publish(client, book, revision):
    draft = read(client, book)['draft']
    return client.post(f'{API}/offerings/{book.offering}/publish', headers=book.headers,
                       json={'expected_revision': revision, 'expected_preview_hash': draft['preview_hash'],
                             'reason': '期末确认'})


def enter(client, book, values, revision):
    entries = [{'student_id': student_id, 'column_key': key, 'state': 'scored', 'score': str(score)}
               for (student_id, key), score in values.items()]
    response = client.patch(f'{API}/offerings/{book.offering}/entries', headers=book.headers,
                            json={'expected_revision': revision, 'entries': entries})
    assert response.status_code == 200, response.text
    return response.json()


def four_category_book(client, book):
    saved = save(client, book, grades.default_config(), 0)
    values = {(student.id, key): score for student, student_no in zip(book.students, book.student_nos)
              for key, score in zip(COLUMN_KEYS, SCORES[student_no])}
    return enter(client, book, values, saved['revision'])


def task_column(config, source_kind, source_id):
    for category in config:
        for column in category['columns']:
            if column['source_kind'] == source_kind and column['source_id'] == source_id:
                return category, column
    return None, None


def create_exam(client, book, *, exam_kind, title):
    now = utc_now()
    response = client.post('/api/v1/exams', headers=book.headers, json={
        'course_id': book.course, 'offering_id': book.offering, 'title': title,
        'duration_minutes': 60, 'exam_kind': exam_kind,
        'start_at': (now - timedelta(minutes=5)).isoformat(),
        'end_at': (now + timedelta(minutes=90)).isoformat()})
    assert response.status_code == 201, response.text
    exam_id = response.json()['id']
    question = client.post(f'/api/v1/exams/{exam_id}/questions', headers=book.headers, json={
        'question_type': 'single_choice', 'prompt': '2 + 2 = ?', 'options': {'A': '4', 'B': '5'},
        'correct_answer': {'correct': ['A']}, 'points': 10})
    assert question.status_code == 201, question.text
    return exam_id


def make_module(factory, teacher_id, name='批次四实验'):
    version_id = make_template_version(factory)
    with factory() as db:
        version = db.get(NotebookTemplateVersion, version_id)
        template = db.get(NotebookTemplate, version.template_id)
        template.status, template.current_version_id = 'published', version_id
        module = ExperimentModule(name=name, status='published', owner_id=teacher_id,
                                  template_id=version.template_id)
        db.add(module); db.commit()
        return module.id


def release_experiment(client, factory, book, *, category_key='labs'):
    module_id = make_module(factory, book.teacher.id)
    response = client.post(f'/api/v1/offerings/{book.offering}/experiments', headers=book.headers,
                           json={'entry_kind': 'module', 'entry_id': module_id, 'status': 'published',
                                 'gradebook_binding': {'enabled': True, 'category_key': category_key}})
    assert response.status_code == 200, response.text
    return response.json()['id'], module_id


def test_four_category_totals_match_hand_checked_expectations(client, book):
    edited = four_category_book(client, book)
    assert [(row['key'], Decimal(row['weight']), row['weight_mode']) for row in edited['config']] == \
           [('regular', Decimal(20), 'equal'), ('labs', Decimal(30), 'equal'),
            ('midterm', Decimal(20), 'equal'), ('final', Decimal(30), 'equal')]
    rows = {row['student_no']: row for row in edited['draft']['rows']}
    assert sorted(rows) == sorted(SCORES)
    for student_no, row in rows.items():
        assert [row['cells'][key]['score'] for key in COLUMN_KEYS] == \
               [f'{value:.2f}' for value in SCORES[student_no]]
        assert [category['weighted_points'] for category in row['categories']] == \
               EXPECTED_CATEGORY_POINTS[student_no]
        assert row['calculated_score'] == EXPECTED_TOTAL[student_no]
        assert row['final_score'] == EXPECTED_TOTAL[student_no]
        for category in row['categories']:
            assert category['effective_weights'] == {f"{category['key']}-main": '100.0000'}
    assert edited['draft']['blockers'] == []
    published = publish(client, book, edited['revision'])
    assert published.status_code == 201, published.text
    assert {row['student_no']: row['final_score'] for row in published.json()['payload']['rows']} == EXPECTED_TOTAL


def test_course_template_seeds_new_classes_and_never_rewrites_existing_ones(client, db_session_factory, book):
    template = [{'key': 'regular', 'title': '平时', 'weight': '50', 'weight_mode': 'equal'},
                {'key': 'labs', 'title': '实验', 'weight': '30', 'weight_mode': 'equal'},
                {'key': 'final', 'title': '期末', 'weight': '20', 'weight_mode': 'equal'}]
    response = client.put(f'{API}/courses/{book.course}/default-config', headers=book.headers,
                          json={'categories': template})
    assert response.status_code == 200, response.text
    assert response.json()['categories'] == template
    with db_session_factory() as db:
        stored = db.get(Course, book.course).default_gradebook_config
    # 模板只存分类与规则：不存任务 ID、不存任务分项。
    assert stored == template
    assert all('columns' not in item and 'source_id' not in item for item in stored)
    # 首次建册（发布任务触发）复制课程模板：分类键与比例来自模板而不是内置四类。
    release_id, _ = release_experiment(client, db_session_factory, book)
    seeded = read(client, book)
    assert [(row['key'], Decimal(row['weight'])) for row in seeded['config']] == \
           [('regular', Decimal(50)), ('labs', Decimal(30)), ('final', Decimal(20))]
    category, column = task_column(seeded['config'], 'experiment', release_id)
    assert category['key'] == 'labs' and column['allocation_status'] == 'allocated'
    # 课程模板后续修改不回溯已开班。
    client.put(f'{API}/courses/{book.course}/default-config', headers=book.headers,
               json={'categories': [{'key': 'regular', 'title': '平时', 'weight': '60', 'weight_mode': 'custom'},
                                    {'key': 'final', 'title': '期末', 'weight': '40', 'weight_mode': 'equal'}]})
    assert [(row['key'], Decimal(row['weight'])) for row in read(client, book)['config']] == \
           [('regular', Decimal(50)), ('labs', Decimal(30)), ('final', Decimal(20))]
    # 同课程新开的班按最新模板建册。
    with db_session_factory() as db:
        klass = TeachingClass(academic_term_id=book.term, code='GB4-2', name='班级二', status='active')
        db.add(klass); db.flush()
        second = CourseOffering(course_id=book.course, academic_term_id=book.term,
                                teaching_class_id=klass.id, teacher_id=book.teacher.id, status='active')
        db.add(second); db.commit(); second_id = second.id
    fresh = SimpleNamespace(offering=second_id, headers=book.headers)
    assert [(row['key'], Decimal(row['weight']), row['weight_mode']) for row in read(client, fresh)['config']] == \
           [('regular', Decimal(60), 'custom'), ('final', Decimal(40), 'equal')]
    assert read(client, fresh)['revision'] == 0  # 读取不建册；模板在首次写入时复制


def test_restore_course_default_endpoint_rebuilds_class_categories(client, book):
    template = [{'key': 'regular', 'title': '平时', 'weight': '60', 'weight_mode': 'equal'},
                {'key': 'final', 'title': '期末', 'weight': '40', 'weight_mode': 'equal'}]
    client.put(f'{API}/courses/{book.course}/default-config', headers=book.headers, json={'categories': template})
    saved = save(client, book, [{'key': 'main', 'title': '临时栏目', 'weight': '100', 'weight_mode': 'equal',
                                 'columns': [{'key': 'main', 'title': '临时', 'weight': None,
                                              'source_kind': 'manual'}]}], 0)
    response = client.post(f'{API}/offerings/{book.offering}/config/restore-default', headers=book.headers,
                           json={'expected_revision': saved['revision'], 'reason': '改用课程模板重新配置'})
    assert response.status_code == 200, response.text
    assert [(row['key'], Decimal(row['weight'])) for row in response.json()['config']] == \
           [('regular', Decimal(60)), ('final', Decimal(40))]
    stale = client.post(f'{API}/offerings/{book.offering}/config/restore-default', headers=book.headers,
                        json={'expected_revision': saved['revision'], 'reason': '重复恢复'})
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'GRADEBOOK_REVISION_CONFLICT'


def test_publishing_three_task_kinds_binds_one_column_each_and_repeat_is_idempotent(
        client, db_session_factory, book):
    with db_session_factory() as db:
        assignment = Assignment(course_id=book.course, offering_id=book.offering, title='第一次作业',
                                status='draft')
        db.add(assignment); db.flush()
        db.add(JudgeQuestion(assignment_id=assignment.id, title='作业题', function_name='f',
                             grading_mode='legacy', hidden_tests='def test(): pass'))
        assignment_id = assignment.id
        db.commit()
    first = client.post(f'/api/v1/assignments/{assignment_id}/publish', headers=book.headers,
                        json={'gradebook_binding': {'enabled': True, 'category_key': 'regular'}})
    assert first.status_code == 200, first.text
    category, column = task_column(read(client, book)['config'], 'assignment', assignment_id)
    assert category['key'] == 'regular' and column['key'] == f'assignment-{assignment_id}'
    assert column['weight'] is None and column['allocation_status'] == 'allocated'
    # 取消发布后再发布：同一任务不产生第二列。
    assert client.post(f'/api/v1/assignments/{assignment_id}/unpublish', headers=book.headers).status_code == 200
    assert client.post(f'/api/v1/assignments/{assignment_id}/publish',
                       headers=book.headers).status_code == 200
    with db_session_factory() as db:
        bound = db.scalars(select(GradebookColumn).where(GradebookColumn.source_kind == 'assignment',
                                                         GradebookColumn.source_id == assignment_id)).all()
    assert len(bound) == 1

    # 考试按 exam_kind 归类：midterm→期中。
    exam_id = create_exam(client, book, exam_kind='midterm', title='期中考试')
    published = client.patch(f'/api/v1/exams/{exam_id}', headers=book.headers,
                             json={'status': 'published', 'gradebook_binding': {'enabled': True}})
    assert published.status_code == 200, published.text
    category, column = task_column(read(client, book)['config'], 'exam', exam_id)
    assert category['key'] == 'midterm' and column['source_id'] == exam_id
    # 已发布考试再次提交（含别的分类）不改动既有分项：重复发布不新增列、不换分类。
    again = client.patch(f'/api/v1/exams/{exam_id}', headers=book.headers,
                         json={'gradebook_binding': {'enabled': True, 'category_key': 'final'}})
    assert again.status_code == 200, again.text
    with db_session_factory() as db:
        rows = db.scalars(select(GradebookColumn).where(GradebookColumn.source_kind == 'exam',
                                                        GradebookColumn.source_id == exam_id)).all()
        assert len(rows) == 1 and db.get(GradebookCategory, rows[0].category_id).key == 'midterm'

    # 班级级发布实验→实验分类，source_id 是班级发布记录；重复发布不新增列。
    release_id, module_id = release_experiment(client, db_session_factory, book)
    category, column = task_column(read(client, book)['config'], 'experiment', release_id)
    assert category['key'] == 'labs' and column['source_id'] == release_id
    repeat = client.post(f'/api/v1/offerings/{book.offering}/experiments', headers=book.headers,
                         json={'entry_kind': 'module', 'entry_id': module_id})
    assert repeat.status_code == 200, repeat.text
    with db_session_factory() as db:
        rows = db.scalars(select(GradebookColumn).where(GradebookColumn.source_kind == 'experiment')).all()
        assert [row.source_id for row in rows] == [release_id]


def test_other_exam_without_category_is_rejected_then_binds_with_category(client, book):
    exam_id = create_exam(client, book, exam_kind='other', title='随堂测验')
    rejected = client.patch(f'/api/v1/exams/{exam_id}', headers=book.headers,
                            json={'status': 'published', 'gradebook_binding': {'enabled': True}})
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()['detail']['code'] == 'GRADEBOOK_CATEGORY_REQUIRED'
    # 对照组：被拒时考试仍是草稿，成绩册也没有半成品分项。
    assert client.get(f'/api/v1/exams/{exam_id}', headers=book.headers).json()['status'] == 'draft'
    assert all(column['source_kind'] != 'exam'
               for row in read(client, book)['config'] for column in row['columns'])
    accepted = client.patch(f'/api/v1/exams/{exam_id}', headers=book.headers,
                            json={'status': 'published',
                                  'gradebook_binding': {'enabled': True, 'category_key': 'final'}})
    assert accepted.status_code == 200, accepted.text
    category, column = task_column(read(client, book)['config'], 'exam', exam_id)
    assert category['key'] == 'final' and column['source_id'] == exam_id


def test_custom_weights_keep_old_ratios_and_gate_publish_until_allocated(client, db_session_factory, book):
    with db_session_factory() as db:
        assignment = Assignment(course_id=book.course, offering_id=book.offering, title='自定义比例作业',
                                status='draft')
        db.add(assignment); db.flush()
        question = JudgeQuestion(assignment_id=assignment.id, title='作业题', function_name='f',
                                 grading_mode='legacy', hidden_tests='def test(): pass')
        db.add(question); db.flush()
        db.add_all([Submission(question_id=question.id, student_id=student.id, code='', status='accepted',
                               grading_status='completed', score=80) for student in book.students])
        assignment_id = assignment.id
        db.commit()
    configured = save(client, book, [{'key': 'regular', 'title': '平时', 'weight': '100', 'weight_mode': 'custom',
                                      'columns': [
                                          {'key': 'manual-a', 'title': '人工一', 'weight': '60',
                                           'source_kind': 'manual'},
                                          {'key': 'manual-b', 'title': '人工二', 'weight': '40',
                                           'source_kind': 'manual'}]}], 0)
    assert [Decimal(column['weight']) for column in configured['config'][0]['columns']] == [Decimal(60), Decimal(40)]
    edited = enter(client, book, {(student.id, key): 90 for student in book.students
                                  for key in ('manual-a', 'manual-b')}, configured['revision'])
    assert edited['draft']['rows'][0]['final_score'] == '90.00'
    assert publish(client, book, edited['revision']).status_code == 201
    first_snapshot = read(client, book)['snapshot']

    bound = client.post(f'/api/v1/assignments/{assignment_id}/publish', headers=book.headers,
                        json={'gradebook_binding': {'enabled': True, 'category_key': 'regular'}})
    assert bound.status_code == 200, bound.text
    category, column = task_column(read(client, book)['config'], 'assignment', assignment_id)
    assert category['key'] == 'regular' and category['weight_mode'] == 'custom'
    # 原有分项比例逐个不变；新分项待分配、不带比例。
    assert [(row['key'], None if row['weight'] is None else Decimal(row['weight'])) for row in category['columns']] == \
           [('manual-a', Decimal(60)), ('manual-b', Decimal(40)), (f'assignment-{assignment_id}', None)]
    assert column['allocation_status'] == 'pending'
    draft = read(client, book)['draft']
    assert [item['code'] for item in draft['blockers']] == ['UNALLOCATED_WEIGHTS']
    # 待分配列不参与计算：原比例未被静默改动，草稿总分仍是 90.00。
    assert draft['rows'][0]['final_score'] == '90.00'
    blocked = publish(client, book, read(client, book)['revision'])
    assert blocked.status_code == 409 and blocked.json()['detail']['code'] == 'GRADEBOOK_INCOMPLETE'
    assert blocked.json()['detail']['fields']['blockers'][0]['code'] == 'UNALLOCATED_WEIGHTS'
    # 教师确认分配后按新比例计算并可以发布。
    allocated = save(client, book, [{'key': 'regular', 'title': '平时', 'weight': '100', 'weight_mode': 'custom',
                                     'columns': [
                                         {'key': 'manual-a', 'title': '人工一', 'weight': '40',
                                          'source_kind': 'manual'},
                                         {'key': 'manual-b', 'title': '人工二', 'weight': '30',
                                          'source_kind': 'manual'},
                                         {'key': f'assignment-{assignment_id}', 'title': '自定义比例作业',
                                          'weight': '30', 'source_kind': 'assignment',
                                          'source_id': assignment_id}]}], read(client, book)['revision'])
    assert [Decimal(row['weight']) for row in allocated['config'][0]['columns']] == \
           [Decimal(40), Decimal(30), Decimal(30)]
    assert allocated['draft']['blockers'] == []
    assert allocated['draft']['rows'][0]['final_score'] == '87.00'  # 90*0.4 + 90*0.3 + 80*0.3
    assert publish(client, book, allocated['revision']).status_code == 201

    # Switching the same existing columns to equal mode clears stale custom
    # ratios and re-derives every allocation state, including old rows.
    equal = save(client, book, [{'key': 'regular', 'title': '平时', 'weight': '100', 'weight_mode': 'equal',
                                 'columns': [
                                     {'key': 'manual-a', 'title': '人工一', 'weight': 40,
                                      'source_kind': 'manual'},
                                     {'key': 'manual-b', 'title': '人工二', 'weight': 30,
                                      'source_kind': 'manual'},
                                     {'key': f'assignment-{assignment_id}', 'title': '自定义比例作业',
                                      'weight': 30, 'source_kind': 'assignment',
                                      'source_id': assignment_id}]}], read(client, book)['revision'])
    assert all(row['weight'] is None and row['allocation_status'] == 'allocated'
               for row in equal['config'][0]['columns'])
    assert not any(item['code'] == 'UNALLOCATED_WEIGHTS' for item in equal['draft']['blockers'])
    restored_custom = save(client, book, [{'key': 'regular', 'title': '平时', 'weight': '100', 'weight_mode': 'custom',
                                          'columns': [
                                              {'key': 'manual-a', 'title': '人工一', 'source_kind': 'manual'},
                                              {'key': 'manual-b', 'title': '人工二', 'source_kind': 'manual'},
                                              {'key': f'assignment-{assignment_id}', 'title': '自定义比例作业',
                                               'source_kind': 'assignment', 'source_id': assignment_id}]}],
                                equal['revision'])
    assert all(row['weight'] is None and row['allocation_status'] == 'pending'
               for row in restored_custom['config'][0]['columns'])
    assert any(item['code'] == 'UNALLOCATED_WEIGHTS' for item in restored_custom['draft']['blockers'])
    # 已发布快照不被改写。
    original = client.get(f"{API}/snapshots/{first_snapshot['id']}", headers=book.headers).json()
    assert original['payload_hash'] == first_snapshot['payload_hash']
    assert original['payload']['rows'][0]['final_score'] == '90.00'


def test_equal_category_splits_columns_evenly_and_snapshot_hash_is_immutable(client, book):
    configured = save(client, book, [{'key': 'labs', 'title': '实验', 'weight': '100', 'weight_mode': 'equal',
                                      'columns': [
                                          {'key': 'a', 'title': '实验一', 'weight': None, 'source_kind': 'manual'},
                                          {'key': 'b', 'title': '实验二', 'weight': None, 'source_kind': 'manual'},
                                          {'key': 'c', 'title': '实验三', 'weight': None, 'source_kind': 'manual'}]}], 0)
    values = {(student.id, key): score for student in book.students
              for key, score in zip(('a', 'b', 'c'), (90, 60, 30))}
    edited = enter(client, book, values, configured['revision'])
    row = edited['draft']['rows'][0]
    assert row['categories'][0]['score'] == '60.00'
    assert row['categories'][0]['effective_weights'] == {'a': '33.3333', 'b': '33.3333', 'c': '33.3333'}
    assert row['final_score'] == '60.00'
    first = publish(client, book, edited['revision'])
    assert first.status_code == 201, first.text
    snapshot = first.json()
    save(client, book, [{'key': 'labs', 'title': '实验', 'weight': '100', 'weight_mode': 'custom',
                         'columns': [{'key': 'a', 'title': '实验一', 'weight': '100',
                                      'source_kind': 'manual'}]}], read(client, book)['revision'])
    unchanged = client.get(f"{API}/snapshots/{snapshot['id']}", headers=book.headers).json()
    assert unchanged['payload_hash'] == snapshot['payload_hash']
    assert unchanged['payload']['rows'][0]['final_score'] == '60.00'


def test_swapping_two_column_sources_is_not_blocked_by_the_unique_constraint(client, book):
    first = create_exam(client, book, exam_kind='quiz', title='测验一')
    second = create_exam(client, book, exam_kind='quiz', title='测验二')
    for exam_id in (first, second):
        published = client.patch(f'/api/v1/exams/{exam_id}', headers=book.headers,
                                 json={'status': 'published', 'gradebook_binding': {'enabled': False}})
        assert published.status_code == 200, published.text
    config = [{'key': 'regular', 'title': '平时', 'weight': '100', 'weight_mode': 'equal', 'columns': [
        {'key': 'one', 'title': '测验一', 'weight': None, 'source_kind': 'exam', 'source_id': first},
        {'key': 'two', 'title': '测验二', 'weight': None, 'source_kind': 'exam', 'source_id': second}]}]
    saved = save(client, book, config, 0)
    assert [(row['key'], row['source_id']) for row in saved['config'][0]['columns']] == \
           [('one', first), ('two', second)]
    swapped = deepcopy(config)
    swapped[0]['columns'][0]['source_id'], swapped[0]['columns'][1]['source_id'] = second, first
    updated = save(client, book, swapped, saved['revision'])
    assert [(row['key'], row['source_id']) for row in updated['config'][0]['columns']] == \
           [('one', second), ('two', first)]


def test_migration_matches_model_constraints_and_revision_chain():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / 'alembic' / 'versions' / '20261013_0004_gradebook_templates.py'
    spec = importlib.util.spec_from_file_location('gb4_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == '20261013_0004' and module.down_revision == '20261013_0003'
    source = path.read_text()
    named = {'uq_gradebook_column_source', 'ck_gradebook_column_allocation',
             'ck_gradebook_category_weight_mode', 'fk_gradebook_columns_bound_by_id'}
    for name in named:
        assert source.count(name) >= 2, name  # upgrade 建立 + downgrade 删除，两边同名
    model_constraints = {item.name for item in GradebookColumn.__table__.constraints} | \
                        {item.name for item in GradebookCategory.__table__.constraints}
    assert named - {'fk_gradebook_columns_bound_by_id'} <= model_constraints
    assert {column.name for column in GradebookColumn.__table__.columns} >= \
           {'auto_bound', 'bound_at', 'bound_by_id', 'allocation_status'}
    assert any(fk.name == 'fk_gradebook_columns_bound_by_id'
               for fk in GradebookColumn.__table__.foreign_key_constraints)
    assert GradebookColumn.weight.nullable is True and GradebookCategory.weight_mode.nullable is False


def test_gradebook_source_kinds_exclude_practice():
    from app.schemas.gradebook import ColumnConfig

    constraint = next(item for item in GradebookColumn.__table__.constraints
                      if item.name == 'ck_gradebook_column_source')
    assert 'practice' not in str(constraint.sqltext)
    assert set(grades.TASK_CATEGORY_BY_KIND) | set(grades.EXAM_CATEGORY_BY_KIND) <= \
           {'assignment', 'experiment', 'quiz', 'midterm', 'final'}
    with pytest.raises(Exception):
        ColumnConfig(key='practice', title='练习', weight=None, source_kind='practice', source_id=1)
    unique = next(item for item in GradebookColumn.__table__.constraints
                  if item.name == 'uq_gradebook_column_source')
    assert [column.name for column in unique.columns] == ['gradebook_id', 'source_kind', 'source_id']


def _column(gradebook_id, category_id, key, source_kind, source_id):
    return GradebookColumn(gradebook_id=gradebook_id, category_id=category_id, key=key, title=key,
                           weight=None, source_kind=source_kind, source_id=source_id)


def _fresh_book(db, offering_id):
    row = Gradebook(offering_id=offering_id, revision=1)
    db.add(row); db.flush()
    category = GradebookCategory(gradebook_id=row.id, key='regular', title='平时', weight=100)
    db.add(category); db.flush()
    return row, category


def test_duplicate_auto_binding_is_blocked_by_unique_constraint(db_session_factory, book):
    with db_session_factory() as db:
        row, category = _fresh_book(db, book.offering)
        db.add_all([_column(row.id, category.id, 'dup-one', 'assignment', 999),
                    _column(row.id, category.id, 'dup-two', 'assignment', 999)])
        with pytest.raises(IntegrityError):
            db.commit()
    # 对照组：manual 分项 source_id 为 NULL，MySQL 与 SQLite 都允许重复；且单一来源合法。
    with db_session_factory() as db:
        row, category = _fresh_book(db, book.offering)
        db.add_all([_column(row.id, category.id, 'manual-one', 'manual', None),
                    _column(row.id, category.id, 'manual-two', 'manual', None),
                    _column(row.id, category.id, 'auto-one', 'exam', 7)])
        db.commit()
        assert len(db.scalars(select(GradebookColumn).where(
            GradebookColumn.gradebook_id == row.id)).all()) == 3
