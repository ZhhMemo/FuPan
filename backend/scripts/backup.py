"""训练数据备份入口（FR-8.7，P0）。

对 ``app.duckdb``（下单记录 / 训练会话 / 交易流水，**不可再生**）执行：
1. ``EXPORT DATABASE`` 一致快照备份；
2. **恢复验证**：真正 ``IMPORT DATABASE`` 到一个临时库并逐表比对行数 + 读探针。

用法：
    cd backend && ../.venv/bin/python -m scripts.backup
    cd backend && ../.venv/bin/python -m scripts.backup --label nightly
    cd backend && ../.venv/bin/python -m scripts.backup --verify-only <备份目录>

退出码：0=备份且恢复验证通过；1=失败（**必须可见**，不静默）。
"""

from __future__ import annotations

import argparse
import json
import sys

from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.data.backup import TrainingDataBackup

log = get_logger("scripts.backup")


def main(argv: list[str] | None = None) -> int:
    """执行备份（+恢复验证）或仅验证既有备份。"""
    p = argparse.ArgumentParser(description="训练数据（app.duckdb）备份 + 恢复验证")
    p.add_argument("--label", default=None, help="备份标签（拼到目录名）")
    p.add_argument("--verify-only", default=None, help="仅恢复验证给定备份目录（不做新备份）")
    args = p.parse_args(argv)

    configure_logging(settings.log_level, settings.log_json)
    settings.ensure_dirs()

    svc = TrainingDataBackup()
    if args.verify_only:
        report = svc.verify_restore(args.verify_only)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
        if not report.ok:
            log.error("backup_verify_failed", mismatches=report.mismatches)
            return 1
        log.info("backup_verify_ok", backup_dir=report.backup_dir)
        return 0

    result = svc.backup_and_verify(args.label)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    if not result.ok:
        log.error("backup_failed", backup_dir=result.backup_dir)
        return 1
    log.info("backup_ok", backup_dir=result.backup_dir, size=result.size_bytes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
