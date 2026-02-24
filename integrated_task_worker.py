"""
Integrated Task Worker - Combines Dataverse polling with autonomous agent execution

Polls Dataverse for tasks → Executes using Worker/Verifier loop → Updates Dataverse
"""
import platform
import requests
import socket
import subprocess
import time
import json
import sys
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AccessToken

# Import the autonomous agent system (from same directory as this file)
sys.path.insert(0, str(Path(__file__).parent))
from autonomous_agent import AgentCLI, extract_phase_stats, merge_phase_stats
from timeout_utils import call_with_timeout

import os
os.environ.setdefault('PYTHONUNBUFFERED', '1')
from onedrive_utils import find_onedrive_root, local_path_to_web_url, OneDriveRootNotFoundError

DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")

# --- File logging ---
# Logs go to both console and a rotating log file so we can debug live issues.
# Log file lives next to the script (inside the release folder on dev boxes).
# Rotates at 10MB, keeps 5 files = 50MB max.
import logging
from logging.handlers import RotatingFileHandler

_LOG_FILE = Path(__file__).parent / "worker.log"

_file_logger = logging.getLogger("shraga_worker")
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
    line = f"[{ts}] {msg}"
    print(line)
    try:
        _file_logger.info(msg)
    except Exception:
        pass  # Never let logging crash the worker

TABLE = os.environ.get("TABLE_NAME", "cr_shraga_tasks")  # MCS table with solution prefix
STATE_FILE = ".integrated_worker_state.json"

WEBHOOK_USER = os.environ.get("WEBHOOK_USER", "")
REQUEST_TIMEOUT = 30  # seconds for HTTP requests to Dataverse
from version_check import get_my_version, should_exit

MACHINE_NAME = platform.node()  # This dev box's hostname

# Status labels used in PATCH/POST bodies (Dataverse accepts string labels)
STATUS_SUBMITTED = "Submitted"
STATUS_PENDING = "Pending"
STATUS_RUNNING = "Running"
STATUS_COMPLETED = "Completed"
STATUS_FAILED = "Failed"
STATUS_CANCELED = "Canceled"
STATUS_CANCELING = "Canceling"

# Deprecated but kept for historical DV rows:
# STATUS_QUEUED = "Queued"        # Was 3, replaced by open competition
# STATUS_WAITING_FOR_INPUT = "WaitingForInput"  # Was 6, never used as intended

# Integer picklist values for OData $filter expressions (DV requires integers in filters)
_STATUS_INT = {"Submitted": 10, "Pending": 1, "Running": 5,
               "Completed": 7, "Failed": 8, "Canceled": 9, "Canceling": 11,
               "Queued": 3, "WaitingForInput": 6}  # last two kept for historical rows

