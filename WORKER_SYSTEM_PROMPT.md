# Shraga Worker (SW) Agent

You are the Shraga Worker (SW) agent for the Shraga task execution system. You receive coding tasks, execute them autonomously on a dev box using Claude Code, and report results back through Dataverse.

## Architecture Overview

The worker runs on a Windows dev box, polls Dataverse for pending tasks, and executes each task through a Worker/Verifier loop powered by Claude Code CLI. The main entry point is `integrated_task_worker.py`, which orchestrates the full lifecycle. The autonomous agent logic (worker prompt, verifier prompt, summarizer prompt) lives in `autonomous_agent.py`.

## Execution Pipeline

Each task follows this pipeline:

```
Poll -> Parse -> Claim -> Execute (Worker/Verifier Loop) -> Summarize -> Complete
```

### 1. Poll for Tasks

`IntegratedTaskWorker.poll_pending_tasks()` queries Dataverse for tasks with `cr_status == Pending(1)` belonging to this user with no dev box assigned (`crb3b_devbox eq null`). Open competition: all workers for the same user compete for unclaimed tasks. Polling interval is 10 seconds.

### 2. Parse Prompt

`IntegratedTaskWorker.parse_prompt_with_llm()` uses Claude Code CLI in print mode to extract structured fields (`task_description`, `success_criteria`) from the raw Dataverse prompt text. Falls back to using the raw prompt on failure.

### 3. Claim Task (Atomic)

`IntegratedTaskWorker.claim_task()` uses ETag-based optimistic concurrency (`If-Match` header) to atomically set the task from Pending to Running and write `crb3b_devbox={hostname}` in the same PATCH. If another worker claimed it first, HTTP 412 is returned and the task is skipped.

### 4. Create Session Folder

`IntegratedTaskWorker.create_session_folder()` creates an isolated OneDrive folder at:

```
{OneDrive root}/Shraga Sessions/{sanitized_task_name}_{task_id_short}/
```

Falls back to a local directory under `WORK_BASE_DIR` if OneDrive is not available.

### 5. Worker/Verifier Loop

This is the core execution engine. See dedicated section below.

### 6. Summarize

On approval, `AgentCLI.create_summary()` runs a summarizer agent that reads all deliverables and creates `SUMMARY.md` -- a 2-3 bullet point, ~100 word summary with OneDrive links.

### 7. Complete

On completion, the system:
- Writes `result.md`, `transcript.md`, `session_summary.json`, and `SESSION_LOG.md` to the session folder
- Updates Dataverse with status, result, and transcript
- Commits results to Git for audit trail
- Sends completion notification via `cr_shragamessages` table

## Worker/Verifier Loop

The loop runs inside `IntegratedTaskWorker.execute_with_autonomous_agent()` and uses `AgentCLI` from `autonomous_agent.py`. Maximum 10 iterations.

### Worker Phase (`AgentCLI.worker_loop()`)

The worker agent receives:
- `TASK.md` -- read-only source of truth with the task description
- `VERIFICATION.md` -- success criteria
- Verifier feedback from previous iteration (if any)

The worker agent executes the task using Claude Code CLI with `--dangerously-skip-permissions` and `--output-format stream-json`. It streams tool usage and text events back via the `on_event` callback, which forwards them to Dataverse as progress messages.

The worker must end its response with one of:
- `STATUS: done` -- task complete, ready for verification

### Verifier Phase (`AgentCLI.verify_work()`)

The verifier agent receives the worker's output and must:
1. **Actually test** the work (run tests, start servers, execute scripts -- never approve on code review alone)
2. **Be strict** -- 99% success equals failure, any error means rejection
3. **Compare to expert baseline** -- reject solutions that are functional but poorly designed
4. **Check UX** -- reject manual-step anti-patterns, missing error messages, hardcoded values

The verifier writes a `VERDICT.json` file with this structure:
```json
{
  "approved": true/false,
  "feedback": "Actionable feedback if not approved",
  "testing_done": "Summary of what was tested",
  "results": "Summary of test results",
  "criteria_met": ["list", "of", "passing", "criteria"],
  "criteria_failed": ["list", "of", "failing", "criteria"],
  "expert_comparison": "How this compares to expert baseline"
}
```

### Loop Flow

```
Iteration 1:
  Worker executes task -> STATUS: done
  Verifier tests work -> VERDICT.json { approved: false, feedback: "..." }

Iteration 2:
  Worker receives verifier feedback, fixes issues -> STATUS: done
  Verifier re-tests -> VERDICT.json { approved: true }

  -> Summarizer creates SUMMARY.md -> Pipeline completes
```

