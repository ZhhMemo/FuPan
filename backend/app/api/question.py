"""出题与观察接口（FR-2 / FR-3，M1-T4）。

- ``POST /api/questions/custom``    手工指定一道题（含空仓型 / 持仓型初始状态）
- ``GET  /api/questions/{qid}``     题目详情
- ``GET  /api/questions/{qid}/kline``  K 线（**严格截断于决策点**，红线①）
- ``GET  /api/questions/{qid}/advice`` 判卷模式：模型仓位建议（N6）
- ``GET  /api/stocks``              时点标的池（红线⑥）

红线①：所有行情接口返回前**必须**过 ``VisibilityGuard``（``cutoff = end_date``），
指标同样严格截断（末点日期 ≤ 决策点）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user
from app.api.schemas import CustomQuestionRequest, envelope
from app.core.errors import NotFound, ValidationError
from app.core.logging import get_logger
from app.core.timeutil import now_bj_naive
from app.data.adjuster import PriceAdjuster
from app.data.models import AdjustMode
from app.data.repository import Repository
from app.data.universe import UniverseProvider
from app.data.visibility import mask_df
from app.engine.advice import AdviceEngine

log = get_logger(__name__)

router = APIRouter(prefix="/api/questions", tags=["question"])
stocks_router = APIRouter(prefix="/api/stocks", tags=["question"])

_QUESTION_COLUMNS = [
    "question_id",
    "code",
    "start_date",
    "end_date",
    "pattern_tag",
    "position_type",
    "position_state_tier",
    "initial_position",
    "initial_cash",
    "source",
    "visible_until",
    "settle_window",
    "settle_params",
    "decision_points",
    "question_mode",
    "title",
    "description",
    "key_points",
    "difficulty_prior",
    "difficulty_post",
    "created_at",
]

MA_WINDOWS = (5, 10, 20, 60)


def _make_question_id(payload: CustomQuestionRequest) -> str:
    """由题目参数生成**可复现**的题目 ID。"""
    ip = payload.initial_position
    key = "|".join(
        [
            payload.code,
            payload.start_date.isoformat(),
            payload.end_date.isoformat(),
            payload.position_type,
            payload.question_mode,
            f"{payload.initial_cash:.2f}",
            f"{ip.shares}:{ip.avg_cost:.4f}" if ip else "-",
        ]
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"q_{digest}"


def _ser_question(rec: dict[str, Any]) -> dict[str, Any]:
    """把 dim_question 行序列化为 JSON 友好结构。"""
    out: dict[str, Any] = {}
    for k, v in rec.items():
        if v is None:
            out[k] = None
        elif isinstance(v, pd.Timestamp):
            out[k] = v.isoformat()
        elif hasattr(v, "isoformat") and not isinstance(v, (list, dict)):
            out[k] = v.isoformat()
        elif isinstance(v, (list, dict)):
            out[k] = v
        elif isinstance(v, float) and pd.isna(v):
            out[k] = None
        else:
            out[k] = v
    # 解析 JSON 字段
    for jf in ("initial_position", "settle_params", "decision_points"):
        raw = out.get(jf)
        if isinstance(raw, str) and raw.strip():
            try:
                out[jf] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return out


def _load_question(repo: Repository, qid: str) -> dict[str, Any]:
    """读取题目；不存在抛 404。"""
    rec = repo.get_question(qid)
    if rec is None:
        raise NotFound(f"题目不存在：{qid}")
    return rec


@router.post("/custom")
def create_custom_question(
    payload: CustomQuestionRequest,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """手工指定一道题（含初始持仓），落 ``dim_question``。"""
    repo = Repository()
    # 校验证券存在 + 区间有数据
    stock = repo.get_stock(payload.code)
    if stock is None:
        raise NotFound(f"证券不存在或未初始化：{payload.code}")
    probe = repo.get_daily(payload.code, start=payload.start_date, end=payload.end_date)
    if probe is None or probe.empty:
        raise ValidationError(f"区间 {payload.start_date}~{payload.end_date} 无行情数据")

    qid = _make_question_id(payload)
    ip_json = None
    if payload.position_type == "holding":
        if payload.initial_position is None:
            raise ValidationError("持仓型题目必须提供 initial_position")
        ip_json = json.dumps(
            {
                "shares": payload.initial_position.shares,
                "avg_cost": payload.initial_position.avg_cost,
                "cost_date": payload.initial_position.cost_date.isoformat()
                if payload.initial_position.cost_date
                else None,
            },
            ensure_ascii=False,
        )
    settle_window = payload.settle_window
    row = [
        qid,
        payload.code,
        payload.start_date,
        payload.end_date,
        "",
        payload.position_type,
        payload.position_state_tier,
        ip_json,
        payload.initial_cash,
        "manual",
        payload.end_date,  # visible_until = end_date（决策点）
        settle_window,
        json.dumps({}, ensure_ascii=False),
        json.dumps([payload.end_date.isoformat()], ensure_ascii=False),  # N13：M1 单决策点
        payload.question_mode,
        f"{stock.name or payload.code} 手工题",
        "",
        "",
        None,
        None,
        now_bj_naive(),
    ]
    collist = ", ".join(_QUESTION_COLUMNS)
    placeholders = ", ".join(["?"] * len(_QUESTION_COLUMNS))
    with repo.manager.acquire_write("app") as con:
        con.execute(
            f"INSERT OR REPLACE INTO dim_question ({collist}) VALUES ({placeholders})",
            row,
        )
    log.info("question_created", qid=qid, code=payload.code, mode=payload.question_mode, by=username)
    rec = _load_question(repo, qid)
    return envelope(_ser_question(rec), "题目已创建")


@router.get("/{qid}")
def get_question(qid: str, username: str = Depends(get_current_user)) -> dict[str, Any]:
    """题目详情。"""
    rec = _load_question(Repository(), qid)
    return envelope(_ser_question(rec))


@router.get("/{qid}/kline")
def get_kline(
    qid: str,
    adjust: str = Query(default="qfq", pattern="^(qfq|hfq|none)$"),
    indicators: str = Query(default="ma"),
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """K 线数据（前/后/不复权），**严格截断于决策点**（红线①）。"""
    repo = Repository()
    rec = _load_question(repo, qid)
    code = rec["code"]
    end_date = pd.Timestamp(rec["end_date"]).date()
    start_date = pd.Timestamp(rec["start_date"]).date()

    raw = repo.get_daily(code, start=start_date, end=end_date)
    masked = mask_df(raw, end_date, "date")  # 红线①
    if masked is None or masked.empty:
        raise NotFound("该区间无可见行情数据")

    adjusted = PriceAdjuster(repo).adjust(masked, AdjustMode(adjust))
    bars = [
        {
            "date": pd.Timestamp(r["date"]).date().isoformat(),
            "open": round(float(r["open"]), 4),
            "high": round(float(r["high"]), 4),
            "low": round(float(r["low"]), 4),
            "close": round(float(r["close"]), 4),
            "volume": int(r["volume"]) if r.get("volume") is not None else 0,
            "amount": round(float(r["amount"]), 2) if r.get("amount") is not None else 0.0,
        }
        for _, r in adjusted.iterrows()
    ]

    ind_data: dict[str, Any] = {}
    requested = [x.strip() for x in (indicators or "").split(",") if x.strip()]
    if "ma" in requested:
        closes = pd.to_numeric(adjusted["close"], errors="coerce")
        dates = [b["date"] for b in bars]
        ma: dict[str, list[float | None]] = {}
        for w in MA_WINDOWS:
            series = closes.rolling(window=w).mean()
            ma[f"ma{w}"] = [None if pd.isna(v) else round(float(v), 4) for v in series]
        ind_data["ma"] = {"dates": dates, "series": ma}
    # 说明：MACD/KDJ/BOLL 等指标按 FR-3.7 规划，M1 先交付均线（严格截断）

    visible_until = max((b["date"] for b in bars), default=end_date.isoformat())
    return envelope(
        {
            "code": code,
            "adjust": adjust,
            "bars": bars,
            "indicators": ind_data,
            "visible_until": visible_until,
            "cutoff": end_date.isoformat(),
            "requested_indicators": requested,
        }
    )


@router.get("/{qid}/advice")
def get_advice(qid: str, username: str = Depends(get_current_user)) -> dict[str, Any]:
    """判卷模式（N6）：模型仓位建议（只用决策点前数据，严格过 VisibilityGuard）。"""
    repo = Repository()
    rec = _load_question(repo, qid)
    if str(rec.get("question_mode") or "normal") != "judge":
        raise ValidationError("该接口仅在判卷模式（question_mode=judge）下可用")
    end_date = pd.Timestamp(rec["end_date"]).date()
    advice = AdviceEngine(repo).advise_for(rec["code"], end_date)
    return envelope(advice.to_dict())


@stocks_router.get("")
def list_stocks(
    as_of: date | None = Query(default=None),
    board: str | None = Query(default=None),
    industry: str | None = Query(default=None),
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """时点标的池（红线⑥）：某历史日在市的证券。"""
    repo = Repository()
    provider = UniverseProvider(repo)
    day = as_of or pd.Timestamp.today().date()
    stocks = provider.as_of(day)
    items = [
        {"code": s.code, "name": s.name, "board": s.board, "industry": s.industry}
        for s in stocks
        if (board is None or s.board == board) and (industry is None or s.industry == industry)
    ]
    return envelope({"items": items, "total": len(items), "as_of": day.isoformat()})


__all__ = ["router", "stocks_router"]
