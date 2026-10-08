from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, ConfigDict, Field
from app.config import Settings, get_settings
from sqlalchemy.orm import Session

from app.dependencies import get_authenticated_user, get_current_user, get_db, get_redis_client, require_roles, PaginationParams, pagination
from app.errors import api_error
from app.models import User, UserPreference
from app.roles import VALID_ROLES
from app.schemas import PaginatedResponse, PasswordUpdate, StatusUpdate, UserCreate, UserRead, UserUpdate
from app.schemas.preferences import UserPreferencesRead, UserPreferencesUpdate
from app.security import hash_password, validate_password_rules
from app.services import account_service as accounts
from app.services import credential_delivery_service as credentials
from app.services.time_utils import utc_now
from datetime import timedelta

router = APIRouter(prefix="/users", tags=["users"])

VALID_STATUSES = {"active", "disabled"}


@router.get("", response_model=PaginatedResponse)
def list_users(
    q: str | None = None,
    role: str | None = None,
    status_filter: str | None = None,
    pagination: PaginationParams = Depends(pagination),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    page, page_size = pagination.page, pagination.page_size
    query = select(User)
    count_query = select(func.count()).select_from(User)
    search_query = q.strip() if q else None
    if search_query:
        user_match = or_(
            User.username == search_query,
            User.student_no == search_query,
            User.real_name == search_query,
        )
        query = query.where(user_match)
        count_query = count_query.where(user_match)
    if role:
        query = query.where(User.role == role)
        count_query = count_query.where(User.role == role)
    if status_filter:
        query = query.where(User.status == status_filter)
        count_query = count_query.where(User.status == status_filter)
    total = db.scalar(count_query) or 0
    users = db.scalars(query.order_by(User.id).offset((page - 1) * page_size).limit(page_size)).all()
    return PaginatedResponse(items=[UserRead.model_validate(user) for user in users], page=page, page_size=page_size, total=total)


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    if payload.password is not None and payload.username and payload.password.strip().casefold() == payload.username.strip().casefold():
        raise api_error(422, 'PASSWORD_EQUALS_USERNAME', '密码不能与用户名相同')
    student_no = payload.student_no.strip() if payload.student_no else None
    if payload.role == "student" and not student_no:
        raise api_error(422, "STUDENT_NO_REQUIRED", "新建学生必须填写学号")
    username = student_no if payload.role == 'student' else payload.username.strip()
    if not username or len(username) > 80 or not payload.real_name.strip() or len(payload.real_name) > 120:
        raise api_error(422, 'INVALID_USER_FIELD', '请检查学号、账号及姓名长度')
    if db.scalar(select(User).where(User.username == username)):
        raise api_error(409, "USERNAME_EXISTS", "用户名已存在")
    if student_no and db.scalar(select(User).where(User.student_no == student_no)):
        raise api_error(409, "STUDENT_NO_EXISTS", "学号已存在")
    days = 7
    delivery = None
    if payload.role == 'student':
        _legacy_password, days, _version = accounts.temporary_policy(db, settings)
        delivery, password = credentials.prepare(db, _.id, settings, f'student:{student_no}')
    else:
        password = payload.password
    try:
        validate_password_rules(password or '', username)
    except ValueError as exc:
        raise api_error(422, 'PASSWORD_POLICY', str(exc))
    user = User(username=username, student_no=student_no, real_name=payload.real_name.strip(),
                department=payload.department, role=payload.role, status=payload.status,
                password_hash=hash_password(password), must_change_password=payload.role == 'student',
                temporary_password_expires_at=utc_now() + timedelta(days=days) if payload.role == 'student' else None)
    db.add(user)
    try:
        db.flush()
        if delivery:
            credentials.issue(db, delivery, user, settings)
        accounts.audit(db, _.id, 'user.create', 'user', user.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, 'USER_IDENTITY_CONFLICT', '学号或账号已存在')
    db.refresh(user)
    return user


@router.get("/students", response_model=PaginatedResponse)
def list_students(
    q: str | None = None,
    exclude_class_id: int | None = None,
    pagination: PaginationParams = Depends(pagination),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("teacher", "admin")),
):
    """学生候选列表——教师选择白名单学生用，只暴露 active student"""
    page, page_size = pagination.page, pagination.page_size
    from app.services.offering_service import scoped_student_ids
    filters = (User.role == "student", User.status == "active", User.id.in_(scoped_student_ids(db, _)))
    query = select(User).where(*filters)
    count_query = select(func.count()).select_from(User).where(*filters)
    if exclude_class_id:
        from app.models import TeachingClassStudent
        exclusion = select(TeachingClassStudent.student_id).where(
            TeachingClassStudent.teaching_class_id == exclude_class_id, TeachingClassStudent.status == 'active')
        query = query.where(User.id.not_in(exclusion))
        count_query = count_query.where(User.id.not_in(exclusion))
    if q:
        like = f"%{q}%"
        name_match = or_(
            User.username.ilike(like),
            User.real_name.ilike(like),
            User.student_no.ilike(like),
        )
        query = query.where(name_match)
        count_query = count_query.where(name_match)
    total = db.scalar(count_query) or 0
    users = db.scalars(
        query.order_by(User.id).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return PaginatedResponse(
        items=[UserRead.model_validate(user) for user in users],
        page=page, page_size=page_size, total=total,
    )


@router.get("/me/preferences", response_model=UserPreferencesRead)
def get_my_preferences(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.get(UserPreference, current_user.id)
    if row is None:
        return UserPreferencesRead(user_id=current_user.id, preferences={})
    return UserPreferencesRead(user_id=row.user_id, preferences=row.preferences or {})


@router.patch("/me/preferences", response_model=UserPreferencesRead)
def update_my_preferences(
    payload: UserPreferencesUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.get(UserPreference, current_user.id)
    if row is None:
        row = UserPreference(user_id=current_user.id, preferences={})
        db.add(row)
    updates = payload.model_dump(exclude_unset=True)
    row.preferences = {**(row.preferences or {}), **updates}
    db.commit()
    db.refresh(row)
    return UserPreferencesRead(user_id=row.user_id, preferences=row.preferences or {})


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    nickname: str | None = Field(default=None, max_length=80)


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    current_password: str
    reason: str = Field(min_length=1, max_length=500)
    password: str | None = None  # Only used for non-student targets.


@router.get('/me/profile')
def my_profile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models import TeachingClass, TeachingClassStudent
    result = UserRead.model_validate(user).model_dump()
    result['classes'] = [{'id': c.id, 'name': c.name, 'code': c.code} for c in db.scalars(
        select(TeachingClass).join(TeachingClassStudent).where(
            TeachingClassStudent.student_id == user.id, TeachingClassStudent.status == 'active')).all()]
    return result


@router.patch('/me/profile', response_model=UserRead)
def patch_profile(payload: ProfileUpdate, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    user.nickname = payload.nickname.strip() or None if payload.nickname else None
    db.commit()
    return user


@router.patch('/me/password', status_code=204)
def my_password(payload: PasswordUpdate, response: Response, db: Session = Depends(get_db),
                user: User = Depends(get_authenticated_user), settings: Settings = Depends(get_settings)):
    accounts.change_own_password(db, user, payload.password, payload.current_password, settings)
    db.commit()
    from app.api.auth import _delete_refresh_cookie
    _delete_refresh_cookie(response)


@router.post('/{user_id}/password-reset', response_model=UserRead)
def reset_password(user_id: int, payload: ResetRequest, request: Request, db: Session = Depends(get_db),
                   actor: User = Depends(require_roles('admin')), settings: Settings = Depends(get_settings),
                   redis=Depends(get_redis_client)):
    if user_id == actor.id:
        raise api_error(422, 'USE_SELF_PASSWORD', '请通过本人改密修改自己的密码')
    from app.api.account_management import verify_admin
    verify_admin(request, actor, payload.current_password, redis, settings, db)
    target = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not target:
        raise api_error(404, 'USER_NOT_FOUND', '用户不存在')
    if target.status != 'active':
        raise api_error(409, 'USER_NOT_ACTIVE', '不能重置禁用账号')
    from app.models import ExamSubmission
    if db.scalar(select(ExamSubmission.id).where(ExamSubmission.student_id == target.id,
                                                 ExamSubmission.status == 'started').limit(1)):
        raise api_error(409, 'EXAM_IN_PROGRESS', '学生正在考试，暂不重置')
    _legacy_password, days, _version = accounts.temporary_policy(db, settings)
    delivery = None
    if target.role == 'student':
        delivery, password = credentials.prepare(db, actor.id, settings, f'user:{target.id}', identity=target.username)
    else:
        password = payload.password or ''
    try:
        validate_password_rules(password, target.username)
    except ValueError as exc:
        raise api_error(422, 'PASSWORD_POLICY', str(exc))
    accounts.change_credentials(db, target, hash_password(password), temporary=True, days=days)
    if delivery:
        credentials.issue(db, delivery, target, settings)
    accounts.audit(db, actor.id, 'password.reset', 'user', target.id, reason=payload.reason)
    db.commit()
    return target


@router.get("/{user_id}", response_model=UserRead)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin" and current_user.id != user_id:
        raise api_error(403, "FORBIDDEN", "没有权限查看该用户")
    user = db.get(User, user_id)
    if not user:
        raise api_error(404, "USER_NOT_FOUND", "用户不存在")
    return user


@router.patch("/{user_id}", response_model=UserRead)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = accounts.update_identity(db, user_id, payload.model_dump(exclude_unset=True), current_user)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/{user_id}/password", response_model=UserRead)
def update_password(
    user_id: int,
    payload: PasswordUpdate,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
):
    if current_user.id != user_id:
        raise api_error(403, 'USE_PASSWORD_RESET', '请使用独立的管理员重置接口')
    user = accounts.change_own_password(db, current_user, payload.password, payload.current_password, settings)
    db.commit()
    return user


@router.patch("/{user_id}/status", response_model=UserRead)
def update_status(
    user_id: int,
    payload: StatusUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    user = accounts.update_identity(db, user_id, payload.model_dump(exclude_unset=True), _)
    db.commit()
    db.refresh(user)
    return user
