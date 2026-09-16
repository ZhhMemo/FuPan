"""UniverseProvider：时点标的池（红线⑥，防幸存者偏差）。

基于 ``dim_stock.list_date`` / ``dim_stock.delist_date`` 回答
「某历史日在市的股票有哪些」——即**当日已上市且尚未退市**的股票。

边界约定：**两端取闭区间**——
``list_date <= as_of <= delist_date``（退市日当天视为仍在市，可成交）。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pandas as pd

from app.core.errors import DataUnavailable
from app.data.models import Stock
from app.data.repository import Repository


class UniverseProvider:
    """时点标的池提供者。

    Args:
        repository: 仓储实例。
    """

    def __init__(self, repository: Repository | None = None) -> None:
        self._repo: Repository = repository or Repository()

    def as_of(self, day: date | str) -> list[Stock]:
        """返回某历史日在市的所有股票。

        Args:
            day: 目标日期。

        Returns:
            在市股票列表（按代码升序）。

        Raises:
            DataUnavailable: ``dim_stock`` 为空（尚未初始化）。
        """
        df = self._repo.list_stocks(as_of=day)
        if df is None or df.empty:
            # 区分"确实为空"与"未初始化"：若表本身无任何股票则视为未初始化
            if self._repo.count("dim_stock") == 0:
                raise DataUnavailable("dim_stock 为空，请先执行数据初始化（make init）")
            return []
        return [_row_to_stock(r) for r in df.itertuples(index=False)]

    def as_of_codes(self, day: date | str) -> list[str]:
        """返回某历史日在市的股票代码列表。"""
        return [s.code for s in self.as_of(day)]

    def is_listed_on(self, code: str, day: date | str) -> bool:
        """判断某证券在指定日期是否在市。"""
        stock = self._repo.get_stock(code)
        if stock is None or stock.list_date is None:
            return False
        d = pd.Timestamp(day).date()
        if stock.list_date > d:
            return False
        return not (stock.delist_date is not None and stock.delist_date < d)

    def filter_listed(self, codes: Iterable[str], day: date | str) -> list[str]:
        """在给定代码集合中筛出某日在市的子集。"""
        out: list[str] = []
        for code in codes:
            if self.is_listed_on(code, day):
                out.append(code)
        return out


def _row_to_stock(row: object) -> Stock:
    """把 ``itertuples`` 行转换为 Stock。"""
    get = lambda name: getattr(row, name, None)  # noqa: E731
    list_date = get("list_date")
    delist_date = get("delist_date")
    return Stock(
        code=str(get("code")),
        name=str(get("name") or ""),
        list_date=pd.Timestamp(list_date).date() if list_date is not None and not pd.isna(list_date) else None,
        delist_date=pd.Timestamp(delist_date).date()
        if delist_date is not None and not pd.isna(delist_date)
        else None,
        board=str(get("board") or "other"),
        industry=str(get("industry") or ""),
        is_st=bool(get("is_st")) if get("is_st") is not None else False,
    )


__all__ = ["UniverseProvider"]
