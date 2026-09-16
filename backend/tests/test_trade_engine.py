"""红线⑤ / FR-4.8：TradeEngine 唯一成交实现 —— 下单/成交/**T+1 开盘价**/持仓/现金/T+1/涨跌停/停牌。

成交时点约定（FR-4.8，默认 T+1 开盘价）：
``place_order(day=决策日 T)`` → 引擎解析成交日（默认 T+1），成交价 = 成交日参考价 × (1 ± 滑点)。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.engine.position import Account, Position, accrue_available
from app.engine.rules import TradeRuleEngine
from app.engine.trade_engine import TradeEngine
from tests.stub_repo import StubRepo, make_daily

CODE = "sh.600000"


def _repo() -> StubRepo:
    """日线夹具：01-03 起为「决策日次日」，01-08 停牌，01-09 一字涨停。"""
    bars = make_daily(
        CODE,
        [
            {"date": "2024-01-02", "open": 10.0, "close": 10.4},  # 决策日 T
            {"date": "2024-01-03", "open": 10.6, "close": 11.0},  # T+1（默认成交日）
            {"date": "2024-01-04", "open": 11.0, "close": 11.4},
            {"date": "2024-01-05", "open": 11.6, "close": 12.0},
            {"date": "2024-01-08", "open": 12.0, "close": 12.0, "is_trade": False, "volume": 0},  # 停牌
            {"date": "2024-01-09", "open": 12.2, "high": 12.2, "low": 12.2, "close": 12.2},  # 一字涨停
            {"date": "2024-01-10", "open": 11.8, "close": 12.4},  # 复牌后正常
        ],
    )
    return StubRepo(
        {CODE: bars},
        limits={(CODE, "2024-01-09"): (12.2, 10.0)},
        trading_days=["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10"],
    )


def _account() -> Account:
    return Account(cash=100000.0, initial_cash=100000.0, position=Position(code=CODE))


# ══════════════════ A：成交价 = T+1 开盘价（默认）══════════════════
def test_fill_price_is_next_open_with_slippage() -> None:
    """**FR-4.1/4.8 核心**：成交价 = T+1 **开盘价** × (1+滑点)，且**不等于** T 日收盘 × (1+滑点)。"""
    eng = TradeEngine(_repo())
    acc = _account()
    fill = eng.place_order(account=acc, code=CODE, side="buy", shares=100, day="2024-01-02")

    t1_open, t_close = 10.6, 10.4
    assert fill.accepted is True
    assert fill.decision_day == date(2024, 1, 2)
    assert fill.fill_day == date(2024, 1, 3)  # T+1
    assert fill.ref_price == pytest.approx(t1_open)  # 参考价 = T+1 开盘
    assert fill.price == pytest.approx(round(t1_open * (1 + 5e-4), 2))  # 10.61
    # 明确断言：**不是** T 日收盘价口径（这是修复前的错误行为）
    assert fill.price != pytest.approx(round(t_close * (1 + 5e-4), 2))


def test_fill_price_source_t0_close_and_t1_close() -> None:
    """FR-4.8：成交价口径可配置（``t0_close`` / ``t1_close``）。"""
    # T 日收盘
    f0 = TradeEngine(_repo(), fill_price_source="t0_close").place_order(
        account=_account(), code=CODE, side="buy", shares=100, day="2024-01-02"
    )
    assert f0.fill_day == date(2024, 1, 2)
    assert f0.ref_price == pytest.approx(10.4)  # T 日收盘
    # T+1 收盘
    f1 = TradeEngine(_repo(), fill_price_source="t1_close").place_order(
        account=_account(), code=CODE, side="buy", shares=100, day="2024-01-02"
    )
    assert f1.fill_day == date(2024, 1, 3)
    assert f1.ref_price == pytest.approx(11.0)  # T+1 收盘


def test_buy_fill_cost_and_position() -> None:
    """买入成交：现金/持仓/费用与手算一致；当日买入不可用（T+1）。"""
    eng = TradeEngine(_repo())
    acc = _account()
    fill = eng.place_order(account=acc, code=CODE, side="buy", shares=100, day="2024-01-02")

    assert fill.accepted is True
    assert fill.price == pytest.approx(10.61)  # T+1 开盘 10.6 × 1.0005 = 10.6053 → 10.61
    assert fill.fee is not None
    assert fill.fee.commission == pytest.approx(5.0)
    assert fill.fee.transfer_fee == pytest.approx(0.01)
    assert fill.fee.total_fee == pytest.approx(5.01)
    assert acc.cash == pytest.approx(98933.99)
    assert acc.position.shares == 100
    assert acc.position.available_shares == 0  # T+1：当日买入不可用
    assert acc.position.avg_cost == pytest.approx(10.6601)


def test_same_day_sell_rejected_by_t1() -> None:
    """T+1：当日买入后不可当日卖出。"""
    eng = TradeEngine(_repo())
    acc = _account()
    eng.place_order(account=acc, code=CODE, side="buy", shares=100, day="2024-01-02")
    sell = eng.place_order(account=acc, code=CODE, side="sell", shares=100, day="2024-01-02")
    assert sell.accepted is False
    assert "T+1" in sell.reason
    assert acc.position.shares == 100  # 未卖出


def test_sell_after_accrue() -> None:
    """次日结转可用后可卖，现金与已实现盈亏正确（成交价 = T+1 开盘）。"""
    eng = TradeEngine(_repo())
    acc = _account()
    eng.place_order(account=acc, code=CODE, side="buy", shares=100, day="2024-01-02")  # 成交 01-03 @10.61
    accrue_available(acc.position)  # T+1 次日结转
    sell = eng.place_order(account=acc, code=CODE, side="sell", shares=100, day="2024-01-03")  # 成交 01-04
    assert sell.accepted is True
    assert sell.fill_day == date(2024, 1, 4)
    assert sell.price == pytest.approx(10.99)  # 11.0 × (1−0.0005) = 10.9945 → 10.99
    assert sell.fee is not None
    assert sell.fee.stamp_tax == pytest.approx(0.55)  # 印花税仅卖出
    assert acc.position.shares == 0
    assert acc.cash == pytest.approx(98933.99 + 1099.0 - 5.56)
    assert sell.realized_pnl == pytest.approx((10.99 - 10.6601) * 100)


def test_one_word_up_buy_rejected_and_deferred() -> None:
    """一字涨停（成交日）：买入被拒并给出顺延日。"""
    eng = TradeEngine(_repo())
    acc = _account()
    fill = eng.place_order(account=acc, code=CODE, side="buy", shares=100, day="2024-01-08")
    assert fill.accepted is False
    assert "一字涨停" in fill.reason
    # 成交日 = 01-09（一字涨停）→ 顺延到下一个可成交日 01-10
    assert fill.fill_day == date(2024, 1, 9)
    assert fill.deferred_to == date(2024, 1, 10)


def test_suspended_rejected() -> None:
    """停牌（成交日）：不可交易。"""
    eng = TradeEngine(_repo())
    acc = _account()
    fill = eng.place_order(account=acc, code=CODE, side="buy", shares=100, day="2024-01-05")
    assert fill.accepted is False
    assert "停牌" in fill.reason
    assert fill.fill_day == date(2024, 1, 8)


def test_buy_rejected_when_cash_insufficient() -> None:
    """现金不足：拒单。"""
    eng = TradeEngine(_repo())
    acc = Account(cash=500.0, initial_cash=500.0, position=Position(code=CODE))
    fill = eng.place_order(account=acc, code=CODE, side="buy", shares=100, day="2024-01-02")
    assert fill.accepted is False
    assert "现金不足" in fill.reason


def test_place_order_is_repeatable_stateful() -> None:
    """N13：place_order 可多次调用，状态落在 account（不写死一题一次）。"""
    eng = TradeEngine(_repo())
    acc = _account()
    f1 = eng.place_order(account=acc, code=CODE, side="buy", shares=50, day="2024-01-02")
    f2 = eng.place_order(account=acc, code=CODE, side="buy", shares=50, day="2024-01-02")
    assert f1.accepted and f2.accepted
    assert acc.position.shares == 100


def test_no_next_trading_day_rejected() -> None:
    """决策日之后无交易日 → 明确拒单，不静默按当日成交。"""
    eng = TradeEngine(_repo())
    fill = eng.place_order(account=_account(), code=CODE, side="buy", shares=100, day="2024-01-10")
    assert fill.accepted is False
    assert "无法确定成交日" in fill.reason


def test_open_seal_blocks_buy_and_is_configurable() -> None:
    """`00` §7 规则#4：开盘价==涨停价 → 保守视为无法买入；可通过开关关闭。"""
    # 开盘即封板但非一字（high≠low）：open==limit_up
    bars = make_daily(
        CODE,
        [
            {"date": "2024-03-01", "open": 5.0, "close": 5.0},
            {"date": "2024-03-04", "open": 5.5, "high": 5.6, "low": 5.4, "close": 5.5},  # open==limit_up
        ],
    )
    repo = StubRepo(
        {CODE: bars},
        limits={(CODE, "2024-03-04"): (5.5, 4.5)},
        trading_days=["2024-03-01", "2024-03-04"],
    )
    # 非一字（有振幅），仅因开盘即封板被拒
    blocked = TradeEngine(repo).place_order(account=_account(), code=CODE, side="buy", shares=100, day="2024-03-01")
    assert blocked.accepted is False
    assert "开盘即封板" in blocked.reason
    assert TradeRuleEngine(repo).is_one_word_up(repo.bars[CODE].iloc[1], limit_up=5.5) is False  # 证明确非一字

    # 关闭开关 → 允许买入
    from app.config import Settings

    cfg = Settings(open_seal_no_buy=False)
    allowed = TradeEngine(repo, config=cfg).place_order(
        account=_account(), code=CODE, side="buy", shares=100, day="2024-03-01"
    )
    assert allowed.accepted is True


def test_apply_dividend() -> None:
    """除权：送股调股数、现金分红入现金（红利税按 0 简化）。"""
    eng = TradeEngine(_repo())
    acc = _account()
    acc.position.shares = 1000
    div = {"cash_per_share": 0.5, "share_ratio": 0.1, "rights_ratio": 0.0}
    res = eng.apply_dividend(div, acc.position, acc)
    assert res["cash_added"] == pytest.approx(500.0)
    assert res["shares_added"] == 100
    assert acc.position.shares == 1100
    assert acc.cash == pytest.approx(100500.0)
