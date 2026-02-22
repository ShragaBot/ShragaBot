# Shraga Dev Box Setup Script (runs ON the dev box)
# Installs tools, clones code, authenticates, configures, starts services.
# Idempotent -- safe to re-run at any time.
#
# First box:      irm https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/setup-devbox.ps1 | iex
# Additional box: powershell -ExecutionPolicy Bypass -File setup-devbox.ps1 -WorkerOnly
# Re-run:         Double-click "Shraga Setup" shortcut on desktop

param(
    [switch]$WorkerOnly  # Skip PM -- used by setup-workerbox.ps1
)

$ErrorActionPreference = "Continue"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
$WORKING_DIR = "C:\Dev\shraga-worker"
$REPO_URL = "https://github.com/ShragaBot/ShragaBot.git"
$MAX_AZ_LOGIN_RETRIES = 3
$WORKER_SCRIPT = Join-Path $WORKING_DIR "integrated_task_worker.py"
$PM_SCRIPT = Join-Path $WORKING_DIR "task-manager\task_manager.py"
$LOG_FILE = Join-Path $env:TEMP "shraga-setup.log"

# ---------------------------------------------------------------------------
# Start logging
# ---------------------------------------------------------------------------
Start-Transcript -Path $LOG_FILE -Append -Force | Out-Null

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Step { param([string]$S, [string]$M); Write-Host ""; Write-Host "[$S] $M" -ForegroundColor Yellow }
function Write-OK { param([string]$M); Write-Host "  [OK] $M" -ForegroundColor Green }
function Write-Fail { param([string]$M); Write-Host "  [FAIL] $M" -ForegroundColor Red }
function Write-Info { param([string]$M); Write-Host "  $M" -ForegroundColor Gray }
function Write-Warning2 { param([string]$M); Write-Host "  [WARN] $M" -ForegroundColor Yellow }

function Find-Python {
    foreach ($c in @(
        "C:\Python312\python.exe",
        "C:\Python311\python.exe",
        "C:\Python310\python.exe",
        "C:\ProgramData\chocolatey\lib\python312\tools\python.exe",
        "C:\ProgramData\chocolatey\bin\python3.exe",
        "C:\ProgramData\chocolatey\bin\python.exe",
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe"
    )) { if (Test-Path $c) { return $c } }
    # Fallback to Get-Command but skip the Windows Store stub
    $found = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($found -and $found -notlike "*WindowsApps*") { return $found }
    $found = (Get-Command python3 -ErrorAction SilentlyContinue).Source
    if ($found -and $found -notlike "*WindowsApps*") { return $found }
    return $null
}

function Refresh-Path {
    # Merge Machine + User + current session PATH without losing in-process additions
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine") -split ";" | Where-Object { $_ }
    $user = [System.Environment]::GetEnvironmentVariable("Path", "User") -split ";" | Where-Object { $_ }
    $current = $env:Path -split ";" | Where-Object { $_ }
    $merged = ($machine + $user + $current) | Select-Object -Unique
    $env:Path = $merged -join ";"
}

function Ensure-InPath {
    param([string]$Dir)
    if ((Test-Path $Dir) -and ($env:Path -split ";" | Where-Object { $_ -eq $Dir }).Count -eq 0) {
        $env:Path += ";$Dir"
        $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
        if (($userPath -split ";" | Where-Object { $_ -eq $Dir }).Count -eq 0) {
            $cleanPath = if ([string]::IsNullOrWhiteSpace($userPath)) { $Dir } else { "$userPath;$Dir" }
            [System.Environment]::SetEnvironmentVariable("Path", $cleanPath, "User")
        }
    }
}

