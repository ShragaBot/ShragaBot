"""
Tests for Personal Task Manager -- Thin Wrapper Architecture + DV-based sessions.

The PM is now a thin wrapper around a persistent Claude Code session.
All tool dispatch code has been removed. Claude Code reads CLAUDE.md
and runs scripts directly. Session resolution uses Dataverse (cr_processed_by)
as the single source of truth -- no local JSON files. These tests verify:
  - DV polling, claiming, response writing (preserved)
  - Session resolution via resolve_session()
  - Stale outbound cleanup (preserved)
  - Claude Code subprocess delegation (preserved)
  - cr_claimed_by format: pm:version:box:instance_id (new)
  - cr_processed_by written on outbound rows (new)
  - [PM:xxxx] message prefix (new)
  - No tool dispatch code remains (preserved)
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
    """Create a TaskManager with mocked credentials."""
    monkeypatch.setenv("USER_EMAIL", "testuser@example.com")
    with patch("task_manager.create_credential", return_value=mock_credential):
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
    """Verify the wrapper stays under 400 lines."""

    def test_wrapper_under_400_lines(self):
        """task_manager.py must be under 400 lines."""
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
        """cr_claimed_by should use new format: pm:version:box:instance_id."""
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.claim_message(SAMPLE_INBOUND_MSG)
        call_kwargs = manager.dv.patch.call_args[1]
        body = call_kwargs["data"]
        assert body["cr_status"] == "Claimed"
        claimed_by = body["cr_claimed_by"]
        assert claimed_by.startswith("pm:")
        parts = claimed_by.split(":")
        assert len(parts) == 4  # pm:version:box:instance_id

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

    def test_claim_uses_etag(self, manager):
        """The ETag from the message must be sent via the etag kwarg to dv.patch."""
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.claim_message(SAMPLE_INBOUND_MSG)
        call_kwargs = manager.dv.patch.call_args[1]
        assert call_kwargs["etag"] == 'W/"12345"'


# -- Response Tests ------------------------------------------------------------

class TestResponse:
    def test_send_response_creates_outbound_row(self, manager):
        """Response without session_id has no prefix."""
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
        assert body["cr_message"] == "Task created!"  # No prefix without session_id
        assert body["cr_useremail"] == "testuser@example.com"

    def test_send_response_with_session_prefix(self, manager):
        """Response with session_id gets [PM:xxxx] prefix."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.send_response(
            in_reply_to=SAMPLE_CONVERSATION_ID,
            mcs_conversation_id=SAMPLE_MCS_CONV_ID,
            text="Task created!",
            session_id="a7f3c2d1-abcd-1234",
        )
        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_message"] == "[PM:a7f3] Task created!"

    def test_send_response_with_processed_by(self, manager):
        """cr_processed_by is written on outbound row when provided."""
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.send_response(
            in_reply_to="row-1",
            mcs_conversation_id="mcs-1",
            text="Hi",
            processed_by="pm:v19:some-session-id",
        )
        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_processed_by"] == "pm:v19:some-session-id"

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
        with patch("task_manager.create_credential", return_value=mock_credential):
            from task_manager import TaskManager
            with pytest.raises(ValueError, match="USER_EMAIL"):
                TaskManager("")

    def test_sets_user_email(self, manager):
        assert manager.user_email == "testuser@example.com"

    def test_has_last_session_id(self, manager):
        """Manager must have _last_session_id for processed_by tracking."""
        assert hasattr(manager, "_last_session_id")
        assert isinstance(manager._last_session_id, str)

    def test_has_agent_role(self):
        """Module should have AGENT_ROLE constant."""
        from task_manager import AGENT_ROLE
        assert AGENT_ROLE == "PM"

    def test_has_version(self, manager):
        """Manager should have _my_version attribute."""
        assert hasattr(manager, "_my_version")
        assert isinstance(manager._my_version, str)

    def test_has_box_name(self, manager):
        """Manager should have _box_name attribute."""
        assert hasattr(manager, "_box_name")
        assert isinstance(manager._box_name, str)

    def test_no_old_session_attributes(self, manager):
        """Old session attributes must not exist."""
        assert not hasattr(manager, "_sessions")
        assert not hasattr(manager, "_sessions_path")
        assert not hasattr(manager, "manager_id")

    def test_no_old_session_methods(self, manager):
        """Old session methods must not exist."""
        assert not hasattr(manager, "_load_sessions")
        assert not hasattr(manager, "_save_sessions")
        assert not hasattr(manager, "_forget_session")


