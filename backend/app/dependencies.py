from collections.abc import Callable

import redis
from fastapi import Depends, Query, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import get_db_session
from .errors import api_error
from .models import User
from .roles import is_supported_role
from .security import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


class PaginationParams:
    """统一分页契约（TASK-021 / F-24）：page >= 1，1 <= page_size <= 100。

    非法值由 FastAPI Query 校验统一返回 422；响应结构不变。
    """

    def __init__(self, page: int, page_size: int):
        self.page = page
        self.page_size = page_size


def pagination(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


def get_db():
    yield from get_db_session()


def get_redis_client(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Return the app-scoped Redis client, lazily initialized for non-lifespan callers.

    客户端契约见 ``app/redis_client.py``：连接/读取各 0.5 秒、零自动重试、
    连接池上限 32。lifespan 未运行时（测试/脚本）用同一工厂惰性创建。
    """
    from app.redis_client import build_api_redis

    client = getattr(request.app.state, "redis_client", None)
    if client is None:
        client = build_api_redis(settings)
        request.app.state.redis_client = client
    return client


def get_current_payload(
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
    redis_client=Depends(get_redis_client),
) -> dict:
    try:
        payload = decode_token(token, settings.secret_key, settings.algorithm)
    except ValueError:
        raise api_error(401, "INVALID_TOKEN", "Token 无效")
    if payload.get("type") != "access":
        raise api_error(401, "INVALID_TOKEN_TYPE", "Token 类型无效")
    jti = payload.get("jti")
    if jti:
        # 认证关键读取：Redis 故障必须拒绝放行（503），绝不能当成"令牌不存在"。
        try:
            revoked = redis_client.exists(f"blacklist:{jti}")
        except redis.exceptions.RedisError:
            raise api_error(503, "SERVICE_UNAVAILABLE", "认证服务暂不可用，请稍后重试")
        if revoked:
            raise api_error(401, "TOKEN_REVOKED", "Token 已失效")
    payload["_raw_token"] = token
    return payload


def get_authenticated_user(
    payload: dict = Depends(get_current_payload),
    db: Session = Depends(get_db),
    redis_client=Depends(get_redis_client),
) -> User:
    user_id = payload.get("sub")
    user = db.get(User, int(user_id)) if user_id else None
    if not user or user.status != "active":
        raise api_error(401, "USER_NOT_ACTIVE", "用户不存在或已禁用")
    if not is_supported_role(user.role):
        raise api_error(403, "ROLE_NOT_SUPPORTED", "账号角色不受支持，请联系管理员")
    token_sv = payload.get("sv")
    if token_sv is None or int(token_sv) != user.session_version:
        raise api_error(401, "SESSION_REVOKED", "会话已失效，请重新登录")
    # A long-running request must also notice a single-token logout before
    # committing completion metadata. Never retain the raw bearer token.
    db.info['teaching_token_jti'] = payload.get('jti')
    db.info['teaching_token_redis'] = redis_client
    return user


def get_current_user(user: User = Depends(get_authenticated_user), db: Session = Depends(get_db), request: Request = None) -> User:
    from app.services.account_service import ensure_full_account
    ensure_full_account(user)
    from app.services.teaching_query_policy import install_request_scope
    install_request_scope(db, user)
    if request is not None:
        db.info["teaching_request_method"] = request.method
    return user


def require_roles(*roles: str) -> Callable:
    def dependency(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise api_error(403, "FORBIDDEN", "没有权限执行该操作")
        return current_user

    return dependency
