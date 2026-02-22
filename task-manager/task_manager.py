"""PM thin wrapper: DV I/O + session persistence + stale detection.
Claude Code handles all task management autonomously via CLAUDE.md."""
import requests, json, time, os, sys, subprocess, uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from azure.identity import DefaultAzureCredential
sys.path.insert(0, str(Path(__file__).parent.parent))
from auto_update import AutoUpdater

os.environ.setdefault('PYTHONUNBUFFERED', '1')

INSTANCE_ID = uuid.uuid4().hex[:8]
DV_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DV_API = f"{DV_URL}/api/data/v9.2"
CONV_TBL = os.environ.get("CONVERSATIONS_TABLE", "cr_shraga_conversations")
TASKS_TBL = os.environ.get("TASKS_TABLE", "cr_shraga_tasks")
USER_EMAIL = os.environ.get("USER_EMAIL")
POLL_SEC = int(os.environ.get("POLL_INTERVAL", "1"))
REQ_TMO = 30
CHAT_MODEL = os.environ.get("CHAT_MODEL", "")
DIR_IN, DIR_OUT = "Inbound", "Outbound"
ST_UNCLAIMED, ST_CLAIMED, ST_PROCESSED, ST_EXPIRED = "Unclaimed", "Claimed", "Processed", "Expired"
TASK_RUNNING, TASK_FAILED = 5, 8  # Integer picklist values for OData filters
FALLBACK_MESSAGE = "The system is temporarily unavailable, please try again shortly."
WORKING_DIR = os.environ.get("WORKING_DIR", "")
SESSIONS_FILE = os.environ.get("SESSIONS_FILE", "")


