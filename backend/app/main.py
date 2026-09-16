"""FastAPI 入口：中间件、路由挂载、APScheduler 启动、全局异常处理。

- APScheduler 跑在 **API 进程内**（规避 DuckDB 跨进程写冲突），
  时区**显式指定** ``Asia/Shanghai``，每日 21:00 触发增量同步。
- 全局异常处理器把业务异常统一转为 ``{code, message}``。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import admin
from app.config import TZ, settings
from app.core.db import get_manager
from app.core.errors import FupanError
from app.core.logging import configure_logging, get_logger
from app.core.timeutil import now_bj
from app.data.sync.service import run_daily_sync

log = get_logger(__name__)

SYNC_JOB_ID = "daily_sync"


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
    """应用生命周期：初始化日志/目录/表结构 + 启动调度器。"""
    configure_logging(settings.log_level, settings.log_json)
    settings.ensure_dirs()

    try:
        get_manager().init_schemas()
    except Exception as exc:  # noqa: BLE001 - 建表失败必须可见但不阻断启动
        log.error("schema_init_failed", error=str(exc))

    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    log.info(
        "app_started",
        tz=str(TZ),
        sync_time=f"{settings.sync_hour:02d}:{settings.sync_minute:02d}",
        data_root=str(settings.data_root),
    )
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        log.info("app_stopped")


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="复盘 · A 股历史决策训练平台（M0 数据底座）",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# ─────────────── 基础健康探针 ───────────────
@app.get("/api/health")
def health() -> dict[str, object]:
    """服务健康探针（返回 200）。"""
    return {"code": 0, "data": {"status": "ok", "time": now_bj().isoformat(), "tz": str(TZ)}, "message": "ok"}


app.include_router(admin.router)


__all__ = ["app", "build_scheduler"]
