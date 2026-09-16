"""SnapshotFreezer：结算快照冻结（红线④）。

红线④：**任何结算/复算只能读 ``fact_order.params_snapshot``，禁止重读当前 ``adj_factor``/费率。**
因此：
- ``freeze`` 在**首次**下单/结算时把「费率版本 + 复权因子 + 成交价 + 结算窗口输入」一并冻结；
- ``freeze`` 具备**幂等性**：若订单已存在冻结快照，则**原样返回**，绝不覆盖；
- ``SettlementEngine`` 一律从快照取值，从而「即使事后修改 ``adj_factor``，重算结果完全一致」。
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
        fee_version: str,
        fill_price: float | None = None,
        start_day: date | str | None = None,
        benchmark_code: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ParamsSnapshot:
        """按证券与日期自动收集「复权因子 + 端点未复权价 + 基准」后冻结（幂等）。

        冻结项全部写入 ``extra``，使得 ``SettlementEngine`` **无需重读当前 ``adj_factor``**，
        从而满足红线④（事后改因子重算完全一致）。

        Args:
            order: 订单。
            code: 证券代码。
            fill_day: 成交日（决策日）。
            exit_day: 结算退出日。
            fee_version: 费率版本。
            fill_price: 成交价；None 则取订单 ``price``。
            start_day: 题目起始日（用于「全程持有对照」）。
            benchmark_code: 基准指数代码（默认沪深300 ``sh.000300``）。
            extra: 额外冻结项。
        """
        existing = self.load(order)
        if existing is not None and FROZEN_MARKER in existing.extra:
            return existing

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
        }
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


__all__ = ["SnapshotFreezer", "FROZEN_MARKER"]
