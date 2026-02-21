# Shraga Dev Box Authentication Script
# Handles Azure login, Claude Code login, env var setup, and worker auto-start.
# Designed to be run on a freshly provisioned dev box via the desktop shortcut.

$ErrorActionPreference = "Continue"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
$WORKING_DIR = "C:\Dev\shraga-worker"
$MAX_AZ_LOGIN_RETRIES = 3
$WORKER_SCRIPT = Join-Path $WORKING_DIR "integrated_task_worker.py"

# ---------------------------------------------------------------------------
# Helper: Write colored progress line
# ---------------------------------------------------------------------------
function Write-Step {
    param(
        [string]$StepLabel,
        [string]$Message,
        [string]$Color = "Yellow"
    )
    Write-Host ""
    Write-Host "[$StepLabel] $Message" -ForegroundColor $Color
}

function Write-Success {
    param([string]$Message)
    Write-Host "  [OK] $Message" -ForegroundColor Green
}

function Write-Failure {
    param([string]$Message)
    Write-Host "  [FAIL] $Message" -ForegroundColor Red
}

function Write-Info {
    param([string]$Message)
    Write-Host "  $Message" -ForegroundColor Gray
}

function Write-Warn {
    param([string]$Message)
    Write-Host "  [WARN] $Message" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Shraga Dev Box Authentication" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This script will:" -ForegroundColor White
Write-Host "    1. Sign you into Azure" -ForegroundColor White
Write-Host "    2. Verify Azure authentication" -ForegroundColor White
Write-Host "    3. Sign you into Claude Code" -ForegroundColor White
Write-Host "    4. Set environment variables" -ForegroundColor White
Write-Host "    5. Start the Shraga worker" -ForegroundColor White
Write-Host ""

# =========================================================================
# Step 1: Azure Login with retry logic
# =========================================================================
Write-Step "1/5" "Azure Login" "Yellow"

# Check if already authenticated
$azLoginSuccess = $false
try {
    $existingAccount = az account show --output json 2>$null | ConvertFrom-Json
    if ($existingAccount -and $existingAccount.user.name) {
        Write-Success "Already signed in as: $($existingAccount.user.name)"
        Write-Info "Skipping Azure login (use 'az logout' first to force re-auth)."
        $azLoginSuccess = $true
    }
} catch { }

if (-not $azLoginSuccess) {
    Write-Info "A browser window will open. Sign in with your Microsoft account."
}

for ($attempt = 1; $attempt -le $MAX_AZ_LOGIN_RETRIES -and -not $azLoginSuccess; $attempt++) {
    Write-Info "Attempt $attempt of $MAX_AZ_LOGIN_RETRIES..."

    try {
        # Capture stderr to detect 'no subscription found' warning
        $azOutput = az login 2>&1
        $azExitCode = $LASTEXITCODE

        # Convert output to string for inspection
        $azOutputStr = ($azOutput | Out-String)

        if ($azExitCode -eq 0) {
            # Check for 'no subscription found' warning - this is non-fatal
            if ($azOutputStr -match "No subscriptions found" -or $azOutputStr -match "no subscription") {
                Write-Warn "Azure login succeeded but no subscriptions were found."
                Write-Warn "This is OK for dev box authentication - continuing."
            }
            $azLoginSuccess = $true
            Write-Success "Azure login completed."
            break
        } else {
            Write-Failure "Azure login failed (exit code: $azExitCode)."
            if ($attempt -lt $MAX_AZ_LOGIN_RETRIES) {
                Write-Info "Retrying in 5 seconds..."
                Start-Sleep -Seconds 5
            }
        }
    } catch {
        Write-Failure "Azure login encountered an error: $_"
        if ($attempt -lt $MAX_AZ_LOGIN_RETRIES) {
            Write-Info "Retrying in 5 seconds..."
            Start-Sleep -Seconds 5
        }
    }
}

if (-not $azLoginSuccess) {
    Write-Failure "Azure login failed after $MAX_AZ_LOGIN_RETRIES attempts."
    Write-Host ""
    Write-Host "  Troubleshooting tips:" -ForegroundColor Yellow
    Write-Host "    - Ensure you have network connectivity" -ForegroundColor White
    Write-Host "    - Try running 'az login' manually in a terminal" -ForegroundColor White
    Write-Host "    - Check if your account has the required permissions" -ForegroundColor White
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

# =========================================================================
# Step 2: Verify Azure authentication
# =========================================================================
Write-Step "2/5" "Verifying Azure Authentication" "Yellow"

$userEmail = $null
try {
    $accountInfo = az account show --output json 2>&1
    $accountExitCode = $LASTEXITCODE

    if ($accountExitCode -eq 0) {
        $accountObj = $accountInfo | ConvertFrom-Json
        $userEmail = $accountObj.user.name
        $subscriptionName = $accountObj.name
        $tenantId = $accountObj.tenantId

        Write-Success "Authentication verified."
        Write-Info "Signed in as: $userEmail"
        if ($subscriptionName) {
            Write-Info "Subscription: $subscriptionName"
        }
        Write-Info "Tenant: $tenantId"
    } else {
        Write-Failure "Could not verify Azure authentication."
        Write-Info "az account show returned exit code: $accountExitCode"
        Write-Host ""
        Read-Host "Press Enter to close"
        exit 1
    }
} catch {
    Write-Failure "Error verifying Azure authentication: $_"
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

# If email is still empty, prompt the user
if ([string]::IsNullOrWhiteSpace($userEmail)) {
    Write-Warn "Could not determine user email from Azure account."
    $userEmail = Read-Host "  Please enter your email address"
    if ([string]::IsNullOrWhiteSpace($userEmail)) {
        Write-Failure "Email address is required."
        Read-Host "Press Enter to close"
        exit 1
    }
}

# =========================================================================
# Step 3: Claude Code Login with error handling
# =========================================================================
Write-Step "3/5" "Claude Code Login" "Yellow"
Write-Info "Starting Claude Code authentication..."

# Ensure Claude Code is in PATH (native installer puts it in .local\bin)
$claudeLocalBin = Join-Path $env:USERPROFILE ".local\bin"
if ((Test-Path (Join-Path $claudeLocalBin "claude.exe")) -and ($env:Path -notlike "*$claudeLocalBin*")) {
    $env:Path += ";$claudeLocalBin"
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$claudeLocalBin*") {
        [System.Environment]::SetEnvironmentVariable("Path", "$userPath;$claudeLocalBin", "User")
        Write-Info "Added Claude Code to PATH"
    }
}

$claudeLoginSuccess = $false
try {
    $proc = Start-Process -FilePath "claude" -ArgumentList "/login" -Wait -PassThru -NoNewWindow
    $claudeExitCode = $proc.ExitCode

    if ($claudeExitCode -eq 0) {
        $claudeLoginSuccess = $true
        Write-Success "Claude Code login completed."
    } else {
        Write-Failure "Claude Code login returned exit code: $claudeExitCode"
    }
} catch {
    Write-Failure "Claude Code login encountered an error: $_"
}

if (-not $claudeLoginSuccess) {
    Write-Warn "Claude Code login may not have succeeded."
    Write-Info "You can retry later by running: claude /login"
    Write-Info "Continuing with remaining setup steps..."
}

# =========================================================================
# Step 4: Set Environment Variables
# =========================================================================
Write-Step "4/5" "Setting Environment Variables" "Yellow"

# Set USER_EMAIL (machine-level so it persists across sessions)
try {
    [System.Environment]::SetEnvironmentVariable("USER_EMAIL", $userEmail, "Machine")
    # Also set for current session
    $env:USER_EMAIL = $userEmail
    Write-Success "USER_EMAIL set to: $userEmail"
} catch {
    Write-Warn "Could not set machine-level USER_EMAIL (may need admin): $_"
    # Fall back to user-level
    try {
        [System.Environment]::SetEnvironmentVariable("USER_EMAIL", $userEmail, "User")
        $env:USER_EMAIL = $userEmail
        Write-Success "USER_EMAIL set (user-level) to: $userEmail"
    } catch {
        Write-Failure "Could not set USER_EMAIL: $_"
    }
}

# Set WORKING_DIR
try {
    [System.Environment]::SetEnvironmentVariable("WORKING_DIR", $WORKING_DIR, "Machine")
    $env:WORKING_DIR = $WORKING_DIR
    Write-Success "WORKING_DIR set to: $WORKING_DIR"
} catch {
    Write-Warn "Could not set machine-level WORKING_DIR (may need admin): $_"
    try {
        [System.Environment]::SetEnvironmentVariable("WORKING_DIR", $WORKING_DIR, "User")
        $env:WORKING_DIR = $WORKING_DIR
        Write-Success "WORKING_DIR set (user-level) to: $WORKING_DIR"
    } catch {
        Write-Failure "Could not set WORKING_DIR: $_"
    }
}

# Set WEBHOOK_USER to the same email for worker compatibility
try {
    [System.Environment]::SetEnvironmentVariable("WEBHOOK_USER", $userEmail, "Machine")
    $env:WEBHOOK_USER = $userEmail
    Write-Success "WEBHOOK_USER set to: $userEmail"
} catch {
    Write-Warn "Could not set machine-level WEBHOOK_USER: $_"
    try {
        [System.Environment]::SetEnvironmentVariable("WEBHOOK_USER", $userEmail, "User")
        $env:WEBHOOK_USER = $userEmail
        Write-Success "WEBHOOK_USER set (user-level) to: $userEmail"
    } catch {
        Write-Failure "Could not set WEBHOOK_USER: $_"
    }
}

# =========================================================================
# Step 5: Auto-start Worker
# =========================================================================
Write-Step "5/5" "Starting Shraga Worker and PM" "Yellow"

$PM_SCRIPT = Join-Path $WORKING_DIR "task-manager\task_manager.py"

foreach ($taskInfo in @(
    @{ Name = "ShragaWorker"; Script = $WORKER_SCRIPT; Label = "Worker" },
    @{ Name = "ShragaPM"; Script = $PM_SCRIPT; Label = "PM" }
)) {
    $taskName = $taskInfo.Name
    $script = $taskInfo.Script
    $label = $taskInfo.Label

    if (Test-Path $script) {
        try {
            $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if ($existingTask -and $existingTask.State -eq "Running") {
                Write-Info "Restarting $label scheduled task..."
                Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 2
            }
            if ($existingTask) {
                Start-ScheduledTask -TaskName $taskName
                Write-Success "$label started via scheduled task."
            } else {
                Write-Info "No scheduled task for $label. Starting directly..."
                $pyCandidates = @(
                    "C:\Python312\python.exe",
                    "C:\ProgramData\chocolatey\lib\python312\tools\python.exe",
                    "C:\ProgramData\chocolatey\bin\python3.exe",
                    "C:\ProgramData\chocolatey\bin\python.exe"
                )
                $pythonExe = "python"
                foreach ($c in $pyCandidates) {
                    if (Test-Path $c) { $pythonExe = $c; break }
                }
                Start-Process -FilePath $pythonExe `
                    -ArgumentList $script `
                    -WorkingDirectory $WORKING_DIR `
                    -WindowStyle Hidden
                Write-Success "$label started directly."
            }
        } catch {
            Write-Warn "Could not auto-start ${label}: $_"
        }
    } else {
        Write-Warn "$label script not found at: $script"
    }
}

if (-not (Test-Path $WORKER_SCRIPT) -and -not (Test-Path $PM_SCRIPT)) {
    Write-Info "Try running: git clone https://github.com/ShragaBot/ShragaBot.git $WORKING_DIR"
}

# =========================================================================
# Summary
# =========================================================================
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Authentication Complete" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Summary:" -ForegroundColor White
Write-Host "    Azure:       Signed in as $userEmail" -ForegroundColor $(if ($azLoginSuccess) { "Green" } else { "Red" })
Write-Host "    Claude Code: $(if ($claudeLoginSuccess) { 'Authenticated' } else { 'Needs manual login (claude /login)' })" -ForegroundColor $(if ($claudeLoginSuccess) { "Green" } else { "Yellow" })
Write-Host "    USER_EMAIL:  $env:USER_EMAIL" -ForegroundColor Green
Write-Host "    WORKING_DIR: $env:WORKING_DIR" -ForegroundColor Green
Write-Host "    Worker:      $(if (Test-Path $WORKER_SCRIPT) { 'Started' } else { 'Not deployed' })" -ForegroundColor $(if (Test-Path $WORKER_SCRIPT) { "Green" } else { "Yellow" })
Write-Host ""
Read-Host "Press Enter to close"
