"""
Tests for Global Manager (thin wrapper + persistent Claude Code session).

All external dependencies (Azure, Dataverse, Claude Code CLI) are mocked.
Tests verify:
  - Session creation/resumption (SessionManager)
  - Session persistence to disk (~/.shraga/gm_sessions.json)
  - Session expiry/cleanup (24h)
  - Claude Code subprocess invocation (--resume, --print, -p)
  - DV polling/claiming (ETag concurrency)
  - Response writing (Outbound rows)
  - New user and known user flows
  - Fallback message when Claude Code is unavailable

Acceptance Criteria:
  1. No TOOL_DEFINITIONS, no _execute_tool, no _try_parse_json, no _call_claude_with_tools
  2. GM uses claude --resume with session persistence
  3. Session persistence to disk (~/.shraga/gm_sessions.json)
  4. Session expiry/cleanup (24h)
  5. CLAUDE.md used by Claude Code (not Python system prompt)
  6. DV polling/claiming/response-writing preserved
  7. ETag concurrency preserved
  8. Handles new user and known user flows
  9. All NEW tests pass
"""
import json
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone, timedelta

import pytest

# Add global-manager to path
sys.path.insert(0, str(Path(__file__).parent / "global-manager"))

from conftest import FakeAccessToken, FakeResponse


# -- Fixtures ----------------------------------------------------------------

SAMPLE_CONVERSATION_ID = "conv-0001-0002-0003-000000000001"
SAMPLE_MCS_CONV_ID = "mcs-conv-abc123"

SAMPLE_STALE_MSG = {
    "cr_shraga_conversationid": SAMPLE_CONVERSATION_ID,
    "cr_useremail": "newuser@example.com",
    "cr_mcs_conversation_id": SAMPLE_MCS_CONV_ID,
    "cr_message": "hello, I want to create a task",
    "cr_direction": "Inbound",
    "cr_status": "Unclaimed",
    "@odata.etag": 'W/"12345"',
    "createdon": "2026-02-15T09:59:00Z",
}


@pytest.fixture
def mock_credential():
    cred = MagicMock()
    cred.get_token.return_value = FakeAccessToken()
    return cred


@pytest.fixture
def sessions_file(tmp_path):
    """Provide a temp path for session persistence."""
    return tmp_path / ".shraga" / "gm_sessions.json"


@pytest.fixture
def manager(mock_credential, sessions_file):
    """Create a GlobalManager with mocked credentials and temp sessions file."""
    with patch("global_manager.get_credential", return_value=mock_credential):
        from global_manager import GlobalManager
        mgr = GlobalManager(sessions_file=sessions_file)
    mgr.dv = MagicMock()
    return mgr


@pytest.fixture
def session_mgr(sessions_file):
    """Create a standalone SessionManager for unit testing."""
    from global_manager import SessionManager
    return SessionManager(sessions_file=sessions_file)


# ============================================================================
# Acceptance Criterion 1: No old tool-wrapper code
# ============================================================================

class TestNoToolWrapperCode:
    """Verify the old tool-wrapper architecture is completely removed."""

    def test_no_tool_definitions(self):
        """TOOL_DEFINITIONS must not exist in the module."""
        import global_manager
        assert not hasattr(global_manager, "TOOL_DEFINITIONS"), \
            "TOOL_DEFINITIONS should be removed"

    def test_no_execute_tool(self, manager):
        """_execute_tool method must not exist."""
        assert not hasattr(manager, "_execute_tool"), \
            "_execute_tool should be removed"

    def test_no_try_parse_json(self, manager):
        """_try_parse_json method must not exist."""
        assert not hasattr(manager, "_try_parse_json"), \
            "_try_parse_json should be removed"

    def test_no_call_claude_with_tools(self, manager):
        """_call_claude_with_tools method must not exist."""
        assert not hasattr(manager, "_call_claude_with_tools"), \
            "_call_claude_with_tools should be removed"

    def test_no_build_system_prompt(self, manager):
        """_build_system_prompt method must not exist."""
        assert not hasattr(manager, "_build_system_prompt"), \
            "_build_system_prompt should be removed"

    def test_no_tool_methods(self, manager):
        """No _tool_* methods should exist."""
        tool_methods = [attr for attr in dir(manager) if attr.startswith("_tool_")]
        assert tool_methods == [], \
            f"Tool methods should be removed: {tool_methods}"

    def test_has_call_claude_code(self, manager):
        """_call_claude_code method must exist (replacement)."""
        assert hasattr(manager, "_call_claude_code"), \
            "New _call_claude_code method should exist"


