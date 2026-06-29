#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# PR Agent Demo — 数据库播种
# 用法: bash scripts/seed_db.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=========================================="
echo " PR Agent Demo — Seed Database"
echo "=========================================="

# 确保 MongoDB 在运行
if ! docker compose ps mongodb | grep -q "Up"; then
    echo "[!] MongoDB is not running. Start it first:"
    echo "    docker compose up -d mongodb"
    exit 1
fi

# 导入知识库种子数据（如果存在）
if [ -f mongodb/seed/knowledge_base.json ]; then
    echo "[*] Seeding knowledge_base..."
    docker compose exec -T mongodb mongoimport \
        --db pr_agent \
        --collection knowledge_base \
        --file /docker-entrypoint-initdb.d/seed/knowledge_base.json \
        --jsonArray \
        --upsert \
        --upsertFields key
    echo "[*] knowledge_base seeded."
else
    echo "[*] No seed data found (mongodb/seed/knowledge_base.json). Skipping."
fi

echo "[*] Done."
