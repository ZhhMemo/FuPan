"""红线②：PriceAdjuster 复权三口径可互推。

验证：
- ``NONE`` 恒等；
- ``HFQ = NONE × factor``；
- ``QFQ = NONE × factor / factor_last``（锚定区间最后一行）；
- 三口径可互相推导（``QFQ = HFQ / factor_last``，``NONE = HFQ / factor``）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core.errors import DataUnavailable
from app.data.adjuster import PriceAdjuster
from app.data.models import AdjustMode, round_to_cent


@pytest.fixture()
def daily_df() -> pd.DataFrame:
    """构造一段跨"除权日"的不复权日线（adj_factor 在中间跳变）。"""
    return pd.DataFrame(
        {
            "code": ["sh.600000"] * 5,
            "date": pd.to_datetime(
                ["2020-01-02", "2020-01-03", "2020-06-02", "2020-06-03", "2020-06-04"]
            ).date,
            "open": [10.0, 10.5, 10.8, 11.0, 11.2],
            "high": [11.0, 10.8, 11.0, 11.5, 11.6],
            "low": [9.8, 10.2, 10.7, 10.9, 11.0],
            "close": [10.0, 10.5, 10.9, 11.1, 11.3],
            "volume": [1000, 1200, 1100, 1300, 1400],
            "amount": [10000.0, 12600.0, 11990.0, 14430.0, 15820.0],
            "adj_factor": [1.0, 1.0, 1.5, 1.5, 1.5],
            "is_trade": [True] * 5,
        }
    )


def test_none_is_identity(daily_df: pd.DataFrame) -> None:
    """NONE 口径不改动任何价格。"""
    adjuster = PriceAdjuster()
    out = adjuster.adjust(daily_df, AdjustMode.NONE)
    np.testing.assert_allclose(out["close"].to_numpy(), daily_df["close"].to_numpy())
    np.testing.assert_allclose(out["open"].to_numpy(), daily_df["open"].to_numpy())


def test_hfq_equals_none_times_factor(daily_df: pd.DataFrame) -> None:
    """HFQ = NONE × adj_factor。"""
    adjuster = PriceAdjuster()
    out = adjuster.adjust(daily_df, AdjustMode.HFQ)
    expected = (daily_df["close"] * daily_df["adj_factor"]).to_numpy()
    np.testing.assert_allclose(out["close"].to_numpy(), expected)
    assert out["close"].iloc[0] == daily_df["close"].iloc[0]  # 首日因子=1，后复权首日不动


def test_qfq_anchors_to_last_row(daily_df: pd.DataFrame) -> None:
    """QFQ 以区间最后一行为锚：末行价格不变。"""
    adjuster = PriceAdjuster()
    out = adjuster.adjust(daily_df, AdjustMode.QFQ)
    assert out["close"].iloc[-1] == pytest.approx(daily_df["close"].iloc[-1])
    factor_last = float(daily_df["adj_factor"].iloc[-1])
    expected = (daily_df["close"] * daily_df["adj_factor"] / factor_last).to_numpy()
    np.testing.assert_allclose(out["close"].to_numpy(), expected)


def test_three_modes_mutually_derivable(daily_df: pd.DataFrame) -> None:
    """三口径可互推：QFQ = HFQ / factor_last，NONE = HFQ / factor。"""
    adjuster = PriceAdjuster()
    modes = adjuster.adjust_all(daily_df)
    none_close = modes["none"]["close"].to_numpy()
    hfq_close = modes["hfq"]["close"].to_numpy()
    qfq_close = modes["qfq"]["close"].to_numpy()

    factor = daily_df["adj_factor"].to_numpy()
    factor_last = factor[-1]

    np.testing.assert_allclose(none_close, hfq_close / factor)
    np.testing.assert_allclose(qfq_close, hfq_close / factor_last)
    np.testing.assert_allclose(none_close * factor / factor_last, qfq_close)


def test_volume_inverse_scaling(daily_df: pd.DataFrame) -> None:
    """量价反向调整：amount ≈ price × volume 在复权后仍成立。"""
    adjuster = PriceAdjuster()
    out = adjuster.adjust(daily_df, AdjustMode.QFQ)
    amount = out["close"] * out["volume"]
    np.testing.assert_allclose(amount.to_numpy(), daily_df["amount"].to_numpy(), rtol=1e-3)


def test_input_not_mutated(daily_df: pd.DataFrame) -> None:
    """复权不得修改入参。"""
    original = daily_df.copy()
    PriceAdjuster().adjust(daily_df, AdjustMode.HFQ)
    pd.testing.assert_frame_equal(daily_df, original)


def test_missing_factor_raises(daily_df: pd.DataFrame) -> None:
    """缺少 adj_factor 列必须明确报错。"""
    with pytest.raises(DataUnavailable):
        PriceAdjuster().adjust(daily_df.drop(columns=["adj_factor"]), AdjustMode.QFQ)


def test_empty_frame() -> None:
    """空 DataFrame 原样返回。"""
    out = PriceAdjuster().adjust(pd.DataFrame(), AdjustMode.QFQ)
    assert out.empty


def test_round_to_cent_half_up() -> None:
    """价格四舍五入到分（ROUND_HALF_UP）。"""
    assert round_to_cent(11.055) == 11.06
    assert round_to_cent(2.675) == 2.68
    assert round_to_cent(1.005) == 1.01
    assert round_to_cent(9.994) == 9.99
    assert round_to_cent(None) is None
