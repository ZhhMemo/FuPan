"""结算接口（FR-4.7，M1-T3）。

- ``POST /api/settle/{qid}``  **独立延后**接口：仅在下单后可调（防抓包提前结算）；
  复用 ``SnapshotFreezer`` 冻结快照 + ``SettlementEngine`` 账户视角结算（红线④⑤）。

结算**不判对错**：返回账户收益率 / 标的 / 基准 / 超额 / 机会成本 / 最大回撤 / 全程持有对照。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.schemas import SettleRequest, envelope
from app.api.trade import build_account_after_orders, load_question_or_404
from app.core.errors import Conflict
from app.core.logging import get_logger
from app.core.timeutil import now_bj_naive
from app.data.repository import Repository
from app.engine.settle import SettlementEngine
from app.engine.snapshot import SnapshotFreezer
from app.engine.trade_engine import TradeEngine

log = get_logger(__name__)

router = APIRouter(prefix="/api/settle", tags=["settle"])


@router.post("/{qid}")
def settle(
    qid: str,
    payload: SettleRequest | None = None,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """结算某题目（仅下单后可调）。"""
    repo = Repository()
    engine = TradeEngine(repo)
    rec = load_question_or_404(repo, qid)

    orders = repo.list_orders(qid)
    if not orders:
        raise Conflict("尚未下单，无法结算（结算接口仅下单后可调）")

    order = orders[-1]  # 最近一笔订单
    snap = SnapshotFreezer(repo).load(order)
    account = build_account_after_orders(repo, engine, rec)

    settlement_engine = SettlementEngine(repo, trade_engine=engine)
    # 只用快照结算（红线④）：传入 orders 仅作「快照未冻结账户」时的兜底（按冻结费率回放）
    result = settlement_engine.settle(
        order,
        rec,
        snapshot=snap,
        account=account,
        window=(payload.window if payload else None),
        orders=orders,
    )

    # 持久化（冻结）
    fee_detail = snap.to_json() if snap is not None else None
    repo.upsert_settlement(
        {
            "order_id": result.order_id,
            "account_return": result.account_return,
            "stock_return": result.stock_return,
            "benchmark_return": result.benchmark_return,
            "alpha": result.alpha,
            "opp_cost": result.opp_cost,
            "max_dd": result.max_dd,
            "hold_all_return": result.hold_all_return,
            "fee_detail": fee_detail,
            "created_at": now_bj_naive(),
        }
    )
    log.info(
        "settlement_done",
        qid=qid,
        order_id=result.order_id,
        account_return=result.account_return,
        delisted=result.delisted,
        by=username,
    )
    return envelope(result.to_dict(), "结算完成（账户视角，不判对错）")


__all__ = ["router"]
