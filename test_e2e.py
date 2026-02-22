"""
End-to-end integration tests for the Shraga system.

These tests exercise the full flow: Orchestrator discovers tasks, creates mirrors,
assigns to workers, and workers execute tasks through the autonomous agent loop.

All external dependencies are mocked (Azure, Dataverse, Claude CLI, Git).
"""
import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock, call
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_modules(monkeypatch, tmp_path):
    """Import both orchestrator and worker with mocked externals."""
    monkeypatch.setenv("DATAVERSE_URL", "https://test-org.crm.dynamics.com")
    monkeypatch.setenv("TABLE_NAME", "cr_shraga_tasks")
    monkeypatch.setenv("WORKERS_TABLE", "cr_shraga_workers")
    monkeypatch.setenv("WEBHOOK_URL", "https://test-webhook.example.com")
    monkeypatch.setenv("WEBHOOK_USER", "testuser@example.com")
    monkeypatch.setenv("GIT_BRANCH", "main")
    monkeypatch.setenv("PROVISION_THRESHOLD", "5")

    # Clear cached modules
    for mod_name in list(sys.modules):
        if mod_name in ("orchestrator", "integrated_task_worker"):
            del sys.modules[mod_name]

    # Mock external modules
    mock_devbox = MagicMock()
    mock_agent = MagicMock()
    monkeypatch.setitem(sys.modules, "orchestrator_devbox", mock_devbox)
    monkeypatch.setitem(sys.modules, "autonomous_agent", mock_agent)

    with patch("azure.identity.DefaultAzureCredential") as mock_cred:
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(
            token="fake-token",
            expires_on=(datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
        )
        mock_cred.return_value = mock_cred_inst

        import orchestrator as orch_mod
        import integrated_task_worker as worker_mod

        return orch_mod, worker_mod, mock_cred_inst


# ===========================================================================
# E2E: Orchestrator discovers task, creates mirror, assigns to worker
# ===========================================================================

class TestE2EOrchestratorPipeline:

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.requests.patch")
    @patch("orchestrator.requests.post")
    @patch("orchestrator.requests.get")
    def test_discover_mirror_assign(self, mock_get, mock_post, mock_patch, mock_sleep,
                                     monkeypatch, tmp_path):
        """Full orchestrator pipeline: discover -> mirror -> assign"""
        orch_mod, _, _ = _import_modules(monkeypatch, tmp_path)

        user_task = {
            "cr_shraga_taskid": "user-task-001",
            "cr_name": "Build REST API",
            "cr_prompt": "Create a REST API for user authentication",
            "cr_status": "Pending",
            "cr_ismirror": False,
            "cr_mirrortaskid": None,
            "_ownerid_value": "user-aaa-bbb",
        }

        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [user_task]}
        )
        mock_post.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"cr_shraga_taskid": "mirror-001"},
            headers={}
        )
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())

        orch = orch_mod.Orchestrator()
        orch.admin_user_id = "admin-xyz"
        orch.shared_workers = ["worker-1", "worker-2"]

        orch.process_new_tasks()

        # Mirror was created
        assert mock_post.called
        post_data = mock_post.call_args[1]["json"]
        assert post_data["cr_ismirror"] is True
        assert post_data["cr_mirroroftaskid"] == "user-task-001"

        # Task was assigned (PATCH for link + PATCH for assignment)
        assert mock_patch.call_count >= 2

    @patch("orchestrator.time.sleep")
    @patch("orchestrator.requests.get")
    def test_no_tasks_discovered(self, mock_get, mock_sleep, monkeypatch, tmp_path):
        """When no tasks exist, nothing happens"""
        orch_mod, _, _ = _import_modules(monkeypatch, tmp_path)

        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        orch = orch_mod.Orchestrator()
        orch.admin_user_id = "admin-xyz"
        orch.shared_workers = ["worker-1"]

        orch.process_new_tasks()
        # No POST for mirror creation
        # (we only patched GET, so no POST mock to check - this just verifies no crash)


