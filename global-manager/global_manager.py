"""
Global Manager -- Thin wrapper + persistent Claude Code session architecture.

Polls the conversations table for any unclaimed inbound messages older than 15s.
Handles new user onboarding (dev box provisioning) and users whose personal
manager is down.

Session continuity is managed by session_utils.resolve_session(), which uses
Dataverse (cr_processed_by column) as the single source of truth. No local
session JSON files.

The GM's responsibilities are limited to:
  1. Poll DV for unclaimed Inbound messages
  2. Claim messages with ETag-based optimistic concurrency
  3. Resolve session via DV-based session_utils
  4. Run `claude --resume {session_id} --print -p "{user_message}"`
  5. Write Claude's response back to DV with cr_processed_by
  6. Add [GM:xxxx] message prefix for session tracking

The ONLY hardcoded user-facing message is the single fallback for when Claude
Code is completely unavailable:
    "The system is temporarily unavailable, please try again shortly."
"""
import logging
from logging.handlers import RotatingFileHandler
import json
import time
import os
import sys
import socket
import subprocess
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(Path(__file__).parent.parent))
from timeout_utils import call_with_timeout
from dv_client import DataverseClient, DataverseError, DataverseRetryExhausted, ETagConflictError, create_credential
from session_utils import resolve_session, sanitize_odata

os.environ.setdefault('PYTHONUNBUFFERED', '1')

# Unique instance ID for this process (helps distinguish multiple GM instances)
INSTANCE_ID = uuid.uuid4().hex[:8]
AGENT_ROLE = "GM"

# --- File logging ---
_LOG_FILE = Path(__file__).parent / "gm.log"

_file_logger = logging.getLogger("shraga_gm")
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

DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
CONVERSATIONS_TABLE = os.environ.get("CONVERSATIONS_TABLE", "cr_shraga_conversations")
USERS_TABLE = os.environ.get("USERS_TABLE", "crb3b_shragausers")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))  # seconds
CLAIM_DELAY_NEW_USER = int(os.environ.get("CLAIM_DELAY_NEW_USER", "0"))  # immediate for new users
CLAIM_DELAY_KNOWN_USER = int(os.environ.get("CLAIM_DELAY_KNOWN_USER", "30"))  # 30s for known users
REQUEST_TIMEOUT = 30

logger = logging.getLogger(__name__)

# Conversation direction (string values in Dataverse)
DIRECTION_INBOUND = "Inbound"
DIRECTION_OUTBOUND = "Outbound"

# Conversation status (string values in Dataverse)
STATUS_UNCLAIMED = "Unclaimed"
STATUS_CLAIMED = "Claimed"
STATUS_PROCESSED = "Processed"

# Single fallback message for when Claude CLI is unavailable
FALLBACK_MESSAGE = "The system is temporarily unavailable, please try again shortly."


def get_credential():
    """Get Azure credential via AzureCliCredential.

    Requires a valid ``az login`` session.  Returns an ``AzureCliCredential``
    instance after verifying it can obtain a Dataverse token.
    """
    cred = create_credential(log_fn=_log)
    try:
        call_with_timeout(
            lambda: cred.get_token(f"{DATAVERSE_URL}/.default"),
            timeout_sec=30,
            description="initial credential.get_token()"
        )
    except TimeoutError:
        _log("[CRITICAL] Initial credential.get_token() timed out after 30s. Exiting.")
        _log("[CRITICAL] HINT: Run 'az login'")
        sys.exit(1)
    except Exception as e:
        _log(f"[CRITICAL] Initial credential.get_token() failed: {e} -- Exiting.")
        _log("[CRITICAL] HINT: Run 'az login'")
        sys.exit(1)
    _log("[AUTH] Using existing Azure credentials")
    return cred


# ── Global Manager ───────────────────────────────────────────────────────