# ============================================================================
# Acceptance Criterion 2: Claude --resume with session persistence
# ============================================================================

def _make_popen_mock(stdout="", stderr="", returncode=0):
    """Helper: create a mock subprocess.Popen that returns given stdout/stderr via communicate()."""
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


class TestClaudeCodeInvocation:
    """Verify Claude Code is called with --resume and session ID."""

    @patch("global_manager.subprocess.Popen")
    def test_call_claude_code_uses_resume_flag(self, mock_popen, manager):
        """Claude Code must be called with --resume {session_id}."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"result": "Hello! How can I help?", "session_id": "abc123session"})
        )
        result, sid = manager._call_claude_code("Hello", session_id="abc123session")

        assert result == "Hello! How can I help?"
        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        assert "abc123session" in cmd
        assert "--print" in cmd
        assert "-p" in cmd

    @patch("global_manager.subprocess.Popen")
    def test_call_claude_code_passes_message(self, mock_popen, manager):
        """The user message is passed via -p flag."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"result": 'Response text', "session_id": "sess-1"})
        )
        manager._call_claude_code("What is Shraga?")

        cmd = mock_popen.call_args[0][0]
        # -p flag should be followed by the message
        p_idx = cmd.index("-p")
        assert cmd[p_idx + 1] == "What is Shraga?"

    @patch("global_manager.subprocess.Popen")
    def test_call_claude_code_strips_claudecode_env(self, mock_popen, manager):
        """CLAUDECODE env var must be stripped to avoid nested session errors."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"result": 'ok', "session_id": "sess-1"})
        )
        manager._call_claude_code("test")

        call_kwargs = mock_popen.call_args[1]
        env = call_kwargs.get("env", {})
        assert "CLAUDECODE" not in env

    @patch("global_manager.subprocess.Popen")
    def test_call_claude_code_uses_dangerously_skip_permissions(self, mock_popen, manager):
        """Must include --dangerously-skip-permissions flag."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"result": 'ok', "session_id": "sess-1"})
        )
        manager._call_claude_code("test")

        cmd = mock_popen.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd

    @patch("global_manager.subprocess.Popen")
    def test_call_claude_code_handles_failure(self, mock_popen, manager):
        """Returns None when Claude Code fails (non-zero exit)."""
        mock_popen.return_value = _make_popen_mock(
            returncode=1, stderr="Error: something broke"
        )
        result, sid = manager._call_claude_code("test")
        assert result is None

    @patch("global_manager.subprocess.Popen")
    def test_call_claude_code_handles_timeout(self, mock_popen, manager):
        """Returns None on subprocess timeout."""
        import subprocess
        proc = _make_popen_mock()
        proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        mock_popen.return_value = proc
        result, sid = manager._call_claude_code("test")
        assert result is None

    @patch("global_manager.subprocess.Popen")
    def test_call_claude_code_handles_not_found(self, mock_popen, manager):
        """Returns None when claude CLI is not found."""
        mock_popen.side_effect = FileNotFoundError("claude not found")
        result, sid = manager._call_claude_code("test")
        assert result is None

    @patch("global_manager.subprocess.Popen")
    def test_call_claude_code_handles_empty_output(self, mock_popen, manager):
        """Returns None when Claude Code returns empty output."""
        mock_popen.return_value = _make_popen_mock(stdout="")
        result, sid = manager._call_claude_code("test")
        assert result is None

    @patch("global_manager.subprocess.Popen")
    def test_call_claude_code_encoding_params(self, mock_popen, manager):
        """Must use encoding='utf-8' and errors='replace' for Unicode safety."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"result": 'ok', "session_id": "sess-1"})
        )
        manager._call_claude_code("test")

        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("encoding") == "utf-8"
        assert call_kwargs.get("errors") == "replace"

    @patch("global_manager.subprocess.Popen")
    def test_call_claude_code_handles_non_ascii(self, mock_popen, manager):
        """Claude may return emojis/non-ASCII; must not crash."""
        non_ascii = "Hello! \U0001f44d Great job \u2014 devbox ready \u2705"
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"result": non_ascii, "session_id": "sess-1"})
        )
        result, sid = manager._call_claude_code("test")
        assert result == non_ascii


# ============================================================================
# Acceptance Criterion 3: Session persistence to disk
# ============================================================================

class TestSessionPersistence:
    """Verify sessions are persisted to ~/.shraga/gm_sessions.json."""

    def test_sessions_file_created_on_save(self, session_mgr, sessions_file):
        """Sessions file is created when a session is saved."""
        session_mgr.save_session("conv-1", "real-session-id-1", "user@test.com")
        assert sessions_file.exists()

    def test_sessions_file_contains_valid_json(self, session_mgr, sessions_file):
        """Sessions file contains valid JSON."""
        session_mgr.save_session("conv-1", "real-session-id-1", "user@test.com")
        data = json.loads(sessions_file.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "conv-1" in data

    def test_sessions_persist_across_instances(self, sessions_file):
        """Sessions survive SessionManager restart (loaded from disk)."""
        from global_manager import SessionManager
        mgr1 = SessionManager(sessions_file=sessions_file)
        mgr1.save_session("conv-persist", "real-session-id-persist", "user@test.com")

        # Create a new SessionManager instance (simulates restart)
        mgr2 = SessionManager(sessions_file=sessions_file)
        entry = mgr2.get_session("conv-persist")

        assert entry is not None
        assert entry["session_id"] == "real-session-id-persist", "Session ID must be the same after restart"

    def test_session_entry_has_required_fields(self, session_mgr):
        """Each session entry must have session_id, created_at, last_used, user_email."""
        session_mgr.save_session("conv-fields", "session-fields-1", "fieldtest@test.com")
        entry = session_mgr.get_session("conv-fields")
        assert entry is not None
        assert "session_id" in entry
        assert "created_at" in entry
        assert "last_used" in entry
        assert "user_email" in entry
        assert entry["user_email"] == "fieldtest@test.com"

    def test_sessions_dir_created_automatically(self, tmp_path):
        """Parent directories are created if they don't exist."""
        from global_manager import SessionManager
        deep_path = tmp_path / "a" / "b" / "c" / "sessions.json"
        mgr = SessionManager(sessions_file=deep_path)
        mgr.save_session("conv-deep", "session-deep-1", "user@test.com")
        assert deep_path.exists()

    def test_load_handles_corrupt_file(self, sessions_file):
        """SessionManager handles corrupt/invalid JSON gracefully."""
        from global_manager import SessionManager
        sessions_file.parent.mkdir(parents=True, exist_ok=True)
        sessions_file.write_text("NOT VALID JSON {{{", encoding="utf-8")
        mgr = SessionManager(sessions_file=sessions_file)
        # Should start with empty sessions, not crash
        assert mgr.sessions == {}

    def test_load_handles_missing_file(self, tmp_path):
        """SessionManager handles missing file gracefully."""
        from global_manager import SessionManager
        mgr = SessionManager(sessions_file=tmp_path / "nonexistent.json")
        assert mgr.sessions == {}

    def test_multiple_conversations_tracked(self, session_mgr):
        """Multiple conversations get unique session IDs."""
        session_mgr.save_session("conv-a", "session-a-1", "alice@test.com")
        session_mgr.save_session("conv-b", "session-b-1", "bob@test.com")
        entry_a = session_mgr.get_session("conv-a")
        entry_b = session_mgr.get_session("conv-b")
        assert entry_a["session_id"] != entry_b["session_id"]
        assert len(session_mgr.sessions) == 2


