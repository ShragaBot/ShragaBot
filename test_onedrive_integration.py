"""Tests for OneDrive integration across onedrive_utils, integrated_task_worker, and autonomous_agent."""
import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock, call
from datetime import datetime, timezone, timedelta

# Ensure the repo root is on sys.path
REPO_ROOT = Path(__file__).parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from onedrive_utils import (
    find_onedrive_root,
    local_path_to_web_url,
    OneDriveRootNotFoundError,
)
from autonomous_agent import AgentCLI


# ---------------------------------------------------------------------------
# Helper: import IntegratedTaskWorker with all necessary patches
# (mirrors _import_worker from test_integrated_task_worker.py)
# ---------------------------------------------------------------------------

def _import_worker(monkeypatch, tmp_path):
    """Import integrated_task_worker with Azure + agent mocks."""
    monkeypatch.setenv("DATAVERSE_URL", "https://test-org.crm.dynamics.com")
    monkeypatch.setenv("TABLE_NAME", "cr_shraga_tasks")
    monkeypatch.setenv("WEBHOOK_USER", "testuser@example.com")

    # Remove cached module to force re-import with new env vars
    for mod_name in list(sys.modules):
        if mod_name == "integrated_task_worker":
            del sys.modules[mod_name]

    # Mock the AgentCLI import that happens at module level
    mock_agent_module = MagicMock()
    monkeypatch.setitem(sys.modules, "autonomous_agent", mock_agent_module)

    # Patch AzureCliCredential before import (create_credential uses AzureCliCredential)
    with patch("azure.identity.AzureCliCredential") as mock_cred:
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(
            token="fake-token",
            expires_on=(datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
        )
        mock_cred.return_value = mock_cred_inst

        import integrated_task_worker as mod

        return mod, mock_cred_inst


# ===========================================================================
# 1. TestFindOneDriveRoot
# ===========================================================================

class TestFindOneDriveRoot:
    """Tests for onedrive_utils.find_onedrive_root()."""

    def test_explicit_override(self, monkeypatch, tmp_path):
        """ONEDRIVE_SESSIONS_DIR env var is returned when set and the dir exists."""
        override_dir = tmp_path / "my_override"
        override_dir.mkdir()
        monkeypatch.setenv("ONEDRIVE_SESSIONS_DIR", str(override_dir))
        # Clear any other OneDrive env vars to isolate the test
        monkeypatch.delenv("OneDriveCommercial", raising=False)
        monkeypatch.delenv("OneDrive", raising=False)

        result = find_onedrive_root()
        assert result == str(override_dir)

    def test_onedrive_commercial_env(self, monkeypatch, tmp_path):
        """Falls back to OneDriveCommercial env var when ONEDRIVE_SESSIONS_DIR is unset."""
        commercial_dir = tmp_path / "OneDrive - Contoso"
        commercial_dir.mkdir()
        monkeypatch.delenv("ONEDRIVE_SESSIONS_DIR", raising=False)
        monkeypatch.setenv("OneDriveCommercial", str(commercial_dir))
        monkeypatch.delenv("OneDrive", raising=False)

        result = find_onedrive_root()
        assert result == str(commercial_dir)

    def test_onedrive_generic_env(self, monkeypatch, tmp_path):
        """Falls back to OneDrive env var when it matches the business folder pattern."""
        # Business folders typically contain " - OrgName" in their basename
        generic_dir = tmp_path / "OneDrive - Microsoft"
        generic_dir.mkdir()
        monkeypatch.delenv("ONEDRIVE_SESSIONS_DIR", raising=False)
        monkeypatch.delenv("OneDriveCommercial", raising=False)
        monkeypatch.setenv("OneDrive", str(generic_dir))

        result = find_onedrive_root(business_only=True)
        assert result == str(generic_dir)

    def test_no_onedrive_found(self, monkeypatch, tmp_path):
        """Raises OneDriveRootNotFoundError when no env vars, no registry, and no FS match."""
        monkeypatch.delenv("ONEDRIVE_SESSIONS_DIR", raising=False)
        monkeypatch.delenv("OneDriveCommercial", raising=False)
        monkeypatch.delenv("OneDrive", raising=False)

        # Mock registry readers to return nothing
        with patch("onedrive_utils.get_onedrive_account_info", return_value=[]), \
             patch("onedrive_utils.get_sync_engine_mappings", return_value=[]), \
             patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(OneDriveRootNotFoundError):
                find_onedrive_root()

    def test_priority_order(self, monkeypatch, tmp_path):
        """ONEDRIVE_SESSIONS_DIR wins over OneDriveCommercial when both are set."""
        override_dir = tmp_path / "explicit_override"
        override_dir.mkdir()
        commercial_dir = tmp_path / "OneDrive - Commercial"
        commercial_dir.mkdir()

        monkeypatch.setenv("ONEDRIVE_SESSIONS_DIR", str(override_dir))
        monkeypatch.setenv("OneDriveCommercial", str(commercial_dir))

        result = find_onedrive_root()
        assert result == str(override_dir)


# ===========================================================================
# 2. TestLocalPathToWebUrl
# ===========================================================================

class TestLocalPathToWebUrl:
    """Tests for onedrive_utils.local_path_to_web_url()."""

    def test_returns_none_when_no_mapping(self, tmp_path):
        """Returns None when the path is not inside any known sync folder."""
        unmapped_path = tmp_path / "random_folder" / "file.txt"
        unmapped_path.parent.mkdir(parents=True, exist_ok=True)
        unmapped_path.write_text("hello")

        # Mock both registry sources to return empty lists
        with patch("onedrive_utils.get_sync_engine_mappings", return_value=[]), \
             patch("onedrive_utils.get_onedrive_account_info", return_value=[]):
            result = local_path_to_web_url(str(unmapped_path))
            assert result is None


# ===========================================================================
# 3. TestCreateSessionFolder (on IntegratedTaskWorker)
# ===========================================================================

class TestCreateSessionFolder:
    """Tests for IntegratedTaskWorker.create_session_folder()."""

    def test_creates_folder_in_onedrive(self, monkeypatch, tmp_path):
        """When find_onedrive_root succeeds, folder is created under 'Shraga Sessions/'."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        onedrive_root = tmp_path / "OneDrive - Contoso"
        onedrive_root.mkdir()

        with patch("integrated_task_worker.find_onedrive_root", return_value=str(onedrive_root)):
            worker = mod.IntegratedTaskWorker()
            folder = worker.create_session_folder("My Test Task", "abcd1234-5678-9012-3456-789012345678")

        # Folder should be under {onedrive_root}/Shraga Sessions/
        assert folder.parent.name == "Shraga Sessions"
        assert folder.parent.parent == onedrive_root
        assert folder.exists()
        # Folder name should contain the sanitized task name and first 8 chars of task_id
        assert "My Test Task" in folder.name
        assert "abcd1234" in folder.name

    def test_sanitizes_task_name(self, monkeypatch, tmp_path):
        """Special characters are removed from the folder name."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        onedrive_root = tmp_path / "OneDrive - Contoso"
        onedrive_root.mkdir()

        with patch("integrated_task_worker.find_onedrive_root", return_value=str(onedrive_root)):
            worker = mod.IntegratedTaskWorker()
            folder = worker.create_session_folder(
                "Build: API <v2> for auth/login",
                "task0001-0002-0003-000000000001",
            )

        # Verify special characters (:, <, >, /) are replaced with underscores
        assert ":" not in folder.name
        assert "<" not in folder.name
        assert ">" not in folder.name
        assert "/" not in folder.name
        assert folder.exists()

    def test_truncates_long_names(self, monkeypatch, tmp_path):
        """Task names longer than 50 characters are truncated."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        onedrive_root = tmp_path / "OneDrive - Contoso"
        onedrive_root.mkdir()

        long_name = "A" * 100  # 100 chars, exceeds 50 limit

        with patch("integrated_task_worker.find_onedrive_root", return_value=str(onedrive_root)):
            worker = mod.IntegratedTaskWorker()
            folder = worker.create_session_folder(long_name, "taskid00")

        # The sanitized name part (before the _taskid suffix) should be at most 50 chars
        # Full folder name is "{safe_name}_{task_id_short}"
        name_part = folder.name.rsplit("_", 1)[0]  # everything before the last _
        assert len(name_part) <= 50
        assert folder.exists()

    def test_handles_empty_task_name(self, monkeypatch, tmp_path):
        """Empty task_name still creates a folder using just the task_id."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        onedrive_root = tmp_path / "OneDrive - Contoso"
        onedrive_root.mkdir()

        with patch("integrated_task_worker.find_onedrive_root", return_value=str(onedrive_root)):
            worker = mod.IntegratedTaskWorker()
            folder = worker.create_session_folder("", "abcd1234-5678-9012-3456-789012345678")

        assert folder.exists()
        assert folder.parent.name == "Shraga Sessions"
        # With an empty task name, the folder name should still contain the task_id short prefix
        assert "abcd1234" in folder.name

    def test_handles_unicode_task_name(self, monkeypatch, tmp_path):
        """Unicode characters (e.g. emojis) in task_name are stripped or replaced."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        onedrive_root = tmp_path / "OneDrive - Contoso"
        onedrive_root.mkdir()

        with patch("integrated_task_worker.find_onedrive_root", return_value=str(onedrive_root)):
            worker = mod.IntegratedTaskWorker()
            folder = worker.create_session_folder(
                "Deploy \U0001f680 app \u2728 now",
                "unic0de1-0000-0000-0000-000000000000",
            )

        assert folder.exists()
        assert folder.parent.name == "Shraga Sessions"
        # Emojis and special unicode should not appear in the folder name
        assert "\U0001f680" not in folder.name
        assert "\u2728" not in folder.name
        # The alphanumeric parts of the task name should survive
        assert "Deploy" in folder.name
        assert "app" in folder.name
        assert "now" in folder.name
        assert "unic0de1" in folder.name

    def test_handles_very_long_task_id(self, monkeypatch, tmp_path):
        """Only the first 8 characters of a very long task_id are used in the folder name."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        onedrive_root = tmp_path / "OneDrive - Contoso"
        onedrive_root.mkdir()

        long_task_id = "a" * 200  # 200-char task_id

        with patch("integrated_task_worker.find_onedrive_root", return_value=str(onedrive_root)):
            worker = mod.IntegratedTaskWorker()
            folder = worker.create_session_folder("Short Task", long_task_id)

        assert folder.exists()
        assert folder.parent.name == "Shraga Sessions"
        # The task_id portion should be exactly 8 chars ("aaaaaaaa")
        # Folder name format: "{safe_name}_{task_id_short}"
        assert folder.name.endswith("_aaaaaaaa")
        # The full 200-char task_id should NOT appear
        assert long_task_id not in folder.name

    def test_falls_back_to_local_on_onedrive_error(self, monkeypatch, tmp_path):
        """Falls back to work_base_dir when find_onedrive_root raises."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        with patch(
            "integrated_task_worker.find_onedrive_root",
            side_effect=mod.OneDriveRootNotFoundError("No OneDrive found"),
        ):
            worker = mod.IntegratedTaskWorker()
            worker.work_base_dir = tmp_path
            folder = worker.create_session_folder("Fallback Task", "fallback1")

        # Folder should be under work_base_dir with the agent_task_ prefix
        assert folder.parent == tmp_path
        assert "agent_task_" in folder.name
        assert folder.exists()


# ===========================================================================
# 4. TestSetupProjectWithCustomPath (on AgentCLI)
# ===========================================================================

class TestSetupProjectWithCustomPath:
    """Tests for AgentCLI.setup_project with optional project_folder_path."""

    def test_uses_custom_path(self, tmp_path, monkeypatch):
        """When project_folder_path is given, it is used instead of a timestamped folder."""
        monkeypatch.chdir(tmp_path)
        custom_path = tmp_path / "custom_session_folder"

        cli = AgentCLI()
        folder = cli.setup_project(
            "Build something",
            "Tests pass",
            project_folder_path=custom_path,
        )

        assert folder == custom_path
        assert folder.exists()
        # The standard project files should be created inside the custom folder
        assert (folder / "TASK.md").exists()
        assert (folder / "VERIFICATION.md").exists()

    def test_creates_intermediate_dirs(self, tmp_path, monkeypatch):
        """Deep paths are created with parents=True."""
        monkeypatch.chdir(tmp_path)
        deep_path = tmp_path / "level1" / "level2" / "level3" / "session"

        cli = AgentCLI()
        folder = cli.setup_project(
            "Deep task",
            "Done when created",
            project_folder_path=deep_path,
        )

        assert folder == deep_path
        assert folder.exists()
        assert (folder / "TASK.md").exists()

    def test_default_behavior_unchanged(self, tmp_path, monkeypatch):
        """When project_folder_path is not passed, a timestamped folder is created."""
        monkeypatch.chdir(tmp_path)

        cli = AgentCLI()
        folder = cli.setup_project("Default task", "Completed")

        # Default folder name starts with "agent_task_" and contains a timestamp
        assert "agent_task_" in folder.name
        assert folder.exists()
        assert (folder / "TASK.md").exists()


# ===========================================================================
# 5. TestUpdateTaskWithWorkingDir
# ===========================================================================

class TestUpdateTaskWithWorkingDir:
    """Tests for IntegratedTaskWorker.update_task with the workingdir parameter."""

    @patch("integrated_task_worker.requests.patch")
    def test_workingdir_included_in_patch(self, mock_patch, monkeypatch, tmp_path):
        """crb3b_workingdir is included in the PATCH body when workingdir is passed."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())

        worker = mod.IntegratedTaskWorker()
        result = worker.update_task(
            "task-123",
            status="Running",
            workingdir=r"C:\Users\test\OneDrive - Contoso\Shraga Sessions\task_folder",
        )

        assert result is True
        sent_data = mock_patch.call_args[1]["json"]
        assert "crb3b_workingdir" in sent_data
        assert sent_data["crb3b_workingdir"] == r"C:\Users\test\OneDrive - Contoso\Shraga Sessions\task_folder"

    @patch("integrated_task_worker.requests.patch")
    def test_workingdir_not_included_when_none(self, mock_patch, monkeypatch, tmp_path):
        """crb3b_workingdir is NOT in PATCH body when workingdir is not passed."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())

        worker = mod.IntegratedTaskWorker()
        result = worker.update_task("task-123", status="Completed")

        assert result is True
        sent_data = mock_patch.call_args[1]["json"]
        assert "crb3b_workingdir" not in sent_data


# ===========================================================================
# 6. TestResultFormatting
# ===========================================================================

class TestResultFormatting:
    """Tests for the result string formatting in execute_with_autonomous_agent,
    specifically the OneDrive link inclusion logic."""

    def test_result_includes_onedrive_link(self, monkeypatch, tmp_path):
        """When local_path_to_web_url returns a URL, the result string includes it."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        onedrive_root = tmp_path / "OneDrive - Contoso"
        onedrive_root.mkdir()
        session_folder = onedrive_root / "Shraga Sessions" / "test_task_abc12345"
        session_folder.mkdir(parents=True)

        fake_web_url = "https://contoso-my.sharepoint.com/personal/user/Documents/Shraga%20Sessions/test_task"

        # Mock all external dependencies needed by execute_with_autonomous_agent
        mock_agent_instance = MagicMock()
        mock_agent_instance.setup_project.return_value = session_folder
        mock_agent_instance.worker_loop.return_value = ("done", "I finished the work.\nSTATUS: done", {})
        mock_agent_instance.verify_work.return_value = (True, None, {})
        mock_agent_instance.create_summary.return_value = ("# Summary\nTask completed successfully.", {})

        with patch("integrated_task_worker.find_onedrive_root", return_value=str(onedrive_root)), \
             patch("integrated_task_worker.local_path_to_web_url", return_value=fake_web_url), \
             patch("integrated_task_worker.AgentCLI", return_value=mock_agent_instance), \
             patch("integrated_task_worker.requests.patch", return_value=MagicMock(raise_for_status=MagicMock())), \
             patch("integrated_task_worker.requests.post", return_value=MagicMock(raise_for_status=MagicMock())):

            worker = mod.IntegratedTaskWorker()
            worker.current_user_id = "user-123"

            parsed_prompt = {
                "task_description": "Test task",

                "success_criteria": "Tests pass",
            }

            success, result, transcript, _stats = worker.execute_with_autonomous_agent(
                "Test task prompt",
                "abc12345-0000-0000-0000-000000000000",
                "",
                parsed_prompt_data=parsed_prompt,
            )

        assert success is True
        assert fake_web_url in result
        assert "View in OneDrive" in result

    def test_result_fallback_when_no_url(self, monkeypatch, tmp_path):
        """When local_path_to_web_url returns None, the result includes the local path."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        onedrive_root = tmp_path / "OneDrive - Contoso"
        onedrive_root.mkdir()

        mock_agent_instance = MagicMock()
        # setup_project returns whatever path was passed in
        mock_agent_instance.setup_project.side_effect = lambda *a, **kw: kw.get("project_folder_path", tmp_path)
        mock_agent_instance.worker_loop.return_value = ("done", "Work complete.\nSTATUS: done", {})
        mock_agent_instance.verify_work.return_value = (True, None, {})
        mock_agent_instance.create_summary.return_value = ("# Summary\nDone.", {})

        with patch("integrated_task_worker.find_onedrive_root", return_value=str(onedrive_root)), \
             patch("integrated_task_worker.local_path_to_web_url", return_value=None), \
             patch("integrated_task_worker.AgentCLI", return_value=mock_agent_instance), \
             patch("integrated_task_worker.requests.patch", return_value=MagicMock(raise_for_status=MagicMock())), \
             patch("integrated_task_worker.requests.post", return_value=MagicMock(raise_for_status=MagicMock())):

            worker = mod.IntegratedTaskWorker()
            worker.current_user_id = "user-123"

            parsed_prompt = {
                "task_description": "Fallback task",

                "success_criteria": "Tests pass",
            }

            success, result, transcript, _stats = worker.execute_with_autonomous_agent(
                "Fallback task prompt",
                "def45678-0000-0000-0000-000000000000",
                "",
                parsed_prompt_data=parsed_prompt,
            )

        assert success is True
        # When local_path_to_web_url returns None, the result should still contain
        # the session folder path (local fallback) and the OneDrive link text
        assert "Shraga Sessions" in result
        assert "View in OneDrive" in result


# ===========================================================================
# 7. TestEarlyOneDriveUrlWrite
# ===========================================================================

class TestEarlyOneDriveUrlWrite:
    """Tests that the OneDrive URL is written to Dataverse EARLY (before task
    execution), not only on success.  Lines 588-592 of integrated_task_worker.py
    call ``update_task(task_id, workingdir=...)`` followed immediately by
    ``update_task(task_id, onedriveurl=...)`` *before* the worker/verifier loop
    starts."""

    def test_onedriveurl_written_early_with_workingdir(self, monkeypatch, tmp_path):
        """update_task is called with onedriveurl BEFORE worker_loop executes."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        onedrive_root = tmp_path / "OneDrive - Contoso"
        onedrive_root.mkdir()
        session_folder = onedrive_root / "Shraga Sessions" / "test_early_url_abc12345"
        session_folder.mkdir(parents=True)

        fake_web_url = (
            "https://contoso-my.sharepoint.com/personal/user/_layouts/15/"
            "onedrive.aspx?id=/personal/user/Documents/Shraga%20Sessions/test_early_url"
        )

        # Track the order of significant calls so we can assert ordering.
        call_order = []

        mock_agent_instance = MagicMock()
        mock_agent_instance.setup_project.return_value = session_folder
        mock_agent_instance.worker_loop.side_effect = lambda *a, **kw: (
            call_order.append("worker_loop"),
            ("done", "Work complete.\nSTATUS: done", {}),
        )[1]
        mock_agent_instance.verify_work.return_value = (True, None, {})
        mock_agent_instance.create_summary.return_value = ("# Summary\nDone.", {})

        # We need to intercept update_task at the *worker* level so we can
        # record when onedriveurl and workingdir are written.
        original_update_task = mod.IntegratedTaskWorker.update_task

        def tracking_update_task(self_inner, task_id, **kwargs):
            if "workingdir" in kwargs:
                call_order.append("update_task:workingdir")
            if "onedriveurl" in kwargs:
                call_order.append("update_task:onedriveurl")
            return original_update_task(self_inner, task_id, **kwargs)

        with patch("integrated_task_worker.find_onedrive_root", return_value=str(onedrive_root)), \
             patch("integrated_task_worker.local_path_to_web_url", return_value=fake_web_url), \
             patch("integrated_task_worker.AgentCLI", return_value=mock_agent_instance), \
             patch("integrated_task_worker.requests.patch", return_value=MagicMock(raise_for_status=MagicMock())), \
             patch("integrated_task_worker.requests.post", return_value=MagicMock(raise_for_status=MagicMock())), \
             patch.object(mod.IntegratedTaskWorker, "update_task", tracking_update_task):

            worker = mod.IntegratedTaskWorker()
            worker.current_user_id = "user-123"

            parsed_prompt = {
                "task_description": "Early URL task",

                "success_criteria": "Tests pass",
            }

            success, result, transcript, _stats = worker.execute_with_autonomous_agent(
                "Early URL task prompt",
                "abc12345-0000-0000-0000-000000000000",
                "",
                parsed_prompt_data=parsed_prompt,
            )

        # Both workingdir and onedriveurl must appear BEFORE worker_loop
        assert "update_task:workingdir" in call_order, (
            "update_task was never called with workingdir"
        )
        assert "update_task:onedriveurl" in call_order, (
            "update_task was never called with onedriveurl"
        )
        assert "worker_loop" in call_order, (
            "worker_loop was never called"
        )

        workingdir_idx = call_order.index("update_task:workingdir")
        onedriveurl_idx = call_order.index("update_task:onedriveurl")
        worker_loop_idx = call_order.index("worker_loop")

        assert workingdir_idx < worker_loop_idx, (
            f"workingdir written at index {workingdir_idx} but worker_loop at {worker_loop_idx}; "
            "expected workingdir BEFORE worker_loop"
        )
        assert onedriveurl_idx < worker_loop_idx, (
            f"onedriveurl written at index {onedriveurl_idx} but worker_loop at {worker_loop_idx}; "
            "expected onedriveurl BEFORE worker_loop"
        )
        # workingdir should come first, then onedriveurl
        assert workingdir_idx < onedriveurl_idx, (
            f"workingdir at index {workingdir_idx}, onedriveurl at {onedriveurl_idx}; "
            "expected workingdir first, then onedriveurl"
        )

    def test_onedriveurl_written_even_when_task_fails(self, monkeypatch, tmp_path):
        """onedriveurl is written to Dataverse even when the task itself fails."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        onedrive_root = tmp_path / "OneDrive - Contoso"
        onedrive_root.mkdir()
        session_folder = onedrive_root / "Shraga Sessions" / "fail_task_def56789"
        session_folder.mkdir(parents=True)

        fake_web_url = (
            "https://contoso-my.sharepoint.com/personal/user/_layouts/15/"
            "onedrive.aspx?id=/personal/user/Documents/Shraga%20Sessions/fail_task"
        )

        mock_agent_instance = MagicMock()
        mock_agent_instance.setup_project.return_value = session_folder
        # Simulate a task that fails during worker_loop
        mock_agent_instance.worker_loop.side_effect = RuntimeError("Agent crashed")
        mock_agent_instance.verify_work.return_value = (False, "Verification failed", {})
        mock_agent_instance.create_summary.return_value = ("# Summary\nFailed.", {})

        mock_requests_patch = MagicMock(return_value=MagicMock(raise_for_status=MagicMock()))

        with patch("integrated_task_worker.find_onedrive_root", return_value=str(onedrive_root)), \
             patch("integrated_task_worker.local_path_to_web_url", return_value=fake_web_url), \
             patch("integrated_task_worker.AgentCLI", return_value=mock_agent_instance), \
             patch("integrated_task_worker.requests.patch", mock_requests_patch), \
             patch("integrated_task_worker.requests.post", return_value=MagicMock(raise_for_status=MagicMock())):

            worker = mod.IntegratedTaskWorker()
            worker.current_user_id = "user-123"

            parsed_prompt = {
                "task_description": "Failing task",

                "success_criteria": "Tests pass",
            }

            # The method may raise or return failure; either way onedriveurl
            # should already have been written.
            try:
                worker.execute_with_autonomous_agent(
                    "Failing task prompt",
                    "def56789-0000-0000-0000-000000000000",
                    "",
                    parsed_prompt_data=parsed_prompt,
                )
            except Exception:
                pass  # Expected -- task failed

        # Inspect ALL calls to requests.patch and confirm onedriveurl was sent
        onedriveurl_calls = [
            c for c in mock_requests_patch.call_args_list
            if "crb3b_onedriveurl" in c[1].get("json", {})
        ]
        assert len(onedriveurl_calls) >= 1, (
            "update_task was never called with onedriveurl despite task failure; "
            "the URL should be written EARLY before task execution"
        )
        sent_url = onedriveurl_calls[0][1]["json"]["crb3b_onedriveurl"]
        assert sent_url == fake_web_url
