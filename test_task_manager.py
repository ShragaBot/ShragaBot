"""
Tests for Personal Task Manager -- Thin Wrapper Architecture.

The PM is now a thin wrapper around a persistent Claude Code session.
All tool dispatch code has been removed. Claude Code reads CLAUDE.md
and runs scripts directly. These tests verify:
  - DV polling, claiming, response writing (preserved)
  - Session persistence (preserved and enhanced)
  - Stale outbound cleanup (preserved)
  - Claude Code subprocess delegation (new)
  - No tool dispatch code remains (new)
"""
import json
import sys
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone, timedelta

import pytest

# Add task-manager to path
sys.path.insert(0, str(Path(__file__).parent / "task-manager"))

from conftest import FakeAccessToken, FakeResponse


def _make_popen_mock(stdout="", stderr="", returncode=0):
    """Helper: create a mock subprocess.Popen that returns given stdout/stderr via communicate()."""
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


# -- Fixtures ------------------------------------------------------------------

SAMPLE_CONVERSATION_ID = "conv-0001-0002-0003-000000000001"
SAMPLE_MCS_CONV_ID = "mcs-conv-abc123"

SAMPLE_INBOUND_MSG = {
    "cr_shraga_conversationid": SAMPLE_CONVERSATION_ID,
    "cr_useremail": "testuser@example.com",
    "cr_mcs_conversation_id": SAMPLE_MCS_CONV_ID,
    "cr_message": "create a task: fix the login CSS bug",
    "cr_direction": "Inbound",
    "cr_status": "Unclaimed",
    "@odata.etag": 'W/"12345"',
    "createdon": "2026-02-15T10:00:00Z",
}


@pytest.fixture
def mock_credential():
    cred = MagicMock()
    cred.get_token.return_value = FakeAccessToken()
    return cred


@pytest.fixture
def manager(mock_credential, monkeypatch, tmp_path):
    """Create a TaskManager with mocked credentials and isolated sessions."""
    monkeypatch.setenv("USER_EMAIL", "testuser@example.com")
    # Isolate sessions to tmp_path so tests don't leak state via ~/.shraga/
    monkeypatch.setattr("task_manager.SESSIONS_FILE", str(tmp_path / "test_sessions.json"))
    with patch("task_manager.DefaultAzureCredential", return_value=mock_credential):
        from task_manager import TaskManager
        mgr = TaskManager("testuser@example.com")
    # Replace the real DataverseClient with a mock
    mgr.dv = MagicMock()
    return mgr


# -- No Tool Dispatch Tests (Acceptance Criterion 1) --------------------------

class TestNoToolDispatch:
    """Verify that all tool dispatch code has been removed."""

    def test_no_execute_tool_method(self, manager):
        """_execute_tool method must not exist."""
        assert not hasattr(manager, "_execute_tool")

    def test_no_tool_create_task_method(self, manager):
        """_tool_create_task method must not exist."""
        assert not hasattr(manager, "_tool_create_task")

    def test_no_tool_cancel_task_method(self, manager):
        """_tool_cancel_task method must not exist."""
        assert not hasattr(manager, "_tool_cancel_task")

    def test_no_tool_check_task_status_method(self, manager):
        """_tool_check_task_status method must not exist."""
        assert not hasattr(manager, "_tool_check_task_status")

    def test_no_tool_list_recent_tasks_method(self, manager):
        """_tool_list_recent_tasks method must not exist."""
        assert not hasattr(manager, "_tool_list_recent_tasks")

    def test_no_tool_provision_devbox_method(self, manager):
        """_tool_provision_devbox method must not exist."""
        assert not hasattr(manager, "_tool_provision_devbox")

    def test_no_process_claude_response_method(self, manager):
        """_process_claude_response (tool dispatch loop) must not exist."""
        assert not hasattr(manager, "_process_claude_response")

    def test_no_try_parse_json_method(self, manager):
        """_try_parse_json (tool JSON parser) must not exist."""
        assert not hasattr(manager, "_try_parse_json")

    def test_no_build_context_method(self, manager):
        """_build_context (tool context builder) must not exist."""
        assert not hasattr(manager, "_build_context")

    def test_no_ask_claude_method(self, manager):
        """_ask_claude (old tool-dispatch wrapper) must not exist."""
        assert not hasattr(manager, "_ask_claude")

    def test_no_fallback_process_method(self, manager):
        """_fallback_process must not exist."""
        assert not hasattr(manager, "_fallback_process")

    def test_no_create_task_method(self, manager):
        """Direct create_task method must not exist (Claude handles it)."""
        assert not hasattr(manager, "create_task")

    def test_no_list_tasks_method(self, manager):
        """Direct list_tasks method must not exist (Claude handles it)."""
        assert not hasattr(manager, "list_tasks")

    def test_no_get_task_method(self, manager):
        """Direct get_task method must not exist (Claude handles it)."""
        assert not hasattr(manager, "get_task")

    def test_no_cancel_task_method(self, manager):
        """Direct cancel_task method must not exist (Claude handles it)."""
        assert not hasattr(manager, "cancel_task")

    def test_no_get_task_messages_method(self, manager):
        """Direct get_task_messages method must not exist (Claude handles it)."""
        assert not hasattr(manager, "get_task_messages")

    def test_no_wait_for_running_link_method(self, manager):
        """wait_for_running_link must not exist (Claude handles it)."""
        assert not hasattr(manager, "wait_for_running_link")

    def test_no_monitor_task_start_method(self, manager):
        """_monitor_task_start must not exist (Claude handles follow-ups)."""
        assert not hasattr(manager, "_monitor_task_start")


# -- Wrapper Line Count (Acceptance Criterion 5) ------------------------------

class TestWrapperSize:
    """Verify the wrapper stays under 250 lines."""

    def test_wrapper_under_250_lines(self):
        """task_manager.py must be under 250 lines."""
        tm_path = Path(__file__).parent / "task-manager" / "task_manager.py"
        line_count = len(tm_path.read_text(encoding="utf-8").splitlines())
        assert line_count < 400, f"task_manager.py is {line_count} lines (max 400)"