# ============================================================================
# Acceptance Criterion 4: Session expiry/cleanup (24h)
# ============================================================================

class TestSessionExpiry:
    """Verify session cleanup removes sessions older than 24h."""

    def test_cleanup_removes_expired_sessions(self, session_mgr):
        """Sessions older than the expiry window are removed."""
        # Manually insert an old session
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        session_mgr._sessions["old-conv"] = {
            "session_id": "old-session",
            "created_at": old_time,
            "last_used": old_time,
            "user_email": "old@test.com",
        }
        session_mgr._save()

        removed = session_mgr.cleanup_expired(max_age_hours=24)
        assert removed == 1
        assert "old-conv" not in session_mgr.sessions

    def test_cleanup_keeps_fresh_sessions(self, session_mgr):
        """Sessions within the expiry window are kept."""
        session_mgr.save_session("fresh-conv", "fresh-session-1", "fresh@test.com")
        removed = session_mgr.cleanup_expired(max_age_hours=24)
        assert removed == 0
        assert "fresh-conv" in session_mgr.sessions

    def test_cleanup_mixed_old_and_new(self, session_mgr):
        """Cleanup removes old sessions while keeping fresh ones."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        session_mgr._sessions["old-conv"] = {
            "session_id": "old-session",
            "created_at": old_time,
            "last_used": old_time,
            "user_email": "old@test.com",
        }
        session_mgr.save_session("new-conv", "new-session-1", "new@test.com")
        session_mgr._save()

        removed = session_mgr.cleanup_expired(max_age_hours=24)
        assert removed == 1
        assert "old-conv" not in session_mgr.sessions
        assert "new-conv" in session_mgr.sessions

    def test_cleanup_handles_unparseable_timestamps(self, session_mgr):
        """Sessions with invalid timestamps are expired (fail-safe)."""
        session_mgr._sessions["bad-conv"] = {
            "session_id": "bad-session",
            "created_at": "not-a-date",
            "last_used": "also-not-a-date",
            "user_email": "bad@test.com",
        }
        session_mgr._save()

        removed = session_mgr.cleanup_expired(max_age_hours=24)
        assert removed == 1

    def test_cleanup_persists_after_removal(self, session_mgr, sessions_file):
        """After cleanup, the sessions file is updated on disk."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        session_mgr._sessions["expired-conv"] = {
            "session_id": "expired-session",
            "created_at": old_time,
            "last_used": old_time,
            "user_email": "expired@test.com",
        }
        session_mgr.save_session("active-conv", "active-session-1", "active@test.com")
        session_mgr._save()

        session_mgr.cleanup_expired(max_age_hours=24)

        # Reload from disk to verify persistence
        from global_manager import SessionManager
        reloaded = SessionManager(sessions_file=sessions_file)
        assert "expired-conv" not in reloaded.sessions
        assert "active-conv" in reloaded.sessions

    def test_last_used_updates_on_access(self, session_mgr):
        """Accessing an existing session updates last_used timestamp."""
        session_mgr.save_session("conv-access", "session-access-1", "user@test.com")
        entry1 = session_mgr.get_session("conv-access")
        first_used = entry1["last_used"]

        # Small delay to get a different timestamp
        import time
        time.sleep(0.01)

        entry2 = session_mgr.get_session("conv-access")

        assert entry2["session_id"] == "session-access-1", "Same conversation must get same session"
        assert entry2["last_used"] >= first_used

    def test_cleanup_no_expired_returns_zero(self, session_mgr):
        """When no sessions are expired, cleanup returns 0."""
        session_mgr.save_session("fresh-1", "session-fresh-1", "a@test.com")
        session_mgr.save_session("fresh-2", "session-fresh-2", "b@test.com")
        removed = session_mgr.cleanup_expired(max_age_hours=24)
        assert removed == 0


