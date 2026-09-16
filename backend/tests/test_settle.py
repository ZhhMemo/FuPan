"""红线④：结算快照冻结 —— 事后改 adj_factor 重算完全一致；含退市平仓（NA3 口径）。"""

from __future__ import annotations

import pandas as pd

from app.engine.position import Account, Position
from app.engine.settle import SettlementEngine
from app.engine.snapshot import SnapshotFreezer
from app.engine.trade_engine import TradeEngine
from tests.stub_repo import StubRepo, make_daily, ts

CODE = "sz.000001"
BENCH = "sh.000300"


def _repo() -> StubRepo:
    bars = make_daily(
        CODE,
        [
            {"date": "2019-12-02", "close": 9.0},
            {"date": "2020-01-02", "close": 10.0},
            {"date": "2020-01-03", "close": 10.5},
            {"date": "2020-01-06", "close": 11.0},
            {"date": "2020-01-07", "close": 10.8},
            {"date": "2020-01-08", "close": 11.5},
            {"date": "2020-01-09", "close": 12.0},
        ],
    )
    idx = pd.DataFrame(
        {
            "index_code": BENCH,
            "date": [pd.Timestamp(d).date() for d in ["2020-01-02", "2020-01-09"]],
            "close": [4000.0, 4100.0],
        }
    )
    return StubRepo(
        {CODE: bars},
        trading_days=["2019-12-02", "2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08", "2020-01-09"],
        index_daily={BENCH: idx},
    )


def _question() -> dict:
    return {
        "question_id": "q1",
        "code": CODE,
        "start_date": "2019-12-02",
        "end_date": "2020-01-02",
        "initial_cash": 100000.0,
        "settle_window": 5,
    }


def _account() -> Account:
    # 下单后账户：以 10.0 买入 100 股，含费用≈1006.01
    return Account(cash=98993.99, initial_cash=100000.0, position=Position(code=CODE, shares=100, avg_cost=10.06))


def test_settle_uses_frozen_snapshot_after_adj_factor_change() -> None:
    """红线④：冻结快照后，即使事后修改 adj_factor，重算结果完全一致。"""
    repo = _repo()
    engine = SettlementEngine(repo, trade_engine=TradeEngine(repo))
    freezer = SnapshotFreezer(repo)

    order = {"order_id": "o1", "price": 10.0, "dt": ts("2020-01-02")}
    exit_day, delisted = engine.resolve_exit_day(CODE, pd.Timestamp("2020-01-02").date(), 5)
    assert str(exit_day) == "2020-01-09"
    assert delisted is False

    snap = freezer.freeze_for_order(
        order,
        code=CODE,
        fill_day="2020-01-02",
        exit_day=exit_day,
        fee_version="v1",
        fill_price=10.0,
        start_day="2019-12-02",
    )
    # 快照落库形态（JSON 字符串）
    order["params_snapshot"] = snap.to_json()

    acc = _account()
    r1 = engine.settle(order, _question(), snapshot=snap, account=acc)

    # 事后修改 adj_factor（模拟 M0 数据被重算/覆盖）——不应影响结算
    repo.bars[CODE]["adj_factor"] = 2.0
    r2 = engine.settle(order, _question(), snapshot=None, account=acc)  # snapshot=None → 从订单 params_snapshot 读取

    d1, d2 = r1.to_dict(), r2.to_dict()
    d1.pop("fee_version", None)
    d2.pop("fee_version", None)
    assert d1 == d2
    assert r1.stock_return == r2.stock_return
    assert r1.exit_day == r2.exit_day


def test_freeze_is_idempotent() -> None:
    """冻结幂等：订单已有冻结快照则原样返回，绝不覆盖。"""
    repo = _repo()
    freezer = SnapshotFreezer(repo)
    order = {"order_id": "o1", "price": 10.0, "dt": ts("2020-01-02")}
    snap1 = freezer.freeze(order, 1.0, "v1", fill_price=10.0, extra={"a": 1})
    order["params_snapshot"] = snap1.to_json()
    snap2 = freezer.freeze(order, 3.0, "v9", fill_price=99.0, extra={"a": 2})
    assert snap2.adj_factor == snap1.adj_factor  # 未被覆盖
    assert snap2.extra.get("a") == 1


def test_settlement_has_no_right_wrong_wording() -> None:
    """结算无「对/错/成功/失败」字样（不判对错）。"""
    repo = _repo()
    engine = SettlementEngine(repo, trade_engine=TradeEngine(repo))
    order = {"order_id": "o1", "price": 10.0, "dt": ts("2020-01-02")}
    res = engine.settle(order, _question(), account=_account())
    text = res.notes
    for w in ("对", "错", "成功", "失败", "追高", "杀跌"):
        assert w not in text


def test_delisted_exit_uses_last_tradable_day() -> None:
    """退市平仓：退出日取最后可交易日（is_trade 口径），而非 MAX(date)。"""
    delisted_code = "sz.300104"
    bars = make_daily(
        delisted_code,
        [
            {"date": "2020-07-01", "close": 1.6},
            {"date": "2020-07-02", "close": 1.55},
            {"date": "2020-07-20", "close": 1.5},  # 最后可交易日
            {"date": "2020-07-21", "close": 1.5, "is_trade": False, "volume": 0},  # 退市日不可交易（NA3）
        ],
    )
    repo = StubRepo(
        {delisted_code: bars},
        trading_days=["2020-07-01", "2020-07-02", "2020-07-20", "2020-07-21"],
    )
    engine = SettlementEngine(repo, trade_engine=TradeEngine(repo))
    exit_day, delisted = engine.resolve_exit_day(delisted_code, pd.Timestamp("2020-07-01").date(), 20)
    assert delisted is True
    assert str(exit_day) == "2020-07-20"  # 不是 2020-07-21（MAX(date) 会错误取到退市日）
