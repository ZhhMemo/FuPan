"""TradeEngine：**唯一成交实现**（红线⑤），判断模式与回放模式共用。

设计要点：
- ``place_order`` **无状态**、**可被多次调用**（N13：不写死「一题只能调一次」），
  预留 ``decision_point`` 参数（M1 只用单决策点，多决策点扩展位）；
- 全部账户状态外置于 ``Account``，引擎本身不缓存任何状态；
- **成交时点（FR-4.8，默认 T+1 开盘价）**：``place_order(day=决策日 T)`` 内部解析成交日
  （``fill_price_source`` ∈ ``t1_open``(默认) / ``t1_close`` / ``t0_close``），
  成交价 = 成交日参考价 × (1 ± 滑点)；
- 成交可行性由 ``TradeRuleEngine`` 判定（红线③）：停牌/一字板/开盘即封板 → 拒单并给出顺延日；
- 除权处理 ``apply_dividend``：送股调股数、现金分红入现金（红利税按 0 简化，NA5）。

成交日解析约定（**关键**）：
``day`` 参数是**决策日 T**（用户做决定、提交订单之日）；实际成交发生在按
``fill_price_source`` 解析出的**成交日**（默认 = T 之后的第一个交易日，即 T+1）。
``Fill.dt`` 为**下单时间戳**（决策日），``Fill.fill_day`` 为**实际成交日**。
因此 ``fact_order.dt`` 记录的是决策日，回放（``replay_orders``）以决策日重跑即可完全复现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any

import pandas as pd

from app.config import Settings
from app.config import settings as default_settings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.data.repository import Repository
from app.engine.cost import BUY, SELL, CostModel, FeeDetail
from app.engine.position import Account, Position, apply_buy, apply_sell
from app.engine.rules import TradeRuleEngine

log = get_logger(__name__)

# 合法成交价口径（FR-4.8）
FILL_PRICE_SOURCES: tuple[str, ...] = ("t1_open", "t1_close", "t0_close")
# T+1 解析时向后探查交易日历的自然日窗口
_FILL_LOOKAHEAD_DAYS = 30
# 成交时刻（用于快照留痕）：开盘成交取 09:30，收盘成交取 15:00
_OPEN_TIME = dtime(9, 30)
_CLOSE_TIME = dtime(15, 0)


@dataclass(slots=True)
class Fill:
    """一笔下单的结果（成交或拒单/顺延）。"""

    accepted: bool
    code: str
    side: str
    shares: int
    price: float  # 实际成交价（含滑点）；未成交为 0.0
    ref_price: float  # 参考价（未含滑点）
    dt: datetime  # **下单时间戳**（决策日）
    cash_after: float
    position_after: int
    fee: FeeDetail | None = None
    realized_pnl: float = 0.0
    reason: str = ""  # 拒单原因（停牌/一字板/开盘即封板/现金不足/可用不足）
    deferred_to: date | None = None  # 顺延到的下一个可成交日
    decision_point: date | None = None  # N13 预留：本次下单所属决策点
    decision_day: date | None = None  # 决策日 T（= dt 的日期）
    fill_day: date | None = None  # **实际成交日**（默认 T+1）
    fill_ts: datetime | None = None  # **实际成交时刻**（t1_open → T+1 09:30）
    fill_price_source: str = ""  # 本次采用的成交价口径

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "code": self.code,
            "side": self.side,
            "shares": int(self.shares),
            "price": round(float(self.price), 4),
            "ref_price": round(float(self.ref_price), 4),
            "dt": self.dt.isoformat(),
            "cash_after": round(float(self.cash_after), 2),
            "position_after": int(self.position_after),
            "realized_pnl": round(float(self.realized_pnl), 4),
            "reason": self.reason,
            "deferred_to": self.deferred_to.isoformat() if self.deferred_to else None,
            "decision_point": self.decision_point.isoformat() if self.decision_point else None,
            "decision_day": self.decision_day.isoformat() if self.decision_day else None,
            "fill_day": self.fill_day.isoformat() if self.fill_day else None,
            "fill_ts": self.fill_ts.isoformat() if self.fill_ts else None,
            "fill_price_source": self.fill_price_source,
            "fee": self.fee.to_dict() if self.fee else None,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class TradeEngine:
    """交易引擎（唯一成交实现）。

    Args:
        repository: 仓储（取行情 / 日历 / 涨跌停）。
        cost_model: 成本模型；None 用默认。
        rules: 规则引擎；None 用默认。
        config: 配置。
        fill_price_source: 成交价口径（覆盖 config）；∈ ``t1_open`` / ``t1_close`` / ``t0_close``。
    """

    def __init__(
        self,
        repository: Repository | None = None,
        cost_model: CostModel | None = None,
        rules: TradeRuleEngine | None = None,
        config: Settings | None = None,
        *,
        fill_price_source: str | None = None,
    ) -> None:
        self._cfg = config or default_settings
        self._repo: Repository = repository or Repository()
        self.cost_model: CostModel = cost_model or CostModel(self._cfg)
        self.rules: TradeRuleEngine = rules or TradeRuleEngine(self._repo, self._cfg)
        src = str(fill_price_source or getattr(self._cfg, "fill_price_source", "t1_open")).lower()
        if src not in FILL_PRICE_SOURCES:
            raise ValidationError(f"非法成交价口径：{src}（应为 {'/'.join(FILL_PRICE_SOURCES)}）")
        self.fill_price_source: str = src

    # ─────────────── 行情 ───────────────
    def resolve_bar(self, code: str, day: date | str) -> pd.Series | None:
        """取某证券某日的行情 Bar（不复权）；无数据返回 None。"""
        df = self._repo.get_daily(code, start=day, end=day)
        if df is None or df.empty:
            return None
        return df.iloc[0]

    # ─────────────── 成交日 / 成交价（FR-4.8）───────────────
    def resolve_fill_day(self, code: str, decision_day: date, source: str | None = None) -> date | None:
        """由**决策日 T** 解析**成交日**（默认 T+1）。

        Args:
            code: 证券代码。
            decision_day: 决策日 T。
            source: 覆盖成交价口径；None 用引擎自身口径。

        Returns:
            - ``t0_close``  → 决策日当天（T）；
            - ``t1_open`` / ``t1_close`` → 决策日之后**第一个交易日**（T+1）；
              交易日历缺失时返回 ``None``（调用方据此拒单，明确失败）。
        """
        src = (source or self.fill_price_source).lower()
        if src == "t0_close":
            return decision_day
        horizon = decision_day + timedelta(days=_FILL_LOOKAHEAD_DAYS)
        try:
            days = self._repo.get_trading_days(decision_day, horizon)
        except Exception as exc:  # noqa: BLE001 - 日历不可用 → 明确失败
            log.warning("resolve_fill_day_calendar_failed", error=str(exc))
            days = []
        future = [d for d in days if d > decision_day]
        return future[0] if future else None

    def _fill_ref_price(self, bar: Any, source: str | None = None) -> float:
        """成交参考价（未含滑点）：``t1_open`` 取开盘价，其余取收盘价。"""
        src = (source or self.fill_price_source).lower()
        key = "open" if src == "t1_open" else "close"
        val = bar.get(key) if hasattr(bar, "get") else getattr(bar, key, None)
        return 0.0 if val is None or pd.isna(val) else float(val)

    # ─────────────── 下单 ───────────────
    def place_order(
        self,
        *,
        account: Account,
        code: str,
        side: str,
        shares: int,
        day: date | str,
        dt: datetime | None = None,
        decision_point: date | str | None = None,
        order_id: str | None = None,
        question_id: str | None = None,
        session_id: str | None = None,
    ) -> Fill:
        """下单（**可被多次调用**，状态全部落在 ``account`` 上）。

        成交时点（FR-4.8）：``day`` 为**决策日 T**；引擎按 ``fill_price_source`` 解析**成交日**
        （默认 T+1），成交价 = 成交日参考价 × (1 ± 滑点)。成交可行性（停牌/一字板/开盘即封板）
        一律在**成交日**判定。

        Args:
            account: 账户（**原地修改**：现金 / 持仓）。
            code: 证券代码。
            side: ``buy`` / ``sell``。
            shares: 股数（>0）。
            day: **决策日 T**（用户提交订单之日）。
            dt: 下单时间戳；None 取决策日 15:00。
            decision_point: N13 预留：本次下单所属决策点（M1 = ``day``）。
            order_id / question_id / session_id: 仅透传到 ``Fill``（便于落库）。

        Returns:
            ``Fill``（``accepted=False`` 时含拒单原因与 ``deferred_to``）。

        Raises:
            ValidationError: 方向非法 / 股数 ≤ 0。
        """
        side_l = side.lower()
        if side_l not in (BUY, SELL):
            raise ValidationError(f"非法买卖方向：{side}（应为 buy/sell）")
        if shares is None or int(shares) <= 0:
            raise ValidationError(f"股数必须为正整数：{shares}")
        shares = int(shares)

        decision_day = pd.Timestamp(day).date()
        dp = pd.Timestamp(decision_point).date() if decision_point is not None else decision_day
        src = self.fill_price_source
        # 下单时间戳 = 决策日（fact_order.dt）；成交时间另行解析（Fill.fill_ts）
        order_ts = dt or datetime.combine(decision_day, _CLOSE_TIME)

        # 1) 解析成交日（默认 T+1）
        fill_day = self.resolve_fill_day(code, decision_day, src)
        if fill_day is None:
            return self._reject(
                code, side_l, shares, decision_day, order_ts, account, dp,
                reason="无法确定成交日（交易日历缺少决策点之后的数据）",
                defer_from=decision_day, src=src,
            )

        fill_ts = datetime.combine(fill_day, _OPEN_TIME if src == "t1_open" else _CLOSE_TIME)

        # 2) 成交日行情
        bar = self.resolve_bar(code, fill_day)
        if bar is None:
            return self._reject(
                code, side_l, shares, decision_day, order_ts, account, dp,
                reason=f"成交日 {fill_day} 无行情数据（非交易日/未覆盖）",
                defer_from=fill_day, src=src, fill_day=fill_day, fill_ts=fill_ts,
            )

        if self.rules.is_suspended(bar):
            return self._reject(
                code, side_l, shares, decision_day, order_ts, account, dp,
                reason="成交日停牌/无成交，不可交易",
                defer_from=fill_day, src=src, fill_day=fill_day, fill_ts=fill_ts,
            )

        # 3) 成交参考价（默认 T+1 开盘价）+ 涨跌停
        ref_price = self._fill_ref_price(bar, src)
        if ref_price <= 0:
            return self._reject(
                code, side_l, shares, decision_day, order_ts, account, dp,
                reason="成交日参考价非法（≤0）",
                defer_from=fill_day, src=src, fill_day=fill_day, fill_ts=fill_ts,
            )
        prev_close = self._repo.get_prev_close(code, fill_day)
        limit_up, limit_down = self.rules.limit_prices(code, fill_day, prev_close)

        if side_l == BUY:
            if self.rules.is_one_word_up(bar, limit_up):
                return self._reject(
                    code, side_l, shares, decision_day, order_ts, account, dp,
                    reason="一字涨停，无法买入",
                    defer_from=fill_day, src=src, fill_day=fill_day, fill_ts=fill_ts,
                )
            if self.rules.open_seal_blocks_buy(bar, limit_up):
                return self._reject(
                    code, side_l, shares, decision_day, order_ts, account, dp,
                    reason="开盘即封板（开盘价=涨停价），保守规则视为无法买入",
                    defer_from=fill_day, src=src, fill_day=fill_day, fill_ts=fill_ts,
                )
            fee = self.cost_model.calc(BUY, ref_price, shares)
            need = fee.turnover + fee.total_fee
            if account.cash + 1e-9 < need:
                return self._reject(
                    code, side_l, shares, decision_day, order_ts, account, dp,
                    reason=f"现金不足（需 {need:.2f}，有 {account.cash:.2f}）",
                    defer_from=fill_day, src=src, fill_day=fill_day, fill_ts=fill_ts,
                )
            account.cash = round(account.cash - need, 2)
            if account.position.code == "":
                account.position.code = code
            apply_buy(account.position, fee.exec_price, shares, fee.total_fee)
            # T+1：当日买入不计入可用（available_shares 不变）
            return Fill(
                accepted=True,
                code=code,
                side=side_l,
                shares=shares,
                price=fee.exec_price,
                ref_price=ref_price,
                dt=order_ts,
                cash_after=account.cash,
                position_after=int(account.position.shares),
                fee=fee,
                decision_point=dp,
                decision_day=decision_day,
                fill_day=fill_day,
                fill_ts=fill_ts,
                fill_price_source=src,
            )

        # sell
        if self.rules.is_one_word_down(bar, limit_down):
            return self._reject(
                code, side_l, shares, decision_day, order_ts, account, dp,
                reason="一字跌停，无法卖出",
                defer_from=fill_day, src=src, fill_day=fill_day, fill_ts=fill_ts,
            )
        if int(account.position.available_shares) < shares:
            return self._reject(
                code, side_l, shares, decision_day, order_ts, account, dp,
                reason=f"可用股数不足（T+1，可用 {account.position.available_shares}）",
                defer_from=fill_day, src=src, fill_day=fill_day, fill_ts=fill_ts,
            )
        fee = self.cost_model.calc(SELL, ref_price, shares)
        account.cash = round(account.cash + fee.turnover - fee.total_fee, 2)
        realized = apply_sell(account.position, fee.exec_price, shares)
        return Fill(
            accepted=True,
            code=code,
            side=side_l,
            shares=shares,
            price=fee.exec_price,
            ref_price=ref_price,
            dt=order_ts,
            cash_after=account.cash,
            position_after=int(account.position.shares),
            fee=fee,
            realized_pnl=realized,
            decision_point=dp,
            decision_day=decision_day,
            fill_day=fill_day,
            fill_ts=fill_ts,
            fill_price_source=src,
        )

    def _reject(
        self,
        code: str,
        side: str,
        shares: int,
        day: date,
        ts: datetime,
        account: Account,
        decision_point: date,
        reason: str,
        *,
        defer_from: date | None = None,
        src: str = "",
        fill_day: date | None = None,
        fill_ts: datetime | None = None,
    ) -> Fill:
        """构造一个拒单结果，并给出顺延日（一字板/停牌/开盘即封板才有顺延意义）。"""
        deferred: date | None = None
        start = defer_from or day
        try:
            deferred = self.rules.next_tradable(code, start, side)
        except Exception:  # noqa: BLE001 - 顺延失败不影响拒单结论
            deferred = None
        log.info("order_rejected", code=code, side=side, reason=reason, deferred_to=str(deferred))
        return Fill(
            accepted=False,
            code=code,
            side=side,
            shares=shares,
            price=0.0,
            ref_price=0.0,
            dt=ts,
            cash_after=account.cash,
            position_after=int(account.position.shares),
            fee=None,
            reason=reason,
            deferred_to=deferred,
            decision_point=decision_point,
            decision_day=day,
            fill_day=fill_day,
            fill_ts=fill_ts,
            fill_price_source=src,
        )

    # ─────────────── 除权 ───────────────
    def apply_dividend(self, dividend: Any, position: Any, account: Any) -> dict[str, float]:
        """应用一次除权（送股调股数、现金分红入现金；红利税按 0 简化，NA5）。

        Args:
            dividend: 含 ``cash_per_share`` / ``share_ratio`` / ``rights_ratio``。
            position: 持仓对象（原地修改）。
            account: 账户对象（原地修改）。

        Returns:
            ``{cash_added, shares_added, rights_shares}``。
        """
        def _g(obj: Any, key: str, default: float = 0.0) -> float:
            if obj is None:
                return default
            if isinstance(obj, dict):
                v = obj.get(key, default)
            elif hasattr(obj, "get"):
                v = obj.get(key, default)
            else:
                v = getattr(obj, key, default)
            return default if v is None else float(v)

        shares = int(getattr(position, "shares", 0) or 0)
        cash_ps = _g(dividend, "cash_per_share")
        share_ratio = _g(dividend, "share_ratio")
        rights_ratio = _g(dividend, "rights_ratio")

        cash_added = round(cash_ps * shares, 2)
        shares_added = int(shares * share_ratio)
        rights_shares = int(shares * rights_ratio)  # 配股默认不参与，仅记录（NA5）

        if cash_added:
            account.cash = round(float(account.cash) + cash_added, 2)
        if shares_added:
            position.shares = shares + shares_added
            position.available_shares = int(position.available_shares) + shares_added
        return {"cash_added": cash_added, "shares_added": shares_added, "rights_shares": rights_shares}

    # ─────────────── 净值 ───────────────
    def nav(self, account: Account, price: float) -> float:
        """当前总资产（净值口径，元）。"""
        return account.total_asset(price)


def _qget(obj: Any, key: str, default: Any = None) -> Any:
    """从 dict / dataclass / 对象取字段。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, "get"):
        try:
            return obj.get(key, default)
        except Exception:  # noqa: BLE001
            pass
    return getattr(obj, key, default)


