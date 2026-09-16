"""涨跌停价预计算（**按板块 + 按日期分段**，四舍五入到分）。

规则（对齐验收 + Q4 修正）：
- 涨跌停价 = ``prev_close × (1 ± pct)``，**四舍五入到分**（ROUND_HALF_UP）；
- 板块涨跌幅比例**随制度分段**（历史正确性，见 ``models.limit_pct_of``）：

  | 板块 | 比例 | 分段起点 |
  |---|---|---|
  | 主板 | ±10% | 1996-12-16 起（此前**无**统一涨跌停 → 不生成涨跌停行） |
  | 创业板 ``sz.30x`` | ±10% → ±20% | 2020-08-24 起改为 ±20% |
  | 科创板 ``sh.688/689`` | ±20% | 2019-07-22 开板即 20% |
  | 北交所 ``bj.`` | ±30% | — |

- **退市整理期（Q2，近似）**：退市股在「退市整理期」涨跌幅按 **±10%** 处理。
  由于**数据源拿不到整理期公告起止**，代码以「``delist_date`` 往前 **30 个交易日**（含当日）」
  **近似**推断整理期区间 —— 见 ``DELIST_PERIOD_TRADING_DAYS``。
  **⚠️ 该区间为近似值，真实起止需以交易所公告为准；本项仅用于避免按板块规则误算退市段涨跌停。**

注：ST 股 ±5% 限制因 ``is_st`` 历史不可靠（见 ``models.Stock`` docstring）而**不实现**；
新股上市首日等特殊规则同样未实现（标的池已排除上市不足 60 个交易日的证券）。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.core.logging import get_logger
from app.core.timeutil import now_bj
from app.data.models import SyncResult, limit_pct_of, limit_prices_of
from app.data.repository import Repository

log = get_logger(__name__)


def _as_date(value: date | str | None) -> date | None:
    """把 ``date`` / ISO 字符串归一化为 ``date``；``None``/非法值返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.date()

# 退市整理期近似长度（交易日）：delist_date 起往前 30 个交易日（含退市日）。
DELIST_PERIOD_TRADING_DAYS: int = 30
# 退市整理期近似涨跌幅限制比例（Q2：按 10% 处理）。
DELIST_PERIOD_LIMIT_PCT: float = 0.10


def limit_prices(
    prev_close: float,
    code: str,
    day: date | str | None = None,
    pct: float | None = None,
) -> tuple[float | None, float | None]:
    """计算某证券某日的涨停价与跌停价（四舍五入到分）。

    Args:
        prev_close: 前收盘价（不复权）。
        code: 证券代码（用于判定板块）。
        day: 目标日期（用于按制度分段；``None`` 表示按最新规则）。
        pct: 覆盖默认涨跌幅限制（可选）；给定则不使用板块/日期规则。

    Returns:
        ``(limit_up, limit_down)``；当日无涨跌停限制（如 1996-12-16 之前）时返回 ``(None, None)``。

    Note:
        取整走 ``models.limit_prices_of``（Decimal HALF_UP，唯一实现），
        **不可**改用 ``round_to_cent(prev * (1 ± pct))``（浮点会少 1 分）。
    """
    ratio = pct if pct is not None else limit_pct_of(code, day)
    if ratio is None:
        return None, None
    up, down = limit_prices_of(prev_close, ratio)
    return up, down


def delist_period_start(trading_dates: list[date], delist_date: date | None) -> date | None:
    """推断退市整理期起始日（**近似**）。

    以 ``delist_date`` 起往前 ``DELIST_PERIOD_TRADING_DAYS`` 个交易日（含退市日）为整理期。

    Args:
        trading_dates: 该证券的交易日序列（升序）。
        delist_date: 退市日；``None`` 表示未退市。

    Returns:
        整理期起始交易日（含）；无法推断时返回 ``None``。

    Note:
        真实整理期起止需交易公告，**数据源拿不到**，此处为近似区间。
    """
    if delist_date is None or not trading_dates:
        return None
    days = [d for d in trading_dates if d <= delist_date]
    if not days:
        return None
    start_idx = max(0, len(days) - DELIST_PERIOD_TRADING_DAYS)
    return days[start_idx]


