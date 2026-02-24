#!/usr/bin/env python3
"""
Create a task in Dataverse with all required fields set deterministically.

This script is the ONLY way the PM should create tasks. It ensures all
fields (cr_name, cr_status, crb3b_useremail, crb3b_shortdescription,
crb3b_mcsconversationid) are set correctly every time.

Usage:
    python scripts/create_task.py --prompt "Build a REST API" --email user@example.com
    python scripts/create_task.py --prompt "Fix login bug" --email user@example.com --mcs-id abc123

Exit codes:
    0  -- success (task created, JSON with task ID printed to stdout)
    2  -- error (auth failure, API error, etc.)
"""
from __future__ import annotations

import argparse
import json
import sys

from dv_helpers import DataverseClient

TASKS_TABLE = "cr_shraga_tasks"
STATUS_SUBMITTED = 10


def generate_short_description(prompt: str, max_length: int = 100) -> str:
    """Generate a short description from the prompt.

    Takes the first sentence or first max_length chars, whichever is shorter.
    """
    # Take first line
    first_line = prompt.split("\n")[0].strip()
    # Take first sentence if it exists
    for sep in [". ", "! ", "? "]:
        idx = first_line.find(sep)
        if 0 < idx < max_length:
            return first_line[: idx + 1]
    # Truncate with ellipsis
    if len(first_line) > max_length:
        return first_line[: max_length - 3].rstrip() + "..."
    return first_line or "Untitled task"


def create_task(
    prompt: str,
    email: str,
    mcs_conversation_id: str | None = None,
) -> dict:
    """Create a task in Dataverse with all required fields.

    Parameters
    ----------
    prompt:
        The full task prompt from the user.
    email:
        The user's email address.
    mcs_conversation_id:
        The MCS conversation ID for follow-up card links.

    Returns
    -------
    dict
        The created task row from Dataverse including cr_shraga_taskid.

    Raises
    ------
    Exception
        On API errors.
    """
    short_desc = generate_short_description(prompt)

    data = {
        "cr_prompt": prompt,
        "cr_name": short_desc,
        "cr_status": STATUS_SUBMITTED,
        "crb3b_useremail": email,
        "crb3b_shortdescription": short_desc,
    }

    if mcs_conversation_id:
        data["crb3b_mcsconversationid"] = mcs_conversation_id

    dv = DataverseClient()
    result = dv.create_row(TASKS_TABLE, data)

    if result is None:
        raise RuntimeError("Dataverse returned no data for created task")

    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a coding task in Dataverse.",
        epilog="Exit codes: 0 = success, 2 = error.",
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help="The full task prompt.",
    )
    parser.add_argument(
        "--email",
        required=True,
        help="User email address (crb3b_useremail).",
    )
    parser.add_argument(
        "--mcs-id",
        default=None,
        help="MCS conversation ID for follow-up card links.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = create_task(
            prompt=args.prompt,
            email=args.email,
            mcs_conversation_id=args.mcs_id,
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2

    # Extract task ID
    task_id = result.get("cr_shraga_taskid", result.get("_extracted_id", "unknown"))
    short_desc = result.get("crb3b_shortdescription", "")

    output = {
        "task_id": task_id,
        "status": "Submitted",
        "short_description": short_desc,
        "email": args.email,
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
