"""Request-local, class-bounded source rows; formulas stay in gradebook_service."""
from collections import defaultdict

from sqlalchemy import or_, select
from sqlalchemy.orm import load_only

from app.models import (CodeGrade, ExamQuestion, ExamSubmission, ExperimentRecord,
                        ExperimentSubmission, JudgeQuestion, LabGradeRevision, LabRecord,
                        LabRun, LabSubmission, Submission)


def preload_sources(db, columns, targets, offering_id, student_ids, *, lock=False):
    cache = {}

    def read(model, condition, fields):
        query = (select(model).where(condition).order_by(*model.__mapper__.primary_key)
                 .options(load_only(*(getattr(model, field) for field in fields.split()))))
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return list(db.scalars(query))

    def group(name, rows, key):
        bucket = defaultdict(list)
        for row in rows:
            bucket[key(row)].append(row)
        cache[name] = bucket

    kinds = {kind: {targets[col.id].id for col in columns
                    if col.source_kind == kind and targets.get(col.id) is not None}
             for kind in ('assignment', 'exam', 'experiment')}
    questions = read(JudgeQuestion, JudgeQuestion.assignment_id.in_(kinds['assignment']),
                     'id assignment_id') if kinds['assignment'] and student_ids else []
    group('questions', questions, lambda row: row.assignment_id)
    submissions = read(Submission, Submission.question_id.in_([q.id for q in questions])
                       & Submission.student_id.in_(student_ids),
                       'id question_id student_id grading_status score updated_at') if questions else []
    group('submissions', submissions, lambda row: (row.question_id, row.student_id))
    grades = read(CodeGrade, CodeGrade.submission_id.in_([s.id for s in submissions]),
                  'id submission_id mode status needs_teacher_review rubric_id') if submissions else []
    group('code_grades', grades, lambda row: row.submission_id)

    exam_questions = read(ExamQuestion, ExamQuestion.exam_id.in_(kinds['exam']),
                          'id exam_id points') if kinds['exam'] and student_ids else []
    group('exam_questions', exam_questions, lambda row: row.exam_id)
    exams = read(ExamSubmission, ExamSubmission.exam_id.in_(kinds['exam'])
                 & ExamSubmission.student_id.in_(student_ids),
                 'id exam_id student_id status score updated_at graded_at') if kinds['exam'] and student_ids else []
    group('exams', exams, lambda row: (row.exam_id, row.student_id))

    experiments = [targets[col.id] for col in columns
                   if col.source_kind == 'experiment' and targets.get(col.id) is not None]
    lessons = [target.entry_id for target in experiments if target.entry_kind == 'lesson']
    modules = [target.entry_id for target in experiments if target.entry_kind == 'module']
    records = read(ExperimentRecord, (ExperimentRecord.offering_id == offering_id)
                   & ExperimentRecord.student_id.in_(student_ids)
                   & or_(ExperimentRecord.lesson_id.in_(lessons), ExperimentRecord.module_id.in_(modules)),
                   'id student_id lesson_id module_id') if experiments and student_ids else []
    group('records', records, lambda row: (
        'lesson' if row.lesson_id is not None else 'module',
        row.lesson_id if row.lesson_id is not None else row.module_id, row.student_id))
    record_ids = [row.id for row in records]
    bindings = read(LabRecord, LabRecord.record_id.in_(record_ids),
                    'record_id selected_submission_id') if record_ids else []
    group('bindings', bindings, lambda row: row.record_id)
    experiment_submissions = read(ExperimentSubmission, ExperimentSubmission.record_id.in_(record_ids),
                                  'id record_id score reviewed_at') if record_ids else []
    group('experiment_submissions', experiment_submissions, lambda row: row.record_id)
    group('selected', experiment_submissions, lambda row: row.id)
    by_id = {row.id: row for row in experiment_submissions}
    states = read(LabSubmission, LabSubmission.submission_id.in_(list(by_id)),
                  'submission_id published_grade_id current_run_id review_requested') if by_id else []
    group('states', states, lambda row: by_id[row.submission_id].record_id)
    grade_ids = {row.published_grade_id for row in states if row.published_grade_id}
    lab_grades = read(LabGradeRevision, LabGradeRevision.id.in_(grade_ids),
                      'id submission_id assessment_id total published_at') if grade_ids else []
    group('lab_grades', lab_grades, lambda row: row.id)
    run_ids = {row.current_run_id for row in states if row.current_run_id}
    runs = read(LabRun, LabRun.id.in_(run_ids), 'id status') if run_ids else []
    group('runs', runs, lambda row: row.id)
    return cache
