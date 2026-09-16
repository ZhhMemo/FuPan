"""N6：AdviceEngine —— MA20 方向 → 三档仓位（积极 70% / 中性 40% / 谨慎 10%），红线①。"""

from __future__ import annotations

import pandas as pd

from app.engine.advice import TIER_HIGH, TIER_LOW, TIER_MID, TIER_TARGET, AdviceEngine
from tests.stub_repo import StubRepo, make_daily

CODE = "sz.000001"


def _df(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2019-01-01", periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "code": CODE,
            "date": [d.date() for d in dates],
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "adj_factor": [1.0] * len(closes),
        }
    )


def test_advice_high() -> None:
    """收盘价站上 MA20 且均线上行 → 积极 70%。"""
    closes = [10.0 + i * 0.1 for i in range(30)]
    adv = AdviceEngine(StubRepo()).advise(_df(closes))
    assert adv.tier == TIER_HIGH
    assert adv.target_position == TIER_TARGET[TIER_HIGH] == 0.70
    assert "→" in adv.reason_text and adv.rule_version == "advice-ma20-v1"


def test_advice_low() -> None:
    """收盘价跌破 MA20 且均线下行 → 谨慎 10%。"""
    closes = [20.0 - i * 0.1 for i in range(30)]
    adv = AdviceEngine(StubRepo()).advise(_df(closes))
    assert adv.tier == TIER_LOW
    assert adv.target_position == 0.10


def test_advice_mid() -> None:
    """方向不明（均线走平）→ 中性 40%。"""
    closes = [10.0 if i % 2 == 0 else 11.0 for i in range(30)]
    adv = AdviceEngine(StubRepo()).advise(_df(closes))
    assert adv.tier == TIER_MID
    assert adv.target_position == 0.40


def test_advice_insufficient_data_defaults_mid() -> None:
    """数据不足 → 中性默认。"""
    adv = AdviceEngine(StubRepo()).advise(_df([10.0, 10.1, 10.2, 10.3, 10.4]))
    assert adv.tier == TIER_MID
    assert adv.target_position == 0.40


def test_advice_for_truncates_at_cutoff() -> None:
    """红线①：只用决策点前数据——决策点后虽有暴跌，仍应给「积极」。"""
    up = [10.0 + i * 0.1 for i in range(30)]  # 2019-01-01 .. 2019-01-30 上行
    cutoff = pd.Timestamp("2019-01-30").date().isoformat()
    crash = [13.0 - i * 0.5 for i in range(1, 11)]  # 决策点后暴跌
    bars = make_daily(
        CODE,
        [
            {
                "date": (pd.Timestamp("2019-01-01") + pd.Timedelta(days=i)).date(),
                "close": c,
            }
            for i, c in enumerate(up + crash)
        ],
    )
    repo = StubRepo({CODE: bars})
    adv = AdviceEngine(repo).advise_for(CODE, cutoff)
    assert adv.tier == TIER_HIGH  # 若泄漏未来（暴跌）则会误判为谨慎
