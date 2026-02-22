r"""Version check for Shraga services.

Each service runs from an immutable release folder (e.g., C:\Dev\Shraga\releases\v1).
The updater writes current_version.txt when a new release is available.
Services compare their folder name against the file and exit if different.
"""
import os
from pathlib import Path

SHRAGA_ROOT = Path(os.environ.get("SHRAGA_ROOT", os.path.join("C:", os.sep, "Dev", "Shraga")))
VERSION_FILE = SHRAGA_ROOT / "current_version.txt"


def get_my_version(script_path: str) -> str:
    """Get version from the release folder name this script is running from.

    E.g., C:/Dev/Shraga/releases/v1/integrated_task_worker.py -> "v1"
    Falls back to folder name of the script's parent if not in releases/ structure.
    """
    p = Path(script_path).resolve().parent
    # Walk up to find the releases/ parent
    while p.parent.name != "releases" and p != p.parent:
        p = p.parent
    if p.parent.name == "releases":
        return p.name  # e.g., "v1"
    # Not in releases/ structure (dev mode) — return the folder name
    return Path(script_path).resolve().parent.name


def get_current_version() -> str:
    """Read current version from file."""
    try:
        if VERSION_FILE.exists():
            return VERSION_FILE.read_text().strip()
    except Exception:
        pass
    return ""


def should_exit(my_version: str) -> bool:
    """Check if a newer version is available. Returns True if this process should exit."""
    current = get_current_version()
    if not current:
        return False  # No version file — don't exit (dev mode or first run)
    return current != my_version
