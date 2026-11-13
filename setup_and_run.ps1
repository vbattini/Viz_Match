# Quick Start Setup and Run Script for Power BI Dashboard Comparator
# This script handles environment setup and launches the application

param(
    [Parameter(Mandatory=$false)]
    [string]$Mode = "api",  # 'api' or 'cli'
    
    [Parameter(Mandatory=$false)]
    [string]$TenantId = "e7c6f609-f63a-4ec7-b71e-6734c4c9d231",
    
    [Parameter(Mandatory=$false)]
    [string]$GoogleCloudProject = "",
    
    [Parameter(Mandatory=$false)]
    [string]$CredentialsFile = "",
    
    [Parameter(Mandatory=$false)]
    [string]$Port = "8001",
    
    [Parameter(Mandatory=$false)]
    [bool]$SkipVenvCheck = $false
)

$ErrorActionPreference = "Stop"

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "Power BI Dashboard Comparator - Setup Script" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Get script directory
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "Working directory: $ScriptDir" -ForegroundColor Green
Write-Host ""

# Check Python version
Write-Host "Checking Python version..." -ForegroundColor Yellow
$PythonVersion = python --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python is not installed or not in PATH" -ForegroundColor Red
    Write-Host "Please install Python 3.10+ and add it to PATH" -ForegroundColor Red
    exit 1
}
Write-Host "✓ Found: $PythonVersion" -ForegroundColor Green
Write-Host ""

# Create/activate virtual environment
if (-not $SkipVenvCheck) {
    Write-Host "Setting up virtual environment..." -ForegroundColor Yellow
    
    if (-not (Test-Path "venv")) {
        Write-Host "Creating new virtual environment..." -ForegroundColor Yellow
        python -m venv venv
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: Failed to create virtual environment" -ForegroundColor Red
            exit 1
        }
        Write-Host "✓ Virtual environment created" -ForegroundColor Green
    } else {
        Write-Host "✓ Virtual environment already exists" -ForegroundColor Green
    }
    
    Write-Host "Activating virtual environment..." -ForegroundColor Yellow
    & ".\venv\Scripts\Activate.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Note: If you see an execution policy error, run:" -ForegroundColor Yellow
        Write-Host "  Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "✓ Virtual environment activated" -ForegroundColor Green
    Write-Host ""
}

# Install dependencies
Write-Host "Installing dependencies..." -ForegroundColor Yellow
pip install --upgrade pip setuptools wheel 2>&1 | Out-Null
pip install -e . 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to install dependencies" -ForegroundColor Red
    exit 1
}
Write-Host "✓ Dependencies installed" -ForegroundColor Green
Write-Host ""

# Install Playwright browsers
Write-Host "Setting up Playwright browsers..." -ForegroundColor Yellow
$CheckChrome = playwright show-trace --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing Chromium browser..." -ForegroundColor Yellow
    playwright install chromium 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to install Playwright browsers" -ForegroundColor Red
        exit 1
    }
}
Write-Host "✓ Playwright browsers ready" -ForegroundColor Green
Write-Host ""

# Check/create auth.json
Write-Host "Checking Power BI authentication..." -ForegroundColor Yellow
if (-not (Test-Path "auth.json")) {
    Write-Host "⚠ auth.json not found" -ForegroundColor Yellow
    Write-Host "You will need to authenticate with Power BI first" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "To create auth.json, run this PowerShell command once:" -ForegroundColor Cyan
    Write-Host '$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=' + $TenantId + '&experience=power-bi"' -ForegroundColor Cyan
    Write-Host 'python -m Backend.cli --url-a "https://app.powerbi.com/report/test" --url-b "https://app.powerbi.com/report/test" --storage-state auth.json' -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Or use Playwright to create it manually:" -ForegroundColor Cyan
    Write-Host '@"' -ForegroundColor Cyan
    Write-Host 'from playwright.sync_api import sync_playwright; from pathlib import Path' -ForegroundColor Cyan
    Write-Host 'with sync_playwright() as p:' -ForegroundColor Cyan
    Write-Host '    ctx = p.chromium.launch().new_context()' -ForegroundColor Cyan
    Write-Host '    ctx.new_page().goto("https://app.powerbi.com"); input("Sign in and press Enter..."); ctx.storage_state(path="auth.json")' -ForegroundColor Cyan
    Write-Host '"@ | python -' -ForegroundColor Cyan
    Write-Host ""
} else {
    Write-Host "✓ auth.json found" -ForegroundColor Green
}
Write-Host ""

# Set environment variables
Write-Host "Configuring environment..." -ForegroundColor Yellow
$env:POWERBI_HOME_URL = "https://app.powerbi.com/home?ctid=$TenantId&experience=power-bi"
$env:POWERBI_STORAGE_STATE = "auth.json"

if ($GoogleCloudProject) {
    $env:GOOGLE_CLOUD_PROJECT = $GoogleCloudProject
    Write-Host "✓ Google Cloud Project: $GoogleCloudProject" -ForegroundColor Green
}

if ($CredentialsFile -and (Test-Path $CredentialsFile)) {
    $env:GOOGLE_APPLICATION_CREDENTIALS = $CredentialsFile
    Write-Host "✓ Google Credentials: $CredentialsFile" -ForegroundColor Green
}

Write-Host "✓ Environment configured" -ForegroundColor Green
Write-Host ""

# Launch application
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "Launching in $Mode mode..." -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

if ($Mode -eq "api") {
    Write-Host "Starting API server on http://127.0.0.1:$Port" -ForegroundColor Green
    Write-Host "API Documentation: http://127.0.0.1:$Port/docs" -ForegroundColor Green
    Write-Host "Quit with Ctrl+C" -ForegroundColor Yellow
    Write-Host ""
    
    # Start API
    uvicorn Backend.api:app --host 127.0.0.1 --port $Port --reload
} elseif ($Mode -eq "cli") {
    Write-Host "CLI Mode - Ready to run commands" -ForegroundColor Green
    Write-Host ""
    Write-Host "Example commands:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "# Run all five scenarios:" -ForegroundColor Cyan
    Write-Host 'python -m Backend.cli --url-a <DASHBOARD_A_URL> --url-b <DASHBOARD_B_URL> --output-dir output/test --storage-state auth.json --validation-scenarios' -ForegroundColor Cyan
    Write-Host ""
    Write-Host "# Run single scenario (01 = initial state):" -ForegroundColor Cyan
    Write-Host 'python -m Backend.cli --url-a <DASHBOARD_A_URL> --url-b <DASHBOARD_B_URL> --output-dir output/test --storage-state auth.json --validation-scenarios --scenario 01' -ForegroundColor Cyan
    Write-Host ""
    Write-Host "# Run scenarios 02-04:" -ForegroundColor Cyan
    Write-Host 'python -m Backend.cli --url-a <DASHBOARD_A_URL> --url-b <DASHBOARD_B_URL> --output-dir output/test --storage-state auth.json --validation-scenarios --scenario 02 --through 04' -ForegroundColor Cyan
    Write-Host ""
    Write-Host "For more options, run:" -ForegroundColor Cyan
    Write-Host "python -m Backend.cli --help" -ForegroundColor Cyan
    Write-Host ""
} else {
    Write-Host "ERROR: Mode must be 'api' or 'cli'" -ForegroundColor Red
    exit 1
}
