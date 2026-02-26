"""
Tests for standalone CLI scripts.

Covers:
  - scripts/get_user_state.py (T028)
  - scripts/send_message.py (T032)
  - scripts/update_user_state.py (T029)

All external dependencies (Azure CLI, Dataverse HTTP calls) are mocked.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import requests as req

# Ensure the repo root and scripts directory are importable
REPO_ROOT = Path(__file__).parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import get_user_state as gus  # noqa: E402

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

SAMPLE_DV_ROW = {
    "crb3b_shragauserid": "row-abc-123",
    "crb3b_useremail": "alice@example.com",
    "crb3b_onboardingstep": "auth_pending",
    "crb3b_devboxname": "shraga-alice",
    "crb3b_devboxstatus": "Running",
    "crb3b_azureadid": "aad-alice-guid",
    "crb3b_connectionurl": "https://devbox.microsoft.com/connect?devbox=shraga-alice",
    "crb3b_authurl": None,
    "crb3b_claudeauthstatus": "Pending",
    "crb3b_managerstatus": "",
    "crb3b_lastseen": "2026-02-20T10:00:00Z",
}

FAKE_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.fake-token-for-testing"


# ---------------------------------------------------------------------------
# Helper: mock the Azure CLI token acquisition
# ---------------------------------------------------------------------------

def _mock_az_token_success(monkeypatch):
    """Patch get_access_token so it returns FAKE_TOKEN without shelling out."""
    monkeypatch.setattr(gus, "get_access_token", lambda resource_url=None: FAKE_TOKEN)


# ---------------------------------------------------------------------------
# FakeResponse -- import from conftest for shared use
# ---------------------------------------------------------------------------

from conftest import FakeResponse


# ===========================================================================
# get_access_token unit tests
# ===========================================================================

class TestGetAccessToken:
    """Unit tests for the Azure CLI token acquisition wrapper."""

    @patch("get_user_state.subprocess.run")
    def test_returns_token_on_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=FAKE_TOKEN + "\n", stderr=""
        )
        token = gus.get_access_token("https://example.crm.dynamics.com")
        assert token == FAKE_TOKEN

    @patch("get_user_state.subprocess.run")
    def test_raises_on_az_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="AADSTS700024: some error"
        )
        with pytest.raises(RuntimeError, match="az account get-access-token failed"):
            gus.get_access_token()

    @patch("get_user_state.subprocess.run", side_effect=FileNotFoundError)
    def test_raises_when_az_not_installed(self, _mock_run):
        with pytest.raises(RuntimeError, match="Azure CLI.*not found"):
            gus.get_access_token()

    @patch("get_user_state.subprocess.run")
    def test_raises_on_empty_token(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="  \n", stderr=""
        )
        with pytest.raises(RuntimeError, match="empty token"):
            gus.get_access_token()

    @patch("get_user_state.subprocess.run")
    def test_raises_on_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="az", timeout=30)
        with pytest.raises(RuntimeError, match="timed out"):
            gus.get_access_token()


# ===========================================================================
# get_user_state function unit tests
# ===========================================================================

class TestGetUserStateFunction:
    """Unit tests for the get_user_state() Dataverse query function."""

    @patch("get_user_state.requests.get")
    def test_returns_row_when_found(self, mock_get):
        mock_get.return_value = FakeResponse(
            json_data={"value": [SAMPLE_DV_ROW]}
        )
        result = gus.get_user_state("alice@example.com", FAKE_TOKEN)
        assert result is not None
        assert result["crb3b_useremail"] == "alice@example.com"
        assert result["crb3b_onboardingstep"] == "auth_pending"

    @patch("get_user_state.requests.get")
    def test_returns_none_when_not_found(self, mock_get):
        mock_get.return_value = FakeResponse(json_data={"value": []})
        result = gus.get_user_state("nobody@example.com", FAKE_TOKEN)
        assert result is None

    @patch("get_user_state.requests.get")
    def test_builds_correct_url(self, mock_get):
        mock_get.return_value = FakeResponse(json_data={"value": []})
        gus.get_user_state("alice@example.com", FAKE_TOKEN)
        url = mock_get.call_args[0][0]
        assert "crb3b_useremail eq 'alice@example.com'" in url
        assert "$top=1" in url

    @patch("get_user_state.requests.get")
    def test_sends_correct_headers(self, mock_get):
        mock_get.return_value = FakeResponse(json_data={"value": []})
        gus.get_user_state("alice@example.com", FAKE_TOKEN)
        headers = mock_get.call_args[1].get("headers") or mock_get.call_args[0][1] if len(mock_get.call_args[0]) > 1 else mock_get.call_args[1]["headers"]
        assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert headers["OData-Version"] == "4.0"

    @patch("get_user_state.requests.get")
    def test_raises_on_http_error(self, mock_get):
        mock_get.return_value = FakeResponse(status_code=401, text="Unauthorized")
        with pytest.raises(req.exceptions.HTTPError):
            gus.get_user_state("alice@example.com", FAKE_TOKEN)

    @patch("get_user_state.requests.get")
    def test_raises_on_timeout(self, mock_get):
        mock_get.side_effect = req.exceptions.Timeout("timed out")
        with pytest.raises(req.exceptions.Timeout):
            gus.get_user_state("alice@example.com", FAKE_TOKEN)


# ===========================================================================
# format_user_state unit tests
# ===========================================================================

class TestFormatUserState:
    """Unit tests for the output formatting function."""

    def test_basic_formatting(self):
        result = gus.format_user_state(SAMPLE_DV_ROW)
        assert result["found"] is True
        assert result["user_email"] == "alice@example.com"
        assert result["user_id"] == "row-abc-123"
        assert result["onboarding_step"] == "auth_pending"
        assert result["devbox_name"] == "shraga-alice"
        assert result["azure_ad_id"] == "aad-alice-guid"
        assert result["connection_url"] == "https://devbox.microsoft.com/connect?devbox=shraga-alice"

    def test_provisioning_flags_auth_pending(self):
        result = gus.format_user_state(SAMPLE_DV_ROW)
        assert result["provisioning_started"] is True
        assert result["provisioning_complete"] is True
        assert result["auth_complete"] is False

    def test_provisioning_flags_completed(self):
        row = {**SAMPLE_DV_ROW, "crb3b_onboardingstep": "completed"}
        result = gus.format_user_state(row)
        assert result["provisioning_started"] is True
        assert result["provisioning_complete"] is True
        assert result["auth_complete"] is True

    def test_provisioning_flags_new_user(self):
        row = {**SAMPLE_DV_ROW, "crb3b_onboardingstep": "awaiting_setup"}
        result = gus.format_user_state(row)
        assert result["provisioning_started"] is False
        assert result["provisioning_complete"] is False
        assert result["auth_complete"] is False

    def test_missing_optional_columns(self):
        """When connection_url / auth_url columns are absent, defaults to None."""
        minimal_row = {
            "crb3b_shragauserid": "row-min-001",
            "crb3b_useremail": "minimal@example.com",
            "crb3b_onboardingstep": "provisioning",
        }
        result = gus.format_user_state(minimal_row)
        assert result["found"] is True
        assert result["connection_url"] is None
        assert result["auth_url"] is None
        assert result["devbox_name"] == ""

    def test_raw_row_included(self):
        result = gus.format_user_state(SAMPLE_DV_ROW)
        assert result["raw"] is SAMPLE_DV_ROW

    def test_output_is_json_serialisable(self):
        result = gus.format_user_state(SAMPLE_DV_ROW)
        # Must not raise
        serialised = json.dumps(result)
        assert isinstance(serialised, str)


# ===========================================================================
# CLI integration tests (via main())
# ===========================================================================

class TestGetUserStateCLISuccess:
    """test_get_user_state_cli_success -- user found, exit code 0."""

    @patch("get_user_state.requests.get")
    def test_returns_exit_code_0_when_user_found(self, mock_get, monkeypatch, capsys):
        _mock_az_token_success(monkeypatch)
        mock_get.return_value = FakeResponse(
            json_data={"value": [SAMPLE_DV_ROW]}
        )

        exit_code = gus.main(["--email", "alice@example.com"])

        assert exit_code == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["found"] is True
        assert output["user_email"] == "alice@example.com"
        assert output["onboarding_step"] == "auth_pending"

    @patch("get_user_state.requests.get")
    def test_stdout_is_valid_json(self, mock_get, monkeypatch, capsys):
        _mock_az_token_success(monkeypatch)
        mock_get.return_value = FakeResponse(
            json_data={"value": [SAMPLE_DV_ROW]}
        )

        gus.main(["--email", "alice@example.com"])

        captured = capsys.readouterr()
        # Must parse without error
        data = json.loads(captured.out)
        assert isinstance(data, dict)
        assert "found" in data
        assert "raw" in data

    @patch("get_user_state.requests.get")
    def test_contains_all_expected_fields(self, mock_get, monkeypatch, capsys):
        _mock_az_token_success(monkeypatch)
        mock_get.return_value = FakeResponse(
            json_data={"value": [SAMPLE_DV_ROW]}
        )

        gus.main(["--email", "alice@example.com"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        expected_keys = {
            "found", "user_email", "user_id", "onboarding_step",
            "devbox_name", "devbox_status", "azure_ad_id",
            "connection_url", "auth_url", "claude_auth_status",
            "manager_status", "last_seen",
            "provisioning_started", "provisioning_complete",
            "auth_complete", "raw",
        }
        assert expected_keys.issubset(set(data.keys()))


class TestGetUserStateCLINotFound:
    """test_get_user_state_cli_not_found -- user not in DV, exit code 1."""

    @patch("get_user_state.requests.get")
    def test_returns_exit_code_1_when_not_found(self, mock_get, monkeypatch, capsys):
        _mock_az_token_success(monkeypatch)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        exit_code = gus.main(["--email", "nobody@example.com"])

        assert exit_code == 1

    @patch("get_user_state.requests.get")
    def test_stdout_json_shows_not_found(self, mock_get, monkeypatch, capsys):
        _mock_az_token_success(monkeypatch)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        gus.main(["--email", "nobody@example.com"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["found"] is False
        assert data["email"] == "nobody@example.com"


class TestGetUserStateCLIError:
    """test_get_user_state_cli_error -- various error conditions, exit code 2."""

    def test_returns_exit_code_2_on_auth_failure(self, monkeypatch, capsys):
        """When az CLI fails to produce a token, exit code is 2."""
        monkeypatch.setattr(
            gus, "get_access_token",
            lambda resource_url=None: (_ for _ in ()).throw(
                RuntimeError("az login required")
            ),
        )

        exit_code = gus.main(["--email", "alice@example.com"])

        assert exit_code == 2
        captured = capsys.readouterr()
        # Error is on stderr
        error_data = json.loads(captured.err)
        assert "error" in error_data
        assert "az login" in error_data["error"]

    @patch("get_user_state.requests.get")
    def test_returns_exit_code_2_on_http_error(self, mock_get, monkeypatch, capsys):
        _mock_az_token_success(monkeypatch)
        mock_get.return_value = FakeResponse(
            status_code=403, text="Forbidden"
        )

        exit_code = gus.main(["--email", "alice@example.com"])

        assert exit_code == 2
        captured = capsys.readouterr()
        error_data = json.loads(captured.err)
        assert "error" in error_data

    @patch("get_user_state.requests.get")
    def test_returns_exit_code_2_on_timeout(self, mock_get, monkeypatch, capsys):
        _mock_az_token_success(monkeypatch)
        mock_get.side_effect = req.exceptions.Timeout("timed out")

        exit_code = gus.main(["--email", "alice@example.com"])

        assert exit_code == 2
        captured = capsys.readouterr()
        error_data = json.loads(captured.err)
        assert "timed out" in error_data["error"].lower()

    @patch("get_user_state.requests.get")
    def test_returns_exit_code_2_on_unexpected_exception(self, mock_get, monkeypatch, capsys):
        _mock_az_token_success(monkeypatch)
        mock_get.side_effect = ConnectionError("DNS resolution failed")

        exit_code = gus.main(["--email", "alice@example.com"])

        assert exit_code == 2
        captured = capsys.readouterr()
        error_data = json.loads(captured.err)
        assert "DNS resolution failed" in error_data["error"]

    @patch("get_user_state.requests.get")
    def test_error_output_includes_email(self, mock_get, monkeypatch, capsys):
        """Error output always includes the queried email for traceability."""
        _mock_az_token_success(monkeypatch)
        mock_get.side_effect = Exception("boom")

        gus.main(["--email", "trace@example.com"])

        captured = capsys.readouterr()
        error_data = json.loads(captured.err)
        assert error_data["email"] == "trace@example.com"


# ===========================================================================
# Argparse tests
# ===========================================================================

class TestArgparse:
    """Verify argparse configuration and --help behaviour."""

    def test_email_is_required(self):
        """Omitting --email should cause a SystemExit (argparse error)."""
        with pytest.raises(SystemExit) as exc_info:
            gus.build_parser().parse_args([])
        assert exc_info.value.code != 0

    def test_help_flag_exits_cleanly(self):
        """--help should exit with code 0."""
        with pytest.raises(SystemExit) as exc_info:
            gus.build_parser().parse_args(["--help"])
        assert exc_info.value.code == 0

    def test_email_parsed_correctly(self):
        args = gus.build_parser().parse_args(["--email", "test@example.com"])
        assert args.email == "test@example.com"

    def test_dataverse_url_override(self):
        args = gus.build_parser().parse_args([
            "--email", "x@y.com",
            "--dataverse-url", "https://custom.crm.dynamics.com",
        ])
        assert args.dataverse_url == "https://custom.crm.dynamics.com"

    def test_users_table_override(self):
        args = gus.build_parser().parse_args([
            "--email", "x@y.com",
            "--users-table", "custom_users",
        ])
        assert args.users_table == "custom_users"


# ===========================================================================
# _build_headers unit test
# ===========================================================================

class TestBuildHeaders:
    """Verify the OData header builder."""

    def test_headers_include_auth(self):
        h = gus._build_headers("my-token")
        assert h["Authorization"] == "Bearer my-token"

    def test_headers_include_odata(self):
        h = gus._build_headers("tok")
        assert h["OData-Version"] == "4.0"
        assert h["OData-MaxVersion"] == "4.0"
        assert h["Accept"] == "application/json"


# ===========================================================================
# send_message tests (T032)
# ===========================================================================

SAMPLE_REPLY_TO_ID = "conv-reply-0001-0002-0003-000000000001"
SAMPLE_MCS_CONV_ID = "mcs-conv-xyz789"
SAMPLE_USER_EMAIL = "testuser@example.com"

SAMPLE_PARENT_ROW = {
    "cr_shraga_conversationid": SAMPLE_REPLY_TO_ID,
    "cr_useremail": SAMPLE_USER_EMAIL,
    "cr_mcs_conversation_id": SAMPLE_MCS_CONV_ID,
    "cr_message": "Hello, I need help",
    "cr_direction": "Inbound",
    "cr_status": "Processed",
}


class TestSendMessageCli:
    """Tests for scripts/send_message.py CLI and core functions."""

    @patch("send_message.requests.post")
    def test_send_message_creates_outbound_row(self, mock_post):
        mock_post.return_value = FakeResponse(json_data={"cr_shraga_conversationid": "new-row-id"})

        from send_message import send_message as sm_send
        result = sm_send(
            token="fake-token",
            in_reply_to=SAMPLE_REPLY_TO_ID,
            user_email=SAMPLE_USER_EMAIL,
            mcs_conversation_id=SAMPLE_MCS_CONV_ID,
            message="Hello back!",
            followup=False,
        )

        assert result is not None
        body = mock_post.call_args[1]["json"]
        assert body["cr_direction"] == "Outbound"
        assert body["cr_status"] == "Unclaimed"
        assert body["cr_message"] == "Hello back!"
        assert body["cr_in_reply_to"] == SAMPLE_REPLY_TO_ID
        assert body["cr_followup_expected"] == ""

    @patch("send_message.requests.post")
    def test_send_message_followup_flag(self, mock_post):
        mock_post.return_value = FakeResponse(json_data={})

        from send_message import send_message as sm_send
        sm_send(
            token="fake-token",
            in_reply_to=SAMPLE_REPLY_TO_ID,
            user_email=SAMPLE_USER_EMAIL,
            mcs_conversation_id=SAMPLE_MCS_CONV_ID,
            message="Working on it...",
            followup=True,
        )

        body = mock_post.call_args[1]["json"]
        assert body["cr_followup_expected"] == "true"

    @patch("send_message.requests.post")
    def test_send_message_truncates_name(self, mock_post):
        mock_post.return_value = FakeResponse(json_data={})

        from send_message import send_message as sm_send
        sm_send(
            token="fake-token",
            in_reply_to=SAMPLE_REPLY_TO_ID,
            user_email=SAMPLE_USER_EMAIL,
            mcs_conversation_id=SAMPLE_MCS_CONV_ID,
            message="A" * 500,
        )

        body = mock_post.call_args[1]["json"]
        assert len(body["cr_name"]) == 100
        assert body["cr_message"] == "A" * 500

    @patch("send_message.requests.post")
    def test_send_message_handles_204_no_content(self, mock_post):
        mock_post.return_value = FakeResponse(status_code=204, json_data=None, text="")

        from send_message import send_message as sm_send
        result = sm_send(
            token="fake-token",
            in_reply_to=SAMPLE_REPLY_TO_ID,
            user_email=SAMPLE_USER_EMAIL,
            mcs_conversation_id=SAMPLE_MCS_CONV_ID,
            message="Test",
        )

        assert result is True

    @patch("send_message.requests.post")
    def test_send_message_sets_auth_header(self, mock_post):
        mock_post.return_value = FakeResponse(json_data={})

        from send_message import send_message as sm_send
        sm_send(
            token="my-bearer-token-xyz",
            in_reply_to=SAMPLE_REPLY_TO_ID,
            user_email=SAMPLE_USER_EMAIL,
            mcs_conversation_id=SAMPLE_MCS_CONV_ID,
            message="Test",
        )

        headers = mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer my-bearer-token-xyz"
        assert headers["Content-Type"] == "application/json"

    @patch("send_message.requests.get")
    def test_fetch_parent_message_success(self, mock_get):
        mock_get.return_value = FakeResponse(json_data=SAMPLE_PARENT_ROW)

        from send_message import fetch_parent_message
        result = fetch_parent_message("fake-token", SAMPLE_REPLY_TO_ID)

        assert result["cr_useremail"] == SAMPLE_USER_EMAIL

    @patch("send_message.requests.get")
    def test_fetch_parent_message_not_found(self, mock_get):
        mock_get.return_value = FakeResponse(status_code=404, text="Not Found")

        from send_message import fetch_parent_message
        with pytest.raises(Exception):
            fetch_parent_message("fake-token", "nonexistent-id")

    @patch("send_message.send_message")
    @patch("send_message.fetch_parent_message")
    @patch("send_message.get_token")
    def test_main_happy_path(self, mock_get_token, mock_fetch, mock_send):
        mock_get_token.return_value = "fake-token"
        mock_fetch.return_value = SAMPLE_PARENT_ROW
        mock_send.return_value = True

        from send_message import main as sm_main
        exit_code = sm_main([
            "--reply-to", SAMPLE_REPLY_TO_ID,
            "--message", "Hello from CLI",
        ])

        assert exit_code == 0

    @patch("send_message.send_message")
    @patch("send_message.fetch_parent_message")
    @patch("send_message.get_token")
    def test_main_with_followup(self, mock_get_token, mock_fetch, mock_send):
        mock_get_token.return_value = "fake-token"
        mock_fetch.return_value = SAMPLE_PARENT_ROW
        mock_send.return_value = True

        from send_message import main as sm_main
        exit_code = sm_main([
            "--reply-to", SAMPLE_REPLY_TO_ID,
            "--message", "Working on it",
            "--followup",
        ])

        assert exit_code == 0

    @patch("send_message.get_token")
    def test_main_auth_failure(self, mock_get_token):
        mock_get_token.side_effect = Exception("No credentials available")

        from send_message import main as sm_main
        exit_code = sm_main([
            "--reply-to", SAMPLE_REPLY_TO_ID,
            "--message", "Hello",
        ])

        assert exit_code == 1

    @patch("send_message.fetch_parent_message")
    @patch("send_message.get_token")
    def test_main_parent_not_found(self, mock_get_token, mock_fetch):
        mock_get_token.return_value = "fake-token"
        mock_fetch.side_effect = Exception("404 Not Found")

        from send_message import main as sm_main
        exit_code = sm_main([
            "--reply-to", "nonexistent-id",
            "--message", "Hello",
        ])

        assert exit_code == 1

    @patch("send_message.fetch_parent_message")
    @patch("send_message.get_token")
    def test_main_parent_missing_email(self, mock_get_token, mock_fetch):
        mock_get_token.return_value = "fake-token"
        mock_fetch.return_value = {
            "cr_shraga_conversationid": SAMPLE_REPLY_TO_ID,
            "cr_useremail": "",
            "cr_mcs_conversation_id": SAMPLE_MCS_CONV_ID,
        }

        from send_message import main as sm_main
        exit_code = sm_main([
            "--reply-to", SAMPLE_REPLY_TO_ID,
            "--message", "Hello",
        ])

        assert exit_code == 1

    def test_main_missing_required_args(self):
        from send_message import main as sm_main
        with pytest.raises(SystemExit) as exc_info:
            sm_main([])
        assert exc_info.value.code != 0

    @patch("send_message.requests.post")
    def test_body_fields_match_global_manager(self, mock_post):
        mock_post.return_value = FakeResponse(json_data={})

        from send_message import send_message as sm_send
        sm_send(
            token="fake-token",
            in_reply_to="reply-id-123",
            user_email="user@example.com",
            mcs_conversation_id="mcs-conv-456",
            message="Test message",
            followup=True,
        )

        body = mock_post.call_args[1]["json"]
        expected_keys = {
            "cr_name", "cr_useremail", "cr_mcs_conversation_id",
            "cr_message", "cr_direction", "cr_status",
            "cr_in_reply_to", "cr_followup_expected",
        }
        assert set(body.keys()) == expected_keys
        assert body["cr_direction"] == "Outbound"
        assert body["cr_status"] == "Unclaimed"

    def test_build_headers_without_content_type(self):
        from send_message import build_headers as sm_build_headers
        h = sm_build_headers("test-token")
        assert h["Authorization"] == "Bearer test-token"
        assert "Content-Type" not in h

    def test_build_headers_with_content_type(self):
        from send_message import build_headers as sm_build_headers
        h = sm_build_headers("test-token", content_type="application/json")
        assert h["Content-Type"] == "application/json"


# ===========================================================================
# update_user_state tests (T029)
# ===========================================================================

import update_user_state as uus


class TestParseField:
    """Tests for the parse_field helper."""

    def test_simple_key_value(self):
        key, value = uus.parse_field("crb3b_onboardingstep=provisioning")
        assert key == "crb3b_onboardingstep"
        assert value == "provisioning"

    def test_value_with_equals(self):
        key, value = uus.parse_field("crb3b_devboxname=box=01")
        assert key == "crb3b_devboxname"
        assert value == "box=01"

    def test_strips_whitespace(self):
        key, value = uus.parse_field("  crb3b_devboxname = shraga-box  ")
        assert key == "crb3b_devboxname"
        assert value == "shraga-box"

    def test_missing_equals_raises(self):
        with pytest.raises(ValueError, match="Invalid field format"):
            uus.parse_field("crb3b_onboardingstep")

    def test_empty_key_raises(self):
        with pytest.raises(ValueError, match="Empty key"):
            uus.parse_field("=somevalue")


class TestValidateFields:
    """Tests for the validate_fields helper."""

    def test_all_valid(self):
        fields = {
            "crb3b_onboardingstep": "provisioning",
            "crb3b_devboxname": "shraga-box-01",
        }
        assert uus.validate_fields(fields) == []

    def test_some_invalid(self):
        fields = {
            "crb3b_onboardingstep": "provisioning",
            "crb3b_bogusfield": "bad",
            "crb3b_anotherbad": "worse",
        }
        invalid = uus.validate_fields(fields)
        assert "crb3b_bogusfield" in invalid
        assert "crb3b_anotherbad" in invalid
        assert "crb3b_onboardingstep" not in invalid

    def test_empty_fields(self):
        assert uus.validate_fields({}) == []


class TestValidUserFieldsSync:
    """Verify VALID_USER_FIELDS in the script is well-defined.

    The refactored global_manager (T038) delegates field validation to
    update_user_state.py via subprocess, so VALID_USER_FIELDS now lives
    solely in the script.  We verify the constant exists and contains
    the expected core fields.
    """

    def test_valid_user_fields_contains_core_fields(self):
        core = {
            "crb3b_useremail",
            "crb3b_azureadid",
            "crb3b_devboxname",
            "crb3b_devboxstatus",
            "crb3b_claudeauthstatus",
            "crb3b_managerstatus",
            "crb3b_onboardingstep",
            "crb3b_lastseen",
        }
        assert core.issubset(uus.VALID_USER_FIELDS), (
            f"Missing core fields: {core - uus.VALID_USER_FIELDS}"
        )


class TestUpdateUserStateCLI:
    """Tests for the full CLI via main(argv=...)."""

    @patch("update_user_state.get_token", return_value="fake-token")
    @patch("update_user_state.requests.get")
    @patch("update_user_state.requests.patch")
    def test_update_user_state_cli_success(self, mock_patch, mock_get, mock_token, capsys):
        mock_get.return_value = FakeResponse(json_data={
            "value": [{
                "crb3b_shragauserid": "row-abc-123",
                "crb3b_useremail": "user@example.com",
                "crb3b_onboardingstep": "new",
            }]
        })
        mock_patch.return_value = FakeResponse(status_code=204)

        exit_code = uus.main([
            "--email", "user@example.com",
            "--field", "crb3b_onboardingstep=provisioning",
        ])

        assert exit_code == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["success"] is True
        assert output["action"] == "patch"

    def test_update_user_state_cli_invalid_field(self, capsys):
        exit_code = uus.main([
            "--email", "user@example.com",
            "--field", "crb3b_nonexistent=bad",
        ])

        assert exit_code == 1
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["success"] is False
        assert "crb3b_nonexistent" in output["error"]

    @patch("update_user_state.get_token", return_value="fake-token")
    @patch("update_user_state.requests.get")
    @patch("update_user_state.requests.post")
    def test_cli_creates_new_user(self, mock_post, mock_get, mock_token, capsys):
        mock_get.return_value = FakeResponse(json_data={"value": []})
        mock_post.return_value = FakeResponse(json_data={
            "crb3b_shragauserid": "new-row-456",
        })

        exit_code = uus.main([
            "--email", "newuser@example.com",
            "--field", "crb3b_onboardingstep=awaiting_setup",
        ])

        assert exit_code == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["success"] is True
        assert output["action"] == "create"

    @patch("update_user_state.get_token", return_value="fake-token")
    @patch("update_user_state.requests.get")
    @patch("update_user_state.requests.patch")
    def test_cli_multiple_fields(self, mock_patch, mock_get, mock_token, capsys):
        mock_get.return_value = FakeResponse(json_data={
            "value": [{"crb3b_shragauserid": "row-multi-001", "crb3b_useremail": "multi@example.com"}]
        })
        mock_patch.return_value = FakeResponse(status_code=204)

        exit_code = uus.main([
            "--email", "multi@example.com",
            "--field", "crb3b_onboardingstep=provisioning",
            "--field", "crb3b_devboxname=shraga-multi",
            "--field", "crb3b_azureadid=aad-multi-guid",
        ])

        assert exit_code == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["success"] is True

    def test_cli_mixed_valid_and_invalid_fields(self, capsys):
        exit_code = uus.main([
            "--email", "user@example.com",
            "--field", "crb3b_onboardingstep=ok",
            "--field", "crb3b_badfield=nope",
        ])
        assert exit_code == 1

    def test_cli_auth_failure(self, capsys):
        with patch("update_user_state.get_token", side_effect=Exception("No credentials")):
            exit_code = uus.main([
                "--email", "user@example.com",
                "--field", "crb3b_onboardingstep=test",
            ])
        assert exit_code == 1

    @patch("update_user_state.get_token", return_value="fake-token")
    @patch("update_user_state.requests.get")
    def test_cli_dataverse_failure(self, mock_get, mock_token, capsys):
        mock_get.side_effect = Exception("Connection refused")

        exit_code = uus.main([
            "--email", "user@example.com",
            "--field", "crb3b_onboardingstep=test",
        ])
        assert exit_code == 1

    def test_cli_bad_field_format(self, capsys):
        exit_code = uus.main([
            "--email", "user@example.com",
            "--field", "crb3b_onboardingstep",
        ])
        assert exit_code == 1

    def test_cli_missing_email_exits(self):
        with pytest.raises(SystemExit) as exc_info:
            uus.main(["--field", "crb3b_onboardingstep=test"])
        assert exc_info.value.code != 0
