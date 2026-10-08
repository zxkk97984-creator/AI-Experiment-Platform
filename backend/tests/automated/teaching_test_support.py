"""Explicit administrator grants for teaching-domain regression fixtures.

These helpers are called by test setup, never installed as request hooks or
automatically applied to arbitrary users/records.
"""
from sqlalchemy import select

from app.models import Course, ExperimentModule, Lesson, NotebookTemplate, User
from app.models.teaching import OfferingExperiment
from conftest import grant_teaching_fixture


def grant_course_students(factory, course_id, usernames):
    with factory() as db:
        users = list(db.scalars(select(User).where(User.username.in_(usernames))))
        assert len(users) == len(set(usernames)) and all(u.role == 'student' for u in users)
        offering = grant_teaching_fixture(db, db.get(Course, course_id), [u.id for u in users])
        db.commit()
        return offering.id


def grant_experiment(factory, kind, ident, usernames, *, course_id=None, template_version_id=None):
    with factory() as db:
        entry = db.get(Lesson if kind == 'lesson' else ExperimentModule, ident)
        assert entry is not None
        if kind == 'lesson':
            course = entry.chapter.course
            entry.status = 'published'
        elif course_id:
            course = db.get(Course, course_id)
        else:
            course = Course(title=f'实验授课 fixture {ident}', teacher_id=entry.owner_id, status='published')
            db.add(course)
            db.flush()
        users = list(db.scalars(select(User).where(User.username.in_(usernames))))
        assert len(users) == len(set(usernames)) and all(u.role == 'student' for u in users)
        offering = grant_teaching_fixture(db, course, [u.id for u in users])
        template = db.get(NotebookTemplate, entry.template_id) if entry.template_id else None
        version_id = template_version_id or (template.current_version_id if template else None)
        assert version_id is not None, 'Fixture release needs a published Notebook version'
        row = db.scalar(select(OfferingExperiment).where(OfferingExperiment.offering_id == offering.id,
            OfferingExperiment.entry_kind == kind, OfferingExperiment.entry_id == ident))
        if not row:
            row = OfferingExperiment(offering_id=offering.id, entry_kind=kind, entry_id=ident,
                template_version_id=version_id, status='published')
            db.add(row)
        else:
            row.template_version_id, row.status = version_id, 'published'
        db.commit()
        return offering.id
