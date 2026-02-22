"""Shraga Release Updater — standalone script, runs as a scheduled task.

Checks for the latest release/v* branch on GitHub. If a newer release
exists, clones it into an immutable release folder, installs deps,
and writes current_version.txt. Running services detect the version
change and exit gracefully; the watchdog restarts them from the new release.

Directory structure:
  C:\Dev\Shraga\
    current_version.txt       -> "v1"
    updater.py                -> this script
    releases\
      v1\                     -> immutable clone
      v2\                     -> next release
"""
import subprocess
import sys
import re
import os
from pathlib import Path

SHRAGA_ROOT = Path(os.environ.get("SHRAGA_ROOT", "C:\\Dev\\Shraga"))
RELEASES_DIR = SHRAGA_ROOT / "releases"
VERSION_FILE = SHRAGA_ROOT / "current_version.txt"
REPO_URL = "https://github.com/ShragaBot/ShragaBot.git"


def get_current_version() -> str:
    """Read current version from file. Returns empty string if not set."""
    try:
        if VERSION_FILE.exists():
            return VERSION_FILE.read_text().strip()
    except Exception:
        pass
    return ""


def get_latest_release() -> str | None:
    """Query GitHub for the latest release/v* branch. No local repo needed."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", REPO_URL, "release/v*"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(f"[WARN] git ls-remote failed: {result.stderr.strip()}")
            return None

        versions = []
        for line in result.stdout.strip().splitlines():
            match = re.search(r'refs/heads/release/v(\d+)', line)
            if match:
                versions.append(int(match.group(1)))

        if not versions:
            return None

        return f"v{max(versions)}"
    except subprocess.TimeoutExpired:
        print("[WARN] git ls-remote timed out")
        return None
    except Exception as e:
        print(f"[WARN] get_latest_release failed: {e}")
        return None


def find_python() -> str:
    """Find Python executable, skipping Windows Store stub."""
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python312", "python.exe"),
        os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "Programs", "Python", "Python312", "python.exe"),
        r"C:\Program Files\Python312\python.exe",
        r"C:\Python312\python.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # Fallback to PATH but skip WindowsApps
    import shutil
    found = shutil.which("python")
    if found and "WindowsApps" not in found:
        return found
    return sys.executable  # Last resort


def deploy_release(version: str) -> bool:
    """Clone the release branch into an immutable release folder and install deps."""
    release_dir = RELEASES_DIR / version
    if release_dir.exists():
        print(f"[UPDATE] Release {version} already exists at {release_dir}")
        return True

    print(f"[UPDATE] Deploying {version}...")
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)

    # Clone the specific branch
    result = subprocess.run(
        ["git", "clone", "--branch", f"release/{version}", "--depth", "1",
         REPO_URL, str(release_dir)],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        print(f"[ERROR] Clone failed: {result.stderr.strip()}")
        # Clean up partial clone
        if release_dir.exists():
            import shutil
            shutil.rmtree(release_dir, ignore_errors=True)
        return False

    # Install dependencies
    py = find_python()
    print(f"[UPDATE] Installing dependencies with {py}...")
    subprocess.run(
        [py, "-m", "ensurepip", "--upgrade"],
        capture_output=True, text=True, timeout=60,
        cwd=str(release_dir)
    )
    pip_result = subprocess.run(
        [py, "-m", "pip", "install", "--quiet", "--upgrade",
         "requests", "azure-identity", "azure-core", "watchdog"],
        capture_output=True, text=True, timeout=120,
        cwd=str(release_dir)
    )
    if pip_result.returncode != 0:
        print(f"[WARN] pip install issues: {pip_result.stderr[:200]}")

    print(f"[UPDATE] Release {version} deployed to {release_dir}")
    return True


def update_version_file(version: str):
    """Write the current version file. Services will detect this change and restart."""
    VERSION_FILE.write_text(version)
    print(f"[UPDATE] current_version.txt updated to: {version}")


def main():
    print("[UPDATER] Checking for new releases...")

    latest = get_latest_release()
    if not latest:
        print("[UPDATER] No release branches found")
        return

    current = get_current_version()
    if current == latest:
        return  # Up to date, silent

    print(f"[UPDATER] New release available: {latest} (current: {current or 'none'})")

    if deploy_release(latest):
        update_version_file(latest)
        print(f"[UPDATER] Update complete. Services will restart with {latest}.")
    else:
        print(f"[ERROR] Failed to deploy {latest}")


if __name__ == "__main__":
    main()