# -- Module Constants Tests ---------------------------------------------------

class TestModuleConstants:
    """Verify module-level constants are correct."""

    def test_fallback_message(self):
        from task_manager import FALLBACK_MESSAGE
        assert FALLBACK_MESSAGE == "The system is temporarily unavailable, please try again shortly."

    def test_no_sessions_file_constant(self):
        """SESSIONS_FILE should be removed (no local session files)."""
        import task_manager
        assert not hasattr(task_manager, "SESSIONS_FILE")

    def test_instance_id_constant(self):
        """INSTANCE_ID should exist as an 8-char hex string."""
        from task_manager import INSTANCE_ID
        assert isinstance(INSTANCE_ID, str)
        assert len(INSTANCE_ID) == 8

    def test_agent_role_constant(self):
        from task_manager import AGENT_ROLE
        assert AGENT_ROLE == "PM"


# -- Session Resolution Tests (replaces old TestSessionPersistence) -----------

class TestSessionResolution:
    """Verify session resolution via resolve_session()."""

    @patch("task_manager.subprocess.Popen")
    @patch("task_manager.resolve_session")
    def test_message_always_calls_resolve_session(self, mock_resolve, mock_popen, manager):
        """Every message should call resolve_session for correct cross-agent context."""
        mock_resolve.return_value = (None, "", None)  # New session, no context
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Hello!",
                "session_id": "new-sess-123",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        mock_resolve.assert_called_once()
        assert manager._last_session_id == "new-sess-123"

    @patch("task_manager.subprocess.Popen")
    @patch("task_manager.resolve_session")
    def test_resolve_session_resume_with_context(self, mock_resolve, mock_popen, manager):
        """When resolve_session returns a session ID, it is used for resume."""
        mock_resolve.return_value = ("prev-sess-xyz", "", None)
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Resumed!",
                "session_id": "prev-sess-xyz",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        resume_idx = cmd.index("--resume")
        assert cmd[resume_idx + 1] == "prev-sess-xyz"

    @patch("task_manager.subprocess.Popen")
    @patch("task_manager.resolve_session")
    def test_resolve_session_new_with_context_prefix(self, mock_resolve, mock_popen, manager):
        """When resolve_session returns context prefix but no session, context is prepended."""
        context = "[Previous conversation context...]\n\n"
        mock_resolve.return_value = (None, context, None)
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Got it!",
                "session_id": "brand-new-sess",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        cmd = mock_popen.call_args[0][0]
        # --resume should NOT be present (new session)
        assert "--resume" not in cmd
        # The context prefix should be prepended to the prompt text
        p_idx = cmd.index("-p")
        prompt = cmd[p_idx + 1]
        assert "[Previous conversation context" in prompt


# -- Claude Code Subprocess Tests (Acceptance Criteria 3, 6) -------------------

