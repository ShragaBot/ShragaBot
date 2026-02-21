"""
Claude Code authentication via Teams messaging.

The authentication must happen ON THE TARGET DEV BOX, not on the Global
Manager's machine.  The primary flow sends the user a web RDP link so they
can open a browser-based session to the dev box and run ``claude /login``
there.  A legacy ``ClaudeAuthManager`` class is retained for backward
compatibility but is no longer used in the main onboarding path.
"""

import subprocess
import re
import time
import logging
import textwrap
from typing import Optional, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Post-provisioning setup script (run on the dev box by the user)
# ---------------------------------------------------------------------------

DEVBOX_SETUP_SCRIPT = textwrap.dedent(r"""
    # ------------------------------------------------------------------
    # Shraga Dev Box Setup Script  (run in PowerShell on the dev box)
    # ------------------------------------------------------------------

    Write-Host "=== Shraga Dev Box Setup ===" -ForegroundColor Cyan

    # 1. Install Python packages
    Write-Host "`n[1/3] Installing Python packages..." -ForegroundColor Yellow
    pip install requests azure-identity watchdog

    # 2. Clone the shraga-worker repo
    Write-Host "`n[2/3] Cloning shraga-worker repo..." -ForegroundColor Yellow
    if (-not (Test-Path "C:\Dev\shraga-worker")) {
        git clone https://github.com/ShragaBot/ShragaBot.git C:\Dev\shraga-worker
    } else {
        Write-Host "  Repo already exists at C:\Dev\shraga-worker -- pulling latest..."
        Push-Location C:\Dev\shraga-worker
        git pull
        Pop-Location
    }

    # 3. Create the scheduled task for the worker
    # Unified config: user-level, Interactive logon, AtStartup, RestartCount 3,
    # RestartInterval 1 min (matches devbox-customization-shraga.yaml gold standard)
    Write-Host "`n[3/3] Creating scheduled task..." -ForegroundColor Yellow
    $action  = New-ScheduledTaskAction -Execute "C:\Python312\python.exe" `
        -Argument "C:\Dev\shraga-worker\integrated_task_worker.py" `
        -WorkingDirectory "C:\Dev\shraga-worker"
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask -TaskName "ShragaWorker" `
        -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
        -Description "Shraga integrated task worker" `
        -Force | Out-Null

    Write-Host "`nSetup complete! Now run:  claude /login" -ForegroundColor Green
""").strip()


# ---------------------------------------------------------------------------
# Shared auth instructions template (used by both GM and PM)
# ---------------------------------------------------------------------------
# This is the single source of truth for post-provisioning auth instructions.
# Both the Global Manager (via get_rdp_auth_message tool) and the Personal
# Manager (via provision_devbox tool) MUST use this exact text to ensure
# users receive identical instructions regardless of which manager handles
# their onboarding.

AUTH_INSTRUCTIONS_TEMPLATE = (
    "Your dev box is ready! Please complete setup:\n\n"
    "Step 1 -- Connect to your dev box:\n"
    "Open this link in your browser: {connection_url}\n\n"
    "Step 2 -- Authenticate:\n"
    "Double-click the Shraga-Authenticate shortcut on the desktop. "
    "It will run az login (browser sign-in) and claude /login "
    "(device code auth). Follow the prompts for both.\n\n"
    "Once you have completed these steps, reply here with done "
    "and I will verify everything is set up."
)


def build_auth_instructions(connection_url: str) -> str:
    """Build the post-provisioning auth instructions for a given connection URL.

    This is the canonical function both GM and PM should use to generate
    auth instructions.  It ensures the user always receives the same message
    format regardless of which manager handles their onboarding.

    The message includes:
    - The web RDP connection URL
    - Instructions to use the Shraga-Authenticate desktop shortcut
    - A prompt to reply 'done' when finished

    Args:
        connection_url: The web RDP URL for the user's dev box.

    Returns:
        The formatted auth instructions string.
    """
    return AUTH_INSTRUCTIONS_TEMPLATE.format(connection_url=connection_url)


