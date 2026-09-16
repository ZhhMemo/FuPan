"""认证接口（FR-8，P0）。

- ``POST /api/auth/login``  登录（失败限速；密码慢哈希校验）
- ``POST /api/auth/logout`` 登出（会话立即失效）
- ``GET  /api/auth/me``     会话校验

安全（N3）：密码慢哈希、会话令牌、登录限速；令牌通过 ``Authorization: Bearer``
或 ``HttpOnly`` Cookie 传递（双通道，前端默认用 Bearer）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import AUTH_COOKIE_NAME, get_current_user, get_security
from app.api.schemas import LoginRequest, envelope
from app.config import settings
from app.core.security import SecurityService

router = APIRouter(prefix="/api/auth", tags=["auth"])

_COOKIE_MAX_AGE = settings.session_ttl_hours * 3600


@router.post("/login")
def login(
    payload: LoginRequest,
    response: Response,
    security: SecurityService = Depends(get_security),
) -> dict:
    """登录：校验密码（慢哈希 + 限速），签发会话并下发 HttpOnly Cookie。"""
    token, expires_at = security.verify_login(payload.username, payload.password)
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.force_https,
        path="/",
    )
    return envelope(
        {"token": token, "expires_at": expires_at.isoformat(), "username": payload.username},
        "登录成功",
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    security: SecurityService = Depends(get_security),
) -> dict:
    """登出：使当前会话令牌立即失效，并清除 Cookie。"""
    token = getattr(request.state, "token", None) or request.cookies.get(AUTH_COOKIE_NAME)
    if token:
        security.revoke_session(token)
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return envelope({}, "已登出")


@router.get("/me")
def me(username: str = Depends(get_current_user)) -> dict:
    """会话校验：返回当前登录用户名。"""
    return envelope({"username": username})


__all__ = ["router"]
