"""
Dev Box management functions for Shraga Orchestrator
Handles provisioning, authentication, and remote command execution
"""

import re
import requests
import time
import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta
from azure.identity import AzureCliCredential
from dataclasses import dataclass
from timeout_utils import call_with_timeout

# ---------------------------------------------------------------------------
# Shared deployment URLs — centralised so every provisioning path references
# the same GitHub-hosted location.  NO personal OneDrive / 1drv.ms links.
# ---------------------------------------------------------------------------
SHRAGA_REPO_URL = "https://github.com/ShragaBot/ShragaBot"
SHRAGA_ZIP_URL = f"{SHRAGA_REPO_URL}/archive/refs/heads/main.zip"
SHRAGA_AUTH_SCRIPT_URL = f"https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/authenticate.ps1"
SHRAGA_DEPLOY_DIR = r"C:\Dev\shraga-worker"


@dataclass
class DevBoxInfo:
    name: str
    user_id: str
    status: str
    connection_url: str
    provisioning_state: str


class DevBoxManager:
    """Manages Dev Box operations for Shraga workers"""

    def __init__(
        self,
        devcenter_endpoint: str,
        project_name: str,
        pool_name: str = "botdesigner-pool-italynorth",
        credential=None,
    ):
        self.devcenter_endpoint = devcenter_endpoint
        self.project_name = project_name
        self.pool_name = pool_name

        # Use externally-provided credential if given (enables process-scoped auth).
        # Otherwise fall back to the default credential chain (env vars, managed
        # identity, Azure CLI, etc.).
        #
        # NOTE: Device code auth was removed because Azure Conditional Access
        # policies block the device code grant flow in this tenant.  Run
        # ``az login`` before using DevBoxManager if no managed identity is
        # available.  See also orchestrator_auth_devicecode.py (deprecated).
        if credential is not None:
            self.credential = credential
        else:
            self.credential = AzureCliCredential()

        self.api_version = "2024-02-01"

        # Token caching
        self._token_cache = None
        self._token_expires = None

    def _get_token(self) -> str:
        """Get access token for Dev Center API (cached)"""
        # Return cached token if still valid
        if self._token_cache and self._token_expires:
            if datetime.now(timezone.utc) < self._token_expires:
                return self._token_cache

        try:
            token_obj = call_with_timeout(
                lambda: self.credential.get_token("https://devcenter.azure.com/.default"),
                timeout_sec=30,
                description="devcenter credential.get_token()"
            )
        except TimeoutError:
            raise RuntimeError("credential.get_token() timed out after 30s")
        self._token_cache = token_obj.token
        self._token_expires = datetime.fromtimestamp(token_obj.expires_on, tz=timezone.utc) - timedelta(minutes=5)
        return self._token_cache

    def _get_headers(self) -> Dict[str, str]:
        """Get HTTP headers with auth token.

        Includes a custom User-Agent because the Azure Application Gateway
        WAF in front of the Dev Center endpoint blocks the default
        ``python-requests/x.y.z`` user-agent with 403 Forbidden.
        """
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
            "User-Agent": "Shraga-DevBoxManager/1.0",
        }

    def list_devboxes(self, user_azure_ad_id: str) -> List[Dict[str, Any]]:
        """
        List all Dev Boxes for a specific user.

        Args:
            user_azure_ad_id: Azure AD object ID of the user

        Returns:
            List of dev box dicts from the DevCenter API
        """
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes"
        )

        response = requests.get(
            url,
            headers=self._get_headers(),
            params={"api-version": self.api_version},
            timeout=30,
        )

        if response.status_code == 200:
            return response.json().get("value", [])
        else:
            raise Exception(
                f"Failed to list Dev Boxes: {response.status_code} {response.text}"
            )

    def next_devbox_name(self, user_azure_ad_id: str) -> str:
        """
        Find the next available dev box name using the shraga-box-{NN} convention.

        Queries the DevCenter API for existing dev boxes, finds all names matching
        the shraga-box-{NN} pattern, extracts the used numbers, and returns the
        first available name (filling gaps). The number is zero-padded to 2 digits.

        This mirrors the logic from setup.ps1 lines 43-56.

        Args:
            user_azure_ad_id: Azure AD object ID of the user

        Returns:
            Next available name, e.g. "shraga-box-01", "shraga-box-02", etc.
        """
        existing_boxes = self.list_devboxes(user_azure_ad_id)

        # Extract numbers from shraga-box-{NN} names
        pattern = re.compile(r"^shraga-box-(\d+)$")
        used_numbers: set[int] = set()
        for box in existing_boxes:
            name = box.get("name", "")
            match = pattern.match(name)
            if match:
                used_numbers.add(int(match.group(1)))

        # Find the first available number starting at 1 (fill gaps)
        next_num = 1
        while next_num in used_numbers:
            next_num += 1

        return f"shraga-box-{next_num:02d}"

    def provision_devbox(
        self,
        user_azure_ad_id: str,
        user_email: str,
        devbox_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Provision a Dev Box for a specific user

        Args:
            user_azure_ad_id: Azure AD object ID of the user
            user_email: User's email (used for logging, not for naming)
            devbox_name: Explicit Dev Box name.  When *None* (default), the
                next available ``shraga-box-NN`` name is chosen automatically.

        Returns:
            Dict with provisioning details
        """
        # Use the explicit name when provided; otherwise auto-increment.
        if devbox_name is None:
            devbox_name = self.next_devbox_name(user_azure_ad_id)

        # API endpoint
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes/{devbox_name}"
        )

        # Request body
        body = {
            "poolName": self.pool_name
        }

        # Make request
        response = requests.put(
            url,
            json=body,
            headers=self._get_headers(),
            params={"api-version": self.api_version},
            timeout=30
        )

        if response.status_code in [200, 201]:
            result = response.json()
            print(f"[OK] Dev Box provisioning started: {devbox_name}")
            return result
        else:
            raise Exception(f"Failed to provision Dev Box: {response.status_code} {response.text}")

    def get_devbox_status(self, user_azure_ad_id: str, devbox_name: str) -> DevBoxInfo:
        """
        Get current status of a Dev Box

        Args:
            user_azure_ad_id: Azure AD object ID of the user
            devbox_name: Name of the Dev Box

        Returns:
            DevBoxInfo with current status
        """
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes/{devbox_name}"
        )

        response = requests.get(
            url,
            headers=self._get_headers(),
            params={"api-version": self.api_version},
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()

            # Get direct web RDP URL from remoteConnection API
            connection_url = self._get_remote_connection_url(user_azure_ad_id, devbox_name)

            return DevBoxInfo(
                name=data.get("name"),
                user_id=data.get("user"),
                status=data.get("powerState", "Unknown"),
                connection_url=connection_url,
                provisioning_state=data.get("provisioningState", "Unknown")
            )
        else:
            raise Exception(f"Failed to get Dev Box status: {response.status_code} {response.text}")

    def _get_remote_connection_url(self, user_azure_ad_id: str, devbox_name: str) -> str:
        """
        Get the direct web RDP URL via the remoteConnection API.

        Returns the webUrl from show-remote-connection which opens the RDP
        session directly (not the portal page).
        """
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes/{devbox_name}"
            f"/remoteConnection"
        )

        try:
            response = requests.get(
                url,
                headers=self._get_headers(),
                params={"api-version": self.api_version},
                timeout=30
            )
            if response.status_code == 200:
                data = response.json()
                web_url = data.get("webUrl", "")
                if web_url:
                    return web_url
        except Exception:
            pass

        # Fallback: construct portal URL if API fails
        return f"https://devbox.microsoft.com/connect?devbox={devbox_name}"

    def get_connection_url(self, user_azure_ad_id: str, devbox_name: str) -> str:
        """
        Get the web RDP connection URL for a Dev Box.

        This is a convenience wrapper around get_devbox_status() that returns
        only the connection URL string, suitable for sending to the user so
        they can open a browser-based remote session.

        Args:
            user_azure_ad_id: Azure AD object ID of the user
            devbox_name: Name of the Dev Box

        Returns:
            Web RDP connection URL string
        """
        info = self.get_devbox_status(user_azure_ad_id, devbox_name)
        return info.connection_url

    def wait_for_provisioning(
        self,
        user_azure_ad_id: str,
        devbox_name: str,
        timeout_minutes: int = 35
    ) -> DevBoxInfo:
        """
        Wait for Dev Box provisioning to complete

        Args:
            user_azure_ad_id: Azure AD object ID of the user
            devbox_name: Name of the Dev Box
            timeout_minutes: Max time to wait

        Returns:
            DevBoxInfo when provisioning is complete
        """
        start_time = time.time()
        timeout_seconds = timeout_minutes * 60

        print(f"⏳ Waiting for Dev Box provisioning (timeout: {timeout_minutes}m)...")

        while True:
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed > timeout_seconds:
                raise TimeoutError(f"Dev Box provisioning timed out after {timeout_minutes} minutes")

            # Get status
            try:
                info = self.get_devbox_status(user_azure_ad_id, devbox_name)
            except Exception as e:
                print(f"  Error checking status: {e}")
                time.sleep(30)
                continue

            if info.provisioning_state == "Succeeded":
                print(f"[OK] Dev Box provisioned successfully!")
                return info
            elif info.provisioning_state == "Failed":
                raise Exception("Dev Box provisioning failed")
            else:
                print(f"  Status: {info.provisioning_state} (elapsed: {int(elapsed)}s)")

            # Wait before next check
            time.sleep(30)

    def apply_customizations(
        self,
        user_azure_ad_id: str,
        devbox_name: str,
    ) -> Dict[str, Any]:
        """
        Apply customization tasks to a provisioned Dev Box via the
        Customization API (2025-04-01-preview).

        Installs Git, Claude Code, and Python 3.12 using the proven recipe.

        Args:
            user_azure_ad_id: Azure AD object ID (GUID) of the user
            devbox_name: Name of the Dev Box

        Returns:
            Dict with API response (includes operation status)
        """
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes/{devbox_name}"
            f"/customizationGroups/shraga-tools"
        )

        body = {
            "tasks": [
                {
                    "name": "DevBox.Catalog/winget",
                    "parameters": {"package": "Git.Git"},
                },
                {
                    "name": "DevBox.Catalog/winget",
                    "parameters": {"package": "Anthropic.ClaudeCode"},
                },
                {
                    "name": "DevBox.Catalog/choco",
                    "parameters": {"package": "python312"},
                },
            ]
        }

        response = requests.put(
            url,
            json=body,
            headers=self._get_headers(),
            params={"api-version": "2025-04-01-preview"},
            timeout=30,
        )

        if response.status_code in [200, 201, 202]:
            result = response.json()
            print(f"Customization applied to {devbox_name}")
            return result
        elif response.status_code == 409:
            # Customization group already exists (e.g., re-applying to an
            # already-customized box).  Treat as success.
            print(f"Customization group already exists on {devbox_name}, skipping")
            return {"status": "AlreadyExists", "name": "shraga-tools"}
        else:
            raise Exception(
                f"Failed to apply customizations: {response.status_code} {response.text}"
            )

    def get_customization_status(
        self,
        user_azure_ad_id: str,
        devbox_name: str,
    ) -> Dict[str, Any]:
        """
        Poll the customization group status for a Dev Box.

        Args:
            user_azure_ad_id: Azure AD object ID (GUID) of the user
            devbox_name: Name of the Dev Box

        Returns:
            Dict with 'status' key — one of 'NotStarted', 'Running',
            'Succeeded', 'Failed', 'ValidationFailed'.
        """
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes/{devbox_name}"
            f"/customizationGroups/shraga-tools"
        )

        response = requests.get(
            url,
            headers=self._get_headers(),
            params={"api-version": "2025-04-01-preview"},
            timeout=30,
        )

        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(
                f"Failed to get customization status: {response.status_code} {response.text}"
            )

    def apply_deploy_customizations(
        self,
        user_azure_ad_id: str,
        devbox_name: str,
    ) -> Dict[str, Any]:
        """
        Apply deployment customizations (Group 2) to a Dev Box.

        This must run AFTER apply_customizations() (Group 1) succeeds,
        because it depends on Git and Python being installed.

        Installs: repo clone, pip packages, ShragaWorker scheduled task,
        and the Shraga-Authenticate desktop shortcut.

        IMPORTANT -- Python PATH limitations (per DEVBOX_CUSTOMIZATION_FINDINGS
        2026-02-16):

        - Python is installed via ``choco`` (winget fails with InstallerErrorCode 3
          in system context).
        - After choco install, Python is NOT on PATH in the system customization
          context.  The standard ``C:\\Python312\\python.exe`` path does not exist;
          choco installs to ``C:\\Python312`` only when using the official installer,
          but the choco ``python312`` package places the binary under the
          Chocolatey lib directory.
        - This script resolves the Python executable dynamically by searching
          well-known choco and system install locations.
        - ``runAs: User`` tasks block on ``WaitingForUserSession`` and are
          therefore not usable for fully automated provisioning.
        - pip packages may still fail if Python cannot be found.  The recommended
          fallback is to install pip packages via Playwright-controlled RDP
          after first user login, or to bake them into the dev box image.
        """
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes/{devbox_name}"
            f"/customizationGroups/shraga-deploy"
        )

        # Single powershell task matching setup.ps1 Step 5
        # Scheduled task uses user-level principal ($env:USERNAME), Interactive
        # logon, AtStartup trigger, RestartCount 3, RestartInterval 1 min --
        # unified with devbox-customization-shraga.yaml gold standard.
        #
        # Code deployment uses the GitHub release ZIP instead of ``git clone``
        # so that (a) Git does not need to be on PATH yet when this runs,
        # (b) the download is a single deterministic archive, and (c) no
        # personal OneDrive / 1drv.ms URLs are referenced anywhere.
        #
        # Python path resolution: choco installs python312 to a location that
        # is NOT C:\Python312.  We search several well-known locations and
        # fall back to PATH-based "python" if none are found.  The pip install
        # and scheduled task both use the resolved $pyExe variable.
        deploy_dir_ps = SHRAGA_DEPLOY_DIR.replace("\\", "\\\\")
        deploy_command = (
            "powercfg /change monitor-timeout-ac 0; "
            "powercfg /change standby-timeout-ac 0; "
            "powercfg /change hibernate-timeout-ac 0; "
            "powercfg /change disk-timeout-ac 0; "
            "powercfg /hibernate off; "
            "reg add 'HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows NT\\Terminal Services' "
            "/v fResetBroken /t REG_DWORD /d 0 /f; "
            f"New-Item -ItemType Directory -Force -Path '{deploy_dir_ps}' | Out-Null; "
            f"Invoke-WebRequest -Uri '{SHRAGA_ZIP_URL}' "
            f"-OutFile '$env:TEMP\\shraga-worker.zip'; "
            f"Expand-Archive -Path '$env:TEMP\\shraga-worker.zip' "
            f"-DestinationPath '$env:TEMP\\shraga-worker-extract' -Force; "
            f"Copy-Item -Path '$env:TEMP\\shraga-worker-extract\\shraga-worker-main\\*' "
            f"-Destination '{deploy_dir_ps}' -Recurse -Force; "
            f"Remove-Item '$env:TEMP\\shraga-worker.zip' -Force -ErrorAction SilentlyContinue; "
            f"Remove-Item '$env:TEMP\\shraga-worker-extract' -Recurse -Force -ErrorAction SilentlyContinue; "
            # Resolve Python executable from choco or standard install locations
            "$pyExe = $null; "
            "$pyCandidates = @("
            "'C:\\Python312\\python.exe', "
            "'C:\\ProgramData\\chocolatey\\lib\\python312\\tools\\python.exe', "
            "'C:\\ProgramData\\chocolatey\\bin\\python3.exe', "
            "'C:\\ProgramData\\chocolatey\\bin\\python.exe'"
            "); "
            "foreach ($c in $pyCandidates) { "
            "if (Test-Path $c) { $pyExe = $c; break } "
            "}; "
            "if (-not $pyExe) { $pyExe = (Get-Command python -ErrorAction SilentlyContinue).Source }; "
            "if (-not $pyExe) { "
            "Write-Warning 'Python not found in any known location. pip install and scheduled task will fail.'; "
            "$pyExe = 'python' "
            "}; "
            "Write-Host \"Using Python: $pyExe\"; "
            "& $pyExe -m pip install requests azure-identity azure-core watchdog; "
            "$action = New-ScheduledTaskAction -Execute $pyExe "
            f"-Argument '{deploy_dir_ps}\\integrated_task_worker.py' "
            f"-WorkingDirectory '{deploy_dir_ps}'; "
            "$trigger = New-ScheduledTaskTrigger -AtStartup; "
            "$loggedUser = (Get-CimInstance -ClassName Win32_ComputerSystem).UserName; "
            "if (-not $loggedUser) { $loggedUser = 'BUILTIN\\\\Users' }; "
            "$principal = New-ScheduledTaskPrincipal -UserId $loggedUser "
            "-LogonType Interactive -RunLevel Limited; "
            "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -StartWhenAvailable "
            "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
            "Register-ScheduledTask -TaskName 'ShragaWorker' -Action $action "
            "-Trigger $trigger -Principal $principal -Settings $settings -Force; "
            f"Invoke-WebRequest -Uri '{SHRAGA_AUTH_SCRIPT_URL}' "
            "-OutFile 'C:\\Users\\Public\\Desktop\\Shraga-Authenticate.ps1'; "
            "$ws = New-Object -ComObject WScript.Shell; "
            "$sc = $ws.CreateShortcut('C:\\Users\\Public\\Desktop\\Shraga-Authenticate.lnk'); "
            "$sc.TargetPath = 'powershell.exe'; "
            "$sc.Arguments = '-ExecutionPolicy Bypass -File "
            "C:\\Users\\Public\\Desktop\\Shraga-Authenticate.ps1'; "
            "$sc.Save()"
        )

        body = {
            "tasks": [
                {
                    "name": "DevBox.Catalog/powershell",
                    "parameters": {"command": deploy_command},
                }
            ]
        }

        response = requests.put(
            url,
            json=body,
            headers=self._get_headers(),
            params={"api-version": "2025-04-01-preview"},
            timeout=30,
        )

        if response.status_code in [200, 201, 202]:
            result = response.json()
            print(f"Deploy customization applied to {devbox_name}")
            return result
        elif response.status_code == 409:
            print(f"Deploy customization already exists on {devbox_name}, skipping")
            return {"status": "AlreadyExists", "name": "shraga-deploy"}
        else:
            raise Exception(
                f"Failed to apply deploy customizations: "
                f"{response.status_code} {response.text}"
            )

    def run_command_on_devbox(
        self,
        devbox_name: str,
        command: str,
        user_azure_ad_id: str
    ) -> Dict[str, Any]:
        """
        Run a PowerShell command on a Dev Box

        Note: This requires the Dev Box to have Azure Run Command enabled
        or use Azure DevOps Agent / custom agent for remote execution

        Args:
            devbox_name: Name of the Dev Box
            command: PowerShell command to run
            user_azure_ad_id: Azure AD object ID of the user

        Returns:
            Command execution result
        """
        # TODO: Implement remote command execution
        # Options:
        # 1. Azure Run Command (if Dev Box supports it)
        # 2. Azure DevOps Agent on Dev Box
        # 3. Custom agent polling Dataverse for commands
        # 4. SSH/WinRM (if enabled)

        print(f"Remote command execution not yet implemented")
        print(f"   Command: {command}")
        print(f"   Target: {devbox_name}")

        # For MVP, we can have a small agent on Dev Box that polls Dataverse
        # for commands to execute
        return {
            "status": "pending",
            "command": command,
            "devbox_name": devbox_name
        }

    def request_kiosk_auth(
        self,
        user_id: str,
        user_email: str,
        devbox_name: str,
        user_azure_ad_id: str
    ) -> str:
        """
        Request user to authenticate Claude Code via kiosk mode

        Args:
            user_id: Dataverse user ID
            user_email: User's email
            devbox_name: Name of the Dev Box
            user_azure_ad_id: Azure AD object ID

        Returns:
            Connection URL to send to user
        """
        print(f"🔐 Requesting kiosk authentication for {user_email}...")

        # 1. Trigger kiosk auth script on Dev Box
        # For MVP, this could be done via:
        # - Small agent on Dev Box polling Dataverse
        # - Or manual trigger by user
        # - Or scheduled task that checks a flag file

        # Command to run on Dev Box
        command = "powershell -File C:\\Dev\\shraga-worker\\kiosk-auth-helper.ps1 -Action Start"

        # Queue command for execution
        self.run_command_on_devbox(
            devbox_name=devbox_name,
            command=command,
            user_azure_ad_id=user_azure_ad_id
        )

        # 2. Get connection URL
        info = self.get_devbox_status(user_azure_ad_id, devbox_name)
        connection_url = info.connection_url

        print(f"[OK] Kiosk auth requested")
        print(f"  Connection URL: {connection_url}")

        return connection_url

    def check_claude_auth_status(
        self,
        devbox_name: str,
        user_azure_ad_id: str
    ) -> bool:
        """
        Check if Claude Code is authenticated on the Dev Box

        Args:
            devbox_name: Name of the Dev Box
            user_azure_ad_id: Azure AD object ID

        Returns:
            True if authenticated, False otherwise
        """
        # Command to check auth status
        command = "powershell -File C:\\Dev\\shraga-worker\\kiosk-auth-helper.ps1 -Action Status"

        result = self.run_command_on_devbox(
            devbox_name=devbox_name,
            command=command,
            user_azure_ad_id=user_azure_ad_id
        )

        # Parse result (this depends on how command execution is implemented)
        # For now, return False (assume not authenticated)
        return False

    def delete_devbox(self, user_azure_ad_id: str, devbox_name: str) -> None:
        """
        Delete a Dev Box.

        Sends an HTTP DELETE to the DevCenter API to permanently remove the
        specified Dev Box.  The operation is asynchronous on the server side;
        this method returns once the API has accepted the request.

        Args:
            user_azure_ad_id: Azure AD object ID of the user
            devbox_name: Name of the Dev Box to delete
        """
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes/{devbox_name}"
        )

        response = requests.delete(
            url,
            headers=self._get_headers(),
            params={"api-version": self.api_version},
            timeout=30,
        )

        if response.status_code in [200, 202, 204]:
            print(f"[OK] Dev Box '{devbox_name}' deletion accepted.")
        elif response.status_code == 404:
            print(f"[WARN] Dev Box '{devbox_name}' not found (already deleted?).")
        else:
            raise Exception(
                f"Failed to delete Dev Box '{devbox_name}': "
                f"{response.status_code} {response.text}"
            )


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def _build_parser() -> "argparse.ArgumentParser":
    """Build the argparse parser with all subcommands.

    Separated from ``if __name__`` so that tests can import and exercise the
    parser without running the whole script.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="orchestrator_devbox",
        description="Shraga Dev Box Manager CLI -- provision, manage, and "
                    "connect to Microsoft Dev Boxes.",
    )

    # Common arguments shared by (almost) all subcommands
    parser.add_argument(
        "--endpoint",
        default=None,
        help="DevCenter endpoint URL (or set DEVCENTER_ENDPOINT env var).",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="DevCenter project name (or set DEVCENTER_PROJECT env var).",
    )
    parser.add_argument(
        "--pool",
        default="botdesigner-pool-italynorth",
        help="DevCenter pool name (default: botdesigner-pool-italynorth).",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="Azure AD object ID of the user (or set AZURE_USER_ID env var).",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # -- provision ----------------------------------------------------------
    sp_provision = subparsers.add_parser(
        "provision",
        help="Provision a new Dev Box for a user.",
    )
    sp_provision.add_argument(
        "--name",
        default=None,
        help="Dev Box name. If omitted, the next available shraga-box-NN "
             "name is chosen automatically.",
    )
    sp_provision.add_argument(
        "--email",
        default="user@example.com",
        help="User email (used for logging only).",
    )

    # -- status -------------------------------------------------------------
    sp_status = subparsers.add_parser(
        "status",
        help="Get the current status of a Dev Box.",
    )
    sp_status.add_argument(
        "--name",
        required=True,
        help="Name of the Dev Box.",
    )

    # -- customize ----------------------------------------------------------
    sp_customize = subparsers.add_parser(
        "customize",
        help="Apply standard Shraga customizations (Git, Claude Code, Python) "
             "to a Dev Box.",
    )
    sp_customize.add_argument(
        "--name",
        required=True,
        help="Name of the Dev Box.",
    )

    # -- connect ------------------------------------------------------------
    sp_connect = subparsers.add_parser(
        "connect",
        help="Get the web RDP connection URL for a Dev Box.",
    )
    sp_connect.add_argument(
        "--name",
        required=True,
        help="Name of the Dev Box.",
    )

    # -- delete -------------------------------------------------------------
    sp_delete = subparsers.add_parser(
        "delete",
        help="Delete a Dev Box.",
    )
    sp_delete.add_argument(
        "--name",
        required=True,
        help="Name of the Dev Box to delete.",
    )

    # -- list ---------------------------------------------------------------
    subparsers.add_parser(
        "list",
        help="List all Dev Boxes for the user.",
    )

    return parser


def _resolve_common_args(args) -> tuple:
    """Resolve endpoint / project / user-id from CLI flags or env vars.

    Returns (endpoint, project, pool, user_id).  Raises SystemExit with a
    helpful message when a required value is missing.
    """
    import os as _os
    import sys as _sys

    endpoint = args.endpoint or _os.environ.get("DEVCENTER_ENDPOINT")
    project = args.project or _os.environ.get("DEVCENTER_PROJECT")
    pool = args.pool
    user_id = args.user_id or _os.environ.get("AZURE_USER_ID")

    missing = []
    if not endpoint:
        missing.append("--endpoint / DEVCENTER_ENDPOINT")
    if not project:
        missing.append("--project / DEVCENTER_PROJECT")
    if not user_id:
        missing.append("--user-id / AZURE_USER_ID")

    if missing:
        _sys.stderr.write(
            f"Error: the following required values are missing: "
            f"{', '.join(missing)}\n"
        )
        _sys.exit(1)

    return endpoint, project, pool, user_id


def cli_main(argv: Optional[List[str]] = None) -> int:
    """Run the CLI.  Returns an integer exit code (0 = success).

    Parameters
    ----------
    argv : list of str, optional
        Command-line arguments.  If *None*, ``sys.argv[1:]`` is used.  Passing
        an explicit list makes the function easy to test without monkeypatching
        ``sys.argv``.
    """
    import sys as _sys

    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    endpoint, project, pool, user_id = _resolve_common_args(args)

    manager = DevBoxManager(
        devcenter_endpoint=endpoint,
        project_name=project,
        pool_name=pool,
    )

    # ----- dispatch --------------------------------------------------------

    if args.command == "provision":
        result = manager.provision_devbox(
            user_id, args.email, devbox_name=args.name,
        )
        print(json.dumps(result, indent=2))

    elif args.command == "status":
        info = manager.get_devbox_status(user_id, args.name)
        print(json.dumps({
            "name": info.name,
            "user_id": info.user_id,
            "status": info.status,
            "connection_url": info.connection_url,
            "provisioning_state": info.provisioning_state,
        }, indent=2))

    elif args.command == "customize":
        result = manager.apply_customizations(user_id, args.name)
        print(json.dumps(result, indent=2))

    elif args.command == "connect":
        url = manager.get_connection_url(user_id, args.name)
        print(url)

    elif args.command == "delete":
        manager.delete_devbox(user_id, args.name)

    elif args.command == "list":
        boxes = manager.list_devboxes(user_id)
        if not boxes:
            print("No Dev Boxes found.")
        else:
            for box in boxes:
                name = box.get("name", "<unknown>")
                state = box.get("provisioningState", "?")
                power = box.get("powerState", "?")
                print(f"  {name}  provisioning={state}  power={power}")

    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
