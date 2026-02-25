#!/usr/bin/env python3
"""
Standalone script to create or update a user row in the crb3b_shragausers
Dataverse table.

Extracted from global-manager/global_manager.py for ad-hoc administrative
use.  Supports field validation against the same VALID_USER_FIELDS allow-list
used by the Global Manager at runtime.

Usage:
    python scripts/update_user_state.py --email user@example.com --field crb3b_onboardingstep=provisioning
    python scripts/update_user_state.py --email user@example.com --field crb3b_devboxname=shraga-box-01 --field crb3b_azureadid=aad-guid

Multiple --field arguments are supported.  Each must be in key=value format.
Field names are validated against the allow-list; invalid fields are rejected
and the script exits with a non-zero status.

Output is JSON on stdout for easy consumption by other tools.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import requests
from azure.identity import DefaultAzureCredential

# ---------------------------------------------------------------------------
# Configuration -- mirrors global_manager.py defaults
# ---------------------------------------------------------------------------

DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
USERS_TABLE = os.environ.get("USERS_TABLE", "crb3b_shragausers")
REQUEST_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Valid user fields -- must stay in sync with global_manager.VALID_USER_FIELDS
# ---------------------------------------------------------------------------

VALID_USER_FIELDS = frozenset({
    "crb3b_shragauserid",
    "crb3b_useremail",
    "crb3b_azureadid",
    "crb3b_devboxname",
    "crb3b_devboxstatus",
    "crb3b_claudeauthstatus",
    "crb3b_managerstatus",
    "crb3b_onboardingstep",
    "crb3b_lastseen",
    "crb3b_connectionurl",
    "crb3b_authurl",
})


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def get_token() -> str:
    """Authenticate via DefaultAzureCredential (AZURE_TOKEN_CREDENTIALS controls provider)."""
    cred = DefaultAzureCredential()
    token = cred.get_token(f"{DATAVERSE_URL}/.default")
    return token.token


def build_headers(token: str, content_type: str | None = None) -> dict:
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


def lookup_user(token: str, user_email: str) -> dict | None:
    """Query crb3b_shragausers for a user by email.

    Returns the first matching row as a dict, or None if the user is not found.
    """
    # Sanitize email to prevent OData injection (double single-quotes)
    safe_email = user_email.replace("'", "''")
    url = (
        f"{DATAVERSE_API}/{USERS_TABLE}"
        f"?$filter=crb3b_useremail eq '{safe_email}'"
        f"&$top=1"
    )
    resp = requests.get(url, headers=build_headers(token), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    return rows[0] if rows else None


def update_user_state(
    token: str,
    user_email: str,
    fields: dict,
) -> dict:
    """Create or update a user row in the crb3b_shragausers Dataverse table.

    If the user already exists (by lookup), PATCH the row.
    Otherwise, POST to create a new row.

    Returns a result dict suitable for JSON serialisation:
        {"success": True, "action": "patch"|"create", "fields": {...}}
    or  {"success": False, "error": "..."}
    """
    hdrs = build_headers(token, content_type="application/json")

    # Always include last-seen timestamp (same as GlobalManager._update_user_state)
    fields.setdefault("crb3b_lastseen", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    # Try to resolve existing row
    existing = lookup_user(token, user_email)
    row_id = existing.get("crb3b_shragauserid") if existing else None

    if row_id:
        # PATCH existing row
        url = f"{DATAVERSE_API}/{USERS_TABLE}({row_id})"
        resp = requests.patch(url, headers=hdrs, json=fields, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return {"success": True, "action": "patch", "row_id": row_id, "fields": fields}
    else:
        # POST new row
        fields["crb3b_useremail"] = user_email
        url = f"{DATAVERSE_API}/{USERS_TABLE}"
        resp = requests.post(url, headers=hdrs, json=fields, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        new_id = None
        if resp.status_code != 204 and resp.content:
            new_row = resp.json()
            new_id = new_row.get("crb3b_shragauserid")
        return {"success": True, "action": "create", "row_id": new_id, "fields": fields}


# ---------------------------------------------------------------------------
# Field parsing and validation
# ---------------------------------------------------------------------------

def parse_field(field_str: str) -> tuple[str, str]:
    """Parse a 'key=value' string into (key, value).

    Raises ValueError if the string does not contain exactly one '='.
    """
    if "=" not in field_str:
        raise ValueError(
            f"Invalid field format: '{field_str}'. Expected key=value."
        )
    key, value = field_str.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        raise ValueError(f"Empty key in field: '{field_str}'.")
    return key, value


def validate_fields(fields: dict) -> list[str]:
    """Validate field names against VALID_USER_FIELDS.

    Returns a list of invalid field names (empty list means all valid).
    """
    return sorted(set(fields.keys()) - VALID_USER_FIELDS)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Create or update a user row in the crb3b_shragausers Dataverse table. "
            "Fields are validated against the VALID_USER_FIELDS allow-list."
        ),
    )
    parser.add_argument(
        "--email",
        required=True,
        help="User email address (crb3b_useremail).",
    )
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="key=value",
        help=(
            "Field to set (repeatable). Must be in key=value format. "
            "Valid keys: " + ", ".join(sorted(VALID_USER_FIELDS))
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns 0 on success, non-zero on failure.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Parse --field arguments into a dict
    fields: dict[str, str] = {}
    for field_str in args.field:
        try:
            key, value = parse_field(field_str)
        except ValueError as exc:
            result = {"success": False, "error": str(exc)}
            print(json.dumps(result, indent=2))
            return 1
        fields[key] = value

    # Validate field names against allow-list
    invalid = validate_fields(fields)
    if invalid:
        result = {
            "success": False,
            "error": f"Invalid field(s): {', '.join(invalid)}",
            "invalid_fields": invalid,
            "valid_fields": sorted(VALID_USER_FIELDS),
        }
        print(json.dumps(result, indent=2))
        return 1

    # Authenticate and execute
    try:
        token = get_token()
    except Exception as exc:
        result = {"success": False, "error": f"Authentication failed: {exc}"}
        print(json.dumps(result, indent=2))
        return 1

    try:
        result = update_user_state(token, args.email, fields)
    except Exception as exc:
        result = {"success": False, "error": f"Dataverse update failed: {exc}"}
        print(json.dumps(result, indent=2))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
