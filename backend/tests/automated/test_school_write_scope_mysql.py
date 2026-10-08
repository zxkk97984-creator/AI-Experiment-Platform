"""Independent InnoDB transactions prove current write gates after lock waits.

Only the explicitly reserved empty schema is accepted. This suite never opens
or resets an application database.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event, get_ident

from fastapi import HTTPException
import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import make_url

from app.database import Base, create_engine_from_url, sessionmaker_for_engine
from app.services.account_service import lock_policy
from app.services.offering_service import require_offering
from test_school_write_scope import cache_scope, change_scope, make_scope_case

pytestmark = pytest.mark.no_auto_env_seed


@pytest.fixture()
def mysql_scope_factory():
    raw = os.environ.get('DAI_TEST_SCOPE_MYSQL_URL')
    if not raw:
        pytest.skip('Requires reserved disposable dai_school_scope_verify MySQL schema')
    url = make_url(raw)
    if url.get_backend_name() != 'mysql' or url.database != 'dai_school_scope_verify':
        pytest.fail('Refusing teaching concurrency tests outside the reserved disposable schema')
    engine = create_engine_from_url(raw).execution_options(isolation_level='REPEATABLE READ')
    if inspect(engine).get_table_names():
        engine.dispose()
        pytest.fail('Refusing to modify a nonempty teaching concurrency schema')
    Base.metadata.create_all(engine)
    factory = sessionmaker_for_engine(engine)
    try:
        yield factory
    finally:
        with engine.begin() as conn:
            conn.execute(text('UPDATE notebook_templates SET current_version_id = NULL'))
            conn.execute(text('UPDATE environment_profiles SET current_version_id = NULL'))
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize('kind', ['teacher', 'member', 'course', 'term', 'class', 'disabled', 'role', 'session', 'credential'])
def test_mysql_old_snapshot_write_waits_for_policy_then_rejects_revocation(mysql_scope_factory, kind):
    factory = mysql_scope_factory
    case = make_scope_case(factory)
    actor_id = case.student if kind in ('member', 'course') else case.teacher
    cached, held, waiting = Event(), Event(), Event()
    reader_thread = []
    connections = []
    engine = factory.kw['bind']
    def before_cursor(_connection, _cursor, statement, _parameters, _context, _executemany):
        if reader_thread and get_ident() == reader_thread[0] and statement.lower().startswith('update account_security_policies'):
            waiting.set()
    event.listen(engine, 'before_cursor_execute', before_cursor)
    def reader():
        reader_thread.append(get_ident())
        with factory() as db:
            db.execute(text('SET SESSION innodb_lock_wait_timeout=5'))
            connections.append(db.scalar(text('SELECT CONNECTION_ID()')))
            actor, strong_cache = cache_scope(db, case, actor_id)
            cached.set()
            assert held.wait(timeout=10)
            try:
                require_offering(db, case.offering, actor, write=True)
                db.commit()
                return 'unexpectedly_authorized'
            except HTTPException as exc:
                db.rollback()
                assert len(strong_cache) == 5
                return exc.status_code
    def administrator():
        assert cached.wait(timeout=10)
        with factory() as db:
            connections.append(db.scalar(text('SELECT CONNECTION_ID()')))
            lock_policy(db)
            held.set()
            # The reader is issuing its policy UPDATE while this independent
            # connection still owns the row, after all cached RR reads.
            assert waiting.wait(timeout=10)
            change_scope(db, case, kind, actor_id)
            db.commit()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reading, writing = pool.submit(reader), pool.submit(administrator)
            writing.result(timeout=20)
            assert reading.result(timeout=20) == (409 if kind in ('term', 'class') else 403)
    finally:
        event.remove(engine, 'before_cursor_execute', before_cursor)
    assert len(set(connections)) == 2
