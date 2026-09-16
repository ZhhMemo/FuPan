"""红线⑤：TradeEngine 唯一成交实现 —— 下单/成交/持仓/现金/T+1/涨跌停/停牌。"""

from __future__ import annotations

import pytest

from app.engine.position import Account, Position, accrue_available
from app.engine.trade_engine import TradeEngine
from tests.stub_repo import StubRepo, make_daily

CODE = "sh.600000"


def _repo() -> StubRepo:
    bars = make_daily(
        CODE,
        [
            {"date": "2024-01-02", "close": 10.0},
            {"date": "2024-01-03", "close": 12.0},
            {"date": "2024-01-04", "close": 11.0, "open": 11.0, "high": 11.0, "low": 11.0},  # 一字（配 limit）
            {"date": "2024-01-05", "close": 11.0, "is_trade": False, "volume": 0},  # 停牌
        ],
    )
    return StubRepo(
        {CODE: bars},
        limits={(CODE, "2024-01-04"): (11.0, 9.0)},
        trading_days=["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
    )


def _account() -> Account:
    return Account(cash=100000.0, initial_cash=100000.0, position=Position(code=CODE))


def test_buy_fill_cost_and_position() -> None:
    """买入成交：现金/持仓/费用与手算一致；当日买入不可用（T+1）。"""
    eng = TradeEngine(_repo())
    acc = _account()
    fill = eng.place_order(account=acc, code=CODE, side="buy", shares=100, day="2024-01-02")

    assert fill.accepted is True
    assert fill.price == pytest.approx(10.00)  # 10.0 × 1.0005 = 10.0005 → 四舍五入到分 = 10.00
    assert fill.fee is not None
    assert fill.fee.commission == pytest.approx(5.0)
    assert fill.fee.transfer_fee == pytest.approx(0.01)
    assert fill.fee.total_fee == pytest.approx(5.01)
    assert acc.cash == pytest.approx(98994.99)
    assert acc.position.shares == 100
    assert acc.position.available_shares == 0  # T+1：当日买入不可用
    assert acc.position.avg_cost == pytest.approx(10.0501)


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
    """次日结转可用后可卖，现金与已实现盈亏正确。"""
    eng = TradeEngine(_repo())
    acc = _account()
    eng.place_order(account=acc, code=CODE, side="buy", shares=100, day="2024-01-02")
    accrue_available(acc.position)  # T+1 次日结转
    sell = eng.place_order(account=acc, code=CODE, side="sell", shares=100, day="2024-01-03")
    assert sell.accepted is True
    assert sell.price == pytest.approx(11.99)  # 12.0 × (1−0.0005)
    assert sell.fee is not None
    assert sell.fee.stamp_tax == pytest.approx(0.60)  # 印花税仅卖出
    assert acc.position.shares == 0
    assert acc.cash == pytest.approx(98994.99 + 1199.0 - 5.61)
    assert sell.realized_pnl == pytest.approx((11.99 - 10.0501) * 100)


def test_one_word_up_buy_rejected_and_deferred() -> None:
    """一字涨停：买入被拒并给出顺延日。"""
    eng = TradeEngine(_repo())
    acc = _account()
    fill = eng.place_order(account=acc, code=CODE, side="buy", shares=100, day="2024-01-04")
    assert fill.accepted is False
    assert "一字涨停" in fill.reason
    # 顺延到下一个可成交日（01-05 停牌 → 无 → None）
    assert fill.deferred_to is None


def test_suspended_rejected() -> None:
    """停牌：不可交易。"""
    eng = TradeEngine(_repo())
    acc = _account()
    fill = eng.place_order(account=acc, code=CODE, side="buy", shares=100, day="2024-01-05")
    assert fill.accepted is False
    assert "停牌" in fill.reason


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
