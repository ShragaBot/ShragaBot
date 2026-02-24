# Shraga Dev Box Setup Script (runs ON the dev box)
# Installs tools, clones code, authenticates, configures, starts services.
# Idempotent -- safe to re-run at any time.
#
# First box:      irm https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/setup-devbox.ps1 | iex
# Additional box: irm https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/setup-workerbox.ps1 | iex
# Re-run:         Same command again (idempotent)

param(
    [switch]$WorkerOnly  # Skip PM -- used by setup-workerbox.ps1
)

$ErrorActionPreference = "Continue"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
$SHRAGA_ROOT = "C:\Dev\Shraga"
$RELEASES_DIR = Join-Path $SHRAGA_ROOT "releases"
$VERSION_FILE = Join-Path $SHRAGA_ROOT "current_version.txt"
$REPO_URL = "https://github.com/ShragaBot/ShragaBot.git"
$MAX_AZ_LOGIN_RETRIES = 3
$LOG_FILE = Join-Path $env:TEMP "shraga-setup.log"
# INITIAL_VERSION, WORKING_DIR, WORKER_SCRIPT, PM_SCRIPT set after Git is installed (need git ls-remote)

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
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:USERPROFILE "AppData\Local\Programs\Python\Python312\python.exe"),
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
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
Write-Host "    1. Install tools (Git, Python, Node.js, Claude Code)" -ForegroundColor White
Write-Host "    2. Clone the Shraga code repository" -ForegroundColor White
Write-Host "    3. Install Python dependencies" -ForegroundColor White
Write-Host "    4. Configure sleep/hibernation prevention" -ForegroundColor White
Write-Host "    5. Sign you into Azure (browser opens)" -ForegroundColor White
Write-Host "    6. Sign you into Claude Code (window opens)" -ForegroundColor White
Write-Host "    7. Set up FE repo npm auth (for frontend tasks)" -ForegroundColor White
Write-Host "    8. Start Shraga services" -ForegroundColor White
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
        Start-Sleep -Seconds 3
        Refresh-Path
        $pyExe = Find-Python
    }
    if (-not $pyExe) {
        Write-Info "Installing Python 3.12 from python.org... (this may take a minute)"
        $pyInstaller = Join-Path $env:TEMP "python-3.12.9-amd64.exe"
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe" -OutFile $pyInstaller -ErrorAction SilentlyContinue
        if (Test-Path $pyInstaller) {
            Start-Process $pyInstaller -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1" -Wait
            Refresh-Path
            $pyExe = Find-Python
        }
    }
    if ($pyExe) { Write-OK "Python installed: $pyExe" }
    else { Write-Fail "Python install failed. Install from: https://python.org/downloads" }
}

