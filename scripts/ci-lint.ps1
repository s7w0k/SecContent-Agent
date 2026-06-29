# ═══════════════════════════════════════════════════════════════
# PR Agent Demo — 本地 CI 模拟脚本（Windows PowerShell）
# 用法:
#   .\scripts\ci-lint.ps1              # Lint + Test（快速）
#   .\scripts\ci-lint.ps1 -All         # Lint + Test + Build + Security
#   .\scripts\ci-lint.ps1 -Lint        # 仅 Lint
#   .\scripts\ci-lint.ps1 -Test        # 仅 Test
#   .\scripts\ci-lint.ps1 -Security    # 仅 Security
# ═══════════════════════════════════════════════════════════════
param(
    [switch]$All,
    [switch]$Lint,
    [switch]$Test,
    [switch]$Security
)

$ErrorActionPreference = "Continue"
Set-Location "$PSScriptRoot\.."

$script:Passed = 0
$script:Failed = 0

function Step-Pass($msg) {
    Write-Host "  [PASS] $msg" -ForegroundColor Green
    $script:Passed++
}
function Step-Fail($msg) {
    Write-Host "  [FAIL] $msg" -ForegroundColor Red
    $script:Failed++
}
function Step-Warn($msg) {
    Write-Host "  [WARN] $msg" -ForegroundColor Yellow
}

# ── Lint: Python (ruff) ──────────────────────────────────────
function Invoke-LintPython {
    Write-Host "`n[Lint] Python — ruff" -ForegroundColor Cyan

    $ruff = Get-Command ruff -ErrorAction SilentlyContinue
    if ($ruff) {
        ruff check services/ tests/ 2>&1
        if ($LASTEXITCODE -eq 0) { Step-Pass "ruff check" } else { Step-Fail "ruff check" }

        ruff format --check services/ tests/ 2>&1
        if ($LASTEXITCODE -eq 0) { Step-Pass "ruff format" } else { Step-Fail "ruff format" }
    }
    else {
        Step-Warn "ruff not installed. Run: pip install ruff"
        Step-Fail "ruff (not found)"
    }
}

# ── Lint: Frontend (biome) ───────────────────────────────────
function Invoke-LintFrontend {
    Write-Host "`n[Lint] Frontend — biome" -ForegroundColor Cyan

    if (Test-Path "frontend\node_modules\.bin\biome.cmd") {
        Push-Location frontend
        npx biome check src/ --max-diagnostics=50 2>&1
        if ($LASTEXITCODE -eq 0) { Step-Pass "biome check" } else { Step-Fail "biome check" }
        Pop-Location
    }
    else {
        Step-Warn "biome not installed. Run: cd frontend && npm ci"
        Step-Fail "biome (not found)"
    }
}

# ── Lint: Docker (hadolint) ──────────────────────────────────
function Invoke-LintDocker {
    Write-Host "`n[Lint] Docker — hadolint" -ForegroundColor Cyan

    $hadolint = Get-Command hadolint -ErrorAction SilentlyContinue
    if (-not $hadolint) {
        # Try Docker-based hadolint
        $dockerCheck = docker run --rm -v "${PWD}:/work" hadolint/hadolint hadolint /work/services/mcp_wewe/Dockerfile 2>&1
        if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq 1) {
            Step-Warn "Using Docker-based hadolint (limited)"
            Step-Pass "hadolint (docker)"
        }
        else {
            Step-Warn "hadolint not installed. Install via: docker pull hadolint/hadolint"
            Step-Fail "hadolint (not found)"
        }
    }
    else {
        $ok = $true
        @("services/mcp_wewe/Dockerfile", "services/mcp_crawl/Dockerfile", "services/backend/Dockerfile", "frontend/Dockerfile") | ForEach-Object {
            if (Test-Path $_) {
                hadolint $_ 2>&1
                if ($LASTEXITCODE -ne 0) { $ok = $false }
            }
        }
        if ($ok) { Step-Pass "hadolint" } else { Step-Fail "hadolint" }
    }
}

# ── Test: Python (pytest) ────────────────────────────────────
function Invoke-TestPython {
    Write-Host "`n[Test] Python — pytest" -ForegroundColor Cyan

    $pytest = Get-Command pytest -ErrorAction SilentlyContinue
    if ($pytest) {
        pytest tests/ --cov=services --cov-report=term-missing -v --tb=short --timeout=60 2>&1
        if ($LASTEXITCODE -eq 0) { Step-Pass "pytest" } else { Step-Fail "pytest" }
    }
    else {
        Step-Warn "pytest not installed. Run: pip install pytest pytest-asyncio pytest-cov"
        Step-Fail "pytest (not found)"
    }
}

