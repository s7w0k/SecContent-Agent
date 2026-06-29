#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# PR Agent Demo — 开发环境一键启动
# 用法: bash scripts/dev.sh [--build]
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=========================================="
echo " PR Agent Demo — Dev Mode"
echo "=========================================="

# 检查 .env 文件
if [ ! -f .env ]; then
    echo "[!] .env not found. Copy .env.example:"
    echo "    cp .env.example .env"
    echo "    # then edit .env with real values"
    exit 1
fi

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo "[!] Docker not found. Please install Docker first."
    exit 1
fi

BUILD_FLAG=""
if [[ "${1:-}" == "--build" ]]; then
    BUILD_FLAG="--build"
    echo "[*] Rebuilding images..."
fi

echo "[*] Starting development services..."
docker compose -f docker-compose.yml -f docker-compose.dev.yml up $BUILD_FLAG

echo "[*] Dev environment stopped."
