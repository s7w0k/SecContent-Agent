# ═══════════════════════════════════════════════════════════════
# PR Agent Demo — 数据库播种（Windows PowerShell）
# 用法:
#   .\scripts\seed_db.ps1
# ═══════════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " PR Agent Demo — Seed Database" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 检查 Docker Compose
$composeCmd = if (Get-Command docker -ErrorAction SilentlyContinue) { "docker compose" } else { $null }
if (-not $composeCmd) {
    Write-Host "[!] Docker not found." -ForegroundColor Red
    exit 1
}

# 确保 MongoDB 运行
$mongoStatus = & docker compose ps mongodb --format json 2>$null | ConvertFrom-Json
if (-not $mongoStatus -or $mongoStatus.State -ne "running") {
    Write-Host "[!] MongoDB is not running. Start it first:" -ForegroundColor Yellow
    Write-Host "    docker compose up -d mongodb" -ForegroundColor Gray
    exit 1
}

# 导入知识库种子数据
$seedFile = "mongodb\seed\knowledge_base.json"
if (Test-Path $seedFile) {
    Write-Host "[*] Seeding knowledge_base..." -ForegroundColor Green
    Get-Content $seedFile | docker compose exec -T mongodb mongoimport `
        --db pr_agent `
        --collection knowledge_base `
        --jsonArray `
        --upsert `
        --upsertFields key
    Write-Host "[*] knowledge_base seeded." -ForegroundColor Green
}
else {
    Write-Host "[*] No seed data found ($seedFile). Skipping." -ForegroundColor Yellow
}

Write-Host "[*] Done." -ForegroundColor Green