# ===========================================================================
# E2E: Worker token + poll + update lifecycle
# ===========================================================================

class TestE2EWorkerLifecycle:

    @patch("integrated_task_worker.requests.get")
    def test_worker_authenticates_and_polls(self, mock_get, monkeypatch, tmp_path):
        """Worker authenticates, gets user ID, and polls for tasks"""
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        # WhoAmI response then task poll response
        mock_get.side_effect = [
            MagicMock(
                raise_for_status=MagicMock(),
                json=lambda: {"UserId": "worker-user-id"}
            ),
            MagicMock(
                raise_for_status=MagicMock(),
                json=lambda: {"value": []}
            ),
        ]

        worker = worker_mod.IntegratedTaskWorker()
        assert worker.get_current_user() == "worker-user-id"
        tasks = worker.poll_pending_tasks()
        assert tasks == []

    @patch("integrated_task_worker.requests.patch")
    def test_worker_updates_task_status(self, mock_patch, monkeypatch, tmp_path):
        """Worker can update task status in Dataverse"""
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())

        worker = worker_mod.IntegratedTaskWorker()
        result = worker.update_task("task-001", status="Running", status_message="Running")
        assert result is True

        sent_data = mock_patch.call_args[1]["json"]
        assert sent_data["cr_status"] == 5

    @patch("integrated_task_worker.requests.post")
    def test_worker_sends_webhook_message(self, mock_post, monkeypatch, tmp_path):
        """Worker can send messages through webhook"""
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())

        worker = worker_mod.IntegratedTaskWorker()
        result = worker.send_to_webhook("Task started!")
        assert result is True


# ===========================================================================
# E2E: Worker transcript management
# ===========================================================================

class TestE2ETranscript:

    def test_transcript_accumulates(self, monkeypatch, tmp_path):
        """Transcript accumulates entries across multiple appends"""
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        worker = worker_mod.IntegratedTaskWorker()

        t = ""
        t = worker.append_to_transcript(t, "system", "Task started")
        t = worker.append_to_transcript(t, "worker", "Working on it")
        t = worker.append_to_transcript(t, "verifier", "Looks good")
        t = worker.append_to_transcript(t, "summarizer", "Done")

        lines = t.strip().split("\n")
        assert len(lines) == 4

        entries = [json.loads(line) for line in lines]
        assert entries[0]["from"] == "system"
        assert entries[1]["from"] == "worker"
        assert entries[2]["from"] == "verifier"
        assert entries[3]["from"] == "summarizer"

    def test_transcript_entries_have_timestamps(self, monkeypatch, tmp_path):
        """Each transcript entry has an ISO timestamp"""
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        worker = worker_mod.IntegratedTaskWorker()
        t = worker.append_to_transcript("", "system", "Hello")
        entry = json.loads(t)
        assert "time" in entry
        # Verify it's a valid ISO format
        datetime.fromisoformat(entry["time"])


# ===========================================================================
# E2E: Version checking across components
# ===========================================================================

class TestE2EVersionChecking:

    @patch("orchestrator.subprocess.run")
    def test_orchestrator_detects_update(self, mock_run, monkeypatch, tmp_path):
        orch_mod, _, _ = _import_modules(monkeypatch, tmp_path)

        orch = orch_mod.Orchestrator()
        orch.current_version = "1.0.0"

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=0, stdout="2.0.0\n"),  # git show VERSION
        ]

        assert orch.check_for_updates() is True

    def test_worker_updater_exists(self, monkeypatch, tmp_path):
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)
        worker = worker_mod.IntegratedTaskWorker()
        assert hasattr(worker, 'updater')
        assert worker.updater.current_branch is not None

    def test_worker_updater_should_check_first_time(self, monkeypatch, tmp_path):
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)
        worker = worker_mod.IntegratedTaskWorker()
        # First check should always return True (last_check is None)
        assert worker.updater.should_check() is True

    def test_worker_updater_respects_interval(self, monkeypatch, tmp_path):
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)
        worker = worker_mod.IntegratedTaskWorker()
        from datetime import datetime, timezone
        worker.updater.last_check = datetime.now(timezone.utc)
        # Just checked — should not check again
        assert worker.updater.should_check() is False


