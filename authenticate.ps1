# Shraga Dev Box Setup & Authentication Script
# Runs ON the dev box. Installs all tools, clones code, authenticates, starts services.
#
# Usage: irm https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/authenticate.ps1 | iex

$ErrorActionPreference = "Continue"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
$WORKING_DIR = "C:\Dev\shraga-worker"
$REPO_URL = "https://github.com/ShragaBot/ShragaBot.git"
$MAX_AZ_LOGIN_RETRIES = 3
$WORKER_SCRIPT = Join-Path $WORKING_DIR "integrated_task_worker.py"
$PM_SCRIPT = Join-Path $WORKING_DIR "task-manager\task_manager.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Step { param([string]$StepLabel, [string]$Message, [string]$Color = "Yellow"); Write-Host ""; Write-Host "[$StepLabel] $Message" -ForegroundColor $Color }
function Write-Success { param([string]$Message); Write-Host "  [OK] $Message" -ForegroundColor Green }
function Write-Failure { param([string]$Message); Write-Host "  [FAIL] $Message" -ForegroundColor Red }
function Write-Info { param([string]$Message); Write-Host "  $Message" -ForegroundColor Gray }
function Write-Warn { param([string]$Message); Write-Host "  [WARN] $Message" -ForegroundColor Yellow }

function Find-Python {
    $candidates = @(
        "C:\Python312\python.exe",
        "C:\ProgramData\chocolatey\lib\python312\tools\python.exe",
        "C:\ProgramData\chocolatey\bin\python3.exe",
        "C:\ProgramData\chocolatey\bin\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $found = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($found) { return $found }
    return $null
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Shraga Dev Box Setup" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# =========================================================================
# Step 1: Install Tools (Git, Claude Code, Python)
# =========================================================================
Write-Step "1/7" "Installing Tools" "Yellow"

# Git
if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Success "Git already installed: $(git --version)"
} else {
    Write-Info "Installing Git via winget..."
    winget install --id Git.Git --accept-source-agreements --accept-package-agreements --silent 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Success "Git installed" } else { Write-Warn "Git install may have failed" }
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
}

# Claude Code
$claudeLocalBin = Join-Path $env:USERPROFILE ".local\bin"
if (Test-Path (Join-Path $claudeLocalBin "claude.exe")) {
    if ($env:Path -notlike "*$claudeLocalBin*") {
        $env:Path += ";$claudeLocalBin"
        $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
        if ($userPath -notlike "*$claudeLocalBin*") {
            [System.Environment]::SetEnvironmentVariable("Path", "$userPath;$claudeLocalBin", "User")
        }
    }
    Write-Success "Claude Code already installed: $(claude --version 2>$null)"
} elseif (Get-Command claude -ErrorAction SilentlyContinue) {
    Write-Success "Claude Code already installed: $(claude --version 2>$null)"
} else {
    Write-Info "Installing Claude Code..."
    try {
        irm https://claude.ai/install.ps1 | iex 2>$null
        # Add to PATH
        if (Test-Path (Join-Path $claudeLocalBin "claude.exe")) {
            $env:Path += ";$claudeLocalBin"
            $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
            if ($userPath -notlike "*$claudeLocalBin*") {
                [System.Environment]::SetEnvironmentVariable("Path", "$userPath;$claudeLocalBin", "User")
            }
        }
        Write-Success "Claude Code installed"
    } catch {
        Write-Warn "Claude Code install failed: $_"
        Write-Info "Install manually: irm https://claude.ai/install.ps1 | iex"
    }
}

# Python
$pyExe = Find-Python
if ($pyExe) {
    Write-Success "Python already installed: $($pyExe)"
} else {
    Write-Info "Installing Python 3.12 via winget..."
    winget install --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Info "Trying chocolatey..."
        if (Get-Command choco -ErrorAction SilentlyContinue) {
            choco install python312 -y 2>$null
        }
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $pyExe = Find-Python
    if ($pyExe) { Write-Success "Python installed: $pyExe" } else { Write-Warn "Python install failed — install manually" }
}

# =========================================================================
# Step 2: Clone/Update Repository
# =========================================================================
Write-Step "2/7" "Setting Up Code" "Yellow"

