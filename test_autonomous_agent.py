"""Tests for autonomous_agent.py – AgentCLI class"""
import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open
from datetime import datetime

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
from autonomous_agent import AgentCLI, extract_phase_stats, merge_phase_stats


# ===========================================================================
# setup_project
# ===========================================================================

class TestSetupProject:
    """Tests for AgentCLI.setup_project"""

    def test_creates_project_folder(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli = AgentCLI()
        folder = cli.setup_project("Do something", "Tests pass")
        assert folder.exists()
        assert folder.is_dir()

    def test_creates_task_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli = AgentCLI()
        folder = cli.setup_project("Build API", "All endpoints work")
        task_file = folder / "TASK.md"
        assert task_file.exists()
        content = task_file.read_text(encoding="utf-8")
        assert "Build API" in content
        assert "READ ONLY" in content

    def test_creates_verification_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli = AgentCLI()
        folder = cli.setup_project("Task", "100% coverage")
        vf = folder / "VERIFICATION.md"
        assert vf.exists()
        content = vf.read_text(encoding="utf-8")
        assert "100% coverage" in content

    def test_folder_name_contains_timestamp(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli = AgentCLI()
        folder = cli.setup_project("T", "C")
        assert "agent_task_" in folder.name

    def test_sets_internal_state(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli = AgentCLI()
        cli.setup_project("T", "C")
        assert cli.project_folder is not None
        assert cli.task_file is not None
        assert cli.verification_file is not None


# ===========================================================================
# worker_loop
# ===========================================================================

class TestWorkerLoop:

    def _make_cli(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli = AgentCLI()
        cli.setup_project("Build hello world", "Script prints hello")
        return cli

    @patch.object(AgentCLI, "call_claude")
    def test_returns_done_when_status_done(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {"result": "I created the script.\n\nSTATUS: done"}
        status, output, phase_stats = cli.worker_loop(1)
        assert status == "done"
        assert "STATUS: done" in output
        assert isinstance(phase_stats, dict)

    @patch.object(AgentCLI, "call_claude")
    def test_defaults_to_done_on_unclear(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {"result": "I'm not sure what to do"}
        status, output, _stats = cli.worker_loop(1)
        assert status == "done"

    @patch.object(AgentCLI, "call_claude")
    def test_passes_verifier_feedback(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {"result": "Fixed issues.\n\nSTATUS: done"}
        status, output, _stats = cli.worker_loop(2, verifier_feedback="Tests failing")
        # Verify the prompt sent to call_claude includes verifier feedback
        call_args = mock_call.call_args
        prompt = call_args[0][0]
        assert "Tests failing" in prompt

    @patch.object(AgentCLI, "call_claude")
    def test_on_event_callback_received(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {"result": "Done.\nSTATUS: done"}
        events = []
        def on_event(et, d):
            events.append((et, d))
        cli.worker_loop(1, on_event=on_event)
        # on_event is passed through to call_claude
        assert mock_call.call_args[1].get("on_event") == on_event


# ===========================================================================
# verify_work
# ===========================================================================

class TestVerifyWork:

    def _make_cli(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli = AgentCLI()
        cli.setup_project("T", "C")
        return cli

    @patch.object(AgentCLI, "call_claude")
    def test_approved_when_verdict_true(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {"result": "All good"}

        # Write VERDICT.json
        verdict = {
            "approved": True,
            "feedback": "",
            "testing_done": "ran tests",
            "results": "all pass",
            "criteria_met": ["test1"],
            "criteria_failed": [],
            "expert_comparison": "good"
        }
        (cli.project_folder / "VERDICT.json").write_text(
            json.dumps(verdict), encoding="utf-8"
        )

        approved, feedback, phase_stats = cli.verify_work("worker output")
        assert approved is True
        assert feedback is None
        assert isinstance(phase_stats, dict)

    @patch.object(AgentCLI, "call_claude")
    def test_not_approved_when_verdict_false(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {"result": "Issues found"}

        verdict = {
            "approved": False,
            "feedback": "Tests are failing",
            "testing_done": "ran tests",
            "results": "2 failures",
            "criteria_met": [],
            "criteria_failed": ["test1"],
            "expert_comparison": "below baseline"
        }
        (cli.project_folder / "VERDICT.json").write_text(
            json.dumps(verdict), encoding="utf-8"
        )

        approved, feedback, phase_stats = cli.verify_work("worker output")
        assert approved is False
        assert "Tests are failing" in feedback
        assert isinstance(phase_stats, dict)

    @patch.object(AgentCLI, "call_claude")
    def test_returns_false_when_no_verdict_file(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {"result": "oops"}

        approved, feedback, _stats = cli.verify_work("worker output")
        assert approved is False
        assert "VERDICT.json" in feedback

    @patch.object(AgentCLI, "call_claude")
    def test_returns_false_on_invalid_json(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {"result": "done"}

        (cli.project_folder / "VERDICT.json").write_text(
            "not valid json {{{", encoding="utf-8"
        )

        approved, feedback, _stats = cli.verify_work("worker output")
        assert approved is False

    @patch.object(AgentCLI, "call_claude")
    def test_returns_false_when_approved_not_bool(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {"result": "done"}

        verdict = {"approved": "yes", "feedback": ""}
        (cli.project_folder / "VERDICT.json").write_text(
            json.dumps(verdict), encoding="utf-8"
        )

        approved, feedback, _stats = cli.verify_work("worker output")
        assert approved is False


# ===========================================================================
# create_summary
# ===========================================================================

class TestCreateSummary:

    def _make_cli(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli = AgentCLI()
        cli.setup_project("T", "C")
        return cli

    @patch.object(AgentCLI, "call_claude")
    def test_returns_summary_from_file(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {"result": "Summary created"}

        summary_text = "# Summary\nEverything worked."
        (cli.project_folder / "SUMMARY.md").write_text(summary_text, encoding="utf-8")

        result, phase_stats = cli.create_summary()
        assert "Everything worked" in result
        assert isinstance(phase_stats, dict)

    @patch.object(AgentCLI, "call_claude")
    def test_returns_raw_response_if_no_file(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {"result": "Summarizer raw output"}

        result, phase_stats = cli.create_summary()
        assert "Summarizer raw output" in result
        assert isinstance(phase_stats, dict)


# ===========================================================================
# call_claude – cmd construction
# ===========================================================================

class TestCallClaudeCmdConstruction:
    """Tests verifying the cmd list built by call_claude()"""

    def _make_cli(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli = AgentCLI()
        cli.setup_project("T", "C")
        return cli

    @patch("autonomous_agent.subprocess.Popen")
    def test_call_claude_includes_skip_permissions(self, mock_popen, tmp_path, monkeypatch):
        """Verify --dangerously-skip-permissions is in the cmd list"""
        cli = self._make_cli(tmp_path, monkeypatch)

        # Configure the mock process returned by Popen
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.returncode = 0
        # Non-streaming mode returns JSON from communicate()
        mock_process.communicate.return_value = ('{"result": "ok"}', '')
        mock_popen.return_value = mock_process

        cli.call_claude("test prompt", cli.project_folder, stream=False)

        # Extract the cmd list passed to Popen
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert "--dangerously-skip-permissions" in cmd

    @patch("autonomous_agent.subprocess.Popen")
    def test_call_claude_includes_skip_permissions_streaming(self, mock_popen, tmp_path, monkeypatch):
        """Verify --dangerously-skip-permissions is in the cmd list for streaming mode"""
        cli = self._make_cli(tmp_path, monkeypatch)

        # Configure the mock process for streaming mode
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.returncode = 0
        # Streaming mode reads lines from stdout; return a result line then empty
        result_line = json.dumps({"type": "result", "result": "ok"}) + "\n"
        mock_process.stdout.readline = MagicMock(side_effect=[result_line, ""])
        mock_process.poll = MagicMock(return_value=None)
        mock_popen.return_value = mock_process

        cli.call_claude("test prompt", cli.project_folder, stream=True)

        # Extract the cmd list passed to Popen
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert "--dangerously-skip-permissions" in cmd

    @patch("autonomous_agent.subprocess.Popen")
    def test_call_claude_cmd_starts_with_claude(self, mock_popen, tmp_path, monkeypatch):
        """Verify the cmd list starts with 'claude'"""
        cli = self._make_cli(tmp_path, monkeypatch)

        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = ('{"result": "ok"}', '')
        mock_popen.return_value = mock_process

        cli.call_claude("test prompt", cli.project_folder, stream=False)

        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert cmd[0] == "claude"

    @patch("autonomous_agent.subprocess.Popen")
    def test_call_claude_cmd_includes_print_mode(self, mock_popen, tmp_path, monkeypatch):
        """Verify -p (print mode) is in the cmd list"""
        cli = self._make_cli(tmp_path, monkeypatch)

        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = ('{"result": "ok"}', '')
        mock_popen.return_value = mock_process

        cli.call_claude("test prompt", cli.project_folder, stream=False)

        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert "-p" in cmd

    @patch("autonomous_agent.subprocess.Popen")
    def test_call_claude_cmd_excludes_add_dir(self, mock_popen, tmp_path, monkeypatch):
        """Verify --add-dir is NOT in the cmd list (cwd is sufficient)"""
        cli = self._make_cli(tmp_path, monkeypatch)

        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = ('{"result": "ok"}', '')
        mock_popen.return_value = mock_process

        cli.call_claude("test prompt", cli.project_folder, stream=False)

        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert "--add-dir" not in cmd


# ===========================================================================
# extract_phase_stats
# ===========================================================================

class TestExtractPhaseStats:

    def test_extracts_all_fields_from_full_response(self):
        response = {
            "type": "result",
            "result": "Done",
            "total_cost_usd": 0.15,
            "duration_ms": 45000,
            "duration_api_ms": 38000,
            "num_turns": 8,
            "session_id": "sess-abc-123",
            "is_error": False,
            "usage": {
                "input_tokens": 15000,
                "output_tokens": 5000,
                "cache_read_input_tokens": 3000,
                "cache_creation_input_tokens": 2000,
            },
            "modelUsage": {
                "claude-sonnet-4-20250514": {
                    "costUSD": 0.10,
                    "inputTokens": 10000,
                    "outputTokens": 3000,
                },
                "claude-haiku-4-20250514": {
                    "costUSD": 0.05,
                    "inputTokens": 5000,
                    "outputTokens": 2000,
                },
            }
        }
        stats = extract_phase_stats(response)

        assert stats["cost_usd"] == 0.15
        assert stats["duration_ms"] == 45000
        assert stats["duration_api_ms"] == 38000
        assert stats["num_turns"] == 8
        assert stats["session_id"] == "sess-abc-123"
        assert stats["is_error"] is False
        assert stats["tokens"]["input"] == 15000
        assert stats["tokens"]["output"] == 5000
        assert stats["tokens"]["cache_read"] == 3000
        assert stats["tokens"]["cache_creation"] == 2000
        assert len(stats["model_usage"]) == 2
        assert stats["model_usage"]["claude-sonnet-4-20250514"]["cost_usd"] == 0.10

    def test_returns_defaults_for_empty_response(self):
        stats = extract_phase_stats({})
        assert stats["cost_usd"] == 0.0
        assert stats["duration_ms"] == 0
        assert stats["num_turns"] == 0
        assert stats["session_id"] == ""
        assert stats["tokens"]["input"] == 0
        assert stats["model_usage"] == {}

    def test_handles_none_response(self):
        stats = extract_phase_stats(None)
        assert stats["cost_usd"] == 0.0

    def test_handles_missing_usage_key(self):
        response = {"result": "ok", "total_cost_usd": 0.05}
        stats = extract_phase_stats(response)
        assert stats["cost_usd"] == 0.05
        assert stats["tokens"]["input"] == 0

    def test_handles_none_values_gracefully(self):
        response = {
            "total_cost_usd": None,
            "duration_ms": None,
            "num_turns": None,
            "session_id": None,
            "usage": None,
            "modelUsage": None,
        }
        stats = extract_phase_stats(response)
        assert stats["cost_usd"] == 0.0
        assert stats["duration_ms"] == 0
        assert stats["session_id"] == ""
        assert stats["tokens"]["input"] == 0


# ===========================================================================
# merge_phase_stats
# ===========================================================================

class TestMergePhaseStats:

    def test_merge_into_empty_accumulator(self):
        acc = {}
        phase = {
            "cost_usd": 0.10,
            "duration_ms": 30000,
            "duration_api_ms": 25000,
            "num_turns": 5,
            "tokens": {"input": 1000, "output": 500, "cache_read": 200, "cache_creation": 100},
            "model_usage": {
                "claude-sonnet-4-20250514": {"cost_usd": 0.10, "input_tokens": 1000, "output_tokens": 500}
            }
        }
        result = merge_phase_stats(acc, phase)
        assert result["total_cost_usd"] == 0.10
        assert result["total_duration_ms"] == 30000
        assert result["total_turns"] == 5
        assert result["tokens"]["input"] == 1000
        assert "claude-sonnet-4-20250514" in result["model_usage"]

    def test_merge_accumulates_multiple_phases(self):
        acc = {}
        phase1 = {
            "cost_usd": 0.10, "duration_ms": 30000, "duration_api_ms": 25000,
            "num_turns": 5,
            "tokens": {"input": 1000, "output": 500, "cache_read": 200, "cache_creation": 100},
            "model_usage": {"model-a": {"cost_usd": 0.10, "input_tokens": 1000, "output_tokens": 500}}
        }
        phase2 = {
            "cost_usd": 0.05, "duration_ms": 10000, "duration_api_ms": 8000,
            "num_turns": 3,
            "tokens": {"input": 500, "output": 200, "cache_read": 100, "cache_creation": 50},
            "model_usage": {"model-a": {"cost_usd": 0.05, "input_tokens": 500, "output_tokens": 200}}
        }
        merge_phase_stats(acc, phase1)
        merge_phase_stats(acc, phase2)

        assert abs(acc["total_cost_usd"] - 0.15) < 1e-9
        assert acc["total_duration_ms"] == 40000
        assert acc["total_turns"] == 8
        assert acc["tokens"]["input"] == 1500
        assert acc["tokens"]["output"] == 700
        assert abs(acc["model_usage"]["model-a"]["cost_usd"] - 0.15) < 1e-9
        assert acc["model_usage"]["model-a"]["input_tokens"] == 1500

    def test_merge_handles_new_model_in_second_phase(self):
        acc = {}
        phase1 = {
            "cost_usd": 0.10, "duration_ms": 1000, "duration_api_ms": 900,
            "num_turns": 1,
            "tokens": {"input": 100, "output": 50, "cache_read": 0, "cache_creation": 0},
            "model_usage": {"model-a": {"cost_usd": 0.10, "input_tokens": 100, "output_tokens": 50}}
        }
        phase2 = {
            "cost_usd": 0.05, "duration_ms": 500, "duration_api_ms": 400,
            "num_turns": 1,
            "tokens": {"input": 50, "output": 25, "cache_read": 0, "cache_creation": 0},
            "model_usage": {"model-b": {"cost_usd": 0.05, "input_tokens": 50, "output_tokens": 25}}
        }
        merge_phase_stats(acc, phase1)
        merge_phase_stats(acc, phase2)

        assert "model-a" in acc["model_usage"]
        assert "model-b" in acc["model_usage"]
        assert acc["model_usage"]["model-b"]["cost_usd"] == 0.05

    def test_merge_with_empty_phase(self):
        acc = {"total_cost_usd": 0.10, "total_duration_ms": 1000, "total_api_duration_ms": 800,
               "total_turns": 3, "tokens": {"input": 100, "output": 50, "cache_read": 0, "cache_creation": 0},
               "model_usage": {}}
        empty_phase = {"cost_usd": 0, "duration_ms": 0, "duration_api_ms": 0,
                       "num_turns": 0, "tokens": {}, "model_usage": {}}
        merge_phase_stats(acc, empty_phase)
        assert acc["total_cost_usd"] == 0.10
        assert acc["total_turns"] == 3


# ===========================================================================
# worker_loop returns phase stats with telemetry data
# ===========================================================================

class TestWorkerLoopPhaseStats:

    def _make_cli(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli = AgentCLI()
        cli.setup_project("Build hello world", "Script prints hello")
        return cli

    @patch.object(AgentCLI, "call_claude")
    def test_worker_loop_returns_stats_from_response(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {
            "result": "Done.\n\nSTATUS: done",
            "total_cost_usd": 0.12,
            "duration_ms": 5000,
            "num_turns": 3,
            "session_id": "test-session-id",
        }
        status, output, phase_stats = cli.worker_loop(1)
        assert status == "done"
        assert phase_stats["cost_usd"] == 0.12
        assert phase_stats["duration_ms"] == 5000
        assert phase_stats["num_turns"] == 3
        assert phase_stats["session_id"] == "test-session-id"


# ===========================================================================
# verify_work returns phase stats
# ===========================================================================

class TestVerifyWorkPhaseStats:

    def _make_cli(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli = AgentCLI()
        cli.setup_project("T", "C")
        return cli

    @patch.object(AgentCLI, "call_claude")
    def test_verify_work_returns_stats(self, mock_call, tmp_path, monkeypatch):
        cli = self._make_cli(tmp_path, monkeypatch)
        mock_call.return_value = {
            "result": "Verified",
            "total_cost_usd": 0.03,
            "duration_ms": 2000,
            "num_turns": 1,
        }
        verdict = {"approved": True, "feedback": ""}
        (cli.project_folder / "VERDICT.json").write_text(
            json.dumps(verdict), encoding="utf-8"
        )
        approved, feedback, phase_stats = cli.verify_work("output")
        assert approved is True
        assert phase_stats["cost_usd"] == 0.03
        assert phase_stats["duration_ms"] == 2000
