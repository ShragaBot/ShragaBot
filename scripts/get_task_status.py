#!/usr/bin/env python3
"""
Get the status of a specific task from Dataverse.

Usage:
    python scripts/get_task_status.py --task-id <uuid>
    python scripts/get_task_status.py --task-id <short-id> --email user@example.com

Exit codes:
    0  -- success (task found, JSON printed to stdout)
    1  -- task not found
    2  -- error (auth failure, API error, etc.)
"""
from __future__ import annotations

import argparse
import json
import sys

from dv_helpers import DataverseClient

TASKS_TABLE = "cr_shraga_tasks"

STATUS_NAMES = {
    1: "Pending",
    5: "Running",
    7: "Completed",
    8: "Failed",
    9: "Canceled",
    10: "Submitted",
    11: "Canceling",
}

SELECT_FIELDS = (
    "cr_shraga_taskid,cr_name,cr_status,cr_prompt,cr_result,"
    "crb3b_shortdescription,crb3b_devbox,crb3b_useremail,"
    "crb3b_onedriveurl,crb3b_sessioncost,crb3b_sessiontokens,"
    "crb3b_sessionduration,createdon,modifiedon"
)


def get_task_status(task_id: str) -> dict | None:
    """Get task details by full UUID."""
    dv = DataverseClient()
    try:
        row = dv.get_row(TASKS_TABLE, task_id, select=SELECT_FIELDS)
    except Exception:
        return None
    return format_task(row)


def find_task_by_short_id(short_id: str, email: str) -> dict | None:
    """Find a task by short ID prefix (first 8 chars) scoped to user."""
    dv = DataverseClient()
    safe_email = dv.sanitize_odata(email)
    rows = dv.get_rows(
        TASKS_TABLE,
        filter=f"crb3b_useremail eq '{safe_email}'",
        select=SELECT_FIELDS,
        orderby="createdon desc",
        top=50,
    )
    for row in rows:
        tid = row.get("cr_shraga_taskid", "")
        if tid.startswith(short_id):
            return format_task(row)
    return None


def format_task(row: dict) -> dict:
    """Format a raw DV row into a clean output dict."""
    status_int = row.get("cr_status")
    return {
        "task_id": row.get("cr_shraga_taskid"),
        "task_id_short": row.get("cr_shraga_taskid", "")[:8],
        "name": row.get("cr_name") or row.get("crb3b_shortdescription") or "Unnamed",
        "status": STATUS_NAMES.get(status_int, f"Unknown({status_int})"),
        "status_code": status_int,
        "prompt": row.get("cr_prompt", "")[:200],
        "result": row.get("cr_result", "")[:500] if row.get("cr_result") else None,
        "devbox": row.get("crb3b_devbox"),
        "onedrive_url": row.get("crb3b_onedriveurl"),
        "cost": row.get("crb3b_sessioncost"),
        "tokens": row.get("crb3b_sessiontokens"),
        "duration": row.get("crb3b_sessionduration"),
        "created": row.get("createdon"),
        "modified": row.get("modifiedon"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Get task status from Dataverse.",
        epilog="Exit codes: 0 = found, 1 = not found, 2 = error.",
    )
    parser.add_argument(
        "--task-id",
        required=True,
        help="Task ID (full UUID or short 8-char prefix).",
    )
    parser.add_argument(
        "--email",
        default=None,
        help="User email (required when using short ID prefix).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        # Full UUID (36 chars) or short ID
        if len(args.task_id) >= 32:
            try:
                result = get_task_status(args.task_id)
            except Exception:
                result = None
        else:
            if not args.email:
                print(json.dumps({"error": "Short ID requires --email"}, indent=2), file=sys.stderr)
                return 2
            result = find_task_by_short_id(args.task_id, args.email)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2

    if result is None:
        print(json.dumps({"found": False, "task_id": args.task_id}, indent=2))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
