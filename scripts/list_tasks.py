#!/usr/bin/env python3
"""
List recent tasks from Dataverse for a user.

Usage:
    python scripts/list_tasks.py --email user@example.com
    python scripts/list_tasks.py --email user@example.com --status running
    python scripts/list_tasks.py --email user@example.com --top 20

Exit codes:
    0  -- success (JSON array printed to stdout)
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

STATUS_BY_NAME = {v.lower(): k for k, v in STATUS_NAMES.items()}

SELECT_FIELDS = (
    "cr_shraga_taskid,cr_name,cr_status,crb3b_shortdescription,"
    "crb3b_devbox,crb3b_onedriveurl,crb3b_sessionduration,createdon,modifiedon"
)


def list_tasks(
    email: str,
    status: int | None = None,
    top: int = 10,
) -> list[dict]:
    """List recent tasks for a user.

    Parameters
    ----------
    email:
        User email to filter by.
    status:
        Optional status code to filter by.
    top:
        Maximum number of tasks to return.

    Returns
    -------
    list[dict]
        Formatted task summaries.
    """
    dv = DataverseClient()

    safe_email = DataverseClient.sanitize_odata(email)
    filter_parts = [f"crb3b_useremail eq '{safe_email}'"]
    if status is not None:
        filter_parts.append(f"cr_status eq {status}")

    rows = dv.get_rows(
        TASKS_TABLE,
        filter=" and ".join(filter_parts),
        select=SELECT_FIELDS,
        orderby="createdon desc",
        top=top,
    )

    return [format_task_summary(row) for row in rows]


def format_task_summary(row: dict) -> dict:
    """Format a task row into a concise summary."""
    status_int = row.get("cr_status")
    return {
        "task_id": row.get("cr_shraga_taskid"),
        "task_id_short": row.get("cr_shraga_taskid", "")[:8],
        "name": row.get("cr_name") or row.get("crb3b_shortdescription") or "Unnamed",
        "status": STATUS_NAMES.get(status_int, f"Unknown({status_int})"),
        "devbox": row.get("crb3b_devbox"),
        "duration": row.get("crb3b_sessionduration"),
        "onedrive_url": row.get("crb3b_onedriveurl"),
        "created": row.get("createdon"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List recent tasks from Dataverse.",
        epilog="Exit codes: 0 = success, 2 = error.",
    )
    parser.add_argument(
        "--email",
        required=True,
        help="User email to filter tasks by.",
    )
    parser.add_argument(
        "--status",
        default=None,
        choices=list(STATUS_BY_NAME.keys()),
        help="Filter by status name (e.g., running, completed, failed).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Maximum number of tasks to return (default: 10).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    status_int = STATUS_BY_NAME.get(args.status) if args.status else None

    try:
        tasks = list_tasks(
            email=args.email,
            status=status_int,
            top=args.top,
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2

    print(json.dumps({"count": len(tasks), "tasks": tasks}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
