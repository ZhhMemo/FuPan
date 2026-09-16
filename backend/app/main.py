"""FastAPI 入口：中间件、路由挂载、APScheduler 启动、全局异常处理。

- APScheduler 跑在 **API 进程内**（规避 DuckDB 跨进程写冲突），
  时区**显式指定** ``Asia/Shanghai``，每日 21:00 触发增量同步。
- 全局异常处理器把业务异常统一转为 ``{code, message}``。
- 安全（N3）：全站 HTTPS（可开关）+ 安全响应头（CSP 等）+ CSRF 守卫；
  业务路由统一挂 ``get_current_user``（未登录一律 401）；
  TOTP 双因素**预留未启用**。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

from app import __version__
from app.api import admin, auth, question, settle, trade
from app.api.deps import AUTH_COOKIE_NAME, get_current_user, get_security_service
from app.config import TZ, settings
from app.core.db import get_manager
from app.core.errors import FupanError
from app.core.logging import configure_logging, get_logger
from app.core.timeutil import now_bj
from app.data.sync.service import run_daily_sync

log = get_logger(__name__)

SYNC_JOB_ID = "daily_sync"

# 状态改变型方法（需 CSRF 防护 / 需登录）
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def build_scheduler() -> BackgroundScheduler:
    """构建进程内调度器（显式 Asia/Shanghai 时区，每日 21:00）。"""
    scheduler = BackgroundScheduler(timezone=str(TZ))
    scheduler.add_job(
        run_daily_sync,
        trigger=CronTrigger(hour=settings.sync_hour, minute=settings.sync_minute, timezone=str(TZ)),
        id=SYNC_JOB_ID,
        name="每日增量同步",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    return scheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：初始化日志/目录/表结构 + 引导账号 + 启动调度器。"""
    configure_logging(settings.log_level, settings.log_json)
    settings.ensure_dirs()

    try:
        get_manager().init_schemas()
    except Exception as exc:  # noqa: BLE001 - 建表失败必须可见但不阻断启动
        log.error("schema_init_failed", error=str(exc))

    # 认证引导：建默认账号 + 清理过期会话（失败可见但不阻断）
    try:
        sec = get_security_service()
        sec.ensure_default_user()
        sec.purge_expired()
    except Exception as exc:  # noqa: BLE001
        log.error("auth_bootstrap_failed", error=str(exc))

    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    log.info(
        "app_started",
        tz=str(TZ),
        sync_time=f"{settings.sync_hour:02d}:{settings.sync_minute:02d}",
        data_root=str(settings.data_root),
        force_https=settings.force_https,
    )
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        log.info("app_stopped")


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="复盘 · A 股历史决策训练平台（M1：单题闭环）",
    lifespan=lifespan,
)

# ─────────────── 中间件 ───────────────
# ① HTTPS 强制（生产开启：TLS 终止于 nginx，此处兜底跳转 + HSTS）
if settings.force_https:
    app.add_middleware(HTTPSRedirectMiddleware)

# ② CORS（仅允许配置的来源，携带凭证）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """注入安全响应头（CSP / nosniff / frame / referrer / HSTS）。"""
    response = await call_next(request)
    if settings.security_headers:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if settings.force_https:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.middleware("http")
async def csrf_guard_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """CSRF 守卫：Cookie 认证 + 状态改变方法时，要求 ``X-Requested-With`` 头。

    Bearer 令牌认证（前端默认）天然免疫 CSRF，故仅对「使用会话 Cookie 且无 Bearer」的
    不安全方法做校验；登录接口与已带 Bearer 的请求不受影响。
    """
    if request.method in _UNSAFE_METHODS:
        has_bearer = (request.headers.get("authorization") or "").lower().startswith("bearer ")
        has_cookie = bool(request.cookies.get(AUTH_COOKIE_NAME))
        xrw = (request.headers.get("x-requested-with") or "").lower()
        if has_cookie and not has_bearer and xrw != "xmlhttprequest":
            log.warning("csrf_blocked", path=str(request.url.path), method=request.method)
            return JSONResponse(
                status_code=403,
                content={"code": 403, "message": "CSRF 校验失败：缺少 X-Requested-With 头", "data": None},
            )
    return await call_next(request)


# ─────────────── 全局异常处理 ───────────────
@app.exception_handler(FupanError)
async def _fupan_error_handler(request: Request, exc: FupanError) -> JSONResponse:
    status = exc.code if 400 <= exc.code <= 599 else 500
    log.warning("fupan_error", code=exc.code, message=exc.message, path=str(request.url.path))
    return JSONResponse(status_code=status, content=exc.to_dict())


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"code": 400, "message": "参数校验失败", "data": None, "detail": exc.errors()},
    )


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", error=str(exc), path=str(request.url.path))
    return JSONResponse(status_code=500, content={"code": 500, "message": "内部错误", "data": None})


# ─────────────── 基础健康探针（免鉴权）───────────────
@app.get("/api/health")
def health() -> dict[str, object]:
    """服务健康探针（返回 200）。"""
    return {"code": 0, "data": {"status": "ok", "time": now_bj().isoformat(), "tz": str(TZ)}, "message": "ok"}


# ─────────────── 路由挂载 ───────────────
# 认证接口免鉴权（登录本身不能要求已登录）
app.include_router(auth.router)

# 业务接口统一鉴权（未登录一律 401）
_auth = [Depends(get_current_user)]
app.include_router(admin.router, dependencies=_auth)
app.include_router(question.router, dependencies=_auth)
app.include_router(question.stocks_router, dependencies=_auth)
app.include_router(trade.router, dependencies=_auth)
app.include_router(settle.router, dependencies=_auth)


__all__ = ["app", "build_scheduler"]
