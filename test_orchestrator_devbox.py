"""Tests for orchestrator_devbox.py – DevBoxManager"""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from orchestrator_devbox import (
    DevBoxManager,
    DevBoxInfo,
    cli_main,
    _build_parser,
    SHRAGA_REPO_URL,
    SHRAGA_ZIP_URL,
    SHRAGA_AUTH_SCRIPT_URL,
    SHRAGA_DEPLOY_DIR,
)


# ===========================================================================
# Deployment URL constants
# ===========================================================================

class TestDeploymentConstants:
    """Verify shared deployment URL constants point to GitHub (not personal OneDrive)."""

    def test_repo_url_is_github(self):
        assert SHRAGA_REPO_URL.startswith("https://github.com/")
        assert "ShragaBot/ShragaBot" in SHRAGA_REPO_URL

    def test_zip_url_is_github_archive(self):
        assert SHRAGA_ZIP_URL.startswith("https://github.com/")
        assert SHRAGA_ZIP_URL.endswith("/archive/refs/heads/main.zip")

    def test_auth_script_url_is_raw_github(self):
        assert SHRAGA_AUTH_SCRIPT_URL.startswith("https://raw.githubusercontent.com/")
        assert "authenticate.ps1" in SHRAGA_AUTH_SCRIPT_URL

    def test_no_onedrive_references_in_constants(self):
        """None of the deployment constants should reference personal OneDrive."""
        for url in [SHRAGA_REPO_URL, SHRAGA_ZIP_URL, SHRAGA_AUTH_SCRIPT_URL]:
            assert "1drv.ms" not in url
            assert "onedrive" not in url.lower()
            assert "sharepoint" not in url.lower()

    def test_deploy_dir_is_standard_path(self):
        assert SHRAGA_DEPLOY_DIR == r"C:\Dev\shraga-worker"


# ===========================================================================
# DevBoxManager init
# ===========================================================================

class TestDevBoxManagerInit:

    @patch("orchestrator_devbox.AzureCliCredential")
    def test_default_credential(self, mock_cred):
        mgr = DevBoxManager(
            devcenter_endpoint="https://dc.example.com",
            project_name="proj",
            pool_name="pool"
        )
        mock_cred.assert_called_once()
        assert mgr.devcenter_endpoint == "https://dc.example.com"
        assert mgr.project_name == "proj"
        assert mgr.pool_name == "pool"

    @patch("orchestrator_devbox.AzureCliCredential")
    def test_default_pool_name(self, mock_cred):
        mgr = DevBoxManager(
            devcenter_endpoint="https://dc.example.com",
            project_name="proj"
        )
        assert mgr.pool_name == "botdesigner-pool-italynorth"

    def test_external_credential_skips_default(self):
        """When an external credential is provided, AzureCliCredential is not used."""
        external_cred = MagicMock()
        with patch("orchestrator_devbox.AzureCliCredential") as mock_default:
            mgr = DevBoxManager(
                devcenter_endpoint="https://dc.example.com",
                project_name="proj",
                credential=external_cred,
            )
        mock_default.assert_not_called()
        assert mgr.credential is external_cred

    def test_no_device_code_parameter(self):
        """DevBoxManager no longer accepts use_device_code parameter.

        Device code auth was removed because Azure Conditional Access policies
        block the device code grant flow.
        """
        import inspect
        sig = inspect.signature(DevBoxManager.__init__)
        assert "use_device_code" not in sig.parameters


# ===========================================================================
# provision_devbox
# ===========================================================================

