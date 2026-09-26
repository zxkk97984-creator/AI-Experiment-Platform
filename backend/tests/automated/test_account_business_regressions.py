"""Account security rules exercised through public APIs and persisted outcomes."""
import pytest

from app.models import User
from conftest import auth_header, create_user, login

API = "/api/v1"


@pytest.mark.parametrize("role", ["student", "teacher"])
@pytest.mark.parametrize("field,value", [
    ("student_no", "CHANGED-001"), ("real_name", "Changed name"),
    ("department", "Changed department"), ("status", "disabled"),
    ("role", "admin"),
])
def test_self_cannot_change_protected_identity(client, db_session_factory, role, field, value):
    user = create_user(db_session_factory, "identity-user", role)
    token, _ = login(client, user.username)
    with db_session_factory() as db:
        before = getattr(db.get(User, user.id), field)
    response = client.patch(f"{API}/users/{user.id}", headers=auth_header(token), json={field: value})
    assert response.status_code in (403, 422), response.text
    with db_session_factory() as db:
        assert getattr(db.get(User, user.id), field) == before


@pytest.mark.parametrize("current_password", [None, "IncorrectPassword!"])
def test_admin_self_password_change_requires_current_password(client, db_session_factory, current_password):
    admin = create_user(db_session_factory, "self-admin", "admin")
    token, _ = login(client, admin.username)
    payload = {"password": "ChangedPassword!"}
    if current_password is not None:
        payload["current_password"] = current_password
    response = client.patch(f"{API}/users/{admin.id}/password", headers=auth_header(token), json=payload)
    assert response.status_code in (401, 422), response.text
    login(client, admin.username)


@pytest.mark.parametrize("suffix,payload", [
    ("", {"role": "teacher"}), ("", {"status": "disabled"}),
    ("/status", {"status": "disabled"}),
])
def test_last_active_admin_cannot_be_removed(client, db_session_factory, suffix, payload):
    admin = create_user(db_session_factory, "last-admin", "admin")
    create_user(db_session_factory, "disabled-admin", "admin")
    with db_session_factory() as db:
        db.query(User).filter_by(username="disabled-admin").one().status = "disabled"
        db.commit()
    token, _ = login(client, admin.username)
    response = client.patch(f"{API}/users/{admin.id}{suffix}", headers=auth_header(token), json=payload)
    assert response.status_code == 409, response.text
    with db_session_factory() as db:
        saved = db.get(User, admin.id)
        assert (saved.role, saved.status) == ("admin", "active")


@pytest.mark.parametrize("change", [{"role": "teacher"}, {"status": "disabled"}])
def test_identity_change_revokes_old_access_and_refresh_even_after_reenable(client, db_session_factory, change):
    admin = create_user(db_session_factory, "change-admin", "admin")
    student = create_user(db_session_factory, "change-student", "student")
    student_token, refresh_token = login(client, student.username)
    admin_token, _ = login(client, admin.username)
    changed = client.patch(f"{API}/users/{student.id}", headers=auth_header(admin_token), json=change)
    assert changed.status_code == 200, changed.text
    if "status" in change:
        enabled = client.patch(f"{API}/users/{student.id}/status", headers=auth_header(admin_token), json={"status": "active"})
        assert enabled.status_code == 200, enabled.text
    assert client.get(f"{API}/auth/me", headers=auth_header(student_token)).status_code == 401
    # Explicit cookie avoids accidentally refreshing the administrator's session.
    client.cookies.clear()
    client.cookies.set("dai_refresh_token", refresh_token)
    assert client.post(f"{API}/auth/refresh", json={"refresh_token": refresh_token}).status_code == 401
