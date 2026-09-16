"""FR-8.7 / P0：训练数据（``app.duckdb``）备份 + **恢复验证**（真导入 + 逐表比对）。"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.core.db import APP, DuckDBManager
from app.data.backup import TrainingDataBackup
from app.data.repository import Repository


def _make_app_db(tmp_path: Path) -> tuple[DuckDBManager, Repository]:
    """建一个临时 app.duckdb（含题目/订单/结算/用户/参数各一行）。"""
    cfg = Settings(data_root=tmp_path)
    mgr = DuckDBManager(cfg)
    mgr.init_schemas()
    with mgr.acquire_write(APP) as con:
        con.execute(
            "INSERT INTO dim_question (question_id, code, start_date, end_date, initial_cash) VALUES (?,?,?,?,?)",
            ["q_test", "sh.600000", "2024-01-02", "2024-01-05", 100000.0],
        )
        con.execute(
            "INSERT INTO fact_order (order_id, question_id, dt, side, shares, price) VALUES (?,?,?,?,?,?)",
            ["o_test", "q_test", "2024-01-02 15:00:00", "buy", 100, 10.6],
        )
        con.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)",
            ["param.settle_window", "20", "2024-01-02 15:00:00"],
        )
    return mgr, Repository(mgr)


def test_backup_and_restore_verification_passes(tmp_path: Path) -> None:
    """备份后**真正恢复**到临时库并逐表比对行数 —— 通过。"""
    mgr, repo = _make_app_db(tmp_path)
    try:
        svc = TrainingDataBackup(repo, Settings(data_root=tmp_path))
        report = svc.backup_and_verify("unit")

        assert report.ok is True, report.to_dict()
        assert report.restored is not None
        r = report.restored
        assert r.ok is True
        assert r.source_counts["dim_question"] == 1
        assert r.restored_counts["dim_question"] == 1
        assert r.restored_counts["fact_order"] == 1
        assert r.restored_counts["app_settings"] == 1
        assert r.mismatches == []
        # 备份产物齐全（schema.sql + load.sql + 表 parquet），且**不是**简单拷贝的 .duckdb
        bdir = Path(report.backup_dir)
        assert (bdir / "schema.sql").exists()
        assert (bdir / "load.sql").exists()
        assert list(bdir.glob("*.parquet"))
        assert report.size_bytes > 0
    finally:
        mgr.close()


def test_restore_verification_actually_reads_restored_data(tmp_path: Path) -> None:
    """恢复验证必须**读得到数据**（不是只判「文件存在」）：恢复库中能查到原始行。"""
    import duckdb

    mgr, repo = _make_app_db(tmp_path)
    try:
        svc = TrainingDataBackup(repo, Settings(data_root=tmp_path))
        report = svc.backup_and_verify("read")
        # 独立再开一次临时库，真正导入并查询内容
        con = duckdb.connect(":memory:")
        try:
            con.execute(f"IMPORT DATABASE '{report.backup_dir}'")
            row = con.execute("SELECT question_id, code FROM dim_question").fetchone()
            assert row[0] == "q_test"
            assert row[1] == "sh.600000"
            order = con.execute("SELECT order_id, shares, price FROM fact_order").fetchone()
            assert order[0] == "o_test"
            assert order[1] == 100
        finally:
            con.close()
    finally:
        mgr.close()


def test_verify_missing_backup_reports_failure(tmp_path: Path) -> None:
    """备份目录不存在 → 恢复验证明确失败（不静默通过）。"""
    mgr, repo = _make_app_db(tmp_path)
    try:
        svc = TrainingDataBackup(repo, Settings(data_root=tmp_path))
        report = svc.verify_restore(tmp_path / "does-not-exist")
        assert report.ok is False
        assert "不存在" in report.message
    finally:
        mgr.close()


def test_backup_report_is_json_serializable(tmp_path: Path) -> None:
    """报告可 JSON 序列化（供 ``/api/admin/backup`` 直接返回）。"""
    mgr, repo = _make_app_db(tmp_path)
    try:
        svc = TrainingDataBackup(repo, Settings(data_root=tmp_path))
        report = svc.backup_and_verify()
        blob = json.dumps(report.to_dict(), ensure_ascii=False, default=str)
        assert '"ok": true' in blob
    finally:
        mgr.close()