class TestProvisionDevbox:

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    @patch("orchestrator_devbox.requests.get")
    def test_provision_success(self, mock_get, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        # list_devboxes returns no existing boxes
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"value": []}
        )

        mock_put.return_value = MagicMock(
            status_code=201,
            json=lambda: {"name": "shraga-box-01", "provisioningState": "Provisioning"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.provision_devbox("user-aad-id", "alice@example.com")

        assert result["name"] == "shraga-box-01"
        mock_put.assert_called_once()
        # Verify URL contains user ID and devbox name
        call_url = mock_put.call_args[0][0]
        assert "user-aad-id" in call_url
        assert "shraga-box-01" in call_url

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    @patch("orchestrator_devbox.requests.get")
    def test_provision_failure_raises(self, mock_get, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"value": []}
        )

        mock_put.return_value = MagicMock(status_code=500, text="Server Error")

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Failed to provision"):
            mgr.provision_devbox("user-id", "bob@example.com")

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    @patch("orchestrator_devbox.requests.get")
    def test_devbox_name_uses_box_convention(self, mock_get, mock_put, mock_cred):
        """Dev box names now use shraga-box-{NN} auto-increment, not email-based."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        # One existing box
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"value": [{"name": "shraga-box-01"}]}
        )

        mock_put.return_value = MagicMock(
            status_code=200,
            json=lambda: {"name": "shraga-box-02"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.provision_devbox("uid", "john.doe@example.com")

        call_url = mock_put.call_args[0][0]
        assert "shraga-box-02" in call_url


# ===========================================================================
# get_devbox_status
# ===========================================================================

class TestGetDevboxStatus:

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_returns_devbox_info(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "name": "shraga-test",
                "user": "user-id",
                "powerState": "Running",
                "provisioningState": "Succeeded"
            }
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        info = mgr.get_devbox_status("user-id", "shraga-test")

        assert isinstance(info, DevBoxInfo)
        assert info.name == "shraga-test"
        assert info.status == "Running"
        assert info.provisioning_state == "Succeeded"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_status_failure_raises(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(status_code=404, text="Not Found")

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Failed to get Dev Box status"):
            mgr.get_devbox_status("user-id", "nonexistent")


# ===========================================================================
# get_connection_url (convenience wrapper)
# ===========================================================================

class TestGetConnectionUrl:

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_returns_connection_url_string(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "name": "shraga-test",
                "user": "user-id",
                "powerState": "Running",
                "provisioningState": "Succeeded"
            }
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        url = mgr.get_connection_url("user-id", "shraga-test")
        assert isinstance(url, str)
        assert "devbox.microsoft.com/connect" in url
        assert "shraga-test" in url

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_raises_when_devbox_not_found(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(status_code=404, text="Not Found")

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Failed to get Dev Box status"):
            mgr.get_connection_url("user-id", "nonexistent")


# ===========================================================================
# wait_for_provisioning
# ===========================================================================

class TestWaitForProvisioning:

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.time.sleep")
    @patch("orchestrator_devbox.time.time")
    @patch("orchestrator_devbox.requests.get")
    def test_returns_when_succeeded(self, mock_get, mock_time, mock_sleep, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        # Each get_devbox_status call makes 2 requests: status + remoteConnection
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: {
                "name": "box", "user": "u", "powerState": "Off", "provisioningState": "Provisioning"
            }),
            MagicMock(status_code=200, json=lambda: {"webUrl": "https://rdp.example.com"}),
            MagicMock(status_code=200, json=lambda: {
                "name": "box", "user": "u", "powerState": "Running", "provisioningState": "Succeeded"
            }),
            MagicMock(status_code=200, json=lambda: {"webUrl": "https://rdp.example.com"}),
        ]
        mock_time.side_effect = [0, 10, 20, 40]

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        info = mgr.wait_for_provisioning("user-id", "box", timeout_minutes=5)
        assert info.provisioning_state == "Succeeded"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.time.sleep")
    @patch("orchestrator_devbox.time.time")
    @patch("orchestrator_devbox.requests.get")
    def test_raises_on_failure(self, mock_get, mock_time, mock_sleep, mock_cred):
        """When provisioning fails, the exception propagates immediately
        instead of looping until timeout."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "name": "box", "user": "u", "powerState": "Off", "provisioningState": "Failed"
            }
        )
        mock_time.side_effect = [0, 10]

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Dev Box provisioning failed"):
            mgr.wait_for_provisioning("user-id", "box", timeout_minutes=1)

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.time.sleep")
    @patch("orchestrator_devbox.time.time")
    @patch("orchestrator_devbox.requests.get")
    def test_failure_raises_immediately_without_sleeping(self, mock_get, mock_time, mock_sleep, mock_cred):
        """Provisioning failure should not call sleep before raising."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "name": "box", "user": "u", "powerState": "Off", "provisioningState": "Failed"
            }
        )
        mock_time.side_effect = [0, 10]

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Dev Box provisioning failed"):
            mgr.wait_for_provisioning("user-id", "box", timeout_minutes=5)

        mock_sleep.assert_not_called()

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.time.sleep")
    @patch("orchestrator_devbox.time.time")
    @patch("orchestrator_devbox.requests.get")
    def test_network_error_retries_then_succeeds(self, mock_get, mock_time, mock_sleep, mock_cred):
        """Network errors from get_devbox_status are caught and retried."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        # First call: network error, second call: success
        mock_get.side_effect = [
            MagicMock(status_code=500, text="Server Error"),
            MagicMock(status_code=200, json=lambda: {
                "name": "box", "user": "u", "powerState": "Running", "provisioningState": "Succeeded"
            })
        ]
        mock_time.side_effect = [0, 10, 20, 30]

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        info = mgr.wait_for_provisioning("user-id", "box", timeout_minutes=5)
        assert info.provisioning_state == "Succeeded"
        # Sleep called once for the network error retry
        assert mock_sleep.call_count == 1

    def test_default_timeout_is_35_minutes(self):
        """The default timeout for wait_for_provisioning should be 35 minutes."""
        import inspect
        sig = inspect.signature(DevBoxManager.wait_for_provisioning)
        default = sig.parameters["timeout_minutes"].default
        assert default == 35, f"Expected default timeout_minutes=35 but got {default}"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.time.sleep")
    @patch("orchestrator_devbox.time.time")
    @patch("orchestrator_devbox.requests.get")
    def test_timeout_raises(self, mock_get, mock_time, mock_sleep, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "name": "box", "user": "u", "powerState": "Off", "provisioningState": "Provisioning"
            }
        )
        # Time exceeds 1 minute timeout
        mock_time.side_effect = [0, 61]

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(TimeoutError):
            mgr.wait_for_provisioning("user-id", "box", timeout_minutes=1)


