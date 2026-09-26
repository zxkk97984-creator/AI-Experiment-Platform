"""Read-only student-owned submission projections, independent of current enrollment."""
from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, literal, select, union_all
from sqlalchemy.orm import Session

from app.dependencies import get_db, pagination, PaginationParams, require_roles
from app.errors import api_error
from app.models import (Assignment, Chapter, Course, Exam, ExamAnswer, ExamSubmission, ExperimentModule,
                        ExperimentRecord, ExperimentSubmission, JudgeQuestion, Lesson, Submission, User)

router = APIRouter(prefix='/users/me/history', tags=['student-history'])


def history_query(student_id):
    assignment = select(literal('assignment').label('kind'), Submission.id.label('submission_id'),
        JudgeQuestion.title.label('entry_title'), Course.id.label('course_id'), Course.title.label('course_title'),
        Submission.created_at.label('submitted_at'), Submission.grading_status.label('status'),
        case((Submission.grading_status == 'completed', Submission.score), else_=None).label('score')
    ).join(JudgeQuestion, JudgeQuestion.id == Submission.question_id).join(Assignment, Assignment.id == JudgeQuestion.assignment_id
    ).join(Course, Course.id == Assignment.course_id).where(Submission.student_id == student_id)
    exam = select(literal('exam'), ExamSubmission.id, Exam.title, Course.id, Course.title,
        ExamSubmission.submitted_at, ExamSubmission.status,
        case(((ExamSubmission.status == 'graded') & Exam.show_score_after_grading, ExamSubmission.score), else_=None)
    ).join(Exam, Exam.id == ExamSubmission.exam_id).join(Course, Course.id == Exam.course_id
    ).where(ExamSubmission.student_id == student_id, ExamSubmission.status != 'started')
    experiment = select(literal('experiment'), ExperimentSubmission.id,
        func.coalesce(Lesson.title, ExperimentModule.name), Course.id, Course.title, ExperimentSubmission.submitted_at,
        case((ExperimentSubmission.reviewed_at.is_not(None), 'graded'), else_='submitted'),
        case((ExperimentSubmission.reviewed_at.is_not(None), ExperimentSubmission.score), else_=None)
    ).join(ExperimentRecord, ExperimentRecord.id == ExperimentSubmission.record_id
    ).outerjoin(Lesson, Lesson.id == ExperimentRecord.lesson_id).outerjoin(Chapter, Chapter.id == Lesson.chapter_id
    ).outerjoin(Course, Course.id == Chapter.course_id).outerjoin(ExperimentModule, ExperimentModule.id == ExperimentRecord.module_id
    ).where(ExperimentRecord.student_id == student_id)
    return union_all(assignment, exam, experiment).subquery()


@router.get('')
def list_history(kind: Literal['all', 'assignment', 'exam', 'experiment'] = 'all', course_id: int | None = None,
                 page: PaginationParams = Depends(pagination), db: Session = Depends(get_db),
                 user: User = Depends(require_roles('student'))):
    history = history_query(user.id)
    filters = []
    if kind != 'all':
        filters.append(history.c.kind == kind)
    if course_id is not None:
        filters.append(history.c.course_id == course_id)
    total = db.scalar(select(func.count()).select_from(history).where(*filters))
    rows = db.execute(select(history).where(*filters).order_by(history.c.submitted_at.desc(), history.c.kind,
                       history.c.submission_id.desc()).offset((page.page - 1) * page.page_size).limit(page.page_size)).mappings().all()
    options = db.execute(select(history.c.course_id, history.c.course_title).where(history.c.course_id.is_not(None)).distinct()).all()
    return {'items': [{**r, 'score_visible': r['score'] is not None} for r in rows], 'total': total,
            'page': page.page, 'page_size': page.page_size,
            'courses': [{'id': r[0], 'name': r[1]} for r in options]}


@router.get('/{kind}/{submission_id}')
def history_detail(kind: Literal['assignment', 'exam', 'experiment'], submission_id: int,
                   db: Session = Depends(get_db), user: User = Depends(require_roles('student'))):
    history = history_query(user.id)
    row = db.execute(select(history).where(history.c.kind == kind, history.c.submission_id == submission_id)).mappings().first()
    if row is None:
        raise api_error(404, 'SUBMISSION_NOT_FOUND', '提交记录不存在')
    result = {**row, 'score_visible': row['score'] is not None}
    if kind == 'assignment':
        submission = db.get(Submission, submission_id)
        result.update(code=submission.code, tests_passed=submission.tests_passed if row['score'] is not None else None,
                      tests_total=submission.tests_total if row['score'] is not None else None)
        # Do not expose raw judge stdout, stderr or hidden-test details.
    elif kind == 'exam':
        answers = db.scalars(select(ExamAnswer).where(ExamAnswer.submission_id == submission_id).order_by(ExamAnswer.id)).all()
        result['answers'] = [{'question_id': a.question_id, 'code': a.code_answer, 'selected_options': a.selected_options,
                              'text_answers': a.text_answers, 'score': a.score if row['score'] is not None else None} for a in answers]
    else:
        submission = db.get(ExperimentSubmission, submission_id)
        record = submission.record
        snapshot = submission.cells_snapshot or {}
        if isinstance(snapshot.get('cells'), dict):
            snapshot = snapshot['cells']
        version = record.template_version
        cells = sorted((c for c in (version.cells if version else []) if not c.get('source_hidden')), key=lambda c:c.get('order', 0))
        result['cells'] = [{'id': c['id'], 'type': c.get('type', 'code'), 'source': snapshot[c['id']],
                            'output': (submission.outputs_snapshot or {}).get(c['id'])} for c in cells if c['id'] in snapshot]
        result['feedback'] = submission.feedback if submission.reviewed_at else None
        result['attempt_number'] = submission.attempt_number
    return result
