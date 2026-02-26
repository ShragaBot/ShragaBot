"""
Shraga Orchestrator - Central coordinator for autonomous task execution

Responsibilities:
1. Discover user tasks in Dataverse
2. Create admin mirror tasks (source of truth)
3. Assign tasks to workers
4. Provision Dev Boxes when needed
5. Monitor worker health
6. Sync progress between admin and user tasks
"""

import subprocess
import time
import json
import sys
import traceback
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
import os
from dv_client import DataverseClient, DataverseError, DataverseRetryExhausted, ETagConflictError, create_credential

# --- File logging ---
_LOG_FILE = Path(__file__).parent / "orchestrator.log"

_file_logger = logging.getLogger("shraga_orchestrator")
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


def _log_to_file(msg: str):
    """Write to log file only (no console output)."""
    try:
        _file_logger.info(msg)
    except Exception:
        pass

# Import Dev Box manager
try:
    from orchestrator_devbox import DevBoxManager
    DEVBOX_AVAILABLE = True
except ImportError:
    _log("[WARN] orchestrator_devbox not available - Dev Box provisioning disabled")
    DEVBOX_AVAILABLE = False

# Configuration
DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
TABLE = os.environ.get("TABLE_NAME", "cr_shraga_tasks")
WORKERS_TABLE = os.environ.get("WORKERS_TABLE", "cr_shraga_workers")
STATE_FILE = ".orchestrator_state.json"

# Dev Box configuration
DEVCENTER_ENDPOINT = os.environ.get("DEVCENTER_ENDPOINT")
DEVBOX_PROJECT = os.environ.get("DEVBOX_PROJECT")
DEVBOX_POOL = os.environ.get("DEVBOX_POOL", "botdesigner-pool-italynorth")

# Task status codes -- string labels for readability
STATUS_PENDING = "Pending"
STATUS_RUNNING = "Running"
STATUS_COMPLETED = "Completed"
STATUS_FAILED = "Failed"

# Integer picklist values for OData $filter expressions and PATCH/POST bodies
# (cr_shraga_tasks.cr_status is a Picklist/Whole Number, NOT a string)
_STATUS_INT = {"Pending": 1, "Running": 5, "Completed": 7, "Failed": 8,
               "Canceled": 9, "Submitted": 10, "Canceling": 11}

# Provisioning threshold
PROVISION_THRESHOLD = int(os.environ.get("PROVISION_THRESHOLD", "5"))

# Git branch name
GIT_BRANCH = os.environ.get("GIT_BRANCH", "users/sagik/shraga-worker")

# API timeouts (seconds)
GIT_TIMEOUT = 60


