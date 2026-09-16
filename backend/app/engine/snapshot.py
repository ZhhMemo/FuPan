"""SnapshotFreezer：结算快照冻结（红线④ / FR-4.6）。

红线④：**任何结算/复算只能读 ``fact_order.params_snapshot``，禁止重读当前 ``adj_factor``/费率/结算窗口。**

因此快照必须冻结**结算所需的全部输入**：
- **复权因子**（成交日 / 退出日 / 起始日）；
- **端点未复权价**（成交日 / 退出日 / 起始日收盘）与**基准指数**端点；
- **费率族**（佣金率 / 最低佣金 / 印花税率 / 过户费率 / 规费率 / 滑点率 + ``fee_version``）；
- **结算窗口**（交易日数）与**成交时点**（口径 / 成交日 / 成交时刻）；
- **下单后的账户状态**（现金 / 股数 / 摊薄成本 / 可用股数）—— 使结算无需用当前费率重放订单。

``freeze`` 具备**幂等性**：若订单已存在冻结快照，则**原样返回**，绝不覆盖。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pandas as pd

from app.config import Settings
from app.config import settings as default_settings
from app.core.errors import DataUnavailable
from app.core.logging import get_logger
from app.core.timeutil import now_bj
from app.data.models import ParamsSnapshot
from app.data.repository import Repository

log = get_logger(__name__)

FROZEN_MARKER = "frozen_at"

# 费率族键（冻结进快照；结算只读这些值，**不重读当前 config**）
FEE_KEYS: tuple[str, ...] = (
    "commission_rate",
    "min_commission",
    "stamp_tax_rate",
    "transfer_fee_rate",
    "regulation_fee_rate",
    "slippage_rate",
)


def _get(obj: Any, key: str, default: Any = None) -> Any:
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


def _parse_snapshot(raw: Any) -> dict[str, Any] | None:
    """把 ``params_snapshot``（JSON 字符串或 dict）解析为 dict。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw or None
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _account_to_dict(account: Any) -> dict[str, Any]:
    """把下单后账户（``Account`` / dict / 对象）转为可 JSON 序列化的冻结字典。"""
    if account is None:
        return {}
    if isinstance(account, dict):
        pos = account.get("position") or {}
        if isinstance(pos, dict):
            return {
                "cash": float(account.get("cash", 0.0) or 0.0),
                "initial_cash": float(account.get("initial_cash", 0.0) or 0.0),
                "shares": int(pos.get("shares", 0) or 0),
                "available_shares": int(pos.get("available_shares", 0) or 0),
                "avg_cost": float(pos.get("avg_cost", 0.0) or 0.0),
            }
    pos = getattr(account, "position", None)
    return {
        "cash": float(getattr(account, "cash", 0.0) or 0.0),
        "initial_cash": float(getattr(account, "initial_cash", 0.0) or 0.0),
        "shares": int(getattr(pos, "shares", 0) or 0) if pos is not None else 0,
        "available_shares": int(getattr(pos, "available_shares", 0) or 0) if pos is not None else 0,
        "avg_cost": float(getattr(pos, "avg_cost", 0.0) or 0.0) if pos is not None else 0.0,
    }