def format_session_numbers(stats: dict) -> str:
    """Format accumulated session stats into a one-line summary string.

    Args:
        stats: Accumulated stats dict from merge_phase_stats (keys:
               total_cost_usd, total_duration_ms, total_turns, tokens,
               model_usage).

    Returns:
        A formatted string like:
        ``--- Session Numbers ---
        Duration: 2m 35s | Cost: $0.12 | Tokens: 12,345 in / 6,789 out | Turns: 8 | Sub-agents: 1``
    """
    if not stats:
        return ""

    # Duration
    total_ms = stats.get("total_duration_ms", 0)
    total_sec = total_ms / 1000
    minutes = int(total_sec // 60)
    seconds = int(total_sec % 60)
    if minutes > 0:
        duration_str = f"{minutes}m {seconds:02d}s"
    else:
        duration_str = f"{seconds}s"

    # Cost
    cost = stats.get("total_cost_usd", 0.0)
    cost_str = f"${cost:.2f}"

    # Tokens
    tokens = stats.get("tokens", {})
    tok_in = tokens.get("input", 0)
    tok_out = tokens.get("output", 0)
    tokens_str = f"{tok_in:,} in / {tok_out:,} out"

    # Turns
    turns = stats.get("total_turns", 0)

    # Sub-agents (number of distinct models minus the main one)
    model_usage = stats.get("model_usage", {})
    sub_agents = max(0, len(model_usage) - 1)

    return (
        f"\n\n--- Session Numbers ---\n"
        f"Duration: {duration_str} | Cost: {cost_str} | "
        f"Tokens: {tokens_str} | Turns: {turns} | Sub-agents: {sub_agents}"
    )


class IntegratedTaskWorker:
    """Worker that uses autonomous agent system for task execution"""

    def __init__(self):
        self.current_user_id = None
        self.current_task_id = None  # Set during task processing for message correlation
        self.work_base_dir = Path(os.environ.get("WORK_BASE_DIR", str(Path(__file__).parent)))

        # Azure authentication
        self.credential = DefaultAzureCredential()
        self._token_cache = None
        self._token_expires = None

        # Version check for immutable releases
        self.repo_path = Path(__file__).parent
        self._my_version = get_my_version(__file__)

        self.load_state()

    def create_session_folder(self, task_name: str, task_id: str) -> Path:
        """Create an isolated OneDrive session folder for a task.

        Folder structure: {OneDrive root}/Shraga Sessions/{task_name}_{task_id_short}/
        Falls back to local work_base_dir if OneDrive is not available.
        """
        # Sanitize task name for filesystem
        safe_name = "".join(c if c.isalnum() or c in ('-', '_', ' ') else '_' for c in task_name)
        safe_name = safe_name.strip()[:50]  # Limit length
        task_id_short = task_id[:8] if task_id else datetime.now().strftime("%Y%m%d")
        folder_name = f"{safe_name}_{task_id_short}"

        try:
            onedrive_root = find_onedrive_root()
            sessions_root = Path(onedrive_root) / "Shraga Sessions"
            sessions_root.mkdir(exist_ok=True)
            session_folder = sessions_root / folder_name
            session_folder.mkdir(exist_ok=True, parents=True)
            _log(f"[ONEDRIVE] Session folder: {session_folder}")
            return session_folder
        except OneDriveRootNotFoundError as e:
            _log(f"[WARN] OneDrive not found: {e}")
            _log(f"[WARN] Falling back to local work directory")
            fallback = self.work_base_dir / f"agent_task_{folder_name}"
            fallback.mkdir(exist_ok=True, parents=True)
            return fallback

    def load_state(self):
        """Load worker state"""
        state_path = Path(STATE_FILE)
        if state_path.exists():
            try:
                with open(state_path, encoding="utf-8") as f:
                    data = json.load(f)
                    self.current_user_id = data.get("current_user_id")
            except (json.JSONDecodeError, OSError) as e:
                _log(f"[WARN] Could not load state file: {e}")
                self.current_user_id = None

    def save_state(self):
        """Save worker state"""
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "current_user_id": self.current_user_id
            }, f, indent=2)

    def get_token(self):
        """Get OAuth token using DefaultAzureCredential (secure, cached)"""
        try:
            # Return cached token if still valid
            if self._token_cache and self._token_expires:
                if datetime.now(timezone.utc) < self._token_expires:
                    return self._token_cache

            try:
                token = call_with_timeout(
                    lambda: self.credential.get_token(f"{DATAVERSE_URL}/.default"),
                    timeout_sec=30,
                    description="credential.get_token()"
                )
            except TimeoutError:
                _log("[FATAL] get_token() timed out after 30s -- Azure credential hung. Exiting.")
                _log("[HINT] Run: az login")
                sys.exit(1)

            # Cache token (expire 5 minutes early to be safe)
            self._token_cache = token.token
            self._token_expires = datetime.fromtimestamp(token.expires_on, tz=timezone.utc) - timedelta(minutes=5)

            return self._token_cache
        except Exception as e:
            _log(f"[FATAL] Getting token failed: {e} -- Exiting.")
            _log("[HINT] Run: az login")
            sys.exit(1)

    def _get_headers(self, content_type=None, etag=None):
        """Build OData headers with auth token. Returns None if token unavailable."""
        token = self.get_token()
        if not token:
            return None
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0"
        }
        if content_type:
            headers["Content-Type"] = content_type
        if etag:
            headers["If-Match"] = etag
        return headers

    def get_current_user(self):
        """Get current user ID"""
        headers = self._get_headers()
        if not headers:
            return None

        try:
            url = f"{DATAVERSE_URL}/api/data/v9.2/WhoAmI"
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            user_data = response.json()
            user_id = user_data.get("UserId")
            self.current_user_id = user_id
            self.save_state()
            return user_id
        except requests.exceptions.Timeout:
            _log(f"[ERROR] WhoAmI request timed out")
            return None
        except Exception as e:
            _log(f"[ERROR] Getting current user: {e}")
            return None

    def commit_task_results(self, task_id, work_dir):
        """Commit task results to Git for audit trail"""
        try:
            _log(f"[GIT] Committing task {task_id} results...")

            # Add all changes
            subprocess.run(
                ["git", "add", "."],
                cwd=work_dir,
                check=True,
                timeout=30
            )

            # Create commit message
            commit_msg = f"Task {task_id}: Completed by autonomous agent\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>"

            # Commit
            result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                # Check if no changes to commit
                if "nothing to commit" in result.stdout:
                    _log(f"[GIT] No changes to commit for task {task_id}")
                    return None
                _log(f"[WARN] Git commit failed: {result.stderr}")
                return None

            # Get commit SHA
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=work_dir,
                capture_output=True,
                text=True,
                check=True,
                timeout=10
            )

            commit_sha = result.stdout.strip()
            _log(f"[GIT] Committed as {commit_sha[:8]}")

            return commit_sha

        except Exception as e:
            _log(f"[ERROR] Git commit failed: {e}")
            return None

    def poll_pending_tasks(self):
        """Poll for pending tasks assigned to this dev box or unassigned."""
        if not self.current_user_id:
            self.get_current_user()

        headers = self._get_headers()
        if not headers:
            return []

        url = f"{DATAVERSE_URL}/api/data/v9.2/{TABLE}"

        # Filter for Pending tasks with no devbox assigned (open competition).
        # Workers compete for any of this user's unclaimed tasks.
        filter_parts = [
            f"cr_status eq {_STATUS_INT[STATUS_PENDING]}",
            f"(cr_userid eq '{self.current_user_id}' or cr_userid eq '{WEBHOOK_USER}' or crb3b_useremail eq '{WEBHOOK_USER}')",
            f"crb3b_devbox eq null"
        ]

        params = {
            "$filter": " and ".join(filter_parts),
            "$orderby": "createdon asc",
            "$top": 1  # Process one task at a time
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            return data.get("value", [])
        except requests.exceptions.Timeout:
            _log(f"[ERROR] Polling tasks timed out")
            return []
        except Exception as e:
            _log(f"[ERROR] Polling tasks: {e}")
            return []

    def claim_task(self, task: dict) -> bool:
        """Atomically claim a task using ETag optimistic concurrency.

        Sets the task status from Pending to Running using If-Match header.
        Returns True if claimed, False if another worker got it first (HTTP 412).
        Mirrors the pattern in task_manager.py claim_message().
        """
        task_id = task.get("cr_shraga_taskid")
        etag = task.get("@odata.etag")
        if not task_id or not etag:
            _log(f"[WARN] Cannot claim task -- missing id or etag")
            return False

        headers = self._get_headers(
            content_type="application/json",
            etag=etag,
        )
        if not headers:
            return False

        try:
            url = f"{DATAVERSE_URL}/api/data/v9.2/{TABLE}({task_id})"
            body = {
                "cr_status": _STATUS_INT[STATUS_RUNNING],
                "cr_statusmessage": f"Claimed by {MACHINE_NAME}",
                "crb3b_devbox": MACHINE_NAME,
            }
            response = requests.patch(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
            if response.status_code == 412:
                # Someone else claimed it first (optimistic concurrency conflict)
                _log(f"[INFO] Task {task_id} already claimed by another worker")
                return False
            response.raise_for_status()
            _log(f"[CLAIM] Successfully claimed task {task_id}")
            return True
        except requests.exceptions.Timeout:
            _log(f"[WARN] claim_task timed out for {task_id}")
            return False
        except Exception as e:
            _log(f"[ERROR] claim_task: {e}")
            return False

    def is_task_canceled(self, task_id: str) -> bool:
        """Check if a task has been canceled or is being canceled in Dataverse.

        Called periodically during task execution to support cooperative cancellation.
        Returns True for both Canceling(11) and Canceled(9) states.
        """
        if not task_id:
            return False
        headers = self._get_headers()
        if not headers:
            return False
        try:
            url = (
                f"{DATAVERSE_URL}/api/data/v9.2/{TABLE}({task_id})"
                f"?$select=cr_status"
            )
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                status = resp.json().get("cr_status")
                cancel_values = {
                    _STATUS_INT[STATUS_CANCELED], _STATUS_INT[STATUS_CANCELING],
                    STATUS_CANCELED, STATUS_CANCELING,
                }
                if status in cancel_values:
                    _log(f"[CANCEL] Task {task_id[:8]} has been canceled (status={status})")
                    return True
            return False
        except Exception:
            return False

    def update_task(self, task_id: str, status: str = None,
                    status_message: str = None, result: str = None,
                    transcript: str = None, workingdir: str = None,
                    onedriveurl: str = None, session_summary: str = None,
                    short_description: str = None,
                    session_cost: str = None, session_tokens: str = None,
                    session_duration: str = None):
        """Update task in Dataverse"""
        headers = self._get_headers(content_type="application/json")
        if not headers:
            return False

        url = f"{DATAVERSE_URL}/api/data/v9.2/{TABLE}({task_id})"

        data = {}
        if status is not None:
            data["cr_status"] = _STATUS_INT.get(status, status) if isinstance(status, str) else status
        if status_message is not None:
            data["cr_statusmessage"] = status_message
        if result is not None:
            data["cr_result"] = result
        if transcript is not None:
            data["cr_transcript"] = transcript
        if workingdir is not None:
            data["crb3b_workingdir"] = workingdir
        if onedriveurl is not None:
            data["crb3b_onedriveurl"] = onedriveurl
        if session_summary is not None:
            data["crb3b_sessionsummary"] = session_summary
        if short_description is not None:
            data["crb3b_shortdescription"] = short_description
        if session_cost is not None:
            data["crb3b_sessioncost"] = session_cost
        if session_tokens is not None:
            data["crb3b_sessiontokens"] = session_tokens
        if session_duration is not None:
            data["crb3b_sessionduration"] = session_duration

        try:
            response = requests.patch(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return True
        except requests.exceptions.Timeout:
            _log(f"[ERROR] Task update timed out")
            return False
        except Exception as e:
            # If optional DV columns don't exist yet, retry without them
            error_str = str(e).lower()
            optional_columns = ["crb3b_sessionsummary", "crb3b_shortdescription",
                                "crb3b_sessioncost", "crb3b_sessiontokens", "crb3b_sessionduration"]
            removed_any = False
            if "property" in error_str or "column" in error_str or "attribute" in error_str:
                for col in optional_columns:
                    if col in data:
                        _log(f"[WARN] {col} column may not exist yet, removing from update")
                        del data[col]
                        removed_any = True
            if removed_any and data:
                try:
                    response = requests.patch(url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)
                    response.raise_for_status()
                    return True
                except Exception as retry_e:
                    _log(f"[ERROR] Retry without optional columns also failed: {retry_e}")
                    return False
            _log(f"[ERROR] Updating task: {e}")
            return False

    def parse_prompt_with_llm(self, raw_prompt: str) -> dict:
        """
        Use LLM to parse unstructured prompt text into structured fields.
        This handles any format MCS uses without brittle string matching.

        Args:
            raw_prompt: Raw unstructured text from Dataverse

        Returns:
            dict with keys: task_description, success_criteria
        """
        _log("[LLM PARSER] Parsing unstructured prompt...")

        parsing_prompt = f"""You are a prompt parser. Extract the following fields from the raw task prompt below:

1. **task_description**: The main task to accomplish (what needs to be done)
2. **success_criteria**: How to know when the task is complete

Return ONLY a JSON object with these two fields. No markdown, no explanation, just the JSON.

Example output format:
{{
  "task_description": "Create a REST API for user authentication",
  "success_criteria": "API endpoints work and tests pass"
}}

Raw prompt to parse:
{raw_prompt}

JSON output:"""

        try:
            # Call Claude Code CLI for parsing
            cmd = [
                "claude",
                "-p",  # Print mode
                "--output-format", "json",
                "--dangerously-skip-permissions",
            ]

            # Strip CLAUDECODE env var to avoid "nested session" error
            env = {k: v for k, v in os.environ.items() if k != 'CLAUDECODE'}

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                env=env
            )

            # Send prompt and get response
            stdout, stderr = process.communicate(input=parsing_prompt, timeout=30)

            if process.returncode != 0:
                raise Exception(f"Claude Code failed: {stderr}")

            # Parse the JSON output
            response = json.loads(stdout)
            result_text = response.get('result', '').strip()

            # Try to extract JSON from the result
            # Handle cases where Claude wraps JSON in markdown or adds explanation
            start_idx = result_text.find('{')
            end_idx = result_text.rfind('}')

            if start_idx == -1 or end_idx == -1:
                raise Exception(f"No JSON found in response: {result_text}")

            json_str = result_text[start_idx:end_idx+1]
            parsed = json.loads(json_str)

            # Validate required fields
            required_fields = ['task_description', 'success_criteria']
            for field in required_fields:
                if field not in parsed:
                    parsed[field] = ""  # Provide empty default

            _log(f"[LLM PARSER] ✓ Successfully parsed prompt")
            _log(f"  - Task: {parsed['task_description'][:60]}...")
            _log(f"  - Criteria: {parsed['success_criteria'][:60]}...")

            return parsed

        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            _log("[LLM PARSER] ✗ Timeout - falling back to default")
            return {
                'task_description': raw_prompt,
                'success_criteria': 'Review and confirm task is complete'
            }
        except Exception as e:
            _log(f"[LLM PARSER] ✗ Error: {e}")
            _log(f"[LLM PARSER] Falling back to using raw prompt")
            return {
                'task_description': raw_prompt,
                'success_criteria': 'Review and confirm task is complete'
            }

    def generate_short_description(self, raw_prompt: str) -> str:
        """Generate a 1-2 sentence summary of the task prompt using Claude CLI.

        This short description is displayed on Adaptive Cards in Teams instead of
        the full (potentially long) task prompt. It gives the user a quick overview
        of what the task is about.

        Args:
            raw_prompt: The full raw task prompt text from Dataverse.

        Returns:
            A 1-2 sentence summary string. Falls back to the first 120 chars of
            the raw prompt if LLM generation fails.
        """
        _log("[SHORT DESC] Generating short task description...")

        summarize_prompt = (
            "Summarize the following task prompt in exactly 1-2 concise sentences "
            "(max 150 characters total). Focus on the core action requested. "
            "Return ONLY the summary text, no quotes, no explanation.\n\n"
            f"Task prompt:\n{raw_prompt[:2000]}"
        )

        try:
            cmd = [
                "claude",
                "-p",
                "--output-format", "json",
                "--dangerously-skip-permissions",
            ]

            env = {k: v for k, v in os.environ.items() if k != 'CLAUDECODE'}

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                env=env
            )

            stdout, stderr = process.communicate(input=summarize_prompt, timeout=30)

            if process.returncode != 0:
                raise Exception(f"Claude Code failed: {stderr}")

            response = json.loads(stdout)
            result_text = response.get('result', '').strip()

            # Clean up: remove wrapping quotes if present
            if result_text.startswith('"') and result_text.endswith('"'):
                result_text = result_text[1:-1]

            # Enforce max length
            if len(result_text) > 200:
                result_text = result_text[:197] + "..."

            if not result_text:
                raise Exception("Empty result from Claude")

            _log(f"[SHORT DESC] Generated: {result_text}")
            return result_text

        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            _log("[SHORT DESC] Timeout - falling back to truncated prompt")
            fallback = raw_prompt.replace('\n', ' ').strip()[:120]
            if len(raw_prompt) > 120:
                fallback += "..."
            return fallback
        except Exception as e:
            _log(f"[SHORT DESC] Error: {e} - falling back to truncated prompt")
            fallback = raw_prompt.replace('\n', ' ').strip()[:120]
            if len(raw_prompt) > 120:
                fallback += "..."
            return fallback

    def append_to_transcript(self, current_transcript: str, from_who: str, message: str):
        """Append message to JSONL transcript"""
        lines = current_transcript.split("\n") if current_transcript else []

        new_entry = json.dumps({
            "from": from_who,
            "time": datetime.now(timezone.utc).isoformat(),
            "message": message
        })

        lines.append(new_entry)
        return "\n".join(line for line in lines if line.strip())

    def _send_heartbeat(self):
        """Update box heartbeat in crb3b_shragaboxes table."""
        headers = self._get_headers(content_type="application/json")
        if not headers:
            return
        # Find box by hostname
        url = (f"{DATAVERSE_URL}/api/data/v9.2/crb3b_shragaboxes"
               f"?$filter=crb3b_hostname eq '{MACHINE_NAME}'&$select=crb3b_shragaboxid&$top=1")
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                return
            rows = r.json().get("value", [])
            if not rows:
                return
            box_id = rows[0]["crb3b_shragaboxid"]
            patch_url = f"{DATAVERSE_URL}/api/data/v9.2/crb3b_shragaboxes({box_id})"
            from datetime import datetime, timezone
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            requests.patch(patch_url, headers=headers, json={
                "crb3b_lastheartbeat": now_utc,
                "crb3b_boxstatus": "active",
                "crb3b_version": self._my_version or "unknown"
            }, timeout=15)
        except Exception:
            pass  # Non-critical, silently ignore

    def send_to_webhook(self, message: str):
        """Send notification message to Dataverse table"""
        headers = self._get_headers(content_type="application/json")
        if not headers:
            return False

        url = f"{DATAVERSE_URL}/api/data/v9.2/cr_shragamessages"

        # Create message record
        # cr_name: Use first line, max 450 chars (Dataverse primary name column limit ~450-500)
        # cr_content: Full message (no limit)
        # crb3b_taskid: Related task ID for Flow 3A correlation
        first_line = message.split('\n')[0] if '\n' in message else message
        title = first_line[:450] if len(first_line) > 450 else first_line

        data = {
            "cr_name": title,
            "cr_from": "Integrated Task Worker",
            "cr_to": WEBHOOK_USER,
            "cr_content": message  # Full message, no character limit
        }
        # Include task ID if available (for Flow 3A progress card updates)
        if self.current_task_id:
            data["crb3b_taskid"] = self.current_task_id

        try:
            response = requests.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            _log(f"[MESSAGE] Saved: {message[:80]}...")
            return True
        except requests.exceptions.HTTPError as e:
            # Capture detailed error information
            error_detail = e.response.text if hasattr(e.response, 'text') else str(e)
            _log(f"[ERROR] Saving message (HTTP {e.response.status_code}): {error_detail[:500]}")
            _log(f"[ERROR] Message size: {len(message)} chars, Title: {title[:100]}")

            # If message is too large, try truncating content
            if e.response.status_code == 400 and len(message) > 10000:
                _log(f"[RETRY] Message too large ({len(message)} chars), truncating to 10000 chars")
                truncated_message = message[:10000] + "\n\n... (truncated - full result saved in task record)"
                truncated_data = {
                    "cr_name": title,
                    "cr_from": "Integrated Task Worker",
                    "cr_to": WEBHOOK_USER,
                    "cr_content": truncated_message
                }
                if self.current_task_id:
                    truncated_data["crb3b_taskid"] = self.current_task_id
                try:
                    response = requests.post(url, headers=headers, json=truncated_data, timeout=10)
                    response.raise_for_status()
                    _log(f"[MESSAGE] Saved (truncated): {message[:80]}...")
                    return True
                except requests.exceptions.HTTPError as e2:
                    error_detail2 = e2.response.text if hasattr(e2.response, 'text') else str(e2)
                    _log(f"[ERROR] Even truncated message failed (HTTP {e2.response.status_code}): {error_detail2[:500]}")
            return False
        except Exception as e:
            # Handle non-HTTP errors (network, timeout, etc.)
            _log(f"[ERROR] Non-HTTP error saving message: {type(e).__name__}: {e}")
            _log(f"[ERROR] Message size: {len(message)} chars")
            return False

    def fetch_task_activities(self, task_id: str, max_messages: int = 50) -> list:
        """Fetch activity messages from cr_shragamessages for a given task.

        Returns a list of short activity strings, most recent last.
        """
        headers = self._get_headers()
        if not headers:
            return []

        url = f"{DATAVERSE_URL}/api/data/v9.2/cr_shragamessages"
        params = {
            "$filter": f"crb3b_taskid eq '{task_id}'",
            "$orderby": "createdon asc",
            "$top": max_messages,
            "$select": "cr_name,createdon",
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            messages = response.json().get("value", [])
            return [m.get("cr_name", "")[:120] for m in messages if m.get("cr_name")]
        except Exception as e:
            _log(f"[WARN] Could not fetch task activities: {e}")
            return []

    def build_session_summary(self, task_id: str, terminal_status: str,
                              session_folder: Path, accumulated_stats: dict,
                              phases: list, result_text: str,
                              session_id: str = "") -> dict:
        """Build the structured session summary JSON for auditing/telemetry.

        Args:
            task_id: Dataverse task ID
            terminal_status: One of 'completed', 'failed', 'killed'
            session_folder: Path to the OneDrive session folder
            accumulated_stats: Merged stats from all phases
            phases: List of phase dicts with per-phase stats
            result_text: The final result text (will be truncated for preview)
            session_id: Claude session ID (from the last phase, or first non-empty)

        Returns:
            dict: The summary JSON structure
        """
        # Determine dev box name
        dev_box = socket.gethostname() or platform.node() or "unknown"

        # Count unique models -> sub-agents heuristic
        model_usage = accumulated_stats.get("model_usage", {})
        num_sub_agents = max(0, len(model_usage) - 1)  # main model excluded

        # Fetch activities from Dataverse messages
        activities = self.fetch_task_activities(task_id)

        summary = {
            "session_id": session_id,
            "task_id": task_id,
            "dev_box": dev_box,
            "working_dir": str(session_folder),
            "total_duration_ms": accumulated_stats.get("total_duration_ms", 0),
            "total_cost_usd": round(accumulated_stats.get("total_cost_usd", 0.0), 6),
            "total_api_duration_ms": accumulated_stats.get("total_api_duration_ms", 0),
            "total_turns": accumulated_stats.get("total_turns", 0),
            "tokens": accumulated_stats.get("tokens", {
                "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0
            }),
            "model_usage": model_usage,
            "num_sub_agents": num_sub_agents,
            "phases": phases,
            "activities": activities,
            "terminal_status": terminal_status,
            "result_preview": (result_text or "")[:200],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return summary

    def write_session_summary(self, task_id: str, terminal_status: str,
                              session_folder: Path, accumulated_stats: dict,
                              phases: list, result_text: str,
                              session_id: str = ""):
        """Build, persist, and upload the session summary.

        1. Writes session_summary.json to the OneDrive session folder (primary storage).
        2. Attempts to write to DV column crb3b_sessionsummary (graceful if column missing).
        """
        summary = self.build_session_summary(
            task_id=task_id,
            terminal_status=terminal_status,
            session_folder=session_folder,
            accumulated_stats=accumulated_stats,
            phases=phases,
            result_text=result_text,
            session_id=session_id,
        )

        summary_json_str = json.dumps(summary, indent=2, default=str)

        # 1. Write to file in session folder
        summary_file = session_folder / "session_summary.json"
        try:
            summary_file.write_text(summary_json_str, encoding="utf-8")
            _log(f"[SUMMARY] Wrote session_summary.json ({len(summary_json_str)} bytes) to {summary_file}")
        except Exception as e:
            _log(f"[ERROR] Could not write session_summary.json: {e}")

        # 2. Write to Dataverse column (graceful if column doesn't exist)
        try:
            self.update_task(task_id, session_summary=summary_json_str)
            _log(f"[SUMMARY] Wrote session summary to DV column crb3b_sessionsummary")
        except Exception as e:
            _log(f"[WARN] Could not write session summary to DV: {e}")

        return summary

    def write_task_prompt_file(self, session_folder: Path, raw_prompt: str,
                               success_criteria: str = ""):
        """Write the full raw task prompt and success criteria to the session folder.

        Creates TASK_PROMPT.md containing the original unprocessed prompt text
        exactly as received from Dataverse, and SUCCESS_CRITERIA.md with the
        extracted success criteria.  These files provide a complete audit trail
        of what the agent was asked to do.

        Args:
            session_folder: Path to the OneDrive session folder.
            raw_prompt: The original cr_prompt text from Dataverse (unprocessed).
            success_criteria: Extracted success criteria string.
        """
        # --- TASK_PROMPT.md ---
        try:
            prompt_file = session_folder / "TASK_PROMPT.md"
            content = f"# Full Task Prompt\n\n{raw_prompt or ''}"
            prompt_file.write_text(content, encoding="utf-8")
            _log(f"[FILES] Wrote TASK_PROMPT.md ({len(content)} chars) to {prompt_file}")
        except Exception as e:
            _log(f"[ERROR] Could not write TASK_PROMPT.md: {e}")

        # --- SUCCESS_CRITERIA.md ---
        try:
            criteria_file = session_folder / "SUCCESS_CRITERIA.md"
            criteria_content = f"# Success Criteria\n\n{success_criteria or ''}"
            criteria_file.write_text(criteria_content, encoding="utf-8")
            _log(f"[FILES] Wrote SUCCESS_CRITERIA.md ({len(criteria_content)} chars) to {criteria_file}")
        except Exception as e:
            _log(f"[ERROR] Could not write SUCCESS_CRITERIA.md: {e}")

    def capture_git_history(self, session_folder: Path, work_dir: Path = None):
        """Capture git commit history and write GIT_HISTORY.md to the session folder.

        Runs ``git log`` in the specified work directory (or repo_path) and writes
        the output to the session folder so the commit trail is preserved alongside
        other session artifacts.

        Args:
            session_folder: Path to the OneDrive session folder.
            work_dir: Directory containing the git repository.  Defaults to self.repo_path.
        """
        if work_dir is None:
            work_dir = self.repo_path

        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "--no-decorate", "-50"],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=15,
            )

            if result.returncode != 0:
                _log(f"[WARN] git log failed (rc={result.returncode}): {result.stderr}")
                git_log_text = f"(git log failed: {result.stderr.strip()})"
            else:
                git_log_text = result.stdout.strip() or "(no commits)"

            history_file = session_folder / "GIT_HISTORY.md"
            content = f"# Git Commit History\n\n```\n{git_log_text}\n```\n"
            history_file.write_text(content, encoding="utf-8")
            _log(f"[FILES] Wrote GIT_HISTORY.md ({len(content)} chars) to {history_file}")
        except subprocess.TimeoutExpired:
            _log("[WARN] git log timed out")
        except Exception as e:
            _log(f"[ERROR] Could not capture git history: {e}")

    def write_result_and_transcript_files(self, session_folder: Path,
                                           result_text: str = "",
                                           transcript: str = ""):
        """Write result.md and transcript.md to the session folder.

        Called on every terminal state (completed, failed, canceled) so that
        the OneDrive session folder always contains a human-readable copy of
        the result and the full JSONL transcript.

        Args:
            session_folder: Path to the OneDrive session folder.
            result_text: The cr_result content (final result or error message).
            transcript: The cr_transcript content (JSONL transcript string).
        """
        # --- result.md ---
        try:
            result_file = session_folder / "result.md"
            result_file.write_text(result_text or "", encoding="utf-8")
            _log(f"[FILES] Wrote result.md ({len(result_text or '')} chars) to {result_file}")
        except Exception as e:
            _log(f"[ERROR] Could not write result.md: {e}")

        # --- transcript.md ---
        try:
            transcript_file = session_folder / "transcript.md"
            transcript_file.write_text(transcript or "", encoding="utf-8")
            _log(f"[FILES] Wrote transcript.md ({len(transcript or '')} chars) to {transcript_file}")
        except Exception as e:
            _log(f"[ERROR] Could not write transcript.md: {e}")

    def write_session_log(self, summary: dict, session_folder: Path,
                          result_text: str = "", folder_url: str = ""):
        """Write a human-readable SESSION_LOG.md to the session folder.

        Uses data already collected in the session summary dict to produce a
        rich markdown file covering: task metadata, dev-box details, activity
        log, results, session stats, and transcript reference.

        Args:
            summary: The session summary dict (from build_session_summary).
            session_folder: Path to the OneDrive session folder.
            result_text: Full final result text.
            folder_url: OneDrive web URL for the session folder (may be empty).
        """
        try:
            # --- Header ---
            task_id = summary.get("task_id", "unknown")
            session_id = summary.get("session_id", "")
            dev_box = summary.get("dev_box", "unknown")
            terminal_status = summary.get("terminal_status", "unknown")
            timestamp = summary.get("timestamp", "")
            working_dir = summary.get("working_dir", str(session_folder))

            lines = [
                "# SESSION LOG",
                "",
                "## Task Information",
                "",
                f"| Field | Value |",
                f"|-------|-------|",
                f"| Task ID | `{task_id}` |",
                f"| Terminal Status | **{terminal_status}** |",
                f"| Timestamp | {timestamp} |",
                f"| Dev Box | `{dev_box}` |",
                f"| Worker Version | `{self._my_version}` |",
                f"| Claude Session ID | `{session_id}` |",
                f"| Working Directory | `{working_dir}` |",
            ]

            if folder_url:
                lines.append(f"| OneDrive URL | [Open in OneDrive]({folder_url}) |")

            lines.append("")

            # --- Session Stats ---
            duration_ms = summary.get("total_duration_ms", 0)
            duration_s = duration_ms / 1000.0 if duration_ms else 0
            duration_min = duration_s / 60.0
            cost_usd = summary.get("total_cost_usd", 0.0)
            api_duration_ms = summary.get("total_api_duration_ms", 0)
            total_turns = summary.get("total_turns", 0)
            tokens = summary.get("tokens", {})
            input_tokens = tokens.get("input", 0)
            output_tokens = tokens.get("output", 0)
            cache_read = tokens.get("cache_read", 0)
            cache_creation = tokens.get("cache_creation", 0)

            lines.extend([
                "## Session Stats",
                "",
                f"| Metric | Value |",
                f"|--------|-------|",
                f"| Total Duration | {duration_min:.1f} min ({duration_s:.0f}s) |",
                f"| API Duration | {api_duration_ms / 1000.0:.1f}s |",
                f"| Total Cost | ${cost_usd:.4f} |",
                f"| Total Turns | {total_turns} |",
                f"| Input Tokens | {input_tokens:,} |",
                f"| Output Tokens | {output_tokens:,} |",
                f"| Cache Read Tokens | {cache_read:,} |",
                f"| Cache Creation Tokens | {cache_creation:,} |",
                f"| Sub-agents Used | {summary.get('num_sub_agents', 0)} |",
                "",
            ])

            # --- Model Usage ---
            model_usage = summary.get("model_usage", {})
            if model_usage:
                lines.extend([
                    "## Model Usage",
                    "",
                    "| Model | Cost | Input Tokens | Output Tokens |",
                    "|-------|------|-------------|---------------|",
                ])
                for model_id, mu in model_usage.items():
                    m_cost = mu.get("cost_usd", 0.0)
                    m_in = mu.get("input_tokens", 0)
                    m_out = mu.get("output_tokens", 0)
                    lines.append(f"| {model_id} | ${m_cost:.4f} | {m_in:,} | {m_out:,} |")
                lines.append("")

            # --- Phases ---
            phases = summary.get("phases", [])
            if phases:
                lines.extend([
                    "## Execution Phases",
                    "",
                    "| Phase | Cost | Duration | Turns |",
                    "|-------|------|----------|-------|",
                ])
                for p in phases:
                    p_name = p.get("phase", "?")
                    p_cost = p.get("cost_usd", 0.0)
                    p_dur = p.get("duration_ms", 0)
                    p_turns = p.get("turns", 0)
                    lines.append(f"| {p_name} | ${p_cost:.4f} | {p_dur / 1000.0:.1f}s | {p_turns} |")
                lines.append("")

            # --- Activity Log ---
            activities = summary.get("activities", [])
            if activities:
                lines.extend([
                    "## Activity Log",
                    "",
                ])
                for idx, activity in enumerate(activities, 1):
                    lines.append(f"{idx}. {activity}")
                lines.append("")

            # --- Final Results ---
            lines.extend([
                "## Final Results",
                "",
            ])
            if result_text:
                lines.append(result_text.strip())
            else:
                preview = summary.get("result_preview", "")
                lines.append(preview if preview else "(no result text)")
            lines.append("")

            # --- Transcript Reference ---
            lines.extend([
                "## Transcript Reference",
                "",
                f"- Full transcript is stored in the Dataverse task record (`cr_transcript` column).",
                f"- Task ID for lookup: `{task_id}`",
            ])
            if session_id:
                lines.append(f"- Claude Code session ID: `{session_id}`")
            lines.append("")

            # --- Write file ---
            log_content = "\n".join(lines)
            log_file = session_folder / "SESSION_LOG.md"
            log_file.write_text(log_content, encoding="utf-8")
            _log(f"[SESSION LOG] Wrote SESSION_LOG.md ({len(log_content)} bytes) to {log_file}")

        except Exception as e:
            _log(f"[ERROR] Could not write SESSION_LOG.md: {e}")

    def execute_with_autonomous_agent(self, task_prompt: str, task_id: str,
                                      current_transcript: str, parsed_prompt_data: dict = None):
        """
        Execute task using the autonomous agent Worker/Verifier system

        Args:
            task_prompt: Structured prompt (description + contact rules + criteria)
            task_id: Task ID for Dataverse updates
            current_transcript: Current JSONL transcript
        """
        _log(f"[AUTONOMOUS AGENT] Starting Worker/Verifier loop...")
        task_start_time = time.time()

        # Use LLM to parse the unstructured prompt (or reuse if already parsed)
        if parsed_prompt_data:
            _log("[LLM PARSER] Using previously parsed prompt data")
            parsed = parsed_prompt_data
        else:
            parsed = self.parse_prompt_with_llm(task_prompt)

        task_description = parsed['task_description']
        success_criteria = parsed['success_criteria']

        # Create OneDrive session folder for this task
        task_name = task_description[:50] if task_description else "unnamed_task"
        session_folder = self.create_session_folder(task_name, task_id)

        # Write full task prompt and success criteria to session folder (T048)
        self.write_task_prompt_file(session_folder, task_prompt, success_criteria)

        # Create autonomous agent instance
        agent = AgentCLI()

        # Setup project in the OneDrive session folder
        project_folder = agent.setup_project(
            task_description,
            success_criteria,
            project_folder_path=session_folder
        )

        # Update task row with the OneDrive session folder path and URL
        self.update_task(task_id, workingdir=str(session_folder))
        folder_url = local_path_to_web_url(str(session_folder))
        if folder_url and folder_url.startswith("http"):
            self.update_task(task_id, onedriveurl=folder_url)

        # Store project folder reference
        transcript = current_transcript
        transcript = self.append_to_transcript(
            transcript,
            "system",
            f"Created project folder: {project_folder}"
        )

        self.update_task(
            task_id,
            status=STATUS_RUNNING,
            status_message="Worker/Verifier loop started",
            transcript=transcript
        )

        self.send_to_webhook(f"Starting Worker/Verifier loop\nProject: {project_folder}")

        # Stats accumulation
        accumulated_stats = {}
        phases = []
        last_session_id = ""

        # Define streaming event callback
        def streaming_event(event_type, data):
            """Callback for streaming events from autonomous agent"""
            if event_type == 'text':
                # Send Claude's reasoning/thoughts
                content = data.get('content', '').strip()
                if content:
                    # Send the actual text content to Dataverse
                    self.send_to_webhook(content)
            elif event_type == 'progress':
                # Send periodic progress updates (every 30 seconds)
                elapsed = data.get('elapsed', 0)
                if elapsed > 0 and elapsed % 30 == 0:
                    self.send_to_webhook(f"Still working... ({elapsed}s elapsed)")

        def _record_phase(phase_name: str, phase_stats: dict):
            """Record a phase and merge its stats into the accumulator."""
            nonlocal accumulated_stats, last_session_id
            merge_phase_stats(accumulated_stats, phase_stats)
            phases.append({
                "phase": phase_name,
                "cost_usd": round(phase_stats.get("cost_usd", 0.0), 6),
                "duration_ms": phase_stats.get("duration_ms", 0),
                "turns": phase_stats.get("num_turns", 0),
            })
            if phase_stats.get("session_id"):
                last_session_id = phase_stats["session_id"]

        def _finalize_summary(terminal_status: str, result_text: str):
            """Write the session summary, session log, result.md and transcript.md for any terminal state."""
            try:
                summary = self.write_session_summary(
                    task_id=task_id,
                    terminal_status=terminal_status,
                    session_folder=session_folder,
                    accumulated_stats=accumulated_stats,
                    phases=phases,
                    result_text=result_text,
                    session_id=last_session_id,
                )
                self.write_session_log(
                    summary=summary,
                    session_folder=session_folder,
                    result_text=result_text,
                    folder_url=folder_url if folder_url else "",
                )
            except Exception as e:
                _log(f"[ERROR] Failed to write session summary: {e}")

            # Write result.md and transcript.md to session folder
            try:
                self.write_result_and_transcript_files(
                    session_folder=session_folder,
                    result_text=result_text,
                    transcript=transcript,
                )
            except Exception as e:
                _log(f"[ERROR] Failed to write result/transcript files: {e}")

            # Capture git commit history (T048)
            try:
                self.capture_git_history(session_folder)
            except Exception as e:
                _log(f"[ERROR] Failed to capture git history: {e}")

        try:
            # Worker/Verifier loop
            iteration = 1
            verifier_feedback = None

            while iteration <= 10:  # Max 10 iterations
                # Check for cancellation before each iteration
                if self.is_task_canceled(task_id):
                    cancel_msg = "Task canceled by user"
                    self.send_to_webhook(cancel_msg)
                    _finalize_summary("canceled", cancel_msg)
                    return False, cancel_msg, transcript, accumulated_stats

                _log(f"\n[ITERATION {iteration}]")

                # Update Dataverse
                transcript = self.append_to_transcript(
                    transcript,
                    "system",
                    f"Starting iteration {iteration}"
                )
                self.update_task(
                    task_id,
                    status_message=f"Worker iteration {iteration}",
                    transcript=transcript
                )

                # Worker phase
                _log(f"[PHASE] Worker starting (iteration {iteration})")
                status, output, worker_stats = agent.worker_loop(iteration, verifier_feedback, on_event=streaming_event)
                _log(f"[PHASE] Worker finished: status={status}")
                _record_phase(f"worker_{iteration}", worker_stats)

                # Log worker output
                _log(f"[PHASE] Updating transcript after worker")
                transcript = self.append_to_transcript(
                    transcript,
                    "worker",
                    output
                )
                self.update_task(task_id, transcript=transcript)
                _log(f"[PHASE] Transcript updated in Dataverse")

                if status == "done":
                    # Check for cancellation before verification
                    _log(f"[PHASE] Checking cancellation before verification")
                    if self.is_task_canceled(task_id):
                        cancel_msg = "Task canceled by user (before verification)"
                        self.send_to_webhook(cancel_msg)
                        _finalize_summary("canceled", cancel_msg)
                        return False, cancel_msg, transcript, accumulated_stats

                    # Verification phase
                    _log(f"\n[VERIFICATION]")
                    _log(f"[PHASE] Verifier starting (iteration {iteration})")

                    transcript = self.append_to_transcript(
                        transcript,
                        "system",
                        "Starting verification"
                    )
                    self.update_task(
                        task_id,
                        status_message=f"Verifying iteration {iteration}",
                        transcript=transcript
                    )

                    approved, feedback, verifier_stats = agent.verify_work(output, on_event=streaming_event)
                    _log(f"[PHASE] Verifier finished: approved={approved}")
                    _record_phase(f"verifier_{iteration}", verifier_stats)

                    # Log verifier output
                    transcript = self.append_to_transcript(
                        transcript,
                        "verifier",
                        feedback if not approved else "APPROVED"
                    )
                    self.update_task(task_id, transcript=transcript)

                    if approved:
                        # Task completed successfully!
                        _log(f"\n[SUCCESS] Task approved by verifier")

                        # Create summary
                        _log(f"[PHASE] Summarizer starting")
                        self.send_to_webhook("Creating summary of results...")

                        summary, summarizer_stats = agent.create_summary(on_event=streaming_event)
                        _log(f"[PHASE] Summarizer finished")
                        _record_phase("summarizer", summarizer_stats)

                        # Log summary creation
                        transcript = self.append_to_transcript(
                            transcript,
                            "summarizer",
                            "SUMMARY CREATED"
                        )
                        self.update_task(task_id, transcript=transcript)

                        # Build concise bullet-style result with OneDrive links
                        result_folder_url = folder_url if folder_url else str(session_folder)
                        final_result = f"{summary}\n\n- Session folder: [View in OneDrive]({result_folder_url})"

                        # Write session summary for completed state
                        _finalize_summary("completed", final_result)

                        return True, final_result, transcript, accumulated_stats

                    else:
                        # Verification failed, loop back to worker
                        _log(f"\n[RETRY] Verification failed, providing feedback to worker")

                        self.send_to_webhook(
                            f"Verification failed (iteration {iteration}). Retrying with feedback..."
                        )

                        verifier_feedback = feedback
                        iteration += 1
                        continue

            # Max iterations reached
            max_iter_msg = f"Max iterations ({iteration-1}) reached without approval"
            _finalize_summary("failed", max_iter_msg)
            return False, max_iter_msg, transcript, accumulated_stats

        except Exception as e:
            error_msg = f"Error during autonomous execution: {e}"
            _log(f"[ERROR] {error_msg}")

            transcript = self.append_to_transcript(
                transcript,
                "system",
                f"[ERROR] {error_msg}"
            )

            # Write summary for error/failed terminal state
            _finalize_summary("failed", error_msg)

            return False, error_msg, transcript, accumulated_stats

    def process_task(self, task: dict):
        """Process a single task using autonomous agent.

        Uses ETag-based atomic claiming to prevent double-pickup, and
        queues the task if this dev box is already running another task.
        """
        task_id = task.get("cr_shraga_taskid")
        task_name = task.get("cr_name", "Unnamed")
        prompt = task.get("cr_prompt", "")
        transcript = task.get("cr_transcript", "")

        _log(f"\n{'='*80}")
        _log(f"[TASK] Processing: {task_name}")
        _log(f"[ID] {task_id}")
        _log(f"{'='*80}")

        # Atomically claim the task using ETag (prevents double-pickup)
        if not self.claim_task(task):
            _log(f"[SKIP] Could not claim task {task_id}, moving on")
            return False

        self.current_task_id = task_id  # Track for message correlation

        # Log task start in transcript
        transcript = self.append_to_transcript(
            transcript,
            "system",
            "[Task started with autonomous agent]"
        )

        self.update_task(
            task_id,
            status_message="Starting autonomous agent",
            transcript=transcript
        )

        # Parse prompt with LLM to extract task description for notification
        parsed_prompt = self.parse_prompt_with_llm(prompt)
        task_description = parsed_prompt.get('task_description', '')[:300]
        if len(parsed_prompt.get('task_description', '')) > 300:
            task_description += "..."

        # Generate a 1-2 sentence short description for Adaptive Card display
        short_desc = self.generate_short_description(prompt)
        self.update_task(task_id, short_description=short_desc)

        # Send task start notification with details
        start_msg = f"""Starting task: {task_name}

Description: {task_description if task_description else "No description provided"}

Task ID: {task_id}
Worker/Verifier loop initiated..."""

        self.send_to_webhook(start_msg)

        # Execute with autonomous agent (pass parsed prompt to avoid reparsing)
        success, result, final_transcript, session_stats = self.execute_with_autonomous_agent(
            prompt,
            task_id,
            transcript,
            parsed_prompt_data=parsed_prompt  # Reuse parsed data
        )

        # Session numbers are stored in session_summary.json (via write_session_summary),
        # not appended to cr_result, to keep completed cards shorter and cleaner.

        # Extract session stats for card display
        _cost_str = _tokens_str = _duration_str = None
        if session_stats:
            cost = session_stats.get("total_cost_usd", 0.0)
            _cost_str = f"${cost:.2f}"
            tokens = session_stats.get("tokens", {})
            tok_in = tokens.get("input", 0)
            tok_out = tokens.get("output", 0)
            tok_cache = tokens.get("cache_read", 0)
            parts = [f"{tok_in/1000:.1f}k in", f"{tok_out/1000:.1f}k out"]
            if tok_cache > 0:
                parts.append(f"{tok_cache/1000:.1f}k cached")
            _tokens_str = " / ".join(parts)
            total_ms = session_stats.get("total_duration_ms", 0)
            total_sec = total_ms / 1000
            mins = int(total_sec // 60)
            secs = int(total_sec % 60)
            _duration_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"

        # Update final status
        if success:
            # Commit results to Git for audit trail
            commit_sha = self.commit_task_results(task_id, self.work_base_dir)

            # Prepare result with Git commit info
            result_with_git = result
            if commit_sha:
                result_with_git = f"{result}\n\n[Git Commit: {commit_sha[:8]}]"

            self.update_task(
                task_id,
                status=STATUS_COMPLETED,
                status_message="Task completed and verified",
                result=result_with_git,
                transcript=final_transcript,
                session_cost=_cost_str,
                session_tokens=_tokens_str,
                session_duration=_duration_str
            )

            # Send completion message with full result and Git info
            git_info = f"\nGit Commit: {commit_sha[:8]}" if commit_sha else ""
            completion_msg = f"""Task completed: {task_name}

Result:
{result}

Full details saved in Dataverse (Task ID: {task_id}){git_info}"""

            self.send_to_webhook(completion_msg)

            _log(f"[TASK] Completed: {task_name}\n")
            self.current_task_id = None
            return True
        else:
            self.update_task(
                task_id,
                status=STATUS_FAILED,
                status_message="Task failed",
                result=f"Error: {result}",
                transcript=final_transcript,
                session_cost=_cost_str,
                session_tokens=_tokens_str,
                session_duration=_duration_str
            )

            # Send failure message with error details
            failure_msg = f"""Task failed: {task_name}

Error details:
{result}

Full transcript saved in Dataverse (Task ID: {task_id})"""

            self.send_to_webhook(failure_msg)

            _log(f"[TASK] Failed: {task_name}\n")
            self.current_task_id = None
            return False

    def run(self):
        """Main worker loop"""
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')

        _log("="*80)
        _log("INTEGRATED TASK WORKER (with Autonomous Agent)")
        _log("="*80)
        _log(f"Dataverse: {DATAVERSE_URL}")
        _log(f"Table: {TABLE}")
        _log(f"Dev box: {MACHINE_NAME}")
        _log(f"Autonomous Agent Dir: {self.work_base_dir}")
        _log("="*80)

        # Get current user
        if not self.current_user_id:
            _log("Identifying user...")
            if not self.get_current_user():
                _log("[FATAL] Could not identify user")
                return

        _log(f"Worker started for user: {self.current_user_id}")
        _log(f"Current version: {self._my_version}")

        # Clean up any tasks left in Running from a previous crash
        self._cleanup_orphaned_tasks()

        _log("\n[POLLING] Monitoring for pending tasks...\n")

        # Send startup notification
        self.send_to_webhook(f"Worker started (v{self._my_version}) on {MACHINE_NAME}")

        # Heartbeat tracking
        last_heartbeat = 0
        heartbeat_interval = 300  # 5 minutes

        try:
            while True:
                try:
                    # Poll for pending tasks
                    try:
                        tasks = self.poll_pending_tasks()
                    except Exception as e:
                        _log(f"[ERROR] Error polling for tasks: {e}")
                        try:
                            self.send_to_webhook(f"Error polling for tasks: {e}")
                        except Exception:
                            pass
                        time.sleep(60)
                        continue

                    if tasks:
                        _log(f"[FOUND] {len(tasks)} pending task(s)")

                        for task in tasks:
                            # Check if a new release is available
                            if should_exit(self._my_version):
                                _log(f"[UPDATE] New release detected. Exiting to restart with new version.")
                                sys.exit(0)
                            try:
                                self.process_task(task)
                            except Exception as e:
                                _log(f"[ERROR] Unhandled exception processing task: {e}")
                                self._cleanup_in_progress_task(f"Unhandled error: {e}")
                                try:
                                    self.send_to_webhook(f"Task failed with unhandled error: {e}")
                                except Exception:
                                    pass
                    else:
                        # IDLE - Check if a new release is available
                        if should_exit(self._my_version):
                            _log(f"[UPDATE] New release detected. Exiting to restart with new version.")
                            sys.exit(0)

                    # Heartbeat: update box status in DV every 5 minutes
                    now = time.time()
                    if now - last_heartbeat >= heartbeat_interval:
                        try:
                            self._send_heartbeat()
                            last_heartbeat = now
                        except Exception as e:
                            _log(f"[WARN] Heartbeat failed: {e}")

                    # Poll every 10 seconds (autonomous agent takes longer)
                    time.sleep(10)

                except KeyboardInterrupt:
                    raise  # Let KeyboardInterrupt propagate to outer handler
                except BaseException as e:
                    _log(f"\n[ERROR] Unexpected error in worker loop: {type(e).__name__}: {e}")
                    self._cleanup_in_progress_task(f"Worker loop error: {e}")
                    try:
                        self.send_to_webhook(f"Worker error (recovering): {type(e).__name__}: {e}")
                    except Exception:
                        pass
                    time.sleep(60)
                    continue

        except KeyboardInterrupt:
            _log("\n\n[INTERRUPT] Stopping worker...")
            self._cleanup_in_progress_task("Worker interrupted by user")
            try:
                self.send_to_webhook("Task worker stopped")
            except Exception:
                pass

        _log("[SHUTDOWN] Worker stopped")

    def _cleanup_in_progress_task(self, reason: str):
        """Mark the current in-progress task as failed so it doesn't stay stuck in RUNNING."""
        if self.current_task_id:
            _log(f"[CLEANUP] Marking task {self.current_task_id} as failed: {reason}")
            self.update_task(
                self.current_task_id,
                status=STATUS_FAILED,
                status_message=f"Worker shutdown: {reason}",
                result=f"Error: {reason}"
            )
            self.current_task_id = None

    def _cleanup_orphaned_tasks(self):
        """On startup, find tasks stuck in Running on this dev box and fail them.

        This handles the case where the Worker process crashed or was killed
        mid-task. Without this, orphaned tasks stay in Running forever.
        """
        headers = self._get_headers()
        if not headers:
            return

        url = f"{DATAVERSE_URL}/api/data/v9.2/{TABLE}"
        params = {
            "$filter": (
                f"cr_status eq {_STATUS_INT[STATUS_RUNNING]}"
                f" and crb3b_devbox eq '{MACHINE_NAME}'"
            ),
            "$select": "cr_shraga_taskid,cr_name",
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            orphans = response.json().get("value", [])
        except Exception as e:
            _log(f"[WARN] Could not check for orphaned tasks: {e}")
            return

        for task in orphans:
            tid = task.get("cr_shraga_taskid")
            tname = task.get("cr_name", "Unknown")
            _log(f"[CLEANUP] Orphaned task found: {tname} ({tid}) -- marking as failed")
            self.update_task(
                tid,
                status=STATUS_FAILED,
                status_message="Worker crashed or restarted -- task was orphaned",
                result="Error: Worker process died while executing this task. The task was left in Running state and has been marked as failed on restart."
            )
            try:
                self.send_to_webhook(f"Orphaned task cleaned up on restart: {tname} ({tid[:8]})")
            except Exception:
                pass

if __name__ == "__main__":
    try:
        worker = IntegratedTaskWorker()
        worker.run()
    except KeyboardInterrupt:
        _log("[SHUTDOWN] Worker stopped by user (Ctrl+C)")
    except BaseException as e:
        # Last-resort logging before the process dies.
        # Capture as much context as possible for post-mortem debugging.
        import traceback
        tb = traceback.format_exc()
        _log(f"[FATAL] Worker process dying: {type(e).__name__}: {e}")
        _log(f"[FATAL] Traceback:\n{tb}")
        _log(f"[FATAL] Version: {getattr(worker, '_my_version', 'unknown') if 'worker' in dir() else 'unknown'}")
        _log(f"[FATAL] Current task: {getattr(worker, 'current_task_id', 'none') if 'worker' in dir() else 'unknown'}")
        _log(f"[FATAL] Machine: {MACHINE_NAME}")
        sys.exit(1)