# ===========================================================================
# E2E: Worker task processing (with mocked AgentCLI)
# ===========================================================================

class TestE2ETaskProcessing:

    @patch("integrated_task_worker.subprocess.run")
    @patch("integrated_task_worker.requests.get")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.patch")
    @patch("integrated_task_worker.subprocess.Popen")
    def test_process_task_success(self, mock_popen, mock_patch, mock_post,
                                   mock_get, mock_run, monkeypatch, tmp_path):
        """Worker processes a task successfully end-to-end"""
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        # Mock parse_prompt_with_llm via Popen
        parsed = {
            "task_description": "Create hello world",

            "success_criteria": "Script runs"
        }
        mock_popen.return_value = MagicMock(
            communicate=MagicMock(return_value=(json.dumps({"result": json.dumps(parsed)}), "")),
            returncode=0
        )

        # Mock PATCH (task updates + claim) and POST (webhook messages)
        mock_patch.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())

        # Mock GET (is_devbox_busy returns not busy, promote_queued_tasks returns empty)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        # Mock git operations
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="abc1234\n", stderr=""),  # git rev-parse
        ]

        worker = worker_mod.IntegratedTaskWorker()

        # Mock the execute_with_autonomous_agent to simulate success
        with patch.object(worker, "execute_with_autonomous_agent") as mock_exec:
            mock_exec.return_value = (True, "Task completed!", "transcript-data", {})

            task = {
                "cr_shraga_taskid": "task-e2e-001",
                "cr_name": "E2E Test Task",
                "cr_prompt": "Create a hello world script",
                "cr_transcript": "",
                "@odata.etag": 'W/"e2e-etag-001"',
            }

            result = worker.process_task(task)
            assert result is True

    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.patch")
    @patch("integrated_task_worker.subprocess.Popen")
    def test_process_task_failure(self, mock_popen, mock_patch, mock_post,
                                   monkeypatch, tmp_path):
        """Worker handles task failure"""
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        parsed = {
            "task_description": "Impossible task",

            "success_criteria": "N/A"
        }
        mock_popen.return_value = MagicMock(
            communicate=MagicMock(return_value=(json.dumps({"result": json.dumps(parsed)}), "")),
            returncode=0
        )
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())

        worker = worker_mod.IntegratedTaskWorker()

        with patch.object(worker, "execute_with_autonomous_agent") as mock_exec:
            mock_exec.return_value = (False, "Blocked: Need API key", "transcript", {})

            task = {
                "cr_shraga_taskid": "task-e2e-002",
                "cr_name": "Failing Task",
                "cr_prompt": "Do impossible thing",
                "cr_transcript": "",
            }

            result = worker.process_task(task)
            assert result is False


# ===========================================================================
# E2E: Orchestrator round-robin distribution
# ===========================================================================

class TestE2ERoundRobin:

    def test_tasks_distributed_evenly(self, monkeypatch, tmp_path):
        """Multiple tasks are distributed across workers evenly"""
        orch_mod, _, _ = _import_modules(monkeypatch, tmp_path)

        orch = orch_mod.Orchestrator()
        orch.shared_workers = ["w1", "w2", "w3"]
        orch.worker_round_robin_index = 0

        assignments = []
        for _ in range(9):
            w = orch.get_next_worker()
            assignments.append(w)

        assert assignments == ["w1", "w2", "w3", "w1", "w2", "w3", "w1", "w2", "w3"]


