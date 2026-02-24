"""Tests for integrated_task_worker.py – IntegratedTaskWorker"""
import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock, call
from datetime import datetime, timezone, timedelta

# We need to patch azure.identity and the WEBHOOK_URL check BEFORE importing
# the module, because it runs at import time.


def _import_worker(monkeypatch, tmp_path):
    """Helper: import the worker module with all necessary patches."""
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

    # Patch DefaultAzureCredential before import
    with patch("azure.identity.DefaultAzureCredential") as mock_cred:
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(
            token="fake-token",
            expires_on=(datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
        )
        mock_cred.return_value = mock_cred_inst

        import integrated_task_worker as mod
        return mod, mock_cred_inst


# ===========================================================================
# Token management
# ===========================================================================

class TestGetToken:

    def test_get_token_returns_token(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        token = worker.get_token()
        assert token == "fake-token"

    def test_get_token_caches(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        t1 = worker.get_token()
        t2 = worker.get_token()
        # Should only call get_token once due to caching
        assert mock_cred.get_token.call_count == 1
        assert t1 == t2

    def test_get_token_refreshes_when_expired(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        worker.get_token()
        # Force expire
        worker._token_expires = datetime.now(timezone.utc) - timedelta(hours=1)
        worker.get_token()
        assert mock_cred.get_token.call_count == 2

    def test_get_token_exits_on_error(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_worker(monkeypatch, tmp_path)
        mock_cred.get_token.side_effect = Exception("Auth failed")
        worker = mod.IntegratedTaskWorker()
        # Reset cache
        worker._token_cache = None
        worker._token_expires = None
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            worker.get_token()
        assert exc_info.value.code == 1


# ===========================================================================
# State management
# ===========================================================================

class TestStateManagement:

    def test_save_and_load_state(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "test-user-123"
        worker.save_state()

        worker2 = mod.IntegratedTaskWorker()
        assert worker2.current_user_id == "test-user-123"

    def test_load_state_no_file(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        assert worker.current_user_id is None


# ===========================================================================
# Version management
# ===========================================================================

class TestVersionManagement:
    """Tests for the immutable-release version check system."""

    def test_worker_stores_version(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        assert worker._my_version is not None

    def test_should_exit_false_when_no_version_file(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        from version_check import should_exit
        # No current_version.txt exists, so should_exit returns False
        assert should_exit("v99") is False


# ===========================================================================
# get_current_user
# ===========================================================================

class TestGetCurrentUser:

    @patch("integrated_task_worker.requests.get")
    def test_get_current_user_success(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"UserId": "user-abc-123", "BusinessUnitId": "bu1"},
            raise_for_status=MagicMock()
        )
        worker = mod.IntegratedTaskWorker()
        uid = worker.get_current_user()
        assert uid == "user-abc-123"
        assert worker.current_user_id == "user-abc-123"

    @patch("integrated_task_worker.requests.get")
    def test_get_current_user_failure(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        uid = worker.get_current_user()
        assert uid is None


# ===========================================================================
# check_for_updates
# ===========================================================================

class TestVersionCheck:

    def test_worker_has_version(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        assert hasattr(worker, '_my_version')
        assert worker._my_version is not None

    def test_get_my_version_returns_string(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        from version_check import get_my_version
        version = get_my_version(__file__)
        assert isinstance(version, str)
        assert len(version) > 0

    def test_should_exit_matches_same_version(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        from version_check import should_exit, get_my_version
        v = get_my_version(__file__)
        # No version file means should_exit returns False (dev mode)
        assert should_exit(v) is False


# ===========================================================================
# append_to_transcript
# ===========================================================================

class TestAppendToTranscript:

    def test_append_to_empty_transcript(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        result = worker.append_to_transcript("", "system", "Hello")
        parsed = json.loads(result)
        assert parsed["from"] == "system"
        assert parsed["message"] == "Hello"
        assert "time" in parsed

    def test_append_to_existing_transcript(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        existing = json.dumps({"from": "worker", "time": "2026-01-01T00:00:00", "message": "First"})
        result = worker.append_to_transcript(existing, "system", "Second")
        lines = result.strip().split("\n")
        assert len(lines) == 2
        last = json.loads(lines[1])
        assert last["message"] == "Second"


# ===========================================================================
# update_task
# ===========================================================================

class TestUpdateTask:

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_success(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        result = worker.update_task("task-123", status="Running", status_message="Running")
        assert result is True
        # Verify PATCH was called with correct data
        call_kwargs = mock_patch.call_args
        sent_data = call_kwargs[1]["json"]
        assert sent_data["cr_status"] == 5
        assert sent_data["cr_statusmessage"] == "Running"

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_failure(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        result = worker.update_task("task-123", status="Running")
        assert result is False

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_skips_none_values(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        worker.update_task("task-123", status="Completed", status_message=None)
        sent_data = mock_patch.call_args[1]["json"]
        assert "cr_status" in sent_data
        # status_message is None so should not be in payload
        # Actually the code does include None values only if they're not None
        # Let's check the code logic: if status_message is not None: data["..."] = ...
        assert "cr_statusmessage" not in sent_data


# ===========================================================================
# send_to_webhook
# ===========================================================================

class TestSendToWebhook:

    @patch("integrated_task_worker.requests.post")
    def test_send_success(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        result = worker.send_to_webhook("Test message")
        assert result is True

    @patch("integrated_task_worker.requests.post")
    def test_send_truncates_title(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        long_msg = "A" * 500
        worker.send_to_webhook(long_msg)
        sent_data = mock_post.call_args[1]["json"]
        assert len(sent_data["cr_name"]) <= 450

    @patch("integrated_task_worker.requests.post")
    def test_send_includes_task_id_when_set(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        worker.current_task_id = "task-abc-123"
        worker.send_to_webhook("Test message")
        sent_data = mock_post.call_args[1]["json"]
        assert sent_data["crb3b_taskid"] == "task-abc-123"

    @patch("integrated_task_worker.requests.post")
    def test_send_omits_task_id_when_none(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        worker.current_task_id = None
        worker.send_to_webhook("Test message")
        sent_data = mock_post.call_args[1]["json"]
        assert "crb3b_taskid" not in sent_data

    @patch("integrated_task_worker.requests.post")
    def test_send_retries_with_truncation_on_400_large_message(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        # First call fails with 400, second succeeds
        error_response = MagicMock()
        error_response.status_code = 400
        error_response.text = "Request too large"
        first_error = req_lib.exceptions.HTTPError(response=error_response)
        mock_post.side_effect = [first_error, MagicMock(raise_for_status=MagicMock())]

        worker = mod.IntegratedTaskWorker()
        large_msg = "X" * 20000
        result = worker.send_to_webhook(large_msg)
        assert result is True
        assert mock_post.call_count == 2
        # Second call should have truncated content
        retry_data = mock_post.call_args_list[1][1]["json"]
        assert len(retry_data["cr_content"]) < 20000

    @patch("integrated_task_worker.requests.post")
    def test_send_no_retry_on_400_small_message(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        error_response = MagicMock()
        error_response.status_code = 400
        error_response.text = "Bad request"
        first_error = req_lib.exceptions.HTTPError(response=error_response)
        mock_post.side_effect = first_error

        worker = mod.IntegratedTaskWorker()
        result = worker.send_to_webhook("Short message")
        assert result is False
        assert mock_post.call_count == 1

    @patch("integrated_task_worker.requests.post")
    def test_send_returns_false_on_non_http_error(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_post.side_effect = ConnectionError("Network unreachable")
        worker = mod.IntegratedTaskWorker()
        result = worker.send_to_webhook("Test message")
        assert result is False


# ===========================================================================
# parse_prompt_with_llm
# ===========================================================================

class TestParsePromptWithLlm:

    @patch("integrated_task_worker.subprocess.Popen")
    def test_parse_success(self, mock_popen, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        parsed_json = {
            "task_description": "Create API",
            "success_criteria": "Tests pass"
        }
        response_json = json.dumps({"result": json.dumps(parsed_json)})

        proc = MagicMock()
        proc.communicate.return_value = (response_json, "")
        proc.returncode = 0
        mock_popen.return_value = proc

        worker = mod.IntegratedTaskWorker()
        result = worker.parse_prompt_with_llm("Build an API for auth")
        assert result["task_description"] == "Create API"
        assert result["success_criteria"] == "Tests pass"

    @patch("integrated_task_worker.subprocess.Popen")
    def test_parse_timeout_returns_fallback(self, mock_popen, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import subprocess
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired("claude", 30)
        mock_popen.return_value = proc

        worker = mod.IntegratedTaskWorker()
        result = worker.parse_prompt_with_llm("Raw prompt text")
        assert result["task_description"] == "Raw prompt text"
        assert result["success_criteria"] == "Review and confirm task is complete"

    @patch("integrated_task_worker.subprocess.Popen")
    def test_parse_error_returns_fallback(self, mock_popen, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        proc = MagicMock()
        proc.communicate.return_value = ("not json", "")
        proc.returncode = 0
        mock_popen.return_value = proc

        worker = mod.IntegratedTaskWorker()
        result = worker.parse_prompt_with_llm("Some prompt")
        assert result["task_description"] == "Some prompt"


# ===========================================================================
# commit_task_results
# ===========================================================================

class TestCommitTaskResults:

    @patch("integrated_task_worker.subprocess.run")
    def test_commit_success(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="abc1234\n", stderr=""),  # git rev-parse
        ]
        worker = mod.IntegratedTaskWorker()
        sha = worker.commit_task_results("task-123", tmp_path)
        assert sha == "abc1234"

    @patch("integrated_task_worker.subprocess.run")
    def test_commit_nothing_to_commit(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1, stdout="nothing to commit", stderr=""),  # git commit
        ]
        worker = mod.IntegratedTaskWorker()
        sha = worker.commit_task_results("task-123", tmp_path)
        assert sha is None

    @patch("integrated_task_worker.subprocess.run")
    def test_commit_exception_returns_none(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_run.side_effect = Exception("Git error")
        worker = mod.IntegratedTaskWorker()
        sha = worker.commit_task_results("task-123", tmp_path)
        assert sha is None


# ===========================================================================
# poll_pending_tasks
# ===========================================================================

class TestPollPendingTasks:

    @patch("integrated_task_worker.requests.get")
    def test_poll_returns_tasks(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [{"cr_name": "Task1"}]}
        )
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-123"
        tasks = worker.poll_pending_tasks()
        assert len(tasks) == 1
        assert tasks[0]["cr_name"] == "Task1"

    @patch("integrated_task_worker.requests.get")
    def test_poll_filter_uses_webhook_user(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-123"
        worker.poll_pending_tasks()
        # Verify the filter uses WEBHOOK_USER env var (testuser@example.com), not a hardcoded email
        call_kwargs = mock_get.call_args
        filter_param = call_kwargs[1]["params"]["$filter"]
        assert "testuser@example.com" in filter_param
        assert "sagik@microsoft.com" not in filter_param

    @patch("integrated_task_worker.requests.get")
    def test_poll_filter_includes_devbox_filter(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-123"
        worker.poll_pending_tasks()
        # V2: filter uses only crb3b_devbox eq null (no hostname match)
        call_kwargs = mock_get.call_args
        filter_param = call_kwargs[1]["params"]["$filter"]
        assert "crb3b_devbox eq null" in filter_param
        # Should NOT contain a hostname match
        assert f"crb3b_devbox eq '{mod.MACHINE_NAME}'" not in filter_param

    @patch("integrated_task_worker.requests.get")
    def test_poll_returns_empty_on_error(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-123"
        tasks = worker.poll_pending_tasks()
        assert tasks == []

    @patch("integrated_task_worker.requests.get")
    def test_poll_calls_get_current_user_if_none(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        # First call for WhoAmI, second for task poll
        mock_get.side_effect = [
            MagicMock(
                raise_for_status=MagicMock(),
                json=lambda: {"UserId": "user-abc"}
            ),
            MagicMock(
                raise_for_status=MagicMock(),
                json=lambda: {"value": []}
            ),
        ]
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = None
        tasks = worker.poll_pending_tasks()
        assert worker.current_user_id == "user-abc"


# ===========================================================================
# _get_headers
# ===========================================================================

class TestGetHeaders:

    def test_returns_headers_with_token(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        headers = worker._get_headers()
        assert headers["Authorization"] == "Bearer fake-token"
        assert headers["OData-Version"] == "4.0"
        assert "Content-Type" not in headers

    def test_includes_content_type_when_specified(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        headers = worker._get_headers(content_type="application/json")
        assert headers["Content-Type"] == "application/json"

    def test_includes_if_match_when_etag_specified(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        headers = worker._get_headers(etag='W/"12345"')
        assert headers["If-Match"] == 'W/"12345"'

    def test_exits_when_no_token(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_worker(monkeypatch, tmp_path)
        mock_cred.get_token.side_effect = Exception("Auth failed")
        worker = mod.IntegratedTaskWorker()
        worker._token_cache = None
        worker._token_expires = None
        import pytest
        with pytest.raises(SystemExit):
            worker._get_headers()


# ===========================================================================
# _cleanup_in_progress_task
# ===========================================================================

class TestCleanupInProgressTask:

    @patch("integrated_task_worker.requests.patch")
    def test_marks_running_task_as_failed(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        worker.current_task_id = "task-running-123"
        worker._cleanup_in_progress_task("Worker interrupted")
        # Should have called update_task with FAILED status
        call_kwargs = mock_patch.call_args
        sent_data = call_kwargs[1]["json"]
        assert sent_data["cr_status"] == 8  # Failed (integer picklist)
        assert "Worker interrupted" in sent_data["cr_statusmessage"]
        # Should clear task ID after cleanup
        assert worker.current_task_id is None

    def test_does_nothing_when_no_task(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        worker.current_task_id = None
        # Should not raise or call anything
        worker._cleanup_in_progress_task("No task running")
        assert worker.current_task_id is None


# ===========================================================================
# check_for_updates uses UPDATE_BRANCH
# ===========================================================================

class TestVersionCheckModule:

    def test_get_my_version_dev_mode(self, monkeypatch, tmp_path):
        """In dev mode (not under releases/), returns parent folder name."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        from version_check import get_my_version
        version = get_my_version(__file__)
        assert isinstance(version, str)
        assert len(version) > 0


# ===========================================================================
# Timeout exception handling
# ===========================================================================

class TestTimeoutHandling:

    @patch("integrated_task_worker.requests.get")
    def test_get_current_user_timeout(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.Timeout("Connection timed out")
        worker = mod.IntegratedTaskWorker()
        result = worker.get_current_user()
        assert result is None

    @patch("integrated_task_worker.requests.get")
    def test_poll_pending_tasks_timeout(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.Timeout("Connection timed out")
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-123"
        tasks = worker.poll_pending_tasks()
        assert tasks == []

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_timeout(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_patch.side_effect = req_lib.exceptions.Timeout("Connection timed out")
        worker = mod.IntegratedTaskWorker()
        result = worker.update_task("task-123", status="Running")
        assert result is False

    def test_worker_has_version_check(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        assert hasattr(worker, '_my_version')
        assert worker._my_version is not None


# ===========================================================================
# update_task with session_summary
# ===========================================================================

class TestUpdateTaskSessionSummary:

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_includes_session_summary(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        result = worker.update_task("task-123", status="Completed", session_summary='{"test": true}')
        assert result is True
        sent_data = mock_patch.call_args[1]["json"]
        assert sent_data["crb3b_sessionsummary"] == '{"test": true}'
        assert sent_data["cr_status"] == 7

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_omits_session_summary_when_none(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        worker.update_task("task-123", status="Completed", session_summary=None)
        sent_data = mock_patch.call_args[1]["json"]
        assert "crb3b_sessionsummary" not in sent_data

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_retries_without_summary_on_column_error(self, mock_patch, monkeypatch, tmp_path):
        """If crb3b_sessionsummary column doesn't exist, retry without it."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib

        # First call fails with "property crb3b_sessionsummary doesn't exist"
        first_call_error = Exception("The property 'crb3b_sessionsummary' does not exist")
        # Second call succeeds
        mock_patch.side_effect = [first_call_error, MagicMock(raise_for_status=MagicMock())]

        worker = mod.IntegratedTaskWorker()
        result = worker.update_task("task-123", status="Completed", session_summary='{"test": true}')
        assert result is True
        assert mock_patch.call_count == 2
        # Second call should not have crb3b_sessionsummary
        retry_data = mock_patch.call_args_list[1][1]["json"]
        assert "crb3b_sessionsummary" not in retry_data
        assert retry_data["cr_status"] == 7


# ===========================================================================
# build_session_summary
# ===========================================================================

class TestBuildSessionSummary:

    @patch("integrated_task_worker.requests.get")
    def test_build_summary_basic_structure(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        # Mock fetch_task_activities
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [
                {"cr_name": "Started task"},
                {"cr_name": "Read files"},
            ]}
        )

        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "test_session"
        session_folder.mkdir()

        accumulated_stats = {
            "total_cost_usd": 0.15,
            "total_duration_ms": 45000,
            "total_api_duration_ms": 38000,
            "total_turns": 8,
            "tokens": {"input": 15000, "output": 5000, "cache_read": 3000, "cache_creation": 2000},
            "model_usage": {"claude-sonnet-4-20250514": {"cost_usd": 0.15, "input_tokens": 15000, "output_tokens": 5000}},
        }

        phases = [
            {"phase": "worker_1", "cost_usd": 0.10, "duration_ms": 30000, "turns": 5},
            {"phase": "verifier_1", "cost_usd": 0.03, "duration_ms": 10000, "turns": 2},
            {"phase": "summarizer", "cost_usd": 0.02, "duration_ms": 5000, "turns": 1},
        ]

        summary = worker.build_session_summary(
            task_id="task-001",
            terminal_status="completed",
            session_folder=session_folder,
            accumulated_stats=accumulated_stats,
            phases=phases,
            result_text="Task completed successfully with all tests passing." * 10,
            session_id="sess-abc",
        )

        assert summary["session_id"] == "sess-abc"
        assert summary["task_id"] == "task-001"
        assert summary["terminal_status"] == "completed"
        assert summary["total_cost_usd"] == 0.15
        assert summary["total_duration_ms"] == 45000
        assert summary["total_turns"] == 8
        assert summary["tokens"]["input"] == 15000
        assert len(summary["phases"]) == 3
        assert summary["phases"][0]["phase"] == "worker_1"
        assert summary["dev_box"] != ""
        assert summary["working_dir"] == str(session_folder)
        assert len(summary["result_preview"]) <= 200
        assert "timestamp" in summary
        # Activities fetched from Dataverse
        assert "Started task" in summary["activities"]
        assert "Read files" in summary["activities"]

    @patch("integrated_task_worker.requests.get")
    def test_build_summary_handles_empty_stats(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "empty_session"
        session_folder.mkdir()

        summary = worker.build_session_summary(
            task_id="task-002",
            terminal_status="failed",
            session_folder=session_folder,
            accumulated_stats={},
            phases=[],
            result_text="Error occurred",
        )

        assert summary["terminal_status"] == "failed"
        assert summary["total_cost_usd"] == 0
        assert summary["total_turns"] == 0
        assert summary["phases"] == []
        assert summary["activities"] == []
        assert summary["num_sub_agents"] == 0

    @patch("integrated_task_worker.requests.get")
    def test_build_summary_sub_agents_count(self, mock_get, monkeypatch, tmp_path):
        """num_sub_agents = len(model_usage) - 1 (main model excluded)"""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "multi_model_session"
        session_folder.mkdir()

        accumulated_stats = {
            "model_usage": {
                "model-main": {"cost_usd": 0.10, "input_tokens": 1000, "output_tokens": 500},
                "model-sub1": {"cost_usd": 0.03, "input_tokens": 300, "output_tokens": 100},
                "model-sub2": {"cost_usd": 0.02, "input_tokens": 200, "output_tokens": 50},
            },
        }

        summary = worker.build_session_summary(
            task_id="task-003",
            terminal_status="completed",
            session_folder=session_folder,
            accumulated_stats=accumulated_stats,
            phases=[],
            result_text="Done",
        )

        assert summary["num_sub_agents"] == 2


# ===========================================================================
# write_session_summary
# ===========================================================================

class TestWriteSessionSummary:

    @patch("integrated_task_worker.requests.get")
    @patch("integrated_task_worker.requests.patch")
    def test_writes_json_file_to_session_folder(self, mock_patch, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "summary_test_session"
        session_folder.mkdir()

        summary = worker.write_session_summary(
            task_id="task-write-001",
            terminal_status="completed",
            session_folder=session_folder,
            accumulated_stats={"total_cost_usd": 0.05, "total_duration_ms": 1000,
                               "total_api_duration_ms": 800, "total_turns": 2,
                               "tokens": {"input": 100, "output": 50, "cache_read": 0, "cache_creation": 0},
                               "model_usage": {}},
            phases=[{"phase": "worker_1", "cost_usd": 0.05, "duration_ms": 1000, "turns": 2}],
            result_text="All good",
            session_id="sess-xyz",
        )

        # Verify file was written
        summary_file = session_folder / "session_summary.json"
        assert summary_file.exists()

        # Verify JSON content
        content = json.loads(summary_file.read_text(encoding="utf-8"))
        assert content["task_id"] == "task-write-001"
        assert content["terminal_status"] == "completed"
        assert content["session_id"] == "sess-xyz"
        assert content["total_cost_usd"] == 0.05

        # Verify DV update was attempted
        assert mock_patch.called
        patch_data = mock_patch.call_args[1]["json"]
        assert "crb3b_sessionsummary" in patch_data

    @patch("integrated_task_worker.requests.get")
    @patch("integrated_task_worker.requests.patch")
    def test_write_summary_returns_dict(self, mock_patch, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "return_test"
        session_folder.mkdir()

        result = worker.write_session_summary(
            task_id="task-ret-001",
            terminal_status="failed",
            session_folder=session_folder,
            accumulated_stats={},
            phases=[],
            result_text="Error",
        )

        assert isinstance(result, dict)
        assert result["terminal_status"] == "failed"

    @patch("integrated_task_worker.requests.get")
    @patch("integrated_task_worker.requests.patch")
    def test_write_summary_graceful_on_file_write_failure(self, mock_patch, mock_get, monkeypatch, tmp_path):
        """If session folder doesn't exist, file write fails gracefully."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = mod.IntegratedTaskWorker()
        # Use a non-existent folder path
        bad_folder = tmp_path / "nonexistent" / "deep" / "path"

        # Should not raise
        result = worker.write_session_summary(
            task_id="task-bad-folder",
            terminal_status="failed",
            session_folder=bad_folder,
            accumulated_stats={},
            phases=[],
            result_text="Error",
        )
        assert isinstance(result, dict)


# ===========================================================================
# write_session_log
# ===========================================================================

class TestWriteSessionLog:

    def _make_summary(self, **overrides):
        """Helper: return a minimal session summary dict with optional overrides."""
        base = {
            "session_id": "sess-log-001",
            "task_id": "task-log-001",
            "dev_box": "DEVBOX-01",
            "working_dir": "C:\\sessions\\test",
            "total_duration_ms": 90000,
            "total_cost_usd": 0.25,
            "total_api_duration_ms": 70000,
            "total_turns": 12,
            "tokens": {"input": 20000, "output": 8000, "cache_read": 5000, "cache_creation": 3000},
            "model_usage": {
                "claude-sonnet-4-20250514": {"cost_usd": 0.20, "input_tokens": 18000, "output_tokens": 7000},
                "claude-haiku-3": {"cost_usd": 0.05, "input_tokens": 2000, "output_tokens": 1000},
            },
            "num_sub_agents": 1,
            "phases": [
                {"phase": "worker_1", "cost_usd": 0.15, "duration_ms": 50000, "turns": 7},
                {"phase": "verifier_1", "cost_usd": 0.05, "duration_ms": 25000, "turns": 3},
                {"phase": "summarizer", "cost_usd": 0.05, "duration_ms": 15000, "turns": 2},
            ],
            "activities": ["Started task", "Read files", "Wrote code", "Tests passed"],
            "terminal_status": "completed",
            "result_preview": "All tests passing",
            "timestamp": "2026-02-16T10:30:00+00:00",
        }
        base.update(overrides)
        return base

    def test_writes_session_log_file(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "log_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder, result_text="All tests passing.")

        log_file = session_folder / "SESSION_LOG.md"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "# SESSION LOG" in content

    def test_contains_task_metadata(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "meta_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "task-log-001" in content
        assert "DEVBOX-01" in content
        assert "sess-log-001" in content
        assert "completed" in content

    def test_contains_session_stats(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "stats_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "$0.25" in content
        assert "20,000" in content
        assert "8,000" in content
        assert "12" in content  # turns

    def test_contains_phases(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "phase_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "worker_1" in content
        assert "verifier_1" in content
        assert "summarizer" in content

    def test_contains_activities(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "activity_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "Started task" in content
        assert "Tests passed" in content

    def test_contains_result_text(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "result_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder, result_text="Final output with details.")

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "Final output with details." in content

    def test_contains_onedrive_url(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "url_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(
            summary, session_folder,
            folder_url="https://example.sharepoint.com/sessions/test"
        )

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "https://example.sharepoint.com/sessions/test" in content
        assert "Open in OneDrive" in content

    def test_omits_onedrive_row_when_no_url(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "nourl_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder, folder_url="")

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "Open in OneDrive" not in content

    def test_contains_transcript_reference(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "transcript_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "cr_transcript" in content
        assert "task-log-001" in content

    def test_contains_worker_version(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "version_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "Worker Version" in content

    def test_contains_model_usage(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "model_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "claude-sonnet-4-20250514" in content
        assert "claude-haiku-3" in content

    def test_graceful_on_write_failure(self, monkeypatch, tmp_path):
        """Should not raise if session folder does not exist."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        bad_folder = tmp_path / "nonexistent" / "deep" / "path"

        summary = self._make_summary()
        # Should not raise
        worker.write_session_log(summary, bad_folder)

    def test_empty_summary_fields(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "empty_session"
        session_folder.mkdir()

        summary = self._make_summary(
            session_id="",
            activities=[],
            phases=[],
            model_usage={},
            result_preview="",
        )
        worker.write_session_log(summary, session_folder, result_text="")

        log_file = session_folder / "SESSION_LOG.md"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "# SESSION LOG" in content
        # No activities section header when list is empty
        assert "Activity Log" not in content

    def test_falls_back_to_result_preview_when_no_result_text(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "preview_session"
        session_folder.mkdir()

        summary = self._make_summary(result_preview="Preview of result")
        worker.write_session_log(summary, session_folder, result_text="")

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "Preview of result" in content


# ===========================================================================
# write_result_and_transcript_files (T024)
# ===========================================================================

class TestWriteResultAndTranscriptFiles:
    """Tests for writing result.md and transcript.md to the session folder."""

    def test_writes_result_md(self, monkeypatch, tmp_path):
        """result.md is written to session folder with the result text content."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "result_md_session"
        session_folder.mkdir()

        result_text = "Task completed successfully.\n\nAll 5 tests pass."
        transcript_text = '{"from":"system","time":"2026-02-21T00:00:00","message":"started"}'

        worker.write_result_and_transcript_files(
            session_folder=session_folder,
            result_text=result_text,
            transcript=transcript_text,
        )

        result_file = session_folder / "result.md"
        assert result_file.exists(), "result.md should be created in session folder"
        content = result_file.read_text(encoding="utf-8")
        assert content == result_text

    def test_writes_transcript_md(self, monkeypatch, tmp_path):
        """transcript.md is written to session folder with the JSONL transcript."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "transcript_md_session"
        session_folder.mkdir()

        result_text = "Error: something went wrong"
        transcript_text = (
            '{"from":"system","time":"2026-02-21T00:00:00","message":"started"}\n'
            '{"from":"worker","time":"2026-02-21T00:01:00","message":"working on it"}\n'
            '{"from":"system","time":"2026-02-21T00:02:00","message":"[ERROR] failed"}'
        )

        worker.write_result_and_transcript_files(
            session_folder=session_folder,
            result_text=result_text,
            transcript=transcript_text,
        )

        transcript_file = session_folder / "transcript.md"
        assert transcript_file.exists(), "transcript.md should be created in session folder"
        content = transcript_file.read_text(encoding="utf-8")
        assert content == transcript_text

    def test_writes_empty_files_when_no_content(self, monkeypatch, tmp_path):
        """result.md and transcript.md are written even when content is empty."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "empty_content_session"
        session_folder.mkdir()

        worker.write_result_and_transcript_files(
            session_folder=session_folder,
            result_text="",
            transcript="",
        )

        assert (session_folder / "result.md").exists()
        assert (session_folder / "transcript.md").exists()
        assert (session_folder / "result.md").read_text(encoding="utf-8") == ""
        assert (session_folder / "transcript.md").read_text(encoding="utf-8") == ""

    def test_writes_files_with_none_content(self, monkeypatch, tmp_path):
        """Gracefully handle None values for result_text and transcript."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "none_content_session"
        session_folder.mkdir()

        worker.write_result_and_transcript_files(
            session_folder=session_folder,
            result_text=None,
            transcript=None,
        )

        assert (session_folder / "result.md").exists()
        assert (session_folder / "transcript.md").exists()

    def test_graceful_on_write_failure(self, monkeypatch, tmp_path):
        """Should not raise if session folder does not exist."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        bad_folder = tmp_path / "nonexistent" / "deep" / "path"

        # Should not raise
        worker.write_result_and_transcript_files(
            session_folder=bad_folder,
            result_text="Some result",
            transcript="Some transcript",
        )

    def test_files_written_on_completed_state(self, monkeypatch, tmp_path):
        """Verify result.md content matches what a completed task would produce."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "completed_session"
        session_folder.mkdir()

        completed_result = "All tests passing.\n\n- Session folder: [View in OneDrive](https://example.com)"
        completed_transcript = '{"from":"summarizer","time":"2026-02-21T00:05:00","message":"SUMMARY CREATED"}'

        worker.write_result_and_transcript_files(
            session_folder=session_folder,
            result_text=completed_result,
            transcript=completed_transcript,
        )

        result_content = (session_folder / "result.md").read_text(encoding="utf-8")
        transcript_content = (session_folder / "transcript.md").read_text(encoding="utf-8")
        assert "All tests passing" in result_content
        assert "SUMMARY CREATED" in transcript_content

    def test_files_written_on_failed_state(self, monkeypatch, tmp_path):
        """Verify files are written even for failed tasks."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "failed_session"
        session_folder.mkdir()

        failed_result = "Failed: Missing API credentials"
        failed_transcript = '{"from":"system","time":"2026-02-21T00:03:00","message":"[ERROR] Failed"}'

        worker.write_result_and_transcript_files(
            session_folder=session_folder,
            result_text=failed_result,
            transcript=failed_transcript,
        )

        result_content = (session_folder / "result.md").read_text(encoding="utf-8")
        transcript_content = (session_folder / "transcript.md").read_text(encoding="utf-8")
        assert "Failed: Missing API credentials" in result_content
        assert "[ERROR] Failed" in transcript_content

    def test_files_written_on_canceled_state(self, monkeypatch, tmp_path):
        """Verify files are written even for canceled tasks."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "canceled_session"
        session_folder.mkdir()

        canceled_result = "Task canceled by user"
        canceled_transcript = '{"from":"system","time":"2026-02-21T00:01:00","message":"Task canceled by user"}'

        worker.write_result_and_transcript_files(
            session_folder=session_folder,
            result_text=canceled_result,
            transcript=canceled_transcript,
        )

        result_content = (session_folder / "result.md").read_text(encoding="utf-8")
        transcript_content = (session_folder / "transcript.md").read_text(encoding="utf-8")
        assert "Task canceled by user" in result_content
        assert "Task canceled by user" in transcript_content


# ===========================================================================
# fetch_task_activities
# ===========================================================================

class TestFetchTaskActivities:

    @patch("integrated_task_worker.requests.get")
    def test_fetch_activities_success(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [
                {"cr_name": "Started task", "createdon": "2026-01-01T00:00:00Z"},
                {"cr_name": "Read files", "createdon": "2026-01-01T00:01:00Z"},
                {"cr_name": "Wrote code", "createdon": "2026-01-01T00:02:00Z"},
            ]}
        )
        worker = mod.IntegratedTaskWorker()
        activities = worker.fetch_task_activities("task-001")
        assert len(activities) == 3
        assert activities[0] == "Started task"
        assert activities[2] == "Wrote code"

    @patch("integrated_task_worker.requests.get")
    def test_fetch_activities_truncates_long_names(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        long_name = "A" * 200
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [{"cr_name": long_name}]}
        )
        worker = mod.IntegratedTaskWorker()
        activities = worker.fetch_task_activities("task-001")
        assert len(activities[0]) == 120

    @patch("integrated_task_worker.requests.get")
    def test_fetch_activities_returns_empty_on_error(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        activities = worker.fetch_task_activities("task-001")
        assert activities == []

    @patch("integrated_task_worker.requests.get")
    def test_fetch_activities_skips_empty_names(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [
                {"cr_name": "Valid"},
                {"cr_name": ""},
                {"cr_name": None},
            ]}
        )
        worker = mod.IntegratedTaskWorker()
        activities = worker.fetch_task_activities("task-001")
        assert len(activities) == 1
        assert activities[0] == "Valid"


# ===========================================================================
# V2 status constants
# ===========================================================================

class TestV2StatusConstants:

    def test_submitted_status_constant(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        assert mod.STATUS_SUBMITTED == "Submitted"

    def test_canceling_status_constant(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        assert mod.STATUS_CANCELING == "Canceling"

    def test_submitted_status_int(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        assert mod._STATUS_INT["Submitted"] == 10

    def test_canceling_status_int(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        assert mod._STATUS_INT["Canceling"] == 11

    def test_status_constants_are_strings(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        assert isinstance(mod.STATUS_PENDING, str)
        assert isinstance(mod.STATUS_SUBMITTED, str)
        assert isinstance(mod.STATUS_RUNNING, str)
        assert isinstance(mod.STATUS_CANCELING, str)

    def test_machine_name_constant_exists(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        assert mod.MACHINE_NAME is not None
        assert isinstance(mod.MACHINE_NAME, str)


# ===========================================================================
# claim_task (ETag-based atomic claiming)
# ===========================================================================

class TestClaimTask:

    @patch("integrated_task_worker.requests.patch")
    def test_claim_task_success(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock()
        )
        worker = mod.IntegratedTaskWorker()
        task = {
            "cr_shraga_taskid": "task-claim-001",
            "@odata.etag": 'W/"67890"',
        }
        result = worker.claim_task(task)
        assert result is True
        # Verify If-Match header was sent
        call_headers = mock_patch.call_args[1]["headers"]
        assert call_headers["If-Match"] == 'W/"67890"'
        # Verify body sets status to Running
        call_body = mock_patch.call_args[1]["json"]
        assert call_body["cr_status"] == mod._STATUS_INT[mod.STATUS_RUNNING]

    @patch("integrated_task_worker.requests.patch")
    def test_claim_task_conflict_412(self, mock_patch, monkeypatch, tmp_path):
        """HTTP 412 means another worker claimed it first."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(status_code=412)
        worker = mod.IntegratedTaskWorker()
        task = {
            "cr_shraga_taskid": "task-claim-002",
            "@odata.etag": 'W/"99999"',
        }
        result = worker.claim_task(task)
        assert result is False

    def test_claim_task_missing_etag(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        task = {"cr_shraga_taskid": "task-no-etag"}
        result = worker.claim_task(task)
        assert result is False

    def test_claim_task_missing_id(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        task = {"@odata.etag": 'W/"12345"'}
        result = worker.claim_task(task)
        assert result is False

    @patch("integrated_task_worker.requests.patch")
    def test_claim_task_timeout(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_patch.side_effect = req_lib.exceptions.Timeout("timed out")
        worker = mod.IntegratedTaskWorker()
        task = {
            "cr_shraga_taskid": "task-timeout",
            "@odata.etag": 'W/"11111"',
        }
        result = worker.claim_task(task)
        assert result is False

    @patch("integrated_task_worker.requests.patch")
    def test_claim_task_network_error(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        task = {
            "cr_shraga_taskid": "task-net-err",
            "@odata.etag": 'W/"22222"',
        }
        result = worker.claim_task(task)
        assert result is False


# ===========================================================================
# V2: claim_task includes crb3b_devbox
# ===========================================================================

class TestClaimTaskDevbox:

    @patch("integrated_task_worker.requests.patch")
    def test_claim_task_includes_devbox_in_body(self, mock_patch, monkeypatch, tmp_path):
        """claim_task() PATCH body must include crb3b_devbox = MACHINE_NAME."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock()
        )
        worker = mod.IntegratedTaskWorker()
        task = {
            "cr_shraga_taskid": "task-devbox-001",
            "@odata.etag": 'W/"77777"',
        }
        result = worker.claim_task(task)
        assert result is True
        call_body = mock_patch.call_args[1]["json"]
        assert call_body["crb3b_devbox"] == mod.MACHINE_NAME


# ===========================================================================
# V2: poll_pending_tasks filter uses crb3b_devbox eq null (no hostname match)
# ===========================================================================

class TestPollFilterDevboxNull:

    @patch("integrated_task_worker.requests.get")
    def test_poll_filter_uses_devbox_null_only(self, mock_get, monkeypatch, tmp_path):
        """poll_pending_tasks filter should use 'crb3b_devbox eq null' without hostname match."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-123"
        worker.poll_pending_tasks()
        call_kwargs = mock_get.call_args
        filter_param = call_kwargs[1]["params"]["$filter"]
        assert "crb3b_devbox eq null" in filter_param
        # The filter should NOT contain a hostname match (only null)
        assert f"crb3b_devbox eq '{mod.MACHINE_NAME}'" not in filter_param


# ===========================================================================
# V2: is_task_canceled checks both Canceling(11) and Canceled(9)
# ===========================================================================

class TestIsTaskCanceledV2:

    @patch("integrated_task_worker.requests.get")
    def test_is_task_canceled_true_for_canceling(self, mock_get, monkeypatch, tmp_path):
        """is_task_canceled returns True when status is Canceling (11)."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"cr_status": mod._STATUS_INT[mod.STATUS_CANCELING]},
        )
        worker = mod.IntegratedTaskWorker()
        result = worker.is_task_canceled("task-canceling-001")
        assert result is True

    @patch("integrated_task_worker.requests.get")
    def test_is_task_canceled_true_for_canceled(self, mock_get, monkeypatch, tmp_path):
        """is_task_canceled returns True when status is Canceled (9)."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"cr_status": mod._STATUS_INT[mod.STATUS_CANCELED]},
        )
        worker = mod.IntegratedTaskWorker()
        result = worker.is_task_canceled("task-canceled-001")
        assert result is True

    @patch("integrated_task_worker.requests.get")
    def test_is_task_canceled_true_for_string_canceling(self, mock_get, monkeypatch, tmp_path):
        """is_task_canceled returns True for the string label 'Canceling'."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"cr_status": mod.STATUS_CANCELING},
        )
        worker = mod.IntegratedTaskWorker()
        result = worker.is_task_canceled("task-str-canceling-001")
        assert result is True


# ===========================================================================
# Worker run() loop resilience (GAP-B01 fix)
# ===========================================================================

class TestWorkerRunLoopResilience:
    """Tests that the worker's run() loop continues after various error conditions
    instead of exiting (GAP-B01 fix)."""

    def _make_worker(self, monkeypatch, tmp_path):
        """Helper: import module and create a worker with user ID pre-set."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-test-run"
        return mod, worker

    @patch("integrated_task_worker.time.sleep")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.get")
    def test_worker_continues_after_successful_task(self, mock_get, mock_post, mock_sleep, monkeypatch, tmp_path):
        """After a successful task, the worker should loop back and poll again (not exit)."""
        mod, worker = self._make_worker(monkeypatch, tmp_path)

        # Track how many times poll_pending_tasks is called
        poll_call_count = 0

        def fake_poll():
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count == 1:
                # First poll: return a task
                return [{"cr_shraga_taskid": "task-001", "cr_name": "Test", "@odata.etag": 'W/"1"'}]
            elif poll_call_count == 2:
                # Second poll: no tasks (proves we looped back)
                return []
            else:
                # Third poll: raise KeyboardInterrupt to exit
                raise KeyboardInterrupt()

        worker.poll_pending_tasks = fake_poll
        worker.process_task = MagicMock()  # Succeeds silently
        worker.send_to_webhook = MagicMock()
        worker.check_for_updates = MagicMock(return_value=False)
        worker.last_update_check = datetime.now(tz=timezone.utc)

        worker.run()

        # process_task was called for the first poll's task
        assert worker.process_task.call_count == 1
        # poll was called at least 3 times (first=task, second=empty, third=interrupt)
        assert poll_call_count == 3

    @patch("integrated_task_worker.time.sleep")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.get")
    def test_worker_continues_after_failed_task(self, mock_get, mock_post, mock_sleep, monkeypatch, tmp_path):
        """If process_task raises an exception, the worker should continue polling."""
        mod, worker = self._make_worker(monkeypatch, tmp_path)

        poll_call_count = 0

        def fake_poll():
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count == 1:
                return [{"cr_shraga_taskid": "task-fail", "cr_name": "Fail Task"}]
            elif poll_call_count == 2:
                return []
            else:
                raise KeyboardInterrupt()

        worker.poll_pending_tasks = fake_poll
        worker.process_task = MagicMock(side_effect=RuntimeError("Task exploded"))
        worker.send_to_webhook = MagicMock()
        worker._cleanup_in_progress_task = MagicMock()
        worker.check_for_updates = MagicMock(return_value=False)
        worker.last_update_check = datetime.now(tz=timezone.utc)

        worker.run()

        # process_task was called and raised
        assert worker.process_task.call_count == 1
        # Worker continued to poll again (didn't exit after the error)
        assert poll_call_count == 3

    @patch("integrated_task_worker.time.sleep")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.get")
    def test_worker_continues_after_transient_error(self, mock_get, mock_post, mock_sleep, monkeypatch, tmp_path):
        """If poll_pending_tasks raises a transient error, the worker sleeps 60s and retries."""
        mod, worker = self._make_worker(monkeypatch, tmp_path)

        poll_call_count = 0

        def fake_poll():
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count == 1:
                raise ConnectionError("Network is down")
            elif poll_call_count == 2:
                return []
            else:
                raise KeyboardInterrupt()

        worker.poll_pending_tasks = fake_poll
        worker.send_to_webhook = MagicMock()
        worker.check_for_updates = MagicMock(return_value=False)
        worker.last_update_check = datetime.now(tz=timezone.utc)

        worker.run()

        # Worker recovered from the transient error and polled again
        assert poll_call_count == 3

    @patch("integrated_task_worker.time.sleep")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.get")
    def test_worker_sleeps_on_error(self, mock_get, mock_post, mock_sleep, monkeypatch, tmp_path):
        """On transient error, the worker should sleep 60s (not tight-loop)."""
        mod, worker = self._make_worker(monkeypatch, tmp_path)

        poll_call_count = 0

        def fake_poll():
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count == 1:
                raise ConnectionError("Network is down")
            else:
                raise KeyboardInterrupt()

        worker.poll_pending_tasks = fake_poll
        worker.send_to_webhook = MagicMock()

        worker.run()

        # Verify that time.sleep was called with 60 (the error recovery sleep)
        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert 60 in sleep_calls, f"Expected 60s sleep in error path, got sleep calls: {sleep_calls}"


# ===========================================================================
# process_task (T040 – GAP-T01)
# ===========================================================================

class TestProcessTask:
    """Tests for process_task() covering success, failure, and cancellation paths."""

    def _make_task(self, **overrides):
        """Return a minimal task dict suitable for process_task()."""
        base = {
            "cr_shraga_taskid": "task-pt-001",
            "cr_name": "Unit Test Task",
            "cr_prompt": "Write a hello world script",
            "cr_transcript": "",
            "@odata.etag": 'W/"55555"',
        }
        base.update(overrides)
        return base

    # ------------------------------------------------------------------
    # Success path
    # ------------------------------------------------------------------

    @patch("integrated_task_worker.subprocess.run")
    @patch("integrated_task_worker.subprocess.Popen")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.patch")
    @patch("integrated_task_worker.requests.get")
    def test_process_task_success(self, mock_get, mock_patch, mock_post,
                                  mock_popen, mock_subrun,
                                  monkeypatch, tmp_path):
        """Successful task: claim succeeds, agent returns success, status set
        to COMPLETED, result and transcript saved."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        # --- HTTP mocks ---
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        # requests.patch succeeds (claim_task, update_task)
        mock_patch.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock()
        )
        # requests.post succeeds (send_to_webhook)
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())

        # --- parse_prompt_with_llm mock (subprocess.Popen) ---
        parsed_json = {"task_description": "Write hello world", "success_criteria": "Script runs"}
        popen_proc = MagicMock()
        popen_proc.communicate.return_value = (
            json.dumps({"result": json.dumps(parsed_json)}), ""
        )
        popen_proc.returncode = 0
        mock_popen.return_value = popen_proc

        # --- Git commit mock (subprocess.run) ---
        mock_subrun.side_effect = [
            MagicMock(returncode=0),                                  # git add
            MagicMock(returncode=0, stdout="", stderr=""),            # git commit
            MagicMock(returncode=0, stdout="aabbccdd\n", stderr=""),  # git rev-parse
        ]

        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-test"

        # Mock execute_with_autonomous_agent to return success
        success_result = "Task completed.\n\n- Session folder: [View in OneDrive](https://example.com)"
        final_transcript = '{"from":"summarizer","message":"SUMMARY CREATED"}'
        session_stats = {"total_cost_usd": 0.10, "total_duration_ms": 5000}

        worker.execute_with_autonomous_agent = MagicMock(
            return_value=(True, success_result, final_transcript, session_stats)
        )

        task = self._make_task()
        result = worker.process_task(task)

        # -- Assertions --
        assert result is True, "process_task should return True on success"

        # current_task_id should be cleared after completion
        assert worker.current_task_id is None

        # execute_with_autonomous_agent was called with parsed prompt data
        worker.execute_with_autonomous_agent.assert_called_once()
        call_kwargs = worker.execute_with_autonomous_agent.call_args
        assert call_kwargs[1]["parsed_prompt_data"] is not None

        # update_task was called to set STATUS_COMPLETED
        # Find the PATCH call that sets cr_status to COMPLETED
        completed_update_found = False
        for patch_call in mock_patch.call_args_list:
            call_data = patch_call[1].get("json", {})
            if call_data.get("cr_status") == mod._STATUS_INT[mod.STATUS_COMPLETED]:
                completed_update_found = True
                assert "Task completed and verified" in call_data.get("cr_statusmessage", "")
                assert success_result in call_data.get("cr_result", "") or "aabbccdd" in call_data.get("cr_result", "")
                assert call_data.get("cr_transcript") == final_transcript
                break
        assert completed_update_found, "update_task should set status to STATUS_COMPLETED"

        # send_to_webhook was called with completion message
        webhook_messages = [c[0][0] for c in mock_post.call_args_list
                           if "json" in (c[1] if len(c) > 1 else {})]
        # At minimum, start notification and completion notification were sent
        assert mock_post.call_count >= 2, "At least start and completion webhooks should be sent"

    # ------------------------------------------------------------------
    # Failure path
    # ------------------------------------------------------------------

    @patch("integrated_task_worker.subprocess.Popen")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.patch")
    @patch("integrated_task_worker.requests.get")
    def test_process_task_failure(self, mock_get, mock_patch, mock_post,
                                  mock_popen, monkeypatch, tmp_path):
        """Failed task: agent returns failure, status set to STATUS_FAILED,
        error saved in result."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        # --- HTTP mocks ---
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        mock_patch.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock()
        )
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())

        # --- parse_prompt_with_llm mock ---
        parsed_json = {"task_description": "Broken task", "success_criteria": "N/A"}
        popen_proc = MagicMock()
        popen_proc.communicate.return_value = (
            json.dumps({"result": json.dumps(parsed_json)}), ""
        )
        popen_proc.returncode = 0
        mock_popen.return_value = popen_proc

        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-test"

        # Mock execute_with_autonomous_agent to return failure
        error_msg = "Max iterations (10) reached without approval"
        final_transcript = '{"from":"system","message":"[ERROR] max iterations"}'
        session_stats = {"total_cost_usd": 0.50, "total_duration_ms": 120000}

        worker.execute_with_autonomous_agent = MagicMock(
            return_value=(False, error_msg, final_transcript, session_stats)
        )

        task = self._make_task()
        result = worker.process_task(task)

        # -- Assertions --
        assert result is False, "process_task should return False on failure"

        # current_task_id should be cleared after failure
        assert worker.current_task_id is None

        # update_task was called to set STATUS_FAILED
        failed_update_found = False
        for patch_call in mock_patch.call_args_list:
            call_data = patch_call[1].get("json", {})
            if call_data.get("cr_status") == mod._STATUS_INT[mod.STATUS_FAILED]:
                failed_update_found = True
                assert "Task failed" in call_data.get("cr_statusmessage", "")
                # Result should contain the error message prefixed with "Error: "
                assert error_msg in call_data.get("cr_result", "")
                assert call_data.get("cr_transcript") == final_transcript
                break
        assert failed_update_found, "update_task should set status to STATUS_FAILED"

        # send_to_webhook was called with failure message
        assert mock_post.call_count >= 2, "At least start and failure webhooks should be sent"

    # ------------------------------------------------------------------
    # Cancellation path (agent returns canceled)
    # ------------------------------------------------------------------

    @patch("integrated_task_worker.subprocess.Popen")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.patch")
    @patch("integrated_task_worker.requests.get")
    def test_process_task_canceled(self, mock_get, mock_patch, mock_post,
                                   mock_popen, monkeypatch, tmp_path):
        """Canceled task: when execute_with_autonomous_agent detects
        cancellation (returns success=False with cancel message), the task is
        marked as FAILED with the cancellation reason."""
        mod, _ = _import_worker(monkeypatch, tmp_path)

        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        mock_patch.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock()
        )
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())

        # parse_prompt_with_llm mock
        parsed_json = {"task_description": "Cancelable task", "success_criteria": "N/A"}
        popen_proc = MagicMock()
        popen_proc.communicate.return_value = (
            json.dumps({"result": json.dumps(parsed_json)}), ""
        )
        popen_proc.returncode = 0
        mock_popen.return_value = popen_proc

        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-test"

        # execute_with_autonomous_agent returns cancellation
        cancel_msg = "Task canceled by user"
        cancel_transcript = '{"from":"system","message":"Task canceled by user"}'
        cancel_stats = {"total_cost_usd": 0.02, "total_duration_ms": 3000}

        worker.execute_with_autonomous_agent = MagicMock(
            return_value=(False, cancel_msg, cancel_transcript, cancel_stats)
        )

        task = self._make_task(cr_shraga_taskid="task-cancel-001")
        result = worker.process_task(task)

        assert result is False, "process_task should return False on cancellation"

        # current_task_id cleared
        assert worker.current_task_id is None

        # Status set to FAILED (cancellation comes through the failure branch)
        failed_found = False
        for patch_call in mock_patch.call_args_list:
            call_data = patch_call[1].get("json", {})
            if call_data.get("cr_status") == mod._STATUS_INT[mod.STATUS_FAILED]:
                failed_found = True
                # Result should contain the cancel message
                assert cancel_msg in call_data.get("cr_result", "")
                assert call_data.get("cr_transcript") == cancel_transcript
                break
        assert failed_found, "Canceled task should be marked as STATUS_FAILED"

        # Webhook notification sent with cancellation error details
        webhook_calls = mock_post.call_args_list
        failure_webhook_found = any(
            cancel_msg in str(c) for c in webhook_calls
        )
        assert failure_webhook_found, "Failure webhook should contain the cancellation message"


# ===========================================================================
# execute_with_autonomous_agent (T041)
# ===========================================================================

class TestExecuteWithAutonomousAgent:
    """Tests for execute_with_autonomous_agent() covering success and failure paths."""

    def _make_worker_and_mocks(self, monkeypatch, tmp_path):
        """Helper: import module, create worker, and set up common mocks.

        Returns (mod, worker, mock_agent_instance, session_folder).
        The worker's key external methods are mocked so the test only exercises
        the orchestration logic inside execute_with_autonomous_agent().
        """
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()

        # Create a real session folder in tmp_path
        session_folder = tmp_path / "test_session"
        session_folder.mkdir()

        # Mock create_session_folder to return our tmp session folder
        worker.create_session_folder = MagicMock(return_value=session_folder)

        # Mock AgentCLI at the module level
        mock_agent = MagicMock()
        mock_agent.setup_project.return_value = str(session_folder)

        # Patch the AgentCLI constructor in the module namespace
        monkeypatch.setattr(mod, "AgentCLI", MagicMock(return_value=mock_agent))

        # Mock merge_phase_stats so it doesn't fail on dict operations
        def fake_merge(accumulated, phase_stats):
            # Minimal merge: just copy keys over
            for key in ("total_cost_usd", "total_duration_ms", "total_turns"):
                accumulated[key] = accumulated.get(key, 0) + phase_stats.get(key, 0)
            accumulated.setdefault("tokens", {"input": 0, "output": 0})
            accumulated.setdefault("model_usage", {})
        monkeypatch.setattr(mod, "merge_phase_stats", fake_merge)

        # Mock extract_phase_stats (not directly called but imported)
        monkeypatch.setattr(mod, "extract_phase_stats", MagicMock(return_value={}))

        # Mock local_path_to_web_url
        monkeypatch.setattr(mod, "local_path_to_web_url", MagicMock(
            return_value="https://example.sharepoint.com/sessions/test"
        ))

        # Mock Dataverse-calling methods on the worker
        worker.update_task = MagicMock(return_value=True)
        worker.send_to_webhook = MagicMock(return_value=True)
        worker.is_task_canceled = MagicMock(return_value=False)
        worker.write_session_summary = MagicMock(return_value={
            "session_id": "sess-test",
            "task_id": "task-test",
            "terminal_status": "completed",
        })
        worker.write_session_log = MagicMock()
        worker.write_result_and_transcript_files = MagicMock()

        return mod, worker, mock_agent, session_folder

    # -------------------------------------------------------------------
    # SUCCESS: Worker done -> Verifier approves -> Summarizer runs
    # -------------------------------------------------------------------

    def test_execute_with_autonomous_agent_success(self, monkeypatch, tmp_path):
        """Full success path: worker completes, verifier approves, summarizer runs.

        Verifies:
        - Returns (True, result_text, transcript, stats)
        - Worker loop called once with iteration=1
        - Verifier called once with worker output
        - Summarizer called once
        - Dataverse updated with Running status
        - Session folder created and passed to agent
        - Webhook notifications sent
        - Session summary written with 'completed' terminal status
        - Result/transcript files written
        """
        mod, worker, mock_agent, session_folder = self._make_worker_and_mocks(
            monkeypatch, tmp_path
        )

        # Worker returns "done" with output
        worker_stats = {
            "cost_usd": 0.10, "duration_ms": 30000, "num_turns": 5,
            "session_id": "sess-worker-1",
        }
        mock_agent.worker_loop.return_value = ("done", "Worker completed the task.", worker_stats)

        # Verifier approves
        verifier_stats = {
            "cost_usd": 0.03, "duration_ms": 10000, "num_turns": 2,
            "session_id": "sess-verifier-1",
        }
        mock_agent.verify_work.return_value = (True, "All criteria met", verifier_stats)

        # Summarizer produces summary
        summarizer_stats = {
            "cost_usd": 0.02, "duration_ms": 5000, "num_turns": 1,
            "session_id": "sess-summarizer",
        }
        mock_agent.create_summary.return_value = ("Task summary: everything works.", summarizer_stats)

        # Execute
        parsed_prompt = {
            "task_description": "Build a REST API",
            "success_criteria": "All endpoints return 200",
        }
        success, result, transcript, stats = worker.execute_with_autonomous_agent(
            task_prompt="Build a REST API that returns 200",
            task_id="task-success-001",
            current_transcript="",
            parsed_prompt_data=parsed_prompt,
        )

        # Verify return values
        assert success is True
        assert "Task summary: everything works." in result
        assert "View in OneDrive" in result
        assert isinstance(transcript, str)
        assert isinstance(stats, dict)

        # Worker was called once (iteration 1)
        assert mock_agent.worker_loop.call_count == 1
        call_args = mock_agent.worker_loop.call_args
        assert call_args[0][0] == 1  # iteration=1
        assert call_args[0][1] is None  # no verifier feedback on first iteration

        # Verifier was called once with worker output
        assert mock_agent.verify_work.call_count == 1
        verify_call_args = mock_agent.verify_work.call_args
        assert verify_call_args[0][0] == "Worker completed the task."

        # Summarizer was called once
        assert mock_agent.create_summary.call_count == 1

        # Session folder was used for project setup
        mock_agent.setup_project.assert_called_once()
        setup_args = mock_agent.setup_project.call_args
        assert setup_args[1]["project_folder_path"] == session_folder

        # Dataverse was updated with Running status at start
        update_calls = worker.update_task.call_args_list
        # First update sets workingdir
        assert any(
            c[1].get("workingdir") == str(session_folder)
            for c in update_calls
        ), "Expected update_task called with workingdir=session_folder"
        # At least one call sets STATUS_RUNNING
        assert any(
            c[1].get("status") == mod.STATUS_RUNNING
            for c in update_calls
        ), "Expected update_task called with STATUS_RUNNING"

        # Webhook notifications were sent
        assert worker.send_to_webhook.call_count >= 2  # start + summary creation

        # Session summary was written with 'completed' status
        worker.write_session_summary.assert_called_once()
        summary_kwargs = worker.write_session_summary.call_args[1]
        assert summary_kwargs["terminal_status"] == "completed"
        assert summary_kwargs["task_id"] == "task-success-001"

        # Session log was written
        worker.write_session_log.assert_called_once()

        # Result and transcript files were written
        worker.write_result_and_transcript_files.assert_called_once()

    # -------------------------------------------------------------------
    # SUCCESS with multiple iterations: Verifier rejects first, approves second
    # -------------------------------------------------------------------

    def test_execute_with_autonomous_agent_success_after_retry(self, monkeypatch, tmp_path):
        """Verifier rejects iteration 1, worker retries, verifier approves iteration 2.

        Verifies:
        - Worker called twice (iterations 1 and 2)
        - Verifier called twice
        - Second worker call receives verifier feedback
        - Summarizer called once after final approval
        - Returns (True, ...)
        """
        mod, worker, mock_agent, session_folder = self._make_worker_and_mocks(
            monkeypatch, tmp_path
        )

        worker_stats = {"cost_usd": 0.10, "duration_ms": 20000, "num_turns": 4, "session_id": "s1"}
        verifier_stats = {"cost_usd": 0.03, "duration_ms": 8000, "num_turns": 2, "session_id": "s2"}
        summarizer_stats = {"cost_usd": 0.02, "duration_ms": 3000, "num_turns": 1, "session_id": "s3"}

        # Worker always returns done
        mock_agent.worker_loop.return_value = ("done", "Worker output", worker_stats)

        # Verifier rejects first, approves second
        mock_agent.verify_work.side_effect = [
            (False, "Tests failing: missing edge case", verifier_stats),
            (True, "All good now", verifier_stats),
        ]

        mock_agent.create_summary.return_value = ("Summary after retry", summarizer_stats)

        parsed_prompt = {
            "task_description": "Fix edge cases",
            "success_criteria": "All tests pass",
        }
        success, result, transcript, stats = worker.execute_with_autonomous_agent(
            task_prompt="Fix edge cases",
            task_id="task-retry-001",
            current_transcript="",
            parsed_prompt_data=parsed_prompt,
        )

        assert success is True
        assert "Summary after retry" in result

        # Worker called twice
        assert mock_agent.worker_loop.call_count == 2
        # Second worker call gets verifier feedback
        second_worker_call = mock_agent.worker_loop.call_args_list[1]
        assert second_worker_call[0][0] == 2  # iteration=2
        assert second_worker_call[0][1] == "Tests failing: missing edge case"

        # Verifier called twice
        assert mock_agent.verify_work.call_count == 2

        # Summarizer called once
        assert mock_agent.create_summary.call_count == 1

    # -------------------------------------------------------------------
    # FAILURE: Exception during execution
    # -------------------------------------------------------------------

    def test_execute_with_autonomous_agent_failure_exception(self, monkeypatch, tmp_path):
        """An exception during worker_loop should be caught and return failure.

        Verifies:
        - Returns (False, error_message, transcript, stats)
        - Error message contains the exception text
        - Session summary written with 'failed' terminal status
        - Result/transcript files still written (graceful degradation)
        """
        mod, worker, mock_agent, session_folder = self._make_worker_and_mocks(
            monkeypatch, tmp_path
        )

        # Worker raises an exception
        mock_agent.worker_loop.side_effect = RuntimeError("Claude CLI process crashed")

        parsed_prompt = {
            "task_description": "Run analysis",
            "success_criteria": "Report generated",
        }
        success, result, transcript, stats = worker.execute_with_autonomous_agent(
            task_prompt="Run analysis",
            task_id="task-error-001",
            current_transcript="",
            parsed_prompt_data=parsed_prompt,
        )

        # Returns failure
        assert success is False
        assert "Claude CLI process crashed" in result
        assert "Error during autonomous execution" in result

        # Transcript contains the error
        assert "[ERROR]" in transcript

        # Session summary written with 'failed'
        worker.write_session_summary.assert_called_once()
        summary_kwargs = worker.write_session_summary.call_args[1]
        assert summary_kwargs["terminal_status"] == "failed"

        # Result/transcript files still written
        worker.write_result_and_transcript_files.assert_called_once()

    # -------------------------------------------------------------------
    # FAILURE: Max iterations reached without approval
    # -------------------------------------------------------------------

    def test_execute_with_autonomous_agent_failure_max_iterations(self, monkeypatch, tmp_path):
        """Verifier never approves across all 10 iterations -- task should fail.

        Verifies:
        - Returns (False, "Max iterations..." message, transcript, stats)
        - Worker called 10 times
        - Verifier called 10 times
        - Summarizer NOT called
        - Session summary written with 'failed' terminal status
        """
        mod, worker, mock_agent, session_folder = self._make_worker_and_mocks(
            monkeypatch, tmp_path
        )

        worker_stats = {"cost_usd": 0.01, "duration_ms": 5000, "num_turns": 1, "session_id": "s"}
        verifier_stats = {"cost_usd": 0.01, "duration_ms": 3000, "num_turns": 1, "session_id": "s"}

        mock_agent.worker_loop.return_value = ("done", "Worker output", worker_stats)
        mock_agent.verify_work.return_value = (False, "Still not right", verifier_stats)

        parsed_prompt = {
            "task_description": "Impossible task",
            "success_criteria": "Never met",
        }
        success, result, transcript, stats = worker.execute_with_autonomous_agent(
            task_prompt="Impossible task",
            task_id="task-maxiter-001",
            current_transcript="",
            parsed_prompt_data=parsed_prompt,
        )

        assert success is False
        assert "Max iterations" in result

        # Worker and verifier each called 10 times
        assert mock_agent.worker_loop.call_count == 10
        assert mock_agent.verify_work.call_count == 10

        # Summarizer NOT called (no approval)
        assert mock_agent.create_summary.call_count == 0

        # Session summary written with 'failed'
        worker.write_session_summary.assert_called_once()
        summary_kwargs = worker.write_session_summary.call_args[1]
        assert summary_kwargs["terminal_status"] == "failed"

    # -------------------------------------------------------------------
    # FAILURE: Task canceled before worker starts iteration
    # -------------------------------------------------------------------

    def test_execute_with_autonomous_agent_failure_canceled(self, monkeypatch, tmp_path):
        """Task is canceled before the first worker iteration.

        Verifies:
        - Returns (False, "Task canceled by user", transcript, stats)
        - Worker, verifier, and summarizer are NOT called
        - Session summary written with 'canceled' terminal status
        - Webhook notification sent about cancellation
        """
        mod, worker, mock_agent, session_folder = self._make_worker_and_mocks(
            monkeypatch, tmp_path
        )

        # Task is immediately canceled
        worker.is_task_canceled.return_value = True

        parsed_prompt = {
            "task_description": "Canceled task",
            "success_criteria": "N/A",
        }
        success, result, transcript, stats = worker.execute_with_autonomous_agent(
            task_prompt="Canceled task",
            task_id="task-cancel-001",
            current_transcript="",
            parsed_prompt_data=parsed_prompt,
        )

        assert success is False
        assert "canceled" in result.lower()

        # Worker, verifier, summarizer NOT called
        assert mock_agent.worker_loop.call_count == 0
        assert mock_agent.verify_work.call_count == 0
        assert mock_agent.create_summary.call_count == 0

        # Session summary written with 'canceled'
        worker.write_session_summary.assert_called_once()
        summary_kwargs = worker.write_session_summary.call_args[1]
        assert summary_kwargs["terminal_status"] == "canceled"

        # Cancellation webhook sent
        webhook_args = [str(c) for c in worker.send_to_webhook.call_args_list]
        assert any("cancel" in a.lower() for a in webhook_args)

    # -------------------------------------------------------------------
    # Uses LLM parser when no parsed_prompt_data provided
    # -------------------------------------------------------------------

    def test_execute_with_autonomous_agent_calls_llm_parser(self, monkeypatch, tmp_path):
        """When parsed_prompt_data is None, parse_prompt_with_llm is called.

        Verifies the LLM parser is invoked with the raw prompt text when
        no pre-parsed data is provided.
        """
        mod, worker, mock_agent, session_folder = self._make_worker_and_mocks(
            monkeypatch, tmp_path
        )

        # Mock parse_prompt_with_llm
        worker.parse_prompt_with_llm = MagicMock(return_value={
            "task_description": "Parsed description",
            "success_criteria": "Parsed criteria",
        })

        # Worker immediately fails to keep test short
        mock_agent.worker_loop.side_effect = RuntimeError("Quick fail for test")

        success, result, transcript, stats = worker.execute_with_autonomous_agent(
            task_prompt="Raw unstructured prompt",
            task_id="task-parser-001",
            current_transcript="",
            parsed_prompt_data=None,  # No pre-parsed data
        )

        # parse_prompt_with_llm was called with the raw prompt
        worker.parse_prompt_with_llm.assert_called_once_with("Raw unstructured prompt")

    # -------------------------------------------------------------------
    # Stats accumulation across phases
    # -------------------------------------------------------------------

    def test_execute_with_autonomous_agent_accumulates_stats(self, monkeypatch, tmp_path):
        """Stats from worker, verifier, and summarizer phases are accumulated.

        Verifies:
        - Returned stats dict contains accumulated values from all phases
        - The accumulated stats reflect worker + verifier + summarizer costs
        """
        mod, worker, mock_agent, session_folder = self._make_worker_and_mocks(
            monkeypatch, tmp_path
        )

        worker_stats = {"cost_usd": 0.10, "duration_ms": 30000, "num_turns": 5, "session_id": "w1",
                        "total_cost_usd": 0.10, "total_duration_ms": 30000, "total_turns": 5}
        verifier_stats = {"cost_usd": 0.03, "duration_ms": 10000, "num_turns": 2, "session_id": "v1",
                          "total_cost_usd": 0.03, "total_duration_ms": 10000, "total_turns": 2}
        summarizer_stats = {"cost_usd": 0.02, "duration_ms": 5000, "num_turns": 1, "session_id": "s1",
                            "total_cost_usd": 0.02, "total_duration_ms": 5000, "total_turns": 1}

        mock_agent.worker_loop.return_value = ("done", "Output", worker_stats)
        mock_agent.verify_work.return_value = (True, "Approved", verifier_stats)
        mock_agent.create_summary.return_value = ("Summary", summarizer_stats)

        parsed_prompt = {
            "task_description": "Test stats",
            "success_criteria": "Stats accumulated",
        }
        success, result, transcript, stats = worker.execute_with_autonomous_agent(
            task_prompt="Test stats",
            task_id="task-stats-001",
            current_transcript="",
            parsed_prompt_data=parsed_prompt,
        )

        assert success is True
        # Stats should be accumulated (our fake_merge sums total_cost_usd, etc.)
        assert stats.get("total_cost_usd", 0) > 0
        assert stats.get("total_duration_ms", 0) > 0
        assert stats.get("total_turns", 0) > 0


# ===========================================================================
# is_task_canceled + run() loop continuation (GAP-T01 / T042)
# ===========================================================================

class TestIsTaskCanceled:
    """Tests for IntegratedTaskWorker.is_task_canceled()."""

    @patch("integrated_task_worker.requests.get")
    def test_is_task_canceled_true(self, mock_get, monkeypatch, tmp_path):
        """When Dataverse returns cr_status == STATUS_CANCELED, is_task_canceled returns True."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"cr_status": mod.STATUS_CANCELED},
        )
        worker = mod.IntegratedTaskWorker()
        result = worker.is_task_canceled("task-cancel-001")
        assert result is True

        # Verify the correct URL was called with $select=cr_status
        call_args = mock_get.call_args
        url_called = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "task-cancel-001" in url_called
        assert "$select=cr_status" in url_called

    @patch("integrated_task_worker.requests.get")
    def test_is_task_canceled_false(self, mock_get, monkeypatch, tmp_path):
        """When Dataverse returns a non-canceled status (e.g., STATUS_RUNNING), returns False."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"cr_status": mod.STATUS_RUNNING},
        )
        worker = mod.IntegratedTaskWorker()
        result = worker.is_task_canceled("task-running-001")
        assert result is False

    @patch("integrated_task_worker.requests.get")
    def test_is_task_canceled_false_for_completed(self, mock_get, monkeypatch, tmp_path):
        """A completed task (status 7) is not canceled."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"cr_status": mod.STATUS_COMPLETED},
        )
        worker = mod.IntegratedTaskWorker()
        result = worker.is_task_canceled("task-completed-001")
        assert result is False

    @patch("integrated_task_worker.requests.get")
    def test_is_task_canceled_false_for_pending(self, mock_get, monkeypatch, tmp_path):
        """A pending task (status 1) is not canceled."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"cr_status": mod.STATUS_PENDING},
        )
        worker = mod.IntegratedTaskWorker()
        result = worker.is_task_canceled("task-pending-001")
        assert result is False

    @patch("integrated_task_worker.requests.get")
    def test_is_task_canceled_api_error(self, mock_get, monkeypatch, tmp_path):
        """When the Dataverse API call raises an exception, returns False (fail-open)."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.side_effect = Exception("Network unreachable")
        worker = mod.IntegratedTaskWorker()
        result = worker.is_task_canceled("task-error-001")
        assert result is False

    @patch("integrated_task_worker.requests.get")
    def test_is_task_canceled_api_timeout(self, mock_get, monkeypatch, tmp_path):
        """When the Dataverse API call times out, returns False (fail-open)."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.Timeout("Connection timed out")
        worker = mod.IntegratedTaskWorker()
        result = worker.is_task_canceled("task-timeout-001")
        assert result is False

    @patch("integrated_task_worker.requests.get")
    def test_is_task_canceled_non_200_response(self, mock_get, monkeypatch, tmp_path):
        """When Dataverse returns a non-200 status code (e.g. 404), returns False."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            status_code=404,
            json=lambda: {"error": "not found"},
        )
        worker = mod.IntegratedTaskWorker()
        result = worker.is_task_canceled("task-notfound-001")
        assert result is False

    def test_is_task_canceled_empty_task_id(self, monkeypatch, tmp_path):
        """When task_id is empty string, returns False immediately without API call."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        with patch("integrated_task_worker.requests.get") as mock_get:
            result = worker.is_task_canceled("")
            assert result is False
            # Should not have made any API call
            mock_get.assert_not_called()

    def test_is_task_canceled_none_task_id(self, monkeypatch, tmp_path):
        """When task_id is None, returns False immediately without API call."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        with patch("integrated_task_worker.requests.get") as mock_get:
            result = worker.is_task_canceled(None)
            assert result is False
            mock_get.assert_not_called()

    def test_is_task_canceled_no_auth_token(self, monkeypatch, tmp_path):
        """When auth token is unavailable, worker exits (scheduler restarts it)."""
        mod, mock_cred = _import_worker(monkeypatch, tmp_path)
        mock_cred.get_token.side_effect = Exception("Auth failed")
        worker = mod.IntegratedTaskWorker()
        worker._token_cache = None
        worker._token_expires = None
        import pytest
        with pytest.raises(SystemExit):
            worker.is_task_canceled("task-noauth-001")


class TestRunLoopContinuesAfterTask:
    """Tests for run() loop continuation behavior -- the worker must keep
    polling after processing a task, not exit after the first task."""

    def _make_worker(self, monkeypatch, tmp_path):
        """Helper: import module and create a worker with user ID pre-set."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-test-run"
        return mod, worker

    @patch("integrated_task_worker.time.sleep")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.get")
    def test_run_loop_continues_after_task(self, mock_get, mock_post, mock_sleep, monkeypatch, tmp_path):
        """After processing a task (success or failure), the run() loop must
        continue polling for more tasks rather than exiting.

        This test verifies the core invariant: poll -> process -> poll -> ...
        The worker exits only on KeyboardInterrupt.
        """
        mod, worker = self._make_worker(monkeypatch, tmp_path)

        poll_call_count = 0

        def fake_poll():
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count == 1:
                # First poll: return a task
                return [{
                    "cr_shraga_taskid": "task-loop-001",
                    "cr_name": "Loop Test Task",
                    "@odata.etag": 'W/"100"',
                }]
            elif poll_call_count == 2:
                # Second poll: return another task (proves we looped back)
                return [{
                    "cr_shraga_taskid": "task-loop-002",
                    "cr_name": "Loop Test Task 2",
                    "@odata.etag": 'W/"200"',
                }]
            elif poll_call_count == 3:
                # Third poll: no tasks (idle cycle)
                return []
            else:
                # Fourth poll: exit
                raise KeyboardInterrupt()

        worker.poll_pending_tasks = fake_poll
        worker.process_task = MagicMock(return_value=True)  # Tasks succeed
        worker.send_to_webhook = MagicMock()
        worker.check_for_updates = MagicMock(return_value=False)
        worker.last_update_check = datetime.now(tz=timezone.utc)

        worker.run()

        # process_task was called twice -- once for each task
        assert worker.process_task.call_count == 2
        # The worker polled 4 times total before the KeyboardInterrupt
        assert poll_call_count == 4
        # Verify both tasks were passed to process_task
        first_task = worker.process_task.call_args_list[0][0][0]
        second_task = worker.process_task.call_args_list[1][0][0]
        assert first_task["cr_shraga_taskid"] == "task-loop-001"
        assert second_task["cr_shraga_taskid"] == "task-loop-002"

    @patch("integrated_task_worker.time.sleep")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.get")
    def test_run_loop_continues_after_exception_in_process_task(self, mock_get, mock_post, mock_sleep, monkeypatch, tmp_path):
        """If process_task raises an unhandled exception, the run() loop must
        catch it, clean up, and continue polling -- not crash."""
        mod, worker = self._make_worker(monkeypatch, tmp_path)

        poll_call_count = 0

        def fake_poll():
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count == 1:
                # First poll: return a task that will cause an explosion
                return [{
                    "cr_shraga_taskid": "task-explode",
                    "cr_name": "Exploding Task",
                }]
            elif poll_call_count == 2:
                # Second poll: return a normal task (proves recovery)
                return [{
                    "cr_shraga_taskid": "task-normal",
                    "cr_name": "Normal Task",
                }]
            elif poll_call_count == 3:
                return []
            else:
                raise KeyboardInterrupt()

        call_count = 0

        def fake_process(task):
            nonlocal call_count
            call_count += 1
            if task.get("cr_shraga_taskid") == "task-explode":
                raise RuntimeError("Unhandled task explosion!")
            return True

        worker.poll_pending_tasks = fake_poll
        worker.process_task = fake_process
        worker.send_to_webhook = MagicMock()
        worker._cleanup_in_progress_task = MagicMock()
        worker.check_for_updates = MagicMock(return_value=False)
        worker.last_update_check = datetime.now(tz=timezone.utc)

        worker.run()

        # Both tasks were attempted
        assert call_count == 2
        # Cleanup was called for the exploding task
        worker._cleanup_in_progress_task.assert_called()
        # The worker recovered and polled again
        assert poll_call_count == 4

    @patch("integrated_task_worker.time.sleep")
    @patch("integrated_task_worker.requests.post")
    @patch("integrated_task_worker.requests.get")
    def test_run_loop_normal_sleep_between_iterations(self, mock_get, mock_post, mock_sleep, monkeypatch, tmp_path):
        """On normal polling (no errors), the worker sleeps 10 seconds between iterations."""
        mod, worker = self._make_worker(monkeypatch, tmp_path)

        poll_call_count = 0

        def fake_poll():
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count <= 2:
                return []  # Empty poll cycles
            raise KeyboardInterrupt()

        worker.poll_pending_tasks = fake_poll
        worker.send_to_webhook = MagicMock()
        worker.check_for_updates = MagicMock(return_value=False)
        worker.last_update_check = datetime.now(tz=timezone.utc)

        worker.run()

        # Verify that time.sleep(10) was called (normal polling interval)
        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert 10 in sleep_calls, f"Expected 10s sleep for normal polling, got: {sleep_calls}"


# ===========================================================================
# Session folder enrichment (T048)
# ===========================================================================

class TestSessionFolderEnrichment:
    """Tests for T048: Enrich OneDrive Session Folder Contents.

    Verifies that session folders contain ALL key artifacts:
    - TASK_PROMPT.md (full raw prompt)
    - SUCCESS_CRITERIA.md (extracted success criteria)
    - GIT_HISTORY.md (git commit history)
    """

    def _make_worker(self, monkeypatch, tmp_path):
        """Helper: import module and create worker instance."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        return mod, worker

    # -------------------------------------------------------------------
    # test_session_folder_contains_task_prompt
    # -------------------------------------------------------------------

    def test_session_folder_contains_task_prompt(self, monkeypatch, tmp_path):
        """write_task_prompt_file creates TASK_PROMPT.md and SUCCESS_CRITERIA.md
        in the session folder with the full raw prompt and success criteria.

        Verifies:
        - TASK_PROMPT.md exists and contains the exact raw prompt text
        - SUCCESS_CRITERIA.md exists and contains the extracted criteria
        - Both files use UTF-8 encoding and have the expected markdown heading
        """
        mod, worker = self._make_worker(monkeypatch, tmp_path)
        session_folder = tmp_path / "session_prompt_test"
        session_folder.mkdir()

        raw_prompt = (
            "Create a REST API for managing user profiles.\n"
            "Use Python Flask.\n"
            "Include authentication and CRUD endpoints."
        )
        success_criteria = (
            "All endpoints return correct HTTP status codes.\n"
            "Tests pass with 100% coverage."
        )

        worker.write_task_prompt_file(session_folder, raw_prompt, success_criteria)

        # --- TASK_PROMPT.md ---
        task_prompt_file = session_folder / "TASK_PROMPT.md"
        assert task_prompt_file.exists(), "TASK_PROMPT.md should be created"
        prompt_content = task_prompt_file.read_text(encoding="utf-8")
        assert "# Full Task Prompt" in prompt_content, "Should have markdown heading"
        assert raw_prompt in prompt_content, "Should contain the full raw prompt text"
        # Verify it's not a truncated version
        assert "REST API for managing user profiles" in prompt_content
        assert "Include authentication and CRUD endpoints" in prompt_content

        # --- SUCCESS_CRITERIA.md ---
        criteria_file = session_folder / "SUCCESS_CRITERIA.md"
        assert criteria_file.exists(), "SUCCESS_CRITERIA.md should be created"
        criteria_content = criteria_file.read_text(encoding="utf-8")
        assert "# Success Criteria" in criteria_content, "Should have markdown heading"
        assert success_criteria in criteria_content, "Should contain the full success criteria"
        assert "100% coverage" in criteria_content

    def test_session_folder_contains_task_prompt_handles_empty_inputs(self, monkeypatch, tmp_path):
        """write_task_prompt_file handles empty/None inputs gracefully."""
        mod, worker = self._make_worker(monkeypatch, tmp_path)
        session_folder = tmp_path / "session_empty_test"
        session_folder.mkdir()

        worker.write_task_prompt_file(session_folder, "", "")

        # Files should still be created with just the heading
        task_prompt_file = session_folder / "TASK_PROMPT.md"
        assert task_prompt_file.exists()
        content = task_prompt_file.read_text(encoding="utf-8")
        assert "# Full Task Prompt" in content

        criteria_file = session_folder / "SUCCESS_CRITERIA.md"
        assert criteria_file.exists()
        criteria_content = criteria_file.read_text(encoding="utf-8")
        assert "# Success Criteria" in criteria_content

    # -------------------------------------------------------------------
    # test_session_folder_contains_generated_files
    # -------------------------------------------------------------------

    @patch("integrated_task_worker.subprocess.run")
    def test_session_folder_contains_generated_files(self, mock_run, monkeypatch, tmp_path):
        """capture_git_history writes GIT_HISTORY.md to the session folder.

        Also exercises the full integration: after execute_with_autonomous_agent
        completes (any terminal state), the session folder should contain:
        - TASK_PROMPT.md  (raw prompt)
        - SUCCESS_CRITERIA.md (criteria)
        - GIT_HISTORY.md  (commit log)
        - Plus existing files: result.md, transcript.md, session_summary.json, SESSION_LOG.md

        This test verifies capture_git_history directly and then verifies
        that the finalization path in execute_with_autonomous_agent calls it.
        """
        mod, worker = self._make_worker(monkeypatch, tmp_path)
        session_folder = tmp_path / "session_generated_test"
        session_folder.mkdir()

        # --- Part 1: Direct capture_git_history test ---
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc1234 Initial commit\ndef5678 Add feature X\nghi9012 Fix bug Y\n",
            stderr="",
        )

        worker.capture_git_history(session_folder)

        git_history_file = session_folder / "GIT_HISTORY.md"
        assert git_history_file.exists(), "GIT_HISTORY.md should be created"
        history_content = git_history_file.read_text(encoding="utf-8")
        assert "# Git Commit History" in history_content, "Should have markdown heading"
        assert "abc1234 Initial commit" in history_content, "Should contain commit entries"
        assert "def5678 Add feature X" in history_content
        assert "ghi9012 Fix bug Y" in history_content

        # Verify git log was called with expected args
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "git" in call_args[0][0]
        assert "log" in call_args[0][0]
        assert "--oneline" in call_args[0][0]

        # --- Part 2: Verify capture_git_history handles failures gracefully ---
        mock_run.reset_mock()
        session_folder_2 = tmp_path / "session_git_fail"
        session_folder_2.mkdir()

        mock_run.return_value = MagicMock(
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository",
        )

        worker.capture_git_history(session_folder_2)

        git_history_file_2 = session_folder_2 / "GIT_HISTORY.md"
        assert git_history_file_2.exists(), "GIT_HISTORY.md should still be created on failure"
        content_2 = git_history_file_2.read_text(encoding="utf-8")
        assert "git log failed" in content_2, "Should indicate the failure"

    @patch("integrated_task_worker.subprocess.run")
    def test_capture_git_history_uses_work_dir(self, mock_run, monkeypatch, tmp_path):
        """capture_git_history passes work_dir to git log's cwd parameter."""
        mod, worker = self._make_worker(monkeypatch, tmp_path)
        session_folder = tmp_path / "session_cwd_test"
        session_folder.mkdir()
        custom_work_dir = tmp_path / "custom_repo"
        custom_work_dir.mkdir()

        mock_run.return_value = MagicMock(returncode=0, stdout="aaa1111 commit msg\n", stderr="")

        worker.capture_git_history(session_folder, work_dir=custom_work_dir)

        # Verify cwd was set to the custom work dir
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == str(custom_work_dir)

    def test_execute_calls_write_task_prompt_and_capture_git_history(self, monkeypatch, tmp_path):
        """execute_with_autonomous_agent calls write_task_prompt_file early
        and capture_git_history during finalization.

        This is an integration-level test that verifies the orchestration
        method wires the new T048 functions into the execution pipeline.
        """
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()

        # Create a real session folder
        session_folder = tmp_path / "integration_session"
        session_folder.mkdir()

        # Mock all external dependencies
        worker.create_session_folder = MagicMock(return_value=session_folder)

        mock_agent = MagicMock()
        mock_agent.setup_project.return_value = str(session_folder)
        monkeypatch.setattr(mod, "AgentCLI", MagicMock(return_value=mock_agent))

        def fake_merge(accumulated, phase_stats):
            for key in ("total_cost_usd", "total_duration_ms", "total_turns"):
                accumulated[key] = accumulated.get(key, 0) + phase_stats.get(key, 0)
            accumulated.setdefault("tokens", {"input": 0, "output": 0})
            accumulated.setdefault("model_usage", {})
        monkeypatch.setattr(mod, "merge_phase_stats", fake_merge)
        monkeypatch.setattr(mod, "extract_phase_stats", MagicMock(return_value={}))
        monkeypatch.setattr(mod, "local_path_to_web_url", MagicMock(return_value="https://example.com"))

        worker.update_task = MagicMock(return_value=True)
        worker.send_to_webhook = MagicMock(return_value=True)
        worker.is_task_canceled = MagicMock(return_value=False)
        worker.write_session_summary = MagicMock(return_value={
            "session_id": "s1", "task_id": "t1", "terminal_status": "completed"
        })
        worker.write_session_log = MagicMock()
        worker.write_result_and_transcript_files = MagicMock()

        # Mock write_task_prompt_file and capture_git_history to track calls
        worker.write_task_prompt_file = MagicMock()
        worker.capture_git_history = MagicMock()

        # Worker succeeds immediately
        worker_stats = {"cost_usd": 0.05, "duration_ms": 10000, "num_turns": 3, "session_id": "w"}
        mock_agent.worker_loop.return_value = ("done", "Done.", worker_stats)
        mock_agent.verify_work.return_value = (True, "OK", {"cost_usd": 0.01, "duration_ms": 5000, "num_turns": 1, "session_id": "v"})
        mock_agent.create_summary.return_value = ("Summary text", {"cost_usd": 0.01, "duration_ms": 2000, "num_turns": 1, "session_id": "s"})

        parsed_prompt = {
            "task_description": "Build a widget",
            "success_criteria": "Widget works",
        }

        success, result, transcript, stats = worker.execute_with_autonomous_agent(
            task_prompt="Build a widget that does X, Y, Z",
            task_id="task-t048-001",
            current_transcript="",
            parsed_prompt_data=parsed_prompt,
        )

        assert success is True

        # write_task_prompt_file was called with raw prompt and success criteria
        worker.write_task_prompt_file.assert_called_once_with(
            session_folder,
            "Build a widget that does X, Y, Z",
            "Widget works",
        )

        # capture_git_history was called during finalization
        worker.capture_git_history.assert_called_once_with(session_folder)


# ===========================================================================
# Short description generation (T047)
# ===========================================================================

class TestGenerateShortDescription:

    def test_generate_short_description_success(self, monkeypatch, tmp_path):
        """Test that generate_short_description returns LLM-generated summary."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()

        # Mock subprocess.Popen for Claude CLI call
        fake_response = json.dumps({
            "result": "Create a REST API endpoint for user authentication with JWT tokens."
        })
        mock_popen = MagicMock()
        mock_popen.communicate.return_value = (fake_response, "")
        mock_popen.returncode = 0

        with patch("subprocess.Popen", return_value=mock_popen):
            result = worker.generate_short_description(
                "Build a REST API that handles user login, registration, and "
                "password reset using JWT tokens. The API should support "
                "email/password auth and OAuth2 with Google."
            )

        assert result == "Create a REST API endpoint for user authentication with JWT tokens."
        assert len(result) <= 200

    def test_generate_short_description_strips_quotes(self, monkeypatch, tmp_path):
        """Test that wrapping quotes are stripped from the LLM result."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()

        fake_response = json.dumps({
            "result": '"Build a hello world script in Python."'
        })
        mock_popen = MagicMock()
        mock_popen.communicate.return_value = (fake_response, "")
        mock_popen.returncode = 0

        with patch("subprocess.Popen", return_value=mock_popen):
            result = worker.generate_short_description("Build a hello world script")

        assert result == "Build a hello world script in Python."
        assert not result.startswith('"')
        assert not result.endswith('"')

    def test_generate_short_description_truncates_long_result(self, monkeypatch, tmp_path):
        """Test that results over 200 chars are truncated."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()

        long_text = "A" * 300
        fake_response = json.dumps({"result": long_text})
        mock_popen = MagicMock()
        mock_popen.communicate.return_value = (fake_response, "")
        mock_popen.returncode = 0

        with patch("subprocess.Popen", return_value=mock_popen):
            result = worker.generate_short_description("Some task")

        assert len(result) <= 200
        assert result.endswith("...")

    def test_generate_short_description_fallback_on_timeout(self, monkeypatch, tmp_path):
        """Test that timeout falls back to truncated raw prompt."""
        import subprocess as real_subprocess
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()

        mock_popen = MagicMock()
        mock_popen.communicate.side_effect = real_subprocess.TimeoutExpired(
            cmd="claude", timeout=30
        )

        with patch("subprocess.Popen", return_value=mock_popen):
            result = worker.generate_short_description(
                "Build a REST API " + "x" * 200
            )

        assert len(result) <= 130  # 120 + "..."
        assert result.endswith("...")

    def test_generate_short_description_fallback_on_error(self, monkeypatch, tmp_path):
        """Test that errors fall back to truncated raw prompt."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()

        mock_popen = MagicMock()
        mock_popen.communicate.return_value = ("not-json", "")
        mock_popen.returncode = 0

        with patch("subprocess.Popen", return_value=mock_popen):
            result = worker.generate_short_description("Short task prompt")

        # Should fall back to the raw prompt since it's short enough
        assert "Short task prompt" in result

    def test_generate_short_description_fallback_on_cli_failure(self, monkeypatch, tmp_path):
        """Test that Claude CLI failure falls back to truncated raw prompt."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()

        mock_popen = MagicMock()
        mock_popen.communicate.return_value = ("", "Error: auth failed")
        mock_popen.returncode = 1

        with patch("subprocess.Popen", return_value=mock_popen):
            result = worker.generate_short_description("My task description")

        assert "My task description" in result


class TestUpdateTaskShortDescription:

    def test_update_task_includes_short_description(self, monkeypatch, tmp_path):
        """Test that update_task sends crb3b_shortdescription to Dataverse."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()

        with patch("requests.patch") as mock_patch:
            mock_patch.return_value = MagicMock(status_code=204)
            mock_patch.return_value.raise_for_status = MagicMock()

            worker.update_task(
                "task-123",
                short_description="Build a REST API for auth."
            )

            # Verify the PATCH was called with crb3b_shortdescription in the body
            call_args = mock_patch.call_args
            body = call_args[1]["json"]
            assert body["crb3b_shortdescription"] == "Build a REST API for auth."
