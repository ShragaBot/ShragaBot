r"""Shraga Release Updater -- standalone script, runs as a scheduled task.

Checks for the latest release/v* branch on GitHub. If a newer release
exists, downloads it as a zip into an immutable release folder, installs deps,
and writes current_version.txt. Running services detect the version
change and exit gracefully; the watchdog restarts them from the new release.

Directory structure:
  C:\Dev\Shraga\
    current_version.txt       -> "v1"
    updater.py                -> this script
    releases\
      v1\                     -> immutable file copy
      v2\                     -> next release
"""
import subprocess
import sys
import re
import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime

SHRAGA_ROOT = Path(os.environ.get("SHRAGA_ROOT", os.path.join("C:", os.sep, "Dev", "Shraga")))
RELEASES_DIR = SHRAGA_ROOT / "releases"
VERSION_FILE = SHRAGA_ROOT / "current_version.txt"
REPO_URL = "https://github.com/ShragaBot/ShragaBot.git"

# --- File logging ---
_LOG_FILE = Path(__file__).parent / "updater.log"

_file_logger = logging.getLogger("shraga_updater")
_file_logger.setLevel(logging.DEBUG)
_file_handler = RotatingFileHandler(
    str(_LOG_FILE),
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_file_logger.addHandler(_file_handler)


def _log(msg: str):
    """Print with timestamp to console AND write to log file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")
    try:
        _file_logger.info(msg)
    except Exception:
        pass  # Never let logging crash the service


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
            _log(f"[WARN] git ls-remote failed: {result.stderr.strip()}")
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
        _log("[WARN] git ls-remote timed out")
        return None
    except Exception as e:
        _log(f"[WARN] get_latest_release failed: {e}")
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
    """Download release branch as plain files (no .git) into an immutable release folder."""
    release_dir = RELEASES_DIR / version
    sentinel = release_dir / ".deploy_complete"
    if sentinel.exists():
        _log(f"[UPDATE] Release {version} already deployed at {release_dir}")
        return True
    # Clean up any partial/failed deploy
    if release_dir.exists():
        import shutil
        shutil.rmtree(release_dir, ignore_errors=True)

    _log(f"[UPDATE] Deploying {version}...")
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)

    # Download zip from GitHub and extract (no git clone, no .git directory)
    import zipfile, tempfile, shutil
    zip_url = f"{REPO_URL.removesuffix('.git')}/archive/refs/heads/release/{version}.zip"
    zip_path = Path(tempfile.gettempdir()) / f"shraga-{version}.zip"
    try:
        result = subprocess.run(
            ["curl", "-sfL", "-o", str(zip_path), zip_url],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0 or not zip_path.exists() or zip_path.stat().st_size < 1000:
            _log(f"[ERROR] Download failed: {result.stderr.strip()}")
            return False

        # Extract — GitHub zips have a top-level folder like ShragaBot-release-v1/
        with zipfile.ZipFile(str(zip_path), 'r') as zf:
            extract_dir = Path(tempfile.gettempdir()) / f"shraga-extract-{version}"
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
            zf.extractall(str(extract_dir))
            # Find the single top-level directory
            contents = list(extract_dir.iterdir())
            if len(contents) == 1 and contents[0].is_dir():
                shutil.move(str(contents[0]), str(release_dir))
            else:
                shutil.move(str(extract_dir), str(release_dir))
            # Clean up extract dir if still exists
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
    except Exception as e:
        _log(f"[ERROR] Deploy failed: {e}")
        if release_dir.exists():
            shutil.rmtree(release_dir, ignore_errors=True)
        return False
    finally:
        if zip_path.exists():
            zip_path.unlink(missing_ok=True)

    # Install dependencies
    py = find_python()
    _log(f"[UPDATE] Installing dependencies with {py}...")
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
        _log(f"[WARN] pip install issues: {pip_result.stderr[:200]}")

    # Mark deployment as complete (sentinel file for partial-deploy detection)
    sentinel.write_text(version)
    _log(f"[UPDATE] Release {version} deployed to {release_dir}")
    return True


def update_version_file(version: str):
    """Write the current version file. Services will detect this change and restart."""
    VERSION_FILE.write_text(version)
    _log(f"[UPDATE] current_version.txt updated to: {version}")


def main():
    _log("[UPDATER] Checking for new releases...")

    latest = get_latest_release()
    if not latest:
        _log("[UPDATER] No release branches found")
        return

    current = get_current_version()
    if current == latest:
        return  # Up to date, silent

    _log(f"[UPDATER] New release available: {latest} (current: {current or 'none'})")

    if deploy_release(latest):
        update_version_file(latest)
        _log(f"[UPDATER] Update complete. Services will restart with {latest}.")
    else:
        _log(f"[ERROR] Failed to deploy {latest}")


if __name__ == "__main__":
    main()
