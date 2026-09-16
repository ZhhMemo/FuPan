"""SyncService：数据同步编排（元数据 / 日线 / 分钟 / 涨跌停 / 健康检查）。

- 首次全量初始化走独立 CLI（``scripts/init_data.py``）。
- 每日增量同步（21:00）作为 API 进程内 APScheduler 任务运行（同进程持单写者连接）。
- 失败必须可见：失败清单落 ``sync_failure``，并在返回结构中明确呈现。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.config import Settings, settings as default_settings
from app.core.db import DuckDBManager
from app.core.logging import get_logger
from app.core.timeutil import now_bj, today_bj
from app.data.models import SyncResult
from app.data.repository import Repository
from app.data.sync.baostock_client import BaostockClient
from app.data.sync.checkpoint import Checkpoint
from app.data.sync.health_check import HealthCheck
from app.data.sync.ingest_daily import ingest_daily
from app.data.sync.ingest_meta import ingest_calendar, ingest_dividend, ingest_index_daily, ingest_stock_list
from app.data.sync.ingest_minute import ingest_minute
from app.data.sync.precompute_limit import precompute_limit

log = get_logger(__name__)


class SyncService:
    """数据同步编排服务。"""

    def __init__(self, repository: Repository | None = None, config: Settings | None = None) -> None:
        self._cfg = config or default_settings
        self._repo = repository or Repository()
        self._mgr: DuckDBManager = self._repo.manager
        self._ckpt = Checkpoint(self._mgr)

    # ─────────────── 初始化 ───────────────
    def ensure_schema(self) -> None:
        """建出两库全部表（幂等）。"""
        self._mgr.init_schemas()

    # ─────────────── 元数据 ───────────────
    def ingest_meta(
        self,
        cal_start: str | None = None,
        cal_end: str | None = None,
        with_index: bool = True,
        with_dividend: bool = False,
        dividend_years: int = 30,
        client: BaostockClient | None = None,
    ) -> dict[str, int]:
        """落地股票列表 / 交易日历 / 指数 / 分红。"""
        cfg = self._cfg
        cal_start = cal_start or cfg.full_history_start
        cal_end = cal_end or (today_bj().replace(month=12, day=31).isoformat())
        counts: dict[str, int] = {}

        own = client is None
        cli = client or BaostockClient(cfg.baostock_reconnect_retries)
        if own:
            cli.login()
        try:
            counts["stocks"] = ingest_stock_list(cli, self._repo)
            counts["calendar"] = ingest_calendar(cli, self._repo, cal_start, cal_end)
            if with_index:
                counts["index"] = ingest_index_daily(
                    cli, self._repo, list(cfg.default_index_codes), cal_start, cal_end
                )
            if with_dividend:
                codes = self._repo.get_all_codes()
                counts["dividend"] = ingest_dividend(
                    cli, self._repo, codes, today_bj().year - dividend_years, today_bj().year
                )
        finally:
            if own:
                cli.logout()
        log.info("ingest_meta_done", **counts)
        return counts

    # ─────────────── 日线 ───────────────
    def ingest_daily(
        self,
        full: bool = False,
        codes: list[str] | None = None,
        start: Any = None,
        end: Any = None,
    ) -> SyncResult:
        """日线全量/增量采集。"""
        return ingest_daily(
            self._repo,
            codes=codes,
            full=full,
            start=start,
            end=end,
            checkpoint=self._ckpt,
            config=self._cfg,
        )

    # ─────────────── 分钟线 ───────────────
    def ingest_minute(self, codes: list[str], frequency: str = "5", resume: bool = True) -> SyncResult:
        """分钟线采集（支持断点续传）。"""
        return ingest_minute(
            self._repo,
            codes,
            frequency=frequency,
            checkpoint=self._ckpt if resume else None,
            config=self._cfg,
        )

    # ─────────────── 涨跌停 ───────────────
    def precompute_limit(self, codes: list[str] | None = None, start: Any = None, end: Any = None) -> SyncResult:
        """涨跌停价预计算。"""
        return precompute_limit(self._repo, codes=codes, start=start, end=end)

    # ─────────────── 每日增量（调度入口）───────────────
    def sync_daily(self, codes: list[str] | None = None, with_health: bool = True) -> dict[str, Any]:
        """每日增量同步（元数据 + 日线 + 涨跌停 + 健康检查）。

        Returns:
            结构化摘要（含各子任务结果与失败清单，失败不静默）。
        """
        started = now_bj()
        today = today_bj()
        log.info("sync_daily_start", date=today.isoformat())

        summary: dict[str, Any] = {"task": "sync_daily", "started_at": started.isoformat(), "date": today.isoformat()}

        # ① 元数据增量：刷新证券列表 + 近期/未来日历
        try:
            meta = self.ingest_meta(
                cal_start=(today - timedelta(days=120)).isoformat(),
                cal_end=(today + timedelta(days=400)).isoformat(),
                with_index=True,
            )
            summary["meta"] = meta
        except Exception as exc:  # noqa: BLE001
            summary["meta_error"] = str(exc)
            log.error("sync_daily_meta_failed", error=str(exc))

        # ② 日线增量
        daily = self.ingest_daily(full=False, codes=codes)
        summary["daily"] = daily.to_dict()

        # ③ 涨跌停增量（仅当天）
        limit = self.precompute_limit(codes=codes, start=today)
        summary["limit"] = limit.to_dict()

        # ④ 健康检查
        if with_health:
            report = HealthCheck(self._repo, self._cfg).run()
            summary["health"] = report.to_dict()

        finished = now_bj()
        summary["finished_at"] = finished.isoformat()
        failures = list(daily.failed) + list(limit.failed)
        summary["failures"] = failures[:50]
        summary["ok"] = (len(daily.failed) == 0 and len(limit.failed) == 0 and "meta_error" not in summary)
        if not summary["ok"]:
            log.warning("sync_daily_finished_with_failures", failed=len(failures))
            self._ckpt.record_failures(today, failures[:200])
        else:
            log.info("sync_daily_ok", ok=daily.ok, limit=limit.ok)
        return summary

    # ─────────────── 首次全量 ───────────────
    def init_all(
        self,
        full: bool = True,
        sample: int | None = None,
        with_dividend: bool = False,
        with_minute: bool = False,
        minute_frequency: str = "5",
    ) -> dict[str, Any]:
        """首次全量初始化（建议 API 未启动时执行）。

        Args:
            full: 是否全历史（否则仅占位）。
            sample: 仅初始化前 N 只证券（快速验证用）；None 表示全部。
            with_dividend: 是否拉取分红。
            with_minute: 是否拉取分钟线。
            minute_frequency: 分钟周期。
        """
        self.ensure_schema()
        summary: dict[str, Any] = {"task": "init_all"}

        meta = self.ingest_meta(with_dividend=with_dividend, with_index=True)
        summary["meta"] = meta

        codes = self._repo.get_all_codes()
        if sample is not None:
            codes = codes[:sample]
        summary["target_codes"] = len(codes)

        daily = self.ingest_daily(full=full, codes=codes)
        summary["daily"] = daily.to_dict()

        limit = self.precompute_limit(codes=codes)
        summary["limit"] = limit.to_dict()

        if with_minute and codes:
            minute = self.ingest_minute(codes, frequency=minute_frequency)
            summary["minute"] = minute.to_dict()

        report = HealthCheck(self._repo, self._cfg).run()
        summary["health"] = report.to_dict()
        summary["ok"] = len(daily.failed) == 0 and len(limit.failed) == 0
        return summary


def run_daily_sync(codes: list[str] | None = None) -> dict[str, Any]:
    """模块级入口：供 APScheduler 与 API 后台任务调用。"""
    service = SyncService()
    try:
        service.ensure_schema()
    except Exception as exc:  # noqa: BLE001 - 建表失败不应阻断，但必须可见
        log.error("run_daily_sync_ensure_schema_failed", error=str(exc))
    return service.sync_daily(codes=codes)


__all__ = ["SyncService", "run_daily_sync"]