class TestClaudeCodeSubprocess:
    """Test that process_message delegates to Claude Code via subprocess."""

    @patch("task_manager.subprocess.Popen")
    @patch("task_manager.resolve_session", return_value=(None, "", None))
    def test_process_message_calls_claude_cli(self, mock_resolve, mock_popen, manager):
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
    @patch("task_manager.resolve_session", return_value=(None, "", None))
    def test_process_message_uses_fallback_on_timeout(self, mock_resolve, mock_popen, manager):
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
        assert "temporarily unavailable" in call_kwargs["data"]["cr_message"]

    @patch("task_manager.subprocess.Popen")
    @patch("task_manager.resolve_session", return_value=(None, "", None))
    def test_process_message_uses_fallback_on_cli_not_found(self, mock_resolve, mock_popen, manager):
        """When claude binary is not found, fallback message is sent."""
        mock_popen.side_effect = FileNotFoundError()
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        call_kwargs = manager.dv.post.call_args[1]
        assert "temporarily unavailable" in call_kwargs["data"]["cr_message"]

    def test_process_empty_message(self, manager):
        """Empty messages should just be marked processed."""
        empty_msg = {**SAMPLE_INBOUND_MSG, "cr_message": ""}
        manager.dv.patch.return_value = FakeResponse(status_code=204)
        manager.process_message(empty_msg)
        # mark_processed calls dv.patch
        assert manager.dv.patch.called

    @patch("task_manager.subprocess.Popen")
    @patch("task_manager.resolve_session", return_value=(None, "", None))
    def test_process_message_sends_response_to_dv(self, mock_resolve, mock_popen, manager):
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
        # Message will have [PM:sess] prefix because session_id is "sess-123"
        assert "Task created successfully!" in body["cr_message"]
        assert body["cr_direction"] == "Outbound"
        assert body["cr_in_reply_to"] == SAMPLE_CONVERSATION_ID

    @patch("task_manager.subprocess.Popen")
    @patch("task_manager.resolve_session", return_value=(None, "", None))
    def test_process_message_marks_processed(self, mock_resolve, mock_popen, manager):
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
    @patch("task_manager.resolve_session", return_value=(None, "", None))
    def test_process_message_full_flow(self, mock_resolve, mock_popen, manager):
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

        # Verify response was sent to DV (may have [PM:flow] prefix)
        assert manager.dv.post.called
        call_kwargs = manager.dv.post.call_args[1]
        assert "I have created the task for you." in call_kwargs["data"]["cr_message"]

        # Verify message was marked processed
        assert manager.dv.patch.called

        # Verify session was tracked
        assert manager._last_session_id == "flow-session-123"

    @patch("task_manager.subprocess.Popen")
    @patch("task_manager.resolve_session", return_value=(None, "", None))
    def test_processed_by_written_on_outbound(self, mock_resolve, mock_popen, manager):
        """cr_processed_by should be written on the outbound response row."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Response",
                "session_id": "sess-abc123",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert "cr_processed_by" in body
        assert body["cr_processed_by"].startswith("pm:")
        assert "sess-abc123" in body["cr_processed_by"]

    @patch("task_manager.subprocess.Popen")
    @patch("task_manager.resolve_session", return_value=(None, "", None))
    def test_pm_prefix_on_outbound(self, mock_resolve, mock_popen, manager):
        """Outbound message should have [PM:xxxx] prefix."""
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({
                "result": "Hello!",
                "session_id": "sess-a7f3c2d1",
                "is_error": False,
            })
        )
        manager.dv.post.return_value = FakeResponse(json_data={})
        manager.dv.patch.return_value = FakeResponse(status_code=204)

        manager.process_message(SAMPLE_INBOUND_MSG)

        body = manager.dv.post.call_args[1]["data"]
        assert body["cr_message"].startswith("[PM:sess]")


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
      4. The session is tracked for follow-up context.
    """

    @patch("task_manager.subprocess.Popen")
    @patch("task_manager.resolve_session", return_value=(None, "", None))
    def test_provision_delegation_post_refactor(
        self, mock_resolve, mock_popen, manager
    ):
        """Provisioning is fully delegated to Claude Code via the CLI subprocess.

        When a user says 'provision a dev box', the PM does NOT have its own
        provisioning logic. Instead it:
          1. Passes the message to Claude CLI (subprocess.run with 'claude').
          2. Claude Code reads CLAUDE.md and decides to run provisioning scripts.
          3. The CLI response (with provisioning results) is sent back to DV.
          4. The session is tracked for follow-up.

        This test sends a provisioning request through process_message and
        verifies the entire delegation chain.
        """
        # --- Arrange ---

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

        # 4. Verify the session was tracked for follow-up
        assert manager._last_session_id == "prov-session-xyz", (
            "Session ID from Claude's response must be stored"
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