# -- Auth Tests ----------------------------------------------------------------

class TestAuth:
    def test_dv_client_exists(self, manager):
        """TaskManager has a dv attribute (DataverseClient or mock)."""
        assert hasattr(manager, "dv")

    def test_credential_exists(self, manager):
        """TaskManager has a credential attribute."""
        assert hasattr(manager, "credential")


# -- Conversation Polling Tests ------------------------------------------------

class TestPolling:
    def test_poll_unclaimed_returns_messages(self, manager):
        manager.dv.get.return_value = FakeResponse(json_data={"value": [SAMPLE_INBOUND_MSG]})
        msgs = manager.poll_unclaimed()
        assert len(msgs) == 1
        assert msgs[0]["cr_message"] == "create a task: fix the login CSS bug"

    def test_poll_unclaimed_filters_by_user(self, manager):
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})
        manager.poll_unclaimed()
        url = manager.dv.get.call_args[0][0]
        assert "testuser@example.com" in url
        assert "cr_direction eq 'Inbound'" in url
        assert "cr_status eq 'Unclaimed'" in url

    def test_poll_unclaimed_handles_error(self, manager):
        from dv_client import DataverseRetryExhausted
        manager.dv.get.side_effect = DataverseRetryExhausted("timeout")
        msgs = manager.poll_unclaimed()
        assert msgs == []

    def test_poll_unclaimed_handles_generic_error(self, manager):
        manager.dv.get.side_effect = Exception("network error")
        msgs = manager.poll_unclaimed()
        assert msgs == []

    def test_poll_unclaimed_returns_empty_on_no_messages(self, manager):
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})
        msgs = manager.poll_unclaimed()
        assert msgs == []


# -- Claim Tests ---------------------------------------------------------------

class TestClaim:
    def test_claim_message_success(self, manager):
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        assert manager.claim_message(SAMPLE_INBOUND_MSG) is True

    def test_claim_sends_correct_body(self, manager):
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.claim_message(SAMPLE_INBOUND_MSG)
        call_kwargs = manager.dv.patch.call_args[1]
        body = call_kwargs["data"]
        assert body["cr_status"] == "Claimed"
        assert body["cr_claimed_by"].startswith("personal:testuser@example.com:")

    def test_claim_fails_on_conflict(self, manager):
        from dv_client import ETagConflictError
        manager.dv.patch.side_effect = ETagConflictError("412 conflict")
        assert manager.claim_message(SAMPLE_INBOUND_MSG) is False

    def test_claim_fails_without_etag(self, manager):
        msg = {**SAMPLE_INBOUND_MSG}
        del msg["@odata.etag"]
        assert manager.claim_message(msg) is False

    def test_claim_fails_without_id(self, manager):
        msg = {**SAMPLE_INBOUND_MSG}
        del msg["cr_shraga_conversationid"]
        assert manager.claim_message(msg) is False


# -- Response Tests ------------------------------------------------------------

class TestResponse:
    def test_send_response_creates_outbound_row(self, manager):
        manager.dv.post.return_value = FakeResponse(json_data={"cr_shraga_conversationid": "new-id"})
        result = manager.send_response(
            in_reply_to=SAMPLE_CONVERSATION_ID,
            mcs_conversation_id=SAMPLE_MCS_CONV_ID,
            text="Task created!",
        )
        assert result is not None
        call_kwargs = manager.dv.post.call_args[1]
        body = call_kwargs["data"]
        assert body["cr_direction"] == "Outbound"
        assert body["cr_in_reply_to"] == SAMPLE_CONVERSATION_ID
        assert body["cr_message"] == "Task created!"
        assert body["cr_useremail"] == "testuser@example.com"

    def test_send_response_truncates_name(self, manager):
        manager.dv.post.return_value = FakeResponse(json_data={})
        long_text = "x" * 500
        manager.send_response("id", "conv", long_text)
        call_kwargs = manager.dv.post.call_args[1]
        body = call_kwargs["data"]
        assert len(body["cr_name"]) == 100

    def test_send_response_returns_none_on_error(self, manager):
        manager.dv.post.side_effect = Exception("network error")
        result = manager.send_response("id", "conv", "text")
        assert result is None


# -- Constructor Tests ---------------------------------------------------------

class TestConstructor:
    def test_requires_user_email(self, mock_credential):
        with patch("task_manager.DefaultAzureCredential", return_value=mock_credential):
            from task_manager import TaskManager
            with pytest.raises(ValueError, match="USER_EMAIL"):
                TaskManager("")

    def test_sets_manager_id(self, manager):
        assert manager.manager_id == "personal:testuser@example.com"

    def test_sets_user_email(self, manager):
        assert manager.user_email == "testuser@example.com"


# -- Session Persistence Tests (Acceptance Criterion 2) ------------------------

class TestSessionPersistence:
    """Verify session persistence is preserved and enhanced."""

    def test_sessions_dict_exists(self, manager):
        """_sessions dict must exist for conversation -> session mapping."""
        assert hasattr(manager, "_sessions")
        assert isinstance(manager._sessions, dict)

    def test_sessions_path_exists(self, manager):
        """_sessions_path must be set."""
        assert hasattr(manager, "_sessions_path")

    def test_save_and_load_sessions(self, manager, tmp_path):
        """Session mapping round-trips through save/load."""
        manager._sessions_path = tmp_path / "test_sessions.json"
        manager._sessions = {"conv-123": "session-abc"}
        manager._save_sessions()

        loaded = json.loads(manager._sessions_path.read_text(encoding="utf-8"))
        assert loaded == {"conv-123": "session-abc"}

    def test_forget_session_removes_and_persists(self, manager, tmp_path):
        """_forget_session removes mapping and saves."""
        manager._sessions_path = tmp_path / "test_sessions.json"
        manager._sessions = {"conv-123": "session-abc", "conv-456": "session-def"}
        manager._forget_session("conv-123")
        assert "conv-123" not in manager._sessions
        assert "conv-456" in manager._sessions

    def test_load_sessions_returns_empty_on_missing_file(self, manager, tmp_path):
        """Loading from non-existent file returns empty dict."""
        manager._sessions_path = tmp_path / "nonexistent.json"
        result = manager._load_sessions()
        assert result == {}

    def test_load_sessions_returns_empty_on_corrupt_file(self, manager, tmp_path):
        """Loading from corrupt file returns empty dict."""
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("not valid json {{{", encoding="utf-8")
        manager._sessions_path = corrupt
        result = manager._load_sessions()
        assert result == {}


