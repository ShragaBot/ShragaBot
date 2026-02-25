"""
Tests for Global Manager (thin wrapper + DV-based session resolution).

All external dependencies (Azure, Dataverse, Claude Code CLI) are mocked.
Tests verify:
  - No old tool-wrapper code remains
  - Claude Code subprocess invocation (--resume, --print, -p)
  - DV polling/claiming (ETag concurrency)
  - Response writing (Outbound rows) with [GM:xxxx] prefix
  - New user and known user flows
  - Fallback message when Claude Code is unavailable
  - Session tracking via _current_sessions dict
  - cr_claimed_by format: gm:version:box:instance_id
  - cr_processed_by written on outbound rows
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
def manager(mock_credential):
    """Create a GlobalManager with mocked credentials."""
    with patch("global_manager.get_credential", return_value=mock_credential):
        from global_manager import GlobalManager
        mgr = GlobalManager()
    mgr.dv = MagicMock()
    return mgr


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

    def test_no_session_manager_class(self):
        """SessionManager class must be removed from global_manager module."""
        import global_manager
        assert not hasattr(global_manager, "SessionManager"), \
            "SessionManager class should be removed"

    def test_no_session_manager_attribute(self, manager):
        """Manager must not have session_manager attribute."""
        assert not hasattr(manager, "session_manager"), \
            "session_manager attribute should be removed"

    def test_has_current_sessions_dict(self, manager):
        """Manager must have _current_sessions dict for within-run tracking."""
        assert hasattr(manager, "_current_sessions")
        assert isinstance(manager._current_sessions, dict)


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
    """Response writing tests -- updated for [GM:xxxx] prefix."""

    def test_send_response_without_session(self, manager):
        """Response without session_id has no prefix."""
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
        assert body["cr_message"] == "Welcome!"  # No prefix without session_id

    def test_send_response_with_session_prefix(self, manager):
        """Response with session_id gets [GM:xxxx] prefix."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.send_response(
            in_reply_to=SAMPLE_CONVERSATION_ID,
            mcs_conversation_id=SAMPLE_MCS_CONV_ID,
            user_email="newuser@example.com",
            text="Welcome!",
            session_id="a7f3c2d1-abcd-1234",
        )
        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_message"] == "[GM:a7f3] Welcome!"

    def test_send_response_with_processed_by(self, manager):
        """cr_processed_by is written on outbound row when provided."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.send_response(
            in_reply_to="row-1",
            mcs_conversation_id="mcs-1",
            user_email="user@test.com",
            text="Hi",
            processed_by="gm:v19:some-session-id",
        )
        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_processed_by"] == "gm:v19:some-session-id"

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


# ============================================================================
# Acceptance Criterion 7: ETag concurrency preserved
# ============================================================================

class TestClaim:
    """Claim tests verifying ETag-based optimistic concurrency."""

    def test_claim_success(self, manager):
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        assert manager.claim_message(SAMPLE_STALE_MSG) is True

    def test_claim_sets_new_format(self, manager):
        """cr_claimed_by should use new format: gm:version:box:instance_id."""
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.claim_message(SAMPLE_STALE_MSG)
        body = manager.dv.patch.call_args[1]["data"]
        claimed_by = body["cr_claimed_by"]
        assert claimed_by.startswith("gm:")
        parts = claimed_by.split(":")
        assert len(parts) == 4  # gm:version:box:instance_id

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
    """Message processing using Claude Code sessions with DV-based resolution."""

    def test_process_uses_claude_code_and_sends_response(self, manager):
        """Claude Code is called and its response is sent to the user."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        # Mock DV get for resolve_session (returns no history => new session)
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})

        with patch.object(manager, "_call_claude_code", return_value=("Hello! I can help you.", "sess-1")):
            manager.process_message(SAMPLE_STALE_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert "Hello! I can help you." in body["cr_message"]

    def test_process_writes_processed_by(self, manager):
        """cr_processed_by should be written on the outbound response row."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})

        with patch.object(manager, "_call_claude_code", return_value=("Response", "sess-abc123")):
            manager.process_message(SAMPLE_STALE_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert "cr_processed_by" in body
        assert body["cr_processed_by"].startswith("gm:")
        assert "sess-abc123" in body["cr_processed_by"]

    def test_process_adds_gm_prefix(self, manager):
        """Outbound message should have [GM:xxxx] prefix."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})

        with patch.object(manager, "_call_claude_code", return_value=("Hello!", "sess-a7f3c2d1")):
            manager.process_message(SAMPLE_STALE_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_message"].startswith("[GM:sess]")

    def test_process_fallback_when_claude_unavailable(self, manager):
        """When Claude Code is unavailable, the single fallback message is sent."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})

        with patch.object(manager, "_call_claude_code", return_value=(None, "")):
            manager.process_message(SAMPLE_STALE_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert "temporarily unavailable" in body["cr_message"]

    def test_process_empty_message(self, manager):
        """Empty messages are just marked as processed."""
        empty_msg = {**SAMPLE_STALE_MSG, "cr_message": ""}
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.process_message(empty_msg)
        manager.dv.patch.assert_called_once()

    def test_process_tracks_session_for_reuse(self, manager):
        """After processing, the session ID is stored in _current_sessions."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})

        with patch.object(manager, "_call_claude_code", return_value=("Hi!", "sess-track")):
            manager.process_message(SAMPLE_STALE_MSG)

        assert SAMPLE_MCS_CONV_ID in manager._current_sessions
        assert manager._current_sessions[SAMPLE_MCS_CONV_ID] == "sess-track"

    def test_process_reuses_session_for_same_conversation(self, manager):
        """Second message in same conversation reuses the within-run session."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})

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
        # First call has no session (no within-run session yet)
        assert session_ids_passed[0] is None
        # Second call reuses the within-run session
        assert session_ids_passed[1] == "sess-reuse"

    def test_process_passes_user_context_in_prompt(self, manager):
        """The prompt to Claude Code includes user email, row ID, and message."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})

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
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})

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
        def get_side_effect(url, **kwargs):
            if "shragausers" in url:
                return FakeResponse(json_data={"value": [{"crb3b_shragauserid": "row-1"}]})
            if "conversations" in url:
                recent_msg = {
                    **SAMPLE_STALE_MSG,
                    "createdon": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                return FakeResponse(json_data={"value": [recent_msg]})
            return FakeResponse(json_data={"value": []})

        manager.dv.get.side_effect = get_side_effect
        msgs = manager.poll_stale_unclaimed()
        assert len(msgs) == 0


# ============================================================================
# Acceptance Criterion 9: General tests
# ============================================================================

class TestAuth:
    """Authentication tests -- token management is delegated to DataverseClient."""

    def test_has_dv_client(self, manager):
        assert hasattr(manager, "dv")

    def test_has_credential(self, manager):
        assert hasattr(manager, "credential")
        assert manager.credential is not None


class TestConstructor:
    """Constructor tests."""

    def test_manager_id(self, manager):
        assert manager.manager_id == "global"

    def test_known_users_empty(self, manager):
        assert len(manager._known_users) == 0

    def test_has_agent_role(self, manager):
        """Manager should use AGENT_ROLE constant."""
        from global_manager import AGENT_ROLE
        assert AGENT_ROLE == "GM"

    def test_has_version(self, manager):
        """Manager should have _my_version attribute."""
        assert hasattr(manager, "_my_version")
        assert isinstance(manager._my_version, str)

    def test_has_box_name(self, manager):
        """Manager should have _box_name attribute."""
        assert hasattr(manager, "_box_name")
        assert isinstance(manager._box_name, str)


class TestGetCredential:
    """Tests for the get_credential() function."""

    def test_uses_default_credential_when_available(self):
        fake_cred = MagicMock()
        fake_cred.get_token.return_value = FakeAccessToken()

        with patch("global_manager.create_credential", return_value=fake_cred):
            from global_manager import get_credential
            result = get_credential()

        assert result is fake_cred
        from global_manager import DATAVERSE_URL
        fake_cred.get_token.assert_called_once_with(f"{DATAVERSE_URL}/.default")

    def test_raises_when_no_credentials_available(self):
        broken_cred = MagicMock()
        broken_cred.get_token.side_effect = Exception("No credentials")

        with patch("global_manager.create_credential", return_value=broken_cred):
            from global_manager import get_credential
            with pytest.raises(SystemExit):
                get_credential()


class TestMarkProcessed:
    """Tests for mark_processed."""

    def test_mark_processed_success(self, manager):
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        row_id = "row-12345678-abcd-efgh-ijkl-9999"
        manager.mark_processed(row_id)
        manager.dv.patch.assert_called_once()
        call_args, call_kwargs = manager.dv.patch.call_args
        url = call_args[0]
        from global_manager import CONVERSATIONS_TABLE, DATAVERSE_API, REQUEST_TIMEOUT
        assert CONVERSATIONS_TABLE in url
        assert row_id in url
        body = call_kwargs["data"]
        assert body == {"cr_status": "Processed"}
        assert call_kwargs["timeout"] == REQUEST_TIMEOUT

    def test_mark_processed_dv_failure(self, manager):
        from dv_client import DataverseError, DataverseRetryExhausted
        manager.dv.patch.side_effect = DataverseError("500 error", 500, "Internal Server Error")
        manager.mark_processed("row-fail-500")  # must not raise
        manager.dv.patch.side_effect = DataverseRetryExhausted("timeout")
        manager.mark_processed("row-fail-timeout")  # must not raise


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
        import global_manager
        assert not hasattr(global_manager, "TOOL_DEFINITIONS")

    def test_no_sessions_dir_constant(self):
        """SESSIONS_DIR should be removed (no local session files)."""
        import global_manager
        assert not hasattr(global_manager, "SESSIONS_DIR")

    def test_no_sessions_file_constant(self):
        """SESSIONS_FILE should be removed (no local session files)."""
        import global_manager
        assert not hasattr(global_manager, "SESSIONS_FILE")

    def test_no_session_expiry_constant(self):
        """SESSION_EXPIRY_HOURS should be removed."""
        import global_manager
        assert not hasattr(global_manager, "SESSION_EXPIRY_HOURS")
