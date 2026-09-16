"""数据健康检查（P0，FR-1.6 / FR-7.1）。

四项校验（对齐设计 §11 时序图「股票数突变 / 日期跳跃 / 价格非负非零 / 涨跌幅超限」）：

1. ``stock_count``    证券数量是否正常（>0 且无突变）
2. ``daily_coverage`` 日线覆盖与新鲜度（行数、日期范围、距今停滞天数）
3. ``price_validity`` 价格合法性（close ≤ 0 / NULL / high < low）
4. ``pct_change``     涨跌幅是否超过板块限制

**涨跌幅校验口径（重要，避免误报）**：A 股真实数据中，以下情形单日涨跌幅会**合法地**
超过板块限制，若不剔除会产生大量误报：

- **除权除息 / 配股日**：不复权价自然跳空（用后复权价 ``close × adj_factor`` 消除大部分跳空）；
- **停牌复牌**：长期停牌后复牌（以相邻交易日间隔 ``> max_gap_days`` 排除）；
- **1996-12-16 之前**：A 股尚未统一实行 ±10% 涨跌停板制度（以 ``pct_check_start`` 排除）。

因此本校验在「**后复权价 + 非除权日 + 非停牌复牌 + 制度生效后**」的行上计算，
任何剩余超限即为**真实异常**，应告警。有异常则明确输出，不静默。
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from app.config import Settings
from app.config import settings as default_settings
from app.core.db import MARKET, DuckDBManager
from app.core.logging import get_logger
from app.core.timeutil import now_bj, today_bj
from app.data.models import HealthItem, HealthReport
from app.data.repository import Repository

log = get_logger(__name__)

# A 股统一实行 ±10% 涨跌停板制度的起始日
A_SHARE_LIMIT_REGIME_START = "1996-12-16"


def _board_pct_case(alias: str = "") -> str:
    """生成按板块判定涨跌幅限制的 SQL CASE 表达式。"""
    col = f"{alias}.code" if alias else "code"
    return (
        "CASE "
        f"WHEN {col} LIKE 'bj.%' THEN 0.30 "
        f"WHEN {col} LIKE 'sz.30%' THEN 0.20 "
        f"WHEN {col} LIKE 'sh.688%' THEN 0.20 "
        f"WHEN {col} LIKE 'sh.689%' THEN 0.20 "
        "ELSE 0.10 END"
    )


class HealthCheck:
    """数据健康检查器。

    Args:
        repository: 仓储；None 则新建。
        config: 配置。
        max_staleness_days: 日线最新日期距今天数的容忍上限。
        pct_tolerance: 涨跌幅容差（抵消 round-to-cent 误差）。
        stock_change_limit: 证券数量突变阈值（相对变化）。
        max_gap_days: 相邻交易日最大间隔（超过视为停牌复牌，涨跌幅校验豁免）。
        pct_check_start: 涨跌幅校验起始日期（默认 A 股涨跌停制度生效日）。
    """

    def __init__(
        self,
        repository: Repository | None = None,
        config: Settings | None = None,
        max_staleness_days: int = 15,
        pct_tolerance: float = 0.005,
        stock_change_limit: float = 0.05,
        max_gap_days: int = 10,
        pct_check_start: str = A_SHARE_LIMIT_REGIME_START,
    ) -> None:
        self._cfg = config or default_settings
        self._repo = repository or Repository()
        self._mgr: DuckDBManager = self._repo.manager
        self._max_staleness = max_staleness_days
        self._pct_tol = pct_tolerance
        self._stock_change_limit = stock_change_limit
        self._max_gap_days = max_gap_days
        self._pct_start = pct_check_start

    # ─────────────── 工具 ───────────────
    def _scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        con = self._mgr.get_read(MARKET)
        row = con.execute(sql, params or []).fetchone()
        return None if row is None else row[0]

    def _fetch(self, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
        con = self._mgr.get_read(MARKET)
        return con.execute(sql, params or []).fetchdf()

    # ─────────────── ① 证券数量 ───────────────
    def check_stock_count(self) -> HealthItem:
        """证券数量是否正常（>0 且相对上次无突变）。"""
        n = int(self._scalar("SELECT COUNT(*) FROM dim_stock") or 0)
        prev_raw = self._repo.get_setting("health.last_stock_count")
        detail = f"dim_stock={n}"
        passed = n > 0
        if prev_raw:
            try:
                prev = int(prev_raw)
                if prev > 0:
                    change = abs(n - prev) / prev
                    detail += f"，较上次 {prev} 变化 {change:.2%}"
                    if change > self._stock_change_limit:
                        passed = False
                        detail += "（超过突变阈值）"
            except ValueError:
                detail += "（上次计数不可解析，已忽略）"
        if n > 0:
            self._repo.set_setting("health.last_stock_count", str(n))
        return HealthItem(name="stock_count", passed=passed, detail=detail, metric={"stock_count": n})

    # ─────────────── ② 日期覆盖 ───────────────
    def check_daily_coverage(self) -> HealthItem:
        """日线覆盖与新鲜度。"""
        rows = int(self._scalar("SELECT COUNT(*) FROM fact_daily") or 0)
        maxd = self._scalar("SELECT MAX(date) FROM fact_daily")
        mind = self._scalar("SELECT MIN(date) FROM fact_daily")
        cal_rows = int(self._scalar("SELECT COUNT(*) FROM dim_calendar") or 0)
        metric = {
            "rows": rows,
            "min_date": str(mind) if mind else None,
            "max_date": str(maxd) if maxd else None,
            "calendar_rows": cal_rows,
        }
        if rows == 0 or maxd is None:
            return HealthItem(name="daily_coverage", passed=False, detail="fact_daily 无数据", metric=metric)
        staleness = (today_bj() - pd.Timestamp(maxd).date()).days
        metric["staleness_days"] = staleness
        passed = staleness <= self._max_staleness and cal_rows > 0
        detail = f"rows={rows}，范围 {mind}~{maxd}，距今 {staleness} 天；日历 {cal_rows} 行"
        return HealthItem(name="daily_coverage", passed=passed, detail=detail, metric=metric)

    # ─────────────── ③ 价格合法性 ───────────────
    def check_price_validity(self) -> HealthItem:
        """价格合法性：close ≤ 0 / NULL / high < low。"""
        bad_close = int(self._scalar("SELECT COUNT(*) FROM fact_daily WHERE close IS NULL OR close <= 0") or 0)
        bad_hl = int(
            self._scalar("SELECT COUNT(*) FROM fact_daily WHERE high IS NOT NULL AND low IS NOT NULL AND high < low")
            or 0
        )
        total = int(self._scalar("SELECT COUNT(*) FROM fact_daily") or 0)
        passed = total > 0 and bad_close == 0 and bad_hl == 0
        detail = f"总行 {total}；非法收盘 {bad_close} 行；high<low {bad_hl} 行"
        return HealthItem(
            name="price_validity",
            passed=passed,
            detail=detail,
            metric={"rows": total, "bad_close": bad_close, "bad_high_low": bad_hl},
        )

    # ─────────────── ④ 涨跌幅超限 ───────────────
    def _pct_violation_query(self, select: str) -> str:
        """生成涨跌幅超限校验 SQL（``select`` 为 SELECT 子句）。"""
        return f"""
        WITH o AS (
            SELECT code, date, close * adj_factor AS ac, adj_factor,
                   LAG(close * adj_factor) OVER (PARTITION BY code ORDER BY date) AS prev_ac,
                   LAG(adj_factor) OVER (PARTITION BY code ORDER BY date) AS prev_af,
                   LAG(date) OVER (PARTITION BY code ORDER BY date) AS prev_date
            FROM fact_daily
        )
        SELECT {select} FROM o
        WHERE prev_ac IS NOT NULL AND prev_ac > 0 AND ac IS NOT NULL
          AND date >= CAST(? AS DATE)
          AND (date - prev_date) <= ?
          AND prev_af = adj_factor
          AND ABS(ac / prev_ac - 1.0) > ({_board_pct_case("o")} + ?)
        """

    def check_pct_change(self) -> HealthItem:
        """涨跌幅超限检查（后复权 + 剔除除权日/停牌复牌/制度生效前）。"""
        params = [self._pct_start, self._max_gap_days, self._pct_tol]
        violations = int(self._scalar(self._pct_violation_query("COUNT(*)"), params) or 0)
        considered = int(
            self._scalar(
                """
                WITH o AS (
                    SELECT code, date, adj_factor,
                           LAG(adj_factor) OVER (PARTITION BY code ORDER BY date) AS prev_af,
                           LAG(date) OVER (PARTITION BY code ORDER BY date) AS prev_date
                    FROM fact_daily
                )
                SELECT COUNT(*) FROM o
                WHERE prev_af IS NOT NULL AND prev_af = adj_factor
                  AND (date - prev_date) <= ? AND date >= CAST(? AS DATE)
                """,
                [self._max_gap_days, self._pct_start],
            )
            or 0
        )
        samples: list[dict[str, Any]] = []
        if violations > 0:
            samples = self._fetch(
                self._pct_violation_query(
                    "code, date, round(ac / prev_ac - 1.0, 4) AS pct, (date - prev_date) AS gap_days"
                )
                + " ORDER BY code, date LIMIT 20",
                params,
            ).to_dict(orient="records")
        passed = considered > 0 and violations == 0
        detail = (
            f"超限 {violations} 行（校验 {considered} 行；口径=后复权、非除权日、非停牌复牌、"
            f"制度生效后 {self._pct_start}；容差 {self._pct_tol:.1%}）"
        )
        return HealthItem(
            name="pct_change",
            passed=passed,
            detail=detail,
            metric={
                "violations": violations,
                "considered_rows": considered,
                "samples": samples,
                "criterion": {
                    "adjusted": "hfq",
                    "exclude_ex_dividend": True,
                    "max_gap_days": self._max_gap_days,
                    "start": self._pct_start,
                    "tolerance": self._pct_tol,
                },
            },
        )

    # ─────────────── 汇总 ───────────────
    def run(self) -> HealthReport:
        """执行全部校验并返回报告。"""
        items = [
            self.check_stock_count(),
            self.check_daily_coverage(),
            self.check_price_validity(),
            self.check_pct_change(),
        ]
        report = HealthReport(checked_at=now_bj(), items=items)
        if report.passed:
            log.info("health_check_passed", items=len(items))
        else:
            failed = [i.name for i in items if not i.passed]
            log.warning("health_check_failed", failed=failed)
        return report


def run_health_check_cli() -> None:
    """CLI：执行健康检查并打印 JSON 报告（供 Makefile 使用）。"""
    report = HealthCheck().run()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
    if not report.passed:
        raise SystemExit(1)


__all__ = ["HealthCheck", "run_health_check_cli", "A_SHARE_LIMIT_REGIME_START"]
