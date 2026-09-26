from ipaddress import ip_address, ip_network

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.dependencies import get_authenticated_user, get_current_user, get_db, get_redis_client
from app.errors import api_error
from app.models import User
from app.schemas import LoginRequest, LogoutRequest, RefreshRequest, TokenResponse, UserRead
from app.security import decode_token
from app.services.auth_service import (
    authenticate_user,
    check_login_rate_limits,
    issue_token_pair,
    record_login_failure,
    refresh_token_pair,
    reset_login_failures,
    revoke_tokens,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Cookie 配置
REFRESH_COOKIE_KEY = "dai_refresh_token"
REFRESH_COOKIE_PATH = "/api/v1/auth"


def _validate_origin(request: Request, settings: Settings) -> None:
    """校验请求 Origin 在允许列表中。无 Origin 头（同源请求）放行。"""
    origin = request.headers.get("Origin", "")
    if not origin:
        return  # 同源请求没有 Origin 头，放行
    allowed = settings.cors_origin_list
    if origin not in allowed:
        raise api_error(403, "ORIGIN_NOT_ALLOWED", f"来源 {origin} 不被允许")


def _set_refresh_cookie(response: Response, refresh_token: str, settings: Settings, max_age_days: int = 7):
    """设置 HttpOnly refresh token Cookie"""
    secure = settings.environment == "production"
    response.set_cookie(
        key=REFRESH_COOKIE_KEY,
        value=refresh_token,
        max_age=max_age_days * 86400,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def _delete_refresh_cookie(response: Response):
    """删除 refresh token Cookie"""
    response.delete_cookie(
        key=REFRESH_COOKIE_KEY,
        path=REFRESH_COOKIE_PATH,
    )


def _logout_access_payload(request: Request, settings: Settings) -> dict | None:
    """尽力解析 access token；退出本身不能依赖一个仍在有效期内的 access token。"""
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        payload = decode_token(
            token,
            settings.secret_key,
            settings.algorithm,
            verify_exp=False,
        )
    except ValueError:
        return None
    return payload if payload.get("type") == "access" else None


def _client_ip(request: Request, settings: Settings) -> str:
    """Resolve the client IP only across an explicitly trusted proxy chain.

    The immediate ASGI peer must be allow-listed. Every XFF hop after the
    left-most client address must also be allow-listed; malformed or unknown
    chains fail closed to the direct peer rather than trusting user input.
    """
    peer = request.client.host if request.client else "unknown"
    trusted_tokens = {
        item.strip()
        for item in settings.trusted_proxy_cidrs.split(",")
        if item.strip()
    }

    def is_trusted(host: str) -> bool:
        if host in trusted_tokens:
            return True
        try:
            address = ip_address(host)
        except ValueError:
            return False
        for token in trusted_tokens:
            try:
                if address in ip_network(token, strict=False):
                    return True
            except ValueError:
                continue
        return False

    if not is_trusted(peer):
        return peer
    forwarded = request.headers.get("X-Forwarded-For", "")
    if not forwarded:
        return peer
    hops = [item.strip() for item in forwarded.split(",")]
    if not hops or any(not item for item in hops):
        return peer
    try:
        for item in hops:
            ip_address(item)
    except ValueError:
        return peer
    if all(is_trusted(item) for item in hops[1:]):
        return hops[0]
    return peer


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    redis_client=Depends(get_redis_client),
    settings: Settings = Depends(get_settings),
):
    username = payload.username or ""
    rate_username = username.strip().lower()
    client_ip = _client_ip(request, settings)
    check_login_rate_limits(redis_client, settings, rate_username, client_ip)
    try:
        user = authenticate_user(db, username, payload.password)
    except HTTPException as exc:
        if exc.status_code == 401:
            record_login_failure(redis_client, settings, rate_username, client_ip)
        raise
    reset_login_failures(redis_client, rate_username)
    tokens = issue_token_pair(user, redis_client, settings)
    # Refresh token 仅存入 HttpOnly Cookie，不在 JSON body 返回
    _set_refresh_cookie(response, tokens.refresh_token, settings)
    return {
        "access_token": tokens.access_token,
        "token_type": "bearer",
        "expires_in": tokens.expires_in,
        "user": tokens.user,
    }


@router.post("/refresh")
def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    dai_refresh_token: str | None = Cookie(None, alias=REFRESH_COOKIE_KEY),
    db: Session = Depends(get_db),
    redis_client=Depends(get_redis_client),
    settings: Settings = Depends(get_settings),
):
    # Origin 校验：防止跨域 refresh 攻击
    _validate_origin(request, settings)

    # 优先从 Cookie 读取，兼容 JSON body
    refresh_token_value = dai_refresh_token or (payload.refresh_token if payload else None)
    if not refresh_token_value:
        raise api_error(401, "NO_REFRESH_TOKEN", "缺少刷新令牌")

    tokens = refresh_token_pair(db, refresh_token_value, redis_client, settings)
    _set_refresh_cookie(response, tokens.refresh_token, settings)
    return {
        "access_token": tokens.access_token,
        "token_type": "bearer",
        "expires_in": tokens.expires_in,
        "user": tokens.user,
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    payload: LogoutRequest | None = None,
    dai_refresh_token: str | None = Cookie(None, alias=REFRESH_COOKIE_KEY),
    redis_client=Depends(get_redis_client),
    settings: Settings = Depends(get_settings),
):
    # Origin 校验：防止跨域 logout 攻击
    _validate_origin(request, settings)
    access_payload = _logout_access_payload(request, settings)
    clear_refresh_cookie = revoke_tokens(
        access_payload,
        dai_refresh_token or (payload.refresh_token if payload else None),
        redis_client,
        settings,
    )
    if clear_refresh_cookie:
        _delete_refresh_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return None


@router.get("/me", response_model=UserRead)
def me(current_user: User = Depends(get_authenticated_user)):
    return current_user
