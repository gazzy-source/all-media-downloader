# Setup All-Media Downloader Bot (Windows PowerShell)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "==> All-Media Downloader Bot setup" -ForegroundColor Cyan
Write-Host "    Project: $Root"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python not found. Install Python 3.11+ from https://python.org" -ForegroundColor Red
    exit 1
}

python -m venv .venv
& "$Root\.venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip
pip install -r requirements.txt

if (-not (Test-Path "$Root\.env")) {
    Copy-Item "$Root\.env.example" "$Root\.env"
    Write-Host ""
    Write-Host "Created .env — open it and set BOT_TOKEN from @BotFather" -ForegroundColor Yellow
} else {
    Write-Host ".env already exists" -ForegroundColor Green
}

foreach ($d in @("downloads", "temp", "data")) {
    New-Item -ItemType Directory -Force -Path "$Root\$d" | Out-Null
}

Write-Host ""
Write-Host "Checking FFmpeg..." -ForegroundColor Cyan
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    ffmpeg -version | Select-Object -First 1
    Write-Host "FFmpeg OK" -ForegroundColor Green
} else {
    Write-Host "FFmpeg NOT found. Install with: winget install FFmpeg" -ForegroundColor Yellow
    Write-Host "Audio conversion & video merging need FFmpeg." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "1. Edit .env and set BOT_TOKEN"
Write-Host "2. Run: .\scripts\run.ps1"
