#!/usr/bin/env python3
"""
Autonomous Agent CLI - Worker/Verifier Loop System
Uses Claude Code programmatically for agent execution
"""
import subprocess
import json
import os
import sys
import threading
import time
import traceback
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

from timeout_utils import PipeReader

try:
    from onedrive_utils import local_path_to_web_url, _path_looks_like_file
except ImportError:
    local_path_to_web_url = None
    _path_looks_like_file = None

# Fix Windows console encoding for Unicode characters
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# --- File logging ---
_LOG_FILE = Path(__file__).parent / "agent.log"

_file_logger = logging.getLogger("shraga_agent")
_file_logger.setLevel(logging.DEBUG)
_file_handler = RotatingFileHandler(
    str(_LOG_FILE),
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_file_logger.addHandler(_file_handler)


def _log(msg: str):
    """Print with timestamp to console AND write to log file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")
    try:
        _file_logger.info(msg)
    except Exception:
        pass  # Never let logging crash the service


def _log_to_file(msg: str):
    """Write to log file only (no console output), for augmenting existing prints."""
    try:
        _file_logger.info(msg)
    except Exception:
        pass


def extract_phase_stats(response: dict) -> dict:
    """Extract telemetry/stats from a Claude Code CLI JSON response.

    Works with both ``--output-format json`` (flat) and ``--output-format
    stream-json`` (the final ``type: result`` chunk).  Fields that are absent
    in the response are returned as ``0`` / ``{}`` so callers never need to
    guard against ``None``.

    Returns a dict with keys:
        cost_usd, duration_ms, duration_api_ms, num_turns, session_id,
        tokens (dict), model_usage (dict), is_error (bool)
    """
    stats: dict = {
        "cost_usd": 0.0,
        "duration_ms": 0,
        "duration_api_ms": 0,
        "num_turns": 0,
        "session_id": "",
        "tokens": {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
        },
        "model_usage": {},
        "is_error": False,
    }

    if not isinstance(response, dict):
        return stats

    # Top-level scalar fields (present in both json and stream-json result)
    stats["cost_usd"] = float(response.get("total_cost_usd", 0) or 0)
    stats["duration_ms"] = int(response.get("duration_ms", 0) or 0)
    stats["duration_api_ms"] = int(response.get("duration_api_ms", 0) or 0)
    stats["num_turns"] = int(response.get("num_turns", 0) or 0)
    stats["session_id"] = response.get("session_id", "") or ""
    stats["is_error"] = bool(response.get("is_error", False))

    # Token usage
    usage = response.get("usage", {}) or {}
    stats["tokens"]["input"] = int(usage.get("input_tokens", 0) or 0)
    stats["tokens"]["output"] = int(usage.get("output_tokens", 0) or 0)
    stats["tokens"]["cache_read"] = int(usage.get("cache_read_input_tokens", 0) or 0)
    stats["tokens"]["cache_creation"] = int(usage.get("cache_creation_input_tokens", 0) or 0)

    # Per-model usage breakdown (reveals sub-agents)
    model_usage_raw = response.get("modelUsage", {}) or {}
    model_usage: dict = {}
    for model_id, mu in model_usage_raw.items():
        if isinstance(mu, dict):
            model_usage[model_id] = {
                "cost_usd": float(mu.get("costUSD", 0) or 0),
                "input_tokens": int(mu.get("inputTokens", 0) or 0),
                "output_tokens": int(mu.get("outputTokens", 0) or 0),
            }
    stats["model_usage"] = model_usage

    return stats


def merge_phase_stats(accumulated: dict, phase_stats: dict) -> dict:
    """Merge *phase_stats* into *accumulated* totals (mutates & returns *accumulated*)."""
    accumulated["total_cost_usd"] = accumulated.get("total_cost_usd", 0.0) + phase_stats.get("cost_usd", 0.0)
    accumulated["total_duration_ms"] = accumulated.get("total_duration_ms", 0) + phase_stats.get("duration_ms", 0)
    accumulated["total_api_duration_ms"] = accumulated.get("total_api_duration_ms", 0) + phase_stats.get("duration_api_ms", 0)
    accumulated["total_turns"] = accumulated.get("total_turns", 0) + phase_stats.get("num_turns", 0)

    # Tokens
    acc_tokens = accumulated.setdefault("tokens", {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0})
    phase_tokens = phase_stats.get("tokens", {})
    for key in ("input", "output", "cache_read", "cache_creation"):
        acc_tokens[key] = acc_tokens.get(key, 0) + phase_tokens.get(key, 0)

    # Model usage – merge per model
    acc_models = accumulated.setdefault("model_usage", {})
    for model_id, mu in phase_stats.get("model_usage", {}).items():
        if model_id not in acc_models:
            acc_models[model_id] = {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}
        acc_models[model_id]["cost_usd"] += mu.get("cost_usd", 0.0)
        acc_models[model_id]["input_tokens"] += mu.get("input_tokens", 0)
        acc_models[model_id]["output_tokens"] += mu.get("output_tokens", 0)

    return accumulated


class AgentCLI:
    def __init__(self):
        self.project_folder = None
        self.task_file = None
        self.verification_file = None

    def collect_inputs(self):
        """Phase 1: Collect the 2 required inputs from user"""
        print("\n" + "="*60)
        print("AUTONOMOUS AGENT CLI - Setup")
        print("="*60 + "\n")

        # 1. Task description/requirements
        print("1. TASK DESCRIPTION/REQUIREMENTS")
        print("-" * 40)
        print("Describe the task you want the agent to complete:")
        print("(Enter multi-line input, press Ctrl+Z then Enter when done on Windows,")
        print(" or Ctrl+D on Unix)\n")

        task_lines = []
        try:
            while True:
                line = input()
                task_lines.append(line)
        except EOFError:
            pass
        task_description = "\n".join(task_lines)

        # 2. Success criteria
        print("\n2. SUCCESS CRITERIA")
        print("-" * 40)
        print("How does 'done' look like? What defines success?")
        print("(Enter multi-line input, press Ctrl+Z then Enter when done on Windows,")
        print(" or Ctrl+D on Unix)\n")

        success_lines = []
        try:
            while True:
                line = input()
                success_lines.append(line)
        except EOFError:
            pass
        success_criteria = "\n".join(success_lines)

        return task_description, success_criteria

    def setup_project(self, task_description, success_criteria, project_folder_path=None):
        """Phase 2: Create subfolder and project files"""
        if project_folder_path is not None:
            self.project_folder = Path(project_folder_path)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.project_folder = Path(f"agent_task_{timestamp}")
        self.project_folder.mkdir(exist_ok=True, parents=True)

        # File 1: Task file (READ ONLY - source of truth)
        self.task_file = self.project_folder / "TASK.md"
        task_content = f"""# TASK - READ ONLY - SOURCE OF TRUTH

## Task Description
{task_description}

## Important Notes
- This file is READ ONLY
- This is the ONLY source of truth for the task
- All work should be done in this folder
- You can finish with one status:
  - "done" - Task completed, ready for verification
"""
        self.task_file.write_text(task_content, encoding='utf-8')

        # File 2: Verification criteria
        self.verification_file = self.project_folder / "VERIFICATION.md"
        verification_content = f"""# Verification Criteria

## Success Definition
{success_criteria}

## Verification Instructions
Review the work done in this folder and determine if it meets the success criteria above.
Return one of:
- "approved" - Work meets all success criteria
- "not approved: <reason>" - Work does not meet criteria, explain why
"""
        self.verification_file.write_text(verification_content, encoding='utf-8')

        print(f"\n[+] Project created: {self.project_folder}")
        print(f"  - {self.task_file.name}")
        print(f"  - {self.verification_file.name}\n")

        return self.project_folder

    def call_claude(self, prompt, work_dir, max_tokens=4096, timeout=3600, stream=True, on_event=None):
        """Call Claude Code programmatically with optional streaming

        Args:
            on_event: Optional callback function(event_type, data) called for streaming events
                     event_type can be: 'tool_use', 'progress', 'complete'
        """
        cmd = [
            "claude",
            "-p",  # Print mode
            "--output-format", "stream-json" if stream else "json",
            "--dangerously-skip-permissions",
        ]

        if stream:
            cmd.append("--verbose")  # Required for stream-json in print mode
            cmd.append("--include-partial-messages")

        print(f"[*] Timeout: {timeout}s | Progress: {'Streaming' if stream else 'Spinner'}\n")

        if not stream:
            # Non-streaming mode with progress spinner

            # Strip CLAUDECODE env var to avoid "nested session" error
            env = {k: v for k, v in os.environ.items() if k != 'CLAUDECODE'}

            # Start subprocess
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=str(work_dir),
                env=env
            )

            # Send prompt
            process.stdin.write(prompt)
            process.stdin.close()

            # Progress spinner
            spinner = ['|', '/', '-', '\\']
            spinner_idx = [0]
            stop_spinner = [False]
            start_time = time.time()

            def show_spinner():
                while not stop_spinner[0]:
                    elapsed = int(time.time() - start_time)
                    mins, secs = divmod(elapsed, 60)
                    print(f"\r[{spinner[spinner_idx[0] % 4]}] Working... {mins}:{secs:02d}  ", end='', flush=True)
                    spinner_idx[0] += 1
                    time.sleep(0.2)

            # Start spinner thread
            spinner_thread = threading.Thread(target=show_spinner, daemon=True)
            spinner_thread.start()

            try:
                # Wait for completion
                stdout, stderr = process.communicate(timeout=timeout)
                stop_spinner[0] = True
                time.sleep(0.3)  # Let spinner thread finish
                print(f"\r[+] Completed in {int(time.time() - start_time)}s          \n")

                if process.returncode != 0:
                    raise Exception(f"Claude Code failed: {stderr}")

                return json.loads(stdout)

            except subprocess.TimeoutExpired:
                stop_spinner[0] = True
                process.kill()
                _log_to_file(f"[ERROR] Timeout after {timeout}s")
                raise Exception(f"Timeout after {timeout}s")
            except KeyboardInterrupt:
                stop_spinner[0] = True
                process.kill()
                raise

        # Streaming mode - show real-time progress

        # Strip CLAUDECODE env var to avoid "nested session" error
        env = {k: v for k, v in os.environ.items() if k != 'CLAUDECODE'}

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            cwd=str(work_dir),
            bufsize=1,  # Line buffered
            env=env
        )

        # Send prompt
        process.stdin.write(prompt)
        process.stdin.close()

        # Wrap pipes in non-blocking PipeReaders
        stdout_reader = PipeReader(process.stdout)
        stderr_reader = PipeReader(process.stderr)

        # Collect output
        stdout_data = []
        full_result = None
        start_time = time.time()
        last_activity = start_time
        last_update = start_time

        print("--- AGENT WORKING (live progress) ---\n")
        _log_to_file("AGENT WORKING (live progress)")

        try:
            while True:
                # Check activity-based timeout
                if time.time() - last_activity > timeout:
                    elapsed = int(time.time() - start_time)
                    _log_to_file(f"[TIMEOUT] No activity for {timeout}s (total elapsed: {elapsed}s)")
                    process.kill()
                    raise Exception(f"No activity for {timeout}s (total elapsed: {elapsed}s)")

                # Read stdout
                line = stdout_reader.readline(timeout=60)
                if line:
                    last_activity = time.time()  # reset activity timer
                    line = line.strip()
                    if line:
                        stdout_data.append(line)

                        try:
                            chunk = json.loads(line)
                            chunk_type = chunk.get('type')

                            # Handle different chunk types from stream-json
                            if chunk_type == 'assistant':
                                # Check for tool usage in message content
                                message = chunk.get('message', {})
                                content = message.get('content', [])
                                for item in content:
                                    if item.get('type') == 'tool_use':
                                        tool_name = item.get('name', 'unknown')

                                        print(f"\n[*] Using tool: {tool_name}", flush=True)
                                        _log_to_file(f"Using tool: {tool_name}")

                                        tool_input = item.get('input', {})
                                        if tool_input:
                                            _log_to_file(f"  tool_input: {json.dumps(tool_input, default=str)[:2000]}")

                                        # Callback for tool usage event - DISABLED (user wants only thoughts)
                                        # if on_event:
                                        #     on_event('tool_use', {
                                        #         'tool': tool_name,
                                        #         'details': details.strip(' →').strip()
                                        #     })

                                    elif item.get('type') == 'text':
                                        # Extract and send the actual text content
                                        text_content = item.get('text', '')
                                        if text_content and text_content.strip():
                                            # Show progress dots in console
                                            print(".", end='', flush=True)

                                            # Send actual text via callback
                                            if on_event:
                                                on_event('text', {'content': text_content})

                                        # Update last_update time
                                        last_update = time.time()

                            elif chunk_type == 'result':
                                # Final result found!
                                full_result = chunk
                                break

                            elif chunk_type == 'system':
                                # Initialization, ignore
                                pass

                        except json.JSONDecodeError:
                            pass

                # Check if process finished
                if process.poll() is not None:
                    # Read remaining output
                    remaining = stdout_reader.read_all(timeout=10)
                    if remaining:
                        for line in remaining.strip().split('\n'):
                            if line:
                                stdout_data.append(line)
                                try:
                                    chunk = json.loads(line)
                                    if chunk.get('type') == 'result':
                                        full_result = chunk
                                except json.JSONDecodeError:
                                    pass
                    break

            print("\n\n--- AGENT COMPLETED ---\n")
            _log_to_file("AGENT COMPLETED")

            # Ensure subprocess is fully terminated
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _log_to_file("[WARN] CLI process did not exit within 30s, killing")
                process.kill()
                process.wait(timeout=5)

            if full_result is None:
                # Log diagnostic info before raising
                stderr = stderr_reader.read_all(timeout=10)
                print(f"[!] Stream parsing failed. Return code: {process.returncode}")
                print(f"[!] Stderr: {stderr[:1000]}" if stderr else "[!] No stderr output")
                print(f"[!] Stdout lines collected: {len(stdout_data)}")
                if stdout_data:
                    print(f"[!] Last stdout line: {stdout_data[-1][:500]}")
                raise Exception(f"Could not parse final result from stream. returncode={process.returncode}, lines={len(stdout_data)}, stderr={stderr[:200]}")

            if process.returncode is not None and process.returncode != 0:
                stderr = stderr_reader.read_all(timeout=10)
                if stderr:
                    print(f"[!] Stderr: {stderr}")

            return full_result

        except KeyboardInterrupt:
            process.kill()
            raise
        except Exception as e:
            process.kill()
            raise

    def worker_loop(self, iteration=1, verifier_feedback=None, on_event=None):
        """Phase 3: Worker agent iteration

        Args:
            on_event: Optional callback function(event_type, data) for streaming events
        """
        print(f"\n{'='*60}")
        print(f"WORKER ITERATION #{iteration}")
        print(f"{'='*60}\n")
        _log_to_file(f"WORKER ITERATION #{iteration}")

        # Build worker prompt
        worker_prompt = f"""You are a worker agent executing a task.

TASK FILE: {self.task_file.name} (READ ONLY - this is your source of truth)
WORK DIRECTORY: {self.project_folder}

Read the TASK.md file to understand what you need to do.
All work done so far is in the current folder - review it.

"""

        if verifier_feedback:
            worker_prompt += f"""VERIFIER FEEDBACK FROM PREVIOUS ITERATION:
{verifier_feedback}

The verifier found issues with your previous work. Address the feedback and try again.

"""

        worker_prompt += """When you're done, respond with EXACTLY this status:

STATUS: done
(if task is complete and ready for verification)

Place your status at the END of your response.
"""

        # Call worker agent
        print("[*] Calling worker agent...")
        _log_to_file("Calling worker agent...")
        response = self.call_claude(worker_prompt, self.project_folder, on_event=on_event)

        # Extract phase stats from Claude response
        phase_stats = extract_phase_stats(response)

        result_text = response.get('result', '')
        print(f"\nWorker response:\n{'-'*40}\n{result_text}\n{'-'*40}\n")

        # Parse status
        if "STATUS: done" in result_text:
            return "done", result_text, phase_stats
        else:
            # Default to done if unclear
            return "done", result_text, phase_stats

    def verify_work(self, worker_output, on_event=None):
        """Phase 5: Verifier checks if work is done

        Args:
            on_event: Optional callback function(event_type, data) for streaming events
        """
        print(f"\n{'='*60}")
        print("VERIFICATION PHASE")
        print(f"{'='*60}\n")
        _log_to_file("VERIFICATION PHASE")

        verifier_prompt = f"""You are a verifier agent. Check if the work meets the success criteria.

VERIFICATION FILE: {self.verification_file.name}
WORK DIRECTORY: {self.project_folder}

Read VERIFICATION.md for success criteria. Review all work done.

LATEST WORKER OUTPUT:
{worker_output}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CORE PRINCIPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. ACTUALLY TEST IT
Do the most active testing possible. Never approve based on code review alone.

Examples of active testing:
• Web apps: Start server, test in browser (you have Playwright MCP for browser automation)
• Test files: Run them (npm test, pytest, etc.)
• Scripts: Execute them, verify output
• APIs: Call endpoints, check responses
• CLIs: Run commands, test flags

You can write tests yourself if needed, then run them to verify.

2. BE STRICT
• 99% success = 100% failure
• ANY error = not approved
• Partial success = not approved
• Incomplete solution = fail without a doubt

3. COMPARE TO EXPERT BASELINE
Deeply focus on the task requirements. Ask yourself:
"If 100 experts were given this exact task independently, what would the average solution look like?"

If this solution is NOT similar to what most experts would create, fail it.
This catches solutions that are technically functional but poorly designed, overcomplicated, or missing obvious features.

Report exactly what you tested and what happened.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UX CHECK (MANDATORY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Check for UX anti-patterns:
• Manual file selection every time (no localStorage/config)
• No error messages when things fail
• Hardcoded values that should be configurable
• Repeated manual steps that could be automated

Reject if these exist and aren't necessary for security/requirements.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPONSE FORMAT (MANDATORY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Write your detailed analysis and findings as usual
2. At the END, you MUST write a file called VERDICT.json with this exact structure:

{{
  "approved": true or false,
  "feedback": "Brief feedback for worker if not approved (empty string if approved)",
  "testing_done": "Summary of what you tested",
  "results": "Summary of test results",
  "criteria_met": ["list", "of", "criteria", "that", "passed"],
  "criteria_failed": ["list", "of", "criteria", "that", "failed"],
  "expert_comparison": "How does this compare to expert baseline"
}}

CRITICAL RULES:
1. File must be named EXACTLY "VERDICT.json" (case-sensitive)
2. Must be valid JSON (use proper escaping for quotes, newlines, etc.)
3. "approved" must be boolean true or false (not string)
4. "feedback" should be empty string "" if approved
5. If not approved, "feedback" should be specific and actionable

EXAMPLE VERDICT.json for approval:
{{
  "approved": true,
  "feedback": "",
  "testing_done": "Ran all tests, checked functionality, verified output",
  "results": "All tests passed, functionality works as expected",
  "criteria_met": ["tests pass", "code quality good", "documentation complete"],
  "criteria_failed": [],
  "expert_comparison": "Meets expert baseline - clean implementation"
}}

EXAMPLE VERDICT.json for rejection:
{{
  "approved": false,
  "feedback": "Tests are failing with syntax errors. Fix the import statements and re-run tests.",
  "testing_done": "Ran npm test",
  "results": "3 tests failed with import errors",
  "criteria_met": ["documentation complete"],
  "criteria_failed": ["tests failing", "syntax errors present"],
  "expert_comparison": "Below expert baseline - has preventable errors"
}}

After writing VERDICT.json, summarize your findings in plain text for the user to read.
"""

        print("[*] Calling verifier agent...")
        _log_to_file("Calling verifier agent...")
        response = self.call_claude(verifier_prompt, self.project_folder, on_event=on_event)

        # Extract phase stats from Claude response
        phase_stats = extract_phase_stats(response)

        result_text = response.get('result', '')
        print(f"\nVerifier response:\n{'-'*40}\n{result_text}\n{'-'*40}\n")

        # Parse verdict from VERDICT.json file
        verdict_file = self.project_folder / "VERDICT.json"

        if not verdict_file.exists():
            print("[ERROR] VERDICT.json file not found - verifier did not create it")
            _log_to_file("[ERROR] VERDICT.json file not found - verifier did not create it")
            return False, "Verifier did not create VERDICT.json file", phase_stats

        try:
            with open(verdict_file, 'r', encoding='utf-8') as f:
                verdict_data = json.load(f)

            print(f"[DEBUG] Parsed verdict JSON: {json.dumps(verdict_data, indent=2)}")

            # Validate required fields
            if 'approved' not in verdict_data:
                print("[ERROR] VERDICT.json missing 'approved' field")
                return False, "Invalid VERDICT.json - missing 'approved' field", phase_stats

            approved = verdict_data['approved']
            feedback = verdict_data.get('feedback', '')

            if not isinstance(approved, bool):
                print(f"[ERROR] 'approved' field must be boolean, got: {type(approved)}")
                return False, "Invalid VERDICT.json - 'approved' must be boolean", phase_stats

            if approved:
                print("[SUCCESS] Work approved by verifier")
                _log_to_file("[SUCCESS] Work approved by verifier")
                return True, None, phase_stats
            else:
                print(f"[RETRY] Verification failed: {feedback}")
                _log_to_file(f"[RETRY] Verification failed: {feedback}")
                return False, feedback if feedback else "No feedback provided", phase_stats

        except json.JSONDecodeError as e:
            print(f"[ERROR] Failed to parse VERDICT.json: {e}")
            _log_to_file(f"[ERROR] Failed to parse VERDICT.json: {e}")
            return False, f"Invalid JSON in VERDICT.json: {e}", phase_stats
        except Exception as e:
            print(f"[ERROR] Error reading VERDICT.json: {e}")
            _log_to_file(f"[ERROR] Error reading VERDICT.json: {e}")
            return False, f"Error reading verdict file: {e}", phase_stats

    def create_summary(self, on_event=None):
        """Phase 6: Create a 1-page summary of results

        Args:
            on_event: Optional callback function(event_type, data) for streaming events

        Returns:
            str: The summary text content
        """
        print(f"\n{'='*60}")
        print("SUMMARY CREATION")
        print(f"{'='*60}\n")
        _log_to_file("SUMMARY CREATION")

        # Build OneDrive URL mapping for files in the project folder.
        # Use suffix-based inference (_path_looks_like_file) instead of
        # Path.is_file() so that files still pending OneDrive sync are
        # included (fixes GAP-I11 sync race).
        file_links = {}
        if local_path_to_web_url and self.project_folder:
            _is_file = _path_looks_like_file or (lambda p: Path(p).suffix != "")
            for f in self.project_folder.iterdir():
                if _is_file(f):
                    url = local_path_to_web_url(str(f))
                    if url:
                        file_links[f.name] = url

        # Format file links for the prompt
        if file_links:
            file_links_text = "FILE LINKS (use these for any file references):\n"
            for name, url in file_links.items():
                file_links_text += f"- [{name}]({url})\n"
        else:
            file_links_text = "(No OneDrive links available — use plain file names)"

        summarizer_prompt = f"""You are a results summarizer. Create a very brief summary of the work completed.

WORK DIRECTORY: {self.project_folder}

{file_links_text}

Read all deliverable files (TASK.md, DELIVERABLES.md, VERDICT.json, and any output files created).

Create a file called SUMMARY.md with a summary that follows these rules:

FORMATTING RULES (MANDATORY):
- Output ONLY 2-3 bullet points, ~100 words total. No more.
- Each bullet should be one concise sentence.
- When referencing files, use clickable markdown links: [filename](url)
- Use the FILE LINKS provided above for any file references.
- Do NOT include section headers, horizontal rules, or any other formatting beyond the bullets.

STRUCTURE:

```markdown
# Task Summary: [Task Name]

- [What was done and key result, with specific data/numbers if applicable]
- [Verification outcome: what was tested, pass/fail counts]
- [Notable artifacts or caveats, if any] — [view file](onedrive-url)
```

Write SUMMARY.md with your summary, then output a brief confirmation message.
"""

        print("[*] Calling summarizer agent...")
        _log_to_file("Calling summarizer agent...")
        response = self.call_claude(summarizer_prompt, self.project_folder, on_event=on_event)

        # Extract phase stats from Claude response
        phase_stats = extract_phase_stats(response)

        result_text = response.get('result', '')
        print(f"\nSummarizer response:\n{'-'*40}\n{result_text}\n{'-'*40}\n")

        # Read the generated summary
        summary_file = self.project_folder / "SUMMARY.md"

        if not summary_file.exists():
            print("[WARNING] SUMMARY.md file not found - returning raw response")
            return result_text, phase_stats

        try:
            with open(summary_file, 'r', encoding='utf-8') as f:
                summary_content = f.read()

            print(f"[SUCCESS] Summary created ({len(summary_content)} chars)")
            return summary_content, phase_stats

        except Exception as e:
            print(f"[ERROR] Error reading SUMMARY.md: {e}")
            return result_text, phase_stats

    def run(self):
        """Main execution loop"""
        try:
            # Phase 1: Collect inputs
            task_desc, success_criteria = self.collect_inputs()

            # Phase 2: Setup project
            self.setup_project(task_desc, success_criteria)

            # Phase 3-5: Worker/Verifier loop
            max_iterations = 10
            iteration = 1
            verifier_feedback = None

            while iteration <= max_iterations:
                # Worker phase
                status, output, _worker_stats = self.worker_loop(iteration, verifier_feedback)

                if status == "done":
                    # Verification phase
                    approved, feedback, _verifier_stats = self.verify_work(output)

                    if approved:
                        print(f"\n{'='*60}")
                        print("[+] TASK COMPLETED SUCCESSFULLY!")
                        print(f"{'='*60}\n")
                        print(f"Project folder: {self.project_folder}")
                        _log_to_file(f"TASK COMPLETED SUCCESSFULLY! Project: {self.project_folder}")
                        break
                    else:
                        print(f"\n[!]  Verification failed. Going back to worker with feedback.\n")
                        verifier_feedback = feedback
                        iteration += 1
                        continue
            else:
                # Max iterations reached without approval
                print(f"\n{'='*60}")
                print(f"[!] Max iterations ({max_iterations}) reached without approval")
                print(f"{'='*60}\n")
                print(f"Project folder: {self.project_folder}")
                _log_to_file(f"Max iterations ({max_iterations}) reached without approval. Project: {self.project_folder}")

        except KeyboardInterrupt:
            print("\n\n[!]  Interrupted by user")
            _log_to_file("Interrupted by user")
            sys.exit(1)
        except Exception as e:
            print(f"\n\n[X] Error: {e}")
            _log_to_file(f"[ERROR] {e}")
            traceback.print_exc()
            sys.exit(1)


if __name__ == "__main__":
    cli = AgentCLI()
    cli.run()