class Orchestrator:
    """Main orchestrator for Shraga system"""

    def __init__(self):
        # Dataverse client (handles auth, retry, token caching)
        self.dv = DataverseClient(
            dataverse_url=DATAVERSE_URL,
            credential=create_credential(log_fn=print),
            log_fn=print,
        )

        # Dev Box manager (optional)
        self.devbox_manager = None
        if DEVBOX_AVAILABLE and DEVCENTER_ENDPOINT and DEVBOX_PROJECT:
            try:
                self.devbox_manager = DevBoxManager(
                    devcenter_endpoint=DEVCENTER_ENDPOINT,
                    project_name=DEVBOX_PROJECT,
                    pool_name=DEVBOX_POOL,
                )
                _log(f"[INIT] Dev Box manager initialized")
            except Exception as e:
                _log(f"[WARN] Dev Box manager init failed: {e}")
                _log_to_file(f"[WARN] Dev Box manager init traceback:\n{traceback.format_exc()}")
                self.devbox_manager = None
        else:
            _log("[WARN] Dev Box provisioning not configured")

        # Version and update tracking
        self.repo_path = Path(__file__).parent
        self.current_version = self.load_version()
        self.last_update_check = None
        self.update_check_interval = timedelta(minutes=10)

        # State
        self.admin_user_id = None
        self.shared_workers = []  # List of shared worker IDs
        self.worker_round_robin_index = 0  # For load balancing
        self.load_state()

    def load_state(self):
        """Load orchestrator state with validation"""
        state_path = Path(STATE_FILE)
        if state_path.exists():
            try:
                with open(state_path, encoding="utf-8") as f:
                    data = json.load(f)

                    # Validate and load
                    if isinstance(data, dict):
                        self.admin_user_id = data.get("admin_user_id")
                        workers = data.get("shared_workers", [])

                        # Validate workers is a list
                        if isinstance(workers, list):
                            self.shared_workers = workers
                        else:
                            _log(f"[WARN] Invalid shared_workers in state, using empty list")
                            self.shared_workers = []
                    else:
                        _log(f"[WARN] Invalid state file format")

            except json.JSONDecodeError as e:
                _log(f"[ERROR] State file corrupted: {e}")
            except Exception as e:
                _log(f"[ERROR] Loading state: {e}")
                _log_to_file(f"[ERROR] Loading state traceback:\n{traceback.format_exc()}")

    def save_state(self):
        """Save orchestrator state"""
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "admin_user_id": self.admin_user_id,
                    "shared_workers": self.shared_workers
                }, f, indent=2)
        except Exception as e:
            _log(f"[ERROR] Saving state: {e}")
            _log_to_file(f"[ERROR] Saving state traceback:\n{traceback.format_exc()}")

    def get_token(self) -> Optional[str]:
        """Get Dataverse OAuth token (thin wrapper around DataverseClient)."""
        try:
            return self.dv.get_token()
        except TimeoutError:
            _log("[ERROR] get_token() timed out after 30s -- Azure credential hung")
            return None
        except Exception as e:
            _log(f"[ERROR] Getting token: {e}")
            _log("[HINT] Make sure you've run: az login")
            _log_to_file(f"[ERROR] Getting token traceback:\n{traceback.format_exc()}")
            return None

    def get_current_user(self) -> Optional[str]:
        """Get admin user ID"""
        try:
            url = f"{DATAVERSE_URL}/api/data/v9.2/WhoAmI"
            response = self.dv.get(url)
            user_data = response.json()
            user_id = user_data.get("UserId")
            self.admin_user_id = user_id
            self.save_state()
            return user_id
        except (DataverseError, DataverseRetryExhausted) as e:
            _log(f"[ERROR] Getting current user: {e}")
            _log_to_file(f"[ERROR] Getting current user traceback:\n{traceback.format_exc()}")
            return None
        except Exception as e:
            _log(f"[ERROR] Getting current user: {e}")
            _log_to_file(f"[ERROR] Getting current user traceback:\n{traceback.format_exc()}")
            return None

    def load_version(self) -> str:
        """Load current version from VERSION file"""
        version_file = self.repo_path / "VERSION"
        try:
            if version_file.exists():
                return version_file.read_text().strip()
            return "unknown"
        except Exception as e:
            _log(f"[WARN] Could not read VERSION file: {e}")
            return "unknown"

    def check_for_updates(self) -> bool:
        """Check if new version available (when idle)"""
        try:
            # Fetch latest
            result = subprocess.run(
                ["git", "fetch"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT
            )

            if result.returncode != 0:
                _log(f"[WARN] Git fetch failed: {result.stderr}")
                return False

            # Read remote VERSION
            result = subprocess.run(
                ["git", "show", f"origin/{GIT_BRANCH}:VERSION"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                _log(f"[WARN] Could not read remote VERSION: {result.stderr}")
                return False

            remote_version = result.stdout.strip()

            if remote_version != self.current_version:
                _log(f"[UPDATE] New version available: {remote_version} (current: {self.current_version})")
                return True

            return False
        except subprocess.TimeoutExpired:
            _log(f"[WARN] Update check timed out")
            return False
        except Exception as e:
            _log(f"[WARN] Update check failed: {e}")
            return False

    def apply_update(self):
        """Pull latest code and restart orchestrator"""
        try:
            _log("[UPDATE] Applying update...")

            # Pull latest
            result = subprocess.run(
                ["git", "pull"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT
            )

            if result.returncode != 0:
                _log(f"[ERROR] Git pull failed: {result.stderr}")
                return False

            _log("[UPDATE] Code updated successfully")
            _log("[UPDATE] Restarting orchestrator...")

            # Exit - will be restarted by service/scheduler
            sys.exit(0)

        except subprocess.TimeoutExpired:
            _log(f"[ERROR] Update timed out")
            return False
        except Exception as e:
            _log(f"[ERROR] Update failed: {e}")
            _log_to_file(f"[ERROR] Update failed traceback:\n{traceback.format_exc()}")
            return False

    def discover_user_tasks(self) -> List[Dict[str, Any]]:
        """
        Discover new user tasks that need mirroring

        Query: Pending tasks without mirror, not owned by admin
        """
        try:
            # Query for user tasks that need mirroring
            filter_query = (
                f"cr_status eq {_STATUS_INT[STATUS_PENDING]} "
                f"and cr_ismirror eq false "
                f"and cr_mirrortaskid eq null"
            )

            # Only filter by admin if we know admin ID
            if self.admin_user_id:
                filter_query += f" and _ownerid_value ne '{self.admin_user_id}'"

            url = f"{DATAVERSE_URL}/api/data/v9.2/{TABLE}?$filter={filter_query}"
            response = self.dv.get(url)
            data = response.json()
            return data.get("value", [])

        except (DataverseError, DataverseRetryExhausted) as e:
            _log(f"[ERROR] Discovering user tasks: {e}")
            _log_to_file(f"[ERROR] Discovering user tasks traceback:\n{traceback.format_exc()}")
            return []
        except Exception as e:
            _log(f"[ERROR] Discovering user tasks: {e}")
            _log_to_file(f"[ERROR] Discovering user tasks traceback:\n{traceback.format_exc()}")
            return []

    def create_admin_mirror(self, user_task: Dict[str, Any]) -> Optional[str]:
        """
        Create admin-owned mirror of user task

        Returns:
            Mirror task ID if successful, None otherwise
        """
        user_task_id = user_task.get("cr_shraga_taskid")

        if not user_task_id:
            _log(f"[ERROR] User task missing ID")
            return None

        # Create mirror task with all relevant fields
        mirror_data = {
            "cr_name": user_task.get("cr_name", "Unnamed Task"),
            "cr_prompt": user_task.get("cr_prompt", ""),
            "cr_status": _STATUS_INT[STATUS_PENDING],
            "cr_ismirror": True,
            "cr_mirroroftaskid": user_task_id,
            "cr_transcript": "",  # Start empty
            "cr_result": "",  # Start empty
        }

        # Copy user email so PA flows can send Teams cards
        user_email = user_task.get("crb3b_useremail", "")
        if user_email:
            mirror_data["crb3b_useremail"] = user_email

        # Set admin as owner if we know admin ID
        if self.admin_user_id:
            mirror_data["ownerid@odata.bind"] = f"/systemusers({self.admin_user_id})"

        try:
            url = f"{DATAVERSE_URL}/api/data/v9.2/{TABLE}"
            response = self.dv.post(
                url,
                mirror_data,
                extra_headers={"Prefer": "return=representation"},
            )

            # Extract mirror task ID from response
            created_task = response.json()
            mirror_task_id = created_task.get("cr_shraga_taskid")

            if not mirror_task_id:
                # Try from headers as fallback
                location = response.headers.get("OData-EntityId", "")
                if "(" in location:
                    mirror_task_id = location.split("(")[1].split(")")[0]

            if not mirror_task_id:
                _log(f"[ERROR] Could not extract mirror task ID")
                return None

            # Link user task to mirror (dv client handles retry internally)
            link_success = self.update_task(user_task_id, mirror_task_id=mirror_task_id)

            if not link_success:
                _log(f"[WARN] Created mirror {mirror_task_id[:8]} but failed to link user task")
                # Continue anyway - mirror exists and can be used

            _log(f"[MIRROR] Created: {mirror_task_id[:8]} for user task: {user_task_id[:8]}")

            return mirror_task_id

        except (DataverseError, DataverseRetryExhausted) as e:
            _log(f"[ERROR] Creating admin mirror: {e}")
            _log_to_file(f"[ERROR] Creating admin mirror traceback:\n{traceback.format_exc()}")
            return None
        except Exception as e:
            _log(f"[ERROR] Creating admin mirror: {e}")
            _log_to_file(f"[ERROR] Creating admin mirror traceback:\n{traceback.format_exc()}")
            return None

    def update_task(self, task_id: str, **fields) -> bool:
        """Update task fields in Dataverse"""
        if not task_id:
            _log(f"[ERROR] update_task called with empty task_id")
            return False

        # Map friendly field names to Dataverse names
        dataverse_fields = {
            "status": "cr_status",
            "mirror_task_id": "cr_mirrortaskid",
            "assigned_worker_id": "cr_assignedworkerid",
            "worker_status": "cr_workerstatus"
        }

        data = {}
        for key, value in fields.items():
            dv_field = dataverse_fields.get(key, key)
            if value is not None:
                if dv_field == "cr_status" and isinstance(value, str):
                    data[dv_field] = _STATUS_INT.get(value, value)
                else:
                    data[dv_field] = value

        if not data:
            _log(f"[WARN] update_task called with no fields to update")
            return False

        try:
            url = self.dv.row_url(TABLE, task_id)
            self.dv.patch(url, data)
            return True
        except (DataverseError, DataverseRetryExhausted) as e:
            _log(f"[ERROR] Updating task: {e}")
            _log_to_file(f"[ERROR] Updating task traceback:\n{traceback.format_exc()}")
            return False

    def get_next_worker(self) -> Optional[str]:
        """
        Get next available worker using round-robin

        Returns:
            Worker ID or None if no workers available
        """
        if not self.shared_workers:
            return None

        # Round-robin assignment
        worker_id = self.shared_workers[self.worker_round_robin_index]

        # Move to next worker for next assignment
        self.worker_round_robin_index = (self.worker_round_robin_index + 1) % len(self.shared_workers)

        return worker_id

    def assign_to_worker(self, mirror_task_id: str, user_id: str) -> bool:
        """
        Assign task to worker (user's dedicated or shared pool)

        Logic:
        1. Check if user has dedicated worker (TODO)
        2. If yes: assign to user's worker
        3. If no: assign to shared pool worker (round-robin)
        4. If user has 5+ tasks: trigger provisioning (TODO)
        """
        if not mirror_task_id:
            _log(f"[ERROR] assign_to_worker called with empty mirror_task_id")
            return False

        # Get next available worker
        worker_id = self.get_next_worker()

        if not worker_id:
            _log(f"[ERROR] No workers available for assignment")
            return False

        # Update task with worker assignment
        success = self.update_task(
            mirror_task_id,
            status=STATUS_RUNNING,
            assigned_worker_id=worker_id,
            worker_status="assigned"
        )

        if success:
            _log(f"[ASSIGN] Task {mirror_task_id[:8]} -> Worker {worker_id[:8]}")
        else:
            _log(f"[ERROR] Failed to assign task {mirror_task_id[:8]}")

        return success

    def process_new_tasks(self):
        """Main task processing: discover, mirror, assign"""
        # Discover new user tasks
        user_tasks = self.discover_user_tasks()

        if not user_tasks:
            return

        _log(f"[DISCOVER] Found {len(user_tasks)} new user task(s)")

        for user_task in user_tasks:
            task_name = user_task.get("cr_name", "Unnamed")
            user_id = user_task.get("_ownerid_value")

            _log(f"[TASK] Processing: {task_name}")

            # Create admin mirror
            mirror_task_id = self.create_admin_mirror(user_task)
            if not mirror_task_id:
                _log(f"[ERROR] Failed to create mirror for: {task_name}")
                continue

            # Assign to worker
            if not self.assign_to_worker(mirror_task_id, user_id):
                _log(f"[ERROR] Failed to assign: {task_name}")
                continue

            # Small delay between tasks to avoid overwhelming Dataverse
            time.sleep(0.5)

    def run(self):
        """Main orchestrator loop"""
        _log("=" * 80)
        _log("SHRAGA ORCHESTRATOR")
        _log("=" * 80)
        _log(f"Dataverse: {DATAVERSE_URL}")
        _log(f"Table: {TABLE}")
        _log(f"Version: {self.current_version}")
        _log(f"Dev Box: {'Enabled' if self.devbox_manager else 'Disabled'}")
        _log("=" * 80)

        # Get admin user
        if not self.admin_user_id:
            _log("Identifying admin user...")
            if not self.get_current_user():
                _log("[FATAL] Could not identify admin user")
                return

        _log(f"Admin user: {self.admin_user_id}")

        # Check workers configured
        if not self.shared_workers:
            _log("[WARN] No shared workers configured!")
            _log("[HINT] Add worker IDs to .orchestrator_state.json:")
            _log('       {"shared_workers": ["worker-guid-1", "worker-guid-2"]}')
            _log("[INFO] Orchestrator will run but cannot assign tasks")

        _log(f"Workers: {len(self.shared_workers)} configured")
        _log("\n[POLLING] Monitoring for new user tasks...\n")

        _start_time = time.time()
        _last_heartbeat = 0.0

        try:
            while True:
                # Heartbeat logging every 60 seconds
                _now = time.time()
                if _now - _last_heartbeat > 60:
                    _uptime = int(_now - _start_time)
                    _log(f"[HEARTBEAT] Orchestrator alive | version={self.current_version} | uptime={_uptime}s | workers={len(self.shared_workers)}")
                    _last_heartbeat = _now

                # Process new tasks (only if we have workers)
                if self.shared_workers:
                    self.process_new_tasks()
                else:
                    # Still poll but warn
                    user_tasks = self.discover_user_tasks()
                    if user_tasks:
                        _log(f"[WARN] Found {len(user_tasks)} tasks but no workers to assign!")

                # Check for updates (when idle)
                now = datetime.now(tz=timezone.utc)
                if self.last_update_check is None or (now - self.last_update_check) >= self.update_check_interval:
                    _log("[IDLE] Checking for updates...")
                    self.last_update_check = now

                    if self.check_for_updates():
                        _log("[UPDATE] Applying update...")
                        self.apply_update()
                        # apply_update() exits

                # Poll every 10 seconds
                time.sleep(10)

        except KeyboardInterrupt:
            _log("\n\n[INTERRUPT] Stopping orchestrator...")
        except Exception as e:
            _log(f"\n[FATAL ERROR] {e}")
            _log_to_file(f"[FATAL ERROR] traceback:\n{traceback.format_exc()}")
            traceback.print_exc()

        _log("[SHUTDOWN] Orchestrator stopped")


if __name__ == "__main__":
    orchestrator = Orchestrator()
    orchestrator.run()
