"""北京时间、交易日历与时间工具。

约定（对齐设计 §21）：
- 全系统时区 ``Asia/Shanghai``（``config.TZ``）；
- 一切"N 个交易日"运算走 ``dim_calendar``（由 ``Repository`` 提供），
  本模块只提供**不依赖数据库**的纯函数工具。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date, datetime, time, timedelta

from dateutil import parser as _dateutil_parser

from app.config import TZ

# 中国大陆 A 股常规交易时段（北京时间），回放/快照用
MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)


def now_bj() -> datetime:
    """返回当前北京时间（带时区）。"""
    return datetime.now(TZ)


def today_bj() -> date:
    """返回当前北京日期。"""
    return now_bj().date()


def now_bj_naive() -> datetime:
    """返回当前北京时间（**naive**，用于写入无时区的 DuckDB TIMESTAMP 列）。"""
    return now_bj().replace(tzinfo=None)


def to_bj(dt: datetime | str) -> datetime:
    """将任意 naive/aware datetime 或字符串转换为北京时间（aware）。"""
    if isinstance(dt, str):
        dt = parse_datetime(dt)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def parse_datetime(value: str | datetime) -> datetime:
    """解析 ISO 8601 日期时间字符串。"""
    if isinstance(value, datetime):
        return value
    return _dateutil_parser.parse(value)


def parse_date(value: str | date | datetime) -> date:
    """解析日期（接受 str/date/datetime）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return _dateutil_parser.parse(value).date()


def to_iso(dt: datetime) -> str:
    """格式化为 ISO 8601（带时区偏移）。"""
    return to_bj(dt).isoformat()


def daterange(start: date, end: date) -> Iterator[date]:
    """生成 [start, end] 闭区间的自然日序列。"""
    cur = start
    one = timedelta(days=1)
    while cur <= end:
        yield cur
        cur += one


def next_trading_day(day: date, trading_days: Iterable[date]) -> date | None:
    """返回不早于 ``day`` 的下一个交易日（含当天）；无则返回 None。"""
    for d in trading_days:
        if d >= day:
            return d
    return None


def prev_trading_day(day: date, trading_days: Iterable[date]) -> date | None:
    """返回不晚于 ``day`` 的上一个交易日（含当天）；无则返回 None。"""
    result: date | None = None
    for d in trading_days:
        if d <= day:
            result = d
        else:
            break
    return result


def shift_trading_days(day: date, n: int, trading_days: list[date]) -> date | None:
    """在交易日序列上前后移动 n 个交易日。

    ``n`` 为正表示向后（未来），为负表示向前（历史）。
    """
    if not trading_days:
        return None
    import bisect

    idx = bisect.bisect_left(trading_days, day)
    if idx < len(trading_days) and trading_days[idx] == day:
        target = idx + n
    else:
        # day 非交易日：以最近的前一个交易日为基准再移动
        base = idx - 1
        target = base + n
    if target < 0 or target >= len(trading_days):
        return None
    return trading_days[target]


__all__ = [
    "now_bj",
    "now_bj_naive",
    "today_bj",
    "to_bj",
    "parse_date",
    "parse_datetime",
    "to_iso",
    "daterange",
    "next_trading_day",
    "prev_trading_day",
    "shift_trading_days",
]
