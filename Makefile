.PHONY: test test-unit test-fast lint format web cli clean help

help:
	@echo "TradingAgents-AStock 开发指令集:"
	@echo "  make test        - 运行全部测试套件"
	@echo "  make test-unit   - 运行单元测试"
	@echo "  make lint        - 运行 ruff 语法与代码检查"
	@echo "  make format      - 运行 ruff 自动格式化"
	@echo "  make web         - 启动 Streamlit Web 投研看板"
	@echo "  make cli         - 启动 CLI 交互式终端"
	@echo "  make clean       - 清理 Python 字节码与构建缓存"

test:
	uv run pytest

test-unit:
	uv run pytest -m "unit"

lint:
	uv run ruff check .

format:
	uv run ruff format .

web:
	uv run tradingagents-web

cli:
	uv run tradingagents

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
