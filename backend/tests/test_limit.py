"""M0 派生表：涨跌停价分板块预计算（四舍五入到分）。

注：本文件为 M0 数据底座的补充单测（设计文件列表未显式列出），
用于验证验收点「涨跌停价预计算正确（按板块区分 ±10% / ±20% / ±30%）」。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.data.models import limit_pct_of, round_to_cent
from app.data.sync.precompute_limit import (
    DELIST_PERIOD_LIMIT_PCT,
    build_limit_frame,
    delist_period_start,
    limit_prices,
)


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


# ══════════════════ Q4：按日期分段（历史正确性）══════════════════
def test_gem_regime_segmented() -> None:
    """创业板：2020-08-24 起 ±20%，此前 ±10%（Q4）。"""
    assert limit_pct_of("sz.300750", "2020-08-20") == 0.10
    assert limit_pct_of("sz.300750", "2020-08-21") == 0.10
    assert limit_pct_of("sz.300750", "2020-08-24") == 0.20
    assert limit_pct_of("sz.300750", "2020-08-25") == 0.20
    assert limit_prices(100.0, "sz.300750", "2020-08-20") == (110.0, 90.0)
    assert limit_prices(100.0, "sz.300750", "2020-08-25") == (120.0, 80.0)


def test_star_and_bse() -> None:
    """科创板 ±20%、北交所 ±30%。"""
    assert limit_prices(100.0, "sh.688981", "2019-07-22") == (120.0, 80.0)
    assert limit_prices(100.0, "bj.830799", "2022-01-01") == (130.0, 70.0)


def test_pre_1996_no_price_limit() -> None:
    """1996-12-16 之前无统一涨跌停 → 不生成涨跌停（返回 None）。"""
    assert limit_pct_of("sh.600000", "1996-12-15") is None
    assert limit_pct_of("sh.600000", "1996-12-16") == 0.10
    assert limit_prices(100.0, "sh.600000", "1995-01-01") == (None, None)


def test_build_limit_frame_segmented() -> None:
    """逐行按日期分段：同一只创业板股跨 2020-08-24 前后幅度不同。"""
    df = pd.DataFrame(
        {
            "code": "sz.300750",
            "date": pd.to_datetime(
                ["2020-08-19", "2020-08-20", "2020-08-21", "2020-08-24", "2020-08-25"]
            ).date,
            "close": [100.0, 105.0, 120.0, 150.0, 160.0],
        }
    )
    frame = build_limit_frame("sz.300750", df)
    by_date = {str(r["date"]): r for _, r in frame.iterrows()}
    # 2020-08-20：前收 100，±10%
    assert by_date["2020-08-20"]["limit_up"] == 110.0
    assert by_date["2020-08-20"]["limit_down"] == 90.0
    # 2020-08-25：前收 150，±20%
    assert by_date["2020-08-25"]["limit_up"] == 180.0
    assert by_date["2020-08-25"]["limit_down"] == 120.0


# ══════════════════ Q2：退市整理期（近似 10%）══════════════════
def test_delist_period_start_approx() -> None:
    """退市整理期起点 = delist_date 往前 30 个交易日（含退市日）。"""
    days = [date(2020, 5, 1) + timedelta(days=i) for i in range(60)]
    delist_date = days[50]
    start = delist_period_start(days, delist_date)
    assert start == days[21]  # 50 - 29 = 21（含退市日共 30 个）


def test_delist_period_uses_10pct() -> None:
    """退市整理期内按 ±10% 近似（即便板块规则为 ±20%）。"""
    assert DELIST_PERIOD_LIMIT_PCT == 0.10
    days = [date(2020, 6, 1) + timedelta(days=i) for i in range(51)]  # 到 2020-07-21
    delist_date = days[-1]
    df = pd.DataFrame(
        {"code": "sh.688981", "date": days, "close": [100.0] * len(days)}  # 科创板（板块 20%）
    )
    frame = build_limit_frame("sh.688981", df, delist_date=delist_date).reset_index(drop=True)
    start = delist_period_start(days, delist_date)
    in_period = frame[frame["date"] >= start]
    out_period = frame[frame["date"] < start]
    assert (in_period["limit_up"] == 110.0).all()  # 整理期 +10%
    assert (out_period["limit_up"] == 120.0).all()  # 平日 +20%
