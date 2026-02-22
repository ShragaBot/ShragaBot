# Shraga

Teams bot where developers request coding tasks via chat. Tasks execute on personal Azure Dev Boxes via Claude Code. Real-time progress via Adaptive Cards in Teams.

## Architecture

Three agents, each a persistent Claude Code session:

| Agent | Location | Model | Role |
|-------|----------|-------|------|
| **Global Manager (GM)** | `global-manager/` | Haiku | Onboards new users, guides setup |
| **Personal Manager (PM)** | `task-manager/` | Sonnet | Task creation, status, cancellation |
| **Worker** | `integrated_task_worker.py` | Opus | Executes coding tasks via Worker/Verifier loop |

## Setup

**New dev box (first time):**
1. Run `setup.ps1` on your machine to provision a dev box (~25 min)
2. RDP into the box via the web URL shown
3. On the box: `irm https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/setup-devbox.ps1 | iex`

**Additional dev box (Worker only):**
`irm https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/setup-workerbox.ps1 | iex`

## Deployment

Code is deployed as **immutable releases** -- plain file copies (no .git) under `C:\Dev\Shraga\releases\vN\`.

### How to deploy

1. **Commit to `main`** -- develop and test on main
2. **Create a release** -- go to GitHub Actions > "Create Release" > Run workflow. This auto-creates `release/vN+1` from main.
3. **Wait** -- the updater on each dev box checks every 5 minutes. It detects the new branch, downloads it as a zip, deploys, and updates `current_version.txt`. Services detect the version change and restart automatically.

Or from the CLI:
```bash
git checkout -b release/vN main
git push origin release/vN
```

### How it works

```
GitHub: release/v3 branch created
          |
          v  (within 5 min)
updater.py: git ls-remote detects new branch
          |
          v
Downloads zip -> extracts to C:\Dev\Shraga\releases\v3\
          |
          v
Installs pip deps -> writes .deploy_complete sentinel
          |
          v
Updates current_version.txt to "v3"
          |
          v
Worker/PM: version_check.should_exit() returns True
          |
          v
Services exit gracefully -> watchdog restarts via .cmd wrappers
          |
          v
.cmd wrappers read current_version.txt -> launch from v3 folder
```

### Rollback

Edit `C:\Dev\Shraga\current_version.txt` to the previous version (e.g., `v2`). Services restart from that folder on next watchdog cycle.

### Verify deployment

On a dev box:
```powershell
Get-Content C:\Dev\Shraga\current_version.txt
Get-ChildItem C:\Dev\Shraga\releases
Get-ScheduledTask | Where-Object { $_.TaskName -like "Shraga*" } | Format-Table TaskName,State
```

## Key Files

| File | Description |
|------|-------------|
| `setup.ps1` | Provisions a bare dev box (runs on user's machine) |
| `setup-devbox.ps1` | All-in-one on-box setup: tools, code, auth, services |
| `setup-workerbox.ps1` | Additional dev box setup (Worker only) |
| `updater.py` | Release updater (checks GitHub every 5 min) |
| `version_check.py` | Version comparison for graceful service restarts |
| `integrated_task_worker.py` | Worker entry point |
| `autonomous_agent.py` | Worker/Verifier/Summarizer loop |
| `task-manager/task_manager.py` | PM entry point |
| `global-manager/global_manager.py` | GM entry point |
| `scripts/` | Standalone Dataverse/DevCenter utility scripts |

## Testing

```bash
python -m pytest -x -q
```
