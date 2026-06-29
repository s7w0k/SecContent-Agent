@echo off
REM ═══════════════════════════════════════════════════════════════
REM PR Agent Demo — 本地 CI 模拟脚本（Windows CMD）
REM 用法:
REM   scripts\ci-lint.bat              Lint + Test（快速）
REM   scripts\ci-lint.bat --all        Lint + Test + Build + Security
REM   scripts\ci-lint.bat --lint       仅 Lint
REM   scripts\ci-lint.bat --test       仅 Test
REM ═══════════════════════════════════════════════════════════════

setlocal enabledelayedexpansion
cd /d "%~dp0\.."

set PASSED=0
set FAILED=0
set MODE=%1
if "%MODE%"=="" set MODE=--quick

echo.
echo ========================================
echo  PR Agent Demo — CI Simulation
echo  Mode: %MODE%
echo ========================================

REM ── Lint: Python ────────────────────────────────────────────
:lint_python
if "%MODE%"=="--security" goto test_python_check
echo.
echo [Lint] Python — ruff
where ruff >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo   [WARN] ruff not installed. Run: pip install ruff
    set /a FAILED+=1
    goto lint_frontend
)
ruff check services/ tests/
if %ERRORLEVEL% equ 0 (echo   [PASS] ruff check & set /a PASSED+=1) else (echo   [FAIL] ruff check & set /a FAILED+=1)

ruff format --check services/ tests/
if %ERRORLEVEL% equ 0 (echo   [PASS] ruff format & set /a PASSED+=1) else (echo   [FAIL] ruff format & set /a FAILED+=1)

REM ── Lint: Frontend ──────────────────────────────────────────
:lint_frontend
if "%MODE%"=="--test" goto test_python
if "%MODE%"=="--security" goto test_python_check
echo.
echo [Lint] Frontend — biome
if not exist "frontend\node_modules\.bin\biome.cmd" (
    echo   [WARN] biome not installed. Run: cd frontend ^&^& npm ci
    set /a FAILED+=1
    goto lint_docker
)
cd frontend
npx biome check src/ --max-diagnostics=50
if %ERRORLEVEL% equ 0 (echo   [PASS] biome check & set /a PASSED+=1) else (echo   [FAIL] biome check & set /a FAILED+=1)
cd ..

REM ── Lint: Docker ────────────────────────────────────────────
:lint_docker
if "%MODE%"=="--quick" goto test_python
if "%MODE%"=="--lint" goto test_python_check
echo.
echo [Lint] Docker — hadolint
where hadolint >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo   [WARN] hadolint not installed. Ignore for quick CI.
    set /a FAILED+=1
    goto test_python
)
echo   [INFO] hadolint found, scanning Dockerfiles...
hadolint services\mcp_wewe\Dockerfile services\mcp_crawl\Dockerfile services\backend\Dockerfile frontend\Dockerfile
if %ERRORLEVEL% equ 0 (echo   [PASS] hadolint & set /a PASSED+=1) else (echo   [FAIL] hadolint & set /a FAILED+=1)

REM ── Test: Python ────────────────────────────────────────────
:test_python
echo.
echo [Test] Python — pytest
where pytest >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo   [WARN] pytest not found. Run: pip install pytest pytest-asyncio pytest-cov
    set /a FAILED+=1
    goto test_frontend
)
pytest tests/ --cov=services --cov-report=term-missing -v --tb=short --timeout=60
if %ERRORLEVEL% equ 0 (echo   [PASS] pytest & set /a PASSED+=1) else (echo   [FAIL] pytest & set /a FAILED+=1)

REM ── Test: Frontend ──────────────────────────────────────────
:test_frontend
if "%MODE%"=="--lint" goto results
if "%MODE%"=="--quick" goto results
echo.
echo [Test] Frontend — vitest
if not exist "frontend\node_modules\.bin\vitest.cmd" (
    echo   [WARN] vitest not installed. Run: cd frontend ^&^& npm ci
    set /a FAILED+=1
    goto build_check
)
cd frontend
npx vitest run --reporter=verbose
if %ERRORLEVEL% equ 0 (echo   [PASS] vitest & set /a PASSED+=1) else (echo   [FAIL] vitest & set /a FAILED+=1)
cd ..

REM ── Build ───────────────────────────────────────────────────
:build_check
if "%MODE%"=="--test" goto results
if not "%MODE%"=="--all" goto results
echo.
echo [Build] Docker Compose
where docker >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo   [WARN] Docker not found
    set /a FAILED+=1
    goto security_check
)
docker compose build --no-cache
if %ERRORLEVEL% equ 0 (echo   [PASS] docker compose build & set /a PASSED+=1) else (echo   [FAIL] docker compose build & set /a FAILED+=1)

REM ── Security ────────────────────────────────────────────────
:security_check
if not "%MODE%"=="--all" if not "%MODE%"=="--security" goto results
echo.
echo [Security] bandit
where bandit >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo   [WARN] bandit not installed. Run: pip install bandit
    set /a FAILED+=1
) else (
    bandit -r services/ -f json -o bandit-report.json --exit-zero
    echo   [INFO] bandit report saved to bandit-report.json
    set /a PASSED+=1
)

REM ── Results ─────────────────────────────────────────────────
:test_python_check
if "%MODE%"=="--lint" goto results
goto test_python

:results
echo.
echo ========================================
echo  Results: %PASSED% passed, %FAILED% failed
echo ========================================

if %FAILED% gtr 0 exit /b 1
exit /b 0
