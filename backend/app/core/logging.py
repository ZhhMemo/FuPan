"""结构化日志（同步/结算/匹配可追溯）。

优先使用 ``structlog``；若环境缺少该依赖，则退化为标准库 ``logging``，
保证任何环境下日志都能输出（失败必须可见）。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

try:  # pragma: no cover - 取决于运行环境
    import structlog

    _HAS_STRUCTLOG = True
except Exception:  # pragma: no cover
    structlog = None  # type: ignore[assignment]
    _HAS_STRUCTLOG = False

_CONFIGURED = False
_DEFAULT_LEVEL = "INFO"


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """初始化日志系统（幂等）。"""
    global _CONFIGURED, _DEFAULT_LEVEL
    _DEFAULT_LEVEL = level.upper()

    root = logging.getLogger()
    root.setLevel(_DEFAULT_LEVEL)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)

    if _HAS_STRUCTLOG:
        processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
        ]
        if json_output:
            processors.append(structlog.processors.JSONRenderer(ensure_ascii=False))
        else:
            processors.append(structlog.dev.ConsoleRenderer(colors=False))
        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(_DEFAULT_LEVEL)),
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )

    _CONFIGURED = True


def get_logger(name: str | None = None) -> Any:
    """获取 logger；未初始化时自动以默认级别初始化。"""
    if not _CONFIGURED:
        configure_logging(_DEFAULT_LEVEL)
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name or "fupan")


def bind(**kwargs: Any) -> None:
    """向当前上下文绑定键值（仅 structlog 生效）。"""
    if _HAS_STRUCTLOG:
        structlog.contextvars.bind_contextvars(**kwargs)


__all__ = ["configure_logging", "get_logger", "bind"]