# -- Node.js (needed for FE repo tasks) --
$nvmDir = Join-Path $env:APPDATA "nvm"
$nvmExe = Join-Path $nvmDir "nvm.exe"
if (Get-Command node -ErrorAction SilentlyContinue) {
    Write-OK "Node.js already installed: $(node --version)"
} elseif (Test-Path $nvmExe) {
    Write-Info "nvm found, installing Node 20..."
    & $nvmExe install 20 2>&1 | Out-Null
    & $nvmExe use 20 2>&1 | Out-Null
    Refresh-Path
    if (Get-Command node -ErrorAction SilentlyContinue) { Write-OK "Node.js installed via nvm: $(node --version)" }
    else { Write-Warning2 "Node.js install via nvm failed. Run: nvm install 20 && nvm use 20" }
} else {
    # Direct download -- no winget/nvm needed
    Write-Info "Installing Node.js 20 LTS... (direct download)"
    $nodeZip = Join-Path $env:TEMP "node-v20.18.3-win-x64.zip"
    $nodeDir = Join-Path $env:LOCALAPPDATA "nodejs"
    Invoke-WebRequest -Uri "https://nodejs.org/dist/v20.18.3/node-v20.18.3-win-x64.zip" -OutFile $nodeZip -ErrorAction SilentlyContinue
    if (Test-Path $nodeZip) {
        if (Test-Path $nodeDir) { Remove-Item -Recurse -Force $nodeDir -ErrorAction SilentlyContinue }
        Expand-Archive -Path $nodeZip -DestinationPath $env:LOCALAPPDATA -Force
        Rename-Item (Join-Path $env:LOCALAPPDATA "node-v20.18.3-win-x64") $nodeDir -ErrorAction SilentlyContinue
        Ensure-InPath $nodeDir
        Remove-Item $nodeZip -Force -ErrorAction SilentlyContinue
        Refresh-Path
        if (Get-Command node -ErrorAction SilentlyContinue) { Write-OK "Node.js installed: $(node --version)" }
        else { Write-Warning2 "Node.js install failed. Install manually from https://nodejs.org" }
    } else {
        Write-Warning2 "Node.js download failed. Install manually from https://nodejs.org"
    }
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
# Step 2: Deploy Release
# =========================================================================
Write-Step "2/7" "Deploying Code"

# Detect latest release branch from GitHub
Write-Info "Checking for latest release..."
$latestVersion = $null
$lsRemoteOutput = git ls-remote --heads $REPO_URL "release/v*" 2>&1
if ($LASTEXITCODE -eq 0 -and $lsRemoteOutput) {
    $versions = @()
    foreach ($line in ($lsRemoteOutput -split "`n")) {
        if ($line -match 'refs/heads/release/v(\d+)') { $versions += [int]$Matches[1] }
    }
    if ($versions.Count -gt 0) {
        $latestVersion = "v$($versions | Sort-Object -Descending | Select-Object -First 1)"
    }
}
if (-not $latestVersion) {
    Write-Warning2 "Could not detect latest release. Falling back to v1."
    $latestVersion = "v1"
}
Write-Info "Latest release: $latestVersion"

$WORKING_DIR = Join-Path $RELEASES_DIR $latestVersion
$WORKER_SCRIPT = Join-Path $WORKING_DIR "integrated_task_worker.py"
$PM_SCRIPT = Join-Path $WORKING_DIR "task-manager\task_manager.py"

New-Item -ItemType Directory -Force -Path $RELEASES_DIR -ErrorAction SilentlyContinue | Out-Null

$sentinel = Join-Path $WORKING_DIR ".deploy_complete"
if (Test-Path $sentinel) {
    Write-OK "Release $latestVersion already deployed"
} else {
    # Download zip from GitHub (no git clone -- release folders are plain files)
    Write-Info "Downloading release/$latestVersion..."
    $zipUrl = "https://github.com/ShragaBot/ShragaBot/archive/refs/heads/release/$latestVersion.zip"
    $zipPath = Join-Path $env:TEMP "shraga-$latestVersion.zip"
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -ErrorAction SilentlyContinue
    if (Test-Path $zipPath) {
        $extractDir = Join-Path $env:TEMP "shraga-extract-$latestVersion"
        if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }
        Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
        # GitHub zips have a top-level folder like ShragaBot-release-v1/
        $inner = Get-ChildItem $extractDir | Select-Object -First 1
        if ($inner -and (Test-Path $WORKING_DIR)) { Remove-Item -Recurse -Force $WORKING_DIR }
        Move-Item $inner.FullName $WORKING_DIR
        # Clean up
        Remove-Item -Force $zipPath -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $extractDir -ErrorAction SilentlyContinue
        # Mark as complete
        $latestVersion | Set-Content $sentinel -NoNewline
        Write-OK "Release $latestVersion deployed to $WORKING_DIR"
    } else {
        Write-Fail "Download failed. Check network."
    }
}

# Write current version file (only if deploy succeeded)
if (Test-Path $sentinel) {
    $latestVersion | Set-Content $VERSION_FILE -NoNewline
}
Write-Info "Version set to: $latestVersion"

