"""``06`` §8.2 / FR-3.7：K 线 ``from`` / ``to`` 区间参数（且**不得绕过红线① 截断**）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REAL_ROOT = Path.home() / "workspace" / "fupan"
CODE = "sh.600519"
START = "2023-01-03"
END = "2023-06-30"


def _real_db_available() -> bool:
    return (REAL_ROOT / "market.duckdb").exists() and (REAL_ROOT / "app.duckdb").exists()


pytestmark = pytest.mark.skipif(not _real_db_available(), reason="真实 DuckDB 不存在")


@pytest.fixture()
def qa_root(tmp_path: Path) -> Path:
    for f in ("market.duckdb", "app.duckdb"):
        shutil.copy(REAL_ROOT / f, tmp_path / f)
    return tmp_path


@pytest.fixture()
def client(qa_root: Path, monkeypatch):  # type: ignore[no-untyped-def]
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


def _make_question(client) -> str:  # type: ignore[no-untyped-def]
    body = {"code": CODE, "start_date": START, "end_date": END, "position_type": "empty"}
    r = client.post("/api/questions/custom", json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["question_id"]


def test_kline_from_to_clips_window(client) -> None:  # type: ignore[no-untyped-def]
    """``from`` / ``to`` 生效：返回区间落在 [from, to] 内且比全区间更短。"""
    qid = _make_question(client)
    full = client.get(f"/api/questions/{qid}/kline").json()["data"]["bars"]
    sub = client.get(
        f"/api/questions/{qid}/kline", params={"from": "2023-03-01", "to": "2023-04-01"}
    ).json()["data"]["bars"]
    assert sub, "子区间不应为空"
    assert len(sub) < len(full)
    assert all("2023-03-01" <= b["date"] <= "2023-04-01" for b in sub)


def test_kline_only_from(client) -> None:  # type: ignore[no-untyped-def]
    """仅传 ``from``：起点右移，终点仍为决策点。"""
    qid = _make_question(client)
    bars = client.get(f"/api/questions/{qid}/kline", params={"from": "2023-05-01"}).json()["data"]["bars"]
    assert bars
    assert bars[0]["date"] >= "2023-05-01"
    assert max(b["date"] for b in bars) <= END


def test_kline_to_beyond_decision_point_is_clipped(client) -> None:  # type: ignore[no-untyped-def]
    """红线①：``to`` 越过决策点也会被截断（不可借 ``to`` 偷看未来）。"""
    qid = _make_question(client)
    data = client.get(f"/api/questions/{qid}/kline", params={"to": "2024-12-31"}).json()["data"]
    assert data["bars"]
    assert max(b["date"] for b in data["bars"]) <= END
    assert data["cutoff"] == END


def test_kline_empty_range_rejected(client) -> None:  # type: ignore[no-untyped-def]
    """``from > to``：区间为空 → 400（明确报错，不静默返回空）。"""
    qid = _make_question(client)
    r = client.get(f"/api/questions/{qid}/kline", params={"from": "2023-05-01", "to": "2023-04-01"})
    assert r.status_code == 400, r.text
