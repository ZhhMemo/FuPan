"""数据模型（dataclass）与数据语义常量。

包含：证券基础信息、行情 Bar、分红、涨跌停、题目、订单、结算、快照、
健康检查报告等值对象，以及价格精度与板块判定工具。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any


# ══════════════════ 复权口径 ══════════════════
class AdjustMode(StrEnum):
    """复权口径枚举。

    - ``QFQ``  前复权：以区间最后一行为锚（``ratio = factor / factor_last``）
    - ``HFQ``  后复权：以最早行为基准（``ratio = factor``）
    - ``NONE`` 不复权：原样返回

    存储**恒为 NONE + adj_factor**；前/后复权一律由 ``PriceAdjuster`` 现算（红线②）。
    """

    QFQ = "qfq"
    HFQ = "hfq"
    NONE = "none"


# ══════════════════ 板块 ══════════════════
BOARD_MAIN = "main"
BOARD_GEM = "gem"  # 创业板
BOARD_STAR = "star"  # 科创板
BOARD_BSE = "bse"  # 北交所
BOARD_OTHER = "other"

# 各板块**最新**日内涨跌幅限制（对齐验收：±10% / ±20% / ±30%）。
# 注意：真实规则随时间变化，**必须按日期分段**（见下方 regime 常量与 ``limit_pct_of``）；
# 本字典仅作「无日期上下文」时的兜底，不代表历史任意一天。
BOARD_LIMIT_PCT: dict[str, float] = {
    BOARD_MAIN: 0.10,
    BOARD_GEM: 0.20,
    BOARD_STAR: 0.20,
    BOARD_BSE: 0.30,
    BOARD_OTHER: 0.10,
}

# ── 涨跌幅制度分段（历史正确性，Q4 修正）──
# 主板 ±10%：自 1996-12-16「A 股统一涨跌停板制度」生效起；此前无宽幅一致涨跌停。
MARKET_LIMIT_REGIME_START: date = date(1996, 12, 16)
# 创业板（sz.30x）：2020-08-24 注册制改革，涨跌幅由 ±10% 放宽至 ±20%；此前为 ±10%。
GEM_LIMIT_20PCT_START: date = date(2020, 8, 24)
# 科创板（sh.688/689）：2019-07-22 开板即 ±20%。
STAR_LIMIT_20PCT_START: date = date(2019, 7, 22)
# 北交所（bj.）：2021-11-15 开市即 ±30%（前身新三板精选层亦为 ±30%）。
BSE_LIMIT_30PCT_START: date = date(2021, 11, 15)


def _coerce_day(day: date | datetime | str | None) -> date | None:
    """把 ``date`` / ``datetime`` / ISO 字符串归一化为 ``date``；``None`` 原样返回。"""
    if day is None:
        return None
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    text = str(day).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def board_of(code: str) -> str:
    """根据证券代码判断所属板块。

    支持 Baostock 风格代码（``sh.600000`` / ``sz.300750`` / ``bj.830799``）。
    """
    c = code.lower()
    if c.startswith("sh.688") or c.startswith("sh.689"):
        return BOARD_STAR
    if c.startswith("sh.60") or c.startswith("sh.900"):
        return BOARD_MAIN
    if c.startswith("sz.300") or c.startswith("sz.301"):
        return BOARD_GEM
    if c.startswith("sz.00") or c.startswith("sz.20"):
        return BOARD_MAIN
    if c.startswith("bj."):
        return BOARD_BSE
    return BOARD_OTHER


def limit_pct_of(code: str, day: date | datetime | str | None = None) -> float | None:
    """返回该证券在 ``day`` 当日的涨跌幅限制比例（**按制度分段**，Q4 修正）。

    分段规则（历史正确性）：
    - **主板**：``day >= 1996-12-16`` 为 ±10%；此前**无**统一涨跌停 → 返回 ``None``。
    - **创业板** ``sz.30x``：``day >= 2020-08-24`` 为 ±20%，此前为 ±10%。
    - **科创板** ``sh.688/689``：``day >= 2019-07-22`` 起为 ±20%（开板即 20%）。
    - **北交所** ``bj.``：±30%。

    Args:
        code: 证券代码。
        day: 目标日期；``None`` 表示「无日期上下文」，按**最新**规则返回（不做分段）。

    Returns:
        涨跌幅比例（如 ``0.10``）；``day`` 明确早于制度生效日时返回 ``None``
        （表示当日无该档涨跌停，调用方应视作「无涨跌停限制」）。
    """
    d = _coerce_day(day)
    if d is not None and d < MARKET_LIMIT_REGIME_START:
        return None
    board = board_of(code)
    if board == BOARD_BSE:
        return 0.30
    if board == BOARD_STAR:
        return 0.20
    if board == BOARD_GEM:
        if d is not None and d < GEM_LIMIT_20PCT_START:
            return 0.10
        return 0.20
    return 0.10


def index_board_of(index_code: str) -> str:
    """指数统一归为 other（不参与涨跌停预计算）。"""
    return BOARD_OTHER


# ══════════════════ 价格精度 ══════════════════
def round_to_cent(value: float | Decimal | None) -> float | None:
    """四舍五入到分（ROUND_HALF_UP，与真实交易所规则一致）。

    Args:
        value: 待取整的金额/价格；``None`` 原样返回。

    Returns:
        保留两位小数的浮点数；``None`` 输入返回 ``None``。
    """
    if value is None:
        return None
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ══════════════════ 基础信息 ══════════════════
@dataclass(slots=True)
class Stock:
    """证券基础信息（时点标的池的前提，红线⑥）。

    已知局限（Q1，M0/M1 不改逻辑）：
        ``is_st`` 由**证券名称匹配**（含 ``ST`` / ``*ST``）得到，**不可靠**——
        历史上 ST 前缀会随时间变化（摘帽/戴帽），而 ``dim_stock`` 只存**最新快照**，
        无法还原历史某日的 ST 状态。故日内 ±5%（ST 股）规则在 M0/M1 **不实现**，
        并且已通过「标的池排除 ST + 排除上市不足 60 个交易日」规避多数相关场景。
        **M2 建时点标的池时应改用 ``query_all_stock(day=...)`` 的当日名称快照重做。**
    """

    code: str
    name: str = ""
    list_date: date | None = None
    delist_date: date | None = None
    board: str = BOARD_OTHER
    industry: str = ""
    is_st: bool = False  # 名称匹配得到，历史不可靠；见类 docstring（M2 改用当日名称快照）

    @property
    def listed(self) -> bool:
        """是否有上市日期。"""
        return self.list_date is not None

    @property
    def delisted(self) -> bool:
        """是否已退市。"""
        return self.delist_date is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ══════════════════ 行情 Bar ══════════════════
@dataclass(slots=True)
class DailyBar:
    """日线 Bar（**不复权**口径；复权由 PriceAdjuster 现算）。"""

    code: str
    date: date
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    amount: float = 0.0
    adj_factor: float = 1.0
    is_trade: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MinuteBar:
    """分钟线 Bar。"""

    code: str
    dt: datetime
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    amount: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ══════════════════ 派生信息 ══════════════════
@dataclass(slots=True)
class Dividend:
    """分红送配（除权除息日）。"""

    code: str
    ex_date: date
    cash_per_share: float = 0.0
    share_ratio: float = 0.0  # 每股送股比例
    rights_ratio: float = 0.0  # 每股配股比例

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LimitPrice:
    """某证券某日的涨跌停价（四舍五入到分）。"""

    code: str
    date: date
    limit_up: float
    limit_down: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ══════════════════ 业务对象（M1 及以后使用，M0 先定义）══════════════════
@dataclass(slots=True)
class InitialPosition:
    """持仓型题目的初始持仓。"""

    shares: int = 0
    avg_cost: float = 0.0
    cost_date: date | None = None

    def to_json(self) -> str:
        d: dict[str, Any] = {"shares": self.shares, "avg_cost": self.avg_cost}
        d["cost_date"] = self.cost_date.isoformat() if self.cost_date else None
        return json.dumps(d, ensure_ascii=False)


@dataclass(slots=True)
class Question:
    """题目。"""

    question_id: str
    code: str
    start_date: date
    end_date: date
    pattern_tag: str = ""
    position_type: str = "empty"  # empty / holding
    position_state_tier: str | None = None
    initial_position: InitialPosition | None = None
    initial_cash: float = 100000.0
    settle_params: dict[str, Any] = field(default_factory=dict)
    visible_until: date | None = None
    question_mode: str = "normal"  # normal / judge


@dataclass(slots=True)
class Order:
    """下单记录。"""

    order_id: str
    question_id: str
    session_id: str | None
    dt: datetime
    side: str  # buy / sell
    shares: int
    price: float = 0.0
    params_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Settlement:
    """结算结果（账户视角；**不判对错**）。

    说明（M1）：``account_return`` 等指标均为**账户视角**，无 success/fail 语义；
    额外字段（``entry_price`` 起）用于前端展示账户结果，None/默认不改变既有契约。
    """

    order_id: str
    account_return: float = 0.0
    stock_return: float = 0.0
    benchmark_return: float = 0.0
    alpha: float = 0.0
    opp_cost: float = 0.0
    max_dd: float = 0.0
    hold_all_return: float = 0.0
    # ── M1 扩展（展示用；不参与"对错"判定）──
    entry_price: float = 0.0
    exit_price: float = 0.0
    exit_day: date | None = None
    hold_days: int = 0
    delisted: bool = False  # 是否因退市在最后可交易日平仓
    fee_version: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["exit_day"] = self.exit_day.isoformat() if self.exit_day else None
        return d


@dataclass(slots=True)
class ParamsSnapshot:
    """结算快照冻结（红线④）。"""

    fee_version: str
    adj_factor: float
    fill_price: float
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "fee_version": self.fee_version,
            "adj_factor": self.adj_factor,
            "fill_price": self.fill_price,
            **self.extra,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


# ══════════════════ 运维对象 ══════════════════
@dataclass(slots=True)
class HealthItem:
    """单项健康检查结果。"""

    name: str
    passed: bool
    detail: str = ""
    metric: Any = None


@dataclass(slots=True)
class HealthReport:
    """健康检查报告。"""

    checked_at: datetime
    items: list[HealthItem] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(i.passed for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "passed": self.passed,
            "items": [
                {"name": i.name, "passed": i.passed, "detail": i.detail, "metric": i.metric} for i in self.items
            ],
        }


@dataclass(slots=True)
class SyncResult:
    """一次同步任务的结果（失败必须可见）。"""

    task: str
    started_at: datetime
    finished_at: datetime | None = None
    ok: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    message: str = ""

    @property
    def success(self) -> bool:
        return len(self.failed) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "ok": self.ok,
            "failed_count": len(self.failed),
            "failed_sample": self.failed[:20],
            "message": self.message,
        }


__all__ = [
    "AdjustMode",
    "BOARD_MAIN",
    "BOARD_GEM",
    "BOARD_STAR",
    "BOARD_BSE",
    "BOARD_OTHER",
    "BOARD_LIMIT_PCT",
    "MARKET_LIMIT_REGIME_START",
    "GEM_LIMIT_20PCT_START",
    "STAR_LIMIT_20PCT_START",
    "BSE_LIMIT_30PCT_START",
    "board_of",
    "limit_pct_of",
    "round_to_cent",
    "Stock",
    "DailyBar",
    "MinuteBar",
    "Dividend",
    "LimitPrice",
    "InitialPosition",
    "Question",
    "Order",
    "Settlement",
    "ParamsSnapshot",
    "HealthItem",
    "HealthReport",
    "SyncResult",
]
