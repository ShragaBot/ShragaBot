#!/usr/bin/env python3
"""
Standalone CLI script to query a user's state from the Dataverse
crb3b_shragausers table.

Extracts the core Dataverse query logic from GlobalManager._get_user_state
into a standalone tool that can be invoked from the command line without
instantiating the full GlobalManager.

Authentication uses ``az account get-access-token`` so the script works on
any machine with a valid ``az login`` session -- no Azure SDK dependency
required at runtime.

Usage:
    python scripts/get_user_state.py --email user@example.com

Exit codes:
    0  -- success (user found, JSON printed to stdout)
    1  -- user not found
    2  -- error (auth failure, network error, etc.)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

import requests

# ── Configuration ─────────────────────────────────────────────────────────

DATAVERSE_URL = os.environ.get(
    "DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com"
)
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
USERS_TABLE = os.environ.get("USERS_TABLE", "crb3b_shragausers")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))


# ── Authentication ────────────────────────────────────────────────────────

def get_access_token(resource_url: str | None = None) -> str:
    """Obtain a Dataverse access token via the Azure CLI.

    Calls ``az account get-access-token --resource <dataverse-url>`` and
    returns the ``accessToken`` string.

    Parameters
    ----------
    resource_url:
        The Dataverse resource URL to request a token for.  Defaults to
        ``DATAVERSE_URL``.

    Returns
    -------
    str
        The Bearer access token.

    Raises
    ------
    RuntimeError
        If the ``az`` command fails or returns unexpected output.
    """
    target = resource_url or DATAVERSE_URL
    cmd = [
        "az", "account", "get-access-token",
        "--resource", target,
        "--query", "accessToken",
        "--output", "tsv",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Azure CLI (az) not found. Install it or run 'az login' first."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("az account get-access-token timed out after 30 s.")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"az account get-access-token failed (rc={result.returncode}): {stderr}"
        )

    token = result.stdout.strip()
    if not token:
        raise RuntimeError(
            "az account get-access-token returned an empty token."
        )
    return token


# ── Dataverse Query ───────────────────────────────────────────────────────

def _build_headers(token: str) -> dict[str, str]:
    """Build OData-compliant request headers for the Dataverse REST API."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }


def get_user_state(email: str, token: str) -> dict[str, Any] | None:
    """Query crb3b_shragausers for a user by email.

    This mirrors the logic in ``GlobalManager._get_user_state`` but is
    completely self-contained -- no class instantiation required.

    Parameters
    ----------
    email:
        The user's email address to look up.
    token:
        A valid Bearer token scoped to the Dataverse resource.

    Returns
    -------
    dict | None
        The full Dataverse row as a dict if the user was found, or ``None``
        if no matching row exists.

    Raises
    ------
    requests.exceptions.HTTPError
        On non-2xx responses from Dataverse.
    requests.exceptions.Timeout
        When the request exceeds ``REQUEST_TIMEOUT``.
    """
    # Sanitize email to prevent OData injection (double single-quotes)
    safe_email = email.replace("'", "''")
    url = (
        f"{DATAVERSE_API}/{USERS_TABLE}"
        f"?$filter=crb3b_useremail eq '{safe_email}'"
        f"&$top=1"
    )
    headers = _build_headers(token)

    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    rows = resp.json().get("value", [])
    if rows:
        return rows[0]
    return None


# ── Formatting ────────────────────────────────────────────────────────────

def format_user_state(row: dict[str, Any]) -> dict[str, Any]:
    """Transform a raw Dataverse row into a clean JSON-serialisable dict.

    Mirrors the enrichment that ``GlobalManager._tool_get_user_state``
    performs on top of the raw row, producing a consistent shape for
    downstream consumers.
    """
    onboarding_step = row.get("crb3b_onboardingstep", "")
    return {
        "found": True,
        "user_email": row.get("crb3b_useremail", ""),
        "user_id": row.get("crb3b_shragauserid", ""),
        "onboarding_step": onboarding_step,
        "devbox_name": row.get("crb3b_devboxname", ""),
        "devbox_status": row.get("crb3b_devboxstatus", ""),
        "azure_ad_id": row.get("crb3b_azureadid", ""),
        "connection_url": row.get("crb3b_connectionurl") or None,
        "auth_url": row.get("crb3b_authurl") or None,
        "claude_auth_status": row.get("crb3b_claudeauthstatus", ""),
        "manager_status": row.get("crb3b_managerstatus", ""),
        "last_seen": row.get("crb3b_lastseen", ""),
        "provisioning_started": onboarding_step in (
            "provisioning", "waiting_provisioning",
            "auth_pending", "auth_code_sent", "completed",
        ),
        "provisioning_complete": onboarding_step in (
            "auth_pending", "auth_code_sent", "completed",
        ),
        "auth_complete": onboarding_step == "completed",
        # Include the raw Dataverse row for transparency
        "raw": row,
    }


# ── CLI ───────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser for this script."""
    parser = argparse.ArgumentParser(
        description=(
            "Query user state from the Dataverse crb3b_shragausers table. "
            "Outputs a JSON object to stdout."
        ),
        epilog=(
            "Exit codes: 0 = success (user found), "
            "1 = user not found, "
            "2 = error."
        ),
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Email address of the user to look up.",
    )
    parser.add_argument(
        "--dataverse-url",
        default=None,
        help=(
            "Override the Dataverse instance URL. "
            "Defaults to the DATAVERSE_URL environment variable."
        ),
    )
    parser.add_argument(
        "--users-table",
        default=None,
        help=(
            "Override the users table name. "
            "Defaults to the USERS_TABLE environment variable."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the CLI.

    Parameters
    ----------
    argv:
        Command-line arguments (excluding ``sys.argv[0]``).  When ``None``
        the real command-line is used.  Accepting an explicit list makes the
        function easy to test without spawning a subprocess.

    Returns
    -------
    int
        Exit code: 0 on success, 1 if the user was not found, 2 on error.
    """
    global DATAVERSE_URL, DATAVERSE_API, USERS_TABLE  # noqa: PLW0603

    parser = build_parser()
    args = parser.parse_args(argv)

    # Apply overrides
    if args.dataverse_url:
        DATAVERSE_URL = args.dataverse_url
        DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
    if args.users_table:
        USERS_TABLE = args.users_table

    # ── Authenticate ──────────────────────────────────────────────────
    try:
        token = get_access_token(DATAVERSE_URL)
    except RuntimeError as exc:
        result = {"error": str(exc), "email": args.email}
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 2

    # ── Query ─────────────────────────────────────────────────────────
    try:
        row = get_user_state(args.email, token)
    except requests.exceptions.Timeout:
        result = {"error": "Dataverse request timed out", "email": args.email}
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 2
    except requests.exceptions.HTTPError as exc:
        detail = ""
        if exc.response is not None:
            detail = getattr(exc.response, "text", "")[:500]
        result = {
            "error": f"Dataverse HTTP error: {exc}",
            "detail": detail,
            "email": args.email,
        }
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 2
    except Exception as exc:
        result = {"error": str(exc), "email": args.email}
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 2

    # ── Output ────────────────────────────────────────────────────────
    if row is None:
        result = {"found": False, "email": args.email}
        print(json.dumps(result, indent=2))
        return 1

    formatted = format_user_state(row)
    print(json.dumps(formatted, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