function Set-EnvVar {
    param([string]$Name, [string]$Value)
    # Try machine-level first, fall back to user-level
    try {
        [System.Environment]::SetEnvironmentVariable($Name, $Value, "Machine")
    } catch {
        [System.Environment]::SetEnvironmentVariable($Name, $Value, "User")
    }
    Set-Item -Path "Env:$Name" -Value $Value
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Shraga Dev Box Setup" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This script will:" -ForegroundColor White
Write-Host "    1. Install tools (Git, Python, Claude Code)" -ForegroundColor White
Write-Host "    2. Clone the Shraga code repository" -ForegroundColor White
Write-Host "    3. Install Python dependencies" -ForegroundColor White
Write-Host "    4. Configure sleep/hibernation prevention" -ForegroundColor White
Write-Host "    5. Sign you into Azure (browser opens)" -ForegroundColor White
Write-Host "    6. Sign you into Claude Code (window opens)" -ForegroundColor White
Write-Host "    7. Start Shraga services" -ForegroundColor White
Write-Host ""
Write-Host "  Log file: $LOG_FILE" -ForegroundColor Gray
Write-Host ""

# =========================================================================
# Step 1: Install Tools
# =========================================================================
Write-Step "1/7" "Installing Tools"

$hasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)
if (-not $hasWinget) { Write-Warning2 "winget not found -- tool installs may fail. Install App Installer from Microsoft Store." }

# -- Az CLI --
if (Get-Command az -ErrorAction SilentlyContinue) {
    Write-OK "Azure CLI already installed"
} elseif ($hasWinget) {
    Write-Info "Installing Azure CLI via winget... (this may take a minute)"
    winget install --id Microsoft.AzureCLI --accept-source-agreements --accept-package-agreements --silent 2>&1 | Out-Null
    Start-Sleep -Seconds 3
    Refresh-Path
    if (Get-Command az -ErrorAction SilentlyContinue) { Write-OK "Azure CLI installed" }
    else { Write-Fail "Azure CLI install failed. Install from: https://aka.ms/installazurecli" }
} else {
    Write-Fail "Azure CLI not found and winget not available. Install from: https://aka.ms/installazurecli"
}

# -- Git --
if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-OK "Git already installed"
} elseif ($hasWinget) {
    Write-Info "Installing Git via winget... (this may take a minute)"
    winget install --id Git.Git --accept-source-agreements --accept-package-agreements --silent 2>&1 | Out-Null
    Start-Sleep -Seconds 3
    Refresh-Path
    if (Get-Command git -ErrorAction SilentlyContinue) { Write-OK "Git installed" }
    else { Write-Fail "Git install failed. Install from: https://git-scm.com" }
} else {
    Write-Fail "Git not found and winget not available. Install from: https://git-scm.com"
}

# -- Python --
$pyExe = Find-Python
if ($pyExe) {
    Write-OK "Python already installed: $pyExe"
} else {
    if ($hasWinget) {
        Write-Info "Installing Python 3.12 via winget... (this may take a minute)"
        winget install --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent 2>&1 | Out-Null
    }
    if (-not $hasWinget -or $LASTEXITCODE -ne 0) {
        Write-Info "Winget failed, trying chocolatey..."
        if (Get-Command choco -ErrorAction SilentlyContinue) {
            choco install python312 -y 2>&1 | Out-Null
        }
    }
    Start-Sleep -Seconds 3
    Refresh-Path
    $pyExe = Find-Python
    if ($pyExe) { Write-OK "Python installed: $pyExe" }
    else { Write-Fail "Python install failed. Install from: https://python.org/downloads" }
}

# -- Claude Code --
$claudeLocalBin = Join-Path $env:USERPROFILE ".local\bin"
$claudeExe = Join-Path $claudeLocalBin "claude.exe"
if (Test-Path $claudeExe) {
    Ensure-InPath $claudeLocalBin
    Write-OK "Claude Code already installed"
} elseif (Get-Command claude -ErrorAction SilentlyContinue) {
    Write-OK "Claude Code already installed"
} else {
    Write-Info "Installing Claude Code... (this may take a minute)"
    try {
        $installScript = Invoke-RestMethod -Uri "https://claude.ai/install.ps1" -ErrorAction Stop
        Invoke-Expression $installScript 2>&1 | Out-Null
        if (Test-Path $claudeExe) {
            Ensure-InPath $claudeLocalBin
            Write-OK "Claude Code installed"
        } else {
            Write-Warning2 "Claude Code may not have installed. Run: irm https://claude.ai/install.ps1 | iex"
        }
    } catch {
        Write-Warning2 "Claude Code install failed: $_"
        Write-Info "Install manually: irm https://claude.ai/install.ps1 | iex"
    }
}