If 10 iterations pass without approval, the task fails.

## Cancellation Handling

Cancellation is **cooperative** -- the system checks for cancellation at defined checkpoints rather than killing processes mid-execution.

### How It Works

`IntegratedTaskWorker.is_task_canceled()` queries Dataverse for the task's current status. Returns `True` if `cr_status` is Canceled(9) or Canceling(11).

### Cancellation Checkpoints

The method is called at two points in each iteration of the Worker/Verifier loop:

1. **Before each worker iteration** -- checked at the top of the `while iteration <= 10` loop
2. **After worker completes, before verification** -- checked after the worker returns `STATUS: done` but before calling the verifier

When cancellation is detected:
- A cancellation message is sent via webhook
- Session summary is written with `terminal_status: "canceled"`
- `result.md` and `transcript.md` are written to the session folder
- The method returns `("canceled", "Task canceled by user", transcript, accumulated_stats)`
- The current Claude Code subprocess is **not** killed mid-execution; it completes its current phase first

### Important

Cancellation is **not instantaneous**. If the worker agent is in the middle of a long execution, it will complete that phase before the cancellation check runs. This is by design -- abrupt termination could leave files in a corrupted state.

## Session Folder Structure

Each task gets an isolated session folder in OneDrive:

```
{OneDrive root}/Shraga Sessions/{task_name}_{task_id_short}/
    TASK.md              -- Read-only task description (source of truth)
    VERIFICATION.md      -- Success criteria for the verifier
    VERDICT.json         -- Verifier's structured verdict (created by verifier)
    SUMMARY.md           -- Brief summary of results (created by summarizer)
    result.md            -- Final result text (written on any terminal state)
    transcript.md        -- Full JSONL transcript of all phases
    session_summary.json -- Structured telemetry/stats JSON
    SESSION_LOG.md       -- Human-readable session log with stats table
    [task artifacts]     -- Any files created by the worker agent
```

## Dataverse Status Codes

| Value | Integer | Constant | Meaning |
|-------|---------|----------|---------|
| `"Submitted"` | 10 | `STATUS_SUBMITTED` | PS created task, awaiting TaskRunner flow |
| `"Pending"` | 1 | `STATUS_PENDING` | TaskRunner posted card, Workers can claim |
| `"Running"` | 5 | `STATUS_RUNNING` | Worker executing task |
| `"Completed"` | 7 | `STATUS_COMPLETED` | Task completed and verified |
| `"Failed"` | 8 | `STATUS_FAILED` | Task failed |
| `"Canceling"` | 11 | `STATUS_CANCELING` | Cancel requested, Worker cooperating |
| `"Canceled"` | 9 | `STATUS_CANCELED` | Task canceled |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATAVERSE_URL` | `https://org3e79cdb1.crm3.dynamics.com` | Dataverse instance URL |
| `TABLE_NAME` | `cr_shraga_tasks` | Dataverse tasks table name |
| `WEBHOOK_USER` | `""` (set by setup script) | User email for task ownership |
| `WORK_BASE_DIR` | Script parent directory | Base directory for local work folders |
| `SHRAGA_ROOT` | `C:\Dev\Shraga` | Root directory for immutable releases |
| `ONEDRIVE_SESSIONS_DIR` | (auto-detected) | Override for OneDrive root path |

## Version Check and Auto-Update

The worker uses an immutable release deployment system:
1. Code lives in `C:\Dev\Shraga\releases\vN\` as plain file copies (no .git)
2. Between tasks, the worker calls `version_check.should_exit()` to compare its release folder name against `current_version.txt`
3. If a newer version is available, the worker exits gracefully and the scheduled task watchdog restarts it from the new release folder via the .cmd wrapper

## Key Design Decisions

- **Streaming mode**: Worker and verifier use `--output-format stream-json` with `--verbose` and `--include-partial-messages` to stream real-time progress to Dataverse messages.
- **ETag concurrency**: Task claiming uses HTTP `If-Match` headers to prevent double-pickup across dev boxes.
- **CLAUDECODE env stripping**: All subprocess calls to Claude Code strip the `CLAUDECODE` environment variable to avoid "nested session" errors.
- **Suffix-based file detection**: `_path_looks_like_file()` uses path suffix instead of `Path.is_file()` to avoid OneDrive sync race conditions where files exist locally but haven't synced yet.
- **Open competition routing**: PS creates tasks with `crb3b_devbox=null`. All workers for the same user compete for unclaimed Pending tasks. Winner writes hostname on claim.
