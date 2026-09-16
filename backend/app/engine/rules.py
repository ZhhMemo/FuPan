"""TradeRuleEngine：成交可行性规则（红线③）。

职责（对齐设计 §21 / 验收点）：
- **停牌**：``is_trade=False`` 或当日无成交（``volume<=0``）→ 不可成交；
- **一字板**：一字涨停无法买入、一字跌停无法卖出 → 拒单并**顺延**到下一个可成交日；
- **开盘即封板**（``00`` §7 规则#4，**可配置开关**）：``开盘价 == 涨停价`` → 视为无法买入（保守规则）；
- **板块涨跌停**：优先读 ``dim_limit`` 预计算值，缺失则按前收现算（``models.limit_pct_of``）；
- **T+1**：当日买入不可卖（由 ``Position.available_shares`` 体现）；
- **顺延到分**：``next_tradable(code, start, side)`` 返回下一个可成交交易日。

本模块只回答「能不能成交 / 顺延到哪天」，**不负责现金与持仓变更**（那是 ``TradeEngine``）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import pandas as pd

from app.config import Settings
from app.config import settings as default_settings
from app.core.logging import get_logger
from app.data.models import limit_pct_of, limit_prices_of
from app.data.repository import Repository

log = get_logger(__name__)

_BUY = "buy"
_SELL = "sell"
_PRICE_TOL = 0.005  # 一字板判定容差（元）


def _val(bar: Any, key: str, default: Any = None) -> Any:
    """从 Mapping / dict / pandas.Series / 对象中取字段值。"""
    if bar is None:
        return default
    if isinstance(bar, Mapping):
        return bar.get(key, default)
    if hasattr(bar, "get"):
        try:
            return bar.get(key, default)
        except Exception:  # noqa: BLE001 - 非映射实现
            pass
    return getattr(bar, key, default)


class TradeRuleEngine:
    """成交可行性规则引擎。

    Args:
        repository: 仓储（用于日历 / 涨跌停 / 行情）。
        config: 配置。
    """

    def __init__(self, repository: Repository | None = None, config: Settings | None = None) -> None:
        self._repo: Repository = repository or Repository()
        self._cfg = config or default_settings

    # ─────────────── 停牌 ───────────────
    def is_suspended(self, bar: Any) -> bool:
        """是否停牌/无成交（不可交易）。"""
        if bar is None:
            return True
        is_trade = _val(bar, "is_trade", True)
        if is_trade is False:
            return True
        close = _val(bar, "close")
        if close is None or pd.isna(close) or float(close) <= 0:
            return True
        volume = _val(bar, "volume")
        if volume is not None and not pd.isna(volume) and float(volume) <= 0:
            return True
        return False

    # ─────────────── 涨跌停价 ───────────────
    def limit_prices(
        self,
        code: str,
        day: date | str,
        prev_close: float | None = None,
    ) -> tuple[float | None, float | None]:
        """返回某证券某日的 ``(limit_up, limit_down)``。

        优先读 ``dim_limit``（预计算、含制度分段）；缺失时用 ``prev_close`` 现算。
        """
        try:
            lp = self._repo.get_limit(code, day)
        except Exception:  # noqa: BLE001 - 缺失不应阻断
            lp = None
        if lp is not None:
            return float(lp.limit_up), float(lp.limit_down)

        pc = prev_close
        if pc is None:
            pc = self._repo.get_prev_close(code, day)
        if pc is None:
            return None, None
        pct = limit_pct_of(code, day)
        if pct is None:
            return None, None
        # 取整走 models.limit_prices_of（Decimal HALF_UP，唯一实现）；
        # 不可用 round_to_cent(prev * (1 ± pct))——浮点乘法会「少 1 分」。
        up, down = limit_prices_of(float(pc), pct)
        return (None if up is None else float(up)), (None if down is None else float(down))

    # ─────────────── 一字板 ───────────────
    def is_one_word_up(self, bar: Any, limit_up: float | None = None) -> bool:
        """是否一字涨停（开盘即封、无买入机会）。"""
        o, h, lo, c = (_val(bar, k) for k in ("open", "high", "low", "close"))
        if any(v is None or pd.isna(v) for v in (o, h, lo, c)):
            return False
        flat = abs(float(h) - float(lo)) <= _PRICE_TOL and abs(float(o) - float(c)) <= _PRICE_TOL
        if not flat:
            return False
        if limit_up is not None and abs(float(c) - float(limit_up)) <= _PRICE_TOL:
            return True
        return False

    def is_one_word_down(self, bar: Any, limit_down: float | None = None) -> bool:
        """是否一字跌停（开盘即封、无卖出机会）。"""
        o, h, lo, c = (_val(bar, k) for k in ("open", "high", "low", "close"))
        if any(v is None or pd.isna(v) for v in (o, h, lo, c)):
            return False
        flat = abs(float(h) - float(lo)) <= _PRICE_TOL and abs(float(o) - float(c)) <= _PRICE_TOL
        if not flat:
            return False
        if limit_down is not None and abs(float(c) - float(limit_down)) <= _PRICE_TOL:
            return True
        return False

    # ─────────────── 开盘即封板（`00` §7 规则#4）───────────────
    def is_open_sealed_up(self, bar: Any, limit_up: float | None = None) -> bool:
        """是否「开盘即封板」：``开盘价 == 涨停价``。

        保守规则（**可配置开关**，见 ``config.open_seal_no_buy``）：开盘即以涨停价开板、
        散户难以在开盘价成交 → 视为**无法买入**（但不顺延，与一字板的严格封死不同）。

        Returns:
            ``True`` 表示开盘价触及涨停价。
        """
        if limit_up is None:
            return False
        o = _val(bar, "open")
        if o is None or pd.isna(o):
            return False
        return abs(float(o) - float(limit_up)) <= _PRICE_TOL

    def open_seal_blocks_buy(self, bar: Any, limit_up: float | None = None) -> bool:
        """是否因「开盘即封板」而禁止买入（受配置开关控制）。"""
        if not bool(getattr(self._cfg, "open_seal_no_buy", True)):
            return False
        return self.is_open_sealed_up(bar, limit_up)

    # ─────────────── 可否成交 ───────────────
    def can_buy(self, bar: Any, limit_up: float | None = None) -> bool:
        """可否买入：非停牌 且 非一字涨停 且 非「开盘即封板」（保守规则，可配置）。"""
        if self.is_suspended(bar):
            return False
        if self.is_one_word_up(bar, limit_up):
            return False
        return not self.open_seal_blocks_buy(bar, limit_up)

    def can_sell(self, bar: Any, position: Any = None, limit_down: float | None = None) -> bool:
        """可否卖出：非停牌 且 非一字跌停 且 可用股数 > 0（T+1）。"""
        if self.is_suspended(bar):
            return False
        if self.is_one_word_down(bar, limit_down):
            return False
        if position is not None:
            available = _val(position, "available_shares", None)
            if available is None:
                available = _val(position, "shares", 0)
            if int(available or 0) <= 0:
                return False
        return True

    # ─────────────── 顺延 ───────────────
    def next_tradable(
        self,
        code: str,
        start: date | str,
        side: str,
        max_lookahead: int = 30,
    ) -> date | None:
        """返回 ``start`` 之后（含）第一个**该方向可成交**的交易日。

        Args:
            code: 证券代码。
            start: 起始日期（含）。
            side: ``buy`` / ``sell``。
            max_lookahead: 最多向后查看的自然日窗口。

        Returns:
            可成交交易日；窗口内无则 ``None``。
        """
        start_d = pd.Timestamp(start).date()
        end_d = start_d + pd.Timedelta(days=max_lookahead)
        days = self._repo.get_trading_days(start_d, end_d)
        side_l = side.lower()
        for day in days:
            bar_df = self._repo.get_daily(code, start=day, end=day)
            if bar_df is None or bar_df.empty:
                continue
            bar = bar_df.iloc[0]
            up, down = self.limit_prices(code, day)
            if side_l == _SELL:
                # 顺延判断不依赖具体持仓，只判断"当日是否具备卖出通道"
                if self.can_sell(bar, position=None, limit_down=down):
                    return day
            else:
                if self.can_buy(bar, limit_up=up):
                    return day
        return None


__all__ = ["TradeRuleEngine"]
