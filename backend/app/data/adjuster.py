"""PriceAdjuster：复权口径的唯一算法源（红线②）。

约定：
- 存储恒为「**不复权 OHLCV + adj_factor**」；
- 前复权/后复权一律**现算**，绝不落库（除权后全部历史会变）；
- 三口径可互推：
    - ``hfq = none × factor``
    - ``qfq = none × factor / factor_last``（锚定区间最后一行）
    - 因此 ``qfq = hfq / factor_last``，``none = hfq / factor``

任何地方不得自行实现复权。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.core.errors import DataUnavailable
from app.data.models import AdjustMode

_PRICE_COLS = ("open", "high", "low", "close")
_VOLUME_COL = "volume"


def _norm_mode(mode: AdjustMode | str) -> AdjustMode:
    """把 str/AdjustMode 归一化为 AdjustMode。"""
    if isinstance(mode, AdjustMode):
        return mode
    try:
        return AdjustMode(str(mode).lower())
    except ValueError as exc:
        raise DataUnavailable(f"未知复权口径：{mode}（应为 qfq/hfq/none）") from exc


class PriceAdjuster:
    """复权计算器。

    Args:
        repository: 可选 ``Repository``，用于 ``factor(code, date)`` 查询复权因子。
    """

    def __init__(self, repository: object | None = None) -> None:
        self._repo = repository

    # ─────────────── 因子查询 ───────────────
    def factor(self, code: str, day: date) -> float:
        """查询某证券某日的不复权→后复权因子。

        Args:
            code: 证券代码（如 ``sh.600000``）。
            day: 日期。

        Returns:
            ``adj_factor``（缺失时返回 1.0，表示无复权）。
        """
        if self._repo is None:
            raise DataUnavailable("PriceAdjuster 未绑定 Repository，无法查询复权因子")
        df = self._repo.get_daily(code, start=day, end=day)  # type: ignore[attr-defined]
        if df is None or len(df) == 0:
            return 1.0
        value = df.iloc[0].get("adj_factor", 1.0)
        return 1.0 if value is None or pd.isna(value) else float(value)

    # ─────────────── 核心：复权 ───────────────
    def adjust(self, df: pd.DataFrame, mode: AdjustMode | str = AdjustMode.QFQ) -> pd.DataFrame:
        """把不复权 DataFrame 转换为指定复权口径。

        Args:
            df: 含 ``open/high/low/close/adj_factor``（可选 ``volume``）的 DataFrame。
            mode: 复权口径 ``QFQ`` / ``HFQ`` / ``NONE``。

        Returns:
            复权后的新 DataFrame；``adj_factor`` 列保留（便于互推与审计）。

        Raises:
            DataUnavailable: 缺少 ``adj_factor`` 列或因子非法。
        """
        if df is None:
            return pd.DataFrame()
        out = df.copy()
        if out.empty:
            return out

        mode_enum = _norm_mode(mode)
        if mode_enum is AdjustMode.NONE:
            return out

        if "adj_factor" not in out.columns:
            raise DataUnavailable("复权失败：DataFrame 缺少 adj_factor 列")

        factor = pd.to_numeric(out["adj_factor"], errors="coerce").astype("float64")
        factor = factor.fillna(1.0)

        if mode_enum is AdjustMode.HFQ:
            ratio = factor
        else:  # QFQ
            anchor = float(factor.iloc[-1])
            if anchor == 0 or pd.isna(anchor):
                raise DataUnavailable("复权失败：区间末行 adj_factor 为 0/NaN，无法锚定前复权")
            ratio = factor / anchor

        price_cols = [c for c in _PRICE_COLS if c in out.columns]
        for col in price_cols:
            out[col] = pd.to_numeric(out[col], errors="coerce") * ratio

        if _VOLUME_COL in out.columns:
            # 量价反向调整，保持 amount ≈ price × volume 不变
            vol = pd.to_numeric(out[_VOLUME_COL], errors="coerce") / ratio
            out[_VOLUME_COL] = vol.round().astype("Int64")

        return out

    # ─────────────── 便捷：一次性输出三口径 ───────────────
    def adjust_all(self, df: pd.DataFrame) -> dict[str, pd.DataFrame]:
        """返回三种口径的 DataFrame（便于对比与测试互推）。"""
        return {
            AdjustMode.QFQ.value: self.adjust(df, AdjustMode.QFQ),
            AdjustMode.HFQ.value: self.adjust(df, AdjustMode.HFQ),
            AdjustMode.NONE.value: self.adjust(df, AdjustMode.NONE),
        }


__all__ = ["PriceAdjuster"]
