# Run All-Media Downloader Bot
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (Test-Path "$Root\.venv\Scripts\Activate.ps1") {
    & "$Root\.venv\Scripts\Activate.ps1"
}

if (-not (Test-Path "$Root\.env")) {
    Write-Host "Missing .env — copy .env.example to .env and set BOT_TOKEN" -ForegroundColor Red
    exit 1
}

Write-Host "Starting All-Media Downloader Bot..." -ForegroundColor Cyan
python run.py
