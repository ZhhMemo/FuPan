"""全局配置（pydantic-settings，单一配置源）。

约定：
- 环境变量前缀 ``FUPAN_``（例如 ``FUPAN_DATA_ROOT`` 对应 ``data_root``）。
- 时区常量 ``TZ`` 集中定义，全系统使用 ``Asia/Shanghai``。
- 费率参数在此给出**默认值**（唯一实现在 ``engine.cost.CostModel``，M1），
  本模块只负责承载可配置值与版本号，禁止在别处硬编码费率。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── 时区（全系统唯一来源）──
TZ_NAME: str = "Asia/Shanghai"
TZ: ZoneInfo = ZoneInfo(TZ_NAME)

# ── 默认数据目录（设计约定：~/workspace/fupan）──
DEFAULT_DATA_ROOT: Path = Path.home() / "workspace" / "fupan"


class Settings(BaseSettings):
    """应用配置对象。

    所有字段均带默认值，可通过环境变量或 ``.env`` 覆盖。
    """

    model_config = SettingsConfigDict(
        env_prefix="FUPAN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 应用 ──
    app_name: str = "复盘"
    debug: bool = False

    # ── 路径 ──
    data_root: Path = Field(default=DEFAULT_DATA_ROOT)
    market_db_name: str = "market.duckdb"
    app_db_name: str = "app.duckdb"
    market_readonly: bool = False  # API 只读降级开关（首次全量初始化并发时）
    lock_timeout: float = 0.0  # filelock 获取写锁的超时（0=立即失败）

    # ── 日志 ──
    log_level: str = "INFO"
    log_json: bool = False

    # ── 费率（默认值；唯一实现 CostModel，M1）──
    fee_version: str = "v1"
    commission_rate: float = 2.5e-4  # 佣金 万2.5（双向）
    min_commission: float = 5.0  # 最低 5 元
    stamp_tax_rate: float = 5e-4  # 印花税 0.05%（卖出）
    transfer_fee_rate: float = 1e-5  # 过户费 0.001%（双向）
    regulation_fee_rate: float = 0.0  # 规费（经手费+证管费）：设计口径「含在全佣内」，单列仅用于费率族快照留痕/审计
    slippage_rate: float = 5e-4  # 滑点 0.05%（双向）

    # ── 成交口径（FR-4.8：成交价可配置，默认 T+1 开盘价）──
    # 取值：``t1_open``（默认，最接近真实散户）/ ``t1_close`` / ``t0_close``。
    fill_price_source: str = "t1_open"
    # 「开盘即封板」保守规则（`00` §7 规则#4）：``开盘价 == 涨停价`` → 视为无法买入（可配置开关）。
    open_seal_no_buy: bool = True

    # ── 定时同步 ──
    sync_hour: int = 21
    sync_minute: int = 0

    # ── 结算 ──
    settle_window: int = 20  # 决策点后观察 N 个交易日再结算（默认 20）

    # ── 训练数据备份（FR-8.7，P0：app.duckdb 不可再生）──
    backup_dir: str = "backups"  # 相对 data_root 的备份目录名（禁止入库）
    backup_keep: int = 14  # 保留最近 N 份备份（滚动清理）

    # ── 数据源 ──
    full_history_start: str = "1990-12-19"
    baostock_reconnect_retries: int = 3
    default_index_codes: list[str] = Field(default_factory=lambda: ["sh.000300", "sh.000001", "sz.399006"])

    # ── 认证（M1）──
    secret_key: str = "change-me-in-production"
    admin_username: str = "admin"  # 单账号引导（生产请改）
    admin_password: str = "fupan@2024"  # 初始密码（首启引导；生产务必修改/经环境变量覆盖）
    session_ttl_hours: int = 72  # 会话有效期（小时）
    login_max_attempts: int = 5  # 连续失败阈值
    login_lock_seconds: int = 300  # 触发锁定后的冷却秒数
    force_https: bool = False  # 生产置 True：强制 HTTPS 跳转 + HSTS
    security_headers: bool = True  # 是否注入 CSP 等安全响应头（N3：CSRF/CSP 生效）
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
        ]
    )

    # ── LLM（M2）──
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    # ─────────────── 派生路径 ───────────────

    @property
    def market_db(self) -> Path:
        return self.data_root / self.market_db_name

    @property
    def app_db(self) -> Path:
        return self.data_root / self.app_db_name

    @property
    def raw_dir(self) -> Path:
        return self.data_root / "raw"

    @property
    def clean_dir(self) -> Path:
        return self.data_root / "clean"

    @property
    def app_parquet_dir(self) -> Path:
        return self.data_root / "app"

    @property
    def backup_path(self) -> Path:
        """训练数据备份目录（FR-8.7；禁止入库）。"""
        return self.data_root / self.backup_dir

    @property
    def market_lock(self) -> Path:
        return self.data_root / "market.duckdb.write.lock"

    @property
    def app_lock(self) -> Path:
        return self.data_root / "app.duckdb.write.lock"

    def ensure_dirs(self) -> None:
        """创建所有必需的目录（幂等）。"""
        for d in (self.data_root, self.raw_dir, self.clean_dir, self.app_parquet_dir, self.backup_path):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程级单例配置。"""
    return Settings()


settings: Settings = get_settings()