def _row_pct(
    code: str,
    day: date | None,
    delist_start: date | None,
    delist_date: date | None,
    pct_override: float | None = None,
) -> float | None:
    """单行的涨跌幅比例：优先退市整理期（10%），其次按板块/日期分段。"""
    if pct_override is not None:
        return pct_override
    # 退市整理期：近似区间 [delist_start, delist_date] 内按 10%
    if delist_start is not None and delist_date is not None and day is not None and delist_start <= day <= delist_date:
        return DELIST_PERIOD_LIMIT_PCT
    return limit_pct_of(code, day)


def build_limit_frame(
    code: str,
    daily_df: pd.DataFrame,
    delist_date: date | str | None = None,
) -> pd.DataFrame:
    """由单只证券的不复权日线构建 ``dim_limit`` DataFrame。

    使用**全历史**序列计算 ``prev_close = close.shift(1)``，因此必须在完整序列上调用。
    涨跌幅**逐行按日期分段**（Q4）；退市整理期按 10% 近似（Q2）；当日无涨跌停限制的行不生成。

    Args:
        code: 证券代码。
        daily_df: 单只证券的不复权日线（含 ``date`` / ``close``）。
        delist_date: 退市日（用于近似划定退市整理期）；未退市传 ``None``。

    Returns:
        ``[code, date, limit_up, limit_down]`` 的 DataFrame；无有效行时为空表。
    """
    if daily_df is None or daily_df.empty:
        return pd.DataFrame(columns=["code", "date", "limit_up", "limit_down"])

    g = daily_df.sort_values("date").reset_index(drop=True)
    dates = [d.date() if isinstance(d, pd.Timestamp) else d for d in pd.to_datetime(g["date"]).tolist()]
    prev = pd.to_numeric(g["close"], errors="coerce").shift(1)

    dd = _as_date(delist_date)
    dstart = delist_period_start([d for d in dates if d is not None], dd)

    ups: list[float | None] = []
    downs: list[float | None] = []
    for i in range(len(g)):
        pc = prev.iloc[i]
        if pd.isna(pc):
            ups.append(None)
            downs.append(None)
            continue
        pct = _row_pct(code, dates[i], dstart, dd)
        up, down = limit_prices(float(pc), code, dates[i], pct)
        ups.append(up)
        downs.append(down)

    out = pd.DataFrame({"code": code, "date": g["date"], "limit_up": ups, "limit_down": downs})
    out = out.dropna(subset=["limit_up", "limit_down"]).reset_index(drop=True)
    return out


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

    # 退市日映射：用于近似划定退市整理期（Q2）
    delist_map: dict[str, date | None] = {}
    try:
        stock_df = repo.list_stocks()
        if stock_df is not None and not stock_df.empty and "delist_date" in stock_df.columns:
            for row in stock_df.itertuples(index=False):
                raw = getattr(row, "delist_date", None)
                delist_map[str(row.code)] = (
                    pd.Timestamp(raw).date() if raw is not None and not pd.isna(raw) else None
                )
    except Exception as exc:  # noqa: BLE001 - 缺失退市信息不应阻断，但要可见
        log.warning("precompute_limit_delist_map_failed", error=str(exc))

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
        code_str = str(code)
        frame = build_limit_frame(code_str, g, delist_date=delist_map.get(code_str))
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
    # 全量重算（未指定增量区间）时，先清除这些证券的陈旧行，避免按 Q4 分段后残留
    # 「本不该存在」的涨跌停（如 1996-12-16 之前）。
    if start is None and end is None:
        removed = repo.delete_limit(list(combined["code"].unique()))
        if removed:
            log.info("precompute_limit_purged_stale", removed=removed)
    result.ok = repo.upsert_limit(combined)
    result.finished_at = now_bj()
    log.info("precompute_limit_done", rows=result.ok, codes=len(codes))
    return result


__all__ = [
    "DELIST_PERIOD_TRADING_DAYS",
    "DELIST_PERIOD_LIMIT_PCT",
    "limit_prices",
    "delist_period_start",
    "build_limit_frame",
    "precompute_limit",
]
