"""VisibilityGuard：前视偏差防火墙（红线①）。

所有 ``Repository`` 返回的行情数据**不得直接出 API 层**，
必须经过本守卫按 ``cutoff`` 截断后才可下发：

- 判断模式 ``cutoff = end_date``
- 回放模式 ``cutoff = current_dt``

本模块在 M0 先建起来，后续所有接口都从它走。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from app.core.errors import DataUnavailable, VisibilityViolation


def _to_timestamp(cutoff: date | datetime | str | pd.Timestamp) -> pd.Timestamp:
    """把 cutoff 归一化为 pandas Timestamp。"""
    if isinstance(cutoff, pd.Timestamp):
        return cutoff
    return pd.Timestamp(cutoff)


class VisibilityGuard:
    """可见性守卫。

    Attributes:
        cutoff: 可见截止点（含）。任何 ``dt > cutoff`` 的数据都不得下发。
    """

    def __init__(self, cutoff: date | datetime | str | pd.Timestamp) -> None:
        self.cutoff: pd.Timestamp = _to_timestamp(cutoff)

    # ── 核心：截断 ──
    def mask(self, df: pd.DataFrame, date_col: str = "date") -> pd.DataFrame:
        """裁剪 DataFrame，仅保留 ``date_col <= cutoff`` 的行。

        Args:
            df: 待裁剪的行情 DataFrame。
            date_col: 时间列名（日线为 ``date``，分钟线为 ``dt``）。

        Returns:
            裁剪后的新 DataFrame（不修改入参）。
        """
        return self.mask_by_col(df, date_col)

    def mask_by_col(self, df: pd.DataFrame, col: str = "date") -> pd.DataFrame:
        """按指定列名裁剪。"""
        if df is None:
            return pd.DataFrame()
        if df.empty:
            return df.copy()
        if col not in df.columns:
            raise DataUnavailable(f"可见性守卫：DataFrame 缺少时间列 '{col}'")
        series = pd.to_datetime(df[col])
        return df.loc[series <= self.cutoff].copy()

    # ── 断言：不得存在未来数据 ──
    def assert_no_future(self, df: pd.DataFrame, date_col: str = "date") -> None:
        """断言 DataFrame 中不存在超过 cutoff 的数据；否则抛异常。

        Args:
            df: 待校验 DataFrame。
            date_col: 时间列名。

        Raises:
            VisibilityViolation: 存在未来数据。
        """
        if df is None or df.empty:
            return
        if date_col not in df.columns:
            raise DataUnavailable(f"可见性守卫：DataFrame 缺少时间列 '{date_col}'")
        series = pd.to_datetime(df[date_col])
        future = series[series > self.cutoff]
        if len(future) > 0:
            raise VisibilityViolation(
                f"检测到 {len(future)} 条超过 cutoff({self.cutoff.date()}) 的未来数据",
                detail={"max_dt": str(series.max()), "cutoff": str(self.cutoff)},
            )

    # ── 便捷 ──
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"VisibilityGuard(cutoff={self.cutoff})"


def mask_df(
    df: pd.DataFrame,
    cutoff: date | datetime | str | pd.Timestamp,
    date_col: str = "date",
) -> pd.DataFrame:
    """模块级便捷函数：对 DataFrame 施加可见性截断。"""
    return VisibilityGuard(cutoff).mask(df, date_col)


def guard(cutoff: date | datetime | str | pd.Timestamp) -> VisibilityGuard:
    """构造一个 VisibilityGuard 的便捷工厂。"""
    return VisibilityGuard(cutoff)


def cutoff_of(question: Any) -> pd.Timestamp:
    """从题目/会话对象推导 cutoff（判断模式 = end_date，回放模式 = current_dt）。"""
    if hasattr(question, "end_date") and question.end_date is not None:
        return _to_timestamp(question.end_date)
    if hasattr(question, "current_dt") and question.current_dt is not None:
        return _to_timestamp(question.current_dt)
    raise DataUnavailable("无法从对象推导可见性 cutoff")


__all__ = ["VisibilityGuard", "mask_df", "guard", "cutoff_of"]
