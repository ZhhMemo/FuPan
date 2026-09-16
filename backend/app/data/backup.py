"""训练数据备份与恢复验证（FR-8.7，P0）。

背景（`05` §安全 / R9）：
行情数据丢了可以重下（P2），但**下单记录 / 训练会话 / 交易流水丢了就永久没了**（P0）。
因此本模块只针对 **``app.duckdb``（训练数据）** 做备份，并且**必须验证过能恢复**
（不是「文件存在」即算数）。

实现方式（DuckDB 原生、一致性安全）：
- 备份：``EXPORT DATABASE '<dir>' (FORMAT PARQUET)`` —— 由 DuckDB 在**一致快照**下导出
  ``schema.sql`` + ``load.sql`` + 各表 parquet，避免"边写边拷"的文件级不一致；
- 恢复验证：新建**临时 DuckDB 文件**，``IMPORT DATABASE '<dir>'`` 真正导入，
  再**逐表读数**并与源库逐表比对行数——读得到、对得上，才算备份可用。

产物落 ``<data_root>/backups/``（**禁止入库**，见 ``.gitignore``）。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from app.config import Settings
from app.config import settings as default_settings
from app.core.db import APP
from app.core.logging import get_logger
from app.core.timeutil import now_bj
from app.data.repository import Repository

log = get_logger(__name__)

# 训练数据核心表（备份/恢复验证的比对范围）
APP_TABLES: tuple[str, ...] = (
    "dim_question",
    "fact_order",
    "fact_settlement",
    "app_user",
    "app_settings",
    "sync_failure",
    "sim_session",
    "sim_position",
    "sim_trade",
    "sim_nav",
    "sim_session_log",
)


@dataclass(slots=True)
class RestoreReport:
    """恢复验证报告。"""

    backup_dir: str
    restored_dir: str
    ok: bool = False
    source_counts: dict[str, int] = field(default_factory=dict)
    restored_counts: dict[str, int] = field(default_factory=dict)
    mismatches: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_dir": self.backup_dir,
            "restored_dir": self.restored_dir,
            "ok": self.ok,
            "source_counts": self.source_counts,
            "restored_counts": self.restored_counts,
            "mismatches": self.mismatches,
            "message": self.message,
        }


@dataclass(slots=True)
class BackupReport:
    """一次备份 + 恢复验证的完整报告。"""

    created_at: str
    backup_dir: str
    size_bytes: int = 0
    restored: RestoreReport | None = None

    @property
    def ok(self) -> bool:
        return self.restored is not None and self.restored.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "backup_dir": self.backup_dir,
            "size_bytes": self.size_bytes,
            "ok": self.ok,
            "restored": self.restored.to_dict() if self.restored else None,
        }


class TrainingDataBackup:
    """训练数据（``app.duckdb``）备份与恢复验证。

    Args:
        repository: 仓储（提供 app 库只读连接 + 行数统计）。
        config: 配置（备份目录 / 保留份数）。
    """

    def __init__(self, repository: Repository | None = None, config: Settings | None = None) -> None:
        self._cfg = config or default_settings
        self._repo: Repository = repository or Repository()

    # ─────────────── 备份 ───────────────
    def backup(self, label: str | None = None, *, dest: Path | None = None) -> str:
        """导出 ``app.duckdb`` 到备份目录，返回备份目录路径。

        Args:
            label: 可读标签（拼到目录名，仅用于区分）。
            dest: 覆盖备份目录；None 用 ``<data_root>/backups/app_<ts>[_label]``。

        Returns:
            备份目录（含 ``schema.sql`` / ``load.sql`` / 各表 parquet）。
        """
        self._cfg.ensure_dirs()
        ts = now_bj().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{label}" if label else ""
        target = dest or (self._cfg.backup_path / f"app_{ts}{suffix}")
        if target.exists():
            # 同一秒重复备份：追加纳秒，避免覆盖
            target = target.with_name(f"{target.name}_{now_bj().strftime('%f')}")
        target.mkdir(parents=True, exist_ok=False)

        con = self._repo.manager.get_read(APP)
        # DuckDB 原生导出：一致快照下写 schema.sql/load.sql + parquet，规避文件级拷贝不一致
        con.execute(f"EXPORT DATABASE '{_sql_path(target)}' (FORMAT PARQUET)")
        log.info("app_backup_done", target=str(target))
        self._prune()
        return str(target)

    # ─────────────── 恢复验证（**真恢复 + 真读数**）───────────────
    def verify_restore(self, backup_dir: str | Path, *, workspace: Path | None = None) -> RestoreReport:
        """把备份**真正导入**一个临时 DuckDB 并逐表比对行数。

        Args:
            backup_dir: ``backup()`` 产物目录。
            workspace: 临时工作目录；None 用系统临时目录。

        Returns:
            ``RestoreReport``（``ok=True`` 表示读得到且与源库一致）。
        """
        src = Path(backup_dir)
        report = RestoreReport(backup_dir=str(src), restored_dir="")
        if not src.exists():
            report.message = f"备份目录不存在：{src}"
            return report

        tmp_root = Path(workspace) if workspace is not None else Path(tempfile.mkdtemp(prefix="fupan_restore_"))
        tmp_root.mkdir(parents=True, exist_ok=True)
        restore_db = tmp_root / "restore.duckdb"
        restored_dir = str(tmp_root)
        report.restored_dir = restored_dir

        con = duckdb.connect(str(restore_db))
        try:
            con.execute(f"IMPORT DATABASE '{_sql_path(src)}'")
            source_counts = self._table_counts(self._repo)
            restored_counts: dict[str, int] = {}
            mismatches: list[str] = []
            for table in APP_TABLES:
                n = _safe_count(con, table)
                if n is None:
                    continue  # 源库也可能没有该表（未建/未用）
                restored_counts[table] = n
                src_n = source_counts.get(table, 0)
                if n != src_n:
                    mismatches.append(f"{table}: 源={src_n} 恢复={n}")
            report.source_counts = source_counts
            report.restored_counts = restored_counts
            report.mismatches = mismatches
            # 真读一行（证明数据可用，而非只有表结构）
            read_probe = _read_probe(con)
            report.ok = not mismatches and read_probe
            report.message = "恢复验证通过" if report.ok else f"恢复验证失败：{mismatches or '读取探针失败'}"
        finally:
            con.close()
        log.info("app_restore_verified", ok=report.ok, mismatches=report.mismatches)
        return report

    def backup_and_verify(self, label: str | None = None) -> BackupReport:
        """备份 + 立即恢复验证，返回完整报告。"""
        created = now_bj().isoformat()
        target = self.backup(label)
        size = _dir_size(Path(target))
        restored = self.verify_restore(target)
        return BackupReport(created_at=created, backup_dir=target, size_bytes=size, restored=restored)

    # ─────────────── 工具 ───────────────
    def _table_counts(self, repo: Repository) -> dict[str, int]:
        """源库逐表行数（表不存在则跳过）。"""
        out: dict[str, int] = {}
        con = repo.manager.get_read(APP)
        for table in APP_TABLES:
            n = _safe_count(con, table)
            if n is not None:
                out[table] = n
        return out

    def _prune(self) -> None:
        """滚动清理：仅保留最近 ``backup_keep`` 份备份。"""
        keep = int(getattr(self._cfg, "backup_keep", 14) or 14)
        if keep <= 0:
            return
        dirs = sorted(
            (d for d in self._cfg.backup_path.glob("app_*") if d.is_dir()),
            key=lambda p: p.name,
        )
        for old in dirs[:-keep]:
            try:
                shutil.rmtree(old)
                log.info("app_backup_pruned", target=str(old))
            except Exception as exc:  # noqa: BLE001 - 清理失败不阻断
                log.warning("app_backup_prune_failed", target=str(old), error=str(exc))


def _sql_path(p: Path) -> str:
    """把路径转成 DuckDB SQL 字符串字面量安全形式（单引号转义）。"""
    return str(p).replace("'", "''")


def _safe_count(con: duckdb.DuckDBPyConnection, table: str) -> int | None:
    """统计表行数；表不存在返回 None。"""
    try:
        row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except Exception:  # noqa: BLE001 - 表不存在
        return None
    return int(row[0]) if row and row[0] is not None else 0


def _read_probe(con: duckdb.DuckDBPyConnection) -> bool:
    """真读一行数据（证明恢复后的库可查询，而非仅有空表）。"""
    for table in ("dim_question", "fact_order", "app_settings", "app_user"):
        try:
            con.execute(f"SELECT * FROM {table} LIMIT 1").fetchall()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _dir_size(p: Path) -> int:
    """目录总字节数。"""
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


__all__ = [
    "APP_TABLES",
    "BackupReport",
    "RestoreReport",
    "TrainingDataBackup",
]