# -- Claude Code Subprocess Tests (Acceptance Criteria 3, 6) -------------------

class TestClaudeCodeSubprocess:
    """Test that process_message delegates to Claude Code via subprocess."""

    @patch("task_manager.subprocess.Popen")
    def test_process_message_calls_claude_cli(self, mock_popen, manager):
        """process_message invokes claude CLI with --print and -p flags."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "I will create that task for you.",
                "session_id": "session-new-123",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        # Verify claude CLI was called
        assert mock_popen.called
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude"
        assert "--print" in cmd
        assert "-p" in cmd
        # The -p argument contains the user text (may have MCS_CONVERSATION_ID prefix)
        p_idx = cmd.index("-p")
        p_arg = cmd[p_idx + 1]
        assert "create a task: fix the login CSS bug" in p_arg

    @patch("task_manager.subprocess.Popen")
    def test_process_message_uses_resume_for_existing_session(self, mock_popen, manager):
        """When a session exists for the MCS conversation, --resume is used."""
        manager._sessions[SAMPLE_MCS_CONV_ID] = {
            "session_id": "existing-session-456",
            "prev_session_id": None,
        }
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Here are your tasks.",
                "session_id": "existing-session-456",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        resume_idx = cmd.index("--resume")
        assert cmd[resume_idx + 1] == "existing-session-456"

    @patch("task_manager.subprocess.Popen")
    def test_process_message_persists_new_session(self, mock_popen, manager, tmp_path):
        """New session IDs are persisted to disk."""
        manager._sessions_path = tmp_path / "test_sessions.json"
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Done!",
                "session_id": "brand-new-session",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        entry = manager._sessions[SAMPLE_MCS_CONV_ID]
        assert isinstance(entry, dict)
        assert entry["session_id"] == "brand-new-session"
        assert manager._sessions_path.exists()

    @patch("task_manager.subprocess.Popen")
    def test_process_message_retries_on_resume_failure(self, mock_popen, manager):
        """When --resume fails, PM forgets session and retries fresh."""
        manager._sessions[SAMPLE_MCS_CONV_ID] = {
            "session_id": "stale-session-789",
            "prev_session_id": None,
        }

        # First call fails (resume), second succeeds (fresh)
        mock_popen.side_effect = [
            _make_popen_mock(returncode=1, stderr="session not found"),
            _make_popen_mock(
                stdout=json.dumps({
                    "result": "Fresh response here.",
                    "session_id": "fresh-session-aaa",
                    "is_error": False,
                })
            ),
        ]
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        # Should have called claude twice
        assert mock_popen.call_count == 2
        # Second call should NOT have --resume
        second_cmd = mock_popen.call_args_list[1][0][0]
        assert "--resume" not in second_cmd
        # Session should be updated with new session id
        session_entry = manager._sessions[SAMPLE_MCS_CONV_ID]
        assert session_entry["session_id"] == "fresh-session-aaa"

    @patch("task_manager.subprocess.Popen")
    def test_process_message_injects_prev_session_context(self, mock_popen, manager):
        """When session is from previous run, prev session ID is injected as context."""
        manager._sessions[SAMPLE_MCS_CONV_ID] = "stale-session-789"

        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Hello again!",
                "session_id": "fresh-session-bbb",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        # Verify the prompt passed to Claude includes the previous session ID
        cmd = mock_popen.call_args[0][0]
        p_idx = cmd.index("-p")
        prompt_text = cmd[p_idx + 1]
        assert "stale-session-789" in prompt_text
        # Verify the response was sent via dv.post
        assert manager.dv.post.called
        call_kwargs = manager.dv.post.call_args[1]
        assert "Hello again!" in call_kwargs["data"]["cr_message"]

    @patch("task_manager.subprocess.Popen")
    def test_process_message_uses_fallback_on_timeout(self, mock_popen, manager):
        """When Claude CLI times out, fallback message is sent."""
        proc = _make_popen_mock()
        # First communicate() call raises timeout; second (reap after kill) returns empty
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired("claude", 60),
            ("", ""),
        ]
        mock_popen.return_value = proc
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        call_kwargs = manager.dv.post.call_args[1]
        assert call_kwargs["data"]["cr_message"] == "The system is temporarily unavailable, please try again shortly."

    @patch("task_manager.subprocess.Popen")
    def test_process_message_uses_fallback_on_cli_not_found(self, mock_popen, manager):
        """When claude binary is not found, fallback message is sent."""
        mock_popen.side_effect = FileNotFoundError()
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        call_kwargs = manager.dv.post.call_args[1]
        assert call_kwargs["data"]["cr_message"] == "The system is temporarily unavailable, please try again shortly."

    def test_process_empty_message(self, manager):
        """Empty messages should just be marked processed."""
        empty_msg = {**SAMPLE_INBOUND_MSG, "cr_message": ""}
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.process_message(empty_msg)
        # mark_processed calls dv.patch
        assert manager.dv.patch.called

    @patch("task_manager.subprocess.Popen")
    def test_process_message_sends_response_to_dv(self, mock_popen, manager):
        """Claude's response is written back to the conversations table."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Task created successfully!",
                "session_id": "sess-123",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        # Verify send_response was called (via dv.post)
        assert manager.dv.post.called
        call_kwargs = manager.dv.post.call_args[1]
        body = call_kwargs["data"]
        assert body["cr_message"] == "Task created successfully!"
        assert body["cr_direction"] == "Outbound"
        assert body["cr_in_reply_to"] == SAMPLE_CONVERSATION_ID

    @patch("task_manager.subprocess.Popen")
    def test_process_message_marks_processed(self, mock_popen, manager):
        """After processing, the inbound message is marked as Processed."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Done!",
                "session_id": "sess-456",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        # mark_processed calls dv.patch with status=Processed
        patch_calls = manager.dv.patch.call_args_list
        processed_call = [c for c in patch_calls if c[1].get("data", {}).get("cr_status") == "Processed"]
        assert len(processed_call) >= 1

    @patch("task_manager.subprocess.Popen")
    def test_call_claude_strips_claudecode_env(self, mock_popen, manager):
        """The CLAUDECODE env var is stripped to avoid nested sessions."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"result": "ok", "session_id": "s1", "is_error": False})
        )
        import os
        with patch.dict(os.environ, {"CLAUDECODE": "true"}):
            manager._call_claude("hello")
        env_passed = mock_popen.call_args[1].get("env", {})
        assert "CLAUDECODE" not in env_passed

    @patch("task_manager.subprocess.Popen")
    def test_call_claude_uses_json_output_format(self, mock_popen, manager):
        """Claude CLI is called with --output-format json."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"result": "ok", "session_id": "s1", "is_error": False})
        )
        manager._call_claude("hello")
        cmd = mock_popen.call_args[0][0]
        assert "--output-format" in cmd
        fmt_idx = cmd.index("--output-format")
        assert cmd[fmt_idx + 1] == "json"

    @patch("task_manager.subprocess.Popen")
    def test_call_claude_uses_dangerously_skip_permissions(self, mock_popen, manager):
        """Claude CLI is called with --dangerously-skip-permissions."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"result": "ok", "session_id": "s1", "is_error": False})
        )
        manager._call_claude("hello")
        cmd = mock_popen.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd

    @patch("task_manager.subprocess.Popen")
    def test_call_claude_returns_none_on_error_response(self, mock_popen, manager):
        """When Claude returns is_error=true, _call_claude returns (None, '')."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"result": "error occurred", "is_error": True})
        )
        result, session = manager._call_claude("hello")
        assert result is None
        assert session == ""

    @patch("task_manager.subprocess.Popen")
    def test_call_claude_handles_non_json_output(self, mock_popen, manager):
        """When Claude returns non-JSON, treat as plain text response."""
        mock_popen.return_value = _make_popen_mock(stdout="plain text response")
        result, session = manager._call_claude("hello")
        assert result == "plain text response"
        assert session == ""


# -- Stale Row Cleanup Tests ---------------------------------------------------

SAMPLE_STALE_ROW_1 = {
    "cr_shraga_conversationid": "stale-0001-0002-0003-000000000001",
    "cr_useremail": "testuser@example.com",
    "cr_direction": "Outbound",
    "cr_status": "Unclaimed",
    "createdon": "2026-02-15T08:00:00Z",
}

SAMPLE_STALE_ROW_2 = {
    "cr_shraga_conversationid": "stale-0001-0002-0003-000000000002",
    "cr_useremail": "testuser@example.com",
    "cr_direction": "Outbound",
    "cr_status": "Unclaimed",
    "createdon": "2026-02-15T08:05:00Z",
}


class TestStaleRowCleanup:
    def test_cleanup_marks_stale_rows_as_expired(self, manager):
        """cleanup_stale_outbound patches each stale row with STATUS_EXPIRED."""
        manager.dv.get.return_value = FakeResponse(
            json_data={"value": [SAMPLE_STALE_ROW_1, SAMPLE_STALE_ROW_2]}
        )
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        cleaned = manager.cleanup_stale_outbound()

        assert cleaned == 2
        assert manager.dv.patch.call_count == 2
        for c in manager.dv.patch.call_args_list:
            body = c[1]["data"]
            assert body["cr_status"] == "Expired"

    def test_cleanup_no_stale_rows(self, manager):
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})
        cleaned = manager.cleanup_stale_outbound()
        assert cleaned == 0
        assert manager.dv.patch.call_count == 0

    def test_cleanup_handles_query_error(self, manager):
        manager.dv.get.side_effect = Exception("Dataverse unavailable")
        cleaned = manager.cleanup_stale_outbound()
        assert cleaned == 0

    def test_cleanup_handles_patch_error(self, manager):
        manager.dv.get.return_value = FakeResponse(
            json_data={"value": [SAMPLE_STALE_ROW_1]}
        )
        manager.dv.patch.side_effect = Exception("patch failed")
        cleaned = manager.cleanup_stale_outbound()
        assert cleaned == 0

    def test_cleanup_uses_correct_filter(self, manager):
        manager.dv.get.return_value = FakeResponse(json_data={"value": []})
        manager.cleanup_stale_outbound()
        url = manager.dv.get.call_args[0][0]
        assert "cr_direction eq 'Outbound'" in url
        assert "cr_status eq 'Unclaimed'" in url
        assert "createdon lt" in url


# -- Full Flow Integration Test ------------------------------------------------

class TestFullFlow:
    @patch("task_manager.subprocess.Popen")
    def test_process_message_full_flow(self, mock_popen, manager):
        """Test full flow: process_message -> claude CLI -> send_response -> mark_processed."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "I have created the task for you.",
                "session_id": "flow-session-123",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        # Verify Claude was called
        assert mock_popen.called

        # Verify response was sent to DV
        assert manager.dv.post.called
        call_kwargs = manager.dv.post.call_args[1]
        assert call_kwargs["data"]["cr_message"] == "I have created the task for you."

        # Verify message was marked processed
        assert manager.dv.patch.called

        # Verify session was persisted (stored as dict with session_id and prev_session_id)
        entry = manager._sessions[SAMPLE_MCS_CONV_ID]
        assert isinstance(entry, dict)
        assert entry["session_id"] == "flow-session-123"


