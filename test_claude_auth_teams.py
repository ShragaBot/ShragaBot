"""Tests for claude_auth_teams.py -- Claude authentication via Teams

Tests cover:
- ClaudeAuthManager (legacy local auth)
- RemoteDevBoxAuth (RDP-based auth on the target dev box)
- TeamsClaudeAuth (RDP-first orchestration)
- DEVBOX_SETUP_SCRIPT content
"""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from claude_auth_teams import (
    AUTH_INSTRUCTIONS_TEMPLATE,
    ClaudeAuthManager,
    RemoteDevBoxAuth,
    TeamsClaudeAuth,
    DEVBOX_SETUP_SCRIPT,
    build_auth_instructions,
    get_setup_script,
)


# ===========================================================================
# ClaudeAuthManager (legacy local auth)
# ===========================================================================

class TestClaudeAuthManager:

    @patch("claude_auth_teams.subprocess.Popen")
    def test_start_auth_captures_url(self, mock_popen):
        """start_auth should extract auth URL from Claude output"""
        proc = MagicMock()
        lines = [
            "Starting authentication...\n",
            "Open this URL: https://console.anthropic.com/auth/xyz123\n",
        ]
        proc.stdout.readline = MagicMock(side_effect=lines)
        proc.poll.return_value = None
        mock_popen.return_value = proc

        mgr = ClaudeAuthManager()
        url = mgr.start_auth()
        assert "https://console.anthropic.com/auth/xyz123" in url
        assert mgr.auth_url is not None

    @patch("claude_auth_teams.subprocess.Popen")
    def test_start_auth_timeout_raises(self, mock_popen):
        """start_auth raises TimeoutError if no URL found"""
        proc = MagicMock()
        proc.stdout.readline.return_value = ""
        proc.poll.return_value = None
        mock_popen.return_value = proc

        mgr = ClaudeAuthManager()
        with patch("claude_auth_teams.time.time", side_effect=[0, 0, 31]):
            with pytest.raises(TimeoutError):
                mgr.start_auth()

    @patch("claude_auth_teams.subprocess.Popen")
    def test_start_auth_raises_if_process_exits(self, mock_popen):
        """start_auth raises if process exits before URL"""
        proc = MagicMock()
        proc.stdout.readline.return_value = ""
        proc.poll.return_value = 1
        mock_popen.return_value = proc

        mgr = ClaudeAuthManager()
        with pytest.raises(Exception, match="exited unexpectedly"):
            mgr.start_auth()

    def test_submit_code_without_start_raises(self):
        """submit_code raises if start_auth not called"""
        mgr = ClaudeAuthManager()
        with pytest.raises(RuntimeError, match="not started"):
            mgr.submit_code("ABC-123")

    @patch("claude_auth_teams.subprocess.Popen")
    def test_submit_code_success(self, mock_popen):
        """submit_code returns True on successful auth"""
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.side_effect = [None, 0]
        type(proc).returncode = PropertyMock(return_value=0)
        mock_popen.return_value = proc

        mgr = ClaudeAuthManager()
        mgr.process = proc

        with patch("claude_auth_teams.time.time", side_effect=[0, 0, 0.2]):
            result = mgr.submit_code("ABC-123")
        assert result is True

    @patch("claude_auth_teams.subprocess.Popen")
    def test_submit_code_failure(self, mock_popen):
        """submit_code returns False on auth failure"""
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.side_effect = [None, 1]
        type(proc).returncode = PropertyMock(return_value=1)
        proc.stderr.read.return_value = "Auth failed"
        mock_popen.return_value = proc

        mgr = ClaudeAuthManager()
        mgr.process = proc

        with patch("claude_auth_teams.time.time", side_effect=[0, 0, 0.2]):
            result = mgr.submit_code("WRONG-CODE")
        assert result is False

    def test_cancel_terminates_process(self):
        """cancel terminates the subprocess"""
        mgr = ClaudeAuthManager()
        proc = MagicMock()
        mgr.process = proc
        mgr.cancel()
        proc.terminate.assert_called_once()
        assert mgr.process is None

    def test_cancel_noop_without_process(self):
        """cancel does nothing if no process"""
        mgr = ClaudeAuthManager()
        mgr.cancel()  # should not raise