# ===========================================================================
# E2E: State persistence across restarts
# ===========================================================================

class TestE2EStatePersistence:

    def test_orchestrator_state_persists(self, monkeypatch, tmp_path):
        """Orchestrator state survives restart"""
        orch_mod, _, _ = _import_modules(monkeypatch, tmp_path)

        orch1 = orch_mod.Orchestrator()
        orch1.admin_user_id = "admin-persist-test"
        orch1.shared_workers = ["w1", "w2"]
        orch1.save_state()

        orch2 = orch_mod.Orchestrator()
        assert orch2.admin_user_id == "admin-persist-test"
        assert orch2.shared_workers == ["w1", "w2"]

    def test_worker_state_persists(self, monkeypatch, tmp_path):
        """Worker state survives restart"""
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        w1 = worker_mod.IntegratedTaskWorker()
        w1.current_user_id = "worker-persist-test"
        w1.save_state()

        w2 = worker_mod.IntegratedTaskWorker()
        assert w2.current_user_id == "worker-persist-test"


# ===========================================================================
# E2E: Git commit results
# ===========================================================================

class TestE2EGitCommitResults:

    @patch("integrated_task_worker.subprocess.run")
    def test_commit_creates_sha(self, mock_run, monkeypatch, tmp_path):
        """Worker commits results and gets a SHA"""
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="deadbeef1234\n", stderr=""),  # git rev-parse
        ]

        worker = worker_mod.IntegratedTaskWorker()
        sha = worker.commit_task_results("task-git-001", tmp_path)
        assert sha == "deadbeef1234"

    @patch("integrated_task_worker.subprocess.run")
    def test_commit_handles_no_changes(self, mock_run, monkeypatch, tmp_path):
        """Worker handles 'nothing to commit' gracefully"""
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1, stdout="nothing to commit, working tree clean", stderr=""),
        ]

        worker = worker_mod.IntegratedTaskWorker()
        sha = worker.commit_task_results("task-git-002", tmp_path)
        assert sha is None


# ===========================================================================
# E2E: Error handling across components
# ===========================================================================

class TestE2EErrorHandling:

    def test_orchestrator_handles_no_token(self, monkeypatch, tmp_path):
        """Orchestrator degrades gracefully without token"""
        orch_mod, _, mock_cred = _import_modules(monkeypatch, tmp_path)
        mock_cred.get_token.side_effect = Exception("Auth failed")

        orch = orch_mod.Orchestrator()
        orch._token_cache = None
        orch._token_expires = None

        # Should return empty list, not crash
        assert orch.discover_user_tasks() == []
        assert orch.get_current_user() is None

    def test_worker_handles_no_token(self, monkeypatch, tmp_path):
        """Worker degrades gracefully without token"""
        _, worker_mod, mock_cred = _import_modules(monkeypatch, tmp_path)
        mock_cred.get_token.side_effect = Exception("Auth failed")

        worker = worker_mod.IntegratedTaskWorker()
        worker._token_cache = None
        worker._token_expires = None

        assert worker.get_token() is None
        assert worker.poll_pending_tasks() == []

    @patch("orchestrator.requests.get")
    def test_orchestrator_handles_dataverse_error(self, mock_get, monkeypatch, tmp_path):
        """Orchestrator handles Dataverse API errors"""
        orch_mod, _, _ = _import_modules(monkeypatch, tmp_path)
        mock_get.side_effect = ConnectionError("Connection refused")

        orch = orch_mod.Orchestrator()
        assert orch.discover_user_tasks() == []

    @patch("integrated_task_worker.requests.get")
    def test_worker_handles_poll_error(self, mock_get, monkeypatch, tmp_path):
        """Worker handles poll errors gracefully"""
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)
        mock_get.side_effect = ConnectionError("Connection refused")

        worker = worker_mod.IntegratedTaskWorker()
        worker.current_user_id = "user-1"
        assert worker.poll_pending_tasks() == []


