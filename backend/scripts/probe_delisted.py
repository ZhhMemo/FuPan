"""NA3 探测：退市股 / 退市整理期数据可得性（Baostock 实测）。

一次性探测脚本（**手动运行**，不进入定时任务）。对给定退市股：
1. 查询基础信息（上市/退市日期），确认退市状态；
2. 拉取全历史日线（不复权），检查是否覆盖到退市日；
3. 打印最后若干交易日（含最后成交价），判断**退市整理期**是否有日线。

用法：
    cd backend && ../.venv/bin/python -m scripts.probe_delisted
    cd backend && ../.venv/bin/python -m scripts.probe_delisted --codes sz.300104 sh.600518
"""

from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.data.sync.baostock_client import ADJUST_NONE, BaostockClient

log = get_logger("scripts.probe_delisted")

DEFAULT_CODES = ["sz.300104", "sh.600518", "sz.300431", "sh.600001", "sz.000033", "sh.600401"]


def probe_code(client: BaostockClient, code: str, tail: int = 8) -> dict:
    """探测单只退市股的数据可得性。"""
    info: dict = {"code": code}
    # ① 基础信息
    basic = client.stock_basic(code)
    if basic is not None and not basic.empty:
        row = basic.iloc[0].to_dict()
        info["basic"] = {k: str(v) for k, v in row.items()}
    else:
        info["basic"] = None

    # ② 全历史日线（不复权）
    daily = client.history_daily(code, settings.full_history_start, "2030-12-31", adjustflag=ADJUST_NONE)
    if daily is None or daily.empty:
        info["daily_rows"] = 0
        info["available"] = False
        info["note"] = "Baostock 未返回任何日线"
        return info

    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"])
    valid = daily[daily["close"].astype(str).str.strip() != ""]
    info["daily_rows"] = int(len(daily))
    info["valid_price_rows"] = int(len(valid))
    info["first_date"] = str(daily["date"].min().date())
    info["last_date"] = str(daily["date"].max().date())
    if not valid.empty:
        last = valid.sort_values("date").iloc[-1]
        info["last_close"] = float(last["close"])
        info["last_valid_date"] = str(pd.Timestamp(last["date"]).date())
    # ③ 最后 N 个交易日（含退市整理期）
    tail_df = daily.sort_values("date").tail(tail)
    info["tail"] = [
        {
            "date": str(pd.Timestamp(r["date"]).date()),
            "close": r.get("close", ""),
            "tradestatus": r.get("tradestatus", ""),
            "volume": r.get("volume", ""),
        }
        for _, r in tail_df.iterrows()
    ]
    info["available"] = len(valid) > 0
    return info


def main(argv: list[str] | None = None) -> int:
    """入口。"""
    p = argparse.ArgumentParser(description="NA3 退市股数据可得性探测")
    p.add_argument("--codes", nargs="*", default=None, help="证券代码列表（默认若干已知退市股）")
    p.add_argument("--tail", type=int, default=8, help="每只股打印最后 N 个交易日")
    p.add_argument("--out", default=None, help="原始结果 JSON 输出路径")
    args = p.parse_args(argv)

    configure_logging(settings.log_level)
    codes = args.codes or DEFAULT_CODES

    results = []
    with BaostockClient(settings.baostock_reconnect_retries) as client:
        for code in codes:
            try:
                res = probe_code(client, code, tail=args.tail)
            except Exception as exc:  # noqa: BLE001
                res = {"code": code, "available": False, "error": str(exc)}
            results.append(res)
            log.info(
                "probe_done",
                code=code,
                available=res.get("available"),
                last_date=res.get("last_date"),
            )

    payload = {"generated_at": pd.Timestamp.now().isoformat(), "results": results}
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
