"""PS thin wrapper: DV I/O + session resolution via Dataverse + stale detection.
Claude Code handles all task management autonomously via CLAUDE.md.

Session continuity is managed by session_utils.resolve_session(), which uses
Dataverse (cr_processed_by column) as the single source of truth. No local
session JSON files."""
import socket, json, time, os, sys, subprocess, uuid, traceback
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(Path(__file__).parent.parent))
from version_check import get_my_version, should_exit
from dv_client import DataverseClient, DataverseError, DataverseRetryExhausted, ETagConflictError, create_credential
from session_utils import resolve_session, sanitize_odata

os.environ.setdefault('PYTHONUNBUFFERED', '1')
os.environ.setdefault('DEVBOX_HOSTNAME', os.environ.get('COMPUTERNAME', socket.gethostname()))

INSTANCE_ID = uuid.uuid4().hex[:8]
AGENT_ROLE = "PS"

# --- File logging ---
_LOG_FILE = Path(__file__).parent / "pm.log"

_file_logger = logging.getLogger("shraga_pm")
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

DV_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DV_API = f"{DV_URL}/api/data/v9.2"
CONV_TBL = os.environ.get("CONVERSATIONS_TABLE", "cr_shraga_conversations")
USER_EMAIL = os.environ.get("USER_EMAIL")
POLL_SEC = int(os.environ.get("POLL_INTERVAL", "1"))
REQ_TMO = 30
CHAT_MODEL = os.environ.get("CHAT_MODEL", "")
DIR_IN, DIR_OUT = "Inbound", "Outbound"
ST_UNCLAIMED, ST_CLAIMED, ST_PROCESSED, ST_EXPIRED = "Unclaimed", "Claimed", "Processed", "Expired"
FALLBACK_MESSAGE = "The system is temporarily unavailable, please try again shortly."
WORKING_DIR = os.environ.get("WORKING_DIR", "")


