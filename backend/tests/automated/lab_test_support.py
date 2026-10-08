"""Small isolated lab fixtures; no real accounts, DB services or model calls."""
import copy
from datetime import date, timedelta
from types import SimpleNamespace

from app.models import (AcademicTerm, Course, TeachingClass, EnvironmentVersion, ExperimentModule, LabAssessment,
    NotebookTemplate, NotebookTemplateVersion, User)
from app.models.teaching import CourseOffering, OfferingEnrollment, OfferingExperiment
from app.schemas.lab import LabAssessmentConfig
from app.security import create_token
from app.services.lab_assessment_service import digest, template_hash


def headers(user, settings):
    token = create_token(subject=user.id, role=user.role, token_type="access",
        secret_key=settings.secret_key, algorithm=settings.algorithm,
        expires_delta=timedelta(hours=1), session_version=user.session_version)
    return {"Authorization": f"Bearer {token}"}


def make_case(factory, settings, *, published=True):
    with factory() as db:
        users = []
        for name, role in [("lab-teacher", "teacher"), ("lab-other-teacher", "teacher"),
                           ("lab-student", "student"), ("lab-other-student", "student"), ("lab-admin", "admin")]:
            user = User(username=name, real_name=name, role=role, status="active", password_hash="test")
            db.add(user)
            users.append(user)
        db.flush()
        teacher, other_teacher, student, other_student, admin = users
        environment = db.query(EnvironmentVersion).filter_by(status="available").first()
        cells = [{"id": "code", "type": "code", "source": "print(1)", "student_editable": True},
                 {"id": "report", "type": "markdown", "source": "My analysis", "student_editable": True}]
        template = NotebookTemplate(name="Lab template", owner_id=teacher.id, status="published",
            draft_cells=copy.deepcopy(cells), draft_metadata={}, draft_environment_version_id=environment.id)
        db.add(template)
        db.flush()
        version = NotebookTemplateVersion(template_id=template.id, version_number=1,
            sha256="a" * 64, cells=copy.deepcopy(cells), cell_order=["code", "report"],
            published_by_id=teacher.id, environment_version_id=environment.id)
        db.add(version)
        db.flush()
        template.current_version_id = version.id
        module = ExperimentModule(name="Notebook lab", owner_id=teacher.id, template_id=template.id, status="published")
        db.add(module)
        db.flush()
        config = LabAssessmentConfig(criteria=[{"id": "method", "name": "Method", "description": "Complete the task",
            "source": "check", "max_score": 100}], check_script="REFERENCE_CHECK_SECRET",
            reference_cells=[{"id": "code", "type": "code", "source": "REFERENCE_CODE_SECRET"}],
            confirmed=True).model_dump()
        assessment = None
        if published:
            assessment = LabAssessment(owner_id=teacher.id, entry_key=f"module:{module.id}",
                version=1, status="published", config=copy.deepcopy(config), template_version_id=version.id,
                template_draft_hash=template_hash(db, template),
                config_hash=digest({"config": config, "template_version_id": version.id}))
            db.add(assessment)
        db.flush()
        term = AcademicTerm(code='lab-term', name='Lab term', start_date=date(2026, 9, 1), end_date=date(2027, 1, 31), status='active')
        db.add(term)
        db.flush()
        course = Course(title='Lab course', teacher_id=teacher.id, academic_term_id=term.id, status='published')
        db.add(course)
        db.flush()
        offerings = []
        for index, learner in enumerate((student, other_student), 1):
            klass = TeachingClass(code=f'lab-{index}', name=f'Lab class {index}', academic_term_id=term.id, status='active')
            db.add(klass)
            db.flush()
            offering = CourseOffering(course_id=course.id, academic_term_id=term.id, teaching_class_id=klass.id, teacher_id=teacher.id, status='active')
            db.add(offering)
            db.flush()
            db.add(OfferingEnrollment(offering_id=offering.id, student_id=learner.id, status='active', active_scope_key=f'{course.id}:{term.id}:{learner.id}'))
            db.add(OfferingExperiment(offering_id=offering.id, entry_kind='module', entry_id=module.id,
                template_version_id=version.id, assessment_id=assessment.id if assessment else None, status='published'))
            offerings.append(offering)
        db.commit()
        return SimpleNamespace(teacher=teacher, other_teacher=other_teacher, student=student,
            other_student=other_student, admin=admin, template_id=template.id, version_id=version.id,
            module_id=module.id, assessment_id=assessment.id if assessment else None,
            config=config, environment_id=environment.id, offering_id=offerings[0].id,
            other_offering_id=offerings[1].id)


def record(client, case, settings, user=None):
    offering_id = case.other_offering_id if user and user.id == case.other_student.id else case.offering_id
    response = client.post(f"/api/v1/experiments/records/ensure-for-module/{case.module_id}?offering_id={offering_id}",
        headers=headers(user or case.student, settings))
    assert response.status_code == 200, response.text
    return response.json()


def save(client, record_id, revision, user, settings, source="print(2)"):
    response = client.put(f"/api/v1/experiments/records/{record_id}/cells",
        json={"record_revision": revision, "cells": {"code": source}}, headers=headers(user, settings))
    assert response.status_code == 200, response.text
    return response.json()["record_revision"]


def complete(factory, submission_id, score):
    from app.models import LabRun, LabSubmission
    from app.services.lab_grading import publish_evaluation
    with factory() as db:
        state = db.get(LabSubmission, submission_id)
        run = db.get(LabRun, state.current_run_id)
        run.status = "succeeded"
        result = {"total": score, "items": [], "metrics": {}, "feedback": "test feedback"}
        publish_evaluation(db, run, result)
        db.commit()
        return run.id
