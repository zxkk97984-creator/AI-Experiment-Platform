"""Exercise the School DDL from the real pre-School schema, with old facts.

SQLite files and the optional dedicated MySQL schema are disposable fixtures.
No latest-metadata create_all is used to fabricate a historical database.
"""
from datetime import date, datetime
import os
import shutil

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import User
from app.models.teaching import CourseOffering
from app.services.offering_service import require_offering, teacher_offering_ids
from migration_stage_support import (
    assert_head_metadata,
    assert_historical_integer_types,
    historical_schema,
    load_revision,
    upgrade_to_current_head,
)

SCHOOL = load_revision('20261003_0001')
FIXED_TIME = datetime(2026, 9, 1, 12)
OLD_HASH = '$2b$12$historical-fixture-hash-preserved-verbatim'


def _insert(connection, table, **values):
    """Insert using only columns present at this migration phase.

    Nullable/server-default columns are omitted. Every required FK must be
    specified; inert required scalar fields get deterministic fixture values.
    """
    for column in table.columns:
        if column.name in values or column.nullable or column.server_default:
            continue
        if column.primary_key and column.autoincrement is True:
            continue
        assert not column.foreign_keys, f'Fixture must specify {table.name}.{column.name}'
        if isinstance(column.type, sa.Boolean):
            value = False
        elif isinstance(column.type, (sa.Integer, sa.Float, sa.Numeric)):
            value = 0
        elif isinstance(column.type, sa.JSON):
            value = [] if column.name in {'cells', 'cell_order', 'draft_cells', 'items', 'allowed_imports', 'draft_allowed_imports'} else {}
        elif isinstance(column.type, sa.DateTime):
            value = FIXED_TIME
        elif isinstance(column.type, sa.Date):
            value = date(2026, 9, 1)
        else:
            value = 'fixture'
        values[column.name] = value
    connection.execute(table.insert().values(**values))


def _seed_old_facts(connection):
    metadata = sa.MetaData()
    metadata.reflect(connection)
    tables = metadata.tables
    for ident, role in ((10, 'teacher'), (20, 'student'), (30, 'admin')):
        _insert(connection, tables['users'], id=ident, username=f'migration-{role}',
                password_hash=OLD_HASH, role=role, real_name=f'Fixture {role}',
                status='active', session_version=17, credential_version=4,
                must_change_password=False)
    _insert(connection, tables['academic_terms'], id=101, code='FIXTURE-2026', name='Fixture term', status='active',
            start_date=date(2026, 9, 1), end_date=date(2027, 1, 31))
    for ident in (201, 202):
        _insert(connection, tables['teaching_classes'], id=ident, academic_term_id=101,
                code=f'FIXTURE-{ident}', name=f'Fixture class {ident}', status='active')
    _insert(connection, tables['courses'], id=301, teacher_id=10, academic_term_id=101,
            title='Legacy shared course', status='published', visibility='class')
    for ident, klass in ((501, 201), (502, 202)):
        _insert(connection, tables['course_teaching_classes'], id=ident, course_id=301, teaching_class_id=klass)
    _insert(connection, tables['teaching_class_students'], id=503, teaching_class_id=201, student_id=20, status='active')
    _insert(connection, tables['course_enrollments'], id=504, course_id=301, student_id=20, status='enrolled', origin='manual')
    _insert(connection, tables['chapters'], id=401, course_id=301, title='Fixture chapter')
    _insert(connection, tables['notebook_templates'], id=701, owner_id=10, name='Fixture template', status='published',
            draft_environment_version_id=1, draft_revision=3, draft_import_policy_mode='unrestricted')
    _insert(connection, tables['notebook_template_versions'], id=702, template_id=701, published_by_id=10,
            version_number=1, sha256='a' * 64, environment_version_id=1, import_policy_mode='unrestricted')
    connection.execute(tables['notebook_templates'].update().where(tables['notebook_templates'].c.id == 701).values(current_version_id=702))
    _insert(connection, tables['lessons'], id=601, chapter_id=401, title='Fixture notebook',
            content_type='notebook', template_id=701, status='published')
    _insert(connection, tables['experiment_modules'], id=602, owner_id=10, name='Fixture module', template_id=701, status='published')
    _insert(connection, tables['experiment_records'], id=1001, lesson_id=601, student_id=20,
            template_version_id=702, environment_version_id=1, status='completed', record_revision=3,
            cells_sources={'old-cell': 'print(1)'}, cells_outputs={'old-cell': {'execution_count': 1}})
    _insert(connection, tables['experiment_submissions'], id=1002, record_id=1001, attempt_number=1,
            cells_snapshot={'old-cell': 'print(1)'}, score=77.5, reviewed_by_id=10, reviewed_at=FIXED_TIME)
    _insert(connection, tables['lab_assessments'], id=901, entry_key='lesson:601', owner_id=10, version=1, status='published', template_version_id=702)
    _insert(connection, tables['lab_records'], record_id=1001, assessment_id=901, generation=2, selected_submission_id=1002)
    _insert(connection, tables['lab_submissions'], submission_id=1002, assessment_id=901, submission_hash='b' * 64, published_grade_id=1003)
    _insert(connection, tables['lab_grade_revisions'], id=1003, submission_id=1002, assessment_id=901,
            source='manual', total=77.5, published_at=FIXED_TIME, reviewer_id=10, feedback='Old published grade')
    _insert(connection, tables['lesson_progress'], id=1004, lesson_id=601, student_id=20, status='completed')
    _insert(connection, tables['exams'], id=801, course_id=301, title='Old exam', created_by_id=10,
            status='published', duration_minutes=60, audience_mode='all_enrolled')
    _insert(connection, tables['exam_grades'], id=805, exam_id=801, student_id=20, score=86.25)