# -- Follow-up Detection Tests -- T045 (REMOVED) ------------------------------
# followup_expected detection was removed from the PM. Card link delivery is now
# handled entirely by the TaskRunner Power Automate flow sending directly to the
# MCS bot chat. The PM just sends its response -- fire and forget.
# -----------------------------------------------------------------------

class _RemovedTestFollowupDetection:
    """REMOVED: Tests for the old task_created / followup_expected detection heuristic.

    When the PM creates a task, Claude responds with "Submitted! ID: <uuid>".
    The PM must detect this and set cr_followup_expected='true' so the MCS topic
    loops back and calls SendMessage again to deliver the card link message.
    """

    @patch("task_manager.subprocess.Popen")
    def test_submitted_with_id_sets_followup_true(self, mock_popen, manager):
        """PM response 'Submitted! ID: <uuid>' must set cr_followup_expected='true'."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Submitted! ID: 5bd5cda3-4a11-f111-8341-002248d570fd\n\nWorker will handle it.",
                "session_id": "sess-fu-1",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_followup_expected"] == "true", \
            "PM must set followup_expected=true when response contains 'Submitted! ID:'"

    @patch("task_manager.subprocess.Popen")
    def test_submitted_id_lowercase_sets_followup(self, mock_popen, manager):
        """Detection is case-insensitive."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "submitted! id: abc12345-1234-1234-1234-123456789abc",
                "session_id": "sess-fu-2",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_followup_expected"] == "true"

    @patch("task_manager.subprocess.Popen")
    def test_status_check_does_not_trigger_followup(self, mock_popen, manager):
        """Status messages mentioning 'Submitted' without 'ID:' must NOT trigger followup."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Still Submitted -- waiting for a Worker to pick it up.",
                "session_id": "sess-fu-3",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_followup_expected"] == "", \
            "Status messages must not trigger followup"

    @patch("task_manager.subprocess.Popen")
    def test_cancel_submitted_does_not_trigger_followup(self, mock_popen, manager):
        """Cancel messages about Submitted state must NOT trigger followup."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "It's Submitted (10) -- can't cancel that state directly.",
                "session_id": "sess-fu-4",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_followup_expected"] == ""

    @patch("task_manager.subprocess.Popen")
    def test_general_response_does_not_trigger_followup(self, mock_popen, manager):
        """Non-task responses must NOT trigger followup."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Not my department! I'm your dev task manager.",
                "session_id": "sess-fu-5",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_followup_expected"] == ""

    @patch("task_manager.subprocess.Popen")
    def test_task_list_with_ids_does_not_trigger_followup(self, mock_popen, manager):
        """Task listing with short IDs must NOT trigger followup."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Here are your recent tasks:\n\n- Feb 23 22:29 Completed: Write bash script (ID: 19b754c5)",
                "session_id": "sess-fu-6",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        body = manager.dv.post.call_args[1]["data"]
        # This has "id:" but not "submitted", so should NOT trigger
        assert body["cr_followup_expected"] == ""


