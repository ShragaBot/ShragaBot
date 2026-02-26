# Shraga Box Provisioning Script
# Provisions a bare dev box and shows the RDP link.
# All tool installation and setup happens via setup-shragabox.ps1 on the box itself.
#
# Usage: Right-click -> Run with PowerShell
# Download: https://github.com/ShragaBot/ShragaBot/releases/download/setup-v1/setup.ps1

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "================================" -ForegroundColor Cyan
Write-Host "  Shraga Dev Box Setup" -ForegroundColor Cyan
Write-Host "================================" -ForegroundColor Cyan
Write-Host ""

# Config
$DevCenterEndpoint = "https://72f988bf-86f1-41af-91ab-2d7cd011db47-devcenter-4l24zmpbcslv2-dc.westus3.devcenter.azure.com"
$Project = "PVA"
$Pool = "botdesigner-pool-italynorth"
$ApiVersion = "2024-05-01-preview"
$TenantId = "72f988bf-86f1-41af-91ab-2d7cd011db47"
$SubscriptionId = "b1749b92-7ad4-4211-bcd8-ceb41c5d17f1"  # PJ-PVA

# Pre-check: az CLI must be installed
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Host "  Azure CLI (az) is required but not installed." -ForegroundColor Red
    Write-Host "  Install from: https://aka.ms/installazurecli" -ForegroundColor Yellow
    exit 1
}

# Helper: get fresh token
function Get-DevCenterToken {
    $t = az account get-access-token --resource "https://devcenter.azure.com" --query "accessToken" -o tsv
    return @{ "Authorization" = "Bearer $t"; "User-Agent" = "Shraga-Setup/1.0" }
}

# Step 1: Authenticate
Write-Host "[1/4] Authenticating..." -ForegroundColor Yellow
$existingUser = az account show --query "user.name" -o tsv 2>$null
if ($LASTEXITCODE -eq 0 -and $existingUser) {
    Write-Host "  Already signed in as: $existingUser" -ForegroundColor Green
} else {
    Write-Host "  A browser window will open. Sign in with your Microsoft account." -ForegroundColor Gray
    az login --tenant $TenantId --output none 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Authentication failed. Please try again." -ForegroundColor Red
        exit 1
    }
}
az account set --subscription $SubscriptionId
$userEmail = az account show --query "user.name" -o tsv
Write-Host "  Signed in as: $userEmail" -ForegroundColor Green

# Step 2: Find next available dev box name
Write-Host ""
Write-Host "[2/4] Finding next available dev box..." -ForegroundColor Yellow
$headers = Get-DevCenterToken
$existingBoxes = (Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes?api-version=$ApiVersion" -Headers $headers).value
$shragaBoxes = @($existingBoxes | Where-Object { $_.name -match "^shraga-box-\d+$" })
$usedNumbers = @($shragaBoxes | ForEach-Object { [int]($_.name -replace "shraga-box-", "") })

$nextNum = 1
while ($usedNumbers -contains $nextNum) { $nextNum++ }
$DevBoxName = "shraga-box-{0:D2}" -f $nextNum

Write-Host "  Existing shraga boxes: $($shragaBoxes.Count)" -ForegroundColor Gray
Write-Host "  Creating: $DevBoxName" -ForegroundColor Green

# Step 3: Provision dev box
Write-Host ""
Write-Host "[3/4] Provisioning dev box..." -ForegroundColor Yellow
$headers = Get-DevCenterToken
$body = @{ poolName = $Pool } | ConvertTo-Json
try {
    Invoke-RestMethod -Method Put `
        -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName`?api-version=$ApiVersion" `
        -Headers ($headers + @{ "Content-Type" = "application/json" }) -Body $body | Out-Null
    Write-Host "  Provisioning started" -ForegroundColor Green
} catch {
    Write-Host "  Failed: $_" -ForegroundColor Red
    exit 1
}

Write-Host "  This typically takes ~25 minutes. Go grab a coffee!" -ForegroundColor Gray
Write-Host ""
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$spinner = @('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')
$spinIdx = 0
$state = "Provisioning"
$pollInterval = 30
$lastPoll = $sw.Elapsed.TotalSeconds
while ($true) {
    # Poll API every 30 seconds
    if ($sw.Elapsed.TotalSeconds - $lastPoll -ge $pollInterval) {
        $headers = Get-DevCenterToken
        $status = Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName`?api-version=$ApiVersion" -Headers $headers
        $state = $status.provisioningState
        $lastPoll = $sw.Elapsed.TotalSeconds
        if ($state -eq "Succeeded") {
            $elapsedMin = [math]::Floor($sw.Elapsed.TotalMinutes)
            $elapsedSec = $sw.Elapsed.Seconds
            Write-Host "`r  Done! (took ${elapsedMin}m ${elapsedSec}s)                    " -ForegroundColor Green
            break
        }
        if ($state -eq "Failed") {
            Write-Host "`r  Provisioning failed.                    " -ForegroundColor Red
            exit 1
        }
    }
    # Update spinner every second
    $elapsedMin = [math]::Floor($sw.Elapsed.TotalMinutes)
    $elapsedSec = $sw.Elapsed.Seconds
    $s = $spinner[$spinIdx % $spinner.Count]; $spinIdx++
    Write-Host "`r  $s $state... ${elapsedMin}m ${elapsedSec}s elapsed  " -NoNewline -ForegroundColor Cyan
    Start-Sleep -Seconds 1
}

# Step 4: Show connection info
Write-Host ""
Write-Host "[4/4] Getting connection info..." -ForegroundColor Yellow
$headers = Get-DevCenterToken
$conn = Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName/remoteConnection?api-version=$ApiVersion" -Headers $headers
$webUrl = $conn.webUrl

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Your dev box is ready: $DevBoxName" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor White
Write-Host ""
Write-Host "  1. Open this link to connect:" -ForegroundColor White
Write-Host "     $webUrl" -ForegroundColor Cyan
Write-Host ""
Write-Host "  2. Once connected, open PowerShell and run:" -ForegroundColor White
Write-Host "     irm https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/setup-shragabox.ps1 | iex" -ForegroundColor Cyan
Write-Host ""
Write-Host "     This will install all tools, authenticate, and start Shraga." -ForegroundColor Gray
Write-Host ""
Read-Host "Press Enter to close"