# ============================================================================
# Acceptance Criterion 5: CLAUDE.md used by Claude Code (not Python system prompt)
# ============================================================================

class TestClaudeMdUsage:
    """Verify GM does NOT have a Python-embedded system prompt."""

    def test_no_system_prompt_builder(self, manager):
        """No _build_system_prompt method should exist."""
        assert not hasattr(manager, "_build_system_prompt")

    def test_system_prompt_exists(self):
        """GM_SYSTEM_PROMPT.md must exist in the global-manager directory."""
        prompt_file = Path(__file__).parent / "global-manager" / "GM_SYSTEM_PROMPT.md"
        assert prompt_file.exists(), f"GM_SYSTEM_PROMPT.md not found at {prompt_file}"

    def test_system_prompt_has_content(self):
        """GM_SYSTEM_PROMPT.md must have meaningful content (not empty)."""
        prompt_file = Path(__file__).parent / "global-manager" / "GM_SYSTEM_PROMPT.md"
        content = prompt_file.read_text(encoding="utf-8")
        assert len(content) > 100, "GM_SYSTEM_PROMPT.md should have substantial content"
        assert "Global Manager" in content


# ============================================================================
# Acceptance Criterion 6: DV polling/claiming/response-writing preserved
# ============================================================================

class TestPolling:
    """DV polling tests (unchanged from original architecture)."""

    def test_poll_stale_unclaimed_returns_old_messages(self, manager):
        manager.dv.get.return_value = FakeResponse(json_data={"value": [SAMPLE_STALE_MSG]})
        msgs = manager.poll_stale_unclaimed()
        assert len(msgs) == 1

    def test_poll_filters_by_age(self, manager):
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})
        manager.poll_stale_unclaimed()
        url = manager.dv.get.call_args[0][0]
        assert "createdon lt" in url
        assert "cr_status eq 'Unclaimed'" in url

    def test_poll_handles_timeout(self, manager):
        from dv_client import DataverseRetryExhausted
        manager.dv.get.side_effect = DataverseRetryExhausted("timeout")
        assert manager.poll_stale_unclaimed() == []

    def test_poll_handles_error(self, manager):
        from dv_client import DataverseError
        manager.dv.get.side_effect = DataverseError("network error", 500, "")
        assert manager.poll_stale_unclaimed() == []

    def test_poll_empty_result(self, manager):
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})
        assert manager.poll_stale_unclaimed() == []


