"""全部建表 DDL（schema 单一真相来源）。

- ``MARKET_DDL``：``market.duckdb``（clean 层，可重下，P2）
- ``APP_DDL``：``app.duckdb``（app 层，最珍贵，P0）

存储纪律（红线②）：行情表只存**不复权 OHLCV + adj_factor**，
任何复权口径由 ``PriceAdjuster`` 现算，绝不落库。
"""

from __future__ import annotations

import duckdb

# ══════════════════════════════════════════════════════════════
# market.duckdb · clean 层
# ══════════════════════════════════════════════════════════════
MARKET_DDL: list[str] = [
    # 证券基础信息：时点标的池的前提（红线⑥）
    """
    CREATE TABLE IF NOT EXISTS dim_stock (
      code VARCHAR PRIMARY KEY,
      name VARCHAR,
      list_date DATE,
      delist_date DATE,
      board VARCHAR,
      industry VARCHAR,
      is_st BOOLEAN,
      updated_at TIMESTAMP
    )
    """,
    # 交易日历
    """
    CREATE TABLE IF NOT EXISTS dim_calendar (
      date DATE PRIMARY KEY,
      is_trading_day BOOLEAN
    )
    """,
    # 日线：只存不复权 + 复权因子（红线②）
    """
    CREATE TABLE IF NOT EXISTS fact_daily (
      code VARCHAR,
      date DATE,
      open DOUBLE,
      high DOUBLE,
      low DOUBLE,
      close DOUBLE,
      volume BIGINT,
      amount DOUBLE,
      adj_factor DOUBLE,
      is_trade BOOLEAN,
      PRIMARY KEY (code, date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_daily_code_date ON fact_daily(code, date)",
    # 分钟线：advance ≤300ms 的前提（按 (code,dt) 索引）
    """
    CREATE TABLE IF NOT EXISTS fact_minute (
      code VARCHAR,
      dt TIMESTAMP,
      open DOUBLE,
      high DOUBLE,
      low DOUBLE,
      close DOUBLE,
      volume BIGINT,
      amount DOUBLE,
      PRIMARY KEY (code, dt)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_min_code_dt ON fact_minute(code, dt)",
    # 全市场当前格快照（≤1s 的预聚合，M5）
    """
    CREATE TABLE IF NOT EXISTS fact_minute_snapshot (
      dt TIMESTAMP,
      code VARCHAR,
      close DOUBLE,
      pct_chg DOUBLE,
      amount DOUBLE,
      turnover DOUBLE,
      PRIMARY KEY (dt, code)
    )
    """,
    # 涨跌停价（分板块预计算）
    """
    CREATE TABLE IF NOT EXISTS dim_limit (
      code VARCHAR,
      date DATE,
      limit_up DOUBLE,
      limit_down DOUBLE,
      PRIMARY KEY (code, date)
    )
    """,
    # 指数日线
    """
    CREATE TABLE IF NOT EXISTS dim_index_daily (
      index_code VARCHAR,
      date DATE,
      close DOUBLE,
      PRIMARY KEY (index_code, date)
    )
    """,
    # 分红送配
    """
    CREATE TABLE IF NOT EXISTS dim_dividend (
      code VARCHAR,
      ex_date DATE,
      cash_per_share DOUBLE,
      share_ratio DOUBLE,
      rights_ratio DOUBLE,
      PRIMARY KEY (code, ex_date)
    )
    """,
    # 知识点（M3 使用）
    """
    CREATE TABLE IF NOT EXISTS dim_knowledge (
      kb_id VARCHAR PRIMARY KEY,
      name VARCHAR,
      alias VARCHAR,
      category VARCHAR,
      sub_category VARCHAR,
      definition VARCHAR,
      detect_rule VARCHAR,
      detect_type VARCHAR,
      window_size INT,
      min_confirm_bars INT,
      base_weight DOUBLE,
      signal_direction VARCHAR,
      position_hint VARCHAR,
      meaning VARCHAR,
      pitfalls VARCHAR,
      diagram_ref VARCHAR,
      related VARCHAR,
      sample_min INT DEFAULT 100,
      version INT,
      enabled BOOLEAN
    )
    """,
    # 命中预计算（约 3000 万行）
    """
    CREATE TABLE IF NOT EXISTS fact_knowledge_match (
      match_id VARCHAR PRIMARY KEY,
      kb_id VARCHAR,
      code VARCHAR,
      start_date DATE,
      end_date DATE,
      confidence DOUBLE,
      severity_score DOUBLE,
      detail VARCHAR
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_match_code ON fact_knowledge_match(code, start_date)",
    "CREATE INDEX IF NOT EXISTS idx_match_kb ON fact_knowledge_match(kb_id)",
]

# ══════════════════════════════════════════════════════════════
# app.duckdb · app 层（最珍贵，P0）
# ══════════════════════════════════════════════════════════════
APP_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS dim_question (
      question_id VARCHAR PRIMARY KEY,
      code VARCHAR,
      start_date DATE,
      end_date DATE,
      pattern_tag VARCHAR,
      position_type VARCHAR,
      position_state_tier VARCHAR,
      initial_position VARCHAR,
      initial_cash DOUBLE,
      source VARCHAR,
      visible_until DATE,
      settle_window INT,
      settle_params VARCHAR,
      decision_points VARCHAR,
      question_mode VARCHAR,
      title VARCHAR,
      description VARCHAR,
      key_points VARCHAR,
      difficulty_prior DOUBLE,
      difficulty_post DOUBLE,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_order (
      order_id VARCHAR PRIMARY KEY,
      question_id VARCHAR,
      session_id VARCHAR,
      dt TIMESTAMP,
      side VARCHAR,
      shares INT,
      price DOUBLE,
      params_snapshot VARCHAR,
      knowledge_mode VARCHAR,
      viewed_knowledge BOOLEAN,
      judge_verdict VARCHAR,
      judge_advice VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_settlement (
      order_id VARCHAR PRIMARY KEY,
      account_return DOUBLE,
      stock_return DOUBLE,
      benchmark_return DOUBLE,
      alpha DOUBLE,
      opp_cost DOUBLE,
      max_dd DOUBLE,
      hold_all_return DOUBLE,
      fee_detail VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_session (
      session_id VARCHAR PRIMARY KEY,
      mode VARCHAR,
      code VARCHAR,
      start_date DATE,
      end_date DATE,
      granularity INT,
      current_dt TIMESTAMP,
      cash DOUBLE,
      status VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_position (
      session_id VARCHAR,
      code VARCHAR,
      shares INT,
      available_shares INT,
      avg_cost DOUBLE,
      PRIMARY KEY (session_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_trade (
      trade_id VARCHAR PRIMARY KEY,
      session_id VARCHAR,
      dt TIMESTAMP,
      code VARCHAR,
      side VARCHAR,
      price DOUBLE,
      shares INT,
      fee_detail VARCHAR,
      cash_after DOUBLE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_nav (
      session_id VARCHAR,
      date DATE,
      total_asset DOUBLE,
      cash DOUBLE,
      market_value DOUBLE,
      PRIMARY KEY (session_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_user (
      username VARCHAR PRIMARY KEY,
      password_hash VARCHAR,
      totp_secret VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_session (
      token VARCHAR PRIMARY KEY,
      username VARCHAR,
      expires_at TIMESTAMP,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_settings (
      key VARCHAR PRIMARY KEY,
      value VARCHAR,
      updated_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_failure (
      batch_date DATE,
      code VARCHAR,
      reason VARCHAR,
      created_at TIMESTAMP,
      PRIMARY KEY (batch_date, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_checkpoint (
      code VARCHAR PRIMARY KEY,
      last_date DATE,
      updated_at TIMESTAMP
    )
    """,
]


def execute_script(con: duckdb.DuckDBPyConnection, statements: list[str]) -> int:
    """逐条执行 DDL 语句，返回成功执行的语句数。

    Args:
        con: DuckDB 连接。
        statements: DDL 语句列表（每条为单条语句）。

    Returns:
        成功执行的语句条数。
    """
    count = 0
    for stmt in statements:
        sql = stmt.strip().rstrip(";")
        if not sql:
            continue
        con.execute(sql)
        count += 1
    return count


def all_market_tables() -> list[str]:
    """market 库全部表名。"""
    return [
        "dim_stock",
        "dim_calendar",
        "fact_daily",
        "fact_minute",
        "fact_minute_snapshot",
        "dim_limit",
        "dim_index_daily",
        "dim_dividend",
        "dim_knowledge",
        "fact_knowledge_match",
    ]


def all_app_tables() -> list[str]:
    """app 库全部表名。"""
    return [
        "dim_question",
        "fact_order",
        "fact_settlement",
        "sim_session",
        "sim_position",
        "sim_trade",
        "sim_nav",
        "app_user",
        "app_session",
        "app_settings",
        "sync_failure",
        "sync_checkpoint",
    ]


__all__ = ["MARKET_DDL", "APP_DDL", "execute_script", "all_market_tables", "all_app_tables"]
