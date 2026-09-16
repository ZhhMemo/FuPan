"""首次全量数据初始化（天级，独立进程）。

用法：
    cd backend && ../.venv/bin/python -m scripts.init_data --sample 3
    cd backend && ../.venv/bin/python -m scripts.init_data            # 全市场（耗时长）

约定：本 CLI 应在 API **未启动**时执行；若必须并发，请以
``FUPAN_MARKET_READONLY=1`` 启动 API（market 库只读）。
"""

from __future__ import annotations

import argparse
import json
import sys

from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.data.sync.service import SyncService

log = get_logger("scripts.init_data")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    p = argparse.ArgumentParser(description="复盘 · 首次全量数据初始化")
    p.add_argument("--sample", type=int, default=None, help="仅初始化前 N 只证券（快速验证）")
    p.add_argument("--codes", nargs="*", default=None, help="仅初始化指定证券代码（如 sh.600000 sz.300104）")
    p.add_argument("--no-index", action="store_true", help="跳过基准指数")
    p.add_argument("--with-dividend", action="store_true", help="拉取分红送配（耗时）")
    p.add_argument("--with-minute", action="store_true", help="拉取分钟线（耗时）")
    p.add_argument("--minute-frequency", default="5", choices=["5", "15", "30", "60"], help="分钟周期")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """入口。"""
    args = parse_args(argv)
    configure_logging(settings.log_level, settings.log_json)
    settings.ensure_dirs()

    service = SyncService()
    service.ensure_schema()
    log.info("schema_ready", market_db=str(settings.market_db), app_db=str(settings.app_db))

    if args.codes:
        # 指定证券的定向初始化（用于快速验证）
        service.ingest_meta(with_index=not args.no_index, with_dividend=args.with_dividend)
        daily = service.ingest_daily(full=True, codes=args.codes)
        limit = service.precompute_limit(codes=args.codes)
        summary = {
            "task": "init_codes",
            "codes": args.codes,
            "daily": daily.to_dict(),
            "limit": limit.to_dict(),
            "ok": len(daily.failed) == 0 and len(limit.failed) == 0,
        }
    else:
        summary = service.init_all(
            full=True,
            sample=args.sample,
            with_dividend=args.with_dividend,
            with_minute=args.with_minute,
            minute_frequency=args.minute_frequency,
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok", False) else 1


if __name__ == "__main__":
    sys.exit(main())