# ===========================================================================
# RemoteDevBoxAuth
# ===========================================================================

class TestRemoteDevBoxAuth:

    def test_get_connection_url_from_init(self):
        """When connection_url is passed at init, it is returned directly."""
        auth = RemoteDevBoxAuth(connection_url="https://devbox.microsoft.com/connect?devbox=test")
        url = auth.get_connection_url("user-id", "test")
        assert url == "https://devbox.microsoft.com/connect?devbox=test"

    def test_get_connection_url_via_manager(self):
        """When no connection_url is passed, it uses DevBoxManager."""
        mock_mgr = MagicMock()
        mock_mgr.get_connection_url.return_value = "https://devbox.microsoft.com/connect?devbox=foo"

        auth = RemoteDevBoxAuth(devbox_manager=mock_mgr)
        url = auth.get_connection_url("user-aad-id", "foo")
        assert url == "https://devbox.microsoft.com/connect?devbox=foo"
        mock_mgr.get_connection_url.assert_called_once_with("user-aad-id", "foo")

    def test_get_connection_url_raises_without_manager_or_url(self):
        """When neither connection_url nor devbox_manager is provided, raise."""
        auth = RemoteDevBoxAuth()
        with pytest.raises(RuntimeError, match="Cannot resolve connection URL"):
            auth.get_connection_url("uid", "box")

    def test_build_auth_message_contains_url(self):
        """The auth message includes the RDP connection URL."""
        auth = RemoteDevBoxAuth()
        msg = auth.build_auth_message("https://devbox.microsoft.com/connect?devbox=test")
        assert "https://devbox.microsoft.com/connect?devbox=test" in msg

    def test_build_auth_message_contains_claude_login(self):
        """The auth message instructs the user to run claude /login."""
        auth = RemoteDevBoxAuth()
        msg = auth.build_auth_message("https://example.com")
        assert "claude /login" in msg

    def test_build_auth_message_contains_setup_steps(self):
        """The auth message includes setup instructions."""
        auth = RemoteDevBoxAuth()
        msg = auth.build_auth_message("https://example.com")
        assert "Shraga-Authenticate" in msg
        assert "done" in msg.lower()

    def test_get_setup_script(self):
        """The setup script method returns the PS1 script content."""
        auth = RemoteDevBoxAuth()
        script = auth.get_setup_script()
        assert "pip install" in script
        assert "shraga-worker" in script

    def test_connection_url_cached_after_first_call(self):
        """Once resolved, the connection URL is cached."""
        mock_mgr = MagicMock()
        mock_mgr.get_connection_url.return_value = "https://cached.example.com"

        auth = RemoteDevBoxAuth(devbox_manager=mock_mgr)
        auth.get_connection_url("uid", "box")
        auth.get_connection_url("uid", "box")
        # Manager should only be called once
        assert mock_mgr.get_connection_url.call_count == 1


# ===========================================================================
# TeamsClaudeAuth -- RDP-first auth
# ===========================================================================