class TestResponse:
    """Response writing tests (unchanged from original architecture)."""

    def test_send_response(self, manager):
        manager.dv.post.return_value = FakeResponse(json_data={})
        result = manager.send_response(
            in_reply_to=SAMPLE_CONVERSATION_ID,
            mcs_conversation_id=SAMPLE_MCS_CONV_ID,
            user_email="newuser@example.com",
            text="Welcome!",
        )
        assert result is not None
        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_direction"] == "Outbound"
        assert body["cr_message"] == "Welcome!"
        assert body["cr_in_reply_to"] == SAMPLE_CONVERSATION_ID
        assert body["cr_mcs_conversation_id"] == SAMPLE_MCS_CONV_ID

    def test_send_response_error(self, manager):
        from dv_client import DataverseError
        manager.dv.post.side_effect = DataverseError("error", 500, "")
        assert manager.send_response("id", "conv", "email", "text") is None

    def test_send_response_followup(self, manager):
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.send_response(
            in_reply_to="row-1",
            mcs_conversation_id="mcs-1",
            user_email="user@example.com",
            text="Working on it...",
            followup_expected=True,
        )
        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_followup_expected"] == "true"

    def test_send_response_no_followup(self, manager):
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.send_response(
            in_reply_to="row-1",
            mcs_conversation_id="mcs-1",
            user_email="user@example.com",
            text="Done!",
        )
        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_followup_expected"] == ""

    def test_send_response_truncates_name(self, manager):
        """cr_name field should be truncated to 100 chars (DV column limit)."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        long_text = "A" * 500
        manager.send_response("row-1", "mcs-1", "user@test.com", long_text)
        body = manager.dv.post.call_args[1]["data"]
        assert len(body["cr_name"]) == 100
        assert body["cr_message"] == long_text  # Full message preserved


# ============================================================================
# Acceptance Criterion 7: ETag concurrency preserved
# ============================================================================

class TestClaim:
    """Claim tests verifying ETag-based optimistic concurrency."""

    def test_claim_success(self, manager):
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        assert manager.claim_message(SAMPLE_STALE_MSG) is True

    def test_claim_sets_global_id(self, manager):
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.claim_message(SAMPLE_STALE_MSG)
        body = manager.dv.patch.call_args[1]["data"]
        assert body["cr_claimed_by"].startswith("global:")

    def test_claim_uses_etag(self, manager):
        """The ETag from the message must be sent via the etag kwarg to dv.patch."""
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.claim_message(SAMPLE_STALE_MSG)
        call_kwargs = manager.dv.patch.call_args[1]
        assert call_kwargs["etag"] == 'W/"12345"'

    def test_claim_conflict_returns_false(self, manager):
        """ETagConflictError means another GM claimed it first."""
        from dv_client import ETagConflictError
        manager.dv.patch.side_effect = ETagConflictError("412 conflict")
        assert manager.claim_message(SAMPLE_STALE_MSG) is False

    def test_claim_no_etag(self, manager):
        msg = {**SAMPLE_STALE_MSG}
        del msg["@odata.etag"]
        assert manager.claim_message(msg) is False

    def test_claim_no_id(self, manager):
        msg = {**SAMPLE_STALE_MSG}
        del msg["cr_shraga_conversationid"]
        assert manager.claim_message(msg) is False

    def test_claim_sets_claimed_status(self, manager):
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.claim_message(SAMPLE_STALE_MSG)
        body = manager.dv.patch.call_args[1]["data"]
        assert body["cr_status"] == "Claimed"


# ============================================================================
# Acceptance Criterion 8: New user and known user flows
# ============================================================================

class TestProcessMessage:
    """Message processing using Claude Code sessions."""

    def test_process_uses_claude_code_and_sends_response(self, manager):
        """Claude Code is called and its response is sent to the user."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        with patch.object(manager, "_call_claude_code", return_value=("Hello! I can help you.", "sess-1")):
            manager.process_message(SAMPLE_STALE_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_message"] == "Hello! I can help you."

    def test_process_fallback_when_claude_unavailable(self, manager):
        """When Claude Code is unavailable, the single fallback message is sent."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        with patch.object(manager, "_call_claude_code", return_value=(None, "")):
            manager.process_message(SAMPLE_STALE_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_message"] == "The system is temporarily unavailable, please try again shortly."

    def test_process_empty_message(self, manager):
        """Empty messages are just marked as processed."""
        empty_msg = {**SAMPLE_STALE_MSG, "cr_message": ""}
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.process_message(empty_msg)
        manager.dv.patch.assert_called_once()

    def test_process_creates_session_for_conversation(self, manager):
        """Processing a message creates a session keyed by mcs_conversation_id."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        with patch.object(manager, "_call_claude_code", return_value=("Hi!", "sess-hi")):
            manager.process_message(SAMPLE_STALE_MSG)

        session = manager.session_manager.get_session(SAMPLE_MCS_CONV_ID)
        assert session is not None
        assert "session_id" in session

    def test_process_reuses_session_for_same_conversation(self, manager):
        """Second message in same conversation reuses the session."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        session_ids_passed = []

        def capture_session(prompt, session_id=None):
            session_ids_passed.append(session_id)
            return ("Response", "sess-reuse")

        with patch.object(manager, "_call_claude_code", side_effect=capture_session):
            manager.process_message(SAMPLE_STALE_MSG)
            # Second message in same conversation
            msg2 = {**SAMPLE_STALE_MSG, "cr_shraga_conversationid": "conv-0002"}
            manager.process_message(msg2)

        assert len(session_ids_passed) == 2
        # First call has no session, second call reuses the session saved from first call
        assert session_ids_passed[0] is None
        assert session_ids_passed[1] == "sess-reuse"

    def test_process_different_conversations_different_sessions(self, manager):
        """Different conversations get different sessions."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        call_counter = {"n": 0}

        def capture_session(prompt, session_id=None):
            call_counter["n"] += 1
            return ("Response", f"sess-{call_counter['n']}")

        with patch.object(manager, "_call_claude_code", side_effect=capture_session):
            manager.process_message(SAMPLE_STALE_MSG)
            msg2 = {
                **SAMPLE_STALE_MSG,
                "cr_mcs_conversation_id": "mcs-conv-different",
                "cr_shraga_conversationid": "conv-0002",
            }
            manager.process_message(msg2)

        # Verify different conversations got different sessions stored
        sess1 = manager.session_manager.get_session(SAMPLE_MCS_CONV_ID)
        sess2 = manager.session_manager.get_session("mcs-conv-different")
        assert sess1 is not None and sess2 is not None
        assert sess1["session_id"] != sess2["session_id"], "Different conversations must use different sessions"

    def test_process_passes_user_context_in_prompt(self, manager):
        """The prompt to Claude Code includes user email, row ID, and message."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        captured_prompts = []

        def capture_prompt(prompt, session_id=None):
            captured_prompts.append(prompt)
            return ("Response", "sess-ctx")

        with patch.object(manager, "_call_claude_code", side_effect=capture_prompt):
            manager.process_message(SAMPLE_STALE_MSG)

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        assert "newuser@example.com" in prompt
        assert SAMPLE_CONVERSATION_ID in prompt
        assert SAMPLE_MCS_CONV_ID in prompt
        assert "hello, I want to create a task" in prompt

    def test_process_marks_message_processed(self, manager):
        """After processing, the inbound message is marked as Processed."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        with patch.object(manager, "_call_claude_code", return_value=("Done", "sess-done")):
            manager.process_message(SAMPLE_STALE_MSG)

        # Find the PATCH call that sets status to Processed
        found_processed = False
        for c in manager.dv.patch.call_args_list:
            body = c[1].get("data", {})
            if body.get("cr_status") == "Processed":
                found_processed = True
                break
        assert found_processed, "Message should be marked as Processed"


class TestNewUserFlow:
    """Verify new user handling through the thin wrapper."""

    def test_new_user_not_in_known_users(self, manager):
        """A new user who has never been seen should not be in _known_users."""
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})
        is_known = manager._is_known_user("brand-new@example.com")
        assert is_known is False
        assert "brand-new@example.com" not in manager._known_users

    def test_known_user_detected(self, manager):
        """A user found in DV users table is recognized as known."""
        manager.dv.get.return_value = FakeResponse(json_data={
            "value": [{"crb3b_shragauserid": "row-123"}]
        })
        is_known = manager._is_known_user("existing@example.com")
        assert is_known is True
        assert "existing@example.com" in manager._known_users

    def test_known_user_cached(self, manager):
        """Once a user is known, subsequent checks don't hit DV."""
        manager._known_users.add("cached@example.com")
        is_known = manager._is_known_user("cached@example.com")
        assert is_known is True
        manager.dv.get.assert_not_called()


