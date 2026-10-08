"""Shared module content is reusable without granting other classes' data."""
from app.models import Course, ExperimentModule, NotebookTemplate, NotebookTemplateVersion
from app.models.teaching import CourseOffering
from conftest import make_template_version
from test_school_offerings import school  # noqa: F401


def test_assigned_teacher_can_reuse_module_already_approved_for_shared_course(client, school, db_session_factory):  # noqa: F811
    version_id = make_template_version(db_session_factory)
    with db_session_factory() as db:
        version = db.get(NotebookTemplateVersion, version_id)
        template = db.get(NotebookTemplate, version.template_id)
        template.status, template.current_version_id = 'published', version_id
        module = ExperimentModule(name='共享模块', status='published', owner_id=school['users']['owner'].id,
                                  template_id=version.template_id)
        db.add(module); db.commit(); ident = module.id
    owner, other = school['offerings']
    payload = {'entry_kind': 'module', 'entry_id': ident}
    # Course allocation alone does not let a teacher adopt arbitrary private modules.
    assert client.post(f'/api/v1/offerings/{other}/experiments', json=payload, headers=school['headers']['other']).status_code == 403
    assert client.post(f'/api/v1/offerings/{owner}/experiments', json=payload, headers=school['headers']['owner']).status_code == 200
    templates = client.get(f"/api/v1/courses/{school['course']}/teaching-templates", headers=school['headers']['other'])
    assert templates.status_code == 200
    assert any(row['id'] == ident and row['kind'] == 'module' for row in templates.json()['experiments'])
    released = client.post(f'/api/v1/offerings/{other}/experiments', json=payload, headers=school['headers']['other'])
    assert released.status_code == 200, released.text
    assert client.get(f'/api/v1/offerings/{owner}/students', headers=school['headers']['other']).status_code == 403
    assert client.get(f"/api/v1/courses/{school['course']}/teaching-templates", headers=school['headers']['b']).status_code == 403
    with db_session_factory() as db:
        unrelated = Course(title='其他课程', status='published', teacher_id=school['users']['other'].id)
        db.add(unrelated); db.flush()
        instance = CourseOffering(course_id=unrelated.id, academic_term_id=school['term'],
            teaching_class_id=school['classes'][1], teacher_id=school['users']['other'].id, status='active')
        db.add(instance); db.commit(); unrelated_id, instance_id = unrelated.id, instance.id
    templates = client.get(f'/api/v1/courses/{unrelated_id}/teaching-templates', headers=school['headers']['other'])
    assert all(row['id'] != ident or row['kind'] != 'module' for row in templates.json()['experiments'])
    assert client.post(f'/api/v1/offerings/{instance_id}/experiments', json=payload, headers=school['headers']['other']).status_code == 403
