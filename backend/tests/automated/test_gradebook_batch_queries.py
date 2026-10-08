import time

import pytest
from sqlalchemy import event

from app.models import Assignment, JudgeQuestion, Submission, User
from app.models.gradebook import Gradebook, GradebookCategory, GradebookColumn
from app.models.teaching import OfferingEnrollment
from app.services import gradebook_service as service
from test_school_write_scope import make_scope_case


@pytest.mark.parametrize('size', [30, 100, 200])
def test_gradebook_queries_are_bounded_and_match_uncached_results(db_session_factory, monkeypatch, size):
    case = make_scope_case(db_session_factory)
    with db_session_factory() as db:
        students = [db.get(User, case.student)]
        students += [User(username=f'batch-{i}', real_name=f'Student {i}', role='student',
                          status='active', password_hash='fixture') for i in range(size - 1)]
        db.add_all(students)
        db.flush()
        db.add_all([OfferingEnrollment(offering_id=case.offering, student_id=s.id, status='active')
                    for s in students[1:]])
        assignment = Assignment(course_id=case.course, offering_id=case.offering,
                                title='Batch source', status='published')
        book = Gradebook(offering_id=case.offering)
        db.add_all([assignment, book]); db.flush()
        category = GradebookCategory(gradebook_id=book.id, key='regular', title='Regular', weight=100)
        questions = [JudgeQuestion(assignment_id=assignment.id, title=f'Question {i}',
                                   function_name='f', hidden_tests='') for i in range(2)]
        db.add_all([category, *questions]); db.flush()
        db.add(GradebookColumn(gradebook_id=book.id, category_id=category.id, key='assignment',
                              title='Assignment', weight=100, source_kind='assignment',
                              source_id=assignment.id, selection='highest'))
        db.add_all([Submission(question_id=q.id, student_id=s.id, code='large code is not needed',
                               score=score, status='accepted', grading_status='completed')
                    for s in students for q in questions for score in (60, 80)])
        db.commit()
        book_id = book.id
    counts = []
    engine = db_session_factory.kw['bind']
    def count(*args):
        if args[2].lstrip().upper().startswith('SELECT'):
            counts.append(args[2])
    event.listen(engine, 'before_cursor_execute', count)
    try:
        with db_session_factory() as db:
            book = db.get(Gradebook, book_id)
            started = time.perf_counter()
            actual = service.calculate(db, book)
            elapsed = time.perf_counter() - started
        query_count = len(counts)
        assert query_count <= 30, query_count
        assert {r['final_score'] for r in actual['rows']} == {'80.00'}
        assert len(actual['rows']) == size
        counts.clear()
        with monkeypatch.context() as patch:
            patch.setattr(service, 'preload_sources', lambda *args, **kwargs: None)
            with db_session_factory() as db:
                expected = service.calculate(db, db.get(Gradebook, book_id))
        assert actual == expected  # Includes evidence ordering and preview_hash.
        assert len(counts) > query_count * 5
        print(f'{size} students: batch={query_count}, legacy={len(counts)}, batch_seconds={elapsed:.4f}')
    finally:
        event.remove(engine, 'before_cursor_execute', count)
