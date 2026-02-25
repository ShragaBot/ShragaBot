"""
Shared fixtures and mocks for Shraga worker tests.

All external dependencies (Azure, Dataverse, Claude CLI, Git, etc.) are mocked
so tests can run without any infrastructure.
"""
import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timezone, timedelta

# Ensure the repo root is on sys.path
REPO_ROOT = Path(__file__).parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# UTF-8 encoding fix for Windows
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True, scope="function")
def _ensure_utf8_stdout():
    """Reconfigure stdout/stderr to UTF-8 on Windows so emoji in print() calls
    don't crash with UnicodeEncodeError on cp1252."""
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------------------
# Environment variable fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _set_env_vars(monkeypatch, tmp_path):
    """Set required environment variables for every test."""
    monkeypatch.setenv("DATAVERSE_URL", "https://test-org.crm.dynamics.com")
    monkeypatch.setenv("TABLE_NAME", "cr_shraga_tasks")
    monkeypatch.setenv("WORKERS_TABLE", "cr_shraga_workers")
    monkeypatch.setenv("WEBHOOK_URL", "https://test-webhook.example.com")
    monkeypatch.setenv("WEBHOOK_USER", "testuser@example.com")
    monkeypatch.setenv("GIT_BRANCH", "main")
    monkeypatch.setenv("PROVISION_THRESHOLD", "5")
    monkeypatch.setenv("CONVERSATIONS_TABLE", "cr_shraga_conversations")
    monkeypatch.setenv("USERS_TABLE", "crb3b_shragausers")
    # Make state files use tmp_path so tests don't pollute repo
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# Azure credential mock
# ---------------------------------------------------------------------------

class FakeAccessToken:
    """Mimics azure.core.credentials.AccessToken"""
    def __init__(self, token="fake-token-12345", expires_on=None):
        self.token = token
        self.expires_on = expires_on or (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()


@pytest.fixture
def mock_credential():
    """Return a mock Azure credential that always succeeds."""
    cred = MagicMock()
    cred.get_token.return_value = FakeAccessToken()
    return cred


# ---------------------------------------------------------------------------
# Requests / HTTP mock helpers
# ---------------------------------------------------------------------------

class FakeResponse:
    """Minimal requests.Response stand-in."""
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.headers = headers or {}
        self.content = b'{}' if json_data is not None else (text.encode() if text else b'')

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            exc = requests.exceptions.HTTPError(response=self)
            exc.response = self
            raise exc


# ---------------------------------------------------------------------------
# Sample Dataverse data
# ---------------------------------------------------------------------------

SAMPLE_USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SAMPLE_ADMIN_ID = "11111111-2222-3333-4444-555555555555"
SAMPLE_TASK_ID = "task-0001-0002-0003-000000000001"
SAMPLE_MIRROR_ID = "mirr-0001-0002-0003-000000000001"
SAMPLE_WORKER_ID = "work-0001-0002-0003-000000000001"
SAMPLE_WORKER_ID_2 = "work-0001-0002-0003-000000000002"

SAMPLE_TASK = {
    "cr_shraga_taskid": SAMPLE_TASK_ID,
    "cr_name": "Test Task",
    "cr_prompt": "Create a hello world script",
    "cr_status": "Pending",
    "cr_ismirror": False,
    "cr_mirrortaskid": None,
    "cr_transcript": "",
    "cr_result": "",
    "_ownerid_value": SAMPLE_USER_ID,
    "createdon": "2026-02-09T10:00:00Z",
}

SAMPLE_WHOAMI = {
    "UserId": SAMPLE_USER_ID,
    "BusinessUnitId": "bu-0001",
    "OrganizationId": "org-0001",
}


# ---------------------------------------------------------------------------
# Subprocess mock helpers
# ---------------------------------------------------------------------------

class FakeCompletedProcess:
    """Mimics subprocess.CompletedProcess"""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakePopen:
    """Mimics subprocess.Popen for Claude CLI calls"""
    def __init__(self, stdout_data="", stderr_data="", returncode=0):
        self._stdout_data = stdout_data
        self._stderr_data = stderr_data
        self.returncode = returncode
        self.stdin = MagicMock()
        self.stdout = MagicMock()
        self.stderr = MagicMock()
        self._poll_count = 0

    def communicate(self, input=None, timeout=None):
        return self._stdout_data, self._stderr_data

    def poll(self):
        self._poll_count += 1
        if self._poll_count >= 2:
            return self.returncode
        return None

    def kill(self):
        pass

    def terminate(self):
        pass


# ---------------------------------------------------------------------------
# Tmp path helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def version_file(tmp_path):
    """Create a VERSION file in tmp_path."""
    vf = tmp_path / "VERSION"
    vf.write_text("1.0.0")
    return vf
