"""SettlementEngine：账户视角结算（红线④，**不判对错**）。

口径（对齐设计 §21 与验收点）：
- **只用快照**：复权因子 / 端点价 / 基准 / **费率族** / **结算窗口** / **账户状态** 均取自
  ``ParamsSnapshot``，**绝不重读当前 config** →
  「事后改 adj_factor / 费率 / 滑点 / 窗口，重算完全一致」（红线④ / FR-4.6）；
- 账户指标全部为**账户视角**，**不产生** success/fail、对/错、追高/杀跌 等任何字眼；
- 复算复用 ``TradeEngine`` 的净值口径（红线⑤：全市场唯一成交/持仓实现）。

指标定义：
- ``account_return``   账户收益率 = (期末总资产 − 本金) / 本金；
- ``stock_return``     标的自决策点起、以**后复权价**计的区间收益；
- ``benchmark_return`` 基准（默认沪深300）同区间收益；
- ``alpha``            ``account_return − benchmark_return``；
- ``opp_cost``         机会成本 = ``stock_return − account_return``（未足额参与所放弃的收益）；
- ``max_dd``           观察窗口内账户权益（现金 + 持仓×未复权收盘）最大回撤；
- ``hold_all_return``  从题目起始日到退出日的「全程持有对照」收益（后复权）。

退市平仓（NA3 传导）：若标的在观察窗口内退市/停止交易，
退出日取 **最后一个可交易日** —— 口径为 ``WHERE is_trade ORDER BY date DESC LIMIT 1``
（**不可**用 ``MAX(date)``：退市日当天 ``is_trade=False``、无成交，不可成交）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from app.config import Settings
from app.config import settings as default_settings
from app.core.logging import get_logger
from app.data.models import Settlement
from app.data.repository import Repository
from app.engine.cost import CostModel
from app.engine.position import Account, Position
from app.engine.snapshot import SnapshotFreezer
from app.engine.trade_engine import TradeEngine, replay_orders

log = get_logger(__name__)


def _g(obj: Any, key: str, default: Any = None) -> Any:
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


def _as_date(value: Any) -> date | None:
    """归一化为 date；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.date()


