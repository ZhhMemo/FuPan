"""下单与账户面板接口（FR-4，M1-T2）。

- ``POST /api/trade/order``        下单（落 ``fact_order`` + ``params_snapshot``，红线④）
- ``GET  /api/trade/account/{qid}`` 持仓与资金面板（FR-3.3）

红线⑤：**全市场唯一成交实现**为 ``engine.trade_engine.TradeEngine``；本接口只做编排。
判卷模式：下单可带 ``judge_verdict``（agree/disagree），并冻结 ``judge_advice``，
仅供 FR-6.5 模式分账，**不判对错**。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.schemas import OrderRequest, envelope
from app.config import settings
from app.core.errors import Conflict, NotFound
from app.core.logging import get_logger
from app.core.timeutil import now_bj_naive
from app.data.repository import Repository
from app.engine.advice import AdviceEngine
from app.engine.position import Account
from app.engine.settle import SettlementEngine
from app.engine.snapshot import SnapshotFreezer
from app.engine.trade_engine import TradeEngine, replay_orders

log = get_logger(__name__)

router = APIRouter(prefix="/api/trade", tags=["trade"])


# ─────────────── 共享编排辅助（settle 路由复用）───────────────
def load_question_or_404(repo: Repository, qid: str) -> dict[str, Any]:
    """读取题目记录；不存在抛 404。"""
    rec = repo.get_question(qid)
    if rec is None:
        raise NotFound(f"题目不存在：{qid}")
    return rec


def decision_day(rec: dict[str, Any]) -> date:
    """题目决策点（= ``end_date``）。"""
    return pd.Timestamp(rec["end_date"]).date()


def window_of(rec: dict[str, Any]) -> int:
    """题目结算窗口（交易日数）。"""
    raw = rec.get("settle_window")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return settings.settle_window
    return int(raw)


def build_account_after_orders(repo: Repository, engine: TradeEngine, rec: dict[str, Any]) -> Account:
    """由题目初始状态 + 已下单订单回放，得到**下单后**账户。"""
    orders = repo.list_orders(rec["question_id"])
    return replay_orders(engine, rec, orders)


def last_close(repo: Repository, code: str, day: date) -> float:
    """取某证券某日收盘（缺失返回 0）。"""
    df = repo.get_daily(code, start=day, end=day)
    if df is None or df.empty:
        return 0.0
    val = df.iloc[0].get("close")
    return 0.0 if val is None or pd.isna(val) else float(val)


# ─────────────── 下单 ───────────────
@router.post("/order")
def place_order(payload: OrderRequest, username: str = Depends(get_current_user)) -> dict[str, Any]:
    """下单：经 ``TradeEngine`` 成交，冻结快照并落库。"""
    repo = Repository()
    engine = TradeEngine(repo)
    rec = load_question_or_404(repo, payload.question_id)
    code = str(rec["code"])
    day = decision_day(rec)

    account = build_account_after_orders(repo, engine, rec)
    fill = engine.place_order(
        account=account,
        code=code,
        side=payload.side,
        shares=payload.shares,
        day=day,
        decision_point=day,  # N13：M1 单决策点（预留多决策点）
    )
    if not fill.accepted:
        raise Conflict(
            f"下单未成交：{fill.reason}",
            detail={"reason": fill.reason, "deferred_to": fill.deferred_to.isoformat() if fill.deferred_to else None},
        )

    order_id = f"o_{uuid.uuid4().hex[:16]}"
    window = window_of(rec)
    try:
        exit_day, _delisted = SettlementEngine(repo, trade_engine=engine).resolve_exit_day(code, day, window)
    except Exception:  # noqa: BLE001 - 结算窗口解析失败不阻断下单
        exit_day = day

    # 成交日（默认 T+1）；快照冻结覆盖费率族 + 结算窗口 + 成交时点 + 下单后账户（FR-4.6 / 红线④）
    fill_day = fill.fill_day or day
    freezer = SnapshotFreezer(repo)
    snap = freezer.freeze_for_order(
        {"order_id": order_id, "price": fill.price, "dt": fill.dt},
        code=code,
        fill_day=fill_day,
        exit_day=exit_day,
        fee_version=engine.cost_model.fee_version,
        fill_price=fill.price,
        start_day=pd.Timestamp(rec["start_date"]).date(),
        cost_model=engine.cost_model,
        window=window,
        account=account,
        fill_price_source=engine.fill_price_source,
        decision_day=day,
        fill_ts=fill.fill_ts,
    )

    judge_advice: str | None = None
    if str(rec.get("question_mode") or "normal") == "judge":
        try:
            judge_advice = AdviceEngine(repo).advise_for(code, day).to_json()
        except Exception as exc:  # noqa: BLE001 - 建议失败不阻断下单
            log.warning("judge_advice_failed", error=str(exc))

    repo.insert_order(
        {
            "order_id": order_id,
            "question_id": payload.question_id,
            "session_id": None,
            "dt": fill.dt,
            "side": payload.side,
            "shares": payload.shares,
            "price": fill.price,
            "params_snapshot": snap.to_json(),
            "knowledge_mode": None,
            "viewed_knowledge": False,
            "judge_verdict": payload.judge_verdict,
            "judge_advice": judge_advice,
            "created_at": now_bj_naive(),
        }
    )
    log.info(
        "order_placed",
        order_id=order_id,
        qid=payload.question_id,
        side=payload.side,
        shares=payload.shares,
        judge_verdict=payload.judge_verdict,
        by=username,
    )

    est_cost = None
    if fill.fee is not None:
        est_cost = round(fill.fee.turnover + fill.fee.total_fee, 2)
    return envelope(
        {
            "order_id": order_id,
            "est_cost": est_cost,
            "fill": fill.to_dict(),
            "account": account.to_dict(last_close(repo, code, day)),
        },
        "下单成功",
    )


# ─────────────── 账户面板 ───────────────
@router.get("/account/{qid}")
def get_account(qid: str, username: str = Depends(get_current_user)) -> dict[str, Any]:
    """持仓与资金面板（FR-3.3）。"""
    repo = Repository()
    engine = TradeEngine(repo)
    rec = load_question_or_404(repo, qid)
    code = str(rec["code"])
    day = decision_day(rec)
    account = build_account_after_orders(repo, engine, rec)
    price = last_close(repo, code, day)
    return envelope({"question_id": qid, "price": round(price, 4), **account.to_dict(price)})


__all__ = [
    "router",
    "load_question_or_404",
    "decision_day",
    "window_of",
    "build_account_after_orders",
    "last_close",
]
