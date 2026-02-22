"""Task management operations for the PM agent.

Usage:
    python scripts/manage_tasks.py list [--user EMAIL] [--status STATUS] [--top N]
    python scripts/manage_tasks.py get TASK_ID
    python scripts/manage_tasks.py create --prompt "TASK DESCRIPTION" --user EMAIL [--devbox HOSTNAME]
    python scripts/manage_tasks.py cancel TASK_ID
"""
import argparse
import json
import os
import platform
import sys

# Add parent dir so we can import dv_helpers
sys.path.insert(0, os.path.dirname(__file__))
from dv_helpers import get_headers

DV_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DV_API = f"{DV_URL}/api/data/v9.2"
TASKS_TABLE = os.environ.get("TABLE_NAME", "cr_shraga_tasks")

# Status label -> integer mapping
STATUS_INT = {
    "Pending": 1, "Queued": 3, "Running": 5, "WaitingForInput": 6,
    "Completed": 7, "Failed": 8, "Canceled": 9,
}
# Reverse mapping
INT_STATUS = {v: k for k, v in STATUS_INT.items()}


def list_tasks(user_email: str | None = None, status: str | None = None, top: int = 10):
    """List tasks, optionally filtered by user and/or status."""
    import requests
    headers = get_headers()
    filters = []
    if user_email:
        filters.append(f"crb3b_useremail eq '{user_email}'")
    if status:
        if status in STATUS_INT:
            filters.append(f"cr_status eq {STATUS_INT[status]}")
        else:
            print(f"Unknown status: {status}. Valid: {', '.join(STATUS_INT.keys())}", file=sys.stderr)
            sys.exit(2)
    filter_str = " and ".join(filters) if filters else None
    params = {
        "$orderby": "createdon desc",
        "$top": str(top),
        "$select": "cr_shraga_taskid,cr_name,cr_prompt,cr_status,cr_result,crb3b_useremail,crb3b_devbox,createdon,modifiedon,crb3b_shortdescription",
    }
    if filter_str:
        params["$filter"] = filter_str
    r = requests.get(f"{DV_API}/{TASKS_TABLE}", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    rows = r.json().get("value", [])
    tasks = []
    for row in rows:
        status_int = row.get("cr_status")
        tasks.append({
            "id": row.get("cr_shraga_taskid", ""),
            "name": row.get("cr_name", ""),
            "short_description": row.get("crb3b_shortdescription", ""),
            "status": INT_STATUS.get(status_int, str(status_int)),
            "result": (row.get("cr_result") or "")[:200],
            "devbox": row.get("crb3b_devbox", ""),
            "created": row.get("createdon", ""),
            "modified": row.get("modifiedon", ""),
        })
    print(json.dumps(tasks, indent=2))


def get_task(task_id: str):
    """Get full details of a specific task."""
    import requests
    headers = get_headers()
    r = requests.get(f"{DV_API}/{TASKS_TABLE}({task_id})", headers=headers, timeout=30)
    if r.status_code == 404:
        print(json.dumps({"error": "Task not found"}))
        sys.exit(1)
    r.raise_for_status()
    row = r.json()
    status_int = row.get("cr_status")
    task = {
        "id": row.get("cr_shraga_taskid", ""),
        "name": row.get("cr_name", ""),
        "short_description": row.get("crb3b_shortdescription", ""),
        "prompt": row.get("cr_prompt", ""),
        "status": INT_STATUS.get(status_int, str(status_int)),
        "result": row.get("cr_result", ""),
        "devbox": row.get("crb3b_devbox", ""),
        "user": row.get("crb3b_useremail", ""),
        "created": row.get("createdon", ""),
        "modified": row.get("modifiedon", ""),
        "deep_link": row.get("crb3b_deeplink", ""),
    }
    print(json.dumps(task, indent=2))


def create_task(prompt: str, user_email: str, devbox: str | None = None):
    """Create a new task in Dataverse."""
    import requests
    headers = get_headers()
    body = {
        "cr_name": prompt[:100],
        "cr_prompt": prompt,
        "cr_status": STATUS_INT["Pending"],
        "crb3b_useremail": user_email,
        "crb3b_devbox": devbox or platform.node(),
    }
    r = requests.post(f"{DV_API}/{TASKS_TABLE}", headers=headers, json=body, timeout=30)
    r.raise_for_status()
    task_id = r.json().get("cr_shraga_taskid", "")
    print(json.dumps({"created": True, "task_id": task_id, "status": "Pending"}))


def cancel_task(task_id: str):
    """Cancel a task by setting status to Canceled."""
    import requests
    headers = get_headers()
    body = {"cr_status": STATUS_INT["Canceled"]}
    r = requests.patch(f"{DV_API}/{TASKS_TABLE}({task_id})", headers=headers, json=body, timeout=30)
    if r.status_code == 404:
        print(json.dumps({"error": "Task not found"}))
        sys.exit(1)
    r.raise_for_status()
    print(json.dumps({"canceled": True, "task_id": task_id}))


def main():
    parser = argparse.ArgumentParser(description="Shraga task management")
    sub = parser.add_subparsers(dest="command")

    ls = sub.add_parser("list", help="List tasks")
    ls.add_argument("--user", help="Filter by user email")
    ls.add_argument("--status", help="Filter by status (Pending, Running, Completed, etc.)")
    ls.add_argument("--top", type=int, default=10, help="Max results (default 10)")

    gt = sub.add_parser("get", help="Get task details")
    gt.add_argument("task_id", help="Task ID (GUID)")

    cr = sub.add_parser("create", help="Create a new task")
    cr.add_argument("--prompt", required=True, help="Task description")
    cr.add_argument("--user", required=True, help="User email")
    cr.add_argument("--devbox", help="Target dev box hostname (default: this machine)")

    cn = sub.add_parser("cancel", help="Cancel a task")
    cn.add_argument("task_id", help="Task ID (GUID)")

    args = parser.parse_args()
    if args.command == "list":
        list_tasks(user_email=args.user, status=args.status, top=args.top)
    elif args.command == "get":
        get_task(args.task_id)
    elif args.command == "create":
        create_task(prompt=args.prompt, user_email=args.user, devbox=args.devbox)
    elif args.command == "cancel":
        cancel_task(args.task_id)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
