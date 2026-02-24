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

**Do NOT manually create release branches.** Always use the workflow.

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

## Do NOT

- Manually create `release/v*` branches (use the workflow)
- Use `--add-dir` with Claude CLI
- Push to any remote other than `ShragaBot/ShragaBot`
- Skip tests before pushing