def build_initial_account(question: Any) -> Account:
    """由题目构造**下单前**的初始账户（空仓型 / 持仓型）。

    持仓型的初始持仓为「历史建仓」，故 ``available_shares = shares``（可用）。
    """
    initial_cash = float(_qget(question, "initial_cash", 100000.0) or 100000.0)
    code = str(_qget(question, "code", "") or "")
    acc = Account(cash=initial_cash, initial_cash=initial_cash, position=Position(code=code))
    raw_ip = _qget(question, "initial_position")
    if raw_ip:
        ip = json.loads(raw_ip) if isinstance(raw_ip, str) else raw_ip
        shares = int(ip.get("shares", 0) or 0)
        avg_cost = float(ip.get("avg_cost", 0.0) or 0.0)
        if shares > 0:
            acc.position.shares = shares
            acc.position.available_shares = shares
            acc.position.avg_cost = avg_cost
    return acc


def replay_orders(engine: TradeEngine, question: Any, orders: list[Any]) -> Account:
    """按时间顺序**回放**题目的全部订单，返回下单后的账户。

    回放走同一个 ``TradeEngine.place_order``（红线⑤：全市场唯一成交实现），
    因此账户面板与结算口径完全一致、可复现。

    注：``fact_order.dt`` 记录的是**决策日 T**（下单时间）。回放以 ``dt`` 的日期为决策日
    重跑，成交日由引擎按 ``fill_price_source`` 解析（默认 T+1），与实时下单完全一致。
    """
    acc = build_initial_account(question)
    code = str(_qget(question, "code", "") or acc.position.code)
    ordered = sorted(orders, key=lambda o: _qget(o, "dt") or 0)
    for o in ordered:
        side = str(_qget(o, "side", ""))
        shares = int(_qget(o, "shares", 0) or 0)
        dt = _qget(o, "dt")
        day = dt.date() if hasattr(dt, "date") else dt
        if not side or shares <= 0 or day is None:
            continue
        engine.place_order(account=acc, code=code, side=side, shares=shares, day=day, dt=dt)
    return acc


__all__ = ["TradeEngine", "Fill", "FILL_PRICE_SOURCES", "build_initial_account", "replay_orders"]