class SettlementEngine:
    """结算引擎。

    Args:
        repository: 仓储。
        trade_engine: 交易引擎（净值口径复用）；None 新建。
        freezer: 快照冻结器；None 新建。
        config: 配置。
    """

    def __init__(
        self,
        repository: Repository | None = None,
        trade_engine: TradeEngine | None = None,
        freezer: SnapshotFreezer | None = None,
        config: Settings | None = None,
    ) -> None:
        self._cfg = config or default_settings
        self._repo: Repository = repository or Repository()
        self.trade_engine: TradeEngine = trade_engine or TradeEngine(self._repo, config=self._cfg)
        self.freezer: SnapshotFreezer = freezer or SnapshotFreezer(self._repo, self._cfg)

    # ─────────────── 退出日 ───────────────
    def resolve_exit_day(self, code: str, end_date: date, window: int) -> tuple[date, bool]:
        """确定结算退出日；若窗口内退市/停止交易则取最后可交易日。

        Returns:
            ``(exit_day, delisted)``。
        """
        horizon = end_date + timedelta(days=max(window * 3, 90))
        days = self._repo.get_trading_days(end_date, horizon)
        future = [d for d in days if d > end_date]
        if len(future) >= window:
            target = future[window - 1]
        elif future:
            target = future[-1]
        else:
            target = end_date

        intended = target
        last = self._repo.get_last_tradable(code)
        delisted = False
        if last is not None:
            last_day = _as_date(last["date"])
            if last_day is not None and last_day < intended:
                target = last_day
                delisted = True
        return target, delisted

    # ─────────────── 结算 ───────────────
    def settle(
        self,
        order: Any,
        question: Any,
        snapshot: Any = None,
        *,
        account: Account | None = None,
        window: int | None = None,
        orders: list[Any] | None = None,
    ) -> Settlement:
        """执行结算（**只用快照**，不重读当前因子 / 费率 / 窗口）。

        账户来源优先级（均保证与费率/窗口无关）：
        ① 快照冻结的账户状态（``extra['account']``）；
        ② 传入 ``orders`` → 用**冻结费率**构造的 ``TradeEngine`` 回放（红线⑤）；
        ③ 传入 ``account``（旧口径兜底）。

        Args:
            order: 订单（dict/对象），含 ``order_id`` / ``dt`` / ``price`` / ``params_snapshot``。
            question: 题目（dict/对象），含 ``code`` / ``start_date`` / ``end_date`` / ``initial_cash``。
            snapshot: 已冻结快照（``ParamsSnapshot`` 或 dict）；None 则从订单读取或新建（幂等）。
            account: 下单后的账户（兜底，仅当快照未冻结账户且未传 orders 时使用）。
            window: 覆盖结算窗口（交易日数）；**仅当快照未冻结窗口时**生效。
            orders: 题目全部订单；提供则用冻结费率回放账户。

        Returns:
            ``Settlement``（账户视角，无对错语义）。
        """
        order_id = str(_g(order, "order_id", "") or "")
        code = str(_g(question, "code", _g(order, "code", "")) or "")
        start_date = _as_date(_g(question, "start_date"))
        end_date = _as_date(_g(question, "end_date"))
        initial_cash = float(_g(question, "initial_cash", 100000.0) or 100000.0)

        fill_dt = _g(order, "dt")
        fill_day = _as_date(fill_dt) or end_date
        if fill_day is None or end_date is None:
            raise ValueError("结算失败：缺少决策日/成交日")

        # 1) 取得冻结快照（红线④：优先既有快照，幂等）
        snap = snapshot if snapshot is not None else self.freezer.load(order)
        if snap is None:
            snap = self.freezer.freeze_for_order(
                order,
                code=code,
                fill_day=fill_day,
                exit_day=self.resolve_exit_day(code, end_date, int(window or self._cfg.settle_window))[0],
                fee_version=self._cfg.fee_version,
                fill_price=float(_g(order, "price", 0.0) or 0.0),
                start_day=start_date,
                window=int(window) if window is not None else None,
                account=account,
            )
        extra = getattr(snap, "extra", None)
        if extra is None and isinstance(snap, dict):
            extra = {k: v for k, v in snap.items() if k not in ("fee_version", "adj_factor", "fill_price")}
        extra = extra or {}

        # 2) 结算窗口**只读快照**（缺省才用入参 / 配置）
        win = int(extra.get("settle_window") or (window if window is not None else self._cfg.settle_window))

        exit_day, delisted = self.resolve_exit_day(code, end_date, win)

        af_fill = float(extra.get("adj_factor", getattr(snap, "adj_factor", 1.0)))
        af_exit = float(extra.get("exit_adj_factor", af_fill))
        entry_price = float(getattr(snap, "fill_price", 0.0) or 0.0)
        entry_none = extra.get("entry_close_none")
        exit_none = extra.get("exit_close_none")
        if entry_none is None:
            entry_none = self._close_none(code, fill_day)
        if exit_none is None:
            exit_none = self._close_none(code, exit_day)

        # 3) 标的区间收益（后复权，纯用冻结因子）
        stock_return = 0.0
        if entry_none and exit_none and af_fill:
            stock_return = (float(exit_none) * af_exit) / (float(entry_none) * af_fill) - 1.0

        # 4) 账户（**只用快照冻结的费率族 / 账户**；账户视角）
        acc = self._resolve_account(extra, question, orders, account, code, initial_cash)
        exit_price = float(exit_none) if exit_none else 0.0
        exit_value = float(acc.cash) + int(acc.position.shares) * exit_price
        account_return = (exit_value - initial_cash) / initial_cash if initial_cash else 0.0

        # 5) 基准收益（冻结的指数端点）
        b_entry = extra.get("benchmark_entry")
        b_exit = extra.get("benchmark_exit")
        benchmark_return = 0.0
        if b_entry and b_exit and float(b_entry) != 0:
            benchmark_return = float(b_exit) / float(b_entry) - 1.0

        # 6) 超额 / 机会成本
        alpha = account_return - benchmark_return
        opp_cost = stock_return - account_return

        # 7) 最大回撤（窗口内账户权益路径；只用未复权收盘，不涉因子）
        path_df = self._repo.get_daily(code, start=fill_day, end=exit_day)
        max_dd, hold_days = self._max_drawdown(acc, path_df)

        # 8) 全程持有对照（起始日 → 退出日，后复权；只用冻结因子）
        hold_all_return = stock_return
        start_none = extra.get("start_close_none")
        af_start = float(extra.get("start_adj_factor", af_fill))
        if start_none and exit_none and af_start:
            hold_all_return = (float(exit_none) * af_exit) / (float(start_none) * af_start) - 1.0

        return Settlement(
            order_id=order_id,
            account_return=round(account_return, 6),
            stock_return=round(stock_return, 6),
            benchmark_return=round(benchmark_return, 6),
            alpha=round(alpha, 6),
            opp_cost=round(opp_cost, 6),
            max_dd=round(max_dd, 6),
            hold_all_return=round(hold_all_return, 6),
            entry_price=round(entry_price, 4),
            exit_price=round(exit_price, 4),
            exit_day=exit_day,
            hold_days=hold_days,
            delisted=delisted,
            fee_version=str(getattr(snap, "fee_version", self._cfg.fee_version)),
            notes="账户视角结果：仅呈现收益与风险指标",
        )

    # ─────────────── 账户解析（冻结优先）───────────────
    def _resolve_account(
        self,
        extra: dict[str, Any],
        question: Any,
        orders: list[Any] | None,
        account: Account | None,
        code: str,
        initial_cash: float,
    ) -> Account:
        """按「冻结账户 → 冻结费率回放 → 传入账户 → 空仓」优先级还原下单后账户。"""
        frozen = extra.get("account")
        if isinstance(frozen, dict) and frozen:
            return Account(
                cash=float(frozen.get("cash", 0.0) or 0.0),
                initial_cash=float(frozen.get("initial_cash", initial_cash) or initial_cash),
                position=Position(
                    code=code,
                    shares=int(frozen.get("shares", 0) or 0),
                    available_shares=int(frozen.get("available_shares", 0) or 0),
                    avg_cost=float(frozen.get("avg_cost", 0.0) or 0.0),
                ),
            )
        if orders:
            frozen_cm = CostModel.from_params(extra)
            frozen_engine = TradeEngine(self._repo, cost_model=frozen_cm, config=self._cfg)
            return replay_orders(frozen_engine, question, orders)
        if account is not None:
            return account
        return Account(cash=initial_cash, initial_cash=initial_cash, position=Position(code=code))

    # ─────────────── 工具 ───────────────
    def _close_none(self, code: str, day: date | None) -> float | None:
        """未复权收盘价（缺失返回 None）。"""
        if day is None:
            return None
        df = self._repo.get_daily(code, start=day, end=day)
        if df is None or df.empty:
            return None
        val = df.iloc[0].get("close")
        return None if val is None or pd.isna(val) else float(val)

    def _max_drawdown(self, account: Account, path_df: pd.DataFrame) -> tuple[float, int]:
        """账户权益最大回撤（现金 + 持仓×未复权收盘）。"""
        if path_df is None or path_df.empty:
            return 0.0, 0
        closes = pd.to_numeric(path_df["close"], errors="coerce").fillna(0.0)
        shares = int(account.position.shares)
        equity = float(account.cash) + shares * closes
        if equity.empty:
            return 0.0, 0
        running_max = equity.cummax()
        dd = (equity - running_max) / running_max.replace(0.0, pd.NA)
        dd = dd.fillna(0.0)
        max_dd = float(-dd.min()) if len(dd) else 0.0
        return max(0.0, max_dd), int(len(path_df))


__all__ = ["SettlementEngine"]
