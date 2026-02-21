# Dev Box Customization for Shraga

This directory contains Dev Box customization files for pre-installing Claude Code and setting up the Shraga development environment.

## Files

- **`devbox-customization.yaml`** - Full customization with Claude Code, Git, Node.js, Python, VS Code, and OneDrive SSO
- **`devbox-customization-minimal.yaml`** - Minimal test with just Claude Code and Git
- **`verify-devbox-setup.ps1`** - Verification script to check installation after provisioning

## How to Use (For Non-Admin Users)

### Option 1: Upload File via Developer Portal

1. Navigate to [Developer Portal](https://aka.ms/devbox-portal)
2. Click **New** → **New dev box**
3. Enter dev box details (name, project, pool)
4. Select **Apply customizations** → **Continue**
5. Select **Upload a customization file(s)** → **Add customizations from file**
6. Browse and select `devbox-customization-minimal.yaml` (for testing) or `devbox-customization.yaml` (for full setup)
7. Click **Validate** to verify the YAML syntax
8. Review the summary and click **Create**

### Option 2: Reference from Repository

1. Commit these files to your Azure DevOps repository
2. In Developer Portal, select **New** → **New dev box**
3. Enter dev box details
4. Select **Apply customizations** → **Continue**
5. Select **Choose a customization file from a repository**
6. Enter the repository URL for your YAML file:
   ```
   https://github.com/ShragaBot/ShragaBot/blob/main/devbox-customization.yaml
   ```
7. Click **Validate** and then **Create**

## What Gets Installed

### Full Version (`devbox-customization.yaml`)

**System Tools:**
- Git (required for Claude Code)
- Visual Studio Code
- Node.js LTS
- Python 3.12

**Claude Code:**
- Installed via WinGet (`Anthropic.ClaudeCode`)
- Automatically added to PATH

**Python Packages:**
- requests
- azure-identity

**Configuration:**
- OneDrive Silent Account Configuration (automatic sign-in)
- Git global settings (autocrlf, default branch)
- Environment variables

**User Tasks (after first sign-in):**
- Clone Shraga worker repository to `C:\Dev\shraga-worker`

### Minimal Version (`devbox-customization-minimal.yaml`)

- Git
- Claude Code
- Basic verification

## After Provisioning

### Step 1: Verify Installation

Connect to your Dev Box and run the verification script:

```powershell
# Download and run verification script
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/verify-devbox-setup.ps1" -OutFile verify-devbox-setup.ps1
.\verify-devbox-setup.ps1
```

Or if you cloned the repository:
```powershell
C:\Dev\shraga-worker\verify-devbox-setup.ps1
```

### Step 2: Authenticate Claude Code

```bash
claude /login
```

This will open your browser for OAuth authentication.

### Step 3: Verify OneDrive SSO

1. Sign out and back in to Windows
2. OneDrive should start syncing automatically (no password prompt)
3. Check shared OneDrive folders are accessible

## Troubleshooting

### Customization Failed

1. Check the customization log in the Developer Portal:
   - View your dev box details
   - Look for customization status and logs

2. Common issues:
   - **WinGet package not found**: Package name may have changed
   - **Timeout**: Increase timeout value in YAML
   - **Permission denied**: Some tasks require admin rights (not available for user customizations)

### OneDrive Not Auto-Signing In

1. Verify registry key:
   ```powershell
   Get-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\OneDrive' -Name 'SilentAccountConfig'
   ```

2. Check if SSO is enabled on the Dev Box pool (admin task)

3. Sign out and back in to refresh the Primary Refresh Token (PRT)

### Claude Code Not Found

```powershell
# Check if Claude Code was installed
Get-Command claude

# If not found, check PATH
$env:PATH

# Manually install
winget install Anthropic.ClaudeCode
```

## Testing the Customization

Since you're not an admin, you can only test **user customizations** (not team customizations that run at system level).

### What You CAN Test:
- ✅ Installing software via WinGet in userTasks section
- ✅ Running PowerShell scripts as your user
- ✅ Validating YAML syntax in Developer Portal

### What You CANNOT Test (Requires Admin):
- ❌ System-level installations (tasks section)
- ❌ Registry changes to HKLM (requires admin)
- ❌ Enabling SSO on Dev Box pools

### Workaround for Testing:

Move all tasks to `userTasks` section for testing:

```yaml
$schema: "1.0"
name: "test-user-customization"

userTasks:
  # These run as your user after sign-in
  - name: powershell
    parameters:
      command: |
        winget install Git.Git
        winget install Anthropic.ClaudeCode
        Write-Host "Installation complete"
```

## Architecture Notes

These customization files are designed to support the Shraga architecture:

1. **Pre-install Claude Code** so workers don't need manual setup
2. **Configure OneDrive SSO** for accessing shared folders (execution outputs)
3. **Install Python + dependencies** for running orchestrator/worker scripts
4. **Clone worker repository** automatically on first sign-in

## References

### Microsoft Documentation
- [Dev Box Customizations Overview](https://learn.microsoft.com/en-us/azure/dev-box/concept-what-are-dev-box-customizations)
- [YAML Schema Reference](https://learn.microsoft.com/en-us/azure/dev-box/reference-dev-box-customizations)
- [Configure User Customizations](https://learn.microsoft.com/en-us/azure/dev-box/how-to-configure-user-customizations)
- [Enable Single Sign-On](https://learn.microsoft.com/en-us/azure/dev-box/how-to-enable-single-sign-on)

### Claude Code Installation
- [Set up Claude Code](https://code.claude.com/docs/en/setup)
- [How to Install Claude Code on Windows 11](https://interworks.com/blog/2026/01/27/how-to-install-claude-code-on-windows-11/)
- [Claude Code Installation Guide](https://claudelog.com/install-claude-code/)
- [WinGet Installation Issue](https://github.com/anthropics/claude-code/issues/11571)

## Next Steps

1. Test the minimal customization file first
2. Verify Claude Code installs successfully
3. Once validated, use the full customization for actual Dev Boxes
4. Work with admin to enable SSO on Dev Box pools for OneDrive integration
5. Integrate into Shraga orchestrator provisioning workflow