# Create updater .cmd wrapper (same pattern as Worker/PM -- reads current_version.txt dynamically)
$updaterWrapperDir = Join-Path $env:LOCALAPPDATA "Shraga"
if (-not (Test-Path $updaterWrapperDir)) { New-Item -ItemType Directory -Force -Path $updaterWrapperDir | Out-Null }
$updaterWrapperPath = Join-Path $updaterWrapperDir "ShragaUpdater.cmd"
$updaterWrapperContent = "@echo off`r`nset /p VERSION=<`"$VERSION_FILE`"`r`nif `"%VERSION%`"==`"`" (exit /b 1)`r`nset `"RELEASE_DIR=$RELEASES_DIR\%VERSION%`"`r`nif not exist `"%RELEASE_DIR%`" (exit /b 1)`r`n`"$pyExe`" `"%RELEASE_DIR%\updater.py`"`r`nexit /b %ERRORLEVEL%"
[System.IO.File]::WriteAllText($updaterWrapperPath, $updaterWrapperContent, [System.Text.Encoding]::ASCII)

if (-not (Test-Path $sentinel)) {
    Write-Fail "Code not available. Cannot continue."
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
    # Ensure pip is available (some installs like choco don't include it)
    & $pyExe -m ensurepip --upgrade 2>&1 | Out-Null
    & $pyExe -m pip install --quiet --upgrade -r (Join-Path $WORKING_DIR "requirements.txt") 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-OK "Dependencies installed" }
    else { Write-Warning2 "Some dependencies may have failed. Run: $pyExe -m pip install -r $WORKING_DIR\requirements.txt" }
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
        # Capture all output to prevent red stderr warnings about subscriptions
        $azOutput = az login 2>&1
        if ($LASTEXITCODE -eq 0) {
            $userEmail = az account show --query "user.name" -o tsv 2>$null
            $azLoginSuccess = $true
            Write-OK "Signed in as: $userEmail"
            break
        }
        # Only show output on failure (as gray info, not red)
        $azOutput | ForEach-Object { Write-Info "$_" }
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
        Write-Info "Opening Claude Code login in a separate window..."
        Write-Info "Complete the login there (browser or device code), then press Enter here when done."
        # Open claude auth login in a separate console window so it can handle interactive flows
        Start-Process -FilePath $claudePath -ArgumentList "auth", "login" -Wait:$false
        Write-Host ""
        Read-Host "  Press Enter after you finish the Claude login in the other window"
        # Verify login succeeded
        $authStatus = & $claudePath auth status 2>&1
        if ($authStatus -match '"loggedIn":\s*true') {
            $claudeLoginSuccess = $true
            Write-OK "Claude Code login verified."
        } else {
            Write-Warning2 "Login not detected. You can retry: claude auth login"
        }
    }
}

# =========================================================================
# Step 7: FE Repo npm Auth (for frontend tasks)
# =========================================================================
Write-Step "7/8" "Setting up FE repo npm auth"

$feRepoRoot = "Q:\src\power-platform-ux"
if (-not (Test-Path $feRepoRoot)) {
    Write-Info "FE repo not found at $feRepoRoot -- skipping npm auth"
    Write-Info "Workers can still handle non-FE tasks"
} elseif (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Warning2 "npm not available -- skipping FE repo auth. Install Node.js first."
} else {
    # Check if npm auth is already configured (try a dry-run npm view against the private feed)
    $npmrcPath = Join-Path $feRepoRoot "common\config\rush\.npmrc"
    $userNpmrc = Join-Path $env:USERPROFILE ".npmrc"
    $authConfigured = $false
    if (Test-Path $userNpmrc) {
        $npmrcContent = Get-Content $userNpmrc -Raw -ErrorAction SilentlyContinue
        if ($npmrcContent -match "pkgs.dev.azure.com.*_password") { $authConfigured = $true }
    }

    if ($authConfigured) {
        Write-OK "npm Azure DevOps auth already configured"
    } else {
        Write-Info "Authenticating npm for Azure DevOps feeds..."
        Write-Info "A browser popup may appear -- sign in with your Microsoft account."
        try {
            # Install vsts-npm-auth globally and run directly against the rush .npmrc.
            # This avoids the chicken-and-egg problem where 'npm run renew' needs
            # node_modules (from rush update) which needs npm auth to download.
            # -F forces re-auth, -E 259200 sets token expiry to 180 days.
            if (-not (Get-Command vsts-npm-auth -ErrorAction SilentlyContinue)) {
                Write-Info "Installing vsts-npm-auth..."
                npm install -g vsts-npm-auth 2>&1 | Out-Null
            }
            vsts-npm-auth -config $npmrcPath -F -E 259200 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-OK "npm Azure DevOps auth configured (valid ~180 days)"
            } else {
                Write-Warning2 "npm auth failed. Workers won't be able to build FE code."
                Write-Info "Fix manually: npm install -g vsts-npm-auth && vsts-npm-auth -config $npmrcPath -F"
            }
        } catch {
            Write-Warning2 "npm auth setup error: $_"
            Write-Info "Fix manually: npm install -g vsts-npm-auth && vsts-npm-auth -config $npmrcPath -F"
        }
    }
}

# =========================================================================
# Step 8: Set Env Vars + Register Services + Create Desktop Shortcut
# =========================================================================
Write-Step "8/8" "Starting Shraga Services"

# -- Set environment variables --
$envOk = $true
foreach ($ev in @(
    @{ Name = "USER_EMAIL"; Value = $userEmail },
    @{ Name = "WORKING_DIR"; Value = $WORKING_DIR },
    @{ Name = "WEBHOOK_USER"; Value = $userEmail },
    @{ Name = "SHRAGA_ROOT"; Value = $SHRAGA_ROOT }
)) {
    try { Set-EnvVar $ev.Name $ev.Value }
    catch { Write-Warning2 "Could not set $($ev.Name): $_"; $envOk = $false }
}
if ($envOk) { Write-OK "Environment variables set (USER_EMAIL=$userEmail)" }
else { Write-Warning2 "Some environment variables may not have been set" }


# -- Register and start scheduled tasks --
$pyExe = Find-Python
if ($pyExe -and (Test-Path $WORKER_SCRIPT)) {
    # Two triggers: AtLogOn + repeating every 5 min as watchdog
    # Task Scheduler won't start a duplicate if already running (IgnoreNew default)
    $triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $triggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $triggers = @($triggerLogon, $triggerRepeat)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew

    # Create a small wrapper .cmd for each service that sets env vars before running Python
    # This ensures the scheduled task always has the right environment
    $services = @(
        @{ Name = "ShragaWorker"; Script = $WORKER_SCRIPT; Label = "Worker"; EnvVars = @{ WEBHOOK_USER = $userEmail } }
    )
    if (-not $WorkerOnly) {
        $services += @{ Name = "ShragaPM"; Script = $PM_SCRIPT; Label = "PM"; EnvVars = @{ USER_EMAIL = $userEmail } }
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

            # Write a wrapper .cmd that reads current_version.txt and runs from the right release folder
            $wrapperDir = Join-Path $env:LOCALAPPDATA "Shraga"
            if (-not (Test-Path $wrapperDir)) { New-Item -ItemType Directory -Force -Path $wrapperDir | Out-Null }
            $wrapperPath = Join-Path $wrapperDir "$($svc.Name).cmd"
            $envLines = ($svc.EnvVars.GetEnumerator() | ForEach-Object { "set `"$($_.Key)=$($_.Value)`"" }) -join "`r`n"
            # Script path relative to release root (e.g., integrated_task_worker.py or task-manager\task_manager.py)
            $relScript = $svc.Script.Replace($WORKING_DIR + "\", "")
            $wrapperContent = "@echo off`r`n$envLines`r`nset /p VERSION=<`"$VERSION_FILE`"`r`nif `"%VERSION%`"==`"`" (echo [ERROR] current_version.txt missing or empty & exit /b 1)`r`nset `"RELEASE_DIR=$RELEASES_DIR\%VERSION%`"`r`nif not exist `"%RELEASE_DIR%`" (echo [ERROR] Release dir not found: %RELEASE_DIR% & exit /b 1)`r`nset `"WORKING_DIR=%RELEASE_DIR%`"`r`ncd /d `"%RELEASE_DIR%`"`r`n`"$pyExe`" `"%RELEASE_DIR%\$relScript`"`r`nexit /b %ERRORLEVEL%"
            [System.IO.File]::WriteAllText($wrapperPath, $wrapperContent, [System.Text.Encoding]::ASCII)

            $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$wrapperPath`"" -WorkingDirectory $SHRAGA_ROOT
            Register-ScheduledTask -TaskName $svc.Name -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
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

# -- Register updater task (checks for new releases every 5 min) --
if (Test-Path $updaterWrapperPath) {
    try {
        $updaterAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$updaterWrapperPath`"" -WorkingDirectory $SHRAGA_ROOT
        $updaterTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
        Register-ScheduledTask -TaskName "ShragaUpdater" -Action $updaterAction -Trigger $updaterTrigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
        Start-ScheduledTask -TaskName "ShragaUpdater" -ErrorAction SilentlyContinue
        Write-OK "Updater registered (checks every 5 min)"
    } catch {
        Write-Warning2 "Could not register updater: $_"
    }
}