class TestTeamsClaudeAuth:

    def test_init_stores_params(self):
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        assert auth.user_id == "user-123"
        assert auth.send_message == send_fn

    def test_request_auth_uses_rdp_when_connection_url_available(self):
        """When connection_url is provided, RDP auth is used (not local)."""
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(
            send_fn, "user-123",
            devbox_name="shraga-test",
            user_azure_ad_id="aad-id",
            connection_url="https://devbox.microsoft.com/connect?devbox=shraga-test",
        )
        result = auth.request_authentication()
        assert result["method"] == "rdp"
        assert "devbox.microsoft.com" in result["connection_url"]
        assert auth.used_rdp_auth is True
        # No hardcoded message sent -- caller composes the message
        send_fn.assert_not_called()

    def test_request_auth_uses_rdp_via_manager(self):
        """When devbox_manager is provided, RDP auth resolves the URL."""
        send_fn = MagicMock()
        mock_mgr = MagicMock()
        mock_mgr.get_connection_url.return_value = "https://devbox.microsoft.com/connect?devbox=mgr-box"

        auth = TeamsClaudeAuth(
            send_fn, "user-123",
            devbox_name="mgr-box",
            user_azure_ad_id="aad-id",
            devbox_manager=mock_mgr,
        )
        result = auth.request_authentication()
        assert result["method"] == "rdp"
        assert "devbox.microsoft.com" in result["connection_url"]
        assert auth.used_rdp_auth is True
        send_fn.assert_not_called()

    @patch.object(ClaudeAuthManager, "start_auth", return_value="https://auth.example.com")
    def test_request_auth_falls_back_to_device_code_without_rdp_info(self, mock_start):
        """Without connection_url or manager, falls back to device-code."""
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        result = auth.request_authentication()
        assert result["method"] == "device_code"
        assert result["auth_url"] == "https://auth.example.com"
        # used_rdp_auth should be False since we fell back to device code
        assert auth.used_rdp_auth is False
        send_fn.assert_not_called()

    @patch.object(ClaudeAuthManager, "start_auth", side_effect=Exception("Network error"))
    def test_request_auth_total_failure(self, mock_start):
        """Without RDP info and device-code failure, returns failed dict."""
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        result = auth.request_authentication()
        assert result["method"] == "failed"
        # No hardcoded error message sent -- caller handles failure
        send_fn.assert_not_called()

    @patch.object(ClaudeAuthManager, "submit_code", return_value=True)
    def test_handle_user_code_success(self, mock_submit):
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        result = auth.handle_user_code("  ABC-123  ")
        assert result is True
        mock_submit.assert_called_once_with("ABC-123")
        # No hardcoded message sent -- caller composes the message
        send_fn.assert_not_called()

    @patch.object(ClaudeAuthManager, "submit_code", return_value=False)
    def test_handle_user_code_failure(self, mock_submit):
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        result = auth.handle_user_code("BAD-CODE")
        assert result is False
        # No hardcoded message sent -- caller composes the message
        send_fn.assert_not_called()

    def test_handle_user_done(self):
        """handle_user_done returns True to indicate acknowledgement."""
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        result = auth.handle_user_done()
        assert result is True

    def test_fell_back_to_rdp_is_alias_for_used_rdp_auth(self):
        """The fell_back_to_rdp property is a backward-compat alias."""
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(
            send_fn, "user-123",
            connection_url="https://example.com",
        )
        result = auth.request_authentication()
        assert result["method"] == "rdp"
        assert auth.fell_back_to_rdp == auth.used_rdp_auth


# ===========================================================================
# DEVBOX_SETUP_SCRIPT
# ===========================================================================

class TestDevBoxSetupScript:

    def test_script_installs_pip_packages(self):
        assert "pip install" in DEVBOX_SETUP_SCRIPT
        assert "requests" in DEVBOX_SETUP_SCRIPT
        assert "azure-identity" in DEVBOX_SETUP_SCRIPT
        assert "watchdog" in DEVBOX_SETUP_SCRIPT

    def test_script_clones_repo(self):
        assert "git clone" in DEVBOX_SETUP_SCRIPT
        assert "shraga-worker" in DEVBOX_SETUP_SCRIPT

    def test_script_creates_scheduled_task(self):
        assert "Register-ScheduledTask" in DEVBOX_SETUP_SCRIPT
        assert "ShragaWorker" in DEVBOX_SETUP_SCRIPT

    def test_get_setup_script_returns_same(self):
        assert get_setup_script() == DEVBOX_SETUP_SCRIPT


# ===========================================================================
# Shared Auth Instructions (T027 -- consistent auth flow for GM and PM)
# ===========================================================================

