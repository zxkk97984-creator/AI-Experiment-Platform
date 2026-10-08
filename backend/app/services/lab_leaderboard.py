"""An offering-local display of selected, published results; never grade input."""
from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import select

from app.errors import api_error
from app.models import ExperimentRecord, ExperimentSubmission, User
from app.models.lab import LabAssessment, LabGradeRevision, LabRecord, LabRun, LabSubmission


def leaderboard(db, record, user, settings):
    from app.services.offering_service import require_offering
    if not record.offering_id:
        raise api_error(409, 'LAB_LEADERBOARD_SCOPE', '排行榜仅用于已关联教学班的正式实验')
    offering = require_offering(db, record.offering_id, user)
    binding = db.get(LabRecord, record.id)
    if not binding:
        raise api_error(409, 'LAB_NOT_ENABLED', '实验尚未启用评测')
    assessment = db.get(LabAssessment, binding.assessment_id)
    # Use an explicitly authorized Core projection: the request ORM policy
    # intentionally restricts student ExperimentRecord queries to their own
    # work. The board exposes only the public projection of this one offering.
    rec, bound = ExperimentRecord.__table__, LabRecord.__table__
    submitted, state = ExperimentSubmission.__table__, LabSubmission.__table__
    grade, users = LabGradeRevision.__table__, User.__table__
    query = (select(rec.c.student_id, users.c.real_name, grade.c.total,
        grade.c.metrics, grade.c.run_id, submitted.c.id.label('submission_id'))
        .select_from(rec.join(bound, bound.c.record_id == rec.c.id)
            .join(submitted, submitted.c.id == bound.c.selected_submission_id)
            .join(state, state.c.submission_id == submitted.c.id)
            .join(grade, grade.c.id == state.c.published_grade_id)
            .join(users, users.c.id == rec.c.student_id))
        .where(rec.c.offering_id == offering.id, bound.c.assessment_id == assessment.id,
            grade.c.assessment_id == assessment.id, grade.c.published_at.is_not(None),
            rec.c.lesson_id == record.lesson_id, rec.c.module_id == record.module_id)
        .order_by(grade.c.total.desc(), rec.c.student_id))
    rows = db.execute(query).mappings().all()
    # The published manual grade can have no run. Retain the most recent verified
    # measurements for display without using them to alter that manual grade.
    evidence = {}
    submission_ids = [r['submission_id'] for r in rows]
    if submission_ids:
        for grade, run in db.execute(select(LabGradeRevision, LabRun)
                .join(LabRun, LabRun.id == LabGradeRevision.run_id)
                .where(LabGradeRevision.submission_id.in_(submission_ids), LabGradeRevision.assessment_id == assessment.id,
                    LabGradeRevision.published_at.is_not(None), LabRun.status == 'succeeded')
                .order_by(LabGradeRevision.id)):
            evidence[grade.submission_id] = (grade.metrics or {}, run.result or {})
    result, rank, previous = [], 0, None
    named = bool(offering.leaderboard_named)
    for index, row in enumerate(rows, 1):
        if previous != row['total']:
            rank = index
        previous = row['total']
        alias = hmac.new(settings.secret_key.encode(), f'{offering.id}:{assessment.id}:{row["student_id"]}'.encode(), hashlib.sha256).hexdigest()[:6].upper()
        metrics, run_result = evidence.get(row['submission_id'], (row['metrics'] or {}, {}))
        # Do not expose student IDs, usernames, submission IDs or raw run records
        # when anonymity is selected, including to the teacher via this endpoint.
        result.append({'rank': rank, 'display_name': row['real_name'] if named else f'同学 {alias}',
            'is_me': user.role == 'student' and row['student_id'] == user.id,
            'score': row['total'], 'metric': metrics.get(assessment.config.get('metric')),
            'program_elapsed_seconds': run_result.get('program_elapsed_seconds')})
    return {'named': named, 'assessment_version': assessment.version,
            'metric': assessment.config.get('metric') if assessment.config.get('task_type') != 'notebook' else None,
            'grade_selection': assessment.config.get('grade_selection', 'highest'), 'items': result}
