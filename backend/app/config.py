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
    slippage_rate: float = 5e-4  # 滑点 0.05%（双向）

    # ── 定时同步 ──
    sync_hour: int = 21
    sync_minute: int = 0

    # ── 数据源 ──
    full_history_start: str = "1990-12-19"
    baostock_reconnect_retries: int = 3
    default_index_codes: list[str] = Field(default_factory=lambda: ["sh.000300", "sh.000001", "sz.399006"])

    # ── 认证（M1）──
    secret_key: str = "change-me-in-production"

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
    def market_lock(self) -> Path:
        return self.data_root / "market.duckdb.write.lock"

    @property
    def app_lock(self) -> Path:
        return self.data_root / "app.duckdb.write.lock"

    def ensure_dirs(self) -> None:
        """创建所有必需的目录（幂等）。"""
        for d in (self.data_root, self.raw_dir, self.clean_dir, self.app_parquet_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程级单例配置。"""
    return Settings()


settings: Settings = get_settings()
