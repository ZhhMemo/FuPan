"""依赖注入：仓储 / 认证服务 / 当前用户 / 可见截止点。

对齐设计 §8：**未登录一律 401**；行情类接口返回前必须过 ``VisibilityGuard``。
业务路由统一以 ``Depends(get_current_user)`` 作为全局鉴权依赖；
``/api/health`` 与 ``/api/auth/*`` 不挂该依赖。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import Header, Request

from app.core.errors import Unauthorized
from app.core.security import SecurityService
from app.data.repository import Repository

AUTH_COOKIE_NAME = "fupan_session"


@lru_cache(maxsize=1)
def get_security_service() -> SecurityService:
    """返回进程级认证服务单例。"""
    return SecurityService()


def get_repo() -> Repository:
    """返回仓储（每次新建，轻量）。"""
    return Repository()


def get_security() -> SecurityService:
    """依赖：认证服务。"""
    return get_security_service()


def _extract_token(request: Request, authorization: str | None) -> str | None:
    """从 ``Authorization: Bearer`` 或会话 Cookie 提取令牌。"""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    cookie = request.cookies.get(AUTH_COOKIE_NAME)
    return cookie or None


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    """依赖：解析当前登录用户；未登录 / 会话失效抛 401。"""
    token = _extract_token(request, authorization)
    if not token:
        raise Unauthorized("未登录：缺少访问令牌")
    username = get_security_service().resolve_session(token)
    if not username:
        raise Unauthorized("会话已失效，请重新登录")
    request.state.username = username
    request.state.token = token
    return username


def get_cutoff(question: dict[str, Any]) -> Any:
    """从题目推导可见截止点（判断模式 = ``end_date``）。"""
    return question.get("end_date") or question.get("visible_until")


__all__ = [
    "AUTH_COOKIE_NAME",
    "get_repo",
    "get_security",
    "get_security_service",
    "get_current_user",
    "get_cutoff",
]
