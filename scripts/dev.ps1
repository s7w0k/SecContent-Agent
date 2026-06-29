# ═══════════════════════════════════════════════════════════════
# PR Agent Demo — 开发环境一键启动（Windows PowerShell）
# 用法:
#   .\scripts\dev.ps1              # 启动开发环境
#   .\scripts\dev.ps1 -Build       # 重新构建并启动
#   .\scripts\dev.ps1 -Prod        # 启动生产环境（后台运行）
# ═══════════════════════════════════════════════════════════════
param(
    [switch]$Build,   # 强制重新构建镜像
    [switch]$Prod     # 生产模式（后台运行，无热重载）
)

$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " PR Agent Demo — Dev Mode" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# ── 检查 .env 文件 ──────────────────────────────────────────
if (-not (Test-Path ".env")) {
    Write-Host "[!] .env not found. Copy .env.example:" -ForegroundColor Yellow
    Write-Host "    copy .env.example .env" -ForegroundColor Gray
    Write-Host "    # then edit .env with real values" -ForegroundColor Gray
    exit 1
}

# ── 检查 Docker ─────────────────────────────────────────────
$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    Write-Host "[!] Docker not found. Please install Docker Desktop first." -ForegroundColor Red
    Write-Host "    https://docs.docker.com/desktop/setup/install/windows-install/" -ForegroundColor Gray
    exit 1
}

# ── 检查 Docker Compose 可用性 ──────────────────────────────
$composeCmd = $null
$composeV2 = docker compose version 2>$null
if ($LASTEXITCODE -eq 0) {
    $composeCmd = "docker compose"
    Write-Host "[*] Using Docker Compose v2" -ForegroundColor Green
}
else {
    $composeV1 = docker-compose version 2>$null
    if ($LASTEXITCODE -eq 0) {
        $composeCmd = "docker-compose"
        Write-Host "[*] Using Docker Compose v1" -ForegroundColor Green
    }
    else {
        Write-Host "[!] Docker Compose not found." -ForegroundColor Red
        exit 1
    }
}

# ── 构建标志 ────────────────────────────────────────────────
$buildFlag = ""
if ($Build) {
    $buildFlag = "--build"
    Write-Host "[*] Rebuilding images..." -ForegroundColor Yellow
}

# ── 启动 ────────────────────────────────────────────────────
if ($Prod) {
    Write-Host "[*] Starting production environment (detached)..." -ForegroundColor Green
    Invoke-Expression "$composeCmd up -d $buildFlag"
    Write-Host "[*] Services running in background." -ForegroundColor Green
    Write-Host "    Backend:  http://localhost:${env:BACKEND_PORT:-8000}" -ForegroundColor Gray
    Write-Host "    API Docs: http://localhost:${env:BACKEND_PORT:-8000}/docs" -ForegroundColor Gray
}
else {
    Write-Host "[*] Starting development environment..." -ForegroundColor Green
    Invoke-Expression "$composeCmd -f docker-compose.yml -f docker-compose.dev.yml up $buildFlag"
}

Write-Host "[*] Done." -ForegroundColor Green
