[CmdletBinding()]
param(
    [ValidateSet("build", "up", "upgrade", "rollback", "down", "logs", "status", "config")]
    [string]$Action = "status",
    [string]$Tag = "",
    [string]$EnvFile = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectName = if ($env:MCP_CRAWL_PROJECT_NAME) { $env:MCP_CRAWL_PROJECT_NAME } else { "pr-crawler" }
if (-not $EnvFile) { $EnvFile = Join-Path $ScriptDir ".env.crawler" }
$ComposeFile = Join-Path $ScriptDir "docker-compose.yml"
$BuildFile = Join-Path $ScriptDir "docker-compose.build.yml"

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Missing $EnvFile; copy .env.crawler.example and set MCP_CRAWL_API_KEY."
}

$BaseArgs = @("compose", "-p", $ProjectName, "--env-file", $EnvFile, "-f", $ComposeFile)

function Invoke-Docker {
    param([string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed with exit code $LASTEXITCODE" }
}

switch ($Action) {
    "build" {
        Invoke-Docker ($BaseArgs + @("-f", $BuildFile, "build"))
    }
    "up" {
        Invoke-Docker ($BaseArgs + @("up", "-d", "--no-build", "--remove-orphans"))
    }
    "upgrade" {
        Invoke-Docker ($BaseArgs + @("pull"))
        Invoke-Docker ($BaseArgs + @("up", "-d", "--no-build", "--remove-orphans"))
    }
    "rollback" {
        if (-not $Tag) { throw "Usage: manage.ps1 rollback -Tag <image-tag>" }
        $PreviousTag = $env:MCP_CRAWL_IMAGE_TAG
        try {
            $env:MCP_CRAWL_IMAGE_TAG = $Tag
            Invoke-Docker ($BaseArgs + @("pull"))
            Invoke-Docker ($BaseArgs + @("up", "-d", "--no-build", "--remove-orphans"))
        }
        finally {
            $env:MCP_CRAWL_IMAGE_TAG = $PreviousTag
        }
    }
    "down" { Invoke-Docker ($BaseArgs + @("down")) }
    "logs" { Invoke-Docker ($BaseArgs + @("logs", "-f", "--tail", "200")) }
    "status" { Invoke-Docker ($BaseArgs + @("ps")) }
    "config" { Invoke-Docker ($BaseArgs + @("config")) }
}
