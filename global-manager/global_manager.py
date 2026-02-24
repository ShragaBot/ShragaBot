"""
Global Manager -- Thin wrapper + persistent Claude Code session architecture.

Polls the conversations table for any unclaimed inbound messages older than 15s.
Handles new user onboarding (dev box provisioning) and users whose personal
manager is down.

Instead of a tool-wrapper architecture, the GM delegates all decision-making to
a persistent Claude Code session.  Claude Code reads CLAUDE.md and runs scripts
directly (get_user_state.py, update_user_state.py, check_devbox_status.py, etc.).

The GM's responsibilities are limited to:
  1. Poll DV for unclaimed Inbound messages
  2. Claim messages with ETag-based optimistic concurrency
  3. Look up or create a Claude Code session for each conversation
  4. Run `claude --resume {session_id} --print -p "{user_message}"`
  5. Write Claude's response back to DV
  6. Clean up expired sessions (24h TTL)

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
import subprocess
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from azure.identity import DefaultAzureCredential

sys.path.insert(0, str(Path(__file__).parent.parent))
from timeout_utils import call_with_timeout
from dv_client import DataverseClient, DataverseError, DataverseRetryExhausted, ETagConflictError

os.environ.setdefault('PYTHONUNBUFFERED', '1')

# Unique instance ID for this process (helps distinguish multiple GM instances)
INSTANCE_ID = uuid.uuid4().hex[:8]

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
SESSION_EXPIRY_HOURS = int(os.environ.get("SESSION_EXPIRY_HOURS", "24"))

# Session persistence file
SESSIONS_DIR = Path(os.environ.get("SHRAGA_SESSIONS_DIR", Path.home() / ".shraga"))
SESSIONS_FILE = SESSIONS_DIR / "gm_sessions.json"

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
    """Get Azure credential via DefaultAzureCredential.

    Requires a valid ``az login`` session or managed-identity / service-principal
    environment variables.  Returns a ``DefaultAzureCredential`` instance.
    """
    cred = DefaultAzureCredential()
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


# ── Session Manager ──────────────────────────────────────────────────────

class SessionManager:
    """Manages Claude Code session persistence for conversation continuity.

    Maps {mcs_conversation_id -> session_entry} where each session_entry is:
        {
            "session_id": str,      # Claude Code session ID (UUID)
            "created_at": str,      # ISO timestamp
            "last_used": str,       # ISO timestamp
            "user_email": str,      # user this session is for
        }

    Sessions are persisted to ~/.shraga/gm_sessions.json and expire after
    SESSION_EXPIRY_HOURS (default 24h).
    """

    def __init__(self, sessions_file: Path | None = None):
        self._sessions_file = sessions_file or SESSIONS_FILE
        self._sessions: dict[str, dict] = {}
        self._load()

    def _load(self):
        """Load sessions from disk."""
        try:
            if self._sessions_file.exists():
                data = json.loads(self._sessions_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._sessions = data
                    logger.info("Loaded %d sessions from %s", len(self._sessions), self._sessions_file)
        except Exception as e:
            logger.warning("Failed to load sessions from %s: %s", self._sessions_file, e)
            self._sessions = {}

    def _save(self):
        """Persist sessions to disk."""
        try:
            self._sessions_file.parent.mkdir(parents=True, exist_ok=True)
            self._sessions_file.write_text(
                json.dumps(self._sessions, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save sessions to %s: %s", self._sessions_file, e)

    def get_session(self, conversation_id: str) -> dict | None:
        """Get session entry by conversation ID, or None if not found."""
        entry = self._sessions.get(conversation_id)
        if entry:
            entry["last_used"] = datetime.now(timezone.utc).isoformat()
            self._save()
        return entry

    def save_session(self, conversation_id: str, session_id: str, user_email: str = ""):
        """Save a real session ID returned by Claude CLI."""
        now = datetime.now(timezone.utc).isoformat()
        is_new = conversation_id not in self._sessions
        self._sessions[conversation_id] = {
            "session_id": session_id,
            "created_at": self._sessions.get(conversation_id, {}).get("created_at", now),
            "last_used": now,
            "user_email": user_email,
        }
        self._save()
        if is_new:
            _log(f"[SESSIONS] New session {session_id[:8]}... for {conversation_id[:20]}...")

    def forget(self, conversation_id: str):
        """Remove a session (e.g. after resume failure)."""
        if conversation_id in self._sessions:
            old = self._sessions.pop(conversation_id)
            self._save()
            _log(f"[SESSIONS] Forgot session {old['session_id'][:8]}... for {conversation_id[:20]}...")

    def cleanup_expired(self, max_age_hours: int | None = None):
        """Remove sessions older than max_age_hours (default SESSION_EXPIRY_HOURS).

        Returns the number of sessions removed.
        """
        max_age = max_age_hours if max_age_hours is not None else SESSION_EXPIRY_HOURS
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age)
        expired = []

        for conv_id, entry in self._sessions.items():
            last_used_str = entry.get("last_used", entry.get("created_at", ""))
            try:
                last_used = datetime.fromisoformat(last_used_str)
                if last_used.tzinfo is None:
                    last_used = last_used.replace(tzinfo=timezone.utc)
                if last_used < cutoff:
                    expired.append(conv_id)
            except (ValueError, TypeError):
                # Can't parse timestamp -- expire it
                expired.append(conv_id)

        for conv_id in expired:
            del self._sessions[conv_id]

        if expired:
            self._save()
            logger.info("Cleaned up %d expired sessions", len(expired))

        return len(expired)

    @property
    def sessions(self) -> dict[str, dict]:
        """Read-only access to the sessions dict (deep copy)."""
        return json.loads(json.dumps(self._sessions))


# ── Global Manager ───────────────────────────────────────────────────────

class GlobalManager:
    """Thin wrapper manager for orphaned messages and new user onboarding.

    Delegates all decision-making to Claude Code via persistent sessions.
    Claude Code reads CLAUDE.md and runs scripts directly.
    """

    def __init__(self, sessions_file: Path | None = None):
        self.manager_id = "global"
        self.credential = get_credential()
        self.dv = DataverseClient(
            credential=self.credential,
            dataverse_url=DATAVERSE_URL,
            log_fn=_log,
        )
        self._known_users: set[str] = set()
        self.session_manager = SessionManager(sessions_file=sessions_file)
        # System prompt file path (passed via --system-prompt-file)
        prompt_file = Path(__file__).parent / "GM_SYSTEM_PROMPT.md"
        self._system_prompt_file = str(prompt_file) if prompt_file.exists() else ""
        if self._system_prompt_file:
            _log(f"[CONFIG] System prompt: {prompt_file} ({prompt_file.stat().st_size} bytes)")
        else:
            _log(f"[WARN] No system prompt found at {prompt_file}")

    # ── User Lookup (for differential claiming delay) ─────────────────

    def _is_known_user(self, user_email: str) -> bool:
        """Check if a user exists in the DV users table (for claim delay logic)."""
        if user_email in self._known_users:
            return True
        try:
            url = (
                f"{DATAVERSE_API}/{USERS_TABLE}"
                f"?$filter=crb3b_useremail eq '{user_email}'"
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
            body = {
                "cr_status": STATUS_CLAIMED,
                "cr_claimed_by": f"{self.manager_id}:{INSTANCE_ID}",
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
                      user_email: str, text: str, followup_expected: bool = False):
        try:
            body = {
                "cr_name": text[:100],
                "cr_useremail": user_email,
                "cr_mcs_conversation_id": mcs_conversation_id,
                "cr_message": text,
                "cr_direction": DIRECTION_OUTBOUND,
                "cr_status": STATUS_UNCLAIMED,
                "cr_in_reply_to": in_reply_to,
                "cr_followup_expected": "true" if followup_expected else "",
            }
            url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
            resp = self.dv.post(url, data=body, timeout=REQUEST_TIMEOUT)
            _log(f"[DV] Wrote outbound response to {user_email} (reply_to={in_reply_to[:8]}): \"{text[:60]}...\"")
            if resp.status_code == 204 or not resp.content:
                return True
            return resp.json()
        except (DataverseError, DataverseRetryExhausted) as e:
            _log(f"[ERROR] send_response: {e}")
            return None

    # ── Claude Code Session ──────────────────────────────────────────

    def _call_claude_code(self, user_message: str, session_id: str | None = None) -> tuple[str | None, str]:
        """Call Claude Code, optionally resuming an existing session.

        On first call (session_id=None): starts a fresh session.
        On subsequent calls: resumes the session with --resume.

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

        Claude Code reads CLAUDE.md for instructions and uses scripts directly
        to query/update Dataverse, check devbox status, etc.
        """
        row_id = msg.get("cr_shraga_conversationid")
        user_email = msg.get("cr_useremail", "")
        mcs_conv_id = msg.get("cr_mcs_conversation_id", "")
        user_text = msg.get("cr_message", "").strip()

        if not user_text:
            self.mark_processed(row_id)
            return

        _log(f"[GLOBAL] Processing orphaned message from {user_email}: {user_text[:80]}...")

        # Look up existing session (None if first message in this conversation)
        existing = self.session_manager.get_session(mcs_conv_id)
        session_id = existing["session_id"] if existing else None

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

        # Let Claude Code handle everything
        response, new_sid = self._call_claude_code(prompt, session_id=session_id)

        # If resume failed, retry without session
        if response is None and session_id:
            _log(f"[SESSIONS] Resume failed for {session_id[:8]}..., starting fresh")
            self.session_manager.forget(mcs_conv_id)
            response, new_sid = self._call_claude_code(prompt, session_id=None)

        if not response:
            response = FALLBACK_MESSAGE

        # Save the real session ID returned by Claude
        if new_sid and mcs_conv_id:
            self.session_manager.save_session(mcs_conv_id, new_sid, user_email)

        _log(f"[PROCESS] Finished processing {row_id[:8]}. Sending response ({len(response)} chars)...")
        self.send_response(
            in_reply_to=row_id,
            mcs_conversation_id=mcs_conv_id,
            user_email=user_email,
            text=response,
        )
        self.mark_processed(row_id)
        _log(f"[PROCESS] Done with {row_id[:8]}")

    # ── Main Loop ─────────────────────────────────────────────────────

    def run(self):
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        _log(f"[START] Global Manager (thin wrapper) | instance={INSTANCE_ID} | pid={os.getpid()}")
        _log(f"[CONFIG] Dataverse: {DATAVERSE_URL}")
        _log(f"[CONFIG] Users table: {USERS_TABLE}")
        _log(f"[CONFIG] Claim delay: new users={CLAIM_DELAY_NEW_USER}s, known users={CLAIM_DELAY_KNOWN_USER}s")
        _log(f"[CONFIG] Poll interval: {POLL_INTERVAL}s")
        _log(f"[CONFIG] Session expiry: {SESSION_EXPIRY_HOURS}h")
        _log(f"[CONFIG] Sessions file: {SESSIONS_FILE}")

        cleanup_counter = 0

        while True:
            try:
                # Periodic session cleanup (every 100 poll cycles)
                cleanup_counter += 1
                if cleanup_counter >= 100:
                    self.session_manager.cleanup_expired()
                    cleanup_counter = 0

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
