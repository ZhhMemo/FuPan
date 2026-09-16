"""系统管理接口（M0：同步触发 / 进度 / 健康检查 / 参数 / 训练数据备份）。

对齐设计 §8.7：
- ``POST /api/admin/sync/daily``  手动触发增量同步
- ``GET  /api/admin/sync/status`` 查询同步状态
- ``GET  /api/admin/health``      健康检查报告（P0）
- ``GET/PUT /api/admin/params``   参数读取 / 更新
- ``POST /api/admin/backup``      训练数据备份 + **恢复验证**（FR-8.7，P0）

注：M0 阶段**未加鉴权**（认证属 M1）；M1 将在这些路由上挂全局鉴权依赖。
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks

from app.config import settings
from app.core.errors import Conflict, ValidationError
from app.core.logging import get_logger
from app.core.timeutil import now_bj
from app.data.backup import TrainingDataBackup
from app.data.repository import Repository
from app.data.sync.health_check import HealthCheck
from app.data.sync.service import run_daily_sync

log = get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 同步任务状态（进程内；单实例）
_sync_state: dict[str, Any] = {
    "running": False,
    "job_id": None,
    "last_started": None,
    "last_finished": None,
    "last_result": None,
    "last_error": None,
}
_state_lock = threading.Lock()

# 可配置参数键（对齐 §21 成本模型「费率全部可配置」+ FR-4.8 成交口径 + `00` §7 开关）
_PARAM_KEYS = [
    "commission_rate",
    "min_commission",
    "stamp_tax_rate",
    "transfer_fee_rate",
    "regulation_fee_rate",
    "slippage_rate",
    "fee_version",
    "fill_price_source",
    "open_seal_no_buy",
    "sync_hour",
    "sync_minute",
    "settle_window",
]


def _envelope(data: Any, message: str = "ok") -> dict[str, Any]:
    """统一响应格式 ``{code, data, message}``。"""
    return {"code": 0, "data": data, "message": message}


# ─────────────── 同步 ───────────────
def _run_sync_job(job_id: str) -> None:
    """后台执行一次增量同步。"""
    try:
        result = run_daily_sync()
        with _state_lock:
            _sync_state["last_result"] = result
            _sync_state["last_error"] = None
    except Exception as exc:  # noqa: BLE001 - 失败必须可见
        log.error("admin_sync_failed", error=str(exc))
        with _state_lock:
            _sync_state["last_error"] = str(exc)
    finally:
        with _state_lock:
            _sync_state["running"] = False
            _sync_state["last_finished"] = now_bj().isoformat()
            _sync_state["job_id"] = None


@router.post("/sync/daily")
def trigger_daily_sync(background: BackgroundTasks) -> dict[str, Any]:
    """手动触发一次增量同步（异步执行）。"""
    with _state_lock:
        if _sync_state["running"]:
            raise Conflict("已有同步任务在运行")
        job_id = uuid.uuid4().hex
        _sync_state.update(
            running=True,
            job_id=job_id,
            last_started=now_bj().isoformat(),
            last_error=None,
        )
    background.add_task(_run_sync_job, job_id)
    return _envelope({"job_id": job_id, "status": "started"})


@router.get("/sync/status")
def sync_status() -> dict[str, Any]:
    """查询同步任务状态。"""
    with _state_lock:
        snapshot = dict(_sync_state)
    repo = Repository()
    snapshot["failures"] = _recent_failures(repo)
    return _envelope(snapshot)


def _recent_failures(repo: Repository, limit: int = 50) -> list[dict[str, str]]:
    """读取最近失败清单（供状态接口展示）。"""
    try:
        con = repo.manager.get_read("app")
        rows = con.execute(
            "SELECT batch_date, code, reason FROM sync_failure ORDER BY batch_date DESC, code LIMIT ?",
            [limit],
        ).fetchall()
        return [{"batch_date": str(r[0]), "code": r[1], "reason": r[2]} for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("read_failures_failed", error=str(exc))
        return []


# ─────────────── 健康检查 ───────────────
@router.get("/health")
def health() -> dict[str, Any]:
    """数据健康检查报告（P0）。"""
    report = HealthCheck().run()
    return _envelope(report.to_dict())


# ─────────────── 参数 ───────────────
@router.get("/params")
def get_params() -> dict[str, Any]:
    """读取当前参数（app_settings 覆盖值优先，否则用默认值）。"""
    repo = Repository()
    params: dict[str, Any] = {}
    for key in _PARAM_KEYS:
        override = repo.get_setting(f"param.{key}")
        params[key] = override if override is not None else str(getattr(settings, key, ""))
    return _envelope(params)


@router.put("/params")
def put_params(payload: dict[str, Any]) -> dict[str, Any]:
    """更新参数（仅接受白名单键，禁止未知参数）。"""
    if not isinstance(payload, dict) or not payload:
        raise ValidationError("请求体必须为非空对象")
    unknown = [k for k in payload if k not in _PARAM_KEYS]
    if unknown:
        raise ValidationError(f"未知参数：{unknown}", detail={"allowed": _PARAM_KEYS})
    repo = Repository()
    for key, value in payload.items():
        repo.set_setting(f"param.{key}", str(value))
    return _envelope({"updated": list(payload.keys())})


# ─────────────── 训练数据备份（FR-8.7，P0）───────────────
@router.post("/backup")
def backup_training_data(label: str | None = None) -> dict[str, Any]:
    """备份 ``app.duckdb`` 并**恢复验证**（真导入临时库 + 逐表比对行数）。

    训练数据不可再生（`05` §安全 / R9），备份必须**验证过能恢复**才算数。
    """
    report = TrainingDataBackup(Repository()).backup_and_verify(label)
    message = "备份完成且恢复验证通过" if report.ok else "备份完成但恢复验证未通过（请检查）"
    log.info("admin_backup_done", ok=report.ok, backup_dir=report.backup_dir)
    return _envelope(report.to_dict(), message)


@router.get("/backup/list")
def list_backups() -> dict[str, Any]:
    """列出已有训练数据备份（目录名 + 大小 + 时间）。"""
    backup_root = settings.backup_path
    items: list[dict[str, Any]] = []
    if backup_root.exists():
        for d in sorted((p for p in backup_root.glob("app_*") if p.is_dir()), key=lambda p: p.name, reverse=True):
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            items.append(
                {
                    "name": d.name,
                    "path": str(d),
                    "size_bytes": size,
                    "has_schema": (d / "schema.sql").exists(),
                }
            )
    return _envelope({"items": items, "total": len(items), "backup_dir": str(backup_root)})


__all__ = ["router"]
