"""QA 独立验证（严过关）——不复述工程师结论，独立攻击六条红线 + Q4 / NA3 / 判卷语义。

设计原则
--------
1. **不复用工程师的夹具**：真实数据走真实 ``market.duckdb``（只读/独立 manager），
   不依赖 ``stub_repo``，避免"测试夹具自证"。
2. **攻击性**：每条红线都尝试**推翻**，而非确认。
3. **可复现**：命令与断言都可重跑。

运行：
    mkdir -p /tmp/qa_bt && TMPDIR=/tmp/qa_bt .venv/bin/python -m pytest \\
        --basetemp=/tmp/qa_bt/bt -o addopts="" -q backend/tests/test_qa_independent.py
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.config import Settings
from app.core.db import DuckDBManager
from app.data.models import limit_pct_of
from app.data.repository import Repository
from app.data.universe import UniverseProvider
from app.engine.settle import SettlementEngine
from app.engine.trade_engine import TradeEngine

REAL_ROOT = Path.home() / "workspace" / "fupan"
MARKET_DB = REAL_ROOT / "market.duckdb"

_HAVE_REAL_DB = MARKET_DB.exists()
real_db = pytest.mark.skipif(not _HAVE_REAL_DB, reason="真实 market.duckdb 不存在（M0 未拉数）")


@pytest.fixture()
def repo() -> Repository:
    """独立 manager（不污染进程级单例），指向真实数据目录。"""
    mgr = DuckDBManager(Settings(data_root=REAL_ROOT))
    try:
        yield Repository(mgr)
    finally:
        mgr.close()


# ══════════════════════════════════════════════════════════════════
# 红线③：真实一字板日期（不用构造夹具）
# ══════════════════════════════════════════════════════════════════
@real_db
def test_r3_real_one_word_up_rejected(repo: Repository) -> None:
    """真实一字涨停（贵州茅台 2006-02-13，open=high=low=close=涨停价）→ 拒买。

    该日期由 SQL 在真实数据中筛选得到（非夹具构造），见 __doc__。

    注（FR-4.8）：成交时点为 **T+1**；故决策日 = 2006-02-13 的**前一交易日 2006-02-10**，
    成交日 = 2006-02-13（一字涨停）→ 拒单。
    """
    code, fill_day, decision_day = "sh.600519", date(2006, 2, 13), date(2006, 2, 10)
    bar = TradeEngine(repo).resolve_bar(code, fill_day)
    assert bar is not None
    # 先用独立口径核对"这确实是一字涨停"
    prev = repo.get_prev_close(code, fill_day)
    lp = repo.get_limit(code, fill_day)
    assert lp is not None
    assert abs(lp.limit_up - round(prev * 1.10, 2)) < 1e-9  # 独立算：前收 × 1.1
    assert bar["high"] == bar["low"] == bar["open"] == bar["close"] == lp.limit_up

    acc = _fresh_account(code)
    fill = TradeEngine(repo).place_order(account=acc, code=code, side="buy", shares=100, day=decision_day)
    assert fill.accepted is False
    assert fill.fill_day == fill_day  # 成交日 = T+1 = 一字涨停日
    assert "一字涨停" in fill.reason


@real_db
def test_r3_real_one_word_down_rejected(repo: Repository) -> None:
    """真实一字跌停（贵州茅台 2018-10-29）→ 拒卖（有可用持仓）。

    注（FR-4.8）：决策日 = 2018-10-29 的**前一交易日 2018-10-26**，成交日 = 2018-10-29。
    """
    code, fill_day, decision_day = "sh.600519", date(2018, 10, 29), date(2018, 10, 26)
    bar = TradeEngine(repo).resolve_bar(code, fill_day)
    lp = repo.get_limit(code, fill_day)
    assert bar is not None and lp is not None
    assert bar["high"] == bar["low"] == bar["open"] == bar["close"] == lp.limit_down

    acc = _fresh_account(code)
    acc.position.shares = 1000
    acc.position.available_shares = 1000
    acc.position.avg_cost = 600.0
    fill = TradeEngine(repo).place_order(account=acc, code=code, side="sell", shares=100, day=decision_day)
    assert fill.accepted is False
    assert fill.fill_day == fill_day
    assert "一字跌停" in fill.reason


def _exact_limit(prev: float, pct: float, sign: int) -> float:
    """独立口径：精确十进制 × (1±pct) 后 HALF_UP 到分（交易所口径）。"""
    from decimal import ROUND_HALF_UP, Decimal

    factor = Decimal(1) + sign * Decimal(str(pct))
    return float((Decimal(str(prev)) * factor).quantize(Decimal("0.01"), ROUND_HALF_UP))


@real_db
def test_r3_limit_up_matches_exact_half_up(repo: Repository) -> None:
    """涨停价逐行对齐"精确十进制 HALF_UP"（独立 oracle，非项目自身 round_to_cent）。"""
    df = _limit_vs_oracle(repo)
    bad = [r for r in df if r["up_bad"]]
    assert bad == [], f"涨停价与精确 HALF_UP 不一致 {len(bad)} 行：{bad[:5]}"


@real_db
def test_r3_limit_down_matches_exact_half_up(repo: Repository) -> None:
    """跌停价逐行对齐"精确十进制 HALF_UP"。

    **已修复**（B 缺陷）：源码改为 ``limit_prices_of``（Decimal HALF_UP 唯一实现），
    不再出现「半分位少 1 分」。原 ``xfail`` 标记随修复移除。
    """
    df = _limit_vs_oracle(repo)
    bad = [r for r in df if r["dn_bad"]]
    assert bad == [], f"跌停价与精确 HALF_UP 不一致 {len(bad)} 行：{bad[:5]}"


@real_db
def test_r3_limit_down_deviation_is_gone(repo: Repository) -> None:
    """**B 缺陷回归**：跌停价与精确 HALF_UP 的偏差行数必须为 **0**（修复前为 145 行）。"""
    df = _limit_vs_oracle(repo)
    bad = [r for r in df if r["dn_bad"]]
    assert bad == [], f"仍存在 {len(bad)} 行跌停价偏差：{bad[:5]}"
    # 且全表（涨+跌）无任何偏差
    assert df == [], f"全表仍有偏差 {len(df)} 行"


def _limit_vs_oracle(repo: Repository) -> list[dict]:
    """遍历 dim_limit 全表，逐行与精确十进制 HALF_UP 比对，返回差异清单。"""
    con = repo.manager.get_read("market")
    rows = con.execute(
        """
        SELECT l.code, l.date, l.limit_up, l.limit_down,
               (SELECT f.close FROM fact_daily f
                  WHERE f.code = l.code AND f.date < l.date
                  ORDER BY f.date DESC LIMIT 1) AS prev
        FROM dim_limit l
        """
    ).fetchdf()
    out: list[dict] = []
    for r in rows.itertuples(index=False):
        if r.prev is None:
            continue
        d = pd.Timestamp(r.date).date()
        pct = limit_pct_of(str(r.code), d)
        if pct is None:
            continue
        exp_up = _exact_limit(float(r.prev), pct, +1)
        exp_dn = _exact_limit(float(r.prev), pct, -1)
        up_bad = abs(float(r.limit_up) - exp_up) > 1e-9
        dn_bad = abs(float(r.limit_down) - exp_dn) > 1e-9
        if up_bad or dn_bad:
            out.append(
                {
                    "code": str(r.code),
                    "date": str(d),
                    "prev": float(r.prev),
                    "src_up": float(r.limit_up),
                    "exact_up": exp_up,
                    "src_down": float(r.limit_down),
                    "exact_down": exp_dn,
                    "up_bad": up_bad,
                    "dn_bad": dn_bad,
                }
            )
    return out


# ══════════════════════════════════════════════════════════════════
# Q4 ：涨跌停日期分段（分界当天两档边界）
# ══════════════════════════════════════════════════════════════════
def test_q4_gem_regime_boundary_exact() -> None:
    """创业板分界：2020-08-21（含）仍 ±10%；2020-08-24（分界当天）即 ±20%。"""
    assert limit_pct_of("sz.300750", "2020-08-21") == 0.10
    assert limit_pct_of("sz.300750", "2020-08-24") == 0.20  # 分界当天 = 新制
    assert limit_pct_of("sz.300750", "2020-08-23") == 0.10  # 分界前一日


@real_db
def test_q4_gem_dim_limit_boundary_values(repo: Repository) -> None:
    """落库的 dim_limit 在 2020-08-20 / 08-25 两日的实际幅度应分别为 10% / 20%。"""
    for d, pct in [("2020-08-20", 0.10), ("2020-08-25", 0.20)]:
        day = pd.Timestamp(d).date()
        prev = repo.get_prev_close("sz.300750", day)
        lp = repo.get_limit("sz.300750", day)
        assert lp is not None
        assert abs(lp.limit_up - round(prev * (1 + pct), 2)) < 1e-9, d
        assert abs(lp.limit_down - round(prev * (1 - pct), 2)) < 1e-9, d


def test_q4_star_and_bse_boundaries() -> None:
    """科创板 2019-07-22 开板即 ±20%；北交所 ±30%。"""
    assert limit_pct_of("sh.688981", "2019-07-22") == 0.20
    assert limit_pct_of("bj.830799", "2021-11-15") == 0.30


def test_q4_pre_1996_main_board_has_no_limit() -> None:
    """主板 1996-12-16 之前无统一涨跌停 → 返回 None。"""
    assert limit_pct_of("sh.600000", "1996-12-15") is None
    assert limit_pct_of("sh.600000", "1996-12-16") == 0.10


@real_db
def test_q4_no_limit_rows_before_1996(repo: Repository) -> None:
    """dim_limit 中不应存在 1996-12-16 之前的行（Q4 清空断言）。"""
    con = repo.manager.get_read("market")
    n = con.execute("SELECT COUNT(*) FROM dim_limit WHERE date < DATE '1996-12-16'").fetchone()[0]
    assert n == 0
    # 且 1996-12-16 当天应当**有**行（制度生效日）
    n2 = con.execute("SELECT COUNT(*) FROM dim_limit WHERE date = DATE '1996-12-16'").fetchone()[0]
    assert n2 >= 1


# ══════════════════════════════════════════════════════════════════
# 红线⑥：时点标的池（真实 dim_stock，抽查历史时点）
# ══════════════════════════════════════════════════════════════════
@real_db
def test_r6_as_of_excludes_not_yet_listed(repo: Repository) -> None:
    """2010-06-01：不得含 2018 才上市的 sz.300750，也不得含 2010-08-12 才上市的 sz.300104。"""
    codes = set(UniverseProvider(repo).as_of_codes("2010-06-01"))
    assert "sz.300750" not in codes  # list 2018-06-11
    assert "sz.300104" not in codes  # list 2010-08-12（晚于 2010-06-01）
    assert "sh.600000" in codes  # list 1999-11-10，在市


@real_db
def test_r6_as_of_excludes_already_delisted(repo: Repository) -> None:
    """2020-08-01：不得含已于 2020-07-21 退市的 sz.300104。"""
    codes = set(UniverseProvider(repo).as_of_codes("2020-08-01"))
    assert "sz.300104" not in codes


@real_db
def test_r6_as_of_includes_then_listed_and_open_ended(repo: Repository) -> None:
    """2015-06-01：sz.300104（2010 上市、2020 退市）应在市；sh.600000 应在市。"""
    codes = set(UniverseProvider(repo).as_of_codes("2015-06-01"))
    assert {"sz.300104", "sh.600000", "sz.000001"}.issubset(codes)


# ══════════════════════════════════════════════════════════════════
# NA3：退市口径（is_trade 最后一行，而非 MAX(date)）
# ══════════════════════════════════════════════════════════════════
@real_db
def test_na3_last_tradable_is_2020_07_20_not_07_21(repo: Repository) -> None:
    """乐视退 sz.300104：最后可成交日 = 2020-07-20；2020-07-21 不可成交。"""
    last = repo.get_last_tradable("sz.300104")
    assert last is not None
    assert pd.Timestamp(last["date"]).date() == date(2020, 7, 20)
    # 反证：MAX(date) 会取到 2020-07-21（不可成交）
    assert repo.max_date("sz.300104") == date(2020, 7, 21)
    # 2020-07-21 确实停牌/无成交
    bar_0721 = TradeEngine(repo).resolve_bar("sz.300104", date(2020, 7, 21))
    assert bar_0721["is_trade"] is False or bar_0721["volume"] == 0


@real_db
def test_na3_settlement_exit_day_uses_last_tradable(repo: Repository) -> None:
    """结算路径：窗口跨越退市日时，退出日取最后可成交日 2020-07-20 且标记退市。"""
    engine = SettlementEngine(repo, trade_engine=TradeEngine(repo))
    exit_day, delisted = engine.resolve_exit_day("sz.300104", date(2020, 6, 30), 20)
    assert delisted is True
    assert exit_day == date(2020, 7, 20)


# ══════════════════════════════════════════════════════════════════
# 红线④：快照冻结 —— 不只改 adj_factor，换改法（改费率 / 改滑点 / 改窗口）
# ══════════════════════════════════════════════════════════════════
def _r4_fixture():  # type: ignore[no-untyped-def]
    """构造一段日线 + 一笔常去，复刻 API 的真实重算路径。"""
    from tests.stub_repo import StubRepo, make_daily, ts

    code, bench = "sz.000001", "sh.000300"
    bars = make_daily(
        code,
        [
            {"date": "2020-01-02", "close": 10.0},
            {"date": "2020-01-03", "close": 10.5},
            {"date": "2020-01-06", "close": 11.0},
            {"date": "2020-01-07", "close": 10.8},
            {"date": "2020-01-08", "close": 11.5},
            {"date": "2020-01-09", "close": 12.0},
            {"date": "2020-01-10", "close": 11.9},
            {"date": "2020-01-13", "close": 12.4},
            {"date": "2020-01-14", "close": 12.6},
        ],
    )
    idx = pd.DataFrame(
        {
            "index_code": bench,
            "date": [pd.Timestamp(d).date() for d in ["2020-01-02", "2020-01-09", "2020-01-14"]],
            "close": [4000.0, 4100.0, 4150.0],
        }
    )
    repo = StubRepo(
        {code: bars},
        trading_days=[
            "2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08",
            "2020-01-09", "2020-01-10", "2020-01-13", "2020-01-14",
        ],
        index_daily={bench: idx},
    )
    q = {
        "question_id": "q_r4",
        "code": code,
        "start_date": "2020-01-02",
        "end_date": "2020-01-02",
        "initial_cash": 100000.0,
        "settle_window": 5,
    }
    order = {
        "order_id": "o_r4",
        "question_id": "q_r4",
        "side": "buy",
        "shares": 1000,
        "price": 10.0,
        "dt": ts("2020-01-02"),
    }
    return repo, q, order


def _r4_settle(repo, q, order, snap, *, fee=None, slip=None, window=None):  # type: ignore[no-untyped-def]
    """复刻 API：用**当前费率**回放订单得账户，再结算（结算**只读快照**）。

    注（C 缺陷/FR-4.6 修复后）：``fee`` / ``slip`` / ``window`` 入参**不再影响结算结果**——
    结算的费率族 / 窗口 / 账户全部取自快照。保留入参以证明"改了也不变"。
    """
    from app.config import Settings
    from app.engine.cost import CostModel
    from app.engine.trade_engine import replay_orders

    cfg = Settings()
    cm = CostModel(cfg, commission_rate=fee, slippage_rate=slip)
    eng = TradeEngine(repo, cost_model=cm)
    acc = replay_orders(eng, q, [order])
    return SettlementEngine(repo, trade_engine=eng).settle(order, q, snapshot=snap, account=acc, window=window)


def _freeze(repo, q, order):  # type: ignore[no-untyped-def]
    """按 API 口径冻结**完整快照**：费率族 + 结算窗口（题目 5）+ 因子 + 端点 + 下单后账户。"""
    from app.config import Settings
    from app.engine.cost import CostModel
    from app.engine.snapshot import SnapshotFreezer
    from app.engine.trade_engine import replay_orders

    cfg = Settings()
    eng = TradeEngine(repo)
    window = int(q.get("settle_window") or cfg.settle_window)
    exit_day, _ = SettlementEngine(repo, trade_engine=eng).resolve_exit_day(q["code"], date(2020, 1, 2), window)
    acc = replay_orders(eng, q, [order])
    snap = SnapshotFreezer(repo).freeze_for_order(
        order,
        code=q["code"],
        fill_day="2020-01-02",
        exit_day=exit_day,
        fee_version="v1",
        fill_price=10.0,
        start_day="2020-01-02",
        cost_model=CostModel(cfg),
        window=window,
        account=acc,
    )
    order["params_snapshot"] = snap.to_json()
    return snap


def test_r4_adj_factor_change_is_frozen() -> None:
    """基线：改 adj_factor 后重算，全部指标一致。"""
    repo, q, order = _r4_fixture()
    snap = _freeze(repo, q, order)
    r1 = _r4_settle(repo, q, order, snap)
    repo.bars[q["code"]]["adj_factor"] = 2.0
    r2 = _r4_settle(repo, q, order, snap)
    assert r1.to_dict() == r2.to_dict()


def test_r4_fee_and_slippage_change_do_not_alter_recompute() -> None:
    """**红线④ / FR-4.6 修复**：改**费率/滑点**后重算，账户收益**不变**（费率族已冻结）。

    （原用例断言"确有变化"以固化缺陷；修复后断言相等。）
    """
    repo, q, order = _r4_fixture()
    snap = _freeze(repo, q, order)

    base = _r4_settle(repo, q, order, snap, fee=2.5e-4, slip=5e-4)
    hi_fee = _r4_settle(repo, q, order, snap, fee=5e-2, slip=5e-4)
    hi_slip = _r4_settle(repo, q, order, snap, fee=2.5e-4, slip=5e-2)

    assert base.account_return == hi_fee.account_return
    assert base.account_return == hi_slip.account_return
    assert base.to_dict() == hi_fee.to_dict()
    assert base.to_dict() == hi_slip.to_dict()


def test_r4_window_change_does_not_alter_recompute() -> None:
    """**红线④ / FR-4.6 修复**：改**结算窗口**后重算，``exit_day`` 与 ``max_dd`` **不变**。"""
    repo, q, order = _r4_fixture()
    snap = _freeze(repo, q, order)  # 冻结时结算窗口 = 题目 5 → exit_day = 2020-01-09
    r5 = _r4_settle(repo, q, order, snap, window=5)
    r8 = _r4_settle(repo, q, order, snap, window=8)
    assert (r5.exit_day, r5.max_dd) == (r8.exit_day, r8.max_dd)
    assert r5.to_dict() == r8.to_dict()


def test_r4_spec_full_freeze_per_FR46() -> None:
    """规范断言（FR-4.6）：日后重算历史成绩，数字不变 —— 不受费率/滑点/窗口变化影响。"""
    repo, q, order = _r4_fixture()
    snap = _freeze(repo, q, order)
    base = _r4_settle(repo, q, order, snap, fee=2.5e-4, slip=5e-4, window=5)
    chg = _r4_settle(repo, q, order, snap, fee=5e-2, slip=5e-2, window=8)
    assert base.to_dict() == chg.to_dict()


# ══════════════════════════════════════════════════════════════════
# 红钱⑤：唯一成交实现 —— 机械代码扫描
# ══════════════════════════════════════════════════════════════════
def test_r5_only_one_fill_price_implementation() -> None:
    """扫描 backend：**成交价**（含滑点）实现只能有一处（``CostModel.exec_price``）。

    涨跌停价的 ``(1 ± pct)`` 已抽到**唯一函数** ``models.limit_prices_of``（Decimal HALF_UP），
    故原始 ``* (1 ± …)`` 形态只应出现在 ``engine/cost.py``。
    """
    import re

    root = Path(__file__).resolve().parents[1] / "app"
    all_mul: dict[str, list[str]] = {}
    slip_mul: list[str] = []
    for py in root.rglob("*.py"):
        rel = str(py.relative_to(root))
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "``" in line:  # 跳过注释与 docstring
                continue
            if re.search(r"\*\s*\(1\s*[+-]", line):
                all_mul.setdefault(rel, []).append(f"{rel}:{i}: {stripped}")
                if "slippage" in line:
                    slip_mul.append(f"{rel}:{i}: {stripped}")

    # 成交价唯一实现：仅 cost.py
    assert set(all_mul) == {"engine/cost.py"}, set(all_mul)
    assert len(slip_mul) == 1 and slip_mul[0].startswith("engine/cost.py:"), slip_mul
    # 涨跌停价唯一实现：models.limit_prices_of（Decimal HALF_UP）
    from app.data.models import limit_prices_of

    assert limit_prices_of(27.65, 0.10) == (30.42, 24.89)


def test_r5_single_trade_engine_class() -> None:
    """全仓只有一个 ``TradeEngine`` 类定义（成交/持仓唯一实现）。"""
    import re

    root = Path(__file__).resolve().parents[1] / "app"
    hits = []
    for py in root.rglob("*.py"):
        if re.search(r"^class\s+TradeEngine\b", py.read_text(encoding="utf-8"), re.M):
            hits.append(str(py.relative_to(root)))
    assert hits == ["engine/trade_engine.py"], hits


# ══════════════════════════════════════════════════════════════════
# 判卷模式语义：不产生"对/错"、结算共用同一引擎
# ══════════════════════════════════════════════════════════════════
def test_judge_mode_settlement_has_no_right_wrong_semantics() -> None:
    """判卷模式结算结果中不得出现任何"对/错/成功/失败/追高/杀跌"语义。"""
    repo, q, order = _r4_fixture()
    snap = _freeze(repo, q, order)
    res = _r4_settle(repo, q, order, snap)
    blob = str(res.to_dict()) + res.notes
    for w in ("对", "错", "成功", "失败", "追高", "杀跌", "正确", "错误", "赢", "亏"):
        assert w not in blob, f"结算结果含判定性字样：{w}"


def test_judge_mode_reuses_single_settlement_engine() -> None:
    """判卷模式不得引入第二套结算：全仓只有一个 SettlementEngine 实现。"""
    import re

    root = Path(__file__).resolve().parents[1] / "app"
    hits = [
        str(py.relative_to(root))
        for py in root.rglob("*.py")
        if re.search(r"^class\s+\w*SettlementEngine\b", py.read_text(encoding="utf-8"), re.M)
    ]
    assert hits == ["engine/settle.py"], hits


# ══════════════════════════════════════════════════════════════════
# 红线②：复权三口径 —— 真实多次分红股票，手工核对区间收益
# ══════════════════════════════════════════════════════════════════
@real_db
def test_r2_three_calibers_mutually_derivable_on_real_data(repo: Repository) -> None:
    """真实数据：qfq = hfq / f_last，none = hfq / f，三口径可互推。"""
    import numpy as np

    from app.data.adjuster import PriceAdjuster
    from app.data.models import AdjustMode

    df = repo.get_daily("sh.600000", start="2000-01-01", end="2010-12-31")
    assert df["adj_factor"].nunique() > 5  # 多次分红/送股
    adj = PriceAdjuster()
    none = adj.adjust(df, AdjustMode.NONE)
    hfq = adj.adjust(df, AdjustMode.HFQ)
    qfq = adj.adjust(df, AdjustMode.QFQ)
    f = df["adj_factor"].astype(float).to_numpy()
    f_last = float(df["adj_factor"].iloc[-1])
    np.testing.assert_allclose(none["close"].to_numpy(), hfq["close"].to_numpy() / f)
    np.testing.assert_allclose(qfq["close"].to_numpy(), hfq["close"].to_numpy() / f_last)


@real_db
def test_r2_interval_return_hfq_differs_from_none(repo: Repository) -> None:
    """区间含分红时，后复权收益 与 不复权收益 **不相等**，且方向合理（分红使收益更高）。"""
    from app.data.adjuster import PriceAdjuster
    from app.data.models import AdjustMode

    # 2006-05-25 有复权因子跳变（分红外）→ 区间跨越除权
    df = repo.get_daily("sh.600000", start="2006-01-04", end="2006-12-29")
    assert df["adj_factor"].nunique() > 1, "该区间应包含除权"
    adj = PriceAdjuster()
    hfq = adj.adjust(df, AdjustMode.HFQ)
    none = adj.adjust(df, AdjustMode.NONE)
    r_hfq = float(hfq["close"].iloc[-1]) / float(hfq["close"].iloc[0]) - 1.0
    r_none = float(none["close"].iloc[-1]) / float(none["close"].iloc[0]) - 1.0
    assert abs(r_hfq - r_none) > 1e-6, (r_hfq, r_none)
    assert r_hfq > r_none, f"分红后复权收益应不低于不复权：hfq={r_hfq} none={r_none}"


# ══════════════════════════════════════════════════════════════════
# FR-4.1 成交时点：**已修复为 T+1 开盘价**
# ══════════════════════════════════════════════════════════════════
@real_db
def test_fr41_fill_price_is_next_open_not_decision_close(repo: Repository) -> None:
    """红线 / FR-4.1：成交价 = **T+1 开盘** × (1+滑点)，而非 T 日收盘。

    规范：``01-业务需求MRD`` FR-4.1「判断模式用 **T+1 开盘价** + 滑点」；
    ``05-产品文档`` §8.2 / §11.2「从决策点 T 的下一个交易日开盘价成交」。
    实现（已修复）：``place_order(day=T)`` 内部解析成交日 = T+1，参考价取 T+1 ``open``。
    """
    code, T, T1 = "sh.600519", "2023-06-30", "2023-07-03"
    bardf = repo.get_daily(code, start=T, end=T1)
    t_close = float(bardf.iloc[0]["close"])
    t1_open = float(bardf.iloc[1]["open"])
    assert t_close != t1_open, "样本日 T 收盘应与 T+1 开盘不同（否则无法区分口径）"

    acc = _fresh_account(code)
    fill = TradeEngine(repo).place_order(account=acc, code=code, side="buy", shares=100, day=T)
    assert str(fill.fill_day) == T1, "成交日应为 T+1"
    assert abs(fill.ref_price - t1_open) < 1e-9, "ref_price 应取 T+1 开盘价"
    assert abs(fill.price - round(t1_open * 1.0005, 2)) < 1e-9
    assert abs(fill.price - round(t_close * 1.0005, 2)) > 1e-9, "若相等则仍是 T 日收盘口径"


# ══════════════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════════════
def _fresh_account(code: str):  # type: ignore[no-untyped-def]
    from app.engine.position import Account, Position

    return Account(cash=10_000_000.0, initial_cash=10_000_000.0, position=Position(code=code))

