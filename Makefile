# ═══════════════════════════════════════════════════════════════
# PR Agent Demo — Makefile（跨平台开发命令）
# 用法:
#   make help        显示所有可用命令
#   make dev         启动开发环境（含热重载）
#   make up          启动生产环境（后台运行）
#   make down        停止并清理所有服务
#   make build       构建全部 Docker 镜像
#   make test        运行全部测试
#   make lint        代码检查
#   make ci          本地 CI 模拟（Lint + Test）
#   make ci-all      本地 CI 模拟（全流程：Lint + Test + Build + Security）
#   make format      自动格式化
#   make clean       清理构建产物
# ═══════════════════════════════════════════════════════════════

.PHONY: help dev up down build test lint ci ci-all format clean

# ── 自动检测 compose 命令 ───────────────────────────────────
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

dev: ## 启动开发环境（含热重载）
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml up --build

up: ## 启动生产环境（后台运行）
	$(COMPOSE) up -d --build

down: ## 停止所有服务并清理卷
	$(COMPOSE) down -v

build: ## 构建全部 Docker 镜像
	$(COMPOSE) build

logs: ## 查看全部服务日志
	$(COMPOSE) logs -f

test: ## 运行全部测试（pytest）
	pytest tests/ --cov=services --cov-report=term-missing -v

lint: ## 代码检查（ruff + biome）
	ruff check services/ tests/
	ruff format --check services/ tests/
	-cd frontend && npx biome check src/ --max-diagnostics=50

format: ## 自动格式化
	ruff format services/ tests/
	cd frontend && npx biome format --write src/

# ── CI 模拟 ──────────────────────────────────────────────────
ci: ## 本地 CI 模拟 — 快速（Lint + Test）
	@echo "=== CI Quick: Lint + Test ==="
	ruff check services/ tests/
	ruff format --check services/ tests/
	python scripts/check_wiki_default.py  # CI Hard Gate 2：禁止生产默认 legacy
	python scripts/check_wiki_scoring_isolation.py  # CI Hard Gate 3：Wiki 评分严格隔离 (PR-2)
	python scripts/check_multiagent_architecture.py  # CI Hard Gate 4：Multi-Agent 分层架构不变量
	python scripts/check_agent_cutover.py  # CI Hard Gate 5：Cutover 接缝不变量（新架构生产接管）
	pytest tests/ --cov=services --cov-report=term-missing -v --tb=short

ci-all: lint test ## 本地 CI 模拟 — 全流程（Lint + Test + Security）
	@echo "=== CI Full: Security Scan ==="
	-bandit -r services/ -f json -o bandit-report.json --exit-zero
	@echo "=== CI Full: Docker Build ==="
	$(COMPOSE) build

ci-security: ## 本地安全扫描（bandit + pip-audit）
	@echo "=== Security: bandit ==="
	bandit -r services/ -f json -o bandit-report.json --exit-zero
	@echo "=== Security: pip-audit ==="
	-pip-audit

# ── 清理 ─────────────────────────────────────────────────────
clean: ## 清理构建产物
	$(COMPOSE) down -v --rmi local 2>/dev/null || true
	-find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
	-find . -type d -name node_modules -exec rm -rf {} + 2>/dev/null
	-find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null
	-find . -type d -name .vitest -exec rm -rf {} + 2>/dev/null
	-rm -f bandit-report.json pip-audit-report.json coverage.xml