# ===========================================================================
# E2E: Full orchestrator + worker flow (simulated)
# ===========================================================================

class TestE2EFullFlow:

    def test_full_flow_orchestrator_to_worker(self, monkeypatch, tmp_path):
        """
        Simulated full flow:
        1. Orchestrator discovers user task
        2. Orchestrator creates admin mirror
        3. Orchestrator assigns to worker
        4. Worker picks up task
        5. Worker processes task
        6. Worker commits results
        """
        orch_mod, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        # --- Orchestrator phase ---
        user_task = {
            "cr_shraga_taskid": "user-flow-001",
            "cr_name": "Full Flow Test",
            "cr_prompt": "Create a calculator app",
            "cr_status": "Pending",
            "cr_ismirror": False,
            "cr_mirrortaskid": None,
            "_ownerid_value": "user-flow-aaa",
        }

        with patch("orchestrator.requests.get") as orch_get, \
             patch("orchestrator.requests.post") as orch_post, \
             patch("orchestrator.requests.patch") as orch_patch, \
             patch("orchestrator.time.sleep"):

            orch_get.return_value = MagicMock(
                raise_for_status=MagicMock(),
                json=lambda: {"value": [user_task]}
            )
            orch_post.return_value = MagicMock(
                raise_for_status=MagicMock(),
                json=lambda: {"cr_shraga_taskid": "mirror-flow-001"},
                headers={}
            )
            orch_patch.return_value = MagicMock(raise_for_status=MagicMock())

            orch = orch_mod.Orchestrator()
            orch.admin_user_id = "admin-flow"
            orch.shared_workers = ["worker-flow-1"]

            orch.process_new_tasks()

            # Verify mirror was created
            assert orch_post.called
            mirror_data = orch_post.call_args[1]["json"]
            assert mirror_data["cr_ismirror"] is True

        # --- Worker phase ---
        with patch("integrated_task_worker.requests.patch") as worker_patch, \
             patch("integrated_task_worker.requests.post") as worker_post, \
             patch("integrated_task_worker.requests.get") as worker_get, \
             patch("integrated_task_worker.subprocess.Popen") as worker_popen, \
             patch("integrated_task_worker.subprocess.run") as worker_run:

            parsed = {
                "task_description": "Create a calculator app",
    
                "success_criteria": "Calculator works"
            }
            worker_popen.return_value = MagicMock(
                communicate=MagicMock(return_value=(json.dumps({"result": json.dumps(parsed)}), "")),
                returncode=0
            )
            worker_patch.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
            worker_post.return_value = MagicMock(raise_for_status=MagicMock())
            # Mock GET (is_devbox_busy returns not busy, promote_queued_tasks returns empty)
            worker_get.return_value = MagicMock(
                raise_for_status=MagicMock(),
                json=lambda: {"value": []}
            )
            worker_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="commit-sha-xyz\n", stderr=""),
            ]

            worker = worker_mod.IntegratedTaskWorker()

            with patch.object(worker, "execute_with_autonomous_agent") as mock_exec:
                mock_exec.return_value = (True, "Calculator created!", "transcript", {})

                mirror_task = {
                    "cr_shraga_taskid": "mirror-flow-001",
                    "cr_name": "Full Flow Test",
                    "cr_prompt": "Create a calculator app",
                    "cr_transcript": "",
                    "@odata.etag": 'W/"flow-etag-001"',
                }

                result = worker.process_task(mirror_task)
                assert result is True

            # Verify worker updated task status
            assert worker_patch.called


# ===========================================================================
# E2E: Cancellation flow
# ===========================================================================

