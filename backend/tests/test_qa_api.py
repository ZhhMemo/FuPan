"""QA 独立验证（API 层）——红线①端到端 + 判卷模式闭环 + 结算延后。

用 ``TestClient`` 打真实路由，数据库为 ``~/workspace/fupan`` 的**临时副本**
（避免污染真实 app 库）；鉴权用依赖覆盖绕过。

运行：
    mkdir -p /tmp/qa_bt && TMPDIR=/tmp/qa_bt .venv/bin/python -m pytest \\
        --basetemp=/tmp/qa_bt/bt -o addopts="" -q backend/tests/test_qa_api.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REAL_ROOT = Path.home() / "workspace" / "fupan"

CODE = "sh.600519"  # 贵州茅台：历史长、无退市、区间可控
START = "2023-01-03"
END = "2023-06-30"


def _real_db_available() -> bool:
    return (REAL_ROOT / "market.duckdb").exists() and (REAL_ROOT / "app.duckdb").exists()


pytestmark = pytest.mark.skipif(not _real_db_available(), reason="真实 DuckDB 不存在")


@pytest.fixture()
def qa_root(tmp_path: Path) -> Path:
    """把真实双库复制到临时目录（与 client 共享同一 tmp_path）。"""
    for f in ("market.duckdb", "app.duckdb"):
        shutil.copy(REAL_ROOT / f, tmp_path / f)
    return tmp_path


@pytest.fixture()
def client(qa_root: Path, monkeypatch):  # type: ignore[no-untyped-def]
    """构造指向临时库、已绕过鉴权的 TestClient。"""
    from fastapi.testclient import TestClient

    from app.api.deps import get_current_user, get_security_service
    from app.config import Settings
    from app.core import db as dbmod
    from app.core.db import DuckDBManager

    mgr = DuckDBManager(Settings(data_root=qa_root))
    monkeypatch.setattr(dbmod, "_MANAGER", mgr, raising=False)
    get_security_service.cache_clear()

    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: "qa"
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        get_security_service.cache_clear()
        mgr.close()


def _make_question(client, *, mode: str = "normal", position_type: str = "empty"):  # type: ignore[no-untyped-def]
    body = {
        "code": CODE,
        "start_date": START,
        "end_date": END,
        "position_type": position_type,
        "question_mode": mode,
    }
    if position_type == "holding":
        body["initial_position"] = {"shares": 100, "avg_cost": 1700.0}
    r = client.post("/api/questions/custom", json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["question_id"]


def _read_market(sql: str, params=None):  # type: ignore[no-untyped-def]
    """经（已 patched 的）进程级 manager 读临时 market 库。"""
    from app.core.db import get_manager

    return get_manager().get_read("market").execute(sql, params or []).fetchdf()


# ══════════════════════════════════════════════════════════════════
# 红线①：K 线 / advice 严格截断于决策点
# ══════════════════════════════════════════════════════════════════
def test_r1_kline_never_exceeds_decision_point(client) -> None:  # type: ignore[no-untyped-def]
    """/kline 任何口径下 max(date) <= end_date，且指标末点 <= 决策点。"""
    # 反证前提：库里确实存在决策点之后的数据
    after = _read_market(
        "SELECT COUNT(*) n FROM fact_daily WHERE code = ? AND date > ?", [CODE, END]
    )["n"].iloc[0]
    assert after > 100, "该股在决策点后应仍有大量数据（否则截断无从验证）"

    qid = _make_question(client)
    for adjust in ("qfq", "hfq", "none"):
        k = client.get(f"/api/questions/{qid}/kline", params={"adjust": adjust, "indicators": "ma"})
        assert k.status_code == 200, k.text
        data = k.json()["data"]
        dates = [b["date"] for b in data["bars"]]
        assert dates == sorted(dates)
        assert max(dates) <= END, f"{adjust} 口径 kline 泄漏决策点后数据：{max(dates)}"
        assert data["cutoff"] == END
        assert data["visible_until"] <= END
        ma_dates = data["indicators"]["ma"]["dates"]
        assert max(ma_dates) <= END, f"MA 指标线延伸过决策点：{max(ma_dates)}"


def test_r1_advice_uses_only_pre_decision_window(client) -> None:  # type: ignore[no-untyped-def]
    """advice 的 close/ma 必须等于「仅用决策点前数据」独立算出的前复权 MA20。"""
    qid = _make_question(client, mode="judge")
    resp = client.get(f"/api/questions/{qid}/advice")
    assert resp.status_code == 200, resp.text
    adv = resp.json()["data"]
    assert adv["rule_version"] == "advice-ma20-v1"

    # 独立算：取决策点前最近 40 个交易日，前复权 = close * f / f_T（锚定 T），
    # 再算 MA20 及其相邻差（与 AdviceEngine 口径一致）
    df = _read_market(
        "SELECT date, close, adj_factor FROM fact_daily WHERE code = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 40",
        [CODE, END],
    ).iloc[::-1].reset_index(drop=True)
    anchor = float(df["adj_factor"].iloc[-1])
    qfq = (df["close"].astype(float) * df["adj_factor"].astype(float) / anchor).reset_index(drop=True)
    ma = qfq.rolling(20).mean()
    exp_ma = round(float(ma.iloc[-1]), 4)
    exp_close = round(float(qfq.iloc[-1]), 4)
    exp_slope = round(float(ma.iloc[-1]) - float(ma.iloc[-2]), 6)  # 用未取整的 MA 求斜率

    assert adv["ma"] == pytest.approx(exp_ma, abs=1e-3), (adv["ma"], exp_ma)
    assert adv["close"] == pytest.approx(exp_close, abs=1e-3), (adv["close"], exp_close)
    assert adv["slope"] == pytest.approx(exp_slope, abs=1e-5), (adv["slope"], exp_slope)


def test_r1_advice_rejects_non_judge_mode(client) -> None:  # type: ignore[no-untyped-def]
    """非判卷模式调用 advice → 400（语义边界）。"""
    qid = _make_question(client, mode="normal")
    r = client.get(f"/api/questions/{qid}/advice")
    assert r.status_code == 400, r.text


# ══════════════════════════════════════════════════════════════════
# FR-4.7：结算接口独立且延后（下单前不可调）
# ══════════════════════════════════════════════════════════════════
def test_settle_before_order_is_rejected(client) -> None:  # type: ignore[no-untyped-def]
    """未下单即结算 → 409（防止抓包提前看结果）。"""
    qid = _make_question(client)
    r = client.post(f"/api/settle/{qid}")
    assert r.status_code == 409, r.text


# ══════════════════════════════════════════════════════════════════
# 判卷模式闭环：建议 → 评判 + 下单 → 结算（共用同一引擎，不判对错）
# ══════════════════════════════════════════════════════════════════
def test_judge_mode_full_flow(client) -> None:  # type: ignore[no-untyped-def]
    """判卷模式全流程可跑通；结算结果无"对/错"字样；judge_verdict 落库。"""
    qid = _make_question(client, mode="judge")

    adv = client.get(f"/api/questions/{qid}/advice").json()["data"]
    assert adv["tier"] in ("high", "mid", "low")

    order = client.post(
        "/api/trade/order",
        json={"question_id": qid, "side": "buy", "shares": 50, "judge_verdict": "disagree"},
    )
    assert order.status_code == 200, order.text
    oid = order.json()["data"]["order_id"]

    settle = client.post(f"/api/settle/{qid}")
    assert settle.status_code == 200, settle.text
    sdata = settle.json()["data"]
    blob = str(sdata)
    for w in ("对", "错", "成功", "失败", "追高", "杀跌"):
        assert w not in blob, f"结算页面出现判定性字样：{w}"

    # judge_verdict 已入库（仅供分账，不判对错）
    from app.core.db import get_manager

    row = get_manager().get_read("app").execute(
        "SELECT judge_verdict, judge_advice FROM fact_order WHERE order_id = ?", [oid]
    ).fetchone()
    assert row[0] == "disagree"
    assert row[1] is not None  # 冻结的建议快照


def test_account_panel_reflects_holdings(client) -> None:  # type: ignore[no-untyped-def]
    """持仓型题目：账户面板显示初始持仓（FR-3.3）。"""
    qid = _make_question(client, position_type="holding")
    acc = client.get(f"/api/trade/account/{qid}")
    assert acc.status_code == 200, acc.text
    pos = acc.json()["data"]["position"]
    assert pos["shares"] == 100
    assert pos["available_shares"] == 100  # 历史建仓 → 可用（非当日买入）


# ══════════════════════════════════════════════════════════════════
# FR-8.1：未登录一律拒绝（401）；健康探针免鉴权
# ══════════════════════════════════════════════════════════════════
@pytest.fixture()
def anon_client(qa_root: Path, monkeypatch):  # type: ignore[no-untyped-def]
    """不带鉴权覆盖的 TestClient（用于验证 401）。"""
    from fastapi.testclient import TestClient

    from app.api.deps import get_security_service
    from app.config import Settings
    from app.core import db as dbmod
    from app.core.db import DuckDBManager

    mgr = DuckDBManager(Settings(data_root=qa_root))
    monkeypatch.setattr(dbmod, "_MANAGER", mgr, raising=False)
    get_security_service.cache_clear()
    from app.main import app

    try:
        with TestClient(app) as c:
            yield c
    finally:
        get_security_service.cache_clear()
        mgr.close()


def test_business_endpoints_require_auth(anon_client) -> None:  # type: ignore[no-untyped-def]
    """未带令牌访问业务接口 → 401。"""
    for method, path in [
        ("get", "/api/stocks"),
        ("get", "/api/questions/q_x"),
        ("post", "/api/questions/custom"),
        ("post", "/api/trade/order"),
        ("get", "/api/trade/account/q_x"),
        ("post", "/api/settle/q_x"),
        ("get", "/api/admin/health"),
    ]:
        call = getattr(anon_client, method)
        r = call(path, json={}) if method == "post" else call(path)
        assert r.status_code == 401, f"{method.upper()} {path} → {r.status_code}"


def test_health_endpoint_needs_no_auth(anon_client) -> None:  # type: ignore[no-untyped-def]
    """健康探针免鉴权。"""
    r = anon_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "ok"


def test_login_wrong_password_rejected(anon_client) -> None:  # type: ignore[no-untyped-def]
    """错误密码 → 401；且响应不含明文密码。"""
    r = anon_client.post("/api/auth/login", json={"username": "admin", "password": "definitely-wrong"})
    assert r.status_code == 401
    assert "definitely-wrong" not in r.text
