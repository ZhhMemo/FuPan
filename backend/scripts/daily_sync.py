"""每日增量同步入口（21:00 定时任务；也可被 APScheduler / 手动调用）。

用法：
    cd backend && ../.venv/bin/python -m scripts.daily_sync
"""

from __future__ import annotations

import json
import sys

from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.data.sync.service import run_daily_sync

log = get_logger("scripts.daily_sync")


def main() -> int:
    """执行一次增量同步并打印结构化摘要。

    Returns:
        进程退出码：0=成功；1=存在失败（失败必须可见，不静默）。
    """
    configure_logging(settings.log_level, settings.log_json)
    settings.ensure_dirs()

    result = run_daily_sync()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if not result.get("ok", False):
        log.error("daily_sync_failed", failures=len(result.get("failures", [])))
        return 1
    log.info("daily_sync_success", ok=result.get("daily", {}).get("ok"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