class TaskManager:
    def __init__(self, user_email: str, working_dir: str = ""):
        if not user_email:
            raise ValueError("USER_EMAIL is required")
        self.user_email = user_email
        self.manager_id = f"personal:{user_email}"
        self.working_dir = working_dir or WORKING_DIR
        self.credential = DefaultAzureCredential()
        self._token_cache = self._token_expires = None
        self._sessions_path = self._resolve_sessions_path()
        self._sessions: dict[str, str] = self._load_sessions()
        # System prompt file path (passed via --system-prompt-file)
        prompt_file = Path(__file__).parent / "PM_SYSTEM_PROMPT.md"
        self._system_prompt_file = str(prompt_file) if prompt_file.exists() else ""
        # Auto-update via release branches
        self.updater = AutoUpdater(Path(__file__).parent.parent, check_interval_minutes=10)

    def _resolve_sessions_path(self) -> Path:
        if SESSIONS_FILE: return Path(SESSIONS_FILE)
        d = Path.home() / ".shraga"; d.mkdir(exist_ok=True)
        return d / f"sessions_{self.user_email.replace('@','_at_').replace('.','_')}.json"

    def _load_sessions(self) -> dict:
        """Load sessions file. Each entry: { mcs_id: { session_id, prev_session_id } }
        On startup, we keep the file for reference but mark all sessions as needing refresh.
        """
        try:
            if self._sessions_path.exists():
                d = json.loads(self._sessions_path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    # On startup: keep prev session IDs for reference, clear current
                    prev = {}
                    for mcs_id, val in d.items():
                        sid = val if isinstance(val, str) else val.get("session_id", "")
                        if sid:
                            prev[mcs_id] = {"prev_session_id": sid, "session_id": None}
                    print(f"[SESSIONS] Loaded {len(prev)} previous session(s)")
                    return prev
        except Exception as e: print(f"[WARN] Failed to load sessions: {e}")
        return {}

    def _save_sessions(self):
        try:
            # Save in the new format: { mcs_id: { session_id, prev_session_id } }
            self._sessions_path.write_text(json.dumps(self._sessions, indent=2), encoding="utf-8")
        except Exception as e: print(f"[WARN] Failed to save sessions: {e}")

    def _get_recent_messages(self, mcs_conversation_id: str, count: int = 10) -> str:
        """Fetch recent messages from DV for this conversation to provide context."""
        hdr = self._headers()
        if not hdr: return ""
        try:
            r = requests.get(
                f"{DV_API}/{CONV_TBL}?$filter=cr_mcs_conversation_id eq '{mcs_conversation_id}'"
                f" and cr_status eq '{ST_PROCESSED}'"
                f"&$orderby=createdon desc&$top={count}",
                headers=hdr, timeout=REQ_TMO)
            r.raise_for_status()
            rows = r.json().get("value", [])
            if not rows: return ""
            lines = []
            for row in reversed(rows):  # oldest first
                direction = "User" if row.get("cr_direction") == DIR_IN else "Assistant"
                msg = row.get("cr_message", "")[:500]
                lines.append(f"{direction}: {msg}")
            return "\n".join(lines)
        except Exception as e:
            print(f"[WARN] Could not fetch recent messages: {e}")
            return ""

    def _set_onboarding_completed(self):
        """Set onboardingstep=completed for this user in DV on startup."""
        try:
            script = Path(__file__).parent.parent / "scripts" / "update_user_state.py"
            if not script.exists():
                print(f"[WARN] update_user_state.py not found at {script}")
                return
            result = subprocess.run(
                [sys.executable, str(script), "--email", self.user_email,
                 "--field", "crb3b_onboardingstep=completed"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                print(f"[ONBOARD] Set onboardingstep=completed for {self.user_email}")
            else:
                print(f"[WARN] Failed to set onboarding: {result.stderr[:200]}")
        except Exception as e:
            print(f"[WARN] Could not set onboarding state: {e}")

    def _forget_session(self, mcs_id: str):
        if mcs_id in self._sessions:
            entry = self._sessions.pop(mcs_id)
            sid = entry.get("session_id", "") if isinstance(entry, dict) else str(entry)
            print(f"[SESSIONS] Forgot session {sid[:8]}... for {mcs_id[:20]}...")
            self._save_sessions()

    def get_token(self) -> str | None:
        try:
            if self._token_cache and self._token_expires and datetime.now(timezone.utc) < self._token_expires:
                return self._token_cache
            t = self.credential.get_token(f"{DV_URL}/.default")
            self._token_cache = t.token
            self._token_expires = datetime.fromtimestamp(t.expires_on, tz=timezone.utc) - timedelta(minutes=5)
            return self._token_cache
        except Exception as e: print(f"[ERROR] Getting token: {e}"); return None

    def _headers(self, content_type=None, etag=None):
        tok = self.get_token()
        if not tok: return None
        h = {"Authorization": f"Bearer {tok}", "Accept": "application/json",
             "OData-MaxVersion": "4.0", "OData-Version": "4.0"}
        if content_type: h["Content-Type"] = content_type
        if etag: h["If-Match"] = etag
        return h

    def poll_unclaimed(self) -> list[dict]:
        hdr = self._headers()
        if not hdr: return []
        try:
            r = requests.get(f"{DV_API}/{CONV_TBL}?$filter=cr_useremail eq '{self.user_email}'"
                f" and cr_direction eq '{DIR_IN}' and cr_status eq '{ST_UNCLAIMED}'"
                f"&$orderby=createdon asc&$top=10", headers=hdr, timeout=REQ_TMO)
            r.raise_for_status(); m = r.json().get("value", [])
            if m: print(f"[POLL] Found {len(m)} unclaimed message(s) for {self.user_email}")
            return m
        except requests.exceptions.Timeout: print("[WARN] poll_unclaimed timed out"); return []
        except Exception as e: print(f"[ERROR] poll_unclaimed: {e}"); return []

    def claim_message(self, msg: dict) -> bool:
        rid, etag = msg.get("cr_shraga_conversationid"), msg.get("@odata.etag")
        if not rid or not etag: print("[WARN] Cannot claim message -- missing id or etag"); return False
        hdr = self._headers(content_type="application/json", etag=etag)
        if not hdr: return False
        try:
            r = requests.patch(f"{DV_API}/{CONV_TBL}({rid})", headers=hdr, timeout=REQ_TMO,
                json={"cr_status": ST_CLAIMED, "cr_claimed_by": f"{self.manager_id}:{INSTANCE_ID}"})
            if r.status_code == 412: print(f"[INFO] Message {rid} already claimed"); return False
            r.raise_for_status(); print(f"[CLAIM] Claimed {rid[:8]} successfully"); return True
        except requests.exceptions.Timeout: print(f"[WARN] claim_message timed out"); return False
        except Exception as e: print(f"[ERROR] claim_message: {e}"); return False

    def mark_processed(self, row_id: str):
        hdr = self._headers(content_type="application/json")
        if not hdr: return
        try:
            requests.patch(f"{DV_API}/{CONV_TBL}({row_id})", headers=hdr,
                json={"cr_status": ST_PROCESSED}, timeout=REQ_TMO)
            print(f"[DV] Marked {row_id[:8]} as Processed")
        except Exception as e: print(f"[WARN] mark_processed failed: {e}")

    def send_response(self, in_reply_to: str, mcs_conversation_id: str, text: str,
                      followup_expected: bool = False):
        hdr = self._headers(content_type="application/json")
        if not hdr: print("[ERROR] Cannot send response -- no auth token"); return None
        try:
            body = {"cr_name": text[:100], "cr_useremail": self.user_email,
                    "cr_mcs_conversation_id": mcs_conversation_id, "cr_message": text,
                    "cr_direction": DIR_OUT, "cr_status": ST_UNCLAIMED,
                    "cr_in_reply_to": in_reply_to,
                    "cr_followup_expected": "true" if followup_expected else ""}
            r = requests.post(f"{DV_API}/{CONV_TBL}", headers=hdr, json=body, timeout=REQ_TMO)
            r.raise_for_status()
            print(f'[DV] Wrote outbound response (reply_to={in_reply_to[:8]}): "{text[:60]}..."')
            return {"cr_shraga_conversationid": "created"} if r.status_code == 204 else r.json()
        except Exception as e: print(f"[ERROR] send_response: {e}"); return None

    def _dv_batch_patch(self, table, filter_q, patch_body, label, top=50):
        """Query rows matching filter_q, PATCH each with patch_body. Returns count patched."""
        hdr = self._headers()
        if not hdr: return 0
        try:
            r = requests.get(f"{DV_API}/{table}?$filter={filter_q}&$top={top}",
                             headers=hdr, timeout=REQ_TMO)
            r.raise_for_status(); rows = r.json().get("value", [])
        except Exception as e: print(f"[{label}] Error querying: {e}"); return 0
        if not rows: return 0
        pk = "cr_shraga_conversationid" if table == CONV_TBL else "cr_shraga_taskid"
        count = 0
        for row in rows:
            rid = row.get(pk)
            if not rid: continue
            try:
                ph = self._headers(content_type="application/json")
                if not ph: continue
                requests.patch(f"{DV_API}/{table}({rid})", headers=ph,
                               json=patch_body, timeout=REQ_TMO).raise_for_status()
                count += 1
                name = row.get("cr_name", rid[:8])
                print(f"[{label}] Patched '{name}' ({rid[:8]}...)")
            except Exception as e: print(f"[{label}] Error patching {rid}: {e}")
        if count: print(f"[{label}] Patched {count} row(s)")
        return count

    def cleanup_stale_outbound(self, max_age_minutes: int = 10):
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._dv_batch_patch(CONV_TBL,
            f"cr_useremail eq '{self.user_email}' and cr_direction eq '{DIR_OUT}'"
            f" and cr_status eq '{ST_UNCLAIMED}' and createdon lt {cutoff}",
            {"cr_status": ST_EXPIRED}, "CLEANUP")

    def sweep_stale_tasks(self, stale_minutes: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._dv_batch_patch(TASKS_TBL,
            f"crb3b_useremail eq '{self.user_email}' and cr_status eq {TASK_RUNNING}"
            f" and modifiedon lt {cutoff}",
            {"cr_status": TASK_FAILED, "cr_result":
             "Task failed: no progress detected for 30+ minutes (likely worker crash or restart)"},
            "SWEEP")

    def _call_claude(self, user_text: str, session_id: str | None = None) -> tuple[str | None, str]:
        cmd = ["claude", "--print", "--output-format", "json", "--dangerously-skip-permissions",
               "--model", CHAT_MODEL or "sonnet", "--effort", "low"]
        if self._system_prompt_file: cmd.extend(["--system-prompt-file", self._system_prompt_file])
        if session_id: cmd.extend(["--resume", session_id])
        cmd.extend(["-p", user_text])
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        cwd = self.working_dir if self.working_dir and os.path.isdir(self.working_dir) else None
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                             env=env, cwd=cwd, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            print(f"[WARN] Claude CLI failed (rc={res.returncode}): {res.stderr[:300]}"); return None, ""
        raw = res.stdout.strip()
        if not raw: return None, ""
        try: data = json.loads(raw)
        except json.JSONDecodeError: return raw, ""
        if data.get("is_error"):
            print(f"[WARN] Claude error: {data.get('result','')[:200]}"); return None, ""
        result = data.get("result", "")
        # Guard: if Claude output raw JSON tool_calls, it's a malformed response - retry without session
        if result and result.strip().startswith('{"tool_calls"'):
            print(f"[WARN] Claude returned raw tool_calls JSON instead of natural language - discarding")
            return None, data.get("session_id", "")
        return result, data.get("session_id", "")

    def process_message(self, msg: dict):
        rid = msg.get("cr_shraga_conversationid")
        mcs = msg.get("cr_mcs_conversation_id", "")
        txt = msg.get("cr_message", "").strip()
        if not txt: self.mark_processed(rid); return
        print(f"[MSG] Processing: {txt[:80]}...")

        session_entry = self._sessions.get(mcs, {}) if mcs else {}
        if isinstance(session_entry, str):
            session_entry = {"prev_session_id": session_entry, "session_id": None}
            self._sessions[mcs] = session_entry

        sid = session_entry.get("session_id")  # Current session (within this run)
        prev_sid = session_entry.get("prev_session_id")  # Previous run's session

        try:
            if sid:
                # Within-run resume -- same model, same prompt
                resp, new_sid = self._call_claude(txt, session_id=sid)
                if resp is None:
                    print(f"[SESSIONS] Resume failed for {sid[:8]}..., starting fresh")
                    sid = None  # Fall through to new session below

            if not sid:
                # New session -- inject context from previous conversation
                context_prefix = ""
                parts = []
                if prev_sid:
                    parts.append(f"[Previous Claude session ID: {prev_sid}]")
                if mcs:
                    recent = self._get_recent_messages(mcs)
                    if recent:
                        parts.append(f"[Recent conversation history:\n{recent}\n]")
                if parts:
                    context_prefix = "\n".join(parts) + "\n\n"

                full_text = context_prefix + txt if context_prefix else txt
                resp, new_sid = self._call_claude(full_text, session_id=None)

            if resp is None: resp = FALLBACK_MESSAGE
            if new_sid and mcs:
                self._sessions[mcs] = {
                    "session_id": new_sid,
                    "prev_session_id": prev_sid
                }
                self._save_sessions()
                if not sid: print(f"[SESSIONS] New session {new_sid[:8]}... (chain depth: {len(chain)}) for {mcs[:20]}...")

        except subprocess.TimeoutExpired: print("[WARN] Claude CLI timed out"); resp = FALLBACK_MESSAGE
        except FileNotFoundError: print("[WARN] Claude CLI not found"); resp = FALLBACK_MESSAGE
        except Exception as e: print(f"[ERROR] process_message: {e}"); resp = FALLBACK_MESSAGE
        self.send_response(in_reply_to=rid, mcs_conversation_id=mcs, text=resp)
        self.mark_processed(rid)
        print(f"[MSG] Responded: {resp[:80]}...")

    def run(self):
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        print(f"[START] PM for {self.user_email} | instance={INSTANCE_ID} | pid={os.getpid()}")
        print(f"[CONFIG] DV: {DV_URL} | Poll: {POLL_SEC}s")
        self._set_onboarding_completed()
        self.cleanup_stale_outbound()
        last_cleanup = time.time()
        last_sweep = 0  # Sweep on first iteration, then every 5 minutes
        while True:
            try:
                for m in self.poll_unclaimed():
                    if self.claim_message(m):
                        try: self.process_message(m)
                        except Exception as e:
                            rid = m.get("cr_shraga_conversationid", "?")
                            print(f"[ERROR] Processing {rid}: {e}")
                            try: self.send_response(rid, m.get("cr_mcs_conversation_id",""), FALLBACK_MESSAGE); self.mark_processed(rid)
                            except Exception: pass
                if time.time() - last_sweep > 300: self.sweep_stale_tasks(); last_sweep = time.time()
                if time.time() - last_cleanup > 1800: self.cleanup_stale_outbound(); last_cleanup = time.time()
                # Check for release branch updates (every 10 min)
                if self.updater.should_check(): self.updater.check_and_update()
                time.sleep(POLL_SEC)
            except KeyboardInterrupt: print("\n[STOP] Shutting down."); break
            except Exception as e: print(f"[ERROR] Main loop: {e}"); time.sleep(POLL_SEC * 2)
def main():
    if not USER_EMAIL: print("ERROR: USER_EMAIL required."); sys.exit(1)
    TaskManager(USER_EMAIL, working_dir=WORKING_DIR).run()
if __name__ == "__main__": main()
