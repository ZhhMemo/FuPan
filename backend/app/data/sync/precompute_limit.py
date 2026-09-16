"""涨跌停价预计算（分板块，四舍五入到分）。

规则（对齐验收）：按板块区分涨跌幅限制——
- 主板 ±10%、创业板 ±20%、科创板 ±20%、北交所 ±30%；
- 涨跌停价 = ``prev_close × (1 ± pct)``，**四舍五入到分**（ROUND_HALF_UP）。

注：``prev_close`` 取**不复权**前收。ST 股 5% 限制、新股上市首日等特殊规则
**本次未实现**（设计未明确），已在交付报告中列为待确认项。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.core.logging import get_logger
from app.core.timeutil import now_bj
from app.data.models import SyncResult, limit_pct_of, round_to_cent
from app.data.repository import Repository

log = get_logger(__name__)


def limit_prices(prev_close: float, code: str, pct: float | None = None) -> tuple[float, float]:
    """计算某证券的涨停价与跌停价（四舍五入到分）。

    Args:
        prev_close: 前收盘价（不复权）。
        code: 证券代码（用于判定板块）。
        pct: 覆盖板块默认涨跌幅限制（可选）。

    Returns:
        ``(limit_up, limit_down)``。
    """
    ratio = limit_pct_of(code) if pct is None else pct
    up = round_to_cent(prev_close * (1 + ratio))
    down = round_to_cent(prev_close * (1 - ratio))
    return float(up), float(down)  # type: ignore[arg-type]


def build_limit_frame(code: str, daily_df: pd.DataFrame) -> pd.DataFrame:
    """由单只证券的不复权日线构建 ``dim_limit`` DataFrame。

    使用**全历史**序列计算 ``prev_close = close.shift(1)``，因此必须在完整序列上调用。
    """
    if daily_df is None or daily_df.empty:
        return pd.DataFrame(columns=["code", "date", "limit_up", "limit_down"])
    g = daily_df.sort_values("date").reset_index(drop=True)
    prev = pd.to_numeric(g["close"], errors="coerce").shift(1)
    pct = limit_pct_of(code)
    out = pd.DataFrame(
        {
            "code": code,
            "date": g["date"],
            "limit_up": prev.map(lambda x: round_to_cent(x * (1 + pct)) if pd.notna(x) else None),
            "limit_down": prev.map(lambda x: round_to_cent(x * (1 - pct)) if pd.notna(x) else None),
        }
    )
    return out.dropna(subset=["limit_up", "limit_down"]).reset_index(drop=True)


def precompute_limit(
    repo: Repository,
    codes: list[str] | None = None,
    start: date | str | None = None,
    end: date | str | None = None,
) -> SyncResult:
    """预计算并落地涨跌停价。

    Args:
        repo: 仓储。
        codes: 证券列表；None 表示全部。
        start: 仅落地该日期（含）之后的涨跌停（增量）；全历史序列仍会被读取。
        end: 仅落地该日期（含）之前的涨跌停。
    """
    result = SyncResult(task="precompute_limit", started_at=now_bj())
    if codes is None:
        codes = repo.get_all_codes()
    if not codes:
        result.finished_at = now_bj()
        result.message = "无证券可计算"
        return result

    df = repo.get_daily_multi(codes)
    if df.empty:
        result.finished_at = now_bj()
        result.message = "fact_daily 无数据"
        log.warning("precompute_limit_no_daily")
        return result

    start_ts = pd.Timestamp(start) if start is not None else None
    end_ts = pd.Timestamp(end) if end is not None else None

    frames: list[pd.DataFrame] = []
    for code, g in df.groupby("code", sort=False):
        frame = build_limit_frame(str(code), g)
        if frame.empty:
            continue
        if start_ts is not None or end_ts is not None:
            mask = pd.Series(True, index=frame.index)
            dates = pd.to_datetime(frame["date"])
            if start_ts is not None:
                mask &= dates >= start_ts
            if end_ts is not None:
                mask &= dates <= end_ts
            frame = frame.loc[mask]
        if not frame.empty:
            frames.append(frame)

    if not frames:
        result.finished_at = now_bj()
        result.message = "区间内无可落地涨跌停"
        return result

    combined = pd.concat(frames, ignore_index=True)
    result.ok = repo.upsert_limit(combined)
    result.finished_at = now_bj()
    log.info("precompute_limit_done", rows=result.ok, codes=len(codes))
    return result


__all__ = ["limit_prices", "build_limit_frame", "precompute_limit"]
