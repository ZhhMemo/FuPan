# 复盘 · 常用命令封装
PY := .venv/bin/python
PIP := .venv/bin/pip
BIN := .venv/bin

.PHONY: help venv install init sync run test lint fmt health backup clean

help:
	@echo "复盘 · 可用命令："
	@echo "  make venv     创建项目内虚拟环境 (.venv)"
	@echo "  make install  安装依赖 (pip install -e .)"
	@echo "  make init     首次全量数据初始化（独立 CLI，API 未启动时执行）"
	@echo "  make sync     手动触发一次增量同步（独立 CLI）"
	@echo "  make run      启动 FastAPI (uvicorn, 端口 8000)"
	@echo "  make test     运行 pytest"
	@echo "  make lint     运行 ruff + mypy"
	@echo "  make fmt      ruff 自动格式化"
	@echo "  make health   跑一次数据健康检查"
	@echo "  make backup   备份训练数据 app.duckdb 并做恢复验证（FR-8.7）"

venv:
	$(PYTHON) -m venv .venv || python3 -m venv .venv

install:
	$(PIP) install -e ".[dev]"

init:
	cd backend && ../$(PY) -m scripts.init_data

sync:
	cd backend && ../$(PY) -m scripts.daily_sync

run:
	cd backend && ../$(BIN)/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test:
	$(PY) -m pytest

lint:
	$(BIN)/ruff check backend
	$(BIN)/mypy backend

fmt:
	$(BIN)/ruff format backend
	$(BIN)/ruff check --fix backend

health:
	cd backend && ../$(PY) -c "from app.data.sync.health_check import run_health_check_cli; run_health_check_cli()"

backup:
	cd backend && ../$(PY) -m scripts.backup

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