# ===========================================================================
# apply_customizations
# ===========================================================================

class TestApplyCustomizations:

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_apply_success(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=201,
            json=lambda: {"status": "Running", "name": "shraga-tools"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.apply_customizations("aad-guid-123", "shraga-alice")

        assert result["status"] == "Running"
        mock_put.assert_called_once()
        call_url = mock_put.call_args[0][0]
        assert "aad-guid-123" in call_url
        assert "shraga-alice" in call_url
        assert "customizationGroups/shraga-tools" in call_url

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_apply_sends_correct_tasks(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_customizations("uid", "box")

        body = mock_put.call_args[1]["json"]
        tasks = body["tasks"]
        assert len(tasks) == 3
        assert tasks[0]["name"] == "DevBox.Catalog/winget"
        assert tasks[0]["parameters"]["package"] == "Git.Git"
        assert tasks[1]["parameters"]["package"] == "Anthropic.ClaudeCode"
        assert tasks[2]["name"] == "DevBox.Catalog/choco"
        assert tasks[2]["parameters"]["package"] == "python312"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_apply_uses_preview_api_version(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=202,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_customizations("uid", "box")

        call_params = mock_put.call_args[1]["params"]
        assert call_params["api-version"] == "2025-04-01-preview"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_apply_409_already_exists_treated_as_success(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=409,
            text="A Customization Group with name shraga-tools already exists."
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.apply_customizations("uid", "box")
        assert result["status"] == "AlreadyExists"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_apply_failure_raises(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(status_code=400, text="Bad Request")

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Failed to apply customizations"):
            mgr.apply_customizations("uid", "box")


# ===========================================================================
# get_customization_status
# ===========================================================================

class TestGetCustomizationStatus:

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_returns_status(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Succeeded", "name": "shraga-tools"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.get_customization_status("aad-guid-123", "shraga-alice")

        assert result["status"] == "Succeeded"
        call_url = mock_get.call_args[0][0]
        assert "customizationGroups/shraga-tools" in call_url
        assert "aad-guid-123" in call_url

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_status_running(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Running", "name": "shraga-tools"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.get_customization_status("uid", "box")
        assert result["status"] == "Running"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_status_uses_preview_api_version(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Succeeded"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.get_customization_status("uid", "box")

        call_params = mock_get.call_args[1]["params"]
        assert call_params["api-version"] == "2025-04-01-preview"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_status_failure_raises(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(status_code=404, text="Not Found")

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Failed to get customization status"):
            mgr.get_customization_status("uid", "box")


# ===========================================================================
# run_command_on_devbox
# ===========================================================================

class TestRunCommandOnDevbox:

    @patch("orchestrator_devbox.AzureCliCredential")
    def test_returns_pending_status(self, mock_cred):
        mock_cred.return_value = MagicMock()
        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.run_command_on_devbox("box", "echo hello", "user-id")
        assert result["status"] == "pending"
        assert result["command"] == "echo hello"


# ===========================================================================
# DevBoxInfo dataclass
# ===========================================================================

class TestDevBoxInfo:
    def test_fields(self):
        info = DevBoxInfo(
            name="box",
            user_id="uid",
            status="Running",
            connection_url="https://example.com",
            provisioning_state="Succeeded"
        )
        assert info.name == "box"
        assert info.user_id == "uid"
        assert info.status == "Running"
        assert info.connection_url == "https://example.com"
        assert info.provisioning_state == "Succeeded"


# ===========================================================================
# next_devbox_name (shraga-box-{NN} auto-increment)
# ===========================================================================

class TestNextDevboxName:
    """Tests for the next_devbox_name() method that implements the
    shraga-box-{NN} auto-increment naming convention ported from setup.ps1."""

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_next_devbox_name_starts_at_01(self, mock_get, mock_cred):
        """When no dev boxes exist, the first name should be shraga-box-01."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"value": []}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        name = mgr.next_devbox_name("user-aad-id")

        assert name == "shraga-box-01"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_next_devbox_name_increments(self, mock_get, mock_cred):
        """When existing boxes are present, the next number is max+1
        (or first gap)."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"value": [
                {"name": "shraga-box-01"},
                {"name": "shraga-box-02"},
                {"name": "shraga-box-03"},
            ]}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        name = mgr.next_devbox_name("user-aad-id")

        assert name == "shraga-box-04"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_next_devbox_name_fills_gaps(self, mock_get, mock_cred):
        """When there are gaps in the numbering (e.g., 01 and 03 exist but not
        02), the next name should fill the gap."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"value": [
                {"name": "shraga-box-01"},
                {"name": "shraga-box-03"},
                {"name": "shraga-box-05"},
            ]}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        name = mgr.next_devbox_name("user-aad-id")

        assert name == "shraga-box-02"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_next_devbox_name_ignores_non_matching_boxes(self, mock_get, mock_cred):
        """Boxes with names that don't match shraga-box-{NN} are ignored."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"value": [
                {"name": "shraga-alice"},
                {"name": "my-dev-box"},
                {"name": "shraga-box-01"},
            ]}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        name = mgr.next_devbox_name("user-aad-id")

        # Should return 02 because 01 is taken (non-matching names are ignored)
        assert name == "shraga-box-02"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_next_devbox_name_two_digit_format(self, mock_get, mock_cred):
        """Name should always use 2-digit zero-padded format."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        # 9 boxes exist (01-09)
        boxes = [{"name": f"shraga-box-{i:02d}"} for i in range(1, 10)]
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"value": boxes}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        name = mgr.next_devbox_name("user-aad-id")

        assert name == "shraga-box-10"


# ===========================================================================
# CLI interface (argparse subcommands)
# ===========================================================================

class TestCliProvision:
    """Test the 'provision' CLI subcommand."""

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    @patch("orchestrator_devbox.requests.get")
    def test_cli_provision(self, mock_get, mock_put, mock_cred, capsys):
        """python orchestrator_devbox.py provision --name shraga-box-01 works."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=201,
            json=lambda: {
                "name": "shraga-box-01",
                "provisioningState": "Provisioning",
            },
        )

        exit_code = cli_main([
            "--endpoint", "https://dc.example.com",
            "--project", "proj",
            "--user-id", "aad-user-id",
            "provision",
            "--name", "shraga-box-01",
        ])

        assert exit_code == 0

        # Verify the PUT was sent with the explicit name in the URL
        call_url = mock_put.call_args[0][0]
        assert "shraga-box-01" in call_url
        assert "aad-user-id" in call_url

        # Verify JSON output was printed
        captured = capsys.readouterr()
        assert "shraga-box-01" in captured.out
        assert "Provisioning" in captured.out

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    @patch("orchestrator_devbox.requests.get")
    def test_cli_provision_auto_name(self, mock_get, mock_put, mock_cred, capsys):
        """When --name is omitted, provision auto-names the Dev Box."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        # list_devboxes returns one existing box
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"value": [{"name": "shraga-box-01"}]},
        )
        mock_put.return_value = MagicMock(
            status_code=201,
            json=lambda: {
                "name": "shraga-box-02",
                "provisioningState": "Provisioning",
            },
        )

        exit_code = cli_main([
            "--endpoint", "https://dc.example.com",
            "--project", "proj",
            "--user-id", "aad-user-id",
            "provision",
        ])

        assert exit_code == 0
        call_url = mock_put.call_args[0][0]
        assert "shraga-box-02" in call_url


class TestCliStatus:
    """Test the 'status' CLI subcommand."""

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_cli_status(self, mock_get, mock_cred, capsys):
        """python orchestrator_devbox.py status --name shraga-box-01 works."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "name": "shraga-box-01",
                "user": "aad-user-id",
                "powerState": "Running",
                "provisioningState": "Succeeded",
            },
        )

        exit_code = cli_main([
            "--endpoint", "https://dc.example.com",
            "--project", "proj",
            "--user-id", "aad-user-id",
            "status",
            "--name", "shraga-box-01",
        ])

        assert exit_code == 0

        captured = capsys.readouterr()
        assert "shraga-box-01" in captured.out
        assert "Running" in captured.out
        assert "Succeeded" in captured.out


class TestCliList:
    """Test the 'list' CLI subcommand."""

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_cli_list(self, mock_get, mock_cred, capsys):
        """python orchestrator_devbox.py list works."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "value": [
                    {
                        "name": "shraga-box-01",
                        "provisioningState": "Succeeded",
                        "powerState": "Running",
                    },
                    {
                        "name": "shraga-box-02",
                        "provisioningState": "Succeeded",
                        "powerState": "Stopped",
                    },
                ]
            },
        )

        exit_code = cli_main([
            "--endpoint", "https://dc.example.com",
            "--project", "proj",
            "--user-id", "aad-user-id",
            "list",
        ])

        assert exit_code == 0

        captured = capsys.readouterr()
        assert "shraga-box-01" in captured.out
        assert "shraga-box-02" in captured.out
        assert "Running" in captured.out
        assert "Stopped" in captured.out

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_cli_list_empty(self, mock_get, mock_cred, capsys):
        """When no Dev Boxes exist, list prints a helpful message."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"value": []},
        )

        exit_code = cli_main([
            "--endpoint", "https://dc.example.com",
            "--project", "proj",
            "--user-id", "aad-user-id",
            "list",
        ])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "No Dev Boxes found" in captured.out


class TestCliSubcommandHelp:
    """Verify all subcommands have --help."""

    def test_all_subcommands_have_help(self):
        """Every registered subcommand should have a help string."""
        parser = _build_parser()
        # Access the subparsers action to check registered commands
        subparsers_actions = [
            action
            for action in parser._subparsers._actions
            if hasattr(action, "_parser_class")
        ]
        assert len(subparsers_actions) == 1
        choices = subparsers_actions[0].choices

        expected_commands = {"provision", "status", "customize", "connect", "delete", "list"}
        assert set(choices.keys()) == expected_commands

        # Each subcommand parser should not raise on --help parse
        # (we just verify they exist and have a description)
        for cmd_name, sub_parser in choices.items():
            assert sub_parser is not None, f"Subparser for '{cmd_name}' is None"

    def test_no_command_prints_help_and_returns_nonzero(self, capsys):
        """Running with no subcommand should print help and return 1."""
        exit_code = cli_main([
            "--endpoint", "https://dc.example.com",
            "--project", "proj",
            "--user-id", "aad-user-id",
        ])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "provision" in captured.out
        assert "status" in captured.out
        assert "list" in captured.out


class TestCliEnvVarFallback:
    """Test that CLI falls back to environment variables for common args."""

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_env_var_fallback(self, mock_get, mock_cred, capsys, monkeypatch):
        """Common args can be passed via env vars instead of CLI flags."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        monkeypatch.setenv("DEVCENTER_ENDPOINT", "https://dc.example.com")
        monkeypatch.setenv("DEVCENTER_PROJECT", "proj")
        monkeypatch.setenv("AZURE_USER_ID", "aad-user-id")

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"value": []},
        )

        exit_code = cli_main(["list"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "No Dev Boxes found" in captured.out

    def test_missing_required_env_vars_exits(self):
        """When required values are missing, cli_main exits with code 1."""
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["list"])
        assert exc_info.value.code == 1


# ===========================================================================
# apply_deploy_customizations (ZIP-based deployment)
# ===========================================================================

class TestApplyDeployCustomizations:
    """Tests for the deploy customization group (Group 2) that deploys code
    from a GitHub ZIP archive to the dev box."""

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_deploy_success(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=201,
            json=lambda: {"status": "Running", "name": "shraga-deploy"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.apply_deploy_customizations("aad-guid-123", "shraga-box-01")

        assert result["status"] == "Running"
        mock_put.assert_called_once()
        call_url = mock_put.call_args[0][0]
        assert "customizationGroups/shraga-deploy" in call_url
        assert "aad-guid-123" in call_url

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_deploy_command_uses_zip_url_not_git_clone(self, mock_put, mock_cred):
        """The deploy command must download a GitHub ZIP archive,
        NOT use git clone from a personal OneDrive or any other
        non-shared location."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_deploy_customizations("uid", "box")

        body = mock_put.call_args[1]["json"]
        command = body["tasks"][0]["parameters"]["command"]

        # Must reference the GitHub ZIP URL
        assert SHRAGA_ZIP_URL in command

        # Must NOT use git clone
        assert "git clone" not in command
        assert "git.exe" not in command.lower() or "clone" not in command.lower()

        # Must NOT reference personal OneDrive
        assert "1drv.ms" not in command
        assert "onedrive" not in command.lower()
        assert "sharepoint" not in command.lower()

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_deploy_command_uses_auth_script_url(self, mock_put, mock_cred):
        """The deploy command should use the shared auth script URL constant."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_deploy_customizations("uid", "box")

        body = mock_put.call_args[1]["json"]
        command = body["tasks"][0]["parameters"]["command"]

        assert SHRAGA_AUTH_SCRIPT_URL in command

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_deploy_command_extracts_zip_to_deploy_dir(self, mock_put, mock_cred):
        """The ZIP should be extracted to SHRAGA_DEPLOY_DIR."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_deploy_customizations("uid", "box")

        body = mock_put.call_args[1]["json"]
        command = body["tasks"][0]["parameters"]["command"]

        # Verify Expand-Archive is used
        assert "Expand-Archive" in command
        # Verify deploy dir is referenced
        assert SHRAGA_DEPLOY_DIR.replace("\\", "\\\\") in command or SHRAGA_DEPLOY_DIR in command

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_deploy_409_already_exists_treated_as_success(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=409,
            text="A Customization Group with name shraga-deploy already exists."
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.apply_deploy_customizations("uid", "box")
        assert result["status"] == "AlreadyExists"
        assert result["name"] == "shraga-deploy"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_deploy_failure_raises(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(status_code=500, text="Server Error")

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Failed to apply deploy customizations"):
            mgr.apply_deploy_customizations("uid", "box")

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_deploy_uses_preview_api_version(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=202,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_deploy_customizations("uid", "box")

        call_params = mock_put.call_args[1]["params"]
        assert call_params["api-version"] == "2025-04-01-preview"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_deploy_command_cleans_up_temp_files(self, mock_put, mock_cred):
        """The deploy command should clean up temporary ZIP and extraction dir."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_deploy_customizations("uid", "box")

        body = mock_put.call_args[1]["json"]
        command = body["tasks"][0]["parameters"]["command"]

        # Verify cleanup of temp ZIP
        assert "Remove-Item" in command
        assert "shraga-worker.zip" in command
        assert "shraga-worker-extract" in command

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_deploy_command_resolves_python_dynamically(self, mock_put, mock_cred):
        """The deploy command should search for Python dynamically rather than
        hardcoding C:\\Python312\\python.exe, because choco installs Python
        to a different location."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=201,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_deploy_customizations("uid", "box")

        body = mock_put.call_args[1]["json"]
        command = body["tasks"][0]["parameters"]["command"]

        # Should contain dynamic Python resolution logic
        assert "pyCandidates" in command or "$pyExe" in command, \
            "Deploy command should resolve Python executable dynamically"
        # Should NOT hardcode C:\Python312\python.exe as the sole path
        assert "& $pyExe" in command, \
            "Deploy command should use the resolved $pyExe variable"

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_deploy_command_includes_pip_packages(self, mock_put, mock_cred):
        """The deploy command should install the required pip packages."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=201,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_deploy_customizations("uid", "box")

        body = mock_put.call_args[1]["json"]
        command = body["tasks"][0]["parameters"]["command"]

        assert "pip install" in command
        assert "requests" in command
        assert "azure-identity" in command
        assert "watchdog" in command

    @patch("orchestrator_devbox.AzureCliCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_deploy_command_includes_scheduled_task(self, mock_put, mock_cred):
        """The deploy command should register the ShragaWorker scheduled task."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=201,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_deploy_customizations("uid", "box")

        body = mock_put.call_args[1]["json"]
        command = body["tasks"][0]["parameters"]["command"]

        assert "Register-ScheduledTask" in command
        assert "ShragaWorker" in command
        assert "AtStartup" in command
