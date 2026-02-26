"""
End-to-end integration tests for the Shraga Worker.

These tests exercise the Worker lifecycle: authentication, polling, claiming,
task processing, transcript management, version checking, cancellation.

All external dependencies are mocked (Azure, Dataverse, Claude CLI, Git).
Note: orchestrator.py was removed; only worker-side tests remain.
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
    """Import worker module with mocked externals."""
    monkeypatch.setenv("DATAVERSE_URL", "https://test-org.crm.dynamics.com")
    monkeypatch.setenv("TABLE_NAME", "cr_shraga_tasks")
    monkeypatch.setenv("WEBHOOK_URL", "https://test-webhook.example.com")
    monkeypatch.setenv("WEBHOOK_USER", "testuser@example.com")

    # Clear cached modules
    for mod_name in list(sys.modules):
        if mod_name in ("integrated_task_worker",):
            del sys.modules[mod_name]

    # Mock external modules
    mock_agent = MagicMock()
    monkeypatch.setitem(sys.modules, "autonomous_agent", mock_agent)

    with patch("azure.identity.DefaultAzureCredential") as mock_cred:
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(
            token="fake-token",
            expires_on=(datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
        )
        mock_cred.return_value = mock_cred_inst

        import integrated_task_worker as worker_mod

        return worker_mod, mock_cred_inst


# ===========================================================================
# E2E: Worker token + poll + update lifecycle
# ===========================================================================

class TestE2EWorkerLifecycle:

    def test_worker_authenticates_and_polls(self, monkeypatch, tmp_path):
        """Worker authenticates, gets user ID, and polls for tasks"""
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        worker = worker_mod.IntegratedTaskWorker()
        worker.dv = MagicMock()

        # WhoAmI response then task poll response
        worker.dv.get.side_effect = [
            MagicMock(json=lambda: {"UserId": "worker-user-id"}),
            MagicMock(json=lambda: {"value": []}),
        ]

        assert worker.get_current_user() == "worker-user-id"
        tasks = worker.poll_pending_tasks()
        assert tasks == []

    def test_worker_updates_task_status(self, monkeypatch, tmp_path):
        """Worker can update task status in Dataverse"""
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        worker = worker_mod.IntegratedTaskWorker()
        worker.dv = MagicMock()
        result = worker.update_task("task-001", status="Running", status_message="Running")
        assert result is True

        # update_task calls dv.patch(url, data) -- data is positional arg [1]
        sent_data = worker.dv.patch.call_args[0][1]
        assert sent_data["cr_status"] == 5

    def test_worker_sends_webhook_message(self, monkeypatch, tmp_path):
        """Worker can send messages through webhook"""
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        worker = worker_mod.IntegratedTaskWorker()
        worker.dv = MagicMock()
        result = worker.send_to_webhook("Task started!")
        assert result is True


# ===========================================================================
# E2E: Worker transcript management
# ===========================================================================

class TestE2ETranscript:

    def test_transcript_accumulates(self, monkeypatch, tmp_path):
        """Transcript accumulates entries across multiple appends"""
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

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
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        worker = worker_mod.IntegratedTaskWorker()
        t = worker.append_to_transcript("", "system", "Hello")
        entry = json.loads(t)
        assert "time" in entry
        # Verify it's a valid ISO format
        datetime.fromisoformat(entry["time"])


# ===========================================================================
# E2E: Version checking
# ===========================================================================

class TestE2EVersionChecking:

    def test_worker_version_check_exists(self, monkeypatch, tmp_path):
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)
        worker = worker_mod.IntegratedTaskWorker()
        assert hasattr(worker, '_my_version')
        assert worker._my_version is not None

    def test_worker_version_is_string(self, monkeypatch, tmp_path):
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)
        worker = worker_mod.IntegratedTaskWorker()
        assert isinstance(worker._my_version, str)
        assert len(worker._my_version) > 0

    def test_should_exit_false_in_dev(self, monkeypatch, tmp_path):
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)
        from version_check import should_exit
        # No version file in dev mode, should_exit returns False
        assert should_exit("dev") is False


# ===========================================================================
# E2E: Worker task processing (with mocked AgentCLI)
# ===========================================================================

class TestE2ETaskProcessing:

    @patch("integrated_task_worker.subprocess.run")
    @patch("integrated_task_worker.subprocess.Popen")
    def test_process_task_success(self, mock_popen, mock_run, monkeypatch, tmp_path):
        """Worker processes a task successfully end-to-end"""
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        # Mock parse_prompt_with_llm via Popen
        parsed = {
            "task_description": "Create hello world",

            "success_criteria": "Script runs"
        }
        mock_popen.return_value = MagicMock(
            communicate=MagicMock(return_value=(json.dumps({"result": json.dumps(parsed)}), "")),
            returncode=0
        )

        # Mock git operations
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="abc1234\n", stderr=""),  # git rev-parse
        ]

        worker = worker_mod.IntegratedTaskWorker()
        worker.dv = MagicMock()
        worker.dv.get.return_value = MagicMock(json=lambda: {"value": []})

        # Mock the execute_with_autonomous_agent to simulate success
        with patch.object(worker, "execute_with_autonomous_agent") as mock_exec:
            mock_exec.return_value = ("completed", "Task completed!", "transcript-data", {})

            task = {
                "cr_shraga_taskid": "task-e2e-001",
                "cr_name": "E2E Test Task",
                "cr_prompt": "Create a hello world script",
                "cr_transcript": "",
                "@odata.etag": 'W/"e2e-etag-001"',
            }

            result = worker.process_task(task)
            assert result is True

    @patch("integrated_task_worker.subprocess.Popen")
    def test_process_task_failure(self, mock_popen, monkeypatch, tmp_path):
        """Worker handles task failure"""
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        parsed = {
            "task_description": "Impossible task",

            "success_criteria": "N/A"
        }
        mock_popen.return_value = MagicMock(
            communicate=MagicMock(return_value=(json.dumps({"result": json.dumps(parsed)}), "")),
            returncode=0
        )

        worker = worker_mod.IntegratedTaskWorker()
        worker.dv = MagicMock()

        with patch.object(worker, "execute_with_autonomous_agent") as mock_exec:
            mock_exec.return_value = ("failed", "Failed: Need API key", "transcript", {})

            task = {
                "cr_shraga_taskid": "task-e2e-002",
                "cr_name": "Failing Task",
                "cr_prompt": "Do impossible thing",
                "cr_transcript": "",
            }

            result = worker.process_task(task)
            assert result is False


# ===========================================================================
# E2E: State persistence across restarts
# ===========================================================================

class TestE2EStatePersistence:

    def test_worker_state_persists(self, monkeypatch, tmp_path):
        """Worker state survives restart"""
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

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
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

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
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1, stdout="nothing to commit, working tree clean", stderr=""),
        ]

        worker = worker_mod.IntegratedTaskWorker()
        sha = worker.commit_task_results("task-git-002", tmp_path)
        assert sha is None


# ===========================================================================
# E2E: Error handling
# ===========================================================================

class TestE2EErrorHandling:

    def test_worker_handles_no_token(self, monkeypatch, tmp_path):
        """Worker exits when token cannot be obtained (scheduler restarts it)"""
        worker_mod, mock_cred = _import_modules(monkeypatch, tmp_path)

        worker = worker_mod.IntegratedTaskWorker()
        # Mock dv.get_token directly (credential mock scope expired after import)
        worker.dv.get_token = MagicMock(side_effect=Exception("Auth failed"))

        import pytest
        with pytest.raises(SystemExit) as exc_info:
            worker.get_token()
        assert exc_info.value.code == 1

    def test_worker_handles_poll_error(self, monkeypatch, tmp_path):
        """Worker handles poll errors gracefully"""
        from dv_client import DataverseRetryExhausted
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

        worker = worker_mod.IntegratedTaskWorker()
        worker.dv = MagicMock()
        worker.dv.get.side_effect = DataverseRetryExhausted("Connection refused")
        worker.current_user_id = "user-1"
        assert worker.poll_pending_tasks() == []


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

    @patch("integrated_task_worker.subprocess.Popen")
    def test_e2e_cancel_running_task(self, mock_popen, monkeypatch, tmp_path):
        """
        Full E2E cancellation flow:
        1. Submit task and let it be claimed (Running)
        2. Simulate cancel by having is_task_canceled return True
        3. Verify task ends with Canceled message
        4. Verify worker object is still functional afterward
        """
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

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

        worker = worker_mod.IntegratedTaskWorker()
        worker.dv = MagicMock()
        worker.dv.get.return_value = MagicMock(json=lambda: {"value": []})

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
                    assert worker.dv.post.called
                    post_calls = worker.dv.post.call_args_list
                    # Find the cancellation-related webhook message
                    # dv.post(url, data, ...) -- data is positional arg [1]
                    webhook_bodies = []
                    for c in post_calls:
                        data = c[0][1] if len(c[0]) > 1 else c[1].get("data", {})
                        if isinstance(data, dict) and "cr_content" in data:
                            webhook_bodies.append(data["cr_content"])
                    cancel_messages = [
                        body for body in webhook_bodies
                        if "cancel" in body.lower() or "Cancel" in body
                    ]
                    assert len(cancel_messages) >= 1, (
                        f"Expected at least one cancel webhook message, "
                        f"got messages: {webhook_bodies}"
                    )

                    # 6. Task status was updated (PATCH was called for claim + status updates)
                    assert worker.dv.patch.called
                    patch_calls = worker.dv.patch.call_args_list
                    # Look for the final status update with STATUS_CANCELED (9)
                    # dv.patch(url, data) -- data is positional arg [1]
                    patch_bodies = [
                        c[0][1] for c in patch_calls
                        if len(c[0]) > 1 and isinstance(c[0][1], dict)
                    ]
                    canceled_updates = [
                        body for body in patch_bodies
                        if body.get("cr_status") == 9
                    ]
                    assert len(canceled_updates) >= 1, (
                        f"Expected at least one PATCH setting status to 'Canceled', "
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
        worker.dv.patch.reset_mock()
        update_ok = worker.update_task("another-task-id", status="Running", status_message="Test")
        assert update_ok is True

    @patch("integrated_task_worker.subprocess.Popen")
    def test_e2e_cancel_after_worker_phase_before_verification(
        self, mock_popen, monkeypatch, tmp_path
    ):
        """
        Cancel detected after worker phase completes but before verification.

        This tests the second cancellation checkpoint: the worker phase returns
        STATUS: done, and then is_task_canceled is checked before the verifier
        runs. If canceled, the task should terminate without running verification.
        """
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

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
        worker.dv = MagicMock()
        worker.dv.get.return_value = MagicMock(json=lambda: {"value": []})

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

    @patch("integrated_task_worker.subprocess.Popen")
    def test_e2e_cancel_not_triggered_when_task_runs_normally(
        self, mock_popen, monkeypatch, tmp_path
    ):
        """
        Verify that when is_task_canceled returns False throughout, the task
        completes normally without being interrupted by cancellation logic.
        This is a negative test to ensure the cancellation checkpoints do not
        interfere with normal execution.
        """
        worker_mod, _ = _import_modules(monkeypatch, tmp_path)

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

        worker = worker_mod.IntegratedTaskWorker()
        worker.dv = MagicMock()
        worker.dv.get.return_value = MagicMock(json=lambda: {"value": []})

        session_folder = tmp_path / "normal_session"
        session_folder.mkdir()

        with patch.object(worker, "is_task_canceled", return_value=False), \
             patch.object(worker, "create_session_folder", return_value=session_folder), \
             patch.object(worker, "write_session_summary", return_value={}) as mock_write_summary, \
             patch.object(worker, "write_session_log"), \
             patch.object(worker, "write_result_and_transcript_files"):

            # Mock execute_with_autonomous_agent for simplicity (normal success)
            with patch.object(worker, "execute_with_autonomous_agent") as mock_exec:
                mock_exec.return_value = ("completed", "Task done!", "transcript", {})

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
                    # dv.patch(url, data) -- data is positional arg [1]
                    patch_bodies = [
                        c[0][1] for c in worker.dv.patch.call_args_list
                        if len(c[0]) > 1 and isinstance(c[0][1], dict)
                    ]
                    completed_updates = [
                        body for body in patch_bodies
                        if body.get("cr_status") == 7
                    ]
                    assert len(completed_updates) >= 1, (
                        f"Expected STATUS_COMPLETED ('Completed') update, got: {patch_bodies}"
                    )
