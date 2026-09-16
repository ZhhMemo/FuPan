"""红线③：成交可行性（TradeRuleEngine）——停牌 / 一字板 / T+1 / 涨跌停 / 顺延。"""

from __future__ import annotations

from app.engine.position import Position
from app.engine.rules import TradeRuleEngine
from tests.stub_repo import StubRepo, make_daily


def _normal_bar() -> dict:
    return {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1_000_000, "is_trade": True}


def test_is_suspended() -> None:
    """停牌 / 无成交判为不可交易。"""
    eng = TradeRuleEngine(StubRepo())
    assert eng.is_suspended({"close": 10.0, "is_trade": False, "volume": 0}) is True
    assert eng.is_suspended({"close": 10.0, "is_trade": True, "volume": 0}) is True
    assert eng.is_suspended({"close": None, "is_trade": True, "volume": 100}) is True
    assert eng.is_suspended(_normal_bar()) is False
    assert eng.is_suspended(None) is True


def test_one_word_up_blocks_buy() -> None:
    """一字涨停：无法买入。"""
    eng = TradeRuleEngine(StubRepo())
    bar = {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 100, "is_trade": True}
    assert eng.is_one_word_up(bar, limit_up=11.0) is True
    assert eng.can_buy(bar, limit_up=11.0) is False
    # 非一字（有波动）则可买
    assert eng.can_buy(_normal_bar(), limit_up=11.0) is True


def test_one_word_down_blocks_sell() -> None:
    """一字跌停：无法卖出。"""
    eng = TradeRuleEngine(StubRepo())
    bar = {"open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0, "volume": 100, "is_trade": True}
    pos = Position(shares=100, available_shares=100, avg_cost=10.0)
    assert eng.is_one_word_down(bar, limit_down=9.0) is True
    assert eng.can_sell(bar, position=pos, limit_down=9.0) is False


def test_t1_available_shares_blocks_sell() -> None:
    """T+1：当日买入（可用=0）不可卖。"""
    eng = TradeRuleEngine(StubRepo())
    bar = _normal_bar()
    pos_fresh = Position(shares=100, available_shares=0, avg_cost=10.0)
    pos_old = Position(shares=100, available_shares=100, avg_cost=10.0)
    assert eng.can_sell(bar, position=pos_fresh, limit_down=9.0) is False
    assert eng.can_sell(bar, position=pos_old, limit_down=9.0) is True


def test_limit_prices_segment_aware() -> None:
    """涨跌停价：无预计算时按板块 + 日期分段现算（Q4）。"""
    eng = TradeRuleEngine(StubRepo())
    up1, down1 = eng.limit_prices("sz.300750", "2020-08-20", prev_close=100.0)
    up2, down2 = eng.limit_prices("sz.300750", "2020-08-25", prev_close=100.0)
    assert (up1, down1) == (110.0, 90.0)  # 创业板 2020-08-24 之前 ±10%
    assert (up2, down2) == (120.0, 80.0)  # 2020-08-24 起 ±20%


def test_next_tradable_skip_one_word() -> None:
    """顺延：一字涨停日不可买，顺延到下一个可成交日。"""
    bars = make_daily(
        "sz.000001",
        [
            {"date": "2024-01-02", "close": 11.0, "open": 11.0, "high": 11.0, "low": 11.0, "volume": 0},
            {"date": "2024-01-03", "close": 11.2, "open": 11.0, "high": 11.5, "low": 10.9, "volume": 1_000_000},
            {"date": "2024-01-04", "close": 11.3, "volume": 1_000_000},
        ],
    )
    repo = StubRepo(
        {"sz.000001": bars},
        trading_days=["2024-01-02", "2024-01-03", "2024-01-04"],
    )
    eng = TradeRuleEngine(repo)
    # 2024-01-02 是非交易日（volume=0 → 视为停牌/无成交），顺延到 01-03
    nxt = eng.next_tradable("sz.000001", "2024-01-02", "buy")
    assert str(nxt) == "2024-01-03"


# ══════════════════ `00` §7 规则#4：开盘即封板 ══════════════════
def test_open_seal_blocks_buy() -> None:
    """开盘价 == 涨停价 → 视为无法买入（保守规则）。"""
    eng = TradeRuleEngine(StubRepo())
    # 有振幅（非一字），但开盘价触及涨停
    bar = {"open": 11.0, "high": 11.5, "low": 10.8, "close": 11.2, "volume": 1_000_000, "is_trade": True}
    assert eng.is_open_sealed_up(bar, limit_up=11.0) is True
    assert eng.is_one_word_up(bar, limit_up=11.0) is False  # 非一字
    assert eng.can_buy(bar, limit_up=11.0) is False
    # 开盘价未触涨停 → 可买
    normal = {"open": 10.0, "high": 11.5, "low": 9.8, "close": 11.2, "volume": 1_000_000, "is_trade": True}
    assert eng.can_buy(normal, limit_up=11.0) is True


def test_open_seal_switch_off_allows_buy() -> None:
    """配置开关关闭时，开盘即封板不再禁止买入。"""
    from app.config import Settings

    eng = TradeRuleEngine(StubRepo(), Settings(open_seal_no_buy=False))
    bar = {"open": 11.0, "high": 11.5, "low": 10.8, "close": 11.2, "volume": 1_000_000, "is_trade": True}
    assert eng.open_seal_blocks_buy(bar, limit_up=11.0) is False
    assert eng.can_buy(bar, limit_up=11.0) is True

