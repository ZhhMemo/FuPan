"""日线采集（全量 + 增量）。

- 存储**不复权** OHLCV + ``adj_factor``（红线②）。
- ``adj_factor`` 由「后复权价 / 不复权价」**定义式推导**（``hfq = none × factor``），
  保证算法正确；若后复权查询不可用，则退化用 Baostock ``query_adjust_factor`` 并前向填充。
- 每只证券成功/失败都记录，失败必须可见（不静默）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.config import Settings
from app.config import settings as default_settings
from app.core.logging import get_logger
from app.core.timeutil import now_bj, parse_date, today_bj
from app.data.models import SyncResult
from app.data.repository import Repository
from app.data.sync.baostock_client import ADJUST_HFQ, ADJUST_NONE, BaostockClient
from app.data.sync.checkpoint import Checkpoint
from app.data.sync.ingest_meta import save_raw

log = get_logger(__name__)


def _numeric(series: pd.Series | None, default: float = 0.0) -> pd.Series:
    """安全转数值（空串 → NaN → 默认值）。"""
    if series is None:
        return pd.Series(dtype="float64")
    cleaned = series.astype("string").str.strip().replace({"": None})
    return pd.to_numeric(cleaned, errors="coerce").fillna(default)


def derive_adj_factor(client: BaostockClient, code: str, start: str, end: str, none_df: pd.DataFrame) -> pd.Series:
    """推导每交易日的复权因子（后复权口径，``hfq = none × factor``）。

    Args:
        client: Baostock 客户端。
        code: 证券代码。
        start: 起始日期（ISO）。
        end: 结束日期（ISO）。
        none_df: 不复权日线（含 ``date`` 与 ``close``）。

    Returns:
        与 ``none_df`` 行对齐的复权因子 Series（缺失按 1.0）。
    """
    dates = pd.to_datetime(none_df["date"])
    # ── 主路径：hfq / none ──
    try:
        hfq_df = client.history_daily(code, start, end, adjustflag=ADJUST_HFQ)
    except Exception as exc:  # noqa: BLE001
        log.warning("hfq_query_failed", code=code, error=str(exc))
        hfq_df = pd.DataFrame()

    if hfq_df is not None and not hfq_df.empty:
        merged = pd.merge(
            pd.DataFrame({"date": dates.values, "none": _numeric(none_df["close"]).values}),
            pd.DataFrame({"date": pd.to_datetime(hfq_df["date"]).values, "hfq": _numeric(hfq_df["close"]).values}),
            on="date",
            how="left",
        )
        ratio = merged["hfq"] / merged["none"]
        ratio = ratio.replace([float("inf"), float("-inf")], pd.NA).fillna(1.0)
        return pd.Series(ratio.values, index=range(len(none_df)))

    # ── 兜底：query_adjust_factor + 前向填充 ──
    log.warning("derive_adj_factor_fallback", code=code)
    fdf = client.adjust_factor(code, start, end)
    if fdf is None or fdf.empty:
        return pd.Series(1.0, index=range(len(none_df)))

    col = next(
        (c for c in ("backAdjustFactor", "adjustFactor", "foreAdjustFactor") if c in fdf.columns),
        None,
    )
    if col is None:
        return pd.Series(1.0, index=range(len(none_df)))
    ex = pd.to_datetime(fdf["dividOperateDate"])
    vals = pd.to_numeric(fdf[col], errors="coerce")
    s = pd.Series(vals.values, index=ex).dropna()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if s.empty:
        return pd.Series(1.0, index=range(len(none_df)))
    idx = pd.DatetimeIndex(sorted(set(dates).union(s.index)))
    filled = s.reindex(idx).ffill().fillna(1.0)
    aligned = filled.reindex(dates).fillna(1.0)
    return pd.Series(aligned.values, index=range(len(none_df)))


def build_daily_frame(code: str, none_df: pd.DataFrame, factor: pd.Series) -> pd.DataFrame:
    """把 Baostock 不复权日线 + 因子转换为 ``fact_daily`` DataFrame。"""
    if none_df is None or none_df.empty:
        return pd.DataFrame()
    dates = pd.to_datetime(none_df["date"])
    close = _numeric(none_df.get("close"), default=float("nan"))
    out = pd.DataFrame(
        {
            "code": code,
            "date": dates.dt.date,
            "open": _numeric(none_df.get("open"), default=float("nan")),
            "high": _numeric(none_df.get("high"), default=float("nan")),
            "low": _numeric(none_df.get("low"), default=float("nan")),
            "close": close,
            "volume": _numeric(none_df.get("volume")).astype("int64"),
            "amount": _numeric(none_df.get("amount")),
            "adj_factor": factor.reset_index(drop=True),
        }
    )
    if "tradestatus" in none_df.columns:
        out["is_trade"] = none_df["tradestatus"].astype("string").str.strip().eq("1")
    else:
        out["is_trade"] = True
    out = out.loc[out["close"].notna()].reset_index(drop=True)
    return out


def resolve_start(
    repo: Repository,
    code: str,
    full: bool,
    checkpoint: Checkpoint | None,
    explicit_start: date | None,
    config: Settings,
) -> date:
    """决定某证券本次拉取的起始日期。"""
    if explicit_start is not None:
        return explicit_start
    if not full and checkpoint is not None:
        last = checkpoint.last_date_of(code)
        if last is not None:
            return last + timedelta(days=1)
    stock = repo.get_stock(code)
    if stock is not None and stock.list_date is not None:
        return stock.list_date
    return parse_date(config.full_history_start)


def ingest_daily(
    repo: Repository,
    codes: list[str] | None = None,
    full: bool = False,
    start: date | str | None = None,
    end: date | str | None = None,
    client: BaostockClient | None = None,
    checkpoint: Checkpoint | None = None,
    config: Settings | None = None,
) -> SyncResult:
    """日线全量/增量采集。

    Args:
        repo: 仓储。
        codes: 证券代码列表；None 表示全部 ``dim_stock``。
        full: 是否全量（从 ``list_date`` 起）。
        start: 显式起始日期（覆盖增量逻辑）。
        end: 结束日期；默认今天。
        client: 可复用客户端；None 则新建。
        checkpoint: 断点续传；None 则不记录。
        config: 配置。

    Returns:
        ``SyncResult``（含失败清单，失败必须可见）。
    """
    cfg = config or default_settings
    result = SyncResult(task="ingest_daily", started_at=now_bj())
    end_date = parse_date(end) if end is not None else today_bj()
    explicit_start = parse_date(start) if start is not None else None

    if codes is None:
        codes = repo.get_all_codes()
    if not codes:
        result.finished_at = now_bj()
        result.message = "无证券可采集（dim_stock 为空）"
        log.warning("ingest_daily_no_codes")
        return result

    own_client = client is None
    cli = client or BaostockClient(cfg.baostock_reconnect_retries)
    if own_client:
        cli.login()

    try:
        for code in codes:
            try:
                s = resolve_start(repo, code, full, checkpoint, explicit_start, cfg)
                if s > end_date:
                    continue
                s_iso, e_iso = s.isoformat(), end_date.isoformat()
                none_df = cli.history_daily(code, s_iso, e_iso, adjustflag=ADJUST_NONE)
                if none_df is None or none_df.empty:
                    if checkpoint is not None:
                        checkpoint.mark(code, end_date)
                    continue
                factor = derive_adj_factor(cli, code, s_iso, e_iso, none_df)
                frame = build_daily_frame(code, none_df, factor)
                if frame.empty:
                    if checkpoint is not None:
                        checkpoint.mark(code, end_date)
                    continue
                save_raw("daily", f"{code}_{s_iso}_{e_iso}", none_df)
                n = repo.upsert_daily(frame)
                result.ok += n
                if checkpoint is not None:
                    checkpoint.mark(code, end_date)
            except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
                result.failed.append((code, str(exc)))
                log.error("ingest_daily_code_failed", code=code, error=str(exc))
                if checkpoint is not None:
                    checkpoint.record_failure(today_bj(), code, str(exc))
    finally:
        result.finished_at = now_bj()
        if own_client:
            cli.logout()

    log.info("ingest_daily_done", ok=result.ok, failed=len(result.failed), codes=len(codes))
    return result


__all__ = ["ingest_daily", "build_daily_frame", "derive_adj_factor", "resolve_start"]