class TaskManager:
    def __init__(self, user_email: str, working_dir: str = ""):
        if not user_email:
            raise ValueError("USER_EMAIL is required")
        self.user_email = user_email
        self._safe_email = sanitize_odata(user_email)  # OData-safe for $filter queries
        self.working_dir = working_dir or WORKING_DIR
        self.credential = create_credential(log_fn=_log)
        self.dv = DataverseClient(dataverse_url=DV_URL, credential=self.credential, log_fn=_log)
        # System prompt file path (passed via --system-prompt-file)
        prompt_file = Path(__file__).parent / "PS_SYSTEM_PROMPT.md"
        self._system_prompt_file = str(prompt_file) if prompt_file.exists() else ""
        # Version check for immutable releases
        self._my_version = get_my_version(__file__)
        # Box name for cr_claimed_by
        self._box_name = os.environ.get("COMPUTERNAME", socket.gethostname())
        # Last session ID from the most recent call (for processed_by)
        self._last_session_id: str = ""

    def _set_onboarding_completed(self):
        """Set onboardingstep=completed for this user in DV on startup."""
        try:
            script = Path(__file__).parent.parent / "scripts" / "update_user_state.py"
            if not script.exists():
                _log(f"[WARN] update_user_state.py not found at {script}")
                return
            result = subprocess.run(
                [sys.executable, str(script), "--email", self.user_email,
                 "--field", "crb3b_onboardingstep=completed"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                _log(f"[ONBOARD] Set onboardingstep=completed for {self.user_email}")
            else:
                _log(f"[WARN] Failed to set onboarding: {result.stderr[:200]}")
        except Exception as e:
            _log(f"[WARN] Could not set onboarding state: {e}")

    def poll_unclaimed(self) -> list[dict]:
        try:
            r = self.dv.get(f"{DV_API}/{CONV_TBL}?$filter=cr_useremail eq '{self._safe_email}'"
                f" and cr_direction eq '{DIR_IN}' and cr_status eq '{ST_UNCLAIMED}'"
                f"&$orderby=createdon asc&$top=1", timeout=REQ_TMO)
            m = r.json().get("value", [])
            if m: _log(f"[POLL] Found {len(m)} unclaimed message(s) for {self.user_email}")
            return m
        except Exception as e: _log(f"[ERROR] poll_unclaimed: {e}"); _log_to_file(f"[ERROR] poll_unclaimed traceback:\n{traceback.format_exc()}"); return []

    def claim_message(self, msg: dict) -> bool:
        rid, etag = msg.get("cr_shraga_conversationid"), msg.get("@odata.etag")
        if not rid or not etag: _log("[WARN] Cannot claim message -- missing id or etag"); return False
        try:
            claimed_by = f"{AGENT_ROLE.lower()}:{self._my_version}:{self._box_name}:{INSTANCE_ID}"
            self.dv.patch(f"{DV_API}/{CONV_TBL}({rid})",
                data={"cr_status": ST_CLAIMED, "cr_claimed_by": claimed_by},
                etag=etag, timeout=REQ_TMO)
            _log(f"[CLAIM] Claimed {rid[:8]} successfully (by {claimed_by})"); return True
        except ETagConflictError: _log(f"[INFO] Message {rid} already claimed"); return False
        except Exception as e: _log(f"[ERROR] claim_message: {e}"); _log_to_file(f"[ERROR] claim_message traceback:\n{traceback.format_exc()}"); return False

    def mark_processed(self, row_id: str):
        try:
            self.dv.patch(f"{DV_API}/{CONV_TBL}({row_id})",
                data={"cr_status": ST_PROCESSED}, timeout=REQ_TMO)
            _log(f"[DV] Marked {row_id[:8]} as Processed")
        except Exception as e: _log(f"[WARN] mark_processed failed: {e}")

    def send_response(self, in_reply_to: str, mcs_conversation_id: str, text: str,
                      session_id: str = "", processed_by: str = ""):
        """Write outbound response to DV. Adds [ROLE:session_short] prefix and
        writes cr_processed_by on the outbound row."""
        try:
            # Add message prefix: [PS:xxxx] text
            prefixed_text = text
            if session_id:
                session_short = session_id[:4]
                prefixed_text = f"[{AGENT_ROLE}:{session_short}] {text}"

            body = {"cr_name": prefixed_text[:100], "cr_useremail": self.user_email,
                    "cr_mcs_conversation_id": mcs_conversation_id, "cr_message": prefixed_text,
                    "cr_direction": DIR_OUT, "cr_status": ST_UNCLAIMED,
                    "cr_in_reply_to": in_reply_to}
            if processed_by:
                body["cr_processed_by"] = processed_by
            r = self.dv.post(f"{DV_API}/{CONV_TBL}", data=body, timeout=REQ_TMO)
            _log(f'[DV] Wrote outbound response (reply_to={in_reply_to[:8]}): "{prefixed_text[:60]}..."')
            return {"cr_shraga_conversationid": "created"} if r.status_code == 204 else r.json()
        except Exception as e: _log(f"[ERROR] send_response: {e}"); _log_to_file(f"[ERROR] send_response traceback:\n{traceback.format_exc()}"); return None

    def _dv_batch_patch(self, table, filter_q, patch_body, label, top=50):
        """Query rows matching filter_q, PATCH each with patch_body. Returns count patched."""
        try:
            r = self.dv.get(f"{DV_API}/{table}?$filter={filter_q}&$top={top}",
                            timeout=REQ_TMO)
            rows = r.json().get("value", [])
        except Exception as e: _log(f"[{label}] Error querying: {e}"); return 0
        if not rows: return 0
        pk = "cr_shraga_conversationid" if table == CONV_TBL else "cr_shraga_taskid"
        count = 0
        for row in rows:
            rid = row.get(pk)
            if not rid: continue
            try:
                self.dv.patch(f"{DV_API}/{table}({rid})",
                              data=patch_body, timeout=REQ_TMO)
                count += 1
                name = row.get("cr_name", rid[:8])
                _log(f"[{label}] Patched '{name}' ({rid[:8]}...)")
            except Exception as e: _log(f"[{label}] Error patching {rid}: {e}")
        if count: _log(f"[{label}] Patched {count} row(s)")
        return count

    def cleanup_stale_outbound(self, max_age_minutes: int = 10):
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._dv_batch_patch(CONV_TBL,
            f"cr_useremail eq '{self._safe_email}' and cr_direction eq '{DIR_OUT}'"
            f" and cr_status eq '{ST_UNCLAIMED}' and createdon lt {cutoff}",
            {"cr_status": ST_EXPIRED}, "CLEANUP")

    def cleanup_stale_claimed(self, max_age_minutes: int = 15):
        """Mark stale Claimed inbound messages as Expired (drop them).

        Runs on PS startup.  Messages stuck in Claimed longer than
        max_age_minutes are from a previous crash — too old to respond to.
        Uses Expired status (same as cleanup_stale_outbound) so they're
        clearly distinguished from successfully processed messages.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._dv_batch_patch(CONV_TBL,
            f"cr_useremail eq '{self._safe_email}' and cr_direction eq '{DIR_IN}'"
            f" and cr_status eq 'Claimed' and createdon lt {cutoff}",
            {"cr_status": ST_EXPIRED}, "STALE_CLAIMED")

    def _call_claude(self, user_text: str, session_id: str | None = None) -> tuple[str | None, str]:
        cmd = ["claude", "--print", "--output-format", "json", "--dangerously-skip-permissions",
               "--model", CHAT_MODEL or "sonnet", "--effort", "low"]
        if self._system_prompt_file: cmd.extend(["--system-prompt-file", self._system_prompt_file])
        if session_id: cmd.extend(["--resume", session_id])
        cmd.extend(["-p", user_text])
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        cwd = self.working_dir if self.working_dir and os.path.isdir(self.working_dir) else None
        # Use Popen so we can kill the process tree on timeout (subprocess.run leaves orphans on Windows)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, cwd=cwd, encoding="utf-8", errors="replace")
        try:
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            _log("[WARN] Claude CLI timed out -- killing process")
            proc.kill()
            proc.communicate()  # reap
            return None, ""
        if proc.returncode != 0:
            _log(f"[WARN] Claude CLI failed (rc={proc.returncode}): {stderr[:300]}"); return None, ""
        raw = stdout.strip()
        if not raw: return None, ""
        try: data = json.loads(raw)
        except json.JSONDecodeError: return raw, ""
        if data.get("is_error"):
            _log(f"[WARN] Claude error: {data.get('result','')[:200]}"); return None, ""
        result = data.get("result", "")
        # Guard: if Claude output raw JSON tool_calls, it's a malformed response - discard
        if result and result.strip().startswith('{"tool_calls"'):
            _log(f"[WARN] Claude returned raw tool_calls JSON instead of natural language - discarding")
            return None, data.get("session_id", "")
        return result, data.get("session_id", "")

    def process_message(self, msg: dict):
        rid = msg.get("cr_shraga_conversationid")
        mcs = msg.get("cr_mcs_conversation_id", "")
        txt = msg.get("cr_message", "").strip()
        if not txt: self.mark_processed(rid); return
        _log(f"[PS] Processing: {txt[:80]}...")
        # Inject IDs so Claude can pass them to create_task.py
        if mcs:
            txt = f"[MCS_CONVERSATION_ID={mcs}]\n[INBOUND_ROW_ID={rid}]\n{txt}"

        try:
            resp = None
            new_sid = ""

            # Always call resolve_session to get correct cross-agent context
            resolved_sid, context_prefix, prev_path = resolve_session(
                self.dv,
                mcs,
                my_version=self._my_version,
                my_role=AGENT_ROLE.lower(),
                log_fn=_log,
                dv_api=DV_API,
                conv_table=CONV_TBL,
                dir_in=DIR_IN,
                dir_out=DIR_OUT,
                st_processed=ST_PROCESSED,
                request_timeout=REQ_TMO,
            )

            full_text = context_prefix + txt if context_prefix else txt
            if resolved_sid:
                # Resume a previous session from DV history
                resp, new_sid = self._call_claude(full_text, session_id=resolved_sid)
                if resp is None:
                    # Resume failed -- start fresh WITH context (not without)
                    _log(f"[SESSIONS] Resume of {resolved_sid[:8]} failed, starting fresh with context")
                    resp, new_sid = self._call_claude(full_text, session_id=None)
            else:
                # New session with optional context
                resp, new_sid = self._call_claude(full_text, session_id=None)

            if resp is None: resp = FALLBACK_MESSAGE

            # Track session for processed_by
            active_sid = new_sid or ""
            self._last_session_id = active_sid
            processed_by = f"{AGENT_ROLE.lower()}:{self._my_version}:{active_sid}" if active_sid else ""

        except Exception as e: _log(f"[ERROR] process_message: {e}"); _log_to_file(f"[ERROR] process_message traceback:\n{traceback.format_exc()}"); resp = FALLBACK_MESSAGE; processed_by = ""; active_sid = ""

        self.send_response(
            in_reply_to=rid,
            mcs_conversation_id=mcs,
            text=resp,
            session_id=active_sid,
            processed_by=processed_by,
        )
        self.mark_processed(rid)
        _log(f"[PS] Responded: {resp[:80]}...")

    def run(self):
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        # Fail fast if credentials are broken
        try:
            self.dv.get_token()
        except TimeoutError:
            _log("[CRITICAL] get_token() timed out after 30s -- Azure credential hung. Exiting.")
            _log("[CRITICAL] HINT: Run 'az login' on this dev box.")
            sys.exit(1)
        except Exception as e:
            _log(f"[CRITICAL] Getting token failed: {e} -- Exiting.")
            _log("[CRITICAL] HINT: Run 'az login' on this dev box.")
            sys.exit(1)
        _log(f"[START] PS for {self.user_email} | version={self._my_version} | instance={INSTANCE_ID} | pid={os.getpid()}")
        _log(f"[CONFIG] DV: {DV_URL} | Poll: {POLL_SEC}s | Box: {self._box_name}")
        self._set_onboarding_completed()
        self.cleanup_stale_claimed()
        self.cleanup_stale_outbound()
        last_cleanup = time.time()
        _last_heartbeat = 0.0
        _start_time = time.time()
        while True:
            try:
                # Idle heartbeat logging every 60 seconds
                _now_hb = time.time()
                if _now_hb - _last_heartbeat > 60:
                    _uptime = int(_now_hb - _start_time)
                    _log(f"[HEARTBEAT] PS alive | version={self._my_version} | uptime={_uptime}s | user={self.user_email}")
                    _last_heartbeat = _now_hb

                for m in self.poll_unclaimed():
                    if self.claim_message(m):
                        try: self.process_message(m)
                        except Exception as e:
                            rid = m.get("cr_shraga_conversationid", "?")
                            _log(f"[ERROR] Processing {rid}: {e}")
                            try: self.send_response(rid, m.get("cr_mcs_conversation_id",""), FALLBACK_MESSAGE); self.mark_processed(rid)
                            except Exception: pass
                if time.time() - last_cleanup > 1800: self.cleanup_stale_outbound(); last_cleanup = time.time()
                # Check if a new release is available
                if should_exit(self._my_version):
                    _log(f"[UPDATE] New release detected. Exiting to restart with new version.")
                    sys.exit(0)
                time.sleep(POLL_SEC)
            except KeyboardInterrupt: _log("\n[STOP] Shutting down."); break
            except Exception as e: _log(f"[ERROR] Main loop: {e}"); _log_to_file(f"[ERROR] PS main loop traceback:\n{traceback.format_exc()}"); time.sleep(POLL_SEC * 2)

def main():
    if not USER_EMAIL: _log("[CRITICAL] USER_EMAIL required."); sys.exit(1)
    TaskManager(USER_EMAIL, working_dir=WORKING_DIR).run()

if __name__ == "__main__": main()