class GlobalManager:
    """Thin wrapper manager for orphaned messages and new user onboarding.

    Delegates all decision-making to Claude Code via persistent sessions.
    Claude Code reads CLAUDE.md and runs scripts directly.
    """

    def __init__(self):
        self.manager_id = "global"
        self.credential = get_credential()
        self.dv = DataverseClient(
            credential=self.credential,
            dataverse_url=DATAVERSE_URL,
            log_fn=_log,
        )
        self._known_users: set[str] = set()
        # System prompt file path (passed via --system-prompt-file)
        prompt_file = Path(__file__).parent / "GM_SYSTEM_PROMPT.md"
        self._system_prompt_file = str(prompt_file) if prompt_file.exists() else ""
        if self._system_prompt_file:
            _log(f"[CONFIG] System prompt: {prompt_file} ({prompt_file.stat().st_size} bytes)")
        else:
            _log(f"[WARN] No system prompt found at {prompt_file}")
        # Version and box info for cr_claimed_by
        from version_check import get_my_version
        self._my_version = get_my_version(__file__)
        self._box_name = os.environ.get("COMPUTERNAME", socket.gethostname())
        # Track current session ID per mcs_conversation_id (within this process run)
        self._current_sessions: dict[str, str] = {}

    # ── User Lookup (for differential claiming delay) ─────────────────

    def _is_known_user(self, user_email: str) -> bool:
        """Check if a user exists in the DV users table (for claim delay logic)."""
        if user_email in self._known_users:
            return True
        try:
            url = (
                f"{DATAVERSE_API}/{USERS_TABLE}"
                f"?$filter=crb3b_useremail eq '{sanitize_odata(user_email)}'"
                f"&$top=1&$select=crb3b_shragauserid"
            )
            resp = self.dv.get(url, timeout=REQUEST_TIMEOUT)
            rows = resp.json().get("value", [])
            if rows:
                self._known_users.add(user_email)
                return True
            return False
        except (DataverseError, DataverseRetryExhausted):
            return False

    # ── Conversations ─────────────────────────────────────────────────

    def poll_stale_unclaimed(self) -> list[dict]:
        """Poll for unclaimed inbound messages with differential delay."""
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_DELAY_NEW_USER)
            cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
            url = (
                f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
                f"?$filter=cr_direction eq '{DIRECTION_INBOUND}'"
                f" and cr_status eq '{STATUS_UNCLAIMED}'"
                f" and createdon lt {cutoff_str}"
                f"&$orderby=createdon asc"
                f"&$top=10"
            )
            resp = self.dv.get(url, timeout=REQUEST_TIMEOUT)
            all_unclaimed = resp.json().get("value", [])

            if not all_unclaimed:
                return []

            now = datetime.now(timezone.utc)
            known_cutoff = now - timedelta(seconds=CLAIM_DELAY_KNOWN_USER)
            claimable = []
            for msg in all_unclaimed:
                user_email = msg.get("cr_useremail", "")
                is_known = self._is_known_user(user_email)

                if not is_known:
                    claimable.append(msg)
                else:
                    created_str = msg.get("createdon", "")
                    try:
                        created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        if created < known_cutoff:
                            claimable.append(msg)
                    except (ValueError, TypeError):
                        claimable.append(msg)

            return claimable
        except DataverseRetryExhausted as e:
            _log(f"[WARN] poll_stale_unclaimed: retry exhausted: {e}")
            return []
        except DataverseError as e:
            _log(f"[ERROR] poll_stale_unclaimed: {e}")
            return []

    def claim_message(self, msg: dict) -> bool:
        row_id = msg.get("cr_shraga_conversationid")
        etag = msg.get("@odata.etag")
        if not row_id or not etag:
            return False
        try:
            url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}({row_id})"
            claimed_by = f"{AGENT_ROLE.lower()}:{self._my_version}:{self._box_name}:{INSTANCE_ID}"
            body = {
                "cr_status": STATUS_CLAIMED,
                "cr_claimed_by": claimed_by,
            }
            self.dv.patch(url, data=body, etag=etag, timeout=REQUEST_TIMEOUT)
            return True
        except ETagConflictError:
            return False
        except (DataverseError, DataverseRetryExhausted) as e:
            _log(f"[ERROR] claim_message: {e}")
            return False

    def mark_processed(self, row_id: str):
        try:
            url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}({row_id})"
            self.dv.patch(
                url, data={"cr_status": STATUS_PROCESSED},
                timeout=REQUEST_TIMEOUT,
            )
            _log(f"[DV] Marked {row_id[:8]} as Processed")
        except (DataverseError, DataverseRetryExhausted) as e:
            _log(f"[WARN] mark_processed failed: {e}")

    def send_response(self, in_reply_to: str, mcs_conversation_id: str,
                      user_email: str, text: str, followup_expected: bool = False,
                      session_id: str = "", processed_by: str = ""):
        """Write outbound response to DV with [GM:xxxx] prefix and cr_processed_by."""
        try:
            # Add message prefix: [GM:xxxx] text
            prefixed_text = text
            if session_id:
                session_short = session_id[:4]
                prefixed_text = f"[{AGENT_ROLE}:{session_short}] {text}"

            body = {
                "cr_name": prefixed_text[:100],
                "cr_useremail": user_email,
                "cr_mcs_conversation_id": mcs_conversation_id,
                "cr_message": prefixed_text,
                "cr_direction": DIRECTION_OUTBOUND,
                "cr_status": STATUS_UNCLAIMED,
                "cr_in_reply_to": in_reply_to,
                "cr_followup_expected": "true" if followup_expected else "",
            }
            if processed_by:
                body["cr_processed_by"] = processed_by
            url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
            resp = self.dv.post(url, data=body, timeout=REQUEST_TIMEOUT)
            _log(f"[DV] Wrote outbound response to {user_email} (reply_to={in_reply_to[:8]}): \"{prefixed_text[:60]}...\"")
            if resp.status_code == 204 or not resp.content:
                return True
            return resp.json()
        except (DataverseError, DataverseRetryExhausted) as e:
            _log(f"[ERROR] send_response: {e}")
            return None

    # ── Claude Code Session ──────────────────────────────────────────

    def _call_claude_code(self, user_message: str, session_id: str | None = None) -> tuple[str | None, str]:
        """Call Claude Code, optionally resuming an existing session.

        Uses --output-format json to capture the session_id from the response.

        Returns (response_text, session_id) or (None, "") on failure.
        """
        cmd = ["claude", "--print", "--output-format", "json", "--dangerously-skip-permissions",
               "--model", "haiku", "--effort", "low"]
        if self._system_prompt_file:
            cmd.extend(["--system-prompt-file", self._system_prompt_file])
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.extend(["-p", user_message])
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        try:
            # Use Popen so we can kill the process tree on timeout (subprocess.run leaves orphans on Windows)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, encoding="utf-8", errors="replace")
            try:
                stdout, stderr = proc.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                _log("[WARN] Claude Code timed out -- killing process")
                proc.kill()
                proc.communicate()  # reap
                return None, ""
            if proc.returncode != 0:
                _log(f"[WARN] Claude Code failed (rc={proc.returncode}): {stderr[:300]}")
                return None, ""
            raw = stdout.strip()
            if not raw:
                return None, ""
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return raw, ""
            if data.get("is_error"):
                _log(f"[WARN] Claude error: {data.get('result', '')[:200]}")
                return None, ""
            resp = data.get("result", "")
            new_sid = data.get("session_id", "")
            # Guard: if Claude returned raw JSON tool_calls, discard
            if resp and resp.strip().startswith('{"tool_calls"'):
                _log("[WARN] Claude returned raw tool_calls JSON - discarding")
                return None, new_sid
            return resp, new_sid
        except FileNotFoundError:
            _log("[WARN] Claude Code CLI not found")
            return None, ""
        except Exception as e:
            _log(f"[ERROR] _call_claude_code: {e}")
            return None, ""

    # ── Message Processing ───────────────────────────────────────────

    def process_message(self, msg: dict):
        """Process an orphaned message using a persistent Claude Code session.

        Uses resolve_session() to determine whether to resume or start fresh.
        Writes cr_processed_by on the outbound response row.
        """
        row_id = msg.get("cr_shraga_conversationid")
        user_email = msg.get("cr_useremail", "")
        mcs_conv_id = msg.get("cr_mcs_conversation_id", "")
        user_text = msg.get("cr_message", "").strip()

        if not user_text:
            self.mark_processed(row_id)
            return

        _log(f"[GLOBAL] Processing orphaned message from {user_email}: {user_text[:80]}...")

        # Build the prompt with context for Claude Code
        prompt = (
            f"User email: {user_email}\n"
            f"Message row ID (for reply-to): {row_id}\n"
            f"MCS conversation ID: {mcs_conv_id}\n"
            f"User message: \"{user_text}\"\n\n"
            f"Process this message according to CLAUDE.md instructions. "
            f"Respond with ONLY the text message to send back to the user. "
            f"No JSON wrapping, no markdown -- just the plain text response."
        )

        # Check if we have a current session for this conversation (within this process run)
        current_sid = self._current_sessions.get(mcs_conv_id) if mcs_conv_id else None

        response = None
        new_sid = ""

        if current_sid:
            # Within-run resume
            response, new_sid = self._call_claude_code(prompt, session_id=current_sid)
            if response is None:
                _log(f"[SESSIONS] Within-run resume failed for {current_sid[:8]}..., falling back")
                # Clear stale entry to avoid repeated failures
                if mcs_conv_id:
                    self._current_sessions.pop(mcs_conv_id, None)
                current_sid = None

        if not current_sid:
            # Use resolve_session to determine what to do
            resolved_sid, context_prefix, prev_path = resolve_session(
                self.dv,
                mcs_conv_id,
                my_version=self._my_version,
                my_role=AGENT_ROLE.lower(),
                log_fn=_log,
                dv_api=DATAVERSE_API,
                conv_table=CONVERSATIONS_TABLE,
                dir_in=DIRECTION_INBOUND,
                dir_out=DIRECTION_OUTBOUND,
                st_processed=STATUS_PROCESSED,
                request_timeout=REQUEST_TIMEOUT,
            )

            full_prompt = context_prefix + prompt if context_prefix else prompt
            if resolved_sid:
                response, new_sid = self._call_claude_code(full_prompt, session_id=resolved_sid)
                if response is None:
                    # Resume failed -- start fresh WITH context
                    _log(f"[SESSIONS] Resume of {resolved_sid[:8]} failed, starting fresh with context")
                    response, new_sid = self._call_claude_code(full_prompt, session_id=None)
            else:
                response, new_sid = self._call_claude_code(full_prompt, session_id=None)

        if not response:
            response = FALLBACK_MESSAGE

        # Track the session ID for within-run resume
        if new_sid and mcs_conv_id:
            self._current_sessions[mcs_conv_id] = new_sid
            _log(f"[SESSIONS] Session {new_sid[:8]}... active for {mcs_conv_id[:20]}...")

        # Build processed_by value for the outbound row
        active_sid = new_sid or current_sid or ""
        processed_by = f"{AGENT_ROLE.lower()}:{self._my_version}:{active_sid}" if active_sid else ""

        _log(f"[PROCESS] Finished processing {row_id[:8]}. Sending response ({len(response)} chars)...")
        self.send_response(
            in_reply_to=row_id,
            mcs_conversation_id=mcs_conv_id,
            user_email=user_email,
            text=response,
            session_id=active_sid,
            processed_by=processed_by,
        )
        self.mark_processed(row_id)
        _log(f"[PROCESS] Done with {row_id[:8]}")

    # ── Main Loop ─────────────────────────────────────────────────────

    def run(self):
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        _log(f"[START] Global Manager (thin wrapper) | version={self._my_version} | instance={INSTANCE_ID} | pid={os.getpid()}")
        _log(f"[CONFIG] Dataverse: {DATAVERSE_URL}")
        _log(f"[CONFIG] Users table: {USERS_TABLE}")
        _log(f"[CONFIG] Claim delay: new users={CLAIM_DELAY_NEW_USER}s, known users={CLAIM_DELAY_KNOWN_USER}s")
        _log(f"[CONFIG] Poll interval: {POLL_INTERVAL}s")
        _log(f"[CONFIG] Box: {self._box_name}")

        while True:
            try:
                messages = self.poll_stale_unclaimed()
                if messages:
                    _log(f"[POLL] Found {len(messages)} unclaimed message(s)")
                for msg in messages:
                    row_id = msg.get("cr_shraga_conversationid", "?")[:8]
                    user_email = msg.get("cr_useremail", "?")
                    user_text = (msg.get("cr_message", "") or "")[:50]
                    _log(f"[CLAIM] Attempting to claim {row_id} from {user_email}: \"{user_text}\"")
                    if self.claim_message(msg):
                        _log(f"[CLAIM] Claimed {row_id} successfully")
                        try:
                            self.process_message(msg)
                        except Exception as e:
                            row_id = msg.get("cr_shraga_conversationid", "?")
                            _log(f"[ERROR] Processing message {row_id}: {e}")
                            try:
                                self.send_response(
                                    in_reply_to=row_id,
                                    mcs_conversation_id=msg.get("cr_mcs_conversation_id", ""),
                                    user_email=msg.get("cr_useremail", ""),
                                    text=FALLBACK_MESSAGE,
                                )
                                self.mark_processed(row_id)
                            except Exception:
                                pass

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                _log("[STOP] Shutting down.")
                break
            except Exception as e:
                _log(f"[ERROR] Main loop: {e}")
                time.sleep(POLL_INTERVAL * 2)


def main():
    manager = GlobalManager()
    manager.run()


if __name__ == "__main__":
    main()