def _migrate(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(SCHOOL, direction)()


def _assert_old_facts(connection):
    row = connection.execute(sa.text('SELECT password_hash,session_version,credential_version,must_change_password FROM users WHERE id=20')).one()
    assert tuple(row) == (OLD_HASH, 17, 4, 0)
    assert connection.scalar(sa.text('SELECT COUNT(*) FROM users WHERE password_hash=:hash AND session_version=17 AND credential_version=4'), {'hash': OLD_HASH}) == 3
    assert connection.execute(sa.text('SELECT id,record_id,score,reviewed_by_id FROM experiment_submissions')).one() == (1002, 1001, 77.5, 10)
    assert connection.execute(sa.text('SELECT id,submission_id,total,feedback FROM lab_grade_revisions')).one() == (1003, 1002, 77.5, 'Old published grade')
    assert connection.execute(sa.text('SELECT record_id,selected_submission_id,generation FROM lab_records')).one() == (1001, 1002, 2)
    assert connection.execute(sa.text('SELECT submission_id,published_grade_id FROM lab_submissions')).one() == (1002, 1003)
    assert connection.execute(sa.text('SELECT id,exam_id,student_id,score FROM exam_grades')).one() == (805, 801, 20, 86.25)


def _assert_pending_and_legacy(connection):
    rows = connection.execute(sa.text('SELECT course_id,academic_term_id,teaching_class_id,teacher_id,status,leaderboard_named FROM course_offerings ORDER BY teaching_class_id')).all()
    assert rows == [(301, 101, 201, 10, 'pending_assignment', 0), (301, 101, 202, 10, 'pending_assignment', 0)]
    assert connection.scalar(sa.text('SELECT COUNT(*) FROM offering_enrollments')) == 0
    assert connection.scalar(sa.text('SELECT COUNT(*) FROM credential_deliveries')) == 0
    assert connection.execute(sa.text('SELECT id,offering_id,scope_key,record_revision FROM experiment_records')).one() == (1001, None, 'legacy', 3)
    assert connection.execute(sa.text('SELECT id,offering_id,scope_key,status FROM lesson_progress')).one() == (1004, None, 'legacy', 'completed')
    assert connection.execute(sa.text('SELECT id,offering_id,source_task_id FROM exams')).one() == (801, None, None)
    # Old SQLite DDL declared each lesson/student FK twice; reflection warns
    # while deduplicating them. Validate both original targets remain present.
    targets = {(tuple(fk['constrained_columns']), fk['referred_table'], tuple(fk['referred_columns']))
               for fk in sa.inspect(connection).get_foreign_keys('lesson_progress')}
    assert targets == {(('lesson_id',), 'lessons', ('id',)), (('student_id',), 'users', ('id',)),
                       (('offering_id',), 'course_offerings', ('id',))}


@pytest.fixture(scope='module')
def legacy_schema(tmp_path_factory):
    path = tmp_path_factory.mktemp('teaching-history') / 'legacy.db'
    historical_schema(path, '20260926_0001')
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as connection:
        assert_historical_integer_types(connection)
        _seed_old_facts(connection)
        _assert_old_facts(connection)
        assert connection.execute(sa.text('PRAGMA foreign_key_check')).all() == []
    engine.dispose()
    return path


@pytest.fixture
def migrated_engine(legacy_schema, tmp_path):
    path = tmp_path / 'school.db'
    shutil.copy2(legacy_schema, path)
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as connection:
        _migrate(connection, 'upgrade')
    engine.dispose()
    # 历史阶段手动应用完成后，用 Alembic 自身收敛到当前 head：后续新增迁移
    # （认领代次、结果修订号等）自动纳入，不再硬编码某一个 revision 号。
    upgrade_to_current_head(path, stamped_at='20261003_0001')
    engine = sa.create_engine(f'sqlite:///{path}')
    yield engine
    engine.dispose()


def test_upgrade_preserves_old_credentials_scores_and_requires_scope_confirmation(migrated_engine):
    with migrated_engine.connect() as connection:
        _assert_old_facts(connection)
        _assert_pending_and_legacy(connection)
        assert_head_metadata(connection)
        assert connection.execute(sa.text('PRAGMA foreign_key_check')).all() == []
    with Session(migrated_engine) as db:
        assert teacher_offering_ids(db, 10) == []
        candidate = db.scalar(sa.select(CourseOffering).order_by(CourseOffering.id))
        for user_id in (10, 20):
            with pytest.raises(HTTPException) as error:
                require_offering(db, candidate.id, db.get(User, user_id))
            assert error.value.status_code == 403
        assert require_offering(db, candidate.id, db.get(User, 30)).id == candidate.id


def _assert_constraints(connection):
    metadata = sa.MetaData()
    metadata.reflect(connection)
    tables = metadata.tables
    offerings = tables['course_offerings']
    ids = list(connection.scalars(sa.select(offerings.c.id).order_by(offerings.c.teaching_class_id)))
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        _insert(connection, offerings, id=8001, course_id=301, academic_term_id=101, teaching_class_id=201,
                teacher_id=10, status='active', leaderboard_named=False)
    enrollments = tables['offering_enrollments']
    key = '301:101:20'
    _insert(connection, enrollments, id=8002, offering_id=ids[0], student_id=20, status='active', active_scope_key=key)
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        _insert(connection, enrollments, id=8003, offering_id=ids[1], student_id=20, status='active', active_scope_key=key)
    # Transfer releases only the active key; the old offering row remains.
    connection.execute(enrollments.update().where(enrollments.c.id == 8002).values(status='transferred', active_scope_key=None))
    _insert(connection, enrollments, id=8003, offering_id=ids[1], student_id=20, status='active', active_scope_key=key)
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        _insert(connection, enrollments, id=8004, offering_id=ids[0], student_id=20, status='transferred', active_scope_key=None)
    records = tables['experiment_records']
    for offset, entry in enumerate(('lesson', 'module')):
        for index, offering_id in enumerate(ids):
            _insert(connection, records, id=8100 + offset * 10 + index, offering_id=offering_id,
                    scope_key=f'offering:{offering_id}', **{f'{entry}_id': 601 + offset}, student_id=20,
                    template_version_id=702, environment_version_id=1, status='started')
        with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
            _insert(connection, records, id=8200 + offset, offering_id=ids[0], scope_key=f'offering:{ids[0]}',
                    **{f'{entry}_id': 601 + offset}, student_id=20, template_version_id=702, environment_version_id=1, status='started')
    progress = tables['lesson_progress']
    for index, offering_id in enumerate(ids):
        _insert(connection, progress, id=8300 + index, offering_id=offering_id, scope_key=f'offering:{offering_id}',
                lesson_id=601, student_id=20, status='in_progress')
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        _insert(connection, progress, id=8302, offering_id=ids[0], scope_key=f'offering:{ids[0]}',
                lesson_id=601, student_id=20, status='in_progress')


def test_new_scope_and_transfer_uniqueness_is_enforced_by_database(migrated_engine):
    with migrated_engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        _assert_constraints(connection)
        assert connection.execute(sa.text('PRAGMA foreign_key_check')).all() == []


@pytest.mark.parametrize('environment,allow', [('production', '1'), ('production', None), ('development', None)])
def test_destructive_downgrade_is_rejected_before_any_ddl(migrated_engine, monkeypatch, environment, allow):
    monkeypatch.setenv('DAI_ENVIRONMENT', environment)
    if allow is None:
        monkeypatch.delenv('DAI_ALLOW_TEACHING_DOWNGRADE', raising=False)
    else:
        monkeypatch.setenv('DAI_ALLOW_TEACHING_DOWNGRADE', allow)
    with migrated_engine.begin() as connection:
        tables = sa.inspect(connection).get_table_names()
        with pytest.raises(RuntimeError, match='production uses forward migrations'):
            _migrate(connection, 'downgrade')
        assert sa.inspect(connection).get_table_names() == tables
        assert 'offering_id' in {column['name'] for column in sa.inspect(connection).get_columns('experiment_records')}
        _assert_old_facts(connection)


def test_explicit_disposable_downgrade_restores_prior_keys_and_keeps_old_facts(migrated_engine, monkeypatch):
    monkeypatch.setenv('DAI_ENVIRONMENT', 'development')
    monkeypatch.setenv('DAI_ALLOW_TEACHING_DOWNGRADE', '1')
    with migrated_engine.begin() as connection:
        _migrate(connection, 'downgrade')
        _assert_old_facts(connection)
        inspector = sa.inspect(connection)
        assert 'course_offerings' not in inspector.get_table_names()
        assert {'offering_id', 'scope_key'}.isdisjoint({column['name'] for column in inspector.get_columns('experiment_records')})
        keys = {constraint['name']: constraint['column_names'] for constraint in inspector.get_unique_constraints('experiment_records')}
        assert keys['uq_record_lesson_student'] == ['lesson_id', 'student_id']
        assert keys['uq_record_module_student'] == ['module_id', 'student_id']
        assert connection.execute(sa.text('PRAGMA foreign_key_check')).all() == []


def test_mysql_historical_upgrade_matches_models_and_preserves_facts(monkeypatch):
    """Opt-in fixture: create and delete only this exact isolated schema."""
    from migration_stage_support import migrate_url, seed_environment

    configured = os.environ.get('DAI_TEACHING_MIGRATION_MYSQL_URL')
    if not configured:
        pytest.skip('Dedicated disposable MySQL migration URL not supplied')
    url = sa.engine.make_url(configured)
    assert url.drivername == 'mysql+pymysql'
    assert url.host in {'127.0.0.1', 'localhost'} and url.port == 13310
    assert url.username == 'dai_test' and url.database == 'dai_school_migration_verify'
    # URL.set(None) retains the old field, so use an explicit empty database.
    server = sa.create_engine(url.set(database=''))
    with server.begin() as connection:
        existing = connection.scalar(sa.text('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name'), {'name': url.database})
        if existing:
            # The caller may provision the exact fixture schema to grant this
            # restricted user privileges. Refuse any schema containing tables.
            assert connection.scalar(sa.text('SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=:name'), {'name': url.database}) == 0, 'Refusing to reuse a populated schema'
        else:
            connection.exec_driver_sql('CREATE DATABASE dai_school_migration_verify CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    engine = sa.create_engine(url)
    try:
        migrate_url(configured, 'upgrade', 'b4c5d6e7f890')
        with engine.begin() as connection:
            seed_environment(connection)
        migrate_url(configured, 'upgrade', '20260926_0001')
        with engine.begin() as connection:
            _seed_old_facts(connection)
            _assert_old_facts(connection)
        # Real MySQL ALTERs keep FK checks enabled, unlike SQLite batch rebuilds.
        migrate_url(configured, 'upgrade', '20261003_0001')
        # 收敛到当前 head：新增迁移（认领代次/结果修订号）自动纳入覆盖。
        migrate_url(configured, 'upgrade', 'head')
        with engine.connect() as connection:
            _assert_old_facts(connection)
            _assert_pending_and_legacy(connection)
            assert_head_metadata(connection)
        with engine.begin() as connection:
            _assert_constraints(connection)
        monkeypatch.setenv('DAI_ENVIRONMENT', 'production')
        monkeypatch.setenv('DAI_ALLOW_TEACHING_DOWNGRADE', '1')
        with engine.begin() as connection:
            with pytest.raises(RuntimeError, match='production uses forward migrations'):
                _migrate(connection, 'downgrade')
            _assert_old_facts(connection)
    finally:
        engine.dispose()
        with server.begin() as connection:
            connection.exec_driver_sql('DROP DATABASE dai_school_migration_verify')
        server.dispose()