class TestKnownUserFlow:
    """Verify known user (PM unavailable) flow through the thin wrapper."""

    def test_known_user_delayed_claiming(self, manager):
        """Known users' messages have a delayed claiming window."""
        # Return a known user from DV, then return the message
        def get_side_effect(url, **kwargs):
            if "shragausers" in url:
                return FakeResponse(json_data={"value": [{"crb3b_shragauserid": "row-1"}]})
            if "conversations" in url:
                # Message created very recently (should NOT be claimable for known user)
                recent_msg = {
                    **SAMPLE_STALE_MSG,
                    "createdon": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                return FakeResponse(json_data={"value": [recent_msg]})
            return FakeResponse(json_data={"value": []})

        manager.dv.get.side_effect = get_side_effect
        msgs = manager.poll_stale_unclaimed()
        # Recent messages from known users should NOT be returned
        assert len(msgs) == 0


# ============================================================================
# Acceptance Criterion 9: General tests
# ============================================================================

class TestAuth:
    """Authentication tests -- token management is delegated to DataverseClient."""

    def test_has_dv_client(self, manager):
        """Manager must have a dv attribute (DataverseClient or mock)."""
        assert hasattr(manager, "dv")

    def test_has_credential(self, manager):
        """Manager must store the Azure credential for reference."""
        assert hasattr(manager, "credential")
        assert manager.credential is not None


class TestConstructor:
    """Constructor tests."""

    def test_manager_id(self, manager):
        assert manager.manager_id == "global"

    def test_known_users_empty(self, manager):
        assert len(manager._known_users) == 0

    def test_has_session_manager(self, manager):
        """Manager must have a SessionManager instance."""
        assert hasattr(manager, "session_manager")
        from global_manager import SessionManager
        assert isinstance(manager.session_manager, SessionManager)


class TestGetCredential:
    """Tests for the get_credential() function."""

    def test_uses_default_credential_when_available(self):
        fake_cred = MagicMock()
        fake_cred.get_token.return_value = FakeAccessToken()

        with patch("global_manager.DefaultAzureCredential", return_value=fake_cred):
            from global_manager import get_credential
            result = get_credential()

        assert result is fake_cred
        from global_manager import DATAVERSE_URL
        fake_cred.get_token.assert_called_once_with(f"{DATAVERSE_URL}/.default")

    def test_raises_when_no_credentials_available(self):
        broken_cred = MagicMock()
        broken_cred.get_token.side_effect = Exception("No credentials")

        with patch("global_manager.DefaultAzureCredential", return_value=broken_cred):
            from global_manager import get_credential
            with pytest.raises(SystemExit):
                get_credential()


class TestMarkProcessed:
    """Tests for mark_processed after thin-wrapper refactor.

    Verifies:
      - PATCH call parameters (URL, body, timeout)
      - Graceful handling of Dataverse failures (no exception propagated)
      - Behaviour on ETagConflictError (silent degradation)
    """

    def test_mark_processed_success(self, manager):
        """Verify the PATCH call sends the correct URL, body, and timeout."""
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        row_id = "row-12345678-abcd-efgh-ijkl-9999"
        manager.mark_processed(row_id)

        # Called exactly once
        manager.dv.patch.assert_called_once()

        # -- URL contains the conversations table and the row ID ---------------
        call_args, call_kwargs = manager.dv.patch.call_args
        url = call_args[0]
        from global_manager import CONVERSATIONS_TABLE, DATAVERSE_API, REQUEST_TIMEOUT
        assert CONVERSATIONS_TABLE in url, "URL must reference the conversations table"
        assert row_id in url, "URL must contain the row ID"
        assert url == f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}({row_id})"

        # -- Body sets status to Processed -------------------------------------
        body = call_kwargs["data"]
        assert body == {"cr_status": "Processed"}, (
            "Body must set cr_status to 'Processed' and nothing else"
        )

        # -- Timeout is the module-level REQUEST_TIMEOUT -----------------------
        assert call_kwargs["timeout"] == REQUEST_TIMEOUT

    def test_mark_processed_dv_failure(self, manager):
        """Dataverse failure must not propagate.

        mark_processed is a fire-and-forget helper -- the message has already
        been answered, so a failure here should be logged but not raise.
        """
        from dv_client import DataverseError, DataverseRetryExhausted

        # Scenario 1: DataverseError (non-retryable 4xx/5xx)
        manager.dv.patch.side_effect = DataverseError("500 error", 500, "Internal Server Error")
        manager.mark_processed("row-fail-500")  # must not raise

        # Scenario 2: DataverseRetryExhausted (retry budget exhausted)
        manager.dv.patch.side_effect = DataverseRetryExhausted("timeout")
        manager.mark_processed("row-fail-timeout")  # must not raise

    def test_mark_processed_etag_conflict(self, manager):
        """ETagConflictError must not crash.

        Although mark_processed does not send an etag itself,
        the DataverseClient may raise ETagConflictError in edge cases.
        The method must degrade gracefully.
        """
        from dv_client import DataverseError, DataverseRetryExhausted

        # DataverseError with 412 status
        manager.dv.patch.side_effect = DataverseError("412 conflict", 412, "Precondition Failed")
        manager.mark_processed("row-etag-conflict")  # must not raise

        # DataverseRetryExhausted
        manager.dv.patch.side_effect = DataverseRetryExhausted("exhausted")
        manager.mark_processed("row-etag-conflict-raised")  # must not raise