# -- Gate check: Git and Python are required for remaining steps --
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Fail "Git is required but not installed. Install it, then re-run this script."
    Write-Host "  Log saved to: $LOG_FILE" -ForegroundColor Gray
    Read-Host "Press Enter to close"
    Stop-Transcript | Out-Null
    exit 1
}
$pyExe = Find-Python
if (-not $pyExe) {
    Write-Fail "Python is required but not installed. Install it, then re-run this script."
    Write-Host "  Log saved to: $LOG_FILE" -ForegroundColor Gray
    Read-Host "Press Enter to close"
    Stop-Transcript | Out-Null
    exit 1
}

# =========================================================================
# Step 2: Clone/Update Repository
# =========================================================================
Write-Step "2/7" "Setting Up Code"

if (Test-Path (Join-Path $WORKING_DIR ".git")) {
    Write-Info "Repo exists, pulling latest..."
    git -C $WORKING_DIR pull 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-OK "Code updated" }
    else { Write-Warning2 "git pull failed -- may have local changes. Run: git -C $WORKING_DIR pull" }
} else {
    Write-Info "Cloning repository..."
    New-Item -ItemType Directory -Force -Path "C:\Dev" -ErrorAction SilentlyContinue | Out-Null
    $gitExe = if (Test-Path "C:\Program Files\Git\cmd\git.exe") { "C:\Program Files\Git\cmd\git.exe" } else { "git" }
    & $gitExe clone $REPO_URL $WORKING_DIR 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $WORKING_DIR ".git"))) {
        Write-OK "Code cloned to $WORKING_DIR"
    } else {
        Write-Fail "Clone failed. Check network and try: git clone $REPO_URL $WORKING_DIR"
    }
}

if (-not (Test-Path (Join-Path $WORKING_DIR ".git"))) {
    Write-Fail "Code repository not available. Cannot continue without it."
    Write-Host "  Log saved to: $LOG_FILE" -ForegroundColor Gray
    Read-Host "Press Enter to close"
    Stop-Transcript | Out-Null
    exit 1
}

# =========================================================================
# Step 3: Install Python Dependencies
# =========================================================================
Write-Step "3/7" "Installing Python Dependencies"

$pyExe = Find-Python
if ($pyExe -and (Test-Path $WORKING_DIR)) {
    & $pyExe -m pip install --quiet --upgrade requests azure-identity azure-core watchdog 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-OK "Dependencies installed" }
    else { Write-Warning2 "Some dependencies may have failed. Run: $pyExe -m pip install requests azure-identity azure-core watchdog" }
} elseif (-not $pyExe) {
    Write-Warning2 "Python not found -- skipping"
} else {
    Write-Warning2 "Code not cloned -- skipping"
}

# =========================================================================
# Step 4: Configure Keep-Alive
# =========================================================================
Write-Step "4/7" "Configuring Keep-Alive"
Write-Info "Preventing sleep/hibernation so your dev box stays online."

$keepAliveOk = $true
& powercfg /change monitor-timeout-ac 0 2>&1 | Out-Null; if ($LASTEXITCODE -ne 0) { $keepAliveOk = $false }
& powercfg /change standby-timeout-ac 0 2>&1 | Out-Null; if ($LASTEXITCODE -ne 0) { $keepAliveOk = $false }
& powercfg /change hibernate-timeout-ac 0 2>&1 | Out-Null; if ($LASTEXITCODE -ne 0) { $keepAliveOk = $false }
& powercfg /change disk-timeout-ac 0 2>&1 | Out-Null; if ($LASTEXITCODE -ne 0) { $keepAliveOk = $false }
& powercfg /hibernate off 2>&1 | Out-Null; if ($LASTEXITCODE -ne 0) { $keepAliveOk = $false }
& reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services" /v fResetBroken /t REG_DWORD /d 0 /f 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { $keepAliveOk = $false }

