#!/usr/bin/env python3
"""
Manual cleanup script for stale Outbound rows in cr_shraga_conversations.

Marks all Unclaimed Outbound rows as Delivered to prevent them from
interfering with the isFollowup polling filter.

Usage:
    python scripts/cleanup_stale_rows.py
    python scripts/cleanup_stale_rows.py --user-email sagik@microsoft.com
    python scripts/cleanup_stale_rows.py --max-age-minutes 5 --dry-run
"""
import argparse
import os
import sys
import requests
from datetime import datetime, timezone, timedelta
from azure.identity import DefaultAzureCredential

DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
CONVERSATIONS_TABLE = os.environ.get("CONVERSATIONS_TABLE", "cr_shraga_conversations")
REQUEST_TIMEOUT = 30

# Conversation direction / status string values (match task_manager.py)
DIRECTION_OUTBOUND = "Outbound"
STATUS_UNCLAIMED = "Unclaimed"
STATUS_DELIVERED = "Delivered"


def get_token() -> str:
    """Authenticate via DefaultAzureCredential (same as all other components)."""
    cred = DefaultAzureCredential()
    token = cred.get_token(f"{DATAVERSE_URL}/.default")
    return token.token


def headers(token: str, content_type: str | None = None) -> dict:
    """Build OData headers consistent with the rest of the codebase."""
    h = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }
    if content_type:
        h["Content-Type"] = content_type
    return h


def query_stale_rows(
    token: str,
    user_email: str | None = None,
    max_age_minutes: int = 10,
) -> list[dict]:
    """Query cr_shraga_conversations for Unclaimed Outbound rows older than max_age_minutes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    filters = [
        f"cr_direction eq '{DIRECTION_OUTBOUND}'",
        f"cr_status eq '{STATUS_UNCLAIMED}'",
        f"createdon lt {cutoff_iso}",
    ]
    if user_email:
        # Sanitize to prevent OData injection (double single-quotes)
        safe_email = user_email.replace("'", "''")
        filters.append(f"cr_useremail eq '{safe_email}'")

    filter_str = " and ".join(filters)
    url = (
        f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
        f"?$filter={filter_str}"
        f"&$orderby=createdon asc"
        f"&$select=cr_shraga_conversationid,cr_useremail,cr_name,cr_status,"
        f"cr_direction,cr_message,createdon"
    )

    resp = requests.get(url, headers=headers(token), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("value", [])


def mark_delivered(token: str, row_id: str) -> bool:
    """Mark a single conversation row as Delivered."""
    url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}({row_id})"
    body = {"cr_status": STATUS_DELIVERED}
    resp = requests.patch(
        url,
        headers=headers(token, content_type="application/json"),
        json=body,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return True


def print_row_summary(row: dict, index: int) -> None:
    """Print a human-readable summary of one conversation row."""
    row_id = row.get("cr_shraga_conversationid", "?")
    user = row.get("cr_useremail", "?")
    created = row.get("createdon", "?")
    name = (row.get("cr_name") or "")[:80]
    message_preview = (row.get("cr_message") or "")[:120]
    print(f"  [{index}] id={row_id}")
    print(f"       user={user}  created={created}")
    print(f"       name={name}")
    if message_preview:
        print(f"       message={message_preview}...")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Clean up stale Unclaimed Outbound rows in cr_shraga_conversations.",
    )
    parser.add_argument(
        "--user-email",
        default=None,
        help="Only clean up rows for this user email (default: all users).",
    )
    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=10,
        help="Only clean up rows older than this many minutes (default: 10).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be cleaned up without making changes.",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Shraga Conversations — Stale Row Cleanup")
    print("=" * 60)
    print(f"  Dataverse:        {DATAVERSE_URL}")
    print(f"  Table:            {CONVERSATIONS_TABLE}")
    print(f"  Filter:           direction={DIRECTION_OUTBOUND}, status={STATUS_UNCLAIMED}")
    print(f"  Max age:          {args.max_age_minutes} minutes")
    print(f"  User email:       {args.user_email or '(all users)'}")
    print(f"  Dry run:          {args.dry_run}")
    print("=" * 60)
    print()

    # --- Authenticate ---
    print("[AUTH] Acquiring token via DefaultAzureCredential...")
    try:
        token = get_token()
    except Exception as e:
        print(f"[ERROR] Failed to get token: {e}")
        print("Make sure you are logged in (az login) or have valid Azure credentials.")
        sys.exit(1)
    print("[AUTH] Token acquired.")
    print()

    # --- Query stale rows ---
    print("[QUERY] Searching for stale Unclaimed Outbound rows...")
    try:
        rows = query_stale_rows(token, user_email=args.user_email, max_age_minutes=args.max_age_minutes)
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] Query failed: {e}")
        if e.response is not None:
            print(f"        Response: {e.response.text[:500]}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Query failed: {e}")
        sys.exit(1)

    if not rows:
        print("[RESULT] No stale rows found. Nothing to clean up.")
        sys.exit(0)

    print(f"[RESULT] Found {len(rows)} stale row(s):\n")
    for i, row in enumerate(rows, start=1):
        print_row_summary(row, i)

    # --- Mark as Delivered ---
    if args.dry_run:
        print("[DRY RUN] No changes made. Re-run without --dry-run to clean up.")
        sys.exit(0)

    print(f"[CLEANUP] Marking {len(rows)} row(s) as Delivered...")
    success_count = 0
    fail_count = 0
    for row in rows:
        row_id = row.get("cr_shraga_conversationid")
        if not row_id:
            print(f"  [SKIP] Row missing ID, skipping.")
            fail_count += 1
            continue
        try:
            mark_delivered(token, row_id)
            print(f"  [OK] {row_id} -> Delivered")
            success_count += 1
        except Exception as e:
            print(f"  [FAIL] {row_id}: {e}")
            fail_count += 1

    # --- Summary ---
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Total found:      {len(rows)}")
    print(f"  Marked Delivered:  {success_count}")
    print(f"  Failed:           {fail_count}")
    print("=" * 60)

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
