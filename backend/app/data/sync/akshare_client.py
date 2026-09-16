"""AkShare 客户端（按需补充）。

原则（设计 §2 / §13）：
- AkShare **仅提供按需接口**，**绝不做定时任务**；
- 主要用于 1 分钟线（回放模式）与龙虎榜等补充数据；
- 采用惰性导入，避免拖慢主流程启动。
"""

from __future__ import annotations

import pandas as pd

from app.core.errors import DataUnavailable
from app.core.logging import get_logger

log = get_logger(__name__)

_AK = None


def _akshare():
    """惰性导入 akshare。"""
    global _AK
    if _AK is None:
        try:
            import akshare as ak  # noqa: PLC0415

            _AK = ak
        except Exception as exc:  # noqa: BLE001
            raise DataUnavailable(f"未安装 akshare：{exc}") from exc
    return _AK


def to_baostock_code(code: str) -> str:
    """把 ``sh.600000`` 转换为 akshare 常用的 ``600000``。"""
    return code.split(".")[-1] if "." in code else code


class AkshareClient:
    """AkShare 按需数据客户端。"""

    def stock_minute_1m(self, code: str, period: str = "1", adjust: str = "") -> pd.DataFrame:
        """获取单只股票的历史分钟线（1 分钟）。

        Args:
            code: 证券代码（``sh.600000`` 或 ``600000``）。
            period: 周期，'1' 表示 1 分钟。
            adjust: 复权参数，'' 为不复权。
        """
        ak = _akshare()
        symbol = to_baostock_code(code)
        try:
            df = ak.stock_zh_a_hist_min_em(symbol=symbol, period=period, adjust=adjust)
        except Exception as exc:  # noqa: BLE001
            raise DataUnavailable(f"akshare 1分钟线获取失败：{code}（{exc}）") from exc
        log.info("akshare_minute_ok", code=code, rows=len(df))
        return df

    def lhb_detail(self, start: str, end: str) -> pd.DataFrame:
        """龙虎榜明细（补充数据，按需调用）。"""
        ak = _akshare()
        try:
            return ak.stock_lhb_detail_em(start_date=start, end_date=end)
        except Exception as exc:  # noqa: BLE001
            raise DataUnavailable(f"akshare 龙虎榜获取失败：{exc}") from exc


__all__ = ["AkshareClient", "to_baostock_code"]
