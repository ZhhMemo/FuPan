"""测试用轻量 StubRepo：模拟 ``Repository`` 中引擎/结算所需的只读接口。

仅实现引擎与结算路径用到的查询，避免单测依赖真实 DuckDB 数据。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

DAILY_COLUMNS = ["code", "date", "open", "high", "low", "close", "volume", "amount", "adj_factor", "is_trade"]


def make_daily(code: str, rows: list[dict[str, Any]]) -> pd.DataFrame:
    """由行列表构造日线 DataFrame（补齐缺失列）。"""
    out = []
    for r in rows:
        rec = {
            "code": code,
            "date": pd.Timestamp(r["date"]).date(),
            "open": float(r.get("open", r["close"])),
            "high": float(r.get("high", r["close"])),
            "low": float(r.get("low", r["close"])),
            "close": float(r["close"]),
            "volume": int(r.get("volume", 1_000_000)),
            "amount": float(r.get("amount", 0.0)),
            "adj_factor": float(r.get("adj_factor", 1.0)),
            "is_trade": bool(r.get("is_trade", True)),
        }
        out.append(rec)
    return pd.DataFrame(out, columns=DAILY_COLUMNS)


class StubRepo:
    """模拟 Repository 的只读接口。"""

    def __init__(
        self,
        bars: dict[str, pd.DataFrame] | None = None,
        *,
        limits: dict[tuple[str, str], tuple[float, float]] | None = None,
        trading_days: list[str] | None = None,
        stocks: dict[str, Any] | None = None,
        index_daily: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        self.bars = bars or {}
        self.limits = limits or {}
        self.trading_days = [pd.Timestamp(d).date() for d in (trading_days or [])]
        self.stocks = stocks or {}
        self.index_daily = index_daily or {}

    # ── 行情 ──
    def get_daily(
        self,
        code: str,
        start: date | str | None = None,
        end: date | str | None = None,
        cutoff: date | str | None = None,
    ) -> pd.DataFrame:
        df = self.bars.get(code)
        if df is None or df.empty:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        out = df.copy()
        if start is not None:
            out = out[out["date"] >= pd.Timestamp(start).date()]
        if end is not None:
            out = out[out["date"] <= pd.Timestamp(end).date()]
        if cutoff is not None:
            out = out[out["date"] <= pd.Timestamp(cutoff).date()]
        return out.reset_index(drop=True)

    def get_prev_close(self, code: str, day: date | str) -> float | None:
        d = pd.Timestamp(day).date()
        df = self.bars.get(code)
        if df is None or df.empty:
            return None
        prior = df[df["date"] < d]
        if prior.empty:
            return None
        return float(prior.iloc[-1]["close"])

    def get_limit(self, code: str, day: date | str) -> Any:
        key = (code, str(pd.Timestamp(day).date()))
        if key not in self.limits:
            return None
        up, down = self.limits[key]

        class _LP:
            pass

        lp = _LP()
        lp.limit_up = up  # type: ignore[attr-defined]
        lp.limit_down = down  # type: ignore[attr-defined]
        return lp

    def get_last_tradable(self, code: str, on_or_before: date | str | None = None) -> pd.Series | None:
        df = self.bars.get(code)
        if df is None or df.empty:
            return None
        sub = df[df["is_trade"].fillna(False).astype(bool)]
        if on_or_before is not None:
            sub = sub[sub["date"] <= pd.Timestamp(on_or_before).date()]
        if sub.empty:
            return None
        return sub.iloc[-1]

    # ── 日历 ──
    def get_trading_days(self, start: date | str | None = None, end: date | str | None = None) -> list[date]:
        days = list(self.trading_days)
        if start is not None:
            days = [d for d in days if d >= pd.Timestamp(start).date()]
        if end is not None:
            days = [d for d in days if d <= pd.Timestamp(end).date()]
        return days

    def is_trading_day(self, day: date | str) -> bool:
        return pd.Timestamp(day).date() in self.trading_days

    # ── 维表 ──
    def get_stock(self, code: str) -> Any:
        return self.stocks.get(code)

    def get_index_daily(
        self, index_code: str, start: date | str | None = None, end: date | str | None = None
    ) -> pd.DataFrame:
        df = self.index_daily.get(index_code)
        if df is None or df.empty:
            return pd.DataFrame(columns=["index_code", "date", "close"])
        out = df.copy()
        if start is not None:
            out = out[out["date"] >= pd.Timestamp(start).date()]
        if end is not None:
            out = out[out["date"] <= pd.Timestamp(end).date()]
        return out.reset_index(drop=True)


def ts(date_str: str, hour: int = 15) -> datetime:
    """构造北京时间 naive datetime。"""
    return datetime.combine(pd.Timestamp(date_str).date(), datetime.min.time()).replace(hour=hour)


__all__ = ["StubRepo", "make_daily", "ts", "DAILY_COLUMNS"]