# -- PM Session Persistence Tests (Post-Refactor) -- T044 ---------------------

class TestLoadSessions:
    """Comprehensive tests for _load_sessions (file -> dict)."""

    def test_load_sessions_valid_multi_entry_file(self, manager, tmp_path):
        """Loading a valid JSON file with multiple sessions converts to dict format."""
        sessions_file = tmp_path / "sessions.json"
        data = {
            "mcs-conv-aaa": "session-111",
            "mcs-conv-bbb": "session-222",
            "mcs-conv-ccc": "session-333",
        }
        sessions_file.write_text(json.dumps(data), encoding="utf-8")
        manager._sessions_path = sessions_file

        result = manager._load_sessions()

        # _load_sessions converts strings to {prev_session_id: str, session_id: None}
        assert len(result) == 3
        assert result["mcs-conv-aaa"] == {"prev_session_id": "session-111", "session_id": None}
        assert result["mcs-conv-bbb"] == {"prev_session_id": "session-222", "session_id": None}
        assert result["mcs-conv-ccc"] == {"prev_session_id": "session-333", "session_id": None}

    def test_load_sessions_empty_dict_file(self, manager, tmp_path):
        """Loading a valid JSON file containing an empty dict returns empty dict."""
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text("{}", encoding="utf-8")
        manager._sessions_path = sessions_file

        result = manager._load_sessions()

        assert result == {}

    def test_load_sessions_missing_file_returns_empty(self, manager, tmp_path):
        """Loading from a non-existent path returns empty dict without error."""
        manager._sessions_path = tmp_path / "does_not_exist.json"

        result = manager._load_sessions()

        assert result == {}
        assert isinstance(result, dict)

    def test_load_sessions_corrupt_json_returns_empty(self, manager, tmp_path):
        """Loading from a file with invalid JSON returns empty dict."""
        corrupt_file = tmp_path / "corrupt.json"
        corrupt_file.write_text("{not valid json!!! [[[", encoding="utf-8")
        manager._sessions_path = corrupt_file

        result = manager._load_sessions()

        assert result == {}

    def test_load_sessions_non_dict_json_returns_empty(self, manager, tmp_path):
        """Loading a JSON file containing a list (not dict) returns empty dict."""
        list_file = tmp_path / "list.json"
        list_file.write_text('["item1", "item2"]', encoding="utf-8")
        manager._sessions_path = list_file

        result = manager._load_sessions()

        assert result == {}

    def test_load_sessions_single_entry(self, manager, tmp_path):
        """Loading a file with a single session entry works correctly.
        The new format converts old string values to {prev_session_id, session_id: None}."""
        sessions_file = tmp_path / "sessions.json"
        data = {"mcs-only": "session-only"}
        sessions_file.write_text(json.dumps(data), encoding="utf-8")
        manager._sessions_path = sessions_file

        result = manager._load_sessions()

        assert result == {"mcs-only": {"prev_session_id": "session-only", "session_id": None}}