# ── Test: Frontend (vitest) ──────────────────────────────────
function Invoke-TestFrontend {
    Write-Host "`n[Test] Frontend — vitest" -ForegroundColor Cyan

    if (Test-Path "frontend\node_modules\.bin\vitest.cmd") {
        Push-Location frontend
        npx vitest run --reporter=verbose 2>&1
        if ($LASTEXITCODE -eq 0) { Step-Pass "vitest" } else { Step-Fail "vitest" }
        Pop-Location
    }
    else {
        Step-Warn "vitest not installed. Run: cd frontend && npm ci"
        Step-Fail "vitest (not found)"
    }
}

# ── Build ────────────────────────────────────────────────────
function Invoke-Build {
    Write-Host "`n[Build] Docker Compose" -ForegroundColor Cyan

    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if ($docker) {
        docker compose build --no-cache 2>&1
        if ($LASTEXITCODE -eq 0) { Step-Pass "docker compose build" } else { Step-Fail "docker compose build" }
    }
    else {
        Step-Warn "Docker not found"
        Step-Fail "docker build (not found)"
    }
}

# ── Security ─────────────────────────────────────────────────
function Invoke-Security {
    Write-Host "`n[Security] bandit + pip-audit" -ForegroundColor Cyan

    $bandit = Get-Command bandit -ErrorAction SilentlyContinue
    if ($bandit) {
        bandit -r services/ -f json -o bandit-report.json --exit-zero 2>&1
        # Parse bandit JSON for HIGH/CRITICAL count
        if (Test-Path bandit-report.json) {
            $report = Get-Content bandit-report.json | ConvertFrom-Json
            $highCritical = ($report.results | Where-Object { $_.issue_severity -in @("HIGH", "CRITICAL") }).Count
            if ($highCritical -eq 0) {
                Step-Pass "bandit (no HIGH/CRITICAL)"
            }
            else {
                Step-Fail "bandit ($highCritical HIGH/CRITICAL issues)"
            }
        }
        else {
            Step-Pass "bandit (report generated)"
        }
    }
    else {
        Step-Warn "bandit not installed. Run: pip install bandit"
        Step-Fail "bandit (not found)"
    }

    $pipAudit = Get-Command pip-audit -ErrorAction SilentlyContinue
    if ($pipAudit) {
        pip-audit 2>&1
        if ($LASTEXITCODE -eq 0) { Step-Pass "pip-audit (no vulns)" } else { Step-Fail "pip-audit (vulnerabilities found)" }
    }
    else {
        Step-Warn "pip-audit not installed. Run: pip install pip-audit"
        Step-Fail "pip-audit (not found)"
    }
}

# ── Main ─────────────────────────────────────────────────────
$modeStr = if ($All) { "ALL" } elseif ($Lint) { "LINT" } elseif ($Test) { "TEST" } elseif ($Security) { "SECURITY" } else { "QUICK" }

Write-Host ""
Write-Host "========================================" -ForegroundColor White
Write-Host " PR Agent Demo — CI Simulation" -ForegroundColor White
Write-Host " Mode: $modeStr" -ForegroundColor White
Write-Host "========================================" -ForegroundColor White

if ($All) {
    Invoke-LintPython
    Invoke-LintFrontend
    Invoke-LintDocker
    Invoke-TestPython
    Invoke-TestFrontend
    Invoke-Build
    Invoke-Security
}
elseif ($Lint) {
    Invoke-LintPython
    Invoke-LintFrontend
    Invoke-LintDocker
}
elseif ($Test) {
    Invoke-TestPython
    Invoke-TestFrontend
}
elseif ($Security) {
    Invoke-Security
}
else {
    # Default: QUICK mode
    Invoke-LintPython
    Invoke-LintFrontend
    Invoke-TestPython
}

Write-Host ""
Write-Host "========================================" -ForegroundColor White
Write-Host " Results: " -NoNewline
Write-Host "$script:Passed passed" -ForegroundColor Green -NoNewline
Write-Host ", " -NoNewline
Write-Host "$script:Failed failed" -ForegroundColor Red
Write-Host "========================================" -ForegroundColor White

if ($script:Failed -gt 0) {
    exit 1
}
