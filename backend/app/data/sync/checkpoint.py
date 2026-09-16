"""断点续传状态与失败清单。

存储于 ``app.duckdb``：
- ``sync_checkpoint(code PK, last_date, updated_at)``：每只证券已下载到的日期。
- ``sync_failure(batch_date PK, code PK, reason, created_at)``：失败清单（次日重试）。

键约定：分钟线使用 ``"{code}#{frequency}"`` 作为 checkpoint 键，避免多周期相互覆盖。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.core.db import APP, DuckDBManager, get_manager
from app.core.errors import DataUnavailable
from app.core.logging import get_logger
from app.core.timeutil import now_bj_naive, today_bj

log = get_logger(__name__)


class Checkpoint:
    """断点续传状态读写器。"""

    def __init__(self, manager: DuckDBManager | None = None) -> None:
        self._mgr: DuckDBManager = manager or get_manager()

    # ─────────────── 进度 ───────────────
    def last_date_of(self, code: str) -> date | None:
        """返回某证券已下载到的最后日期；无记录返回 None。"""
        con = self._mgr.get_read(APP)
        row = con.execute("SELECT last_date FROM sync_checkpoint WHERE code = ?", [code]).fetchone()
        if row is None or row[0] is None:
            return None
        return pd.Timestamp(row[0]).date()

    def mark(self, code: str, day: date) -> None:
        """记录某证券已下载到 ``day``。"""
        with self._mgr.acquire_write(APP) as con:
            con.execute(
                "INSERT OR REPLACE INTO sync_checkpoint (code, last_date, updated_at) VALUES (?, ?, ?)",
                [code, day.isoformat(), now_bj_naive()],
            )

    def mark_many(self, pairs: list[tuple[str, date]]) -> None:
        """批量记录进度。"""
        if not pairs:
            return
        rows = pd.DataFrame({"code": [p[0] for p in pairs], "last_date": [p[1] for p in pairs]})
        with self._mgr.acquire_write(APP) as con:
            con.register("_ck_tmp", rows)
            try:
                con.execute("INSERT OR REPLACE INTO sync_checkpoint SELECT code, last_date, now() FROM _ck_tmp")
            finally:
                con.unregister("_ck_tmp")

    # ─────────────── 失败清单 ───────────────
    def record_failure(self, batch_date: date, code: str, reason: str) -> None:
        """记录一条失败（同一批次同一证券覆盖）。"""
        with self._mgr.acquire_write(APP) as con:
            con.execute(
                "INSERT OR REPLACE INTO sync_failure (batch_date, code, reason, created_at) VALUES (?, ?, ?, ?)",
                [batch_date.isoformat(), code, reason[:2000], now_bj_naive()],
            )

    def record_failures(self, batch_date: date, items: list[tuple[str, str]]) -> None:
        """批量记录失败。"""
        for code, reason in items:
            self.record_failure(batch_date, code, reason)

    def failed_list(self, batch_date: date | None = None) -> list[tuple[str, str]]:
        """返回失败清单 ``[(code, reason), ...]``。"""
        con = self._mgr.get_read(APP)
        if batch_date is None:
            rows = con.execute("SELECT code, reason FROM sync_failure ORDER BY code").fetchall()
        else:
            rows = con.execute(
                "SELECT code, reason FROM sync_failure WHERE batch_date = ? ORDER BY code",
                [batch_date.isoformat()],
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def count_failures(self, batch_date: date | None = None) -> int:
        """统计失败条数。"""
        con = self._mgr.get_read(APP)
        if batch_date is None:
            row = con.execute("SELECT COUNT(*) FROM sync_failure").fetchone()
        else:
            row = con.execute(
                "SELECT COUNT(*) FROM sync_failure WHERE batch_date = ?", [batch_date.isoformat()]
            ).fetchone()
        return int(row[0]) if row else 0

    def clear_failures(self, batch_date: date) -> None:
        """清空某批次失败清单（重试成功后调用）。"""
        with self._mgr.acquire_write(APP) as con:
            con.execute("DELETE FROM sync_failure WHERE batch_date = ?", [batch_date.isoformat()])

    def today_batch(self) -> date:
        """返回当前批次日期（北京日期）。"""
        return today_bj()


def require_checkpoint_table(checkpoint: Checkpoint) -> None:
    """校验 checkpoint 表存在，否则抛明确错误。"""
    try:
        checkpoint.count_failures()
    except Exception as exc:  # noqa: BLE001
        raise DataUnavailable(f"sync_checkpoint/sync_failure 表不可用：{exc}") from exc


__all__ = ["Checkpoint", "require_checkpoint_table"]