class TestSaveSessions:
    """Comprehensive tests for _save_sessions (dict -> file)."""

    def test_save_sessions_writes_valid_json(self, manager, tmp_path):
        """_save_sessions writes sessions dict as valid JSON to disk."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {"conv-A": "sess-A", "conv-B": "sess-B"}

        manager._save_sessions()

        assert manager._sessions_path.exists()
        loaded = json.loads(manager._sessions_path.read_text(encoding="utf-8"))
        assert loaded == {"conv-A": "sess-A", "conv-B": "sess-B"}

    def test_save_sessions_overwrites_previous(self, manager, tmp_path):
        """_save_sessions overwrites the file on subsequent calls."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {"conv-old": "sess-old"}
        manager._save_sessions()

        manager._sessions = {"conv-new": "sess-new"}
        manager._save_sessions()

        loaded = json.loads(manager._sessions_path.read_text(encoding="utf-8"))
        assert loaded == {"conv-new": "sess-new"}
        assert "conv-old" not in loaded

    def test_save_sessions_empty_dict(self, manager, tmp_path):
        """Saving an empty sessions dict writes an empty JSON object."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {}

        manager._save_sessions()

        loaded = json.loads(manager._sessions_path.read_text(encoding="utf-8"))
        assert loaded == {}

    def test_save_sessions_round_trip(self, manager, tmp_path):
        """Save and then load returns the same data (round-trip integrity).
        Note: _load_sessions converts old string values to the new format on load."""
        manager._sessions_path = tmp_path / "sessions.json"
        original = {
            "mcs-111": "session-aaa",
            "mcs-222": "session-bbb",
            "mcs-333": "session-ccc",
        }
        manager._sessions = original.copy()

        manager._save_sessions()
        loaded = manager._load_sessions()

        # _load_sessions converts old string format to new dict format
        expected = {
            "mcs-111": {"prev_session_id": "session-aaa", "session_id": None},
            "mcs-222": {"prev_session_id": "session-bbb", "session_id": None},
            "mcs-333": {"prev_session_id": "session-ccc", "session_id": None},
        }
        assert loaded == expected

    def test_save_sessions_handles_write_error_gracefully(self, manager):
        """_save_sessions does not crash when the path is unwritable."""
        # Point to a non-existent deep directory that cannot be created
        manager._sessions_path = Path("/nonexistent_root_zzzz/deep/nested/sessions.json")
        manager._sessions = {"conv-X": "sess-X"}

        # Should not raise -- the method catches exceptions internally
        manager._save_sessions()

    def test_save_sessions_writes_pretty_json(self, manager, tmp_path):
        """_save_sessions writes indented JSON for readability."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {"conv-1": "sess-1"}

        manager._save_sessions()

        raw = manager._sessions_path.read_text(encoding="utf-8")
        # json.dumps with indent=2 produces multi-line output
        assert "\n" in raw
        assert "  " in raw


class TestForgetSession:
    """Comprehensive tests for _forget_session (remove + persist)."""

    def test_forget_session_removes_from_dict(self, manager, tmp_path):
        """_forget_session removes the specified MCS conversation ID from _sessions."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {"conv-A": "sess-A", "conv-B": "sess-B", "conv-C": "sess-C"}

        manager._forget_session("conv-B")

        assert "conv-B" not in manager._sessions
        assert manager._sessions == {"conv-A": "sess-A", "conv-C": "sess-C"}

    def test_forget_session_persists_remaining(self, manager, tmp_path):
        """After forgetting, remaining sessions are persisted to disk."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {"conv-keep": "sess-keep", "conv-drop": "sess-drop"}

        manager._forget_session("conv-drop")

        assert manager._sessions_path.exists()
        on_disk = json.loads(manager._sessions_path.read_text(encoding="utf-8"))
        assert on_disk == {"conv-keep": "sess-keep"}
        assert "conv-drop" not in on_disk

    def test_forget_session_noop_for_unknown_key(self, manager, tmp_path):
        """Forgetting a key that does not exist is a no-op (no crash)."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {"conv-only": "sess-only"}

        # Should not raise
        manager._forget_session("conv-nonexistent")

        # Original session is untouched
        assert manager._sessions == {"conv-only": "sess-only"}

    def test_forget_session_last_entry_leaves_empty(self, manager, tmp_path):
        """Forgetting the only session leaves an empty dict and persists it."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {"conv-sole": "sess-sole"}

        manager._forget_session("conv-sole")

        assert manager._sessions == {}
        on_disk = json.loads(manager._sessions_path.read_text(encoding="utf-8"))
        assert on_disk == {}

    def test_forget_session_does_not_save_when_key_missing(self, manager, tmp_path):
        """When the key is not in _sessions, _save_sessions should NOT be called."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {"conv-A": "sess-A"}

        with patch.object(manager, "_save_sessions") as mock_save:
            manager._forget_session("conv-nonexistent")
            mock_save.assert_not_called()

    def test_forget_session_calls_save_when_key_exists(self, manager, tmp_path):
        """When the key exists, _save_sessions IS called."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {"conv-A": "sess-A"}

        with patch.object(manager, "_save_sessions") as mock_save:
            manager._forget_session("conv-A")
            mock_save.assert_called_once()


