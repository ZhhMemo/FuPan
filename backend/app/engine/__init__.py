"""交易与结算引擎（两模式共用）。

红线③/④/⑤ 的结构性落点：
- ``rules.TradeRuleEngine``      成交可行性（T+1 / 一字板 / 停牌 / 板块涨跌停 / 顺延）—— 红线③
- ``snapshot.SnapshotFreezer``    结算快照冻结（费率版本 + 复权因子 + 成交价）—— 红线④
- ``trade_engine.TradeEngine``    唯一成交实现（判断模式与回放模式共用）—— 红线⑤
"""

from __future__ import annotations

from app.engine.advice import Advice, AdviceEngine
from app.engine.cost import CostModel, FeeDetail
from app.engine.position import Account, Position
from app.engine.rules import TradeRuleEngine
from app.engine.settle import SettlementEngine
from app.engine.snapshot import SnapshotFreezer
from app.engine.trade_engine import Fill, TradeEngine

__all__ = [
    "Advice",
    "AdviceEngine",
    "CostModel",
    "FeeDetail",
    "Account",
    "Position",
    "TradeRuleEngine",
    "SettlementEngine",
    "SnapshotFreezer",
    "Fill",
    "TradeEngine",
]
