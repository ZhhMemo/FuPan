"""红线⑥：UniverseProvider 时点标的池（防幸存者偏差）。

验证：指定历史日，标的池**不含未上市 / 已退市**股票。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.config import Settings
from app.core.db import DuckDBManager
from app.data.repository import Repository
from app.data.universe import UniverseProvider


@pytest.fixture()
def repo(tmp_path) -> Repository:
    """构建一个临时的 market/app 库并写入 3 只样本证券。"""
    cfg = Settings(data_root=tmp_path)
    mgr = DuckDBManager(cfg)
    mgr.init_schemas()
    r = Repository(mgr)
    r.upsert_stocks(
        pd.DataFrame(
            {
                "code": ["sh.600000", "sh.600001", "sz.300999"],
                "name": ["浦发银行", "退市样本", "次新股"],
                "list_date": [date(1999, 11, 10), date(2000, 1, 1), date(2025, 1, 1)],
                "delist_date": [None, date(2015, 1, 1), None],
                "board": ["main", "main", "gem"],
                "industry": ["银行", "其他", "其他"],
                "is_st": [False, False, False],
                "updated_at": [pd.Timestamp("2024-01-01")] * 3,
            }
        )
    )
    yield r
    mgr.close()


def test_as_of_excludes_not_yet_listed_and_delisted(repo: Repository) -> None:
    """2020-01-01：仅剩在市的 sh.600000（sh.600001 已退市、sz.300999 未上市）。"""
    provider = UniverseProvider(repo)
    codes = provider.as_of_codes("2020-01-01")
    assert codes == ["sh.600000"]


def test_as_of_historical_includes_then_listed(repo: Repository) -> None:
    """2010-06-01：sh.600000 与 sh.600001 均在市，sz.300999 尚未上市。"""
    provider = UniverseProvider(repo)
    codes = provider.as_of_codes("2010-06-01")
    assert set(codes) == {"sh.600000", "sh.600001"}
    assert "sz.300999" not in codes


def test_as_of_after_new_listing(repo: Repository) -> None:
    """2025-06-01：sz.300999 已上市。"""
    provider = UniverseProvider(repo)
    codes = provider.as_of_codes("2025-06-01")
    assert set(codes) == {"sh.600000", "sz.300999"}


def test_delist_date_inclusive(repo: Repository) -> None:
    """退市日当天视为仍在市（闭区间），次日已退市。"""
    provider = UniverseProvider(repo)
    assert provider.is_listed_on("sh.600001", date(2015, 1, 1)) is True
    assert provider.is_listed_on("sh.600001", date(2015, 1, 2)) is False


def test_filter_listed(repo: Repository) -> None:
    """按给定代码集合过滤在市证券。"""
    provider = UniverseProvider(repo)
    out = provider.filter_listed(["sh.600000", "sh.600001"], "2020-01-01")
    assert out == ["sh.600000"]


def test_as_of_returns_stock_objects(repo: Repository) -> None:
    """返回 Stock 值对象且字段正确。"""
    provider = UniverseProvider(repo)
    stocks = provider.as_of("2020-01-01")
    assert len(stocks) == 1
    assert stocks[0].code == "sh.600000"
    assert stocks[0].board == "main"
    assert stocks[0].delist_date is None
