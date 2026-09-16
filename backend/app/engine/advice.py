"""AdviceEngine：判卷模式仓位建议（N6，**唯一实现**）。

规则（N6 拍板：最简、可解释、**非 ML / 无训练数据**）：
用**决策点之前**的数据计算 MA20（前复权收盘），按其方向映射三档目标仓位：

    | 条件                         | 档位 | 目标仓位 |
    |------------------------------|------|----------|
    | close > MA20 且 MA20 斜率 > 0 | 积极 | 70%      |
    | close < MA20 且 MA20 斜率 < 0 | 谨慎 | 10%      |
    | 其余（中性）                  | 中性 | 40%      |

约束：
- **只用决策点前数据**（``cutoff = end_date``），严格过 ``VisibilityGuard``（红线①）；
- 必须能用**一句话**说清理由（``reason_text``）；
- 规则携带 ``rule_version``；**建议仅为参考**，最终由用户评判并**自行下单**，
  结算按用户实际下单执行（**不做对错判定**）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from app.config import Settings
from app.config import settings as default_settings
from app.core.logging import get_logger
from app.data.adjuster import PriceAdjuster
from app.data.models import AdjustMode
from app.data.repository import Repository
from app.data.visibility import mask_df

log = get_logger(__name__)

# 三档目标仓位（N6）
TIER_HIGH = "high"
TIER_MID = "mid"
TIER_LOW = "low"
TIER_TARGET: dict[str, float] = {TIER_HIGH: 0.70, TIER_MID: 0.40, TIER_LOW: 0.10}
TIER_LABEL: dict[str, str] = {TIER_HIGH: "积极", TIER_MID: "中性", TIER_LOW: "谨慎"}


@dataclass(slots=True)
class Advice:
    """模型仓位建议（规则式、可解释）。"""

    tier: str
    target_position: float
    reason_text: str
    rule_version: str
    ma: float | None = None
    close: float | None = None
    slope: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class AdviceEngine:
    """判卷模式建议引擎。

    Args:
        repository: 仓储。
        ma_window: 均线窗口（默认 20）。
        config: 配置。
    """

    rule_version: str = "advice-ma20-v1"

    def __init__(
        self,
        repository: Repository | None = None,
        ma_window: int = 20,
        config: Settings | None = None,
    ) -> None:
        self._repo: Repository = repository or Repository()
        self._cfg = config or default_settings
        self.ma_window: int = ma_window
        self._adjuster = PriceAdjuster(self._repo)

    # ─────────────── 核心：给定行情切片出建议 ───────────────
    def advise(self, df: pd.DataFrame) -> Advice:
        """基于行情切片（**必须已截断于决策点**）输出仓位建议。

        Args:
            df: 含 ``close``（及可选 ``adj_factor``）的行情 DataFrame。

        Returns:
            ``Advice``；数据不足时给中性默认。
        """
        closes = self._qfq_closes(df)
        n = self.ma_window
        if closes is None or len(closes) < n + 1:
            return self._default("决策点前交易日不足，无法判定均线方向")
        ma_series = closes.rolling(window=n).mean()
        ma_now = float(ma_series.iloc[-1])
        ma_prev = float(ma_series.iloc[-2])
        close_now = float(closes.iloc[-1])
        slope = ma_now - ma_prev
        if pd.isna(ma_now):
            return self._default("决策点前交易日不足，无法判定均线方向")

        if close_now > ma_now and slope > 0:
            return self._build(TIER_HIGH, ma_now, close_now, slope)
        if close_now < ma_now and slope < 0:
            return self._build(TIER_LOW, ma_now, close_now, slope)
        return self._build(TIER_MID, ma_now, close_now, slope)

    # ─────────────── 便捷：按代码 + 决策点 ───────────────
    def advise_for(self, code: str, cutoff: date | datetime | str, lookback: int = 260) -> Advice:
        """按证券代码与决策点取数（严格过 ``VisibilityGuard``）后出建议。"""
        cutoff_d = pd.Timestamp(cutoff).date()
        start = cutoff_d - timedelta(days=lookback)
        raw = self._repo.get_daily(code, start=start, end=cutoff_d)
        masked = mask_df(raw, cutoff_d, "date")  # 红线①
        advice = self.advise(masked)
        log.info("advice_generated", code=code, cutoff=str(cutoff_d), tier=advice.tier)
        return advice

    # ─────────────── 内部 ───────────────
    def _qfq_closes(self, df: pd.DataFrame) -> pd.Series | None:
        """取得前复权收盘序列（若含 adj_factor 则经 PriceAdjuster 现算）。"""
        if df is None or df.empty or "close" not in df.columns:
            return None
        data = df.copy()
        if "adj_factor" in data.columns:
            try:
                data = self._adjuster.adjust(data, AdjustMode.QFQ)
            except Exception as exc:  # noqa: BLE001 - 复权失败退化为不复权
                log.warning("advice_adjust_failed", error=str(exc))
        return pd.to_numeric(data["close"], errors="coerce").dropna()

    def _build(self, tier: str, ma: float, close: float, slope: float) -> Advice:
        label = TIER_LABEL[tier]
        target = TIER_TARGET[tier]
        n = self.ma_window
        if tier == TIER_HIGH:
            reason = f"收盘价站上 {n} 日均线且均线上行 → 建议{label}（约 {int(target * 100)}% 仓位）"
        elif tier == TIER_LOW:
            reason = f"收盘价跌破 {n} 日均线且均线下行 → 建议{label}（约 {int(target * 100)}% 仓位）"
        else:
            reason = f"收盘价与 {n} 日均线关系及均线方向不明 → 建议{label}（约 {int(target * 100)}% 仓位）"
        return Advice(
            tier=tier,
            target_position=target,
            reason_text=reason,
            rule_version=self.rule_version,
            ma=round(ma, 4),
            close=round(close, 4),
            slope=round(slope, 6),
        )

    def _default(self, reason: str) -> Advice:
        """数据不足时的中性默认。"""
        return Advice(
            tier=TIER_MID,
            target_position=TIER_TARGET[TIER_MID],
            reason_text=f"{reason} → 建议{TIER_LABEL[TIER_MID]}（约 {int(TIER_TARGET[TIER_MID] * 100)}% 仓位）",
            rule_version=self.rule_version,
        )


__all__ = ["AdviceEngine", "Advice", "TIER_HIGH", "TIER_MID", "TIER_LOW", "TIER_TARGET", "TIER_LABEL"]