if (Test-Path (Join-Path $WORKING_DIR ".git")) {
    Write-Info "Repo exists, pulling latest..."
    Push-Location $WORKING_DIR
    git pull 2>$null
    Pop-Location
    Write-Success "Code updated"
} else {
    Write-Info "Cloning repository..."
    New-Item -ItemType Directory -Force -Path "C:\Dev" -ErrorAction SilentlyContinue | Out-Null
    # Use full path to git in case PATH isn't refreshed yet
    $gitExe = "C:\Program Files\Git\cmd\git.exe"
    if (-not (Test-Path $gitExe)) { $gitExe = "git" }
    & $gitExe clone --single-branch --depth 1 $REPO_URL $WORKING_DIR 2>$null
    if (Test-Path $WORKING_DIR) { Write-Success "Code cloned to $WORKING_DIR" } else { Write-Warn "Clone failed" }
}

# =========================================================================
# Step 3: Install Python Dependencies
# =========================================================================
Write-Step "3/7" "Installing Python Dependencies" "Yellow"

$pyExe = Find-Python
if ($pyExe) {
    & $pyExe -m pip install --quiet requests azure-identity azure-core watchdog 2>$null
    Write-Success "Dependencies installed"
} else {
    Write-Warn "Python not found — skipping pip install"
}

# =========================================================================
# Step 4: Configure Keep-Alive (prevent hibernation)
# =========================================================================
Write-Step "4/7" "Configuring Keep-Alive" "Yellow"

try {
    powercfg /change monitor-timeout-ac 0 2>$null
    powercfg /change standby-timeout-ac 0 2>$null
    powercfg /change hibernate-timeout-ac 0 2>$null
    powercfg /change disk-timeout-ac 0 2>$null
    powercfg /hibernate off 2>$null
    reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services" /v fResetBroken /t REG_DWORD /d 0 /f 2>$null | Out-Null
    Write-Success "Keep-alive configured"
} catch {
    Write-Warn "Some keep-alive settings may need admin: $_"
}

# =========================================================================
# Step 5: Azure Login
# =========================================================================
Write-Step "5/7" "Azure Login" "Yellow"

$azLoginSuccess = $false
$userEmail = $null

# Check if already authenticated
try {
    $existingAccount = az account show --output json 2>$null | ConvertFrom-Json
    if ($existingAccount -and $existingAccount.user.name) {
        $userEmail = $existingAccount.user.name
        Write-Success "Already signed in as: $userEmail"
        $azLoginSuccess = $true
    }
} catch { }

if (-not $azLoginSuccess) {
    Write-Info "A browser window will open. Sign in with your Microsoft account."
    for ($attempt = 1; $attempt -le $MAX_AZ_LOGIN_RETRIES; $attempt++) {
        try {
            az login 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $userEmail = az account show --query "user.name" -o tsv 2>$null
                $azLoginSuccess = $true
                Write-Success "Signed in as: $userEmail"
                break
            }
        } catch { }
        if ($attempt -lt $MAX_AZ_LOGIN_RETRIES) { Write-Info "Retrying..."; Start-Sleep 5 }
    }
}

if (-not $azLoginSuccess) {
    Write-Failure "Azure login failed. Run 'az login' manually."
    Read-Host "Press Enter to close"
    exit 1
}

# =========================================================================
# Step 6: Claude Code Login
# =========================================================================
Write-Step "6/7" "Claude Code Login" "Yellow"

$claudeLoginSuccess = $false

# Check if already authenticated
try {
    & claude --print --dangerously-skip-permissions "say ok" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $claudeLoginSuccess = $true
        Write-Success "Already authenticated. Skipping login."
    }
} catch { }

if (-not $claudeLoginSuccess) {
    if (Get-Command claude -ErrorAction SilentlyContinue) {
        Write-Info "Claude Code login will open in a new window..."
        $proc = Start-Process -FilePath "claude" -ArgumentList "/login" -Wait -PassThru
        if ($proc.ExitCode -eq 0) { $claudeLoginSuccess = $true; Write-Success "Claude Code login completed." }
        else { Write-Warn "Claude login returned exit code: $($proc.ExitCode). You can retry: claude /login" }
    } else {
        Write-Warn "Claude Code not found in PATH. Install it first, then run: claude /login"
    }
}

