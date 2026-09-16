"""Position / Account 值对象（持仓与账户）。

- ``avg_cost`` 为**摊薄成本**：买入时按 ``(旧市值成本 + 本次成交额 + 本次手续费) / 新股数``
  更新（**含买入滑点与手续费**），卖出时不变；
- ``available_shares`` 体现 **T+1**：当日买入的股份**不计入**可用，需次日结算后才可用；
- 所有金额以元为单位，价格四舍五入到分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Position:
    """单只证券的持仓。"""

    code: str = ""
    shares: int = 0
    available_shares: int = 0  # T+1：当日买入不可卖
    avg_cost: float = 0.0
    last_price: float = 0.0

    @property
    def is_empty(self) -> bool:
        """是否空仓。"""
        return self.shares <= 0

    def market_value(self, price: float | None = None) -> float:
        """持仓市值。"""
        p = self.last_price if price is None else price
        return float(self.shares) * float(p or self.last_price or 0.0)

    def cost_value(self) -> float:
        """持仓成本金额（摊薄成本 × 股数）。"""
        return float(self.shares) * float(self.avg_cost)

    def unrealized_pnl(self, price: float | None = None) -> float:
        """浮动盈亏（市值 − 成本）。"""
        return self.market_value(price) - self.cost_value()

    def to_dict(self, price: float | None = None) -> dict[str, Any]:
        p = self.last_price if price is None else price
        return {
            "code": self.code,
            "shares": int(self.shares),
            "available_shares": int(self.available_shares),
            "avg_cost": round(float(self.avg_cost), 4),
            "last_price": round(float(p or 0.0), 4),
            "market_value": round(self.market_value(price), 2),
            "unrealized_pnl": round(self.unrealized_pnl(price), 2),
        }


@dataclass(slots=True)
class Account:
    """账户（现金 + 单一/主要持仓）。"""

    cash: float = 0.0
    initial_cash: float = 0.0
    position: Position = field(default_factory=Position)

    def total_asset(self, price: float | None = None) -> float:
        """总资产 = 现金 + 持仓市值。"""
        return float(self.cash) + self.position.market_value(price)

    def position_ratio(self, price: float | None = None) -> float:
        """仓位比例 = 持仓市值 / 总资产。"""
        total = self.total_asset(price)
        if total <= 0:
            return 0.0
        return self.position.market_value(price) / total

    def to_dict(self, price: float | None = None) -> dict[str, Any]:
        return {
            "cash": round(float(self.cash), 2),
            "initial_cash": round(float(self.initial_cash), 2),
            "position": self.position.to_dict(price),
            "total_asset": round(self.total_asset(price), 2),
            "position_ratio": round(self.position_ratio(price), 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Account:
        """从字典还原（测试/持久化用）。"""
        pos = data.get("position") or {}
        p = Position(
            code=pos.get("code", ""),
            shares=int(pos.get("shares", 0)),
            available_shares=int(pos.get("available_shares", 0)),
            avg_cost=float(pos.get("avg_cost", 0.0)),
            last_price=float(pos.get("last_price", 0.0)),
        )
        return cls(cash=float(data.get("cash", 0.0)), initial_cash=float(data.get("initial_cash", 0.0)), position=p)


def apply_buy(position: Position, exec_price: float, shares: int, total_fee: float) -> None:
    """把一笔买入应用到持仓（更新股数与摊薄成本；**当日买入不计入可用**，体现 T+1）。"""
    if shares <= 0:
        return
    old_shares = int(position.shares)
    new_shares = old_shares + int(shares)
    old_cost = old_shares * float(position.avg_cost)
    add_cost = float(exec_price) * int(shares) + float(total_fee)
    position.shares = new_shares
    position.avg_cost = (old_cost + add_cost) / new_shares if new_shares > 0 else 0.0
    position.last_price = float(exec_price)


def apply_sell(position: Position, exec_price: float, shares: int) -> float:
    """把一笔卖出应用到持仓，返回**已实现盈亏**（按摊薄成本计）。

    可用股数同步减少；若卖出全部持仓，则成本与可用清零。
    """
    if shares <= 0:
        return 0.0
    sold = min(int(shares), int(position.shares))
    realized = (float(exec_price) - float(position.avg_cost)) * sold
    position.shares = int(position.shares) - sold
    position.available_shares = max(0, int(position.available_shares) - sold)
    position.last_price = float(exec_price)
    if position.shares <= 0:
        position.avg_cost = 0.0
        position.available_shares = 0
    return realized


def accrue_available(position: Position) -> None:
    """结算日结转：把全部持仓置为可用（T+1 次日生效）。"""
    position.available_shares = int(position.shares)


__all__ = ["Position", "Account", "apply_buy", "apply_sell", "accrue_available"]