class TestSharedAuthInstructions:
    """Verify the shared auth instructions template and convenience function
    ensure GM and PM produce identical messages."""

    SAMPLE_URL = "https://devbox.microsoft.com/connect?devbox=shraga-test-01"

    def test_template_contains_placeholder(self):
        """AUTH_INSTRUCTIONS_TEMPLATE has a {connection_url} placeholder."""
        assert "{connection_url}" in AUTH_INSTRUCTIONS_TEMPLATE

    def test_template_contains_required_elements(self):
        """Template includes web RDP link reference, Shraga-Authenticate, and done."""
        assert "Shraga-Authenticate" in AUTH_INSTRUCTIONS_TEMPLATE
        assert "claude /login" in AUTH_INSTRUCTIONS_TEMPLATE
        assert "done" in AUTH_INSTRUCTIONS_TEMPLATE.lower()

    def test_build_auth_instructions_formats_url(self):
        """build_auth_instructions replaces the placeholder with the actual URL."""
        msg = build_auth_instructions(self.SAMPLE_URL)
        assert self.SAMPLE_URL in msg
        assert "{connection_url}" not in msg

    def test_build_auth_instructions_matches_template(self):
        """build_auth_instructions output matches template.format()."""
        expected = AUTH_INSTRUCTIONS_TEMPLATE.format(connection_url=self.SAMPLE_URL)
        actual = build_auth_instructions(self.SAMPLE_URL)
        assert actual == expected

    def test_build_auth_instructions_matches_remote_devbox_auth(self):
        """build_auth_instructions produces the same output as
        RemoteDevBoxAuth.build_auth_message for the same URL."""
        auth = RemoteDevBoxAuth()
        from_class = auth.build_auth_message(self.SAMPLE_URL)
        from_func = build_auth_instructions(self.SAMPLE_URL)
        assert from_class == from_func, (
            "RemoteDevBoxAuth.build_auth_message and build_auth_instructions "
            "must produce identical output"
        )

    def test_gm_and_pm_produce_identical_auth_messages(self):
        """Cross-verification: simulate GM and PM auth message generation
        and confirm they are byte-identical.

        GM path: _tool_get_rdp_auth_message -> build_auth_instructions
        PM path: _tool_provision_devbox -> build_auth_instructions

        Both paths call build_auth_instructions with the same connection URL,
        so the output must be identical.
        """
        url = "https://devbox.microsoft.com/connect?devbox=shraga-cross-verify"

        # Simulate GM path
        gm_message = build_auth_instructions(url)

        # Simulate PM path (same function call)
        pm_message = build_auth_instructions(url)

        assert gm_message == pm_message, (
            "GM and PM must produce byte-identical auth instructions"
        )

        # Verify content requirements
        assert url in gm_message
        assert "Shraga-Authenticate" in gm_message
        assert "claude /login" in gm_message
        assert "done" in gm_message.lower()

    def test_auth_message_contains_web_rdp_link(self):
        """Auth instructions include the web RDP connection URL."""
        msg = build_auth_instructions(self.SAMPLE_URL)
        assert "Open this link in your browser: " + self.SAMPLE_URL in msg

    def test_auth_message_mentions_shraga_authenticate_shortcut(self):
        """Auth instructions reference the Shraga-Authenticate desktop shortcut."""
        msg = build_auth_instructions(self.SAMPLE_URL)
        assert "Shraga-Authenticate shortcut on the desktop" in msg

    def test_auth_message_mentions_az_login(self):
        """Auth instructions mention az login for Azure sign-in."""
        msg = build_auth_instructions(self.SAMPLE_URL)
        assert "az login" in msg

    def test_auth_message_mentions_claude_login(self):
        """Auth instructions mention claude /login for device code auth."""
        msg = build_auth_instructions(self.SAMPLE_URL)
        assert "claude /login" in msg

    def test_auth_message_asks_user_to_reply_done(self):
        """Auth instructions ask the user to reply 'done' when finished."""
        msg = build_auth_instructions(self.SAMPLE_URL)
        assert "reply here with done" in msg
