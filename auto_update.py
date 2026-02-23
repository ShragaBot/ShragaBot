"""Auto-update module for Shraga services (Worker, PM).

Checks for the latest release/v* branch every N minutes.
If a newer release exists, switches to it, installs deps, and exits
so the scheduled task restarts the process with the new code.
"""
import subprocess
import sys
import re
import random
from pathlib import Path
from datetime import datetime, timezone, timedelta


class AutoUpdater:
    """Checks for and applies updates from release branches."""

    def __init__(self, repo_path: str | Path, check_interval_minutes: int = 10):
        self.repo_path = Path(repo_path)
        self.check_interval = timedelta(minutes=check_interval_minutes)
        # Random initial delay (0-60s) so Worker and PM don't check simultaneously
        self.last_check = datetime.now(timezone.utc) - self.check_interval + timedelta(seconds=random.randint(0, 60))
        self.current_branch = self._get_current_branch()

    def _run_git(self, *args, timeout=30) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + list(args),
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=timeout
        )

    def _get_current_branch(self) -> str:
        try:
            result = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
            return result.stdout.strip() if result.returncode == 0 else "unknown"
        except Exception:
            return "unknown"

    def _get_latest_release_branch(self) -> str | None:
        """Find the highest-numbered release/v* branch from remote."""
        try:
            result = self._run_git("branch", "-r", "--list", "origin/release/v*")
            if result.returncode != 0:
                return None

            branches = []
            for line in result.stdout.strip().splitlines():
                name = line.strip()
                match = re.search(r'origin/release/v(\d+)', name)
                if match:
                    branches.append((int(match.group(1)), name))

            if not branches:
                return None

            # Return the branch with the highest version number
            branches.sort(key=lambda x: x[0], reverse=True)
            return branches[0][1]  # e.g., "origin/release/v3"
        except Exception:
            return None

    def should_check(self) -> bool:
        if self.last_check is None:
            return True
        return (datetime.now(timezone.utc) - self.last_check) >= self.check_interval

    def check_and_update(self) -> bool:
        """Check for updates. Calls sys.exit(0) if an update is applied (scheduled task restarts).

        Returns False if no update needed. Never returns True — exits the process instead.
        Call this periodically from the main loop when idle or between tasks.
        """
        if not self.should_check():
            return False

        self.last_check = datetime.now(timezone.utc)

        try:
            # Fetch latest from remote
            result = self._run_git("fetch", "--all", "--prune")
            if result.returncode != 0:
                print(f"[WARN] Git fetch failed: {result.stderr.strip()}")
                return False

            latest = self._get_latest_release_branch()
            if not latest:
                return False

            # Extract local branch name (origin/release/v3 -> release/v3)
            local_branch = latest.removeprefix("origin/")
            current = self._get_current_branch()

            if current == local_branch:
                # Same branch — check if remote has new commits
                result = self._run_git("rev-parse", "HEAD")
                local_head = result.stdout.strip() if result.returncode == 0 else ""
                result = self._run_git("rev-parse", latest)
                remote_head = result.stdout.strip() if result.returncode == 0 else ""

                if local_head == remote_head:
                    return False  # Up to date

                print(f"[UPDATE] New commits on {local_branch}")
            else:
                print(f"[UPDATE] New release branch: {local_branch} (current: {current})")

            # Apply update
            return self._apply_update(local_branch, latest)

        except subprocess.TimeoutExpired:
            print("[WARN] Update check timed out")
            return False
        except Exception as e:
            print(f"[WARN] Update check failed: {e}")
            return False

    def _apply_update(self, local_branch: str, remote_ref: str) -> bool:
        """Switch to the release branch, install deps, and exit."""
        try:
            print(f"[UPDATE] Switching to {local_branch}...")

            # Clean any local modifications that would block checkout
            self._run_git("reset", "--hard", timeout=30)

            # Checkout the branch (create local tracking branch if needed)
            result = self._run_git("checkout", local_branch, timeout=30)
            if result.returncode != 0:
                # Branch doesn't exist locally yet — create from remote
                result = self._run_git("checkout", "-b", local_branch, remote_ref, timeout=30)
                if result.returncode != 0:
                    print(f"[ERROR] Checkout failed: {result.stderr.strip()}")
                    return False
            else:
                # Branch exists locally — pull latest
                result = self._run_git("pull", "origin", local_branch, timeout=60)
                if result.returncode != 0:
                    # Force reset to remote if pull fails (e.g., diverged history)
                    print(f"[UPDATE] Pull failed, resetting to {remote_ref}")
                    self._run_git("reset", "--hard", remote_ref, timeout=30)

            # Install dependencies
            print("[UPDATE] Installing dependencies...")
            pip_result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
                 "requests", "azure-identity", "azure-core", "watchdog"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=120
            )
            if pip_result.returncode != 0:
                print(f"[WARN] pip install had issues: {pip_result.stderr[:200]}")

            self.current_branch = local_branch
            print(f"[UPDATE] Updated to {local_branch}. Restarting...")
            sys.exit(0)  # Task Scheduler restarts the process

        except subprocess.TimeoutExpired:
            print("[ERROR] Update timed out")
            return False
        except SystemExit:
            raise  # Let sys.exit propagate
        except Exception as e:
            print(f"[ERROR] Update failed: {e}")
            return False