class TestE2ECancelRunningTask:
    """Tests for the cooperative cancellation flow.

    The cancellation flow works as follows:
    1. A task is submitted and claimed (status -> Running)
    2. Inside execute_with_autonomous_agent, is_task_canceled() is checked
       at the top of each Worker/Verifier iteration and after the worker
       phase completes (before verification).
    3. When is_task_canceled returns True, the method writes a session
       summary with terminal_status="canceled", sends a webhook message,
       and returns (False, "Task canceled by user", transcript, stats).
    4. process_task then marks the task as Failed and sends a failure
       notification.
    5. The worker object remains functional and can process additional tasks.
    """

    @patch("integrated_task_worker.requests.get")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.patch")
    @patch("integrated_task_worker.subprocess.Popen")
    def test_e2e_cancel_running_task(self, mock_popen, mock_patch, mock_post,
                                      mock_get, monkeypatch, tmp_path):
        """
        Full E2E cancellation flow:
        1. Submit task and let it be claimed (Running)
        2. Simulate cancel by having is_task_canceled return True
        3. Verify task ends with Canceled message
        4. Verify worker object is still functional afterward
        """
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        # --- Mock Popen for parse_prompt_with_llm ---
        parsed = {
            "task_description": "Build a feature",
            "success_criteria": "Feature works correctly"
        }
        mock_popen.return_value = MagicMock(
            communicate=MagicMock(return_value=(
                json.dumps({"result": json.dumps(parsed)}), ""
            )),
            returncode=0
        )

        # --- Mock PATCH (claim_task + status updates) ---
        mock_patch.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock()
        )

        # --- Mock POST (webhook messages) ---
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())

        # --- Mock GET ---
        # is_devbox_busy returns "not busy" (empty value list)
        # is_task_canceled will be patched separately on the instance
        # promote_queued_tasks returns empty list
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = worker_mod.IntegratedTaskWorker()

        # Patch is_task_canceled to return True on first call (simulating
        # the user canceling the task while it's running). This is the
        # first checkpoint inside execute_with_autonomous_agent, at the
        # top of the while loop before the worker phase runs.
        with patch.object(worker, "is_task_canceled", return_value=True) as mock_canceled:
            # Also patch create_session_folder to use tmp_path
            session_folder = tmp_path / "cancel_session"
            session_folder.mkdir()
            with patch.object(worker, "create_session_folder", return_value=session_folder):
                # Patch write_session_summary and write_session_log to avoid
                # complex filesystem interactions; we verify they are called.
                with patch.object(worker, "write_session_summary", return_value={}) as mock_write_summary, \
                     patch.object(worker, "write_session_log") as mock_write_log, \
                     patch.object(worker, "write_result_and_transcript_files") as mock_write_files:

                    task = {
                        "cr_shraga_taskid": "task-cancel-001",
                        "cr_name": "Task To Cancel",
                        "cr_prompt": "Do something that will be canceled",
                        "cr_transcript": "",
                        "@odata.etag": 'W/"cancel-etag-001"',
                    }

                    result = worker.process_task(task)

                    # --- Assertions ---

                    # 1. process_task returns False (task did not succeed)
                    assert result is False

                    # 2. is_task_canceled was called (cancellation was checked)
                    assert mock_canceled.called

                    # 3. Session summary was written with "canceled" terminal status
                    assert mock_write_summary.called
                    summary_kwargs = mock_write_summary.call_args
                    assert summary_kwargs[1]["terminal_status"] == "canceled"

                    # 4. Result/transcript files were written
                    assert mock_write_files.called

                    # 5. Webhook messages were sent (at least one for cancel notification)
                    assert mock_post.called
                    webhook_calls = mock_post.call_args_list
                    # Find the cancellation-related webhook message
                    webhook_bodies = [
                        c[1]["json"]["cr_content"]
                        for c in webhook_calls
                        if "json" in c[1] and "cr_content" in c[1].get("json", {})
                    ]
                    cancel_messages = [
                        body for body in webhook_bodies
                        if "cancel" in body.lower() or "Cancel" in body
                    ]
                    assert len(cancel_messages) >= 1, (
                        f"Expected at least one cancel webhook message, "
                        f"got messages: {webhook_bodies}"
                    )

                    # 6. Task status was updated (PATCH was called for claim + status updates)
                    assert mock_patch.called
                    patch_calls = mock_patch.call_args_list
                    # Look for the final status update with STATUS_FAILED
                    patch_bodies = [
                        c[1]["json"]
                        for c in patch_calls
                        if "json" in c[1]
                    ]
                    failed_updates = [
                        body for body in patch_bodies
                        if body.get("cr_status") == 8
                    ]
                    assert len(failed_updates) >= 1, (
                        f"Expected at least one PATCH setting status to 'Failed', "
                        f"got bodies: {patch_bodies}"
                    )

                    # 7. The result message includes cancel info
                    error_results = [
                        body.get("cr_result", "")
                        for body in patch_bodies
                        if "cr_result" in body
                    ]
                    cancel_results = [
                        r for r in error_results
                        if "cancel" in r.lower() or "Cancel" in r
                    ]
                    assert len(cancel_results) >= 1, (
                        f"Expected cr_result to mention cancellation, "
                        f"got: {error_results}"
                    )

        # 8. Worker is still functional after cancellation -- it can process
        #    another task. Verify the worker object is in a clean state.
        assert worker.current_task_id is None, (
            "current_task_id should be cleared after task processing"
        )

        # Verify the worker can still make API calls (e.g., update_task)
        mock_patch.reset_mock()
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        update_ok = worker.update_task("another-task-id", status="Running", status_message="Test")
        assert update_ok is True

    @patch("integrated_task_worker.requests.get")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.patch")
    @patch("integrated_task_worker.subprocess.Popen")
    def test_e2e_cancel_after_worker_phase_before_verification(
        self, mock_popen, mock_patch, mock_post, mock_get,
        monkeypatch, tmp_path
    ):
        """
        Cancel detected after worker phase completes but before verification.

        This tests the second cancellation checkpoint: the worker phase returns
        STATUS: done, and then is_task_canceled is checked before the verifier
        runs. If canceled, the task should terminate without running verification.
        """
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        # --- Mock Popen for parse_prompt_with_llm ---
        parsed = {
            "task_description": "Build another feature",
            "success_criteria": "Tests pass"
        }
        mock_popen.return_value = MagicMock(
            communicate=MagicMock(return_value=(
                json.dumps({"result": json.dumps(parsed)}), ""
            )),
            returncode=0
        )

        mock_patch.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock()
        )
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        # --- Mock AgentCLI instance ---
        mock_agent_instance = MagicMock()
        session_folder = tmp_path / "cancel_session_2"
        session_folder.mkdir()
        mock_agent_instance.setup_project.return_value = session_folder
        mock_agent_instance.worker_loop.return_value = (
            "done",
            "Worker output: feature built",
            {"cost_usd": 0.01, "duration_ms": 5000, "num_turns": 3, "session_id": "sess-1"}
        )
        # verify_work should NOT be called because cancel happens first
        mock_agent_instance.verify_work.return_value = (
            True, "Approved", {"cost_usd": 0.0, "duration_ms": 0, "num_turns": 0}
        )

        worker = worker_mod.IntegratedTaskWorker()

        # is_task_canceled: False on first call (before worker phase),
        # True on second call (after worker phase, before verification).
        cancel_call_count = {"n": 0}

        def is_canceled_side_effect(task_id):
            cancel_call_count["n"] += 1
            # First call: not canceled (allows worker phase to run)
            # Second call: canceled (stops before verification)
            return cancel_call_count["n"] >= 2

        # Patch AgentCLI on the actual worker_mod (not via decorator, since
        # _import_modules re-imports the module after decorators apply)
        with patch.object(worker_mod, "AgentCLI", return_value=mock_agent_instance), \
             patch.object(worker_mod, "local_path_to_web_url", return_value=""), \
             patch.object(worker, "is_task_canceled", side_effect=is_canceled_side_effect) as mock_canceled, \
             patch.object(worker, "create_session_folder", return_value=session_folder), \
             patch.object(worker, "write_session_summary", return_value={}) as mock_write_summary, \
             patch.object(worker, "write_session_log"), \
             patch.object(worker, "write_result_and_transcript_files"):

            task = {
                "cr_shraga_taskid": "task-cancel-002",
                "cr_name": "Cancel Before Verify",
                "cr_prompt": "Do something, then cancel before verify",
                "cr_transcript": "",
                "@odata.etag": 'W/"cancel-etag-002"',
            }

            result = worker.process_task(task)

            # Task failed due to cancellation
            assert result is False

            # is_task_canceled was called at least twice
            assert mock_canceled.call_count >= 2

            # Verifier was NOT called (cancel caught before verification)
            assert not mock_agent_instance.verify_work.called, (
                "verify_work should not be called when task is canceled "
                "before verification"
            )

            # Session summary recorded as "canceled"
            assert mock_write_summary.called
            summary_kwargs = mock_write_summary.call_args
            assert summary_kwargs[1]["terminal_status"] == "canceled"

            # Worker phase DID run (worker_loop was called)
            assert mock_agent_instance.worker_loop.called

    @patch("integrated_task_worker.requests.get")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.patch")
    @patch("integrated_task_worker.subprocess.Popen")
    def test_e2e_cancel_not_triggered_when_task_runs_normally(
        self, mock_popen, mock_patch, mock_post, mock_get, monkeypatch, tmp_path
    ):
        """
        Verify that when is_task_canceled returns False throughout, the task
        completes normally without being interrupted by cancellation logic.
        This is a negative test to ensure the cancellation checkpoints do not
        interfere with normal execution.
        """
        _, worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        parsed = {
            "task_description": "Normal task",
            "success_criteria": "It works"
        }
        mock_popen.return_value = MagicMock(
            communicate=MagicMock(return_value=(
                json.dumps({"result": json.dumps(parsed)}), ""
            )),
            returncode=0
        )
        mock_patch.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock()
        )
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = worker_mod.IntegratedTaskWorker()

        session_folder = tmp_path / "normal_session"
        session_folder.mkdir()

        with patch.object(worker, "is_task_canceled", return_value=False), \
             patch.object(worker, "create_session_folder", return_value=session_folder), \
             patch.object(worker, "write_session_summary", return_value={}) as mock_write_summary, \
             patch.object(worker, "write_session_log"), \
             patch.object(worker, "write_result_and_transcript_files"):

            # Mock execute_with_autonomous_agent for simplicity (normal success)
            with patch.object(worker, "execute_with_autonomous_agent") as mock_exec:
                mock_exec.return_value = (True, "Task done!", "transcript", {})

                with patch("integrated_task_worker.subprocess.run") as mock_run:
                    mock_run.side_effect = [
                        MagicMock(returncode=0),  # git add
                        MagicMock(returncode=0, stdout="", stderr=""),  # git commit
                        MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # git rev-parse
                    ]

                    task = {
                        "cr_shraga_taskid": "task-normal-001",
                        "cr_name": "Normal Task",
                        "cr_prompt": "Do something normally",
                        "cr_transcript": "",
                        "@odata.etag": 'W/"normal-etag-001"',
                    }

                    result = worker.process_task(task)

                    # Task completed successfully
                    assert result is True

                    # Verify the final status is Completed (7), not Failed
                    patch_bodies = [
                        c[1]["json"]
                        for c in mock_patch.call_args_list
                        if "json" in c[1]
                    ]
                    completed_updates = [
                        body for body in patch_bodies
                        if body.get("cr_status") == 7
                    ]
                    assert len(completed_updates) >= 1, (
                        f"Expected STATUS_COMPLETED ('Completed') update, got: {patch_bodies}"
                    )
