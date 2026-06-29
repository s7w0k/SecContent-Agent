@echo off
REM ═══════════════════════════════════════════════════════════════
REM PR Agent Demo — 开发环境一键启动（Windows CMD）
REM 用法:
REM   scripts\dev.bat             启动开发环境
REM   scripts\dev.bat --build     重新构建并启动
REM   scripts\dev.bat --prod      生产模式（后台运行）
REM ═══════════════════════════════════════════════════════════════

setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo ========================================
echo  PR Agent Demo — Dev Mode
echo ========================================

REM 检查 .env
if not exist .env (
    echo [!] .env not found. Copy .env.example:
    echo     copy .env.example .env
    echo     # then edit .env with real values
    exit /b 1
)

REM 检查 Docker
where docker >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [!] Docker not found. Please install Docker Desktop first.
    echo     https://docs.docker.com/desktop/setup/install/windows-install/
    exit /b 1
)

REM 检测 Compose 命令
set COMPOSE_CMD=
docker compose version >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set COMPOSE_CMD=docker compose
    echo [*] Using Docker Compose v2
) else (
    docker-compose version >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        set COMPOSE_CMD=docker-compose
        echo [*] Using Docker Compose v1
    ) else (
        echo [!] Docker Compose not found.
        exit /b 1
    )
)

REM 参数
set BUILD_FLAG=
if "%1"=="--build" set BUILD_FLAG=--build
if "%1"=="-Build" set BUILD_FLAG=--build
if "%BUILD_FLAG%"=="--build" echo [*] Rebuilding images...

REM 启动
if "%1"=="--prod" goto prod
if "%1"=="-Prod" goto prod

echo [*] Starting development environment...
%COMPOSE_CMD% -f docker-compose.yml -f docker-compose.dev.yml up %BUILD_FLAG%
goto end

:prod
echo [*] Starting production environment (detached)...
%COMPOSE_CMD% up -d %BUILD_FLAG%
echo [*] Services running in background.
echo     Backend:  http://localhost:8000
echo     API Docs: http://localhost:8000/docs

:end
echo [*] Done.
endlocal
