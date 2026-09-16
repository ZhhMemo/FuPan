"""DuckDB 连接管理（双库 + filelock 写锁）。

设计要点（§5 / §21）：
- **双库拆分**：``market.duckdb``（可重下，P2）与 ``app.duckdb``（最珍贵，P0）。
- **唯一入口**：所有 DuckDB 访问经本模块；写操作必须 ``acquire_write()`` 拿 ``filelock``。
- **拿不到锁明确报错**（``LockUnavailable``），不留"以为成功了"的歧义。
- 同进程内 DuckDB 允许「1 个读写连接 + 多个只读连接」；读/写均以 ``cursor()``
  派生独立连接，规避 DuckDB 连接非线程安全的问题。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import duckdb
from filelock import FileLock, Timeout

from app.config import Settings
from app.config import settings as default_settings
from app.core.errors import DataUnavailable, LockUnavailable, ValidationError

Layer = Literal["market", "app"]

MARKET: Layer = "market"
APP: Layer = "app"


class DuckDBManager:
    """双库连接管理器。

    Attributes:
        market_conn: ``market.duckdb`` 的基础连接（读写，或只读降级）。
        app_conn: ``app.duckdb`` 的基础连接（读写）。
    """

    def __init__(self, config: Settings | None = None) -> None:
        self._config: Settings = config or default_settings
        self._config.ensure_dirs()
        # 每层一个基础连接（进程内复用），实际使用经 cursor() 派生
        self._base: dict[str, duckdb.DuckDBPyConnection] = {}
        # 每层一个 filelock（同一实例可重入）
        self._locks: dict[str, FileLock] = {
            MARKET: FileLock(str(self._config.market_lock)),
            APP: FileLock(str(self._config.app_lock)),
        }
        self._guard = threading.RLock()

    # ─────────────── 基础连接 ───────────────

    def _path_for(self, layer: Layer) -> str:
        if layer == MARKET:
            return str(self._config.market_db)
        if layer == APP:
            return str(self._config.app_db)
        raise ValidationError(f"未知数据库层：{layer}")

    def _base_conn(self, layer: Layer) -> duckdb.DuckDBPyConnection:
        with self._guard:
            if layer not in self._base:
                path = self._path_for(layer)
                read_only = layer == MARKET and self._config.market_readonly
                try:
                    self._base[layer] = duckdb.connect(path, read_only=read_only)
                except Exception as exc:  # noqa: BLE001 - 转成统一错误
                    raise DataUnavailable(
                        f"无法打开 {layer} 数据库：{path}（{exc}）"
                    ) from exc
            return self._base[layer]

    @property
    def market_conn(self) -> duckdb.DuckDBPyConnection:
        """market 库基础连接。"""
        return self._base_conn(MARKET)

    @property
    def app_conn(self) -> duckdb.DuckDBPyConnection:
        """app 库基础连接。"""
        return self._base_conn(APP)

    # ─────────────── 读 / 写 连接 ───────────────

    def get_read(self, layer: Layer = MARKET) -> duckdb.DuckDBPyConnection:
        """派生一个只读用途的连接（cursor）。"""
        return self._base_conn(layer).cursor()

    def get_write(self, layer: Layer = MARKET) -> duckdb.DuckDBPyConnection:
        """派生一个写入用途的连接（cursor）。

        注意：本方法**不**加锁；调用方必须自行通过 ``acquire_write()`` 持锁，
        以保证「写者互斥」。
        """
        return self._base_conn(layer).cursor()

    @contextmanager
    def acquire_write(self, layer: Layer = MARKET, timeout: float | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
        """获取某层的建议性写锁，并 yield 一个可写连接。

        拿不到锁时抛出 ``LockUnavailable``（明确失败）。
        """
        lock = self._locks[layer]
        effective_timeout = self._config.lock_timeout if timeout is None else timeout
        acquired = False
        try:
            acquired = lock.acquire(timeout=effective_timeout)
        except Timeout:
            acquired = False
        if not acquired:
            raise LockUnavailable(
                f"无法获取 {layer} 库写锁（已锁定）：{lock.lock_file}",
                detail={"lock_file": str(lock.lock_file), "layer": layer},
            )
        try:
            yield self.get_write(layer)
        finally:
            lock.release()

    # ─────────────── 建表 ───────────────

    def init_market_schema(self) -> list[str]:
        """在 market 库执行全部 DDL，返回执行的语句数。"""
        from app.data import ddl  # 局部导入，避免 core -> data 的循环依赖

        with self.acquire_write(MARKET) as con:
            ddl.execute_script(con, ddl.MARKET_DDL)
        return list(ddl.MARKET_DDL)

    def init_app_schema(self) -> list[str]:
        """在 app 库执行全部 DDL，返回执行的语句数。"""
        from app.data import ddl  # 局部导入

        with self.acquire_write(APP) as con:
            ddl.execute_script(con, ddl.APP_DDL)
        return list(ddl.APP_DDL)

    def init_schemas(self) -> None:
        """建出两库全部表（幂等）。"""
        self.init_market_schema()
        self.init_app_schema()

    # ─────────────── 生命周期 ───────────────

    def close(self) -> None:
        """关闭基础连接。"""
        with self._guard:
            for layer, conn in list(self._base.items()):
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 - 关闭失败不应阻断
                    pass
                self._base.pop(layer, None)


# ─────────────── 进程级单例 ───────────────

_MANAGER: DuckDBManager | None = None
_MANAGER_GUARD = threading.Lock()


def get_manager(config: Settings | None = None) -> DuckDBManager:
    """返回进程级 DuckDBManager 单例。"""
    global _MANAGER
    with _MANAGER_GUARD:
        if _MANAGER is None:
            _MANAGER = DuckDBManager(config)
        return _MANAGER


def reset_manager() -> None:
    """重置单例（测试用）。"""
    global _MANAGER
    with _MANAGER_GUARD:
        if _MANAGER is not None:
            _MANAGER.close()
        _MANAGER = None


__all__ = ["DuckDBManager", "Layer", "MARKET", "APP", "get_manager", "reset_manager"]
