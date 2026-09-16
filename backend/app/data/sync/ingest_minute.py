"""分钟线采集（5/15/30/60）+ 断点续传。

- 供 M5 回放模式使用；单股票按需 / 全股票预下载。
- 断点续传：checkpoint 键为 ``"{code}#{frequency}"``，逐只记录已下载到的日期。
- Baostock 分钟数据可用深度有限，缺失区间不报错，仅记录即可。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.config import Settings
from app.config import settings as default_settings
from app.core.logging import get_logger
from app.core.timeutil import now_bj, parse_date, today_bj
from app.data.models import SyncResult
from app.data.repository import Repository
from app.data.sync.baostock_client import BaostockClient
from app.data.sync.checkpoint import Checkpoint
from app.data.sync.ingest_meta import save_raw

log = get_logger(__name__)

SUPPORTED_FREQ = {"5", "15", "30", "60"}


def checkpoint_key(code: str, frequency: str) -> str:
    """分钟线 checkpoint 键。"""
    return f"{code}#{frequency}"


def _numeric(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype="float64")
    cleaned = series.astype("string").str.strip().replace({"": None})
    return pd.to_numeric(cleaned, errors="coerce")


def build_minute_frame(code: str, raw: pd.DataFrame) -> pd.DataFrame:
    """把 Baostock 分钟线转换为 ``fact_minute`` DataFrame。"""
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    if "time" in df.columns:
        dt = pd.to_datetime(df["time"].astype(str), format="%Y%m%d%H%M%S%f", errors="coerce")
    else:
        dt = pd.to_datetime(df.get("date"), errors="coerce")
    out = pd.DataFrame(
        {
            "code": code,
            "dt": dt,
            "open": _numeric(df.get("open")),
            "high": _numeric(df.get("high")),
            "low": _numeric(df.get("low")),
            "close": _numeric(df.get("close")),
            "volume": _numeric(df.get("volume")).fillna(0).astype("int64"),
            "amount": _numeric(df.get("amount")).fillna(0.0),
        }
    )
    return out.dropna(subset=["dt", "close"]).reset_index(drop=True)


def ingest_minute(
    repo: Repository,
    codes: list[str],
    frequency: str = "5",
    start: date | str | None = None,
    end: date | str | None = None,
    checkpoint: Checkpoint | None = None,
    client: BaostockClient | None = None,
    config: Settings | None = None,
) -> SyncResult:
    """分钟线采集（支持断点续传）。

    Args:
        repo: 仓储。
        codes: 证券列表。
        frequency: ``5`` / ``15`` / ``30`` / ``60``。
        start: 起始日期；None 则从 checkpoint 续传或默认近 30 个自然日。
        end: 结束日期；默认今天。
        checkpoint: 断点续传。
        client: 可复用客户端。
        config: 配置。
    """
    cfg = config or default_settings
    freq = str(frequency)
    if freq not in SUPPORTED_FREQ:
        raise ValueError(f"不支持的分钟周期：{frequency}（应为 {sorted(SUPPORTED_FREQ)}）")

    result = SyncResult(task=f"ingest_minute_{freq}", started_at=now_bj())
    end_date = parse_date(end) if end is not None else today_bj()
    explicit_start = parse_date(start) if start is not None else None

    own_client = client is None
    cli = client or BaostockClient(cfg.baostock_reconnect_retries)
    if own_client:
        cli.login()

    try:
        for code in codes:
            try:
                s = explicit_start
                if s is None and checkpoint is not None:
                    last = checkpoint.last_date_of(checkpoint_key(code, freq))
                    if last is not None:
                        s = last + pd.Timedelta(days=1)
                if s is None:
                    s = end_date - pd.Timedelta(days=30)
                if s > end_date:
                    continue
                s_iso, e_iso = pd.Timestamp(s).date().isoformat(), end_date.isoformat()
                raw = cli.history_minute(code, s_iso, e_iso, frequency=freq)
                if raw is None or raw.empty:
                    if checkpoint is not None:
                        checkpoint.mark(checkpoint_key(code, freq), end_date)
                    continue
                save_raw(f"minute{freq}", f"{code}_{s_iso}_{e_iso}", raw)
                frame = build_minute_frame(code, raw)
                if frame.empty:
                    continue
                result.ok += repo.upsert_minute(frame)
                if checkpoint is not None:
                    checkpoint.mark(checkpoint_key(code, freq), end_date)
            except Exception as exc:  # noqa: BLE001
                result.failed.append((code, str(exc)))
                log.error("ingest_minute_code_failed", code=code, freq=freq, error=str(exc))
                if checkpoint is not None:
                    checkpoint.record_failure(today_bj(), checkpoint_key(code, freq), str(exc))
    finally:
        result.finished_at = now_bj()
        if own_client:
            cli.logout()

    log.info("ingest_minute_done", freq=freq, ok=result.ok, failed=len(result.failed))
    return result


__all__ = ["ingest_minute", "build_minute_frame", "checkpoint_key", "SUPPORTED_FREQ"]
