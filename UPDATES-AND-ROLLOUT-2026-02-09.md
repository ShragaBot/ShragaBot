# Shraga Updates and Rollout Guide

**Date:** February 9, 2026
**Purpose:** Guide for updating worker code and rolling out to team Dev Boxes
**Status:** Design Complete

---

## Overview

This document describes how to deploy updates to Shraga workers running on team Dev Boxes.

---

## Version Management

### VERSION File

Location: `Q:\repos\Users\sagik\shraga-worker\VERSION`

Format: Semantic versioning (MAJOR.MINOR.PATCH)
```
1.0.0
```

### Versioning Strategy

- **MAJOR** (1.x.x): Breaking changes, requires manual intervention
- **MINOR** (x.1.x): New features, backward compatible
- **PATCH** (x.x.1): Bug fixes, backward compatible

---

## Automatic Update Process

### Worker Update Logic

Workers check for updates **only when IDLE** (not executing tasks):

1. **Every 10 minutes** (when idle):
   - `git fetch` to check remote
   - Read remote `VERSION` file
   - Compare with local `VERSION`
   - If different: Pull and restart

2. **During task execution**:
   - Skip update checks
   - Complete task first
   - Then check for updates

3. **On restart**:
   - Task Scheduler auto-restarts worker
   - Worker pulls latest code
   - Continues with new version

### State Machine

```
IDLE (no tasks)
  ↓ Every 10 minutes
Check remote VERSION
  ↓ If new version available
Pull latest code
  ↓
Exit worker process
  ↓ Task Scheduler detects exit
Restart worker automatically
  ↓
Worker starts with new code

EXECUTING (task running)
  ↓
Finish task first
  ↓
Back to IDLE
  ↓ Then check updates
```

---

## Deploying Updates

### For Bug Fixes (Patch Update)

**Example: 1.0.0 → 1.0.1**

1. Fix bug in worker code
2. Test locally
3. Update `VERSION` file:
   ```
   1.0.1
   ```
4. Commit and push:
   ```bash
   git add .
   git commit -m "Fix: Dataverse polling timeout issue"
   git push
   ```
5. Workers auto-detect within 10 minutes
6. Workers update when idle

**Timeline**: All workers updated within 10-20 minutes

### For New Features (Minor Update)

**Example: 1.0.1 → 1.1.0**

1. Implement new feature
2. Test locally
3. Update `VERSION` file:
   ```
   1.1.0
   ```
4. Update documentation if needed
5. Commit and push
6. Workers auto-update when idle

**Timeline**: All workers updated within 10-20 minutes

### For Breaking Changes (Major Update)

**Example: 1.1.0 → 2.0.0**

**Requires**: Manual intervention or coordinated rollout

1. Implement breaking changes
2. Update `VERSION` file:
   ```
   2.0.0
   ```
3. **Send Teams notification** to all users:
   ```
   ⚠️ Major update available!
   Workers will update automatically, but may require re-authentication.
   Please be ready to re-authenticate Claude Code and Azure CLI if prompted.
   ```
4. Commit and push
5. Workers update when idle
6. Monitor for issues

**Timeline**: 10-30 minutes, may require user intervention

---

## Dev Box Customization Updates

### When to Update YAML

Update `devbox-customization-shraga.yaml` when:
- Adding new software dependencies
- Changing Python packages
- Modifying auto-start configuration
- Changing Git repository URL

### Applying YAML Changes

**For existing Dev Boxes**: YAML only runs on first provision
- Users must manually apply changes
- Or recreate Dev Box (nuclear option)

**For new Dev Boxes**: YAML applied automatically
- New team members get latest config
- Fresh provisions use new YAML

### YAML Update Process

1. Update `devbox-customization-shraga.yaml`
2. Test on fresh Dev Box
3. Commit and push
4. Update Azure Dev Box pool definition with new YAML
5. New provisions use updated YAML

**For existing Dev Boxes**:
- Send manual update script if needed
- Document manual steps in Teams
- Or wait for Dev Box recreation

---

## Rollback Process

### Emergency Rollback

If update causes issues:

1. **Revert VERSION**:
   ```bash
   git checkout HEAD~1 VERSION
   git commit -m "Rollback to 1.0.0"
   git push
   ```

2. **Workers auto-rollback** within 10 minutes

### Manual Rollback (Single Worker)

If one worker has issues:

1. RDP to Dev Box
2. Navigate to: `C:\Dev\shraga-worker`
3. Run:
   ```bash
   git pull
   git checkout <previous-commit>
   ```
