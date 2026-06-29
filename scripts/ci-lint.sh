#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# PR Agent Demo — 本地 CI 模拟脚本（Linux / macOS）
# 用法:
#   bash scripts/ci-lint.sh              # Lint + Test（快速）
#   bash scripts/ci-lint.sh --all        # Lint + Test + Build + Security
#   bash scripts/ci-lint.sh --lint       # 仅 Lint
#   bash scripts/ci-lint.sh --test       # 仅 Test
#   bash scripts/ci-lint.sh --security   # 仅 Security
# ═══════════════════════════════════════════════════════════════
set -euo pipefail
cd "$(dirname "$0")/.."

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

MODE="${1:---quick}"
PASSED=0
FAILED=0

step_pass() { echo -e "  ${GREEN}✓${NC} $1"; PASSED=$((PASSED + 1)); }
step_fail() { echo -e "  ${RED}✗${NC} $1"; FAILED=$((FAILED + 1)); }

# ── Lint: Python (ruff) ──────────────────────────────────────
lint_python() {
    echo -e "${CYAN}[Lint] Python — ruff${NC}"

    if command -v ruff &> /dev/null; then
        ruff check services/ tests/ && step_pass "ruff check" || step_fail "ruff check"
        ruff format --check services/ tests/ && step_pass "ruff format" || step_fail "ruff format"
    else
        echo -e "  ${YELLOW}⚠${NC} ruff not installed. Install via: pip install ruff"
        step_fail "ruff (not found)"
    fi
}

# ── Lint: Frontend (biome) ───────────────────────────────────
lint_frontend() {
    echo -e "${CYAN}[Lint] Frontend — biome${NC}"

    if [ -f frontend/node_modules/.bin/biome ]; then
        cd frontend
        npx biome check src/ --max-diagnostics=50 && cd .. && step_pass "biome check" || { cd ..; step_fail "biome check"; }
    else
        echo -e "  ${YELLOW}⚠${NC} biome not installed. Run: cd frontend && npm ci"
        step_fail "biome (not found)"
    fi
}

# ── Lint: Docker (hadolint) ──────────────────────────────────
lint_docker() {
    echo -e "${CYAN}[Lint] Docker — hadolint${NC}"

    if command -v hadolint &> /dev/null; then
        local ok=true
        for df in services/mcp_wewe/Dockerfile services/mcp_crawl/Dockerfile services/backend/Dockerfile frontend/Dockerfile; do
            if [ -f "$df" ]; then
                hadolint "$df" 2>&1 || ok=false
            fi
        done
        $ok && step_pass "hadolint" || step_fail "hadolint"
    else
        echo -e "  ${YELLOW}⚠${NC} hadolint not installed. Install via: docker pull hadolint/hadolint"
        step_fail "hadolint (not found)"
    fi
}

# ── Test: Python (pytest) ────────────────────────────────────
test_python() {
    echo -e "${CYAN}[Test] Python — pytest${NC}"

    if command -v pytest &> /dev/null; then
        pytest tests/ \
            --cov=services \
            --cov-report=term-missing \
            -v --tb=short \
            --timeout=60 \
            && step_pass "pytest" \
            || step_fail "pytest"
    else
        echo -e "  ${YELLOW}⚠${NC} pytest not installed. Install via: pip install pytest pytest-asyncio pytest-cov"
        step_fail "pytest (not found)"
    fi
}

# ── Test: Frontend (vitest) ──────────────────────────────────
test_frontend() {
    echo -e "${CYAN}[Test] Frontend — vitest${NC}"

    if [ -f frontend/node_modules/.bin/vitest ]; then
        cd frontend
        npx vitest run --reporter=verbose && cd .. && step_pass "vitest" || { cd ..; step_fail "vitest"; }
    else
        echo -e "  ${YELLOW}⚠${NC} vitest not installed. Run: cd frontend && npm ci"
        step_fail "vitest (not found)"
    fi
}

# ── Build ────────────────────────────────────────────────────
build() {
    echo -e "${CYAN}[Build] Docker Compose${NC}"

    if command -v docker &> /dev/null; then
        docker compose build --no-cache 2>&1 && step_pass "docker compose build" || step_fail "docker compose build"
    else
        echo -e "  ${YELLOW}⚠${NC} Docker not found"
        step_fail "docker build (not found)"
    fi
}

# ── Security ─────────────────────────────────────────────────
security_scan() {
    echo -e "${CYAN}[Security] bandit + pip-audit${NC}"

    if command -v bandit &> /dev/null; then
        bandit -r services/ -f json -o bandit-report.json --exit-zero 2>&1
        local high_critical=$(python -c "
import json
with open('bandit-report.json') as f:
    report = json.load(f)
hc = [r for r in report.get('results',[]) if r.get('issue_severity') in ('HIGH','CRITICAL')]
print(len(hc))
" 2>/dev/null || echo "0")
        if [ "${high_critical:-0}" -eq 0 ]; then
            step_pass "bandit (no HIGH/CRITICAL)"
        else
            step_fail "bandit ($high_critical HIGH/CRITICAL issues)"
        fi
    else
        echo -e "  ${YELLOW}⚠${NC} bandit not installed. Install via: pip install bandit"
        step_fail "bandit (not found)"
    fi

    if command -v pip-audit &> /dev/null; then
        pip-audit 2>&1 && step_pass "pip-audit (no vulns)" || step_fail "pip-audit (vulnerabilities found)"
    else
        echo -e "  ${YELLOW}⚠${NC} pip-audit not installed. Install via: pip install pip-audit"
        step_fail "pip-audit (not found)"
    fi
}

# ── Main ─────────────────────────────────────────────────────
echo ""
echo "========================================"
echo " PR Agent Demo — CI Simulation"
echo " Mode: $MODE"
echo "========================================"
echo ""

case "$MODE" in
    --quick)
        lint_python
        lint_frontend
        test_python
        ;;
    --all)
        lint_python
        lint_frontend
        lint_docker
        test_python
        test_frontend
        build
        security_scan
        ;;
    --lint)
        lint_python
        lint_frontend
        lint_docker
        ;;
    --test)
        test_python
        test_frontend
        ;;
    --security)
        security_scan
        ;;
    *)
        echo "Usage: bash scripts/ci-lint.sh [--quick|--all|--lint|--test|--security]"
        exit 1
        ;;
esac

echo ""
echo "========================================"
echo -e " Results: ${GREEN}${PASSED} passed${NC}, ${RED}${FAILED} failed${NC}"
echo "========================================"

if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
