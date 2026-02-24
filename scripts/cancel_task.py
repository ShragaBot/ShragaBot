#!/usr/bin/env python3
"""
Cancel a task in Dataverse.

Handles the correct cancellation logic:
- Pending(1) -> Canceled(9) directly
- Running(5) -> Canceling(11) cooperatively (Worker stops at next checkpoint)
- Submitted(10) -> Canceled(9) directly
- Other states -> error (already terminal or invalid)

Usage:
    python scripts/cancel_task.py --task-id <uuid> --email user@example.com
    python scripts/cancel_task.py --latest --email user@example.com

Exit codes:
    0  -- success (task canceled or canceling)
    1  -- task not found or not cancelable
    2  -- error (auth failure, API error, etc.)
"""
from __future__ import annotations

import argparse
import json
import sys

import requests as http_requests

from dv_helpers import DataverseClient

TASKS_TABLE = "cr_shraga_tasks"

STATUS_PENDING = 1
STATUS_RUNNING = 5
STATUS_COMPLETED = 7
STATUS_FAILED = 8
STATUS_CANCELED = 9
STATUS_SUBMITTED = 10
STATUS_CANCELING = 11

CANCELABLE = {STATUS_PENDING, STATUS_RUNNING, STATUS_SUBMITTED}
STATUS_NAMES = {
    STATUS_PENDING: "Pending",
    STATUS_RUNNING: "Running",
    STATUS_COMPLETED: "Completed",
    STATUS_FAILED: "Failed",
    STATUS_CANCELED: "Canceled",
    STATUS_SUBMITTED: "Submitted",
    STATUS_CANCELING: "Canceling",
}


def cancel_task(task_id: str) -> dict:
    """Cancel a task by ID using ETag-based optimistic concurrency.

    Returns a dict with the result: task_id, previous_status, new_status, message.
    """
    dv = DataverseClient()
    try:
        task = dv.get_row(
            TASKS_TABLE,
            task_id,
            select="cr_shraga_taskid,cr_status,crb3b_shortdescription",
        )
    except http_requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return {
                "task_id": task_id,
                "cancelable": False,
                "message": "Task not found.",
            }
        raise

    current_status = task.get("cr_status")
    etag = task.get("@odata.etag")
    status_name = STATUS_NAMES.get(current_status, f"Unknown({current_status})")

    if current_status not in CANCELABLE:
        return {
            "task_id": task_id,
            "cancelable": False,
            "current_status": status_name,
            "message": f"Task is {status_name} -- cannot cancel.",
        }

    # Determine new status
    if current_status in (STATUS_PENDING, STATUS_SUBMITTED):
        new_status = STATUS_CANCELED
        new_name = "Canceled"
        message = f"Task canceled (was {status_name})."
    elif current_status == STATUS_RUNNING:
        new_status = STATUS_CANCELING
        new_name = "Canceling"
        message = "Cancellation requested. Worker will stop at next checkpoint."
    else:
        return {"task_id": task_id, "cancelable": False, "message": "Unexpected state."}

    # Use ETag for optimistic concurrency (prevents race with Worker)
    success = dv.update_row(TASKS_TABLE, task_id, {"cr_status": new_status}, etag=etag)
    if not success:
        return {
            "task_id": task_id,
            "cancelable": False,
            "message": "Task status changed while canceling (concurrent modification). Try again.",
        }

    return {
        "task_id": task_id,
        "cancelable": True,
        "previous_status": status_name,
        "new_status": new_name,
        "message": message,
    }


def find_latest_cancelable_task(email: str) -> str | None:
    """Find the most recent cancelable task for the given user."""
    dv = DataverseClient()
    safe_email = dv.sanitize_odata(email)
    filter_expr = (
        f"crb3b_useremail eq '{safe_email}' and "
        f"(cr_status eq {STATUS_PENDING} or cr_status eq {STATUS_RUNNING} or cr_status eq {STATUS_SUBMITTED})"
    )
    rows = dv.get_rows(
        TASKS_TABLE,
        filter=filter_expr,
        select="cr_shraga_taskid,cr_status,crb3b_shortdescription",
        orderby="createdon desc",
        top=1,
    )
    if rows:
        return rows[0].get("cr_shraga_taskid")
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cancel a task in Dataverse.",
        epilog="Exit codes: 0 = success, 1 = not found/not cancelable, 2 = error.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--task-id", help="Task ID (UUID) to cancel.")
    group.add_argument(
        "--latest",
        action="store_true",
        help="Cancel the most recent cancelable task.",
    )
    parser.add_argument(
        "--email",
        required=True,
        help="User email (required for --latest, used for scoping).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.latest:
            task_id = find_latest_cancelable_task(args.email)
            if not task_id:
                print(json.dumps({
                    "cancelable": False,
                    "message": "No cancelable tasks found.",
                }, indent=2))
                return 1
        else:
            task_id = args.task_id

        result = cancel_task(task_id)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2))
    return 0 if result.get("cancelable") else 1


if __name__ == "__main__":
    sys.exit(main())
