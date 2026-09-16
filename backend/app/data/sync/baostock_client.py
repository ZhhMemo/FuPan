"""Baostock 客户端封装（登录 / 断线重连 / 查询）。

- Baostock 为**公有免费**数据源，含退市股历史（主数据源）。
- 数据为**不复权**口径时使用 ``adjustflag="3"``；复权因子单独查询。
- 全局会话非线程安全：本客户端为「单会话」，不建议多线程共享同一实例。

Baostock ``adjustflag`` 取值：``1``=后复权，``2``=前复权，``3``=不复权。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pandas as pd

from app.core.errors import DataUnavailable
from app.core.logging import get_logger

try:  # pragma: no cover - 取决于是否安装 baostock
    import baostock as bs

    _HAS_BAOSTOCK = True
except Exception:  # pragma: no cover
    bs = None  # type: ignore[assignment]
    _HAS_BAOSTOCK = False

log = get_logger(__name__)

# ── 字段集合 ──
STOCK_DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
INDEX_DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,pctChg"
MINUTE_FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"

ADJUST_NONE = "3"
ADJUST_QFQ = "2"
ADJUST_HFQ = "1"


class BaostockClient:
    """Baostock 查询客户端。

    Args:
        retries: 单次查询失败后的重试次数（重试前会重新登录）。
    """

    def __init__(self, retries: int = 3) -> None:
        if not _HAS_BAOSTOCK:
            raise DataUnavailable("未安装 baostock，请先执行 make install")
        self._retries = max(0, retries)
        self._logged_in = False

    # ─────────────── 会话 ───────────────
    def login(self) -> None:
        """登录 Baostock。"""
        lg = bs.login()
        if getattr(lg, "error_code", "0") != "0":
            raise DataUnavailable(f"Baostock 登录失败：{lg.error_code} {lg.error_msg}")
        self._logged_in = True
        log.info("baostock_login_ok")

    def logout(self) -> None:
        """登出 Baostock（失败不抛出）。"""
        try:
            bs.logout()
        except Exception as exc:  # noqa: BLE001
            log.warning("baostock_logout_error", error=str(exc))
        finally:
            self._logged_in = False

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def __enter__(self) -> BaostockClient:
        self._ensure_login()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.logout()

    # ─────────────── 结果集 → DataFrame ───────────────
    @staticmethod
    def _rs_to_df(rs: Any) -> pd.DataFrame:
        rows: list[list[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=list(rs.fields))

    def _fetch(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> pd.DataFrame:
        """执行查询并带重连重试。"""
        last_err: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                self._ensure_login()
                rs = fn(*args, **kwargs)
                if getattr(rs, "error_code", "0") != "0":
                    raise DataUnavailable(f"Baostock 查询失败：{rs.error_code} {rs.error_msg}")
                return self._rs_to_df(rs)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self._logged_in = False
                if attempt < self._retries:
                    time.sleep(0.5 * (attempt + 1))
        raise DataUnavailable(f"Baostock 查询在 {self._retries} 次重试后仍失败：{last_err}")

    # ─────────────── 查询 API ───────────────
    def stock_basic(self, code: str = "") -> pd.DataFrame:
        """证券基础信息（``code`` 为空时返回全部）。

        返回列：code, code_name, ipoDate, outDate, type, status。
        """
        return self._fetch(bs.query_stock_basic, code=code)

    def trade_dates(self, start: str, end: str) -> pd.DataFrame:
        """交易日历。返回列：calendar_date, is_trading_day。"""
        return self._fetch(bs.query_trade_dates, start_date=start, end_date=end)

    def history_daily(
        self,
        code: str,
        start: str,
        end: str,
        adjustflag: str = ADJUST_NONE,
        is_index: bool = False,
    ) -> pd.DataFrame:
        """日线（默认不复权）。"""
        fields = INDEX_DAILY_FIELDS if is_index else STOCK_DAILY_FIELDS
        return self._fetch(
            bs.query_history_k_data_plus,
            code,
            fields,
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag=adjustflag,
        )

    def history_minute(self, code: str, start: str, end: str, frequency: str = "5") -> pd.DataFrame:
        """分钟线（``frequency`` ∈ {5, 15, 30, 60}）。"""
        return self._fetch(
            bs.query_history_k_data_plus,
            code,
            MINUTE_FIELDS,
            start_date=start,
            end_date=end,
            frequency=str(frequency),
            adjustflag=ADJUST_NONE,
        )

    def adjust_factor(self, code: str, start: str, end: str) -> pd.DataFrame:
        """复权因子（若 Baostock 该接口不可用则返回空 DataFrame）。"""
        fn = getattr(bs, "query_adjust_factor", None)
        if fn is None:
            return pd.DataFrame()
        try:
            return self._fetch(fn, code=code, start_date=start, end_date=end)
        except Exception as exc:  # noqa: BLE001
            log.warning("adjust_factor_unavailable", code=code, error=str(exc))
            return pd.DataFrame()

    def dividend(self, code: str, year: str, year_type: str = "report") -> pd.DataFrame:
        """分红送配。"""
        return self._fetch(bs.query_dividend_data, code=code, year=str(year), yearType=year_type)

    def dividend_years(self, code: str, start_year: str, end_year: str) -> pd.DataFrame:
        """按年查询分红并合并（Baostock 单次只查一年）。"""
        frames: list[pd.DataFrame] = []
        for year in range(int(start_year), int(end_year) + 1):
            try:
                df = self.dividend(code, str(year))
            except Exception as exc:  # noqa: BLE001
                log.warning("dividend_year_failed", code=code, year=year, error=str(exc))
                continue
            if df is not None and not df.empty:
                frames.append(df)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def all_stock(self, day: str) -> pd.DataFrame:
        """某交易日全部证券及交易状态。返回列：code, tradeStatus, code_name。"""
        return self._fetch(bs.query_all_stock, day=day)

    def industry(self) -> pd.DataFrame:
        """行业分类。返回列：updateDate, code, code_name, industry, industryClassification。"""
        return self._fetch(bs.query_stock_industry)


__all__ = [
    "BaostockClient",
    "STOCK_DAILY_FIELDS",
    "INDEX_DAILY_FIELDS",
    "MINUTE_FIELDS",
    "ADJUST_NONE",
    "ADJUST_QFQ",
    "ADJUST_HFQ",
]
