# 复盘 · A 股历史决策训练平台

> 在真实历史行情中练习「观察 → 判断 → 下单 → 结算 → 复盘」，
> 用**规则化的知识点**而非「对/错」来训练交易决策能力。

本仓库当前处于 **M0（数据底座）** 阶段。

---

## 目录结构（M0 已实现部分）

```
fupan/
├── pyproject.toml            # 后端依赖声明唯一来源 + ruff/mypy/pytest 配置
├── .env.example              # 环境变量样例
├── .gitignore                # 忽略 *.duckdb / *.parquet / .venv 等大文件
├── Makefile                  # init/sync/run/test/lint 命令封装
├── docker-compose.yml        # nginx + api 一键起服务骨架
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI 入口 + APScheduler（Asia/Shanghai 21:00）+ 全局异常处理
│   │   ├── config.py         # pydantic-settings 配置 + TZ 常量
│   │   ├── core/             # db / logging / errors / timeutil
│   │   ├── data/             # models / ddl / repository / visibility / adjuster / universe
│   │   │   └── sync/         # baostock/akshare 客户端、采集、涨跌停、断点续传、健康检查
│   │   └── api/              # admin（同步/健康检查/参数）
│   ├── scripts/              # init_data.py / daily_sync.py（独立 CLI）
│   └── tests/                # test_adjuster.py / test_universe.py
└── data/                     # 数据目录（大文件 git 忽略）
```

---

## 六条红线（结构性保证，验收用）

| 红线 | 结构性落点 |
|---|---|
| ① 前视偏差 | `data/visibility.py::VisibilityGuard.mask()` 是所有 `Repository` 结果的必经出口 |
| ② 复权三口径 | `data/adjuster.py::PriceAdjuster` 唯一算法源；**存储恒为「不复权 OHLCV + `adj_factor`」** |
| ③ 成交可行性 | `engine/rules.py::TradeRuleEngine`（M1） |
| ④ 结算快照冻结 | `engine/snapshot.py::SnapshotFreezer`（M1） |
| ⑤ 交易引擎共用 | `engine/trade_engine.py::TradeEngine`（M1，唯一成交实现） |
| ⑥ 幸存者偏差 | `data/universe.py::UniverseProvider.as_of(date)` 基于 `list_date/delist_date` |

---

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
make venv
make install

# 2. 配置环境变量
cp .env.example .env      # 按需修改 FUPAN_DATA_ROOT 等

# 3. 首次全量初始化（独立 CLI，建议 API 未启动时执行）
make init

# 4. 启动 API
make run                  # http://localhost:8000/api/health

# 5. 手动增量同步 / 健康检查
make sync
make health
```

> **`akshare` 为可选依赖**：它是「按需补充」数据源（绝不做定时任务），
> 且其依赖 `jsonpath` 仅以 sdist 发布，在部分环境无法安装。
> 因此归入独立 extra，核心依赖（Baostock）不受影响。如需：
> `pip install -e ".[akshare]"`（或 `.[dev,akshare]`）。缺失时
> `AkshareClient` 会惰性导入并抛出明确的 `DataUnavailable`。

## 关键约定

- **时区**：全系统 `Asia/Shanghai`（`config.TZ`），调度器显式传时区。
- **存储**：`market.duckdb`（可重下，P2）+ `app.duckdb`（训练数据，P0）分离；
  写操作一律经 `DuckDBManager.acquire_write()` 拿 `filelock`，拿不到锁**明确报错**。
- **复权**：任何地方不得自行实现复权，一律走 `PriceAdjuster`。
- **SQL**：全部参数化查询，禁止字符串拼接。
- **价格精度**：涨跌停价/成交价四舍五入到分（half-up）。
