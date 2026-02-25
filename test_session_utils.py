"""
Tests for session_utils -- DV-based session resolution.

Tests verify the decision matrix:
  - Same version + same role -> resume
  - Same version + different role -> new session + cross-agent context
  - Different version + same role -> new session + context
  - Different version + different role -> new session + context
  - No previous messages -> new session, no context
  - Resume but session file missing -> fallback to new session
  - Message prefix format
  - Malformed cr_processed_by handling
  - DV query failure handling
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from conftest import FakeResponse
from session_utils import resolve_session, _format_conversation_history, _find_session_file


# -- Test data helpers --------------------------------------------------------

def _make_outbound_row(
    processed_by: str = "",
    message: str = "Hello from bot",
    createdon: str = "2026-02-25T10:00:00Z",
    direction: str = "Outbound",
    status: str = "Processed",
    mcs_id: str = "mcs-conv-123",
):
    """Create a fake DV conversation row."""
    return {
        "cr_shraga_conversationid": "row-out-001",
        "cr_useremail": "user@test.com",
        "cr_mcs_conversation_id": mcs_id,
        "cr_message": message,
        "cr_direction": direction,
        "cr_status": status,
        "cr_processed_by": processed_by,
        "createdon": createdon,
    }


def _make_inbound_row(
    message: str = "Hello from user",
    createdon: str = "2026-02-25T09:59:00Z",
    mcs_id: str = "mcs-conv-123",
):
    return {
        "cr_shraga_conversationid": "row-in-001",
        "cr_useremail": "user@test.com",
        "cr_mcs_conversation_id": mcs_id,
        "cr_message": message,
        "cr_direction": "Inbound",
        "cr_status": "Processed",
        "cr_processed_by": None,
        "createdon": createdon,
    }


def _make_dv_mock(rows: list[dict]):
    """Create a mock dv client whose .get() returns the given rows."""
    dv = MagicMock()
    dv.api_base = "https://test-org.crm.dynamics.com/api/data/v9.2"
    dv.get.return_value = FakeResponse(json_data={"value": rows})
    return dv


# =============================================================================
# Decision Matrix Tests
# =============================================================================

class TestSameVersionSameRole:
    """Same version + same role -> resume prev_session_id."""

    @patch("session_utils._find_session_file")
    def test_resume_when_session_file_exists(self, mock_find):
        """Should resume the previous session when file exists on disk."""
        mock_find.return_value = "/home/user/.claude/projects/abc/sess-123.jsonl"
        rows = [
            _make_outbound_row(processed_by="pm:v19:sess-123-full-uuid", createdon="2026-02-25T10:01:00Z"),
            _make_inbound_row(createdon="2026-02-25T10:00:00Z"),
        ]
        dv = _make_dv_mock(rows)

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        assert session_id == "sess-123-full-uuid"
        assert context == ""
        assert prev_path == "/home/user/.claude/projects/abc/sess-123.jsonl"

    @patch("session_utils._find_session_file")
    def test_fallback_when_session_file_missing(self, mock_find):
        """Should start new session with context when file is missing."""
        mock_find.return_value = None
        rows = [
            _make_outbound_row(processed_by="pm:v19:sess-gone-uuid", createdon="2026-02-25T10:01:00Z"),
            _make_inbound_row(message="Hi there", createdon="2026-02-25T10:00:00Z"),
        ]
        dv = _make_dv_mock(rows)

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        assert session_id is None  # New session
        assert "previous session could not be resumed" in context.lower() or "previous conversation context" in context.lower()
        assert prev_path is None  # File doesn't exist


class TestSameVersionDifferentRole:
    """Same version + different role -> new session + cross-agent context."""

    @patch("session_utils._find_session_file")
    def test_cross_agent_handoff_gm_to_pm(self, mock_find):
        """PM picking up after GM should get cross-agent context."""
        mock_find.return_value = None
        rows = [
            _make_outbound_row(processed_by="gm:v19:gm-session-id", createdon="2026-02-25T10:05:00Z", message="GM response"),
            _make_inbound_row(message="User question to GM", createdon="2026-02-25T10:04:00Z"),
            _make_outbound_row(processed_by="gm:v19:gm-session-id", createdon="2026-02-25T10:03:00Z", message="GM first response"),
            _make_inbound_row(message="First user msg", createdon="2026-02-25T10:02:00Z"),
        ]
        dv = _make_dv_mock(rows)

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        assert session_id is None  # New session
        assert "handled by the gm" in context.lower()
        assert "you are the pm" in context.lower()
        assert "picking up this conversation" in context.lower()

    @patch("session_utils._find_session_file")
    def test_cross_agent_handoff_pm_to_gm(self, mock_find):
        """GM picking up after PM should get cross-agent context."""
        mock_find.return_value = None
        rows = [
            _make_outbound_row(processed_by="pm:v19:pm-session-id", createdon="2026-02-25T10:05:00Z", message="PM response"),
            _make_inbound_row(message="User msg", createdon="2026-02-25T10:04:00Z"),
        ]
        dv = _make_dv_mock(rows)

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="gm"
        )

        assert session_id is None
        assert "handled by the pm" in context.lower()
        assert "you are the gm" in context.lower()


class TestDifferentVersion:
    """Different version -> new session + inject context."""

    @patch("session_utils._find_session_file")
    def test_version_upgrade_same_role(self, mock_find):
        """Version change with same role should inject history context."""
        mock_find.return_value = None
        rows = [
            _make_outbound_row(processed_by="pm:v18:old-session-id", createdon="2026-02-25T10:01:00Z", message="Old PM response"),
            _make_inbound_row(message="User message", createdon="2026-02-25T10:00:00Z"),
        ]
        dv = _make_dv_mock(rows)

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        assert session_id is None  # New session
        assert "version update" in context.lower()
        assert "recent conversation context" in context.lower()

    @patch("session_utils._find_session_file")
    def test_version_upgrade_different_role(self, mock_find):
        """Version change with different role should also inject context."""
        mock_find.return_value = None
        rows = [
            _make_outbound_row(processed_by="gm:v18:old-gm-session", createdon="2026-02-25T10:01:00Z", message="Old GM response"),
            _make_inbound_row(message="User msg", createdon="2026-02-25T10:00:00Z"),
        ]
        dv = _make_dv_mock(rows)

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        assert session_id is None
        assert "version update" in context.lower()

    @patch("session_utils._find_session_file")
    def test_version_upgrade_includes_prev_session_path(self, mock_find):
        """If prev session file exists on disk, include its path in context."""
        mock_find.return_value = "/home/user/.claude/projects/enc/old-session.jsonl"
        rows = [
            _make_outbound_row(processed_by="pm:v18:old-session", createdon="2026-02-25T10:01:00Z"),
            _make_inbound_row(createdon="2026-02-25T10:00:00Z"),
        ]
        dv = _make_dv_mock(rows)

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        assert session_id is None
        assert prev_path == "/home/user/.claude/projects/enc/old-session.jsonl"
        assert "previous session transcript" in context.lower()


class TestNoPreviousMessages:
    """No previous messages -> new session, no context."""

    def test_empty_history(self):
        """No rows in DV -> new session with empty context."""
        dv = _make_dv_mock([])

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-brand-new", my_version="v19", my_role="pm"
        )

        assert session_id is None
        assert context == ""
        assert prev_path is None

    def test_only_inbound_messages(self):
        """Only inbound messages (no outbound) -> new session, no context."""
        rows = [
            _make_inbound_row(message="Hello", createdon="2026-02-25T10:00:00Z"),
        ]
        dv = _make_dv_mock(rows)

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        assert session_id is None
        assert context == ""
        assert prev_path is None


class TestMalformedData:
    """Handle malformed cr_processed_by gracefully."""

    def test_empty_processed_by(self):
        """Empty cr_processed_by -> new session with context."""
        rows = [
            _make_outbound_row(processed_by="", createdon="2026-02-25T10:01:00Z"),
            _make_inbound_row(createdon="2026-02-25T10:00:00Z"),
        ]
        dv = _make_dv_mock(rows)

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        assert session_id is None
        # Should have some context from the conversation
        assert "fresh session" in context.lower() or context == "" or "context" in context.lower()

    def test_malformed_processed_by_no_colons(self):
        """Malformed cr_processed_by (no colons) -> new session."""
        rows = [
            _make_outbound_row(processed_by="justgarbage", createdon="2026-02-25T10:01:00Z"),
            _make_inbound_row(createdon="2026-02-25T10:00:00Z"),
        ]
        dv = _make_dv_mock(rows)

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        assert session_id is None

    def test_malformed_processed_by_partial(self):
        """Malformed cr_processed_by (only 2 parts) -> new session."""
        rows = [
            _make_outbound_row(processed_by="pm:v19", createdon="2026-02-25T10:01:00Z"),
            _make_inbound_row(createdon="2026-02-25T10:00:00Z"),
        ]
        dv = _make_dv_mock(rows)

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        assert session_id is None


class TestDvFailure:
    """Handle DV query failures gracefully."""

    def test_dv_get_raises_exception(self):
        """When DV query fails, return new session with no context."""
        dv = MagicMock()
        dv.api_base = "https://test-org.crm.dynamics.com/api/data/v9.2"
        dv.get.side_effect = Exception("Network error")

        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        assert session_id is None
        assert context == ""
        assert prev_path is None


class TestContextFormatting:
    """Test conversation history formatting."""

    def test_format_conversation_history_basic(self):
        """Basic formatting of inbound/outbound messages."""
        rows = [
            _make_inbound_row(message="Hello", createdon="2026-02-25T10:00:00Z"),
            _make_outbound_row(message="Hi there!", createdon="2026-02-25T10:01:00Z", processed_by="pm:v19:sess-1"),
        ]
        result = _format_conversation_history(rows)
        assert "User: Hello" in result
        assert "PM: Hi there!" in result

    def test_format_conversation_history_empty(self):
        """Empty rows -> empty string."""
        assert _format_conversation_history([]) == ""

    def test_format_conversation_history_truncates_messages(self):
        """Messages are truncated to 2000 chars."""
        rows = [
            _make_inbound_row(message="A" * 5000, createdon="2026-02-25T10:00:00Z"),
        ]
        result = _format_conversation_history(rows)
        # Message should be truncated to 2000 chars
        assert len(result.split(": ", 1)[1]) <= 2000


class TestRoleCaseInsensitivity:
    """Role comparison should be case-insensitive."""

    @patch("session_utils._find_session_file")
    def test_uppercase_role_matches(self, mock_find):
        """PM role in processed_by should match 'pm' in my_role."""
        mock_find.return_value = "/path/to/session.jsonl"
        rows = [
            _make_outbound_row(processed_by="PM:v19:sess-upper", createdon="2026-02-25T10:01:00Z"),
            _make_inbound_row(createdon="2026-02-25T10:00:00Z"),
        ]
        dv = _make_dv_mock(rows)

        # my_role is lowercase "pm", processed_by has uppercase "PM"
        session_id, context, prev_path = resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm"
        )

        # Should still resume (case-insensitive match)
        assert session_id == "sess-upper"


class TestMessagePrefix:
    """Verify message prefix format [ROLE:xxxx]."""

    def test_pm_prefix_format(self):
        """PM prefix should be [PM:xxxx] where xxxx is first 4 chars of session ID."""
        session_id = "a7f3c2d1-8b2e-4f1a-9c3d-5e6f7a8b9c0d"
        role = "PM"
        session_short = session_id[:4]
        prefix = f"[{role}:{session_short}]"
        assert prefix == "[PM:a7f3]"

    def test_gm_prefix_format(self):
        """GM prefix should be [GM:xxxx]."""
        session_id = "b2e1c9d4-1234-5678-abcd-ef0123456789"
        role = "GM"
        session_short = session_id[:4]
        prefix = f"[{role}:{session_short}]"
        assert prefix == "[GM:b2e1]"


class TestProcessedByFormat:
    """Verify cr_processed_by and cr_claimed_by format."""

    def test_processed_by_format(self):
        """cr_processed_by should be role:version:session_id."""
        role = "pm"
        version = "v19"
        session_id = "a7f3c2d1-8b2e-4f1a-9c3d-5e6f7a8b9c0d"
        processed_by = f"{role}:{version}:{session_id}"
        assert processed_by == "pm:v19:a7f3c2d1-8b2e-4f1a-9c3d-5e6f7a8b9c0d"

    def test_claimed_by_format(self):
        """cr_claimed_by should be role:version:box:instance_id."""
        role = "pm"
        version = "v19"
        box = "CPC-sagik-HC8YC"
        instance_id = "1439be25"
        claimed_by = f"{role}:{version}:{box}:{instance_id}"
        assert claimed_by == "pm:v19:CPC-sagik-HC8YC:1439be25"


class TestFindSessionFile:
    """Test _find_session_file."""

    def test_returns_none_when_claude_dir_missing(self, tmp_path):
        """Returns None when ~/.claude/projects doesn't exist."""
        with patch("session_utils.Path.home", return_value=tmp_path):
            result = _find_session_file("nonexistent-session-id")
            assert result is None

    def test_finds_existing_session_file(self, tmp_path):
        """Finds a session JSONL file in the projects directory."""
        projects_dir = tmp_path / ".claude" / "projects" / "encoded-cwd"
        projects_dir.mkdir(parents=True)
        session_file = projects_dir / "test-session-id.jsonl"
        session_file.write_text('{"test": true}')

        with patch("session_utils.Path.home", return_value=tmp_path):
            result = _find_session_file("test-session-id")
            assert result is not None
            assert "test-session-id.jsonl" in result

    def test_returns_none_for_nonexistent_session(self, tmp_path):
        """Returns None when session ID doesn't match any file."""
        projects_dir = tmp_path / ".claude" / "projects" / "encoded-cwd"
        projects_dir.mkdir(parents=True)

        with patch("session_utils.Path.home", return_value=tmp_path):
            result = _find_session_file("nonexistent-session-id")
            assert result is None


class TestLogCallback:
    """Test that log_fn is called appropriately."""

    def test_logs_on_no_previous_messages(self):
        """Log callback should be called when no previous messages found."""
        log_messages = []
        dv = _make_dv_mock([])

        resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm",
            log_fn=lambda msg: log_messages.append(msg)
        )

        assert any("no previous messages" in m.lower() for m in log_messages)

    @patch("session_utils._find_session_file")
    def test_logs_on_resume(self, mock_find):
        """Log callback should mention resume."""
        mock_find.return_value = "/path/to/file.jsonl"
        log_messages = []
        rows = [
            _make_outbound_row(processed_by="pm:v19:sess-resume", createdon="2026-02-25T10:01:00Z"),
            _make_inbound_row(createdon="2026-02-25T10:00:00Z"),
        ]
        dv = _make_dv_mock(rows)

        resolve_session(
            dv, "mcs-conv-123", my_version="v19", my_role="pm",
            log_fn=lambda msg: log_messages.append(msg)
        )

        assert any("resume" in m.lower() for m in log_messages)
