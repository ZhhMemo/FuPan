"""Repository：仓储（访问 DuckDB 的唯一入口）。

- 读方法返回原始 DataFrame / 值对象；**行情数据出 API 前必须过 VisibilityGuard**。
  ``get_daily`` / ``get_minute`` 提供可选 ``cutoff`` 参数，传入即自动施加截断（红线①）。
- 写方法自带写锁（``acquire_write``），保证写者互斥。
- 全部参数化查询，禁止字符串拼接 SQL。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import pandas as pd

from app.core.db import APP, MARKET, DuckDBManager, Layer, get_manager
from app.core.errors import DataUnavailable, NotFound
from app.core.timeutil import now_bj_naive
from app.data.models import LimitPrice, Stock
from app.data.visibility import mask_df

# ── 各表列定义（用于显式 upsert，避免列顺序漂移）──
DAILY_COLUMNS: list[str] = [
    "code",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "is_trade",
]
MINUTE_COLUMNS: list[str] = ["code", "dt", "open", "high", "low", "close", "volume", "amount"]
STOCK_COLUMNS: list[str] = [
    "code",
    "name",
    "list_date",
    "delist_date",
    "board",
    "industry",
    "is_st",
    "updated_at",
]
CALENDAR_COLUMNS: list[str] = ["date", "is_trading_day"]
LIMIT_COLUMNS: list[str] = ["code", "date", "limit_up", "limit_down"]
INDEX_COLUMNS: list[str] = ["index_code", "date", "close"]
DIVIDEND_COLUMNS: list[str] = ["code", "ex_date", "cash_per_share", "share_ratio", "rights_ratio"]


def _iso(value: date | str) -> str:
    """归一化为 ISO 日期字符串（DuckDB 参数化接受）。"""
    return value.isoformat() if isinstance(value, date) else str(value)


class Repository:
    """仓储（DAL 唯一入口）。"""

    def __init__(self, manager: DuckDBManager | None = None) -> None:
        self._mgr: DuckDBManager = manager or get_manager()

    @property
    def manager(self) -> DuckDBManager:
        """底层连接管理器。"""
        return self._mgr

    # ══════════════════════ 读：行情 ══════════════════════

    def get_daily(
        self,
        code: str,
        start: date | str | None = None,
        end: date | str | None = None,
        cutoff: date | str | None = None,
    ) -> pd.DataFrame:
        """查询日线（不复权）。

        Args:
            code: 证券代码。
            start: 起始日期（含）。
            end: 结束日期（含）。
            cutoff: 若提供，则对结果施加 ``VisibilityGuard`` 截断（红线①）。

        Returns:
            按日期升序的 DataFrame（列为 ``DAILY_COLUMNS``）。
        """
        con = self._mgr.get_read(MARKET)
        sql = (
            "SELECT code, date, open, high, low, close, volume, amount, adj_factor, is_trade "
            "FROM fact_daily WHERE code = ?"
        )
        params: list[Any] = [code]
        if start is not None:
            sql += " AND date >= ?"
            params.append(_iso(start))
        if end is not None:
            sql += " AND date <= ?"
            params.append(_iso(end))
        sql += " ORDER BY date"
        df = con.execute(sql, params).fetchdf()
        if cutoff is not None:
            df = mask_df(df, cutoff, "date")
        return df

    def get_daily_multi(
        self,
        codes: Sequence[str],
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> pd.DataFrame:
        """批量查询多只股票的日线（供预计算使用）。"""
        if not codes:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        con = self._mgr.get_read(MARKET)
        placeholders = ", ".join(["?"] * len(codes))
        sql = (
            "SELECT code, date, open, high, low, close, volume, amount, adj_factor, is_trade "
            f"FROM fact_daily WHERE code IN ({placeholders})"
        )
        params: list[Any] = list(codes)
        if start is not None:
            sql += " AND date >= ?"
            params.append(_iso(start))
        if end is not None:
            sql += " AND date <= ?"
            params.append(_iso(end))
        sql += " ORDER BY code, date"
        return con.execute(sql, params).fetchdf()

    def get_minute(
        self,
        code: str,
        start: date | str | None = None,
        end: date | str | None = None,
        cutoff: date | str | None = None,
    ) -> pd.DataFrame:
        """查询分钟线（可选可见性截断）。"""
        con = self._mgr.get_read(MARKET)
        sql = "SELECT code, dt, open, high, low, close, volume, amount FROM fact_minute WHERE code = ?"
        params: list[Any] = [code]
        if start is not None:
            sql += " AND dt >= ?"
            params.append(_iso(start))
        if end is not None:
            sql += " AND dt <= ?"
            params.append(_iso(end))
        sql += " ORDER BY dt"
        df = con.execute(sql, params).fetchdf()
        if cutoff is not None:
            df = mask_df(df, cutoff, "dt")
        return df

    def get_prev_close(self, code: str, day: date | str) -> float | None:
        """返回给定日期**之前**最近一个交易日的收盘价（不复权）。"""
        con = self._mgr.get_read(MARKET)
        row = con.execute(
            "SELECT close FROM fact_daily WHERE code = ? AND date < ? ORDER BY date DESC LIMIT 1",
            [code, _iso(day)],
        ).fetchone()
        return None if row is None or row[0] is None else float(row[0])

    def get_last_tradable(self, code: str, on_or_before: date | str | None = None) -> pd.Series | None:
        """返回该证券**最后一个可交易日**的行情行（不复权）。

        口径（NA3 实测结论，**必须用 ``is_trade`` 过滤**）：
        ``WHERE is_trade ORDER BY date DESC LIMIT 1``——
        退市日当天 ``is_trade=False`` 且无成交量，**不可成交**；
        若用 ``MAX(date)`` 会错误地取到「退市当天不可交易的占位行」。
        退市平仓必须用本方法定位最后一个可成交日。

        Args:
            code: 证券代码。
            on_or_before: 只考虑不晚于该日期的行（可选）。

        Returns:
            行情行（Series）；无数据返回 ``None``。
        """
        con = self._mgr.get_read(MARKET)
        sql = (
            "SELECT code, date, open, high, low, close, volume, amount, adj_factor, is_trade "
            "FROM fact_daily WHERE code = ? AND is_trade"
        )
        params: list[Any] = [code]
        if on_or_before is not None:
            sql += " AND date <= ?"
            params.append(_iso(on_or_before))
        sql += " ORDER BY date DESC LIMIT 1"
        df = con.execute(sql, params).fetchdf()
        return None if df.empty else df.iloc[0]

    # ══════════════════════ 读：维表 ══════════════════════

    def get_calendar(self, start: date | str | None = None, end: date | str | None = None) -> pd.DataFrame:
        """查询交易日历。"""
        con = self._mgr.get_read(MARKET)
        sql = "SELECT date, is_trading_day FROM dim_calendar WHERE 1=1"
        params: list[Any] = []
        if start is not None:
            sql += " AND date >= ?"
            params.append(_iso(start))
        if end is not None:
            sql += " AND date <= ?"
            params.append(_iso(end))
        sql += " ORDER BY date"
        return con.execute(sql, params).fetchdf()

    def get_trading_days(self, start: date | str | None = None, end: date | str | None = None) -> list[date]:
        """返回区间内全部交易日（升序）。"""
        df = self.get_calendar(start, end)
        if df.empty:
            return []
        days = df.loc[df["is_trading_day"].fillna(False), "date"]
        return [pd.Timestamp(d).date() for d in days.tolist()]

    def is_trading_day(self, day: date | str) -> bool:
        """判断是否为交易日。"""
        con = self._mgr.get_read(MARKET)
        row = con.execute(
            "SELECT is_trading_day FROM dim_calendar WHERE date = ?", [_iso(day)]
        ).fetchone()
        return bool(row[0]) if row is not None else False

    def get_index_daily(
        self, index_code: str, start: date | str | None = None, end: date | str | None = None
    ) -> pd.DataFrame:
        """查询指数日线。"""
        con = self._mgr.get_read(MARKET)
        sql = "SELECT index_code, date, close FROM dim_index_daily WHERE index_code = ?"
        params: list[Any] = [index_code]
        if start is not None:
            sql += " AND date >= ?"
            params.append(_iso(start))
        if end is not None:
            sql += " AND date <= ?"
            params.append(_iso(end))
        sql += " ORDER BY date"
        return con.execute(sql, params).fetchdf()

    def get_limit(self, code: str, day: date | str) -> LimitPrice | None:
        """查询某证券某日的涨跌停价。"""
        con = self._mgr.get_read(MARKET)
        row = con.execute(
            "SELECT code, date, limit_up, limit_down FROM dim_limit WHERE code = ? AND date = ?",
            [code, _iso(day)],
        ).fetchone()
        if row is None:
            return None
        return LimitPrice(
            code=row[0],
            date=pd.Timestamp(row[1]).date(),
            limit_up=float(row[2]),
            limit_down=float(row[3]),
        )

    def get_dividend(
        self, code: str, start: date | str | None = None, end: date | str | None = None
    ) -> pd.DataFrame:
        """查询分红送配。"""
        con = self._mgr.get_read(MARKET)
        sql = "SELECT code, ex_date, cash_per_share, share_ratio, rights_ratio FROM dim_dividend WHERE code = ?"
        params: list[Any] = [code]
        if start is not None:
            sql += " AND ex_date >= ?"
            params.append(_iso(start))
        if end is not None:
            sql += " AND ex_date <= ?"
            params.append(_iso(end))
        sql += " ORDER BY ex_date"
        return con.execute(sql, params).fetchdf()

    def get_stock(self, code: str) -> Stock | None:
        """查询单只证券基础信息。"""
        con = self._mgr.get_read(MARKET)
        row = con.execute(
            "SELECT code, name, list_date, delist_date, board, industry, is_st FROM dim_stock WHERE code = ?",
            [code],
        ).fetchone()
        if row is None:
            return None
        return _row_to_stock(row)

    def list_stocks(self, as_of: date | str | None = None) -> pd.DataFrame:
        """列出证券；若给定 ``as_of``，仅返回该日**在市**的证券（时点标的池，红线⑥）。"""
        con = self._mgr.get_read(MARKET)
        sql = "SELECT code, name, list_date, delist_date, board, industry, is_st FROM dim_stock"
        params: list[Any] = []
        if as_of is not None:
            d = _iso(as_of)
            sql += " WHERE list_date <= ? AND (delist_date IS NULL OR delist_date >= ?)"
            params.extend([d, d])
        sql += " ORDER BY code"
        return con.execute(sql, params).fetchdf()

    def get_all_codes(self) -> list[str]:
        """返回所有已知证券代码。"""
        con = self._mgr.get_read(MARKET)
        rows = con.execute("SELECT code FROM dim_stock ORDER BY code").fetchall()
        return [r[0] for r in rows]

    # ══════════════════════ 读：app 库 ══════════════════════

    def get_question(self, qid: str) -> dict[str, Any] | None:
        """查询题目（app 库）。"""
        con = self._mgr.get_read(APP)
        cur = con.execute("SELECT * FROM dim_question WHERE question_id = ?", [qid])
        cols = [c[0] for c in cur.description]
        row = cur.fetchone()
        return None if row is None else dict(zip(cols, row, strict=False))

    def get_order(self, oid: str) -> dict[str, Any] | None:
        """查询订单（app 库）。"""
        con = self._mgr.get_read(APP)
        cur = con.execute("SELECT * FROM fact_order WHERE order_id = ?", [oid])
        cols = [c[0] for c in cur.description]
        row = cur.fetchone()
        return None if row is None else dict(zip(cols, row, strict=False))

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """读取应用参数。"""
        con = self._mgr.get_read(APP)
        row = con.execute("SELECT value FROM app_settings WHERE key = ?", [key]).fetchone()
        return default if row is None else str(row[0])

    def set_setting(self, key: str, value: str) -> None:
        """写入应用参数。"""
        with self._mgr.acquire_write(APP) as con:
            con.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                [key, value, now_bj_naive()],
            )

    # ══════════════════════ 写（自带写锁）══════════════════════

    def _write_upsert(self, layer: Layer, table: str, columns: list[str], df: pd.DataFrame) -> int:
        """通用 upsert：按主键 INSERT OR REPLACE。"""
        if df is None or len(df) == 0:
            return 0
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise DataUnavailable(f"写入 {table} 失败：缺少列 {missing}")
        sub = df.loc[:, columns].copy()
        collist = ", ".join(columns)
        with self._mgr.acquire_write(layer) as con:
            con.register("_up_tmp", sub)
            try:
                con.execute(f"INSERT OR REPLACE INTO {table} ({collist}) SELECT {collist} FROM _up_tmp")
            finally:
                try:
                    con.unregister("_up_tmp")
                except Exception:  # noqa: BLE001
                    pass
        return len(sub)

    def upsert_daily(self, df: pd.DataFrame) -> int:
        """写入日线（复用主键 (code,date) 覆盖）。"""
        return self._write_upsert(MARKET, "fact_daily", DAILY_COLUMNS, df)

    def upsert_minute(self, df: pd.DataFrame) -> int:
        """写入分钟线。"""
        return self._write_upsert(MARKET, "fact_minute", MINUTE_COLUMNS, df)

    def upsert_stocks(self, df: pd.DataFrame) -> int:
        """写入证券基础信息。"""
        return self._write_upsert(MARKET, "dim_stock", STOCK_COLUMNS, df)

    def upsert_calendar(self, df: pd.DataFrame) -> int:
        """写入交易日历。"""
        return self._write_upsert(MARKET, "dim_calendar", CALENDAR_COLUMNS, df)

    def upsert_limit(self, df: pd.DataFrame) -> int:
        """写入涨跌停价。"""
        return self._write_upsert(MARKET, "dim_limit", LIMIT_COLUMNS, df)

    def delete_limit(
        self,
        codes: Sequence[str],
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> int:
        """删除给定证券（可选日期区间）的涨跌停价，返回删除行数。

        用于**全量重算**前清除陈旧行（例如按 Q4 分段后，1996-12-16 之前不应再有涨跌停行）。
        """
        if not codes:
            return 0
        placeholders = ", ".join(["?"] * len(codes))
        sql = f"DELETE FROM dim_limit WHERE code IN ({placeholders})"
        params: list[Any] = list(codes)
        if start is not None:
            sql += " AND date >= ?"
            params.append(_iso(start))
        if end is not None:
            sql += " AND date <= ?"
            params.append(_iso(end))
        with self._mgr.acquire_write(MARKET) as con:
            row = con.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def upsert_index_daily(self, df: pd.DataFrame) -> int:
        """写入指数日线。"""
        return self._write_upsert(MARKET, "dim_index_daily", INDEX_COLUMNS, df)

    def upsert_dividend(self, df: pd.DataFrame) -> int:
        """写入分红送配。"""
        return self._write_upsert(MARKET, "dim_dividend", DIVIDEND_COLUMNS, df)

    def upsert_snapshot(self, df: pd.DataFrame) -> int:
        """写入全市场当前格快照（M5）。"""
        return self._write_upsert(
            MARKET, "fact_minute_snapshot", ["dt", "code", "close", "pct_chg", "amount", "turnover"], df
        )

    # ══════════════════════ 统计 / 运维 ══════════════════════

    def count(self, table: str, layer: Layer = MARKET) -> int:
        """统计表行数。"""
        con = self._mgr.get_read(layer)
        row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0

    def table_exists(self, table: str, layer: Layer = MARKET) -> bool:
        """判断表是否存在。"""
        con = self._mgr.get_read(layer)
        row = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
        ).fetchone()
        return bool(row and row[0] > 0)

    def max_date(self, code: str | None = None) -> date | None:
        """返回日线最大日期（可按 code 过滤）。"""
        con = self._mgr.get_read(MARKET)
        if code is None:
            row = con.execute("SELECT MAX(date) FROM fact_daily").fetchone()
        else:
            row = con.execute("SELECT MAX(date) FROM fact_daily WHERE code = ?", [code]).fetchone()
        if row is None or row[0] is None:
            return None
        return pd.Timestamp(row[0]).date()

    def min_date(self, code: str | None = None) -> date | None:
        """返回日线最小日期。"""
        con = self._mgr.get_read(MARKET)
        if code is None:
            row = con.execute("SELECT MIN(date) FROM fact_daily").fetchone()
        else:
            row = con.execute("SELECT MIN(date) FROM fact_daily WHERE code = ?", [code]).fetchone()
        if row is None or row[0] is None:
            return None
        return pd.Timestamp(row[0]).date()

    def require_stock(self, code: str) -> Stock:
        """查询证券；不存在则抛 NotFound。"""
        stock = self.get_stock(code)
        if stock is None:
            raise NotFound(f"证券不存在：{code}")
        return stock

    # ══════════════════════ 订单 / 结算（app 库）══════════════════════

    def insert_order(self, row: dict[str, Any]) -> None:
        """写入订单（含 ``params_snapshot``，红线④）。"""
        cols = [
            "order_id",
            "question_id",
            "session_id",
            "dt",
            "side",
            "shares",
            "price",
            "params_snapshot",
            "knowledge_mode",
            "viewed_knowledge",
            "judge_verdict",
            "judge_advice",
            "created_at",
        ]
        values = [row.get(c) for c in cols]
        placeholders = ", ".join(["?"] * len(cols))
        with self._mgr.acquire_write(APP) as con:
            con.execute(
                f"INSERT OR REPLACE INTO fact_order ({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )

    def list_orders(self, question_id: str) -> list[dict[str, Any]]:
        """按时间升序返回某题目的全部订单。"""
        con = self._mgr.get_read(APP)
        cur = con.execute("SELECT * FROM fact_order WHERE question_id = ? ORDER BY dt", [question_id])
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]

    def get_latest_order(self, question_id: str) -> dict[str, Any] | None:
        """返回某题目最近一笔订单。"""
        orders = self.list_orders(question_id)
        return orders[-1] if orders else None

    def upsert_settlement(self, row: dict[str, Any]) -> None:
        """写入结算结果（冻结）。"""
        cols = [
            "order_id",
            "account_return",
            "stock_return",
            "benchmark_return",
            "alpha",
            "opp_cost",
            "max_dd",
            "hold_all_return",
            "fee_detail",
            "created_at",
        ]
        values = [row.get(c) for c in cols]
        placeholders = ", ".join(["?"] * len(cols))
        with self._mgr.acquire_write(APP) as con:
            con.execute(
                f"INSERT OR REPLACE INTO fact_settlement ({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )

    def get_settlement(self, order_id: str) -> dict[str, Any] | None:
        """按订单号查询结算结果。"""
        con = self._mgr.get_read(APP)
        cur = con.execute("SELECT * FROM fact_settlement WHERE order_id = ?", [order_id])
        cols = [c[0] for c in cur.description]
        row = cur.fetchone()
        return None if row is None else dict(zip(cols, row, strict=False))

    def get_settlement_by_question(self, question_id: str) -> dict[str, Any] | None:
        """查询某题目最近一笔订单的结算结果（经 fact_order 关联）。"""
        con = self._mgr.get_read(APP)
        cur = con.execute(
            "SELECT s.* FROM fact_settlement s JOIN fact_order o ON s.order_id = o.order_id "
            "WHERE o.question_id = ? ORDER BY o.dt DESC LIMIT 1",
            [question_id],
        )
        cols = [c[0] for c in cur.description]
        row = cur.fetchone()
        return None if row is None else dict(zip(cols, row, strict=False))


def _row_to_stock(row: Sequence[Any]) -> Stock:
    """把查询行转换为 Stock。"""
    return Stock(
        code=row[0],
        name=row[1] or "",
        list_date=pd.Timestamp(row[2]).date() if row[2] is not None else None,
        delist_date=pd.Timestamp(row[3]).date() if row[3] is not None else None,
        board=row[4] or "other",
        industry=row[5] or "",
        is_st=bool(row[6]) if row[6] is not None else False,
    )


__all__ = ["Repository"]
