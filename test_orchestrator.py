"""Tests for orchestrator.py – Orchestrator class"""
import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock, call
from datetime import datetime, timezone, timedelta


def _import_orchestrator(monkeypatch, tmp_path):
    """Import orchestrator module with all external deps mocked."""
    monkeypatch.setenv("DATAVERSE_URL", "https://test-org.crm.dynamics.com")
    monkeypatch.setenv("TABLE_NAME", "cr_shraga_tasks")
    monkeypatch.setenv("WORKERS_TABLE", "cr_shraga_workers")
    monkeypatch.setenv("GIT_BRANCH", "main")
    monkeypatch.setenv("PROVISION_THRESHOLD", "5")

    # Remove cached module
    for mod_name in list(sys.modules):
        if mod_name == "orchestrator":
            del sys.modules[mod_name]

    # Mock DevBoxManager import
    mock_devbox_module = MagicMock()
    monkeypatch.setitem(sys.modules, "orchestrator_devbox", mock_devbox_module)

    mock_cred_inst = MagicMock()
    mock_cred_inst.get_token.return_value = MagicMock(
        token="fake-token",
        expires_on=(datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
    )

    with patch("azure.identity.AzureCliCredential") as mock_cred:
        mock_cred.return_value = mock_cred_inst
        import orchestrator as mod

    # Persist mock so Orchestrator() construction uses it (with block ended)
    monkeypatch.setattr(mod, "create_credential", lambda log_fn=None, max_retries=2: mock_cred_inst)
    return mod, mock_cred_inst


# ===========================================================================
# Initialization
# ===========================================================================

class TestOrchestratorInit:

    def test_init_defaults(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        assert orch.admin_user_id is None
        assert orch.shared_workers == []
        assert orch.worker_round_robin_index == 0

    def test_load_state_from_file(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        state = {
            "admin_user_id": "admin-123",
            "shared_workers": ["w1", "w2"]
        }
        (tmp_path / ".orchestrator_state.json").write_text(json.dumps(state))
        monkeypatch.chdir(tmp_path)

        orch = mod.Orchestrator()
        assert orch.admin_user_id == "admin-123"
        assert orch.shared_workers == ["w1", "w2"]

    def test_load_state_handles_corrupt_file(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        (tmp_path / ".orchestrator_state.json").write_text("not json!")
        monkeypatch.chdir(tmp_path)

        orch = mod.Orchestrator()
        assert orch.admin_user_id is None
        assert orch.shared_workers == []

    def test_load_state_handles_invalid_workers(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        state = {"admin_user_id": "admin-1", "shared_workers": "not-a-list"}
        (tmp_path / ".orchestrator_state.json").write_text(json.dumps(state))
        monkeypatch.chdir(tmp_path)

        orch = mod.Orchestrator()
        assert orch.shared_workers == []


# ===========================================================================
# Token
# ===========================================================================

class TestOrchestratorToken:

    def test_get_token_returns_token(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        assert orch.get_token() == "fake-token"

    def test_token_caching(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.get_token()
        orch.get_token()
        assert mock_cred.get_token.call_count == 1

    def test_token_returns_none_on_error(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_orchestrator(monkeypatch, tmp_path)
        mock_cred.get_token.side_effect = Exception("Auth error")
        orch = mod.Orchestrator()
        orch._token_cache = None
        orch._token_expires = None
        assert orch.get_token() is None


# ===========================================================================
# get_current_user
# ===========================================================================

class TestOrchestratorGetCurrentUser:

    def test_success(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        orch.dv.get.return_value = MagicMock(
            json=lambda: {"UserId": "admin-xyz"}
        )
        uid = orch.get_current_user()
        assert uid == "admin-xyz"
        assert orch.admin_user_id == "admin-xyz"

    def test_failure(self, monkeypatch, tmp_path):
        from dv_client import DataverseError
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        orch.dv.get.side_effect = DataverseError("Network error", 500, "")
        assert orch.get_current_user() is None

    def test_timeout(self, monkeypatch, tmp_path):
        from dv_client import DataverseRetryExhausted
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        orch.dv.get.side_effect = DataverseRetryExhausted("timeout")
        assert orch.get_current_user() is None


# ===========================================================================
# Version management
# ===========================================================================

class TestOrchestratorVersionManagement:

    def test_load_version(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        (orch.repo_path / "VERSION").write_text("3.0.0")
        assert orch.load_version() == "3.0.0"

    def test_load_version_missing(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        vf = orch.repo_path / "VERSION"
        if vf.exists():
            vf.unlink()
        assert orch.load_version() == "unknown"

    @patch("orchestrator.subprocess.run")
    def test_check_for_updates_no_update(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.current_version = "1.0.0"
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout="1.0.0\n"),
        ]
        assert orch.check_for_updates() is False

    @patch("orchestrator.subprocess.run")
    def test_check_for_updates_available(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.current_version = "1.0.0"
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout="2.0.0\n"),
        ]
        assert orch.check_for_updates() is True

    @patch("orchestrator.subprocess.run")
    def test_check_for_updates_fetch_fails(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        assert orch.check_for_updates() is False

    @patch("orchestrator.subprocess.run")
    def test_check_for_updates_timeout(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("git", 60)
        orch = mod.Orchestrator()
        assert orch.check_for_updates() is False


# ===========================================================================
# discover_user_tasks
# ===========================================================================

class TestDiscoverUserTasks:

    def test_discovers_tasks(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        tasks = [
            {"cr_name": "Task1", "cr_shraga_taskid": "t1"},
            {"cr_name": "Task2", "cr_shraga_taskid": "t2"},
        ]
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        orch.dv.get.return_value = MagicMock(
            json=lambda: {"value": tasks}
        )
        orch.admin_user_id = "admin-123"
        result = orch.discover_user_tasks()
        assert len(result) == 2

    def test_returns_empty_on_error(self, monkeypatch, tmp_path):
        from dv_client import DataverseError
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        orch.dv.get.side_effect = DataverseError("Network error", 500, "")
        assert orch.discover_user_tasks() == []

    def test_returns_empty_on_timeout(self, monkeypatch, tmp_path):
        from dv_client import DataverseRetryExhausted
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        orch.dv.get.side_effect = DataverseRetryExhausted("timeout")
        assert orch.discover_user_tasks() == []

    def test_returns_empty_when_auth_fails(self, monkeypatch, tmp_path):
        from dv_client import DataverseRetryExhausted
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        orch.dv.get.side_effect = DataverseRetryExhausted("Token acquisition failed")
        assert orch.discover_user_tasks() == []


# ===========================================================================
# create_admin_mirror
# ===========================================================================

class TestCreateAdminMirror:

    def test_creates_mirror_and_links(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)

        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        # POST returns created mirror
        orch.dv.post.return_value = MagicMock(
            json=lambda: {"cr_shraga_taskid": "mirror-123"},
            headers={}
        )
        orch.dv.patch.return_value = MagicMock()
        orch.admin_user_id = "admin-abc"

        user_task = {
            "cr_shraga_taskid": "user-task-1",
            "cr_name": "Test Task",
            "cr_prompt": "Do something",
        }

        mirror_id = orch.create_admin_mirror(user_task)
        assert mirror_id == "mirror-123"

    def test_returns_none_on_error(self, monkeypatch, tmp_path):
        from dv_client import DataverseError
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)

        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        orch.dv.post.side_effect = DataverseError("Network error", 500, "")

        user_task = {
            "cr_shraga_taskid": "user-task-1",
            "cr_name": "Test",
            "cr_prompt": "",
        }
        assert orch.create_admin_mirror(user_task) is None

    def test_returns_none_when_task_has_no_id(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        assert orch.create_admin_mirror({}) is None

    def test_extracts_id_from_odata_header(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        # Response body doesn't have ID, but header does
        orch.dv.post.return_value = MagicMock(
            json=lambda: {},
            headers={"OData-EntityId": "https://org.crm.dynamics.com/api/data/v9.2/tasks(header-mirror-id)"}
        )
        orch.dv.patch.return_value = MagicMock()

        user_task = {
            "cr_shraga_taskid": "user-task-1",
            "cr_name": "Test",
            "cr_prompt": "",
        }
        mirror_id = orch.create_admin_mirror(user_task)
        assert mirror_id == "header-mirror-id"


# ===========================================================================
# update_task
# ===========================================================================

class TestOrchestratorUpdateTask:

    def test_update_with_friendly_names(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        result = orch.update_task("task-1", status="Running", assigned_worker_id="w-1")
        assert result is True
        # dv.patch(url, data) -- data is positional arg [1]
        sent_data = orch.dv.patch.call_args[0][1]
        assert sent_data["cr_status"] == 5  # Running (integer picklist)
        assert sent_data["cr_assignedworkerid"] == "w-1"

    def test_returns_false_on_error(self, monkeypatch, tmp_path):
        from dv_client import DataverseError
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        orch.dv.patch.side_effect = DataverseError("Error", 500, "")
        assert orch.update_task("task-1", status="Running") is False

    def test_returns_false_with_empty_id(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        assert orch.update_task("", status="Running") is False
        assert orch.update_task(None, status="Running") is False

    def test_returns_false_with_no_fields(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        # All None values
        assert orch.update_task("task-1", status=None) is False

    def test_skips_none_values(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        orch.update_task("task-1", status="Running", assigned_worker_id=None)
        # dv.patch(url, data) -- data is positional arg [1]
        sent_data = orch.dv.patch.call_args[0][1]
        assert "cr_assignedworkerid" not in sent_data


# ===========================================================================
# Round-robin worker assignment
# ===========================================================================

class TestGetNextWorker:

    def test_round_robin(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.shared_workers = ["w1", "w2", "w3"]
        orch.worker_round_robin_index = 0

        assert orch.get_next_worker() == "w1"
        assert orch.get_next_worker() == "w2"
        assert orch.get_next_worker() == "w3"
        assert orch.get_next_worker() == "w1"  # wraps around

    def test_returns_none_when_no_workers(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.shared_workers = []
        assert orch.get_next_worker() is None

    def test_single_worker(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.shared_workers = ["w1"]
        assert orch.get_next_worker() == "w1"
        assert orch.get_next_worker() == "w1"
        assert orch.get_next_worker() == "w1"


# ===========================================================================
# assign_to_worker
# ===========================================================================

class TestAssignToWorker:

    def test_assign_success(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.dv = MagicMock()
        orch.shared_workers = ["w1"]
        result = orch.assign_to_worker("mirror-1", "user-1")
        assert result is True

    def test_assign_fails_no_workers(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.shared_workers = []
        result = orch.assign_to_worker("mirror-1", "user-1")
        assert result is False

    def test_assign_fails_empty_mirror_id(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.shared_workers = ["w1"]
        result = orch.assign_to_worker("", "user-1")
        assert result is False


# ===========================================================================
# process_new_tasks
# ===========================================================================

class TestProcessNewTasks:

    @patch("orchestrator.time.sleep")
    def test_full_pipeline(self, mock_sleep, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)

        orch = mod.Orchestrator()
        orch.dv = MagicMock()

        # discover returns 1 task
        orch.dv.get.return_value = MagicMock(
            json=lambda: {"value": [{
                "cr_shraga_taskid": "user-task-1",
                "cr_name": "Test",
                "cr_prompt": "Do something",
                "_ownerid_value": "user-abc"
            }]}
        )
        # create_admin_mirror POST
        orch.dv.post.return_value = MagicMock(
            json=lambda: {"cr_shraga_taskid": "mirror-1"},
            headers={}
        )
        # update_task PATCHes
        orch.dv.patch.return_value = MagicMock()

        orch.admin_user_id = "admin-1"
        orch.shared_workers = ["w1", "w2"]

        orch.process_new_tasks()

        # POST was called (mirror creation)
        assert orch.dv.post.called
        # PATCH was called (link + assign)
        assert orch.dv.patch.called


# ===========================================================================
# save_state
# ===========================================================================

class TestSaveState:

    def test_save_and_reload(self, monkeypatch, tmp_path):
        mod, _ = _import_orchestrator(monkeypatch, tmp_path)
        orch = mod.Orchestrator()
        orch.admin_user_id = "admin-x"
        orch.shared_workers = ["w1", "w2"]
        orch.save_state()

        # Read back
        state_path = Path(tmp_path / ".orchestrator_state.json")
        # State file should be in cwd (tmp_path)
        data = json.loads(Path(".orchestrator_state.json").read_text())
        assert data["admin_user_id"] == "admin-x"
        assert data["shared_workers"] == ["w1", "w2"]
