"""CostModel：交易成本模型（**唯一实现**，费率全部可配置 + 版本号）。

约定（对齐设计 §21「成本模型」）：
- 唯一实现本模块；费率全部**可配置**并携带 ``fee_version``；
- 默认值：佣金 万2.5 最低 5 元（双向）、印花税 0.05%（**仅卖出**）、
  过户费 0.001%（双向）、滑点 0.05% 双向；**规费含在全佣内**；
- **禁止硬编码费率**（一律取自 ``config`` 或构造参数）。

成交价口径（含滑点）：
- 买入：``exec_price = ref_price × (1 + slippage_rate)``
- 卖出：``exec_price = ref_price × (1 - slippage_rate)``
- 成交价与各项费用均**四舍五入到分**（half-up）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from app.config import Settings
from app.config import settings as default_settings
from app.core.errors import ValidationError
from app.data.models import round_to_cent

BUY = "buy"
SELL = "sell"
VALID_SIDES = (BUY, SELL)


def _cen(value: float) -> float:
    """四舍五入到分，返回非 None 的 float。"""
    out = round_to_cent(value)
    return 0.0 if out is None else float(out)


@dataclass(slots=True)
class FeeDetail:
    """一笔成交的成本明细（可序列化，用于快照与审计）。"""

    side: str
    shares: int
    ref_price: float  # 参考价（未含滑点）
    exec_price: float  # 成交价（含滑点，四舍五入到分）
    turnover: float  # 成交金额 = exec_price × shares
    commission: float  # 佣金（含规费）
    stamp_tax: float  # 印花税（仅卖出）
    transfer_fee: float  # 过户费（双向）
    slippage: float  # 滑点成本（金额）
    total_fee: float  # commission + stamp_tax + transfer_fee
    fee_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class CostModel:
    """成本模型。

    Args:
        config: 配置源；None 用全局默认。
        commission_rate: 佣金费率（覆盖 config）。
        min_commission: 最低佣金。
        stamp_tax_rate: 印花税率（卖出）。
        transfer_fee_rate: 过户费率（双向）。
        slippage_rate: 滑点率（双向）。
        fee_version: 费率版本号。
    """

    def __init__(
        self,
        config: Settings | None = None,
        *,
        commission_rate: float | None = None,
        min_commission: float | None = None,
        stamp_tax_rate: float | None = None,
        transfer_fee_rate: float | None = None,
        slippage_rate: float | None = None,
        fee_version: str | None = None,
    ) -> None:
        cfg = config or default_settings
        self.commission_rate: float = cfg.commission_rate if commission_rate is None else commission_rate
        self.min_commission: float = cfg.min_commission if min_commission is None else min_commission
        self.stamp_tax_rate: float = cfg.stamp_tax_rate if stamp_tax_rate is None else stamp_tax_rate
        self.transfer_fee_rate: float = cfg.transfer_fee_rate if transfer_fee_rate is None else transfer_fee_rate
        self.slippage_rate: float = cfg.slippage_rate if slippage_rate is None else slippage_rate
        self.fee_version: str = fee_version or cfg.fee_version

    # ─────────────── 成交价 ───────────────
    def exec_price(self, side: str, ref_price: float) -> float:
        """返回计入滑点后的成交价（四舍五入到分）。"""
        s = side.lower()
        if s not in VALID_SIDES:
            raise ValidationError(f"非法买卖方向：{side}（应为 buy/sell）")
        if ref_price is None or ref_price <= 0:
            raise ValidationError(f"参考价非法：{ref_price}")
        raw = ref_price * (1 + self.slippage_rate) if s == BUY else ref_price * (1 - self.slippage_rate)
        return _cen(raw)

    # ─────────────── 费用 ───────────────
    def calc(self, side: str, ref_price: float, shares: int) -> FeeDetail:
        """计算一笔成交的成本明细。

        Args:
            side: ``buy`` / ``sell``。
            ref_price: 成交参考价（未含滑点，一般取当日收盘或下一格价格）。
            shares: 股数（>0）。

        Returns:
            ``FeeDetail``（各项金额四舍五入到分）。

        Raises:
            ValidationError: 方向非法 / 股数 ≤ 0 / 价格非法。
        """
        s = side.lower()
        if s not in VALID_SIDES:
            raise ValidationError(f"非法买卖方向：{side}（应为 buy/sell）")
        if shares is None or shares <= 0:
            raise ValidationError(f"股数必须为正整数：{shares}")
        if ref_price is None or ref_price <= 0:
            raise ValidationError(f"参考价非法：{ref_price}")

        ep = self.exec_price(s, ref_price)
        turnover = _cen(ep * shares)
        commission = _cen(turnover * self.commission_rate)
        if commission < self.min_commission:
            commission = _cen(self.min_commission)
        stamp_tax = _cen(turnover * self.stamp_tax_rate) if s == SELL else 0.0
        transfer_fee = _cen(turnover * self.transfer_fee_rate)
        slippage = _cen(abs(ep - ref_price) * shares)
        total_fee = _cen(commission + stamp_tax + transfer_fee)
        return FeeDetail(
            side=s,
            shares=int(shares),
            ref_price=float(ref_price),
            exec_price=float(ep),
            turnover=float(turnover),
            commission=float(commission),
            stamp_tax=float(stamp_tax),
            transfer_fee=float(transfer_fee),
            slippage=float(slippage),
            total_fee=float(total_fee),
            fee_version=self.fee_version,
        )

    # ─────────────── 现金影响 ───────────────
    def cash_delta(self, fee: FeeDetail) -> float:
        """返回该笔成交对现金的影响（买入为负，卖出为正，均扣减费用）。"""
        if fee.side == BUY:
            return -_cen(fee.turnover + fee.total_fee)
        return _cen(fee.turnover - fee.total_fee)

    def to_params(self) -> dict[str, Any]:
        """导出当前费率参数（写入快照用）。"""
        return {
            "commission_rate": self.commission_rate,
            "min_commission": self.min_commission,
            "stamp_tax_rate": self.stamp_tax_rate,
            "transfer_fee_rate": self.transfer_fee_rate,
            "slippage_rate": self.slippage_rate,
            "fee_version": self.fee_version,
        }


__all__ = ["CostModel", "FeeDetail", "BUY", "SELL", "VALID_SIDES"]