# =========================================================================
# Step 7: Set Environment Variables + Register & Start Services
# =========================================================================
Write-Step "7/7" "Starting Shraga Services" "Yellow"

# Set env vars
foreach ($varInfo in @(
    @{ Name = "USER_EMAIL"; Value = $userEmail },
    @{ Name = "WORKING_DIR"; Value = $WORKING_DIR },
    @{ Name = "WEBHOOK_USER"; Value = $userEmail },
    @{ Name = "AZURE_TOKEN_CREDENTIALS"; Value = "AzureCliCredential" }
)) {
    try {
        [System.Environment]::SetEnvironmentVariable($varInfo.Name, $varInfo.Value, "Machine")
        Set-Item -Path "Env:$($varInfo.Name)" -Value $varInfo.Value
    } catch {
        try {
            [System.Environment]::SetEnvironmentVariable($varInfo.Name, $varInfo.Value, "User")
            Set-Item -Path "Env:$($varInfo.Name)" -Value $varInfo.Value
        } catch { Write-Warn "Could not set $($varInfo.Name)" }
    }
}
Write-Success "Environment variables set"

# Register scheduled tasks + start services
$pyExe = Find-Python
if ($pyExe -and (Test-Path $WORKER_SCRIPT)) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

    foreach ($svc in @(
        @{ Name = "ShragaWorker"; Script = $WORKER_SCRIPT; Label = "Worker" },
        @{ Name = "ShragaPM"; Script = $PM_SCRIPT; Label = "PM" }
    )) {
        try {
            $action = New-ScheduledTaskAction -Execute $pyExe -Argument $svc.Script -WorkingDirectory $WORKING_DIR
            Register-ScheduledTask -TaskName $svc.Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

            # Stop if already running, then start
            $existing = Get-ScheduledTask -TaskName $svc.Name -ErrorAction SilentlyContinue
            if ($existing -and $existing.State -eq "Running") { Stop-ScheduledTask -TaskName $svc.Name -ErrorAction SilentlyContinue; Start-Sleep 2 }
            Start-ScheduledTask -TaskName $svc.Name
            Write-Success "$($svc.Label) registered and started"
        } catch {
            Write-Warn "Could not start $($svc.Label): $_"
            # Fallback: start directly
            Start-Process -FilePath $pyExe -ArgumentList $svc.Script -WorkingDirectory $WORKING_DIR -WindowStyle Hidden
            Write-Info "$($svc.Label) started directly (no scheduled task)"
        }
    }
} else {
    Write-Warn "Worker script not found at: $WORKER_SCRIPT"
    if (-not (Test-Path $WORKING_DIR)) {
        Write-Info "Try: git clone $REPO_URL $WORKING_DIR"
    }
}

# =========================================================================
# Summary
# =========================================================================
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Setup Complete!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Tools:       Git, Claude Code, Python" -ForegroundColor Green
Write-Host "  Azure:       $userEmail" -ForegroundColor $(if ($azLoginSuccess) { "Green" } else { "Yellow" })
Write-Host "  Claude Code: $(if ($claudeLoginSuccess) { 'Authenticated' } else { 'Needs: claude /login' })" -ForegroundColor $(if ($claudeLoginSuccess) { "Green" } else { "Yellow" })
Write-Host "  Worker:      $(if (Test-Path $WORKER_SCRIPT) { 'Running' } else { 'Not deployed' })" -ForegroundColor $(if (Test-Path $WORKER_SCRIPT) { "Green" } else { "Yellow" })
Write-Host "  PM:          $(if (Test-Path $PM_SCRIPT) { 'Running' } else { 'Not deployed' })" -ForegroundColor $(if (Test-Path $PM_SCRIPT) { "Green" } else { "Yellow" })
Write-Host ""
Write-Host "  Setup complete! Your Shraga Box is ready." -ForegroundColor Cyan
Write-Host ""
Read-Host "Press Enter to close"