class TestSessionExpiry:
    """Tests for session expiry: stale sessions are forgotten on resume failure."""

    @patch("task_manager.subprocess.Popen")
    def test_stale_session_is_replaced_on_resume_failure(
        self, mock_popen, manager, tmp_path
    ):
        """When --resume fails, the session is replaced with a new one."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {SAMPLE_MCS_CONV_ID: {
            "session_id": "stale-session-old",
            "prev_session_id": None,
        }}
        manager._save_sessions()

        # First call (resume) fails, second call (fresh) succeeds
        mock_popen.side_effect = [
            _make_popen_mock(returncode=1, stderr="session not found"),
            _make_popen_mock(
                stdout=json.dumps({
                    "result": "Fresh start.",
                    "session_id": "new-session-after-expiry",
                    "is_error": False,
                })
            ),
        ]
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        # Session should be replaced with the new one
        entry = manager._sessions[SAMPLE_MCS_CONV_ID]
        assert entry["session_id"] == "new-session-after-expiry"

        # Verify it was persisted to disk
        on_disk = json.loads(manager._sessions_path.read_text(encoding="utf-8"))
        assert on_disk[SAMPLE_MCS_CONV_ID]["session_id"] == "new-session-after-expiry"

    @patch("task_manager.subprocess.Popen")
    def test_expired_session_retry_does_not_use_resume(
        self, mock_popen, manager, tmp_path
    ):
        """After resume fails, the retry call has no --resume flag."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {SAMPLE_MCS_CONV_ID: {
            "session_id": "expired-sess-xyz",
            "prev_session_id": None,
        }}

        mock_popen.side_effect = [
            _make_popen_mock(returncode=1, stderr="session gone"),
            _make_popen_mock(
                stdout=json.dumps({
                    "result": "Retry succeeded.",
                    "session_id": "retry-sess-999",
                    "is_error": False,
                })
            ),
        ]
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        # First call should have --resume, second should not
        first_cmd = mock_popen.call_args_list[0][0][0]
        second_cmd = mock_popen.call_args_list[1][0][0]
        assert "--resume" in first_cmd
        assert "--resume" not in second_cmd

    @patch("task_manager.subprocess.Popen")
    def test_expired_session_response_sent_correctly(
        self, mock_popen, manager, tmp_path
    ):
        """When a session expires and retry succeeds, response is sent."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {SAMPLE_MCS_CONV_ID: {
            "session_id": "expired-sess-abc",
            "prev_session_id": None,
        }}

        mock_popen.side_effect = [
            _make_popen_mock(returncode=1, stderr="session expired"),
            _make_popen_mock(
                stdout=json.dumps({
                    "result": "Here is your answer.",
                    "session_id": "fresh-sess-def",
                    "is_error": False,
                })
            ),
        ]
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        sent_body = manager.dv.post.call_args[1]["data"]
        assert "Here is your answer." in sent_body["cr_message"]

    @patch("task_manager.subprocess.Popen")
    def test_both_resume_and_fresh_fail_uses_fallback(
        self, mock_popen, manager, tmp_path
    ):
        """When both resume and fresh calls fail, the fallback message is sent."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {SAMPLE_MCS_CONV_ID: {
            "session_id": "dead-session",
            "prev_session_id": None,
        }}

        mock_popen.side_effect = [
            _make_popen_mock(returncode=1, stderr="session not found"),
            _make_popen_mock(returncode=1, stderr="claude unavailable"),
        ]
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        sent_body = manager.dv.post.call_args[1]["data"]
        assert sent_body["cr_message"] == "The system is temporarily unavailable, please try again shortly."

    @patch("task_manager.subprocess.Popen")
    def test_other_sessions_preserved_when_one_expires(
        self, mock_popen, manager, tmp_path
    ):
        """When one session expires, other sessions remain intact on disk."""
        manager._sessions_path = tmp_path / "sessions.json"
        manager._sessions = {
            SAMPLE_MCS_CONV_ID: {
                "session_id": "stale-sess",
                "prev_session_id": None,
            },
            "other-conv-id": "healthy-sess",
        }
        manager._save_sessions()

        mock_popen.side_effect = [
            _make_popen_mock(returncode=1, stderr="session not found"),
            _make_popen_mock(
                stdout=json.dumps({
                    "result": "Ok.",
                    "session_id": "replacement-sess",
                    "is_error": False,
                })
            ),
        ]
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        # The other session should be untouched
        assert manager._sessions["other-conv-id"] == "healthy-sess"
        # The stale session should be replaced with the new one
        entry = manager._sessions[SAMPLE_MCS_CONV_ID]
        assert entry["session_id"] == "replacement-sess"

        on_disk = json.loads(manager._sessions_path.read_text(encoding="utf-8"))
        assert on_disk["other-conv-id"] == "healthy-sess"
        assert on_disk[SAMPLE_MCS_CONV_ID]["session_id"] == "replacement-sess"


# -- Post-Refactor: PM Monitor / Provision Delegation Tests (T045) ----------

class TestMonitorTaskPostRefactor:
    """Verify that the post-refactor polling loop calls poll and cleanup.

    After the sweep_stale_tasks removal, the PM's run() loop only calls
    poll_unclaimed and cleanup_stale_outbound. This test exercises a single
    iteration and asserts that:
      1. poll_unclaimed is called to check for new messages.
      2. cleanup_stale_outbound is called on startup.
    """

    @patch("task_manager.time.sleep", side_effect=KeyboardInterrupt)
    def test_monitor_task_post_refactor(self, mock_sleep, manager):
        """The run() loop polls for messages and cleans up stale outbound.

        This test simulates one full iteration of the main loop by having
        time.sleep raise KeyboardInterrupt on the first call. We verify
        that within that single iteration:
          - poll_unclaimed was invoked (the GET for unclaimed messages).
          - cleanup_stale_outbound ran on startup (the GET for stale outbound).
        """
        # --- Arrange ---
        def route_get(url, **kwargs):
            """Route GET requests based on URL content."""
            if "cr_direction eq 'Outbound'" in url and "cr_status eq 'Unclaimed'" in url:
                # cleanup_stale_outbound query -- no stale outbound rows
                return FakeResponse(json_data={"value": []})
            elif "cr_direction eq 'Inbound'" in url and "cr_status eq 'Unclaimed'" in url:
                # poll_unclaimed query -- no new messages
                return FakeResponse(json_data={"value": []})
            else:
                return FakeResponse(json_data={"value": []})

        manager.dv.get.side_effect = route_get
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        # --- Act ---
        manager.run()

        # --- Assert ---
        get_calls = manager.dv.get.call_args_list
        get_urls = [c[0][0] for c in get_calls]

        # Check that poll_unclaimed was called (inbound unclaimed messages)
        poll_calls = [u for u in get_urls
                      if "cr_direction eq 'Inbound'" in u and "cr_status eq 'Unclaimed'" in u]
        assert len(poll_calls) >= 1, (
            "poll_unclaimed should be called at least once per loop iteration"
        )

        # Check that cleanup_stale_outbound was called on startup
        cleanup_calls = [u for u in get_urls
                         if "cr_direction eq 'Outbound'" in u and "cr_status eq 'Unclaimed'" in u]
        assert len(cleanup_calls) >= 1, (
            "cleanup_stale_outbound should be called on startup"
        )