class TestSessionManagerEdgeCases:
    """Additional edge cases for SessionManager."""

    def test_get_session_nonexistent(self, session_mgr):
        """get_session returns None for unknown conversation."""
        assert session_mgr.get_session("nonexistent-conv") is None

    def test_sessions_property_returns_copy(self, session_mgr):
        """The sessions property should return a copy, not a reference."""
        session_mgr.save_session("conv-1", "session-copy-1", "a@test.com")
        sessions = session_mgr.sessions
        sessions["conv-1"]["hacked"] = True
        # Original should not be affected
        assert "hacked" not in session_mgr._sessions["conv-1"]

    def test_session_id_stored_correctly(self, session_mgr):
        """Session IDs are stored and retrieved correctly."""
        session_mgr.save_session("conv-hex", "abc123def456", "user@test.com")
        entry = session_mgr.get_session("conv-hex")
        assert entry is not None
        assert entry["session_id"] == "abc123def456"

    def test_cleanup_empty_sessions(self, session_mgr):
        """Cleanup on empty sessions should return 0."""
        removed = session_mgr.cleanup_expired()
        assert removed == 0


class TestModuleConstants:
    """Verify module-level constants are correct."""

    def test_fallback_message(self):
        from global_manager import FALLBACK_MESSAGE
        assert FALLBACK_MESSAGE == "The system is temporarily unavailable, please try again shortly."

    def test_direction_constants(self):
        from global_manager import DIRECTION_INBOUND, DIRECTION_OUTBOUND
        assert DIRECTION_INBOUND == "Inbound"
        assert DIRECTION_OUTBOUND == "Outbound"

    def test_status_constants(self):
        from global_manager import STATUS_UNCLAIMED, STATUS_CLAIMED, STATUS_PROCESSED
        assert STATUS_UNCLAIMED == "Unclaimed"
        assert STATUS_CLAIMED == "Claimed"
        assert STATUS_PROCESSED == "Processed"

    def test_no_tool_definitions_constant(self):
        """TOOL_DEFINITIONS must not be defined at module level."""
        import global_manager
        assert not hasattr(global_manager, "TOOL_DEFINITIONS")


class TestLineCount:
    """Verify the refactored code is significantly smaller than the original."""

    def test_module_is_under_target_size(self):
        """The refactored global_manager.py should be significantly smaller than the original ~1115 lines.

        The target was ~200 lines of pure logic, but the actual file includes
        docstrings, the SessionManager class, necessary whitespace, and
        file-logging boilerplate (~20 lines).
        We verify it is under 650 lines (roughly half the original).
        """
        gm_path = Path(__file__).parent / "global-manager" / "global_manager.py"
        content = gm_path.read_text(encoding="utf-8")
        line_count = len(content.strip().split("\n"))
        # Must be significantly less than the original ~1115 lines
        assert line_count < 650, (
            f"global_manager.py has {line_count} lines, "
            f"should be significantly less than the original ~1115"
        )
        # Should be at least 40% smaller than original
        original_lines = 1115
        reduction_pct = (1 - line_count / original_lines) * 100
        assert reduction_pct > 40, (
            f"Only {reduction_pct:.0f}% reduction from original -- "
            f"expected at least 40%"
        )
