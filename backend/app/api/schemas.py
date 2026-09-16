"""全局 Pydantic 请求 / 响应模型（DTO 唯一定义处）。

对齐设计 §8：统一响应 ``{code, data, message}``（``code=0`` 成功）。
前端 ``api/types.ts`` 与后端 DTO **字段名逐字对齐**。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def envelope(data: Any, message: str = "ok") -> dict[str, Any]:
    """统一响应包装 ``{code, data, message}``。"""
    return {"code": 0, "data": data, "message": message}


# ══════════════════ 认证 ══════════════════
class LoginRequest(BaseModel):
    """登录入参。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    """登录出参。"""

    token: str
    expires_at: datetime
    username: str


# ══════════════════ 出题 ══════════════════
class InitialPositionDTO(BaseModel):
    """持仓型题目的初始持仓。"""

    shares: int = Field(default=0, ge=0)
    avg_cost: float = Field(default=0.0, ge=0.0)
    cost_date: date | None = None


class CustomQuestionRequest(BaseModel):
    """手工指定一道题（M1-T4）。"""

    code: str = Field(min_length=1, max_length=16)
    start_date: date
    end_date: date
    position_type: Literal["empty", "holding"] = "empty"
    position_state_tier: str | None = None
    initial_position: InitialPositionDTO | None = None
    initial_cash: float = Field(default=100000.0, gt=0.0)
    settle_window: int | None = Field(default=None, ge=0)
    question_mode: Literal["normal", "judge"] = "normal"

    @field_validator("code")
    @classmethod
    def _norm_code(cls, v: str) -> str:
        v = v.strip()
        if "." not in v and len(v) == 6:
            # 容错：裸 6 位代码按交易所前缀补全
            v = ("sh." if v[0] in ("6", "9") else "sz.") + v
        return v

    @field_validator("end_date")
    @classmethod
    def _check_range(cls, v: date, info: Any) -> date:
        start = info.data.get("start_date")
        if start is not None and v < start:
            raise ValueError("end_date 不能早于 start_date")
        return v


# ══════════════════ 下单 ══════════════════
class OrderRequest(BaseModel):
    """下单入参（判断模式 / 判卷模式共用）。

    判卷模式下可带 ``judge_verdict``（agree/disagree），仅用于 FR-6.5 模式分账，
    **不做对错判定**。
    """

    question_id: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    shares: int = Field(gt=0)
    judge_verdict: Literal["agree", "disagree"] | None = None


# ══════════════════ 结算 ══════════════════
class SettleRequest(BaseModel):
    """结算入参（可选覆盖结算窗口）。"""

    window: int | None = Field(default=None, ge=0)


# ══════════════════ K 线 ══════════════════
class KlineQuery(BaseModel):
    """K 线查询入参。"""

    adjust: Literal["qfq", "hfq", "none"] = "qfq"
    from_date: date | None = None
    to_date: date | None = None


__all__ = [
    "envelope",
    "LoginRequest",
    "LoginResponse",
    "InitialPositionDTO",
    "CustomQuestionRequest",
    "OrderRequest",
    "SettleRequest",
    "KlineQuery",
]
