"""统一错误类型与错误码。

约定（对齐设计 §21 错误处理）：
- 统一抛本模块定义的类型；
- 全局异常处理器（``app.main``）将其转换为 ``{code, message}``；
- 同步/结算/匹配失败必须落日志且可追溯，**不静默**。
"""

from __future__ import annotations

from typing import Any


class FupanError(Exception):
    """项目所有业务异常的基类。"""

    code: int = 500
    default_message: str = "内部错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: int | None = None,
        detail: Any = None,
    ) -> None:
        self.message: str = message or self.default_message
        if code is not None:
            self.code = code
        self.detail: Any = detail
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message, "data": None}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class ValidationError(FupanError):
    """入参校验失败。"""

    code = 400
    default_message = "参数校验失败"


class Unauthorized(FupanError):
    """未登录 / 会话失效。"""

    code = 401
    default_message = "未登录或会话已失效"


class Forbidden(FupanError):
    """无权限。"""

    code = 403
    default_message = "无权限访问"


class NotFound(FupanError):
    """资源不存在。"""

    code = 404
    default_message = "资源不存在"


class Conflict(FupanError):
    """状态冲突（例如重复下单、锁被占用）。"""

    code = 409
    default_message = "状态冲突"


class LockUnavailable(Conflict):
    """拿不到 DuckDB 写锁（明确报错，不留"以为成功了"的歧义）。"""

    code = 409
    default_message = "无法获取数据库写锁"


class DataUnavailable(FupanError):
    """数据不可得（未初始化/数据源不可用/区间无数据）。"""

    code = 503
    default_message = "数据不可用"


class SyncError(FupanError):
    """同步任务失败（必须可见）。"""

    code = 500
    default_message = "数据同步失败"


class VisibilityViolation(FupanError):
    """可见性守卫被击穿：数据中存在超过 cutoff 的未来数据（红线①）。"""

    code = 500
    default_message = "检测到未来数据泄漏（可见性守卫）"


__all__ = [
    "FupanError",
    "ValidationError",
    "Unauthorized",
    "Forbidden",
    "NotFound",
    "Conflict",
    "LockUnavailable",
    "DataUnavailable",
    "SyncError",
    "VisibilityViolation",
]