4. Restart worker manually or wait for Task Scheduler

---

## Monitoring Updates

### Check Worker Versions

**Via Dataverse**:
- Workers report their version in heartbeat
- Query Workers table for version distribution

**Via Git**:
- Check commit history for latest changes
- See which version is live

### Update Status

**Expected behavior**:
- Workers update within 10-20 minutes
- No user intervention needed for minor updates
- Workers continue after restart

**Alert conditions**:
- Worker still on old version after 30 minutes
- Worker crash loops after update
- Authentication failures after update

---

## Worker Auto-Start Configuration

### Task Scheduler Setup

**Configured in YAML** (`userTasks` section):

- **Task Name**: ShragaSW
- **Trigger**: At startup
- **Action**: `python.exe C:\Dev\shraga-worker\worker.py`
- **Working Directory**: `C:\Dev\shraga-worker`
- **Restart Policy**: 3 attempts, 1 minute interval

### Manual Task Scheduler Management

**View task**:
```powershell
Get-ScheduledTask -TaskName "ShragaSW"
```

**Start task manually**:
```powershell
Start-ScheduledTask -TaskName "ShragaSW"
```

**Stop task**:
```powershell
Stop-ScheduledTask -TaskName "ShragaSW"
```

**Disable auto-start**:
```powershell
Disable-ScheduledTask -TaskName "ShragaSW"
```

---

## Team Rollout Process

### Initial Team Member Onboarding

1. **Admin provisions Dev Box** for new team member
2. **Dev Box customization runs**:
   - Installs Git, Claude Code, Python
   - Clones shraga-worker repo
   - Sets up worker auto-start
3. **User receives Teams notification**:
   - "Your Dev Box is ready!"
   - "Please authenticate to complete setup"
4. **User authenticates** (device code flow):
   - Claude Code: `claude /login`
   - Azure CLI: `az login`
5. **Worker starts automatically** on next boot
6. **User submits first task** via Teams
7. **Done!**

### Scaling to Entire Team

**Week 1: Pilot** (2-3 users)
- Provision pilot Dev Boxes
- Collect feedback
- Fix issues quickly

**Week 2-3: Gradual Rollout** (5-10 users)
- Provision in batches
- Monitor for common issues
- Document FAQs

**Week 4+: Full Rollout** (all users)
- Open to entire team
- Automated provisioning based on demand
- Self-service via Teams

---

## Troubleshooting

### Worker Not Starting

**Check Task Scheduler**:
```powershell
Get-ScheduledTask -TaskName "ShragaSW" | Get-ScheduledTaskInfo
```

**Check worker logs**:
- Location: `C:\Dev\shraga-worker\logs\worker.log`
- Look for authentication errors

**Common fixes**:
- Re-run: `az login`
- Re-run: `claude /login`
- Manually start: `python C:\Dev\shraga-worker\worker.py`

### Worker Not Updating

**Check git status**:
```bash
cd C:\Dev\shraga-worker
git status
git fetch
git log --oneline -5
```

**Common issues**:
- Uncommitted local changes (blocks pull)
- Authentication expired
- Network issues

**Fix**:
```bash
git reset --hard origin/users/sagik/shraga-worker
git pull
```

### Update Broke Worker

**Check version**:
```bash
cd C:\Dev\shraga-worker
cat VERSION
```

**Rollback manually**:
```bash
git log --oneline -5
git checkout <previous-good-commit>
```

**Report issue**:
- Post in Teams channel
- Include error logs
- Admin will fix and push update

---

## Best Practices

### Before Deploying Update

1. ✅ Test locally
2. ✅ Review changes carefully
3. ✅ Update VERSION file
4. ✅ Update documentation if needed
5. ✅ Commit with clear message
6. ✅ Push during low-usage hours (if major)

### During Rollout

1. ✅ Monitor worker status in Dataverse
2. ✅ Watch for error messages in Teams
3. ✅ Check logs if issues reported
4. ✅ Be ready to rollback if needed

### After Deployment

1. ✅ Verify all workers updated
2. ✅ Check for any errors in logs
3. ✅ Confirm tasks executing successfully
4. ✅ Document any issues for next time

---

## Future Enhancements

### Potential Improvements

1. **Version dashboard**: Real-time view of worker versions
2. **Staged rollout**: Update 10% of workers first, then rest
3. **A/B testing**: Run two versions simultaneously
4. **Blue-green deployment**: Switch all workers atomically
5. **Health checks**: Automated validation after update

---

**End of Document**
