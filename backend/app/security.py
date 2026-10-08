from datetime import UTC, datetime, timedelta
from math import isfinite
from uuid import uuid4

import jwt
from jwt import PyJWTError
from passlib.context import CryptContext


password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _require_hmac_algorithm(algorithm: str) -> None:
    # This application issues shared-secret JWTs. Never derive an accepted
    # algorithm from token headers or mix HMAC and asymmetric key families.
    if algorithm not in ("HS256", "HS384", "HS512"):
        raise ValueError("Unsupported token algorithm")


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_context.verify(password, password_hash)


def validate_password_rules(password: str, username: str | None = None) -> None:
    """校验所有密码入口共享的 TASK-011 边界。"""
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    if len(password.encode("utf-8")) > 72:
        raise ValueError("密码 UTF-8 字节数超过上限 72")
    if not password.strip():
        raise ValueError("密码不能为全空白")
    if username is not None and password.strip().casefold() == username.strip().casefold():
        raise ValueError("密码不能与用户名相同")


def create_token(
    *,
    subject: int,
    role: str,
    token_type: str,
    secret_key: str,
    algorithm: str,
    expires_delta: timedelta,
    session_version: int,
) -> str:
    _require_hmac_algorithm(algorithm)
    expires_at = datetime.now(UTC) + expires_delta
    payload = {
        "sub": str(subject),
        "role": role,
        "type": token_type,
        "jti": str(uuid4()),
        "exp": expires_at,
        "sv": session_version,
    }
    return jwt.encode(payload, secret_key, algorithm=algorithm)


def decode_token(
    token: str,
    secret_key: str,
    algorithm: str,
    *,
    verify_exp: bool = True,
) -> dict:
    _require_hmac_algorithm(algorithm)
    try:
        payload = jwt.decode(
            token,
            secret_key,
            algorithms=[algorithm],
            options={"verify_exp": verify_exp, "verify_signature": True,
                     "require": ["sub", "exp", "type", "jti"]},
        )
    except (PyJWTError, ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid token") from exc
    subject = payload["sub"]
    expiry = payload["exp"]
    if (not subject.isascii() or not subject.isdecimal() or int(subject) <= 0
            or type(expiry) not in (int, float) or (type(expiry) is float and not isfinite(expiry))
            or not payload["jti"] or not isinstance(payload["type"], str)
            or payload["type"] not in ("access", "refresh")
            or ("sv" in payload and (type(payload["sv"]) is not int or payload["sv"] < 1))):
        raise ValueError("Invalid token claims")
    return payload


def token_ttl_seconds(payload: dict) -> int:
    exp = payload.get("exp")
    if not exp:
        return 0
    return max(int(exp - datetime.now(UTC).timestamp()), 0)