if ($keepAliveOk) { Write-OK "Keep-alive configured" }
else { Write-Warning2 "Some settings need admin. Try: right-click PowerShell -> Run as Administrator -> re-run this script" }

# =========================================================================
# Step 5: Azure Login
# =========================================================================
Write-Step "5/7" "Azure Login"

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Fail "Azure CLI not found. Install from: https://aka.ms/installazurecli"
    Write-Info "Then re-run this script."
    Write-Host "  Log saved to: $LOG_FILE" -ForegroundColor Gray
    Read-Host "Press Enter to close"
    Stop-Transcript | Out-Null
    exit 1
}

$azLoginSuccess = $false
$userEmail = $null

try {
    $existingAccount = az account show --output json 2>$null | ConvertFrom-Json
    if ($existingAccount -and $existingAccount.user.name) {
        $userEmail = $existingAccount.user.name
        Write-OK "Already signed in as: $userEmail"
        $azLoginSuccess = $true
    }
} catch { }

if (-not $azLoginSuccess) {
    Write-Info "A browser window will open. Sign in, then come back here -- the script will wait."
    for ($attempt = 1; $attempt -le $MAX_AZ_LOGIN_RETRIES; $attempt++) {
        az login 2>&1 | Out-Host
        if ($LASTEXITCODE -eq 0) {
            $userEmail = az account show --query "user.name" -o tsv 2>$null
            $azLoginSuccess = $true
            Write-OK "Signed in as: $userEmail"
            break
        }
        if ($attempt -lt $MAX_AZ_LOGIN_RETRIES) { Write-Info "Retrying..."; Start-Sleep 5 }
    }
}

if (-not $azLoginSuccess -or [string]::IsNullOrWhiteSpace($userEmail)) {
    Write-Fail "Azure login failed. Run 'az login' manually, then re-run this script."
    Write-Host "  Log saved to: $LOG_FILE" -ForegroundColor Gray
    Read-Host "Press Enter to close"
    Stop-Transcript | Out-Null
    exit 1
}

# =========================================================================
# Step 6: Claude Code Login
# =========================================================================
Write-Step "6/7" "Claude Code Login"

$claudeLoginSuccess = $false

# Find claude executable (may have been installed in step 1)
$claudePath = $null
if (Test-Path $claudeExe) { $claudePath = $claudeExe }
elseif (Get-Command claude -ErrorAction SilentlyContinue) { $claudePath = (Get-Command claude).Source }

if (-not $claudePath) {
    Write-Warning2 "Claude Code not found. Install it, then run: claude auth login"
} else {
    # Check if already authenticated via claude auth status
    $authStatus = & $claudePath auth status 2>&1
    if ($authStatus -match '"loggedIn":\s*true') {
        $claudeLoginSuccess = $true
        Write-OK "Already authenticated. Skipping login."
    } else {
        Write-Info "A browser will open for sign-in. Complete it there -- the script will wait."
        & $claudePath auth login 2>&1 | Out-Host
        # Re-verify
        $authStatus = & $claudePath auth status 2>&1
        if ($authStatus -match '"loggedIn":\s*true') { $claudeLoginSuccess = $true; Write-OK "Claude Code login verified." }
        else { Write-Warning2 "Login may not have succeeded. You can retry: claude auth login" }
    }
}

# =========================================================================
# Step 7: Set Env Vars + Register Services + Create Desktop Shortcut
# =========================================================================
Write-Step "7/7" "Starting Shraga Services"