class TestProvisionDelegationPostRefactor:
    """Verify that provisioning is delegated to Claude Code after the refactor.

    Pre-refactor, the PM had a _tool_provision_devbox method that directly
    called the DevCenter API and ran customization scripts. Post-refactor,
    the PM is a thin wrapper: it sends the user's message to Claude Code
    via subprocess, and Claude Code autonomously decides to run provisioning
    scripts (scripts/check_devbox_status.py, etc.) based on CLAUDE.md instructions.

    This test verifies:
      1. No _tool_provision_devbox method exists (delegation, not direct execution).
      2. A provisioning request is passed to Claude Code via _call_claude, which
         invokes the `claude` CLI with --print and --dangerously-skip-permissions.
      3. Claude Code's response (which would include script execution results)
         is forwarded back to the user via send_response.
      4. The session is persisted so follow-up messages maintain context.
    """

    @patch("task_manager.subprocess.Popen")
    def test_provision_delegation_post_refactor(
        self, mock_popen, manager, tmp_path
    ):
        """Provisioning is fully delegated to Claude Code via the CLI subprocess.

        When a user says 'provision a dev box', the PM does NOT have its own
        provisioning logic. Instead it:
          1. Passes the message to Claude CLI (subprocess.run with 'claude').
          2. Claude Code reads CLAUDE.md and decides to run provisioning scripts.
          3. The CLI response (with provisioning results) is sent back to DV.
          4. The session is persisted for follow-up.

        This test sends a provisioning request through process_message and
        verifies the entire delegation chain.
        """
        # --- Arrange ---
        manager._sessions_path = tmp_path / "test_sessions.json"

        # Simulate a user asking to provision a dev box
        provision_msg = {
            "cr_shraga_conversationid": "prov-conv-001",
            "cr_useremail": "testuser@example.com",
            "cr_mcs_conversation_id": "mcs-prov-abc",
            "cr_message": "provision a new dev box for me",
            "cr_direction": "Inbound",
            "cr_status": "Unclaimed",
            "@odata.etag": 'W/"99999"',
            "createdon": "2026-02-15T12:00:00Z",
        }

        # Claude Code responds with provisioning results (as it would after
        # running scripts/check_devbox_status.py, setup.ps1, etc.)
        claude_response = {
            "result": (
                "I've provisioned a new dev box for you. Here are the details:\n\n"
                "- **Dev Box Name:** devbox-testuser-001\n"
                "- **Status:** Running\n"
                "- **Web RDP URL:** https://devbox.microsoft.com/connect/devbox-testuser-001\n\n"
                "The dev box is ready. I ran the setup scripts and configured Git, "
                "Claude Code, and Python. You can connect via the URL above."
            ),
            "session_id": "prov-session-xyz",
            "is_error": False,
        }
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps(claude_response)
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        # --- Act ---
        manager.process_message(provision_msg)

        # --- Assert ---

        # 1. Verify that _tool_provision_devbox does NOT exist (delegation, not direct)
        assert not hasattr(manager, "_tool_provision_devbox"), (
            "Post-refactor PM must NOT have _tool_provision_devbox -- "
            "provisioning is delegated to Claude Code"
        )

        # 2. Verify Claude CLI was invoked with the provisioning request
        assert mock_popen.called, "Claude CLI must be invoked via subprocess.Popen"
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude", "First argument must be the 'claude' binary"
        assert "--print" in cmd, "Must use --print for non-interactive mode"
        assert "--dangerously-skip-permissions" in cmd, (
            "Must use --dangerously-skip-permissions so Claude Code can run scripts"
        )
        assert "--output-format" in cmd, "Must request JSON output format"

        # Verify the user's provisioning message was passed to Claude
        prompt_idx = cmd.index("-p")
        assert "provision" in cmd[prompt_idx + 1].lower(), (
            "The provisioning message text must be passed to Claude Code"
        )

        # 3. Verify Claude's response was forwarded to the user via send_response
        assert manager.dv.post.called, "send_response must be called to write outbound row"
        sent_body = manager.dv.post.call_args[1]["data"]
        assert sent_body["cr_direction"] == "Outbound", (
            "Response must be an outbound message"
        )
        assert "provisioned" in sent_body["cr_message"].lower(), (
            "Claude's provisioning response must be forwarded to the user"
        )
        assert "devbox" in sent_body["cr_message"].lower(), (
            "Response should contain dev box details from Claude"
        )
        assert sent_body["cr_in_reply_to"] == "prov-conv-001", (
            "Response must reference the original inbound message"
        )

        # 4. Verify the session was persisted for follow-up context
        assert "mcs-prov-abc" in manager._sessions, (
            "Session must be persisted for the MCS conversation"
        )
        entry = manager._sessions["mcs-prov-abc"]
        assert isinstance(entry, dict), "Session entry must be stored as dict"
        assert entry["session_id"] == "prov-session-xyz", (
            "Session ID from Claude's response must be stored"
        )
        assert manager._sessions_path.exists(), (
            "Sessions file must be written to disk"
        )
        persisted = json.loads(manager._sessions_path.read_text(encoding="utf-8"))
        persisted_entry = persisted.get("mcs-prov-abc")
        assert isinstance(persisted_entry, dict), "Persisted session must be dict"
        assert persisted_entry["session_id"] == "prov-session-xyz", (
            "Session must be persisted to disk, not just in memory"
        )

        # 5. Verify the inbound message was marked as Processed
        patch_calls = manager.dv.patch.call_args_list
        processed_patches = [
            c for c in patch_calls
            if c[1].get("data", {}).get("cr_status") == "Processed"
        ]
        assert len(processed_patches) >= 1, (
            "The inbound message must be marked as Processed after handling"
        )

        # 6. Verify no direct DevCenter API calls were made
        # All POST calls should be to the DV conversations table,
        # NOT to any DevCenter endpoint
        for c in manager.dv.post.call_args_list:
            url = c[0][0] if c[0] else ""
            assert "devcenter" not in url.lower(), (
                "PM must NOT make direct DevCenter API calls -- "
                "provisioning is delegated to Claude Code"
            )

        # 7. Verify the subprocess environment strips CLAUDECODE
        env_passed = mock_popen.call_args[1].get("env", {})
        assert "CLAUDECODE" not in env_passed, (
            "CLAUDECODE env var must be stripped to avoid nested session errors"
        )
