# Development & Deployment Guide

## How We Work

All development happens on the `main` branch. Push directly to main -- no feature branches, no PRs required.

```bash
# Make changes, run tests, push
python -m pytest -x -q
git add <files>
git commit -m "Description of change"
git push
```

## How to Deploy

When you're ready to deploy to dev boxes, trigger the release workflow:

```bash
gh workflow run release.yml --ref main
```

This automatically:
1. Finds the highest existing `release/vN` branch
2. Creates `release/v(N+1)` from main
3. Dev boxes detect the new branch within 5 minutes (ShragaUpdater scheduled task)
4. Services restart automatically with the new code

**Do NOT manually create release branches.** Always use the workflow. Release branches (`release/v*`) are protected by a GitHub ruleset -- only the workflow can create them, using an admin PAT stored as the `RELEASE_PAT` repo secret.

## How Dev Boxes Update

1. **ShragaUpdater** (scheduled task, every 5 min) runs `updater.py`
2. `updater.py` checks GitHub for `release/v*` branches via `git ls-remote --heads`
3. If a higher version exists, downloads as zip, extracts to `C:\Dev\Shraga\releases\vN\`
4. Updates `C:\Dev\Shraga\current_version.txt`
5. Worker/PM detect version mismatch on next poll cycle, exit gracefully
6. Watchdog (scheduled task) restarts them from the new release folder via `.cmd` wrappers

## Running Tests

```bash
cd Q:/repos/shraga-worker
python -m pytest -x -q
```

All tests must pass before pushing. Currently 651+ tests.

## Key Paths

| What | Where |
|------|-------|
| Source repo (local) | `Q:\repos\shraga-worker` |
| Source repo (GitHub) | `github.com/ShragaBot/ShragaBot` |
| Dev box releases | `C:\Dev\Shraga\releases\vN\` |
| Current version | `C:\Dev\Shraga\current_version.txt` |
| Service wrappers | `%LOCALAPPDATA%\Shraga\ShragaWorker.cmd` etc. |

## Checking Dev Box Status

On the dev box:

```powershell
# Version and service status
Get-Content C:\Dev\Shraga\current_version.txt; Get-ScheduledTask ShragaWorker,ShragaPM | Select TaskName,State | Format-Table

# Force updater to run now
Start-ScheduledTask -TaskName ShragaUpdater

# Check Worker logs
Get-Content $env:TEMP\shraga-worker.log -Tail 50
```

## Deploying Power Automate Flows

All 7 Power Automate flows are managed by `deploy_flows.py` (in the session folder). This is the single source of truth for flow definitions.

```bash
# Deploy all 7 flows
python Q:/sessions/shragaTest01/deploy_flows.py all

# Deploy specific flows
python Q:/sessions/shragaTest01/deploy_flows.py TaskRunner TaskCompleted

# After deploying, commit the updated flow JSONs
cd Q:/repos/shraga-worker && git add flows/ && git commit -m "Deploy flow updates" && git push
```

**NEVER patch flows directly via the Flow API.** Always update `deploy_flows.py` or the `flows/*.json` templates, then run the script.

Flows managed:
| Flow | Has Cards | Description |
|------|-----------|-------------|
| TaskRunner | Yes | Posts Queued card on task submit |
| TaskCompleted | Yes | Updates card to Completed |
| TaskFailed | Yes | Updates card to Failed |
| TaskProgressUpdater | Yes | Updates Running card with activity log |
| TaskCanceled | Yes | Updates card to Killed |
| CancelTask | No | Cancels a task (Pending→Canceled, Running→Canceling) |
| SendMessage | No | MCS skill flow for bot messaging |

## Do NOT

- Manually create `release/v*` branches (use the workflow)
- Patch Power Automate flows directly via API (use deploy_flows.py)
- Use `--add-dir` with Claude CLI
- Push to any remote other than `ShragaBot/ShragaBot`
- Skip tests before pushing
