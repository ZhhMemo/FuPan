"""维表落地：股票列表 / 交易日历 / 指数 / 分红，以及原始层（raw）落地。

原始层约定：``raw/<table>/dt=<capture_date>/<code>.csv.gz``，
**已存在则不覆盖**（raw 永不修改）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.config import settings as default_settings
from app.core.errors import DataUnavailable
from app.core.logging import get_logger
from app.core.timeutil import now_bj_naive, today_bj
from app.data.models import board_of
from app.data.repository import Repository
from app.data.sync.baostock_client import BaostockClient

log = get_logger(__name__)


# ══════════════════════ 原始层（raw）══════════════════════
def _safe_name(name: str) -> str:
    return str(name).replace("/", "_").replace("\\", "_")


def raw_path(table: str, name: str, capture: date | None = None) -> Path:
    """返回原始层落盘路径（不创建文件）。"""
    cap = capture or today_bj()
    return default_settings.raw_dir / table / f"dt={cap.isoformat()}" / f"{_safe_name(name)}.csv.gz"


def save_raw(table: str, name: str, df: pd.DataFrame, capture: date | None = None) -> Path | None:
    """把原始数据原样落盘；若目标文件已存在则**跳过**（raw 永不修改）。

    Returns:
        落盘路径；跳过时返回 ``None``。
    """
    if df is None:
        return None
    path = raw_path(table, name, capture)
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")
    return path


def _to_date_series(s: pd.Series) -> pd.Series:
    """把日期列（字符串/空串）转换为 date（空值 → NaT）。"""
    cleaned = s.astype("string").str.strip().replace({"": None})
    return pd.to_datetime(cleaned, errors="coerce")


# ══════════════════════ 股票列表 ══════════════════════
def build_stock_frame(basic: pd.DataFrame, industry_map: dict[str, str] | None = None) -> pd.DataFrame:
    """把 Baostock 证券基础信息转换为 ``dim_stock`` DataFrame。"""
    if basic is None or basic.empty:
        return pd.DataFrame()
    industry_map = industry_map or {}
    df = basic.copy()
    if "type" in df.columns:
        df = df[df["type"].astype(str) == "1"]  # 仅股票；剔除指数/ETF/可转债
    if df.empty:
        return pd.DataFrame()

    name = df.get("code_name", pd.Series([""] * len(df))).fillna("").astype(str)
    out = pd.DataFrame(
        {
            "code": df["code"].astype(str),
            "name": name,
            "list_date": _to_date_series(df.get("ipoDate", pd.Series([None] * len(df)))),
            "delist_date": _to_date_series(df.get("outDate", pd.Series([None] * len(df)))),
            "board": df["code"].astype(str).map(board_of),
            "industry": df["code"].astype(str).map(lambda c: industry_map.get(c, "")),
            "is_st": name.str.upper().str.contains("ST", na=False),
            "updated_at": now_bj_naive(),
        }
    )
    # 说明：Baostock 对退市股会在 outDate 返回退市日期；status=='0' 时 outDate 通常非空，
    # 故此处不再额外兜底，避免引入不确定规则。
    out["list_date"] = out["list_date"].dt.date
    out["delist_date"] = out["delist_date"].dt.date
    return out.reset_index(drop=True)


def ingest_stock_list(client: BaostockClient, repo: Repository) -> int:
    """落地全市场证券基础信息（含退市股）。"""
    basic = client.stock_basic()
    save_raw("stock_basic", "all", basic)

    industry_map: dict[str, str] = {}
    try:
        ind = client.industry()
        if ind is not None and not ind.empty:
            save_raw("stock_industry", "all", ind)
            industry_map = dict(zip(ind["code"].astype(str), ind["industry"].astype(str), strict=False))
    except Exception as exc:  # noqa: BLE001 - 行业非关键路径
        log.warning("industry_fetch_failed", error=str(exc))

    frame = build_stock_frame(basic, industry_map)
    if frame.empty:
        raise DataUnavailable("Baostock 未返回任何证券基础信息")
    n = repo.upsert_stocks(frame)
    log.info("ingest_stock_list_ok", rows=n)
    return n


# ══════════════════════ 交易日历 ══════════════════════
def build_calendar_frame(trade_dates: pd.DataFrame) -> pd.DataFrame:
    """把 Baostock 交易日历转换为 ``dim_calendar`` DataFrame。"""
    if trade_dates is None or trade_dates.empty:
        return pd.DataFrame()
    df = trade_dates.copy()
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df["calendar_date"]).dt.date,
            "is_trading_day": df["is_trading_day"].astype(str).isin({"1", "True", "true"}),
        }
    )
    return out.drop_duplicates(subset=["date"]).reset_index(drop=True)


def ingest_calendar(client: BaostockClient, repo: Repository, start: str, end: str) -> int:
    """落地交易日历。"""
    td = client.trade_dates(start, end)
    save_raw("trade_dates", f"{start}_{end}", td)
    frame = build_calendar_frame(td)
    if frame.empty:
        raise DataUnavailable(f"Baostock 未返回 {start}~{end} 的交易日历")
    n = repo.upsert_calendar(frame)
    log.info("ingest_calendar_ok", rows=n, start=start, end=end)
    return n


# ══════════════════════ 指数日线 ══════════════════════
def ingest_index_daily(client: BaostockClient, repo: Repository, codes: list[str], start: str, end: str) -> int:
    """落地基准指数日线（收盘价）。"""
    frames: list[pd.DataFrame] = []
    for code in codes:
        df = client.history_daily(code, start, end, is_index=True)
        if df is None or df.empty:
            log.warning("index_empty", index_code=code)
            continue
        save_raw("index_daily", code, df)
        frames.append(
            pd.DataFrame(
                {
                    "index_code": code,
                    "date": pd.to_datetime(df["date"]).dt.date,
                    "close": pd.to_numeric(df["close"], errors="coerce"),
                }
            )
        )
    if not frames:
        return 0
    out = pd.concat(frames, ignore_index=True).dropna(subset=["close"])
    n = repo.upsert_index_daily(out)
    log.info("ingest_index_daily_ok", rows=n, indices=len(frames))
    return n


# ══════════════════════ 分红送配 ══════════════════════
def build_dividend_frame(code: str, raw: pd.DataFrame) -> pd.DataFrame:
    """把 Baostock 分红数据转换为 ``dim_dividend`` DataFrame。"""
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    ex = _to_date_series(df.get("dividOperateDate", pd.Series([None] * len(df))))
    cash = pd.to_numeric(df.get("dividCashPsBeforeTax", pd.Series([0.0] * len(df))), errors="coerce").fillna(0.0)
    share = pd.to_numeric(df.get("dividStocksPs", pd.Series([0.0] * len(df))), errors="coerce").fillna(0.0)
    out = pd.DataFrame(
        {
            "code": code,
            "ex_date": ex.dt.date,
            "cash_per_share": cash,
            "share_ratio": share,
            "rights_ratio": 0.0,
        }
    )
    return out.dropna(subset=["ex_date"]).drop_duplicates(subset=["ex_date"]).reset_index(drop=True)


def ingest_dividend(client: BaostockClient, repo: Repository, codes: list[str], start_year: int, end_year: int) -> int:
    """落地分红送配（按年查询；配股比例默认 0，见 §21 NA5 简化）。"""
    frames: list[pd.DataFrame] = []
    for code in codes:
        raw = client.dividend_years(code, str(start_year), str(end_year))
        if raw is None or raw.empty:
            continue
        save_raw("dividend", code, raw)
        frame = build_dividend_frame(code, raw)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return 0
    out = pd.concat(frames, ignore_index=True)
    n = repo.upsert_dividend(out)
    log.info("ingest_dividend_ok", rows=n)
    return n


__all__ = [
    "raw_path",
    "save_raw",
    "build_stock_frame",
    "ingest_stock_list",
    "build_calendar_frame",
    "ingest_calendar",
    "ingest_index_daily",
    "build_dividend_frame",
    "ingest_dividend",
]