# =========================================================================
# Register box in Dataverse (crb3b_shragaboxes)
# =========================================================================
Write-Step "9" "Registering box in Dataverse..."
if ($azLoginSuccess -and -not [string]::IsNullOrWhiteSpace($userEmail)) {
    try {
        $dvToken = (az account get-access-token --resource "https://org3e79cdb1.crm3.dynamics.com" --query "accessToken" -o tsv 2>$null)
        if ($dvToken) {
            $boxName = if ($WorkerOnly) { "$env:COMPUTERNAME-worker" } else { "$env:COMPUTERNAME" }
            $boxType = if ($WorkerOnly) { "worker" } else { "primary" }
            $currentVer = if (Test-Path $VERSION_FILE) { Get-Content $VERSION_FILE -Raw | ForEach-Object { $_.Trim() } } else { "unknown" }

            $regBody = @{
                crb3b_boxname = $boxName
                crb3b_hostname = $env:COMPUTERNAME
                crb3b_useremail = $userEmail
                crb3b_boxtype = $boxType
                crb3b_boxstatus = "active"
                crb3b_version = $currentVer
            } | ConvertTo-Json -Compress

            $headers = @{
                "Authorization" = "Bearer $dvToken"
                "Content-Type" = "application/json"
            }

            # Check if box already registered
            $existingUrl = "https://org3e79cdb1.crm3.dynamics.com/api/data/v9.2/crb3b_shragaboxes?`$filter=crb3b_hostname eq '$($env:COMPUTERNAME)'&`$select=crb3b_shragaboxid"
            $existing = Invoke-RestMethod -Uri $existingUrl -Headers $headers -Method Get -ErrorAction Stop
            if ($existing.value.Count -gt 0) {
                # Update existing row
                $rowId = $existing.value[0].crb3b_shragaboxid
                $patchUrl = "https://org3e79cdb1.crm3.dynamics.com/api/data/v9.2/crb3b_shragaboxes($rowId)"
                Invoke-RestMethod -Uri $patchUrl -Headers $headers -Method Patch -Body $regBody -ErrorAction Stop | Out-Null
                Write-OK "Updated box registration: $boxName ($boxType)"
            } else {
                # Create new row
                $postUrl = "https://org3e79cdb1.crm3.dynamics.com/api/data/v9.2/crb3b_shragaboxes"
                Invoke-RestMethod -Uri $postUrl -Headers $headers -Method Post -Body $regBody -ErrorAction Stop | Out-Null
                Write-OK "Registered new box: $boxName ($boxType)"
            }
        } else {
            Write-Warning2 "Could not get DV token for box registration"
        }
    } catch {
        Write-Warning2 "Box registration failed: $_"
    }
} else {
    Write-Warning2 "Skipping box registration (no Azure login)"
}

# =========================================================================
# Summary
# =========================================================================
# Check actual process state
$workerRunning = (Get-ScheduledTask -TaskName "ShragaWorker" -ErrorAction SilentlyContinue).State -eq "Running"
$pmRunning = if ($WorkerOnly) { $true } else { (Get-ScheduledTask -TaskName "ShragaPM" -ErrorAction SilentlyContinue).State -eq "Running" }

$npmAuthOk = $false
$userNpmrc = Join-Path $env:USERPROFILE ".npmrc"
if ((Test-Path $userNpmrc) -and ((Get-Content $userNpmrc -Raw -ErrorAction SilentlyContinue) -match "pkgs.dev.azure.com.*_password")) { $npmAuthOk = $true }

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
Write-Host "  npm auth:    $(if ($npmAuthOk) { 'Configured (FE tasks OK)' } else { 'Not configured (FE tasks will fail)' })" -ForegroundColor $(if ($npmAuthOk) { "Green" } else { "Yellow" })
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
