# 复盘 · A 股历史决策训练平台

> 在真实历史行情中练习「观察 → 判断 → 下单 → 结算 → 复盘」，
> 用**规则化的知识点**而非「对/错」来训练交易决策能力。

本仓库当前处于 **M1（单题闭环）** 阶段：**数据底座（M0） + 认证/交易/结算/判卷（M1）** 已交付。

---

## 目录结构（M0 + M1 已实现部分）

```
fupan/
├── pyproject.toml            # 后端依赖声明唯一来源 + ruff/mypy/pytest 配置
├── .env.example              # 环境变量样例
├── .gitignore                # 忽略 *.duckdb / *.parquet / .venv / node_modules 等
├── Makefile                  # init/sync/run/test/lint 命令封装
├── docker-compose.yml        # nginx + api 一键起服务骨架
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI 入口 + APScheduler + 安全中间件（CSP/CSRF/HTTPS）+ 全局异常
│   │   ├── config.py         # pydantic-settings 配置 + TZ 常量 + 费率/认证/结算参数
│   │   ├── core/             # db / logging / errors / timeutil / security（认证：密码慢哈希+会话+限速）
│   │   ├── data/             # models / ddl / repository / visibility / adjuster / universe
│   │   │   └── sync/         # baostock/akshare 客户端、采集、涨跌停、断点续传、健康检查
│   │   ├── engine/           # cost / rules / position / trade_engine / snapshot / settle / advice
│   │   └── api/              # auth / question / trade / settle / admin / deps / schemas
│   ├── scripts/              # init_data.py / daily_sync.py（独立 CLI）
│   └── tests/                # 红线单测：visibility / adjuster / rules / trade_engine / settle / advice / universe / limit
├── frontend/                 # Vite + React + TS + Tailwind + MUI（登录/作答/结算 + 判卷交互）
│   └── src/                  # main / App / api / store / pages / components / utils
└── data/                     # 数据目录（大文件 git 忽略）
```

---

## 六条红线（结构性保证，验收用）

| 红线 | 结构性落点 |
|---|---|
| ① 前视偏差 | `data/visibility.py::VisibilityGuard.mask()` 是所有 `Repository` 结果的必经出口 |
| ② 复权三口径 | `data/adjuster.py::PriceAdjuster` 唯一算法源；**存储恒为「不复权 OHLCV + `adj_factor`」** |
| ③ 成交可行性 | `engine/rules.py::TradeRuleEngine`（T+1 / 一字板 / 停牌 / 板块涨跌停 / 顺延） |
| ④ 结算快照冻结 | `engine/snapshot.py::SnapshotFreezer`（费率版本 + 复权因子 + 成交价一次性冻结） |
| ⑤ 交易引擎共用 | `engine/trade_engine.py::TradeEngine`（判断/回放/判卷**唯一成交实现**） |
| ⑥ 幸存者偏差 | `data/universe.py::UniverseProvider.as_of(date)` 基于 `list_date/delist_date` |

> M1 补充：涨跌停**按制度分段**（`models.limit_pct_of`，创业板 2020-08-24 起 ±20%）；
> 退市整理期按 ±10% **近似**处理（详见 `docs/07`）。

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

---

## M1 单题闭环 · 快速开始

```bash
# 后端（默认账号在首启时自动引导；生产请经环境变量覆盖 FUPAN_ADMIN_PASSWORD）
make run                  # http://localhost:8000  （/docs 查看接口）

# 前端
cd frontend && npm install && npm run dev   # http://localhost:5173

# 登录（默认引导账号，务必尽快修改）
#   用户名：admin    密码：fupan@2024
```

M1 流程：**登录 → 手工出题（空仓型/持仓型，可选判卷模式）→ 观察 K 线（严格截断于决策点）
→ 下单（落快照）→ 结算（账户视角，独立延后接口，不判对错）**。
判卷模式额外展示「模型建议（MA20 三档）→ 用户评判（采纳/未采纳）→ 结算」。

| 模块 | 接口 |
|---|---|
| 认证 | `POST /api/auth/login` · `POST /api/auth/logout` · `GET /api/auth/me` |
| 出题/观察 | `POST /api/questions/custom` · `GET /api/questions/{qid}` · `GET /api/questions/{qid}/kline` · `GET /api/questions/{qid}/advice` · `GET /api/stocks` |
| 下单/账户 | `POST /api/trade/order` · `GET /api/trade/account/{qid}` |
| 结算 | `POST /api/settle/{qid}`（**仅下单后可调**） |

> 安全（N3）：单账号 + 密码慢哈希（argon2id）+ 登录失败限速/锁定 + 会话管理；
> 全站 HTTPS（`FUPAN_FORCE_HTTPS`）+ CSP/CSRF 就绪；TOTP 字段**保留但不启用**。

## 关键约定

- **时区**：全系统 `Asia/Shanghai`（`config.TZ`），调度器显式传时区。
- **存储**：`market.duckdb`（可重下，P2）+ `app.duckdb`（训练数据，P0）分离；
  写操作一律经 `DuckDBManager.acquire_write()` 拿 `filelock`，拿不到锁**明确报错**。
- **复权**：任何地方不得自行实现复权，一律走 `PriceAdjuster`。
- **SQL**：全部参数化查询，禁止字符串拼接。
- **价格精度**：涨跌停价/成交价四舍五入到分（half-up）。