class ClaudeAuthManager:
    """Legacy local auth manager -- spawns ``claude /login`` on the *current*
    machine.

    .. warning::
       This authenticates **locally** (i.e. on whatever host runs this code).
       For onboarding a new dev box the ``RemoteDevBoxAuth`` / ``TeamsClaudeAuth``
       classes should be used instead so that ``claude /login`` is executed
       *on the target dev box*, not on the Global Manager.
    """

    def __init__(self):
        self.process = None
        self.auth_url = None
        self.auth_code = None

    def start_auth(self) -> str:
        """
        Start Claude Code authentication **locally** and capture the auth URL.

        Returns:
            Auth URL to send to user

        Raises:
            Exception if auth fails to start
        """
        print("Starting Claude Code authentication...")

        # Start claude /login as subprocess
        self.process = subprocess.Popen(
            ['claude', '/login'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
        )

        # Read output to find the auth URL
        timeout = 30  # 30 seconds timeout
        start_time = time.time()

        auth_url_pattern = r'(https?://[^\s]+)'

        while time.time() - start_time < timeout:
            line = self.process.stdout.readline()

            if not line:
                # Check if process has exited
                if self.process.poll() is not None:
                    raise Exception("Claude /login process exited unexpectedly")
                time.sleep(0.1)
                continue

            print(f"  [Claude] {line.strip()}")

            # Look for URL in output
            match = re.search(auth_url_pattern, line)
            if match:
                self.auth_url = match.group(1)
                print(f"Found auth URL: {self.auth_url}")
                return self.auth_url

        raise TimeoutError("Failed to capture auth URL within 30 seconds")

    def submit_code(self, code: str) -> bool:
        """
        Submit the authorization code to Claude CLI

        Args:
            code: Authorization code from user

        Returns:
            True if successful, False otherwise
        """
        if not self.process:
            raise RuntimeError("Authentication not started. Call start_auth() first.")

        print(f"Submitting code: {code}")

        try:
            # Send code to stdin
            self.process.stdin.write(code + '\n')
            self.process.stdin.flush()

            # Wait for process to complete (with timeout)
            timeout = 10
            start_time = time.time()

            while time.time() - start_time < timeout:
                # Check if process has finished
                if self.process.poll() is not None:
                    # Process finished, check return code
                    if self.process.returncode == 0:
                        print("Authentication successful!")
                        return True
                    else:
                        print(f"Authentication failed (exit code: {self.process.returncode})")
                        # Read any error output
                        stderr = self.process.stderr.read()
                        if stderr:
                            print(f"  Error: {stderr}")
                        return False

                time.sleep(0.1)

            print("Authentication timed out (process still running)")
            return False

        except Exception as e:
            print(f"Error submitting code: {e}")
            return False

    def cancel(self):
        """Cancel the authentication process"""
        if self.process:
            self.process.terminate()
            self.process = None
            print("Authentication cancelled")


class RemoteDevBoxAuth:
    """Directs Claude Code authentication to the *target dev box* via web RDP.

    Instead of spawning ``claude /login`` on the Global Manager's machine,
    this class retrieves the web RDP connection URL for the user's dev box
    and sends them a Teams message with:

    * The RDP link to open in a browser.
    * Instructions to run ``claude /login`` inside that session.
    * A post-provisioning setup script for pip packages, repo clone, and
      the scheduled task.

    The user performs authentication directly on their new dev box.
    """

    def __init__(
        self,
        devbox_manager=None,
        connection_url: Optional[str] = None,
    ):
        """
        Args:
            devbox_manager: A ``DevBoxManager`` instance (used to look up
                            the connection URL if *connection_url* is not
                            provided).
            connection_url: If already known, the web RDP URL can be passed
                            directly and no DevBoxManager lookup is needed.
        """
        self.devbox_manager = devbox_manager
        self._connection_url = connection_url

    def get_connection_url(
        self,
        user_azure_ad_id: str,
        devbox_name: str,
    ) -> str:
        """Return the web RDP URL, fetching it via DevBoxManager if needed."""
        if self._connection_url:
            return self._connection_url
        if not self.devbox_manager:
            raise RuntimeError(
                "Cannot resolve connection URL: no DevBoxManager and no "
                "pre-supplied connection_url."
            )
        url = self.devbox_manager.get_connection_url(user_azure_ad_id, devbox_name)
        self._connection_url = url
        return url

    def build_auth_message(self, connection_url: str) -> str:
        """Build the Teams message that guides the user through dev box auth.

        Delegates to the module-level ``build_auth_instructions`` function
        so that both GM and PM always produce identical output.
        """
        return build_auth_instructions(connection_url)

    def build_setup_script_message(self) -> str:
        """Build a standalone message containing just the setup script."""
        return (
            "**Dev Box Setup Script**\n\n"
            "Run this in PowerShell on your dev box:\n"
            f"```powershell\n{DEVBOX_SETUP_SCRIPT}\n```"
        )


class TeamsClaudeAuth:
    """
    Orchestrates Claude Code authentication via Teams.

    **Primary flow (RDP-first):** After the dev box is provisioned the user
    receives a web RDP link and instructions to run ``claude /login`` on the
    dev box itself.  This is the correct behaviour because authentication
    must happen on the *target* machine, not on the Global Manager.

    **Legacy fallback:** If RDP information is unavailable the class can
    still attempt the old device-code flow (``ClaudeAuthManager``), but
    that path authenticates the *GM* machine which is wrong for onboarding.
    """

    # Timeout (seconds) for the device-code flow before falling back to RDP
    DEVICE_CODE_TIMEOUT = 120

    def __init__(
        self,
        send_message_func: Callable[[str, str], None],
        user_id: str,
        devbox_name: Optional[str] = None,
        user_azure_ad_id: Optional[str] = None,
        devbox_manager=None,
        connection_url: Optional[str] = None,
    ):
        """
        Initialize Teams-based Claude authentication

        Args:
            send_message_func: Function to send Teams message (user_id, message)
            user_id: Teams user ID
            devbox_name: Name of the Dev Box
            user_azure_ad_id: Azure AD object ID
            devbox_manager: Optional pre-configured DevBoxManager instance.
            connection_url: If the web RDP URL is already known it can be
                            passed directly, skipping DevBoxManager lookup.
        """
        self.send_message = send_message_func
        self.user_id = user_id
        self.devbox_name = devbox_name
        self.user_azure_ad_id = user_azure_ad_id
        self.devbox_manager = devbox_manager
        self.connection_url = connection_url
        # Legacy local auth manager (kept for backward compat)
        self.auth_manager = ClaudeAuthManager()
        self._used_rdp_auth = False

    def request_authentication(self) -> bool:
        """
        Request user to authenticate Claude Code via Teams.

        The method now uses an **RDP-first** strategy:

        1. If we have (or can obtain) a web RDP connection URL for the
           target dev box, send the user instructions to authenticate
           directly on the dev box via the browser session.
        2. Only if RDP info is completely unavailable does it fall back
           to the legacy device-code flow (which runs ``claude /login``
           locally on the GM -- generally not what we want).

        Returns:
            True when authentication instructions were sent, False on
            total failure.
        """
        # --- 1. Try RDP-based auth on the target dev box (preferred) --------
        rdp_result = self._initiate_rdp_auth()
        if rdp_result:
            return True

        # --- 2. Legacy device-code fallback (runs on GM -- not ideal) -------
        logger.warning(
            "RDP auth unavailable; falling back to legacy device-code flow "
            "(authenticates the GM, not the dev box)."
        )
        try:
            auth_url = self._try_device_code_flow()

            if auth_url is not None:
                message = (
                    "**Claude Code Authentication Required**\n\n"
                    "Please authenticate Claude Code to enable your Dev Box worker:\n\n"
                    "**Steps:**\n"
                    f"1. Click this link (opens on your device): {auth_url}\n"
                    "2. Sign in with your Anthropic account\n"
                    "3. Copy the authorization code shown in the browser\n"
                    "4. Reply with the code in Teams\n\n"
                    "**Example:** If the browser shows \"Your code is: ABC-123-XYZ\", "
                    "reply with:\n```\nABC-123-XYZ\n```\n\n"
                    "Waiting for your code..."
                )
                self.send_message(self.user_id, message)
                logger.info("Waiting for user to send code via Teams...")
                return True

        except (TimeoutError, Exception) as exc:
            logger.warning("Device code flow failed: %s", exc)

        # --- 3. Total failure ------------------------------------------------
        error = (
            "**Authentication Failed**\n\n"
            "I could not initiate authentication. Please contact your admin."
        )
        self.send_message(self.user_id, error)
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _initiate_rdp_auth(self) -> bool:
        """Send the user a web RDP link to authenticate on the dev box.

        Returns True if the message was sent successfully, False if we
        don't have enough information (no connection URL, no devbox info).
        """
        remote_auth = RemoteDevBoxAuth(
            devbox_manager=self.devbox_manager,
            connection_url=self.connection_url,
        )

        try:
            url = remote_auth.get_connection_url(
                user_azure_ad_id=self.user_azure_ad_id or "",
                devbox_name=self.devbox_name or "",
            )
        except Exception as exc:
            logger.warning("Could not resolve RDP connection URL: %s", exc)
            return False

        message = remote_auth.build_auth_message(url)
        self.send_message(self.user_id, message)
        self._used_rdp_auth = True
        logger.info("Sent RDP auth instructions (url=%s)", url)
        return True

    def _try_device_code_flow(self) -> Optional[str]:
        """Run ``claude /login`` *locally* and return the auth URL, or *None*.

        .. warning::
           This authenticates the **current machine** (the GM), not the
           target dev box.  Prefer ``_initiate_rdp_auth`` for onboarding.
        """
        try:
            auth_url = self.auth_manager.start_auth()
            return auth_url
        except (TimeoutError, Exception) as exc:
            logger.warning("Device code start_auth failed: %s", exc)
            self.auth_manager.cancel()
            return None

    def handle_user_code(self, code: str) -> bool:
        """
        Handle the authorization code sent by user via Teams

        Args:
            code: Authorization code from user

        Returns:
            True if authentication successful
        """
        # Clean up the code (remove whitespace, common formatting)
        code = code.strip()

        # Submit code to Claude CLI
        success = self.auth_manager.submit_code(code)

        if success:
            message = """
**Authentication Complete!**

Your Dev Box worker is now authenticated with Claude Code.
Your pending task will start execution automatically.

Ready to process tasks!
"""
        else:
            message = """
**Authentication Failed**

The code you provided was invalid or expired.

Please try again:
1. Make sure you copied the exact code from the browser
2. Check that the code hasn't expired
3. Reply with the code (without any extra text)
"""

        self.send_message(self.user_id, message)
        return success

    def handle_user_done(self) -> str:
        """Handle the user confirming they completed RDP-based auth.

        Returns a response message to send back to the user.
        """
        return (
            "Thanks for completing the setup! Your dev box is now configured "
            "and Claude Code is authenticated. Your personal assistant is "
            "ready -- you can start creating tasks."
        )

    @property
    def used_rdp_auth(self) -> bool:
        """Whether the auth flow used the RDP path (target dev box)."""
        return self._used_rdp_auth

    # Keep the old name as an alias for backward compatibility
    @property
    def fell_back_to_rdp(self) -> bool:
        """Backward-compatible alias for ``used_rdp_auth``."""
        return self._used_rdp_auth


def get_setup_script() -> str:
    """Return the PowerShell setup script for a new dev box."""
    return DEVBOX_SETUP_SCRIPT


# Example usage in orchestrator
def example_orchestrator_flow():
    """
    Example of how this would be used in the orchestrator.

    Demonstrates the RDP-first auth flow that targets the new dev box.
    """

    # Mock Teams send function
    def send_teams_message(user_id: str, message: str):
        print(f"\n[TEAMS MESSAGE TO {user_id}]")
        print(message)
        print("[END MESSAGE]\n")

    # Initialize with RDP parameters
    user_id = "user@microsoft.com"
    teams_auth = TeamsClaudeAuth(
        send_message_func=send_teams_message,
        user_id=user_id,
        devbox_name="shraga-box-01",
        user_azure_ad_id="b08e39b4-2ac6-4465-a35e-48322efb0f98",
        connection_url="https://devbox.microsoft.com/connect?devbox=shraga-box-01",
    )

    # Request authentication (sends RDP link targeting the dev box)
    print("Starting authentication flow...")
    success = teams_auth.request_authentication()

    if teams_auth.used_rdp_auth:
        print("\n[Auth via RDP -- user completes auth on dev box]")
    elif success:
        # Legacy device-code path
        print("\n[User sends code via Teams]")
        user_code = input("Enter the code you received: ")
        teams_auth.handle_user_code(user_code)
    else:
        print("\n[Authentication flow failed completely]")


if __name__ == "__main__":
    print("Claude Code Teams Authentication - Test")
    print("=" * 60)
    example_orchestrator_flow()
