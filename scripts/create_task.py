#!/usr/bin/env python3
"""
Create a task in Dataverse with all required fields set deterministically.

This script is the ONLY way the PS should create tasks. It ensures all
fields are set correctly and waits for the TaskRunner flow to post the
Adaptive Card before returning (synchronous).

Usage:
    python scripts/create_task.py --prompt "Build a REST API" --email user@example.com --mcs-id abc --reply-to row123

Exit codes:
    0  -- success (task created, card posted, JSON with task ID printed)
    2  -- error (auth failure, API error, timeout, etc.)
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from dv_helpers import DataverseClient

TASKS_TABLE = "cr_shraga_tasks"
STATUS_SUBMITTED = 10
CARD_POLL_INTERVAL = 3  # seconds
CARD_POLL_TIMEOUT = 60  # seconds (must be less than SendMessage flow's 90s timeout)


def generate_short_description(prompt: str, max_length: int = 100) -> str:
    """Generate a short description from the prompt.

    Takes the first sentence or first max_length chars, whichever is shorter.
    """
    first_line = prompt.split("\n")[0].strip()
    for sep in [". ", "! ", "? "]:
        idx = first_line.find(sep)
        if 0 < idx < max_length:
            return first_line[: idx + 1]
    if len(first_line) > max_length:
        return first_line[: max_length - 3].rstrip() + "..."
    return first_line or "Untitled task"


def create_task(
    prompt: str,
    email: str,
    mcs_conversation_id: str | None = None,
    inbound_row_id: str | None = None,
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
    inbound_row_id:
        The conversations table inbound row ID for follow-up matching.

    Returns
    -------
    dict
        The created task row from Dataverse including cr_shraga_taskid.
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
    if inbound_row_id:
        data["crb3b_inboundrowid"] = inbound_row_id

    dv = DataverseClient()
    result = dv.create_row(TASKS_TABLE, data)

    if result is None:
        raise RuntimeError("Dataverse returned no data for created task")

    return result


def wait_for_card(task_id: str, dv: DataverseClient) -> str | None:
    """Poll until TaskRunner posts the card and writes crb3b_deeplink.

    Returns the deep link URL, or None on timeout.
    """
    start = time.time()
    while time.time() - start < CARD_POLL_TIMEOUT:
        time.sleep(CARD_POLL_INTERVAL)
        try:
            row = dv.get_row(TASKS_TABLE, task_id, select="crb3b_deeplink")
            deeplink = row.get("crb3b_deeplink")
            if deeplink:
                return deeplink
        except Exception:
            pass  # Retry on transient errors
    return None


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
    parser.add_argument(
        "--reply-to",
        default=None,
        help="Inbound conversation row ID for follow-up matching.",
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
            inbound_row_id=args.reply_to,
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2

    task_id = result.get("cr_shraga_taskid", result.get("_extracted_id", "unknown"))
    short_desc = result.get("crb3b_shortdescription", "")

    # Wait for TaskRunner to post the card (synchronous -- blocks until card link appears)
    dv = DataverseClient()
    deeplink = wait_for_card(task_id, dv)

    output = {
        "task_id": task_id,
        "status": "Submitted",
        "short_description": short_desc,
        "email": args.email,
        "card_link": deeplink,
    }
    if not deeplink:
        output["warning"] = "Card not posted within timeout -- task was created but card link unavailable"

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