# -- Set environment variables --
$envOk = $true
foreach ($ev in @(
    @{ Name = "USER_EMAIL"; Value = $userEmail },
    @{ Name = "WORKING_DIR"; Value = $WORKING_DIR },
    @{ Name = "WEBHOOK_USER"; Value = $userEmail }
)) {
    try { Set-EnvVar $ev.Name $ev.Value }
    catch { Write-Warning2 "Could not set $($ev.Name): $_"; $envOk = $false }
}
if ($envOk) { Write-OK "Environment variables set (USER_EMAIL=$userEmail)" }
else { Write-Warning2 "Some environment variables may not have been set" }

# -- Create desktop shortcut for re-running this script --
$localScript = Join-Path $WORKING_DIR "setup-devbox.ps1"
if (-not (Test-Path $localScript)) {
    Write-Warning2 "Script not found at $localScript -- skipping desktop shortcut"
} else { try {
    $desktopPath = [System.Environment]::GetFolderPath("Desktop")
    if (-not $desktopPath -or -not (Test-Path $desktopPath)) {
        $desktopPath = Join-Path $env:USERPROFILE "Desktop"
    }
    if (-not (Test-Path $desktopPath)) {
        New-Item -ItemType Directory -Force -Path $desktopPath | Out-Null
    }
    # Point shortcut to run-setup.cmd which does git pull + run script
    $launcherPath = Join-Path $WORKING_DIR "run-setup.cmd"
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut((Join-Path $desktopPath "Shraga Setup.lnk"))
    $sc.TargetPath = $launcherPath
    $sc.WorkingDirectory = $WORKING_DIR
    $sc.Save()
    Write-OK "Desktop shortcut created: Shraga Setup"
} catch {
    Write-Warning2 "Could not create desktop shortcut: $_"
} }