class SnapshotFreezer:
    """结算快照冻结器。

    Args:
        repository: 仓储（用于查询复权因子）。
        config: 配置。
    """

    def __init__(self, repository: Repository | None = None, config: Settings | None = None) -> None:
        self._repo: Repository = repository or Repository()
        self._cfg = config or default_settings

    # ─────────────── 幂等读 ───────────────
    def load(self, order: Any) -> ParamsSnapshot | None:
        """从订单还原已冻结快照；无则返回 None。"""
        data = _parse_snapshot(_get(order, "params_snapshot"))
        if not data:
            return None
        return ParamsSnapshot(
            fee_version=str(data.get("fee_version", self._cfg.fee_version)),
            adj_factor=float(data.get("adj_factor", 1.0)),
            fill_price=float(data.get("fill_price", 0.0)),
            extra={k: v for k, v in data.items() if k not in ("fee_version", "adj_factor", "fill_price")},
        )

    def is_frozen(self, order: Any) -> bool:
        """订单是否已冻结快照。"""
        data = _parse_snapshot(_get(order, "params_snapshot"))
        return bool(data and FROZEN_MARKER in data)

    # ─────────────── 核心：冻结 ───────────────
    def freeze(
        self,
        order: Any,
        adj_factor: float,
        fee_version: str,
        *,
        fill_price: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ParamsSnapshot:
        """冻结快照（**幂等**：订单已有冻结快照则原样返回，绝不覆盖）。

        Args:
            order: 订单（dict 或对象）；可含既有 ``params_snapshot``。
            adj_factor: 成交日复权因子（写入快照）。
            fee_version: 费率版本号。
            fill_price: 成交价（未给则尝试从订单 ``price`` 取）。
            extra: 额外冻结项（复权因子对、未复权价、基准、结算窗口等）。

        Returns:
            ``ParamsSnapshot``。
        """
        existing = self.load(order)
        if existing is not None and FROZEN_MARKER in existing.extra:
            log.info("snapshot_freeze_skip_existing")
            return existing

        fp = fill_price
        if fp is None:
            fp = _get(order, "price", 0.0) or 0.0
        payload_extra: dict[str, Any] = dict(extra or {})
        payload_extra[FROZEN_MARKER] = now_bj().isoformat()
        return ParamsSnapshot(
            fee_version=str(fee_version),
            adj_factor=float(adj_factor),
            fill_price=float(fp),
            extra=payload_extra,
        )

    # ─────────────── 便捷：自动收集因子 ───────────────
    def freeze_for_order(
        self,
        order: Any,
        *,
        code: str,
        fill_day: date | str,
        exit_day: date | str,
        fee_version: str | None = None,
        fill_price: float | None = None,
        start_day: date | str | None = None,
        benchmark_code: str | None = None,
        cost_model: Any = None,
        window: int | None = None,
        account: Any = None,
        fill_price_source: str | None = None,
        decision_day: date | str | None = None,
        fill_ts: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> ParamsSnapshot:
        """按证券与日期自动收集「复权因子 + 端点未复权价 + 基准 + **费率族 + 结算窗口 + 账户**」后冻结（幂等）。

        冻结项全部写入 ``extra``，使得 ``SettlementEngine`` **无需重读当前 ``adj_factor`` / 费率 / 窗口**，
        从而满足红线④ / FR-4.6（事后改因子、改费率、改滑点、改窗口，重算结果完全一致）。

        Args:
            order: 订单。
            code: 证券代码。
            fill_day: **成交日**（默认 T+1）。
            exit_day: 结算退出日。
            fee_version: 费率版本；None 时取 ``cost_model`` / 配置。
            fill_price: 成交价；None 则取订单 ``price``。
            start_day: 题目起始日（用于「全程持有对照」）。
            benchmark_code: 基准指数代码（默认沪深300 ``sh.000300``）。
            cost_model: 成本模型（**冻结其费率族**）；None 用当前配置构造。
            window: 结算窗口（交易日数）；None 取当前配置 ``settle_window``。
            account: 下单后账户（冻结现金 / 股数 / 成本 / 可用）；None 不冻结。
            fill_price_source: 成交价口径（``t1_open`` / ``t1_close`` / ``t0_close``）。
            decision_day: 决策日 T。
            fill_ts: 成交时刻（datetime）。
            extra: 额外冻结项。
        """
        existing = self.load(order)
        if existing is not None and FROZEN_MARKER in existing.extra:
            return existing

        from app.engine.cost import CostModel  # 局部导入，规避潜在循环依赖

        cm = cost_model if cost_model is not None else CostModel(self._cfg)
        fee_params: dict[str, Any] = dict(cm.to_params())
        fee_version = fee_version or str(fee_params.get("fee_version") or self._cfg.fee_version)
        # 兜底：确保规费率字段存在（历史 CostModel 可能未提供）
        fee_params.setdefault("regulation_fee_rate", float(getattr(self._cfg, "regulation_fee_rate", 0.0)))

        bench = benchmark_code or self._cfg.default_index_codes[0]
        merged: dict[str, Any] = {
            "code": code,
            "fill_day": str(pd.Timestamp(fill_day).date()),
            "exit_day": str(pd.Timestamp(exit_day).date()),
            "adj_factor": self._adj_factor(code, fill_day),
            "exit_adj_factor": self._adj_factor(code, exit_day),
            "entry_close_none": self._close_none(code, fill_day),
            "exit_close_none": self._close_none(code, exit_day),
            "benchmark_code": bench,
            "benchmark_entry": self._index_close(bench, fill_day),
            "benchmark_exit": self._index_close(bench, exit_day),
            # ── 费率族 + 结算窗口 + 成交时点（FR-4.6）──
            "fee_version": fee_version,
            "settle_window": int(window if window is not None else self._cfg.settle_window),
            "fill_price_source": str(fill_price_source or getattr(self._cfg, "fill_price_source", "t1_open")),
        }
        merged.update(fee_params)
        if decision_day is not None:
            merged["decision_day"] = str(pd.Timestamp(decision_day).date())
        if fill_ts is not None:
            merged["fill_ts"] = fill_ts.isoformat() if hasattr(fill_ts, "isoformat") else str(fill_ts)
        if account is not None:
            merged["account"] = _account_to_dict(account)
        if start_day is not None:
            merged["start_day"] = str(pd.Timestamp(start_day).date())
            merged["start_adj_factor"] = self._adj_factor(code, start_day)
            merged["start_close_none"] = self._close_none(code, start_day)
        if extra:
            merged.update(extra)
        return self.freeze(
            order,
            float(merged["adj_factor"]),
            fee_version,
            fill_price=fill_price,
            extra=merged,
        )

    def _daily_row(self, code: str, day: date | str) -> pd.Series | None:
        """取某证券某日行情行；无则 None。"""
        try:
            df = self._repo.get_daily(code, start=day, end=day)
        except Exception as exc:  # noqa: BLE001
            raise DataUnavailable(f"快照冻结失败：读取 {code} {day} 出错（{exc}）") from exc
        if df is None or df.empty:
            return None
        return df.iloc[0]

    def _adj_factor(self, code: str, day: date | str) -> float:
        """查询某证券某日复权因子（缺失返回 1.0）。"""
        row = self._daily_row(code, day)
        if row is None:
            return 1.0
        val = row.get("adj_factor", 1.0)
        return 1.0 if val is None or pd.isna(val) else float(val)

    def _close_none(self, code: str, day: date | str) -> float | None:
        """查询某证券某日**未复权**收盘（缺失返回 None）。"""
        row = self._daily_row(code, day)
        if row is None:
            return None
        val = row.get("close")
        return None if val is None or pd.isna(val) else float(val)

    def _index_close(self, index_code: str, day: date | str) -> float | None:
        """查询指数某日收盘（缺失返回 None）。"""
        try:
            df = self._repo.get_index_daily(index_code, start=day, end=day)
        except Exception:  # noqa: BLE001
            return None
        if df is None or df.empty:
            return None
        val = df.iloc[0].get("close")
        return None if val is None or pd.isna(val) else float(val)


__all__ = ["SnapshotFreezer", "FROZEN_MARKER", "FEE_KEYS"]
