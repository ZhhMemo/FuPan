"""M0 派生表：涨跌停价分板块预计算（四舍五入到分）。

注：本文件为 M0 数据底座的补充单测（设计文件列表未显式列出），
用于验证验收点「涨跌停价预计算正确（按板块区分 ±10% / ±20% / ±30%）」。
"""

from __future__ import annotations

import pandas as pd

from app.data.models import round_to_cent
from app.data.sync.precompute_limit import build_limit_frame, limit_prices


def test_main_board_10pct() -> None:
    """主板 ±10%。"""
    assert limit_prices(10.0, "sh.600000") == (11.0, 9.0)
    assert limit_prices(20.0, "sz.000001") == (22.0, 18.0)


def test_gem_20pct() -> None:
    """创业板 ±20%。"""
    assert limit_prices(10.0, "sz.300750") == (12.0, 8.0)


def test_star_20pct() -> None:
    """科创板 ±20%。"""
    assert limit_prices(10.0, "sh.688981") == (12.0, 8.0)


def test_bse_30pct() -> None:
    """北交所 ±30%。"""
    assert limit_prices(10.0, "bj.830799") == (13.0, 7.0)


def test_rounding_half_up() -> None:
    """四舍五入到分（非银行家舍入）。"""
    assert round_to_cent(11.055) == 11.06
    assert round_to_cent(9.045) == 9.05
    assert limit_prices(10.0, "sh.600000") == (11.0, 9.0)


def test_build_limit_frame_uses_prev_close() -> None:
    """涨跌停基于前收盘；首行无前收被剔除。"""
    df = pd.DataFrame(
        {
            "code": "sz.300750",
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]).date,
            "close": [10.0, 11.0, 12.0],
        }
    )
    frame = build_limit_frame("sz.300750", df)
    assert len(frame) == 2  # 首行剔除
    first = frame.iloc[0]
    assert first["limit_up"] == 12.0  # prev=10.0, +20%
    assert first["limit_down"] == 8.0
    second = frame.iloc[1]
    assert second["limit_up"] == 13.2  # prev=11.0, +20%
    assert second["limit_down"] == 8.8