# -- Register and start scheduled tasks --
$pyExe = Find-Python
if ($pyExe -and (Test-Path $WORKER_SCRIPT)) {
    # Use -AtLogOn (works without admin) instead of -AtStartup (needs admin)
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 5)

    # Create a small wrapper .cmd for each service that sets env vars before running Python
    # This ensures the scheduled task always has the right environment
    $services = @(
        @{ Name = "ShragaWorker"; Script = $WORKER_SCRIPT; Label = "Worker"; EnvVars = @{ WEBHOOK_USER = $userEmail; WORKING_DIR = $WORKING_DIR } }
    )
    if (-not $WorkerOnly) {
        $services += @{ Name = "ShragaPM"; Script = $PM_SCRIPT; Label = "PM"; EnvVars = @{ USER_EMAIL = $userEmail; WORKING_DIR = $WORKING_DIR } }
    } else {
        Write-Info "WorkerOnly mode -- skipping PM (runs on your first dev box)"
    }
    foreach ($svc in $services) {
        # Check if this service's script exists
        if (-not (Test-Path $svc.Script)) {
            Write-Warning2 "$($svc.Label) script not found at: $($svc.Script) -- skipping"
            continue
        }

        try {
            # Stop any existing running task BEFORE re-registering
            $existing = Get-ScheduledTask -TaskName $svc.Name -ErrorAction SilentlyContinue
            if ($existing -and $existing.State -eq "Running") {
                Write-Info "Stopping existing $($svc.Label)..."
                Stop-ScheduledTask -TaskName $svc.Name -ErrorAction SilentlyContinue
                Start-Sleep 3
            }

            # Write a wrapper .cmd that sets env vars then runs python
            $wrapperDir = Join-Path $env:LOCALAPPDATA "Shraga"
            if (-not (Test-Path $wrapperDir)) { New-Item -ItemType Directory -Force -Path $wrapperDir | Out-Null }
            $wrapperPath = Join-Path $wrapperDir "$($svc.Name).cmd"
            $envLines = ($svc.EnvVars.GetEnumerator() | ForEach-Object { "set `"$($_.Key)=$($_.Value)`"" }) -join "`r`n"
            $wrapperContent = "@echo off`r`n$envLines`r`ncd /d `"$WORKING_DIR`"`r`n`"$pyExe`" `"$($svc.Script)`"`r`nexit /b %ERRORLEVEL%"
            [System.IO.File]::WriteAllText($wrapperPath, $wrapperContent, [System.Text.Encoding]::ASCII)

            $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$wrapperPath`"" -WorkingDirectory $WORKING_DIR
            Register-ScheduledTask -TaskName $svc.Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
            Start-ScheduledTask -TaskName $svc.Name -ErrorAction Stop
            Start-Sleep 5

            # Verify it's actually running
            $task = Get-ScheduledTask -TaskName $svc.Name -ErrorAction SilentlyContinue
            if ($task.State -eq "Running") { Write-OK "$($svc.Label) registered and running" }
            else { Write-Warning2 "$($svc.Label) registered but state is: $($task.State). Check: Get-ScheduledTask -TaskName $($svc.Name)" }
        } catch {
            Write-Warning2 "Scheduled task failed for $($svc.Label): $_"
            Write-Info "Starting $($svc.Label) directly instead..."
            Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$wrapperPath`"" -WorkingDirectory $WORKING_DIR -WindowStyle Hidden
            Write-Info "$($svc.Label) started (won't survive reboot -- re-run this script as admin)"
        }
    }
} else {
    if (-not $pyExe) { Write-Fail "Python not found -- cannot start services" }
    elseif (-not (Test-Path $WORKER_SCRIPT)) { Write-Fail "Code not deployed at $WORKING_DIR -- clone failed earlier" }
}

# =========================================================================
# Summary
# =========================================================================
# Check actual process state
$workerRunning = (Get-ScheduledTask -TaskName "ShragaWorker" -ErrorAction SilentlyContinue).State -eq "Running"
$pmRunning = if ($WorkerOnly) { $true } else { (Get-ScheduledTask -TaskName "ShragaPM" -ErrorAction SilentlyContinue).State -eq "Running" }

$allGood = $workerRunning -and $pmRunning -and $azLoginSuccess -and $claudeLoginSuccess
$bannerColor = if ($allGood) { "Green" } else { "Yellow" }
$bannerText = if ($allGood) { "Setup Complete!" } else { "Setup Finished (some items need attention)" }

Write-Host ""
Write-Host "================================================" -ForegroundColor $bannerColor
Write-Host "  $bannerText" -ForegroundColor $bannerColor
Write-Host "================================================" -ForegroundColor $bannerColor
Write-Host ""
Write-Host "  Azure:       $userEmail" -ForegroundColor $(if ($azLoginSuccess) { "Green" } else { "Yellow" })
Write-Host "  Claude Code: $(if ($claudeLoginSuccess) { 'Authenticated' } else { 'Needs: claude auth login' })" -ForegroundColor $(if ($claudeLoginSuccess) { "Green" } else { "Yellow" })
Write-Host "  Worker:      $(if ($workerRunning) { 'Running' } else { 'Not running' })" -ForegroundColor $(if ($workerRunning) { "Green" } else { "Yellow" })
if ($WorkerOnly) {
    Write-Host "  PM:          Skipped (WorkerOnly mode)" -ForegroundColor Gray
} else {
    Write-Host "  PM:          $(if ($pmRunning) { 'Running' } else { 'Not running' })" -ForegroundColor $(if ($pmRunning) { "Green" } else { "Yellow" })
}
Write-Host ""
if ($allGood) {
    Write-Host "  Go back to Teams and start sending coding tasks!" -ForegroundColor Cyan
} else {
    Write-Host "  Some items need attention (see yellow items above)." -ForegroundColor Yellow
    Write-Host "  You can re-run this script anytime via the desktop shortcut." -ForegroundColor Gray
}
Write-Host ""
Write-Host "  Log saved to: $LOG_FILE" -ForegroundColor Gray
Write-Host ""

Stop-Transcript | Out-Null
Read-Host "Press Enter to close"
