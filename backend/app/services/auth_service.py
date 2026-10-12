from datetime import timedelta

import redis as redis_lib
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import api_error
from app.models import User
from app.roles import is_supported_role
from app.schemas import TokenResponse
from app.security import create_token, decode_token, token_ttl_seconds, verify_password


def _user_rate_key(username: str) -> str:
    return f"rl:user:{username}"


def _ip_rate_key(ip: str) -> str:
    return f"rl:ip:{ip}"


def _redis_call(redis_client, method: str, *args):
    """Redis 访问统一错误语义：故障时登录返回 503，绝不造成永久锁定。"""
    try:
        return getattr(redis_client, method)(*args)
    except redis_lib.exceptions.RedisError:
        raise api_error(503, "SERVICE_UNAVAILABLE", "认证服务暂不可用，请稍后重试")


def check_login_rate_limits(redis_client, settings: Settings, username: str, ip: str) -> None:
    """登录前检查账户与 IP 双维限流；超限抛 429 并带 Retry-After。"""
    user_key = _user_rate_key(username)
    ip_key = _ip_rate_key(ip)
    user_failures = int(_redis_call(redis_client, "get", user_key) or 0)
    ip_attempts = int(_redis_call(redis_client, "get", ip_key) or 0)
    if user_failures >= settings.login_rate_limit_user_max_failures:
        raise api_error(
            429,
            "RATE_LIMITED",
            "失败次数过多，请稍后再试",
            headers={"Retry-After": str(_rate_retry_after(redis_client, user_key))},
        )
    if ip_attempts >= settings.login_rate_limit_ip_max_attempts:
        raise api_error(
            429,
            "RATE_LIMITED",
            "尝试次数过多，请稍后再试",
            headers={"Retry-After": str(_rate_retry_after(redis_client, ip_key))},
        )


def _rate_retry_after(redis_client, key: str) -> int:
    ttl = _redis_call(redis_client, "ttl", key)
    try:
        return int(ttl) if ttl and int(ttl) > 0 else 0
    except (TypeError, ValueError):
        return 0


def record_login_failure(redis_client, settings: Settings, username: str, ip: str) -> None:
    """记录一次失败：账户与 IP 计数器各 +1，首个计数开启窗口。"""
    for key in (_user_rate_key(username), _ip_rate_key(ip)):
        count = _redis_call(redis_client, "incr", key)
        if count == 1:
            _redis_call(redis_client, "expire", key, settings.login_rate_limit_window_seconds)


def reset_login_failures(redis_client, username: str) -> None:
    """成功登录后清除该账户的失败计数（IP 计数按窗口自然衰减）。"""
    _redis_call(redis_client, "delete", _user_rate_key(username))


def authenticate_user(db: Session, username: str, password: str) -> User:
    user = db.scalar(select(User).where(User.username == username))
    if not user or user.status != "active" or not verify_password(password, user.password_hash):
        raise api_error(401, "INVALID_CREDENTIALS", "用户名或密码错误")
    if not is_supported_role(user.role):
        raise api_error(403, "ROLE_NOT_SUPPORTED", "账号角色不受支持，请联系管理员")
    return user


def issue_token_pair(user: User, redis_client, settings: Settings) -> TokenResponse:
    from app.services.account_service import ensure_password_session
    ensure_password_session(user)
    if not is_supported_role(user.role):
        raise api_error(403, "ROLE_NOT_SUPPORTED", "账号角色不受支持，请联系管理员")
    access_token = create_token(
        subject=user.id,
        role=user.role,
        token_type="access",
        secret_key=settings.secret_key,
        algorithm=settings.algorithm,
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
        session_version=user.session_version,
    )
    refresh_token = create_token(
        subject=user.id,
        role=user.role,
        token_type="refresh",
        secret_key=settings.secret_key,
        algorithm=settings.algorithm,
        expires_delta=timedelta(days=settings.refresh_token_expire_days),
        session_version=user.session_version,
    )
    refresh_payload = decode_token(refresh_token, settings.secret_key, settings.algorithm)
    # 关键写入：Redis 故障必须 503，不能把"未登记可撤销会话"的 Token 发出去
    _redis_call(
        redis_client,
        "setex",
        f"refresh:{refresh_payload['jti']}",
        settings.refresh_token_expire_days * 24 * 60 * 60,
        str(user.id),
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=user,
    )


def refresh_token_pair(db: Session, refresh_token: str, redis_client, settings: Settings) -> TokenResponse:
    try:
        payload = decode_token(refresh_token, settings.secret_key, settings.algorithm)
    except ValueError:
        raise api_error(401, "INVALID_REFRESH_TOKEN", "刷新 Token 无效")
    if payload.get("type") != "refresh":
        raise api_error(401, "INVALID_TOKEN_TYPE", "Token 类型无效")
    refresh_key = f"refresh:{payload['jti']}"
    # 原子化消费 Refresh Token：GETDEL 同时读取并删除，防止并发重复使用。
    # 故障时结果未知（可能已删除也可能没删）——**不重放**，统一 503 让用户重新登录；
    # 不承诺网络故障时旧 refresh token 仍可用（安全优先于可用性）。
    user_id = _redis_call(redis_client, "getdel", refresh_key)
    if not user_id:
        raise api_error(401, "REFRESH_TOKEN_REVOKED", "刷新 Token 已失效")
    # user_id 是 bytes，需要解码
    if isinstance(user_id, bytes):
        user_id = user_id.decode()
    user = db.get(User, int(user_id))
    if not user or user.status != "active":
        raise api_error(401, "USER_NOT_ACTIVE", "用户不存在或已禁用")
    if not is_supported_role(user.role):
        raise api_error(403, "ROLE_NOT_SUPPORTED", "账号角色不受支持，请联系管理员")
    _ensure_session_version(payload, user)
    return issue_token_pair(user, redis_client, settings)


def _ensure_session_version(payload: dict, user: User) -> None:
    """Token 必须携带会话版本且与数据库一致；旧 Token 或改密/禁用后的 Token 立即失效。"""
    token_sv = payload.get("sv")
    if token_sv is None or int(token_sv) != user.session_version:
        raise api_error(401, "SESSION_REVOKED", "会话已失效，请重新登录")


def revoke_tokens(
    access_payload: dict | None,
    refresh_token: str | None,
    redis_client,
    settings: Settings,
) -> bool:
    """撤销同一会话的令牌；返回是否应清除请求携带的 refresh cookie。"""
    jti = access_payload.get("jti") if access_payload else None
    if jti:
        # 退出登录：写黑名单失败必须 503，不能假装"已退出"却让 Token 继续可用
        _redis_call(
            redis_client, "setex",
            f"blacklist:{jti}", max(token_ttl_seconds(access_payload), 1), "1",
        )
    if refresh_token:
        try:
            refresh_payload = decode_token(refresh_token, settings.secret_key, settings.algorithm)
        except ValueError:
            return True
        access_subject = access_payload.get("sub") if access_payload else None
        refresh_subject = refresh_payload.get("sub")
        if access_subject and refresh_subject and access_subject != refresh_subject:
            return False
        _redis_call(redis_client, "delete", f"refresh:{refresh_payload.get('jti')}")
    return True
