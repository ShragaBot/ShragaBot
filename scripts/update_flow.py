#!/usr/bin/env python3
"""
Deploy and manage Power Automate flow definitions via the Power Automate API.

Reads flow JSON exports, validates their structure and connection references,
then PATCHes the flows via api.flow.microsoft.com.  Also supports exporting
current flow definitions from the environment, listing all known flows, and
batch-deploying all 7 Shraga flows at once.

Usage:
    # Deploy a single flow:
    python scripts/update_flow.py deploy --flow-name TaskCompleted
    python scripts/update_flow.py deploy --flow-id da211a8a-3ef5-4291-bd91-67c4e6e88aec --json-file flows/TaskCompleted.json
    python scripts/update_flow.py deploy --flow-name TaskCompleted --dry-run

    # Deploy all 7 flows:
    python scripts/update_flow.py deploy-all
    python scripts/update_flow.py deploy-all --dry-run

    # Export a flow definition to a JSON file:
    python scripts/update_flow.py export --flow-name TaskRunner --output flows/TaskRunner.json

    # Validate a flow JSON without deploying:
    python scripts/update_flow.py validate --json-file flows/TaskCompleted.json

    # List all known flows:
    python scripts/update_flow.py list

Known Shraga flows (7 total):
    TaskProgressUpdater: 4075d69d-eef6-4a67-81c3-2ea8cc49c5b5  (flows/TaskProgressUpdater.json)
    TaskCompleted:       da211a8a-3ef5-4291-bd91-67c4e6e88aec  (flows/TaskCompleted.json)
    TaskFailed:          a4b59d39-a30f-4f4b-a07f-23b5a513bd11  (flows/TaskFailed.json)
    TaskRunner:          ae21fda1-a415-4e88-8cd4-c90fb0321faf  (flows/TaskRunner.json)
    TaskCanceled:        6db87fc1-a341-4b5d-843b-dfcc68054194  (flows/TaskCanceled.json)
    SendMessage:         0e3f6ece-54a1-606e-e34b-5b1d5d4c536d  (flows/SendMessage.json)
    CancelTask:          8b0d9024-4b95-4b92-83aa-0b6db37040af  (flows/CancelTask.json)

Connection references used:
    shared_commondataserviceforapps: 57aef69c3763444e8cfb3b0b5ba18fea  (all 7 flows)
    shared_teams:                   70d2dee52a344508a14a40ee6013baf1  (5 flows -- progress/completed/failed/canceled/runner)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENV_ID = os.environ.get(
    "PA_ENVIRONMENT_ID", "8660590d-33ac-ecd8-aef3-e4dd0d6071f4"
)
PA_API = "https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple"
PA_API_VERSION = "2016-11-01"
REQUEST_TIMEOUT = 30

# The repo root is two levels up from scripts/
REPO_ROOT = Path(__file__).resolve().parent.parent
FLOWS_DIR = REPO_ROOT / "flows"

# ---------------------------------------------------------------------------
# Flow registry -- canonical mapping of all 7 Shraga flows
# ---------------------------------------------------------------------------

FLOW_REGISTRY: dict[str, dict[str, str]] = {
    "TaskProgressUpdater": {
        "id": "4075d69d-eef6-4a67-81c3-2ea8cc49c5b5",
        "json_file": "flows/TaskProgressUpdater.json",
        "description": "Triggered when a message row is added in Dataverse; "
                       "posts progress Adaptive Cards to Teams.",
    },
    "TaskCompleted": {
        "id": "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
        "json_file": "flows/TaskCompleted.json",
        "description": "Triggered when a task reaches Completed status; "
                       "posts completion Adaptive Card to Teams.",
    },
    "TaskFailed": {
        "id": "a4b59d39-a30f-4f4b-a07f-23b5a513bd11",
        "json_file": "flows/TaskFailed.json",
        "description": "Triggered when a task reaches Failed status; "
                       "posts failure Adaptive Card to Teams.",
    },
    "TaskRunner": {
        "id": "ae21fda1-a415-4e88-8cd4-c90fb0321faf",
        "json_file": "flows/TaskRunner.json",
        "description": "Triggered when a new task row is created with status=Pending; "
                       "sends HTTP request to the dev box worker.",
    },
    "TaskCanceled": {
        "id": "6db87fc1-a341-4b5d-843b-dfcc68054194",
        "json_file": "flows/TaskCanceled.json",
        "description": "Triggered when a task reaches Canceled status; "
                       "posts cancellation Adaptive Card to Teams.",
    },
    "SendMessage": {
        "id": "0e3f6ece-54a1-606e-e34b-5b1d5d4c536d",
        "json_file": "flows/SendMessage.json",
        "description": "Copilot Studio skill flow (ShragaRelay); relays messages "
                       "between the bot and task managers via the conversations table.",
    },
    "CancelTask": {
        "id": "8b0d9024-4b95-4b92-83aa-0b6db37040af",
        "json_file": "flows/CancelTask.json",
        "description": "Copilot Studio skill flow; marks a running task "
                       "as canceled in Dataverse.",
    },
}

# Known connection reference names used across the environment
KNOWN_CONNECTIONS: dict[str, str] = {
    "shared_commondataserviceforapps": "57aef69c3763444e8cfb3b0b5ba18fea",
    "shared_teams": "70d2dee52a344508a14a40ee6013baf1",
    # TaskRunner uses a variant key name but the same underlying connection
    "shared_teams-1": "70d2dee52a344508a14a40ee6013baf1",
}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def get_token() -> str:
    """Acquire a bearer token for the Power Automate management API.

    Resolution order:
    1. ``PA_TOKEN`` environment variable (for CI/CD or pre-fetched tokens).
    2. ``DefaultAzureCredential`` — controlled by the
       ``AZURE_TOKEN_CREDENTIALS`` env var (e.g. ``AzureCliCredential`` on
       dev boxes).

    Returns the raw token string.  Raises ``RuntimeError`` if all methods fail.
    """
    # 1. Environment variable
    env_token = os.environ.get("PA_TOKEN")
    if env_token:
        return env_token

    # 2. DefaultAzureCredential (AZURE_TOKEN_CREDENTIALS controls provider)
    from azure.identity import DefaultAzureCredential

    try:
        cred = DefaultAzureCredential()
        token = cred.get_token("https://service.flow.microsoft.com/.default")
        return token.token
    except Exception as exc:
        raise RuntimeError(
            "Could not acquire a Power Automate API token.  "
            "Set PA_TOKEN, or run 'az login' first."
        ) from exc


# ---------------------------------------------------------------------------
# Flow JSON helpers
# ---------------------------------------------------------------------------

def load_flow_json(json_file: str) -> dict:
    """Load and parse a flow JSON export file.

    Raises ``FileNotFoundError`` or ``json.JSONDecodeError`` on failure.
    """
    with open(json_file, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_definition(flow_json: dict) -> dict | None:
    """Extract ``properties.definition`` from a flow JSON export."""
    return flow_json.get("properties", {}).get("definition")


def extract_connection_references(flow_json: dict) -> dict:
    """Extract ``properties.connectionReferences`` from a flow JSON export."""
    return flow_json.get("properties", {}).get("connectionReferences", {})


def validate_flow_json(flow_json: dict) -> list[str]:
    """Validate a flow JSON export and return a list of issues (empty = valid).

    Checks performed:
    - ``properties.definition`` exists and is a dict
    - ``properties.definition`` contains a ``$schema`` field
    - ``properties.definition`` contains ``triggers`` and ``actions``
    - ``properties.connectionReferences`` exists (can be empty for HTTP-only flows)
    - Connection reference names match known connections
    - Flow ``name`` field is a valid GUID format
    """
    issues: list[str] = []

    # Top-level structure
    props = flow_json.get("properties")
    if not props:
        issues.append("Missing top-level 'properties' key.")
        return issues

    # Definition
    definition = props.get("definition")
    if not definition:
        issues.append("Missing 'properties.definition'.")
    elif not isinstance(definition, dict):
        issues.append("'properties.definition' must be a dict.")
    else:
        if "$schema" not in definition:
            issues.append("Definition missing '$schema' field.")
        if "triggers" not in definition:
            issues.append("Definition missing 'triggers'.")
        if "actions" not in definition:
            issues.append("Definition missing 'actions'.")

    # Connection references
    conn_refs = props.get("connectionReferences", {})
    if not isinstance(conn_refs, dict):
        issues.append("'properties.connectionReferences' must be a dict.")
    else:
        for ref_name, ref_data in conn_refs.items():
            if not isinstance(ref_data, dict):
                issues.append(
                    f"Connection reference '{ref_name}' is not a dict."
                )
                continue
            conn_name = ref_data.get("connectionName")
            if not conn_name:
                issues.append(
                    f"Connection reference '{ref_name}' missing 'connectionName'."
                )
            elif ref_name in KNOWN_CONNECTIONS:
                expected = KNOWN_CONNECTIONS[ref_name]
                if conn_name != expected:
                    issues.append(
                        f"Connection '{ref_name}' has connectionName "
                        f"'{conn_name}', expected '{expected}'."
                    )

    # Flow name (GUID) -- optional, some exports may not have it
    flow_name = flow_json.get("name", "")
    if flow_name and not _looks_like_guid(flow_name):
        issues.append(f"Flow 'name' field '{flow_name}' does not look like a GUID.")

    return issues


def _looks_like_guid(value: str) -> bool:
    """Return True if *value* looks like a GUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)."""
    import re
    return bool(
        re.match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
            value,
        )
    )


def resolve_flow(
    flow_name: str | None = None,
    flow_id: str | None = None,
) -> tuple[str, str, dict[str, str]]:
    """Resolve a flow by name or ID.

    Returns ``(flow_id, flow_name, registry_entry)`` or raises ``SystemExit``
    if the flow cannot be found.
    """
    if flow_name:
        entry = FLOW_REGISTRY.get(flow_name)
        if not entry:
            print(f"[ERROR] Unknown flow name: {flow_name}")
            print(f"  Known flows: {', '.join(sorted(FLOW_REGISTRY))}")
            sys.exit(1)
        return entry["id"], flow_name, entry

    if flow_id:
        for name, entry in FLOW_REGISTRY.items():
            if entry["id"] == flow_id:
                return flow_id, name, entry
        # Not in registry -- allow unregistered flows
        return flow_id, "(unregistered)", {"id": flow_id, "json_file": "", "description": ""}

    print("[ERROR] Provide either --flow-name or --flow-id.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# API interaction helpers
# ---------------------------------------------------------------------------

def _build_flow_url(flow_id: str) -> str:
    """Build the Power Automate API URL for a specific flow."""
    return (
        f"{PA_API}/environments/{ENV_ID}/flows/{flow_id}"
        f"?api-version={PA_API_VERSION}"
    )


def _build_headers(token: str) -> dict[str, str]:
    """Build HTTP headers for the Power Automate API."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def get_flow(flow_id: str, token: str) -> dict | None:
    """GET a flow definition from the Power Automate API.

    Returns the full flow JSON or ``None`` if the flow is not found.
    """
    url = _build_flow_url(flow_id)
    headers = _build_headers(token)
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 200:
        return resp.json()
    return None


def patch_flow(
    flow_id: str,
    definition: dict,
    conn_refs: dict,
    token: str,
) -> requests.Response:
    """PATCH a flow definition via the Power Automate API.

    Returns the raw ``requests.Response`` so callers can inspect status.
    """
    url = _build_flow_url(flow_id)
    headers = _build_headers(token)
    body: dict[str, Any] = {
        "properties": {
            "definition": definition,
            "connectionReferences": conn_refs,
        }
    }
    return requests.patch(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)


# ---------------------------------------------------------------------------
# CLI sub-commands
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> None:
    """List all known Shraga flows with their IDs and JSON file paths."""
    print("=" * 76)
    print("Shraga Power Automate Flows (7 total)")
    print("=" * 76)
    print(f"  Environment: {ENV_ID}")
    print()

    fmt = "  {name:<25s} {fid:<40s} {jfile}"
    print(fmt.format(name="FLOW NAME", fid="FLOW ID", jfile="JSON FILE"))
    print(fmt.format(name="-" * 24, fid="-" * 38, jfile="-" * 30))

    for name, entry in FLOW_REGISTRY.items():
        json_path = REPO_ROOT / entry["json_file"]
        exists_marker = "[OK]" if json_path.exists() else "[MISSING]"
        print(
            fmt.format(name=name, fid=entry["id"], jfile=f"{entry['json_file']} {exists_marker}")
        )

    print()
    print("Connection references:")
    for ref_name, conn_name in KNOWN_CONNECTIONS.items():
        print(f"  {ref_name}: {conn_name}")
    print()


def cmd_validate(args: argparse.Namespace) -> None:
    """Validate a flow JSON file without deploying."""
    json_file = args.json_file
    print(f"[VALIDATE] Checking {json_file}...")

    try:
        flow_json = load_flow_json(json_file)
    except FileNotFoundError:
        print(f"[ERROR] File not found: {json_file}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Invalid JSON: {e}")
        sys.exit(1)

    issues = validate_flow_json(flow_json)

    definition = extract_definition(flow_json) or {}
    conn_refs = extract_connection_references(flow_json)
    display_name = flow_json.get("properties", {}).get("displayName", "(unknown)")
    flow_guid = flow_json.get("name", "(unknown)")

    print(f"  Display name:    {display_name}")
    print(f"  Flow GUID:       {flow_guid}")
    print(f"  Actions:         {len(definition.get('actions', {}))}")
    print(f"  Triggers:        {len(definition.get('triggers', {}))}")
    print(f"  Connection refs: {len(conn_refs)}")

    if conn_refs:
        for ref_name, ref_data in conn_refs.items():
            conn_name = ref_data.get("connectionName", "?")
            print(f"    - {ref_name}: {conn_name}")

    print()

    if issues:
        print(f"[FAIL] {len(issues)} issue(s) found:")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)
    else:
        print("[OK] Flow JSON is valid.")


def cmd_deploy(args: argparse.Namespace) -> None:
    """Deploy a single flow definition to Power Automate."""
    # Resolve flow identity
    flow_id, flow_name, entry = resolve_flow(
        flow_name=args.flow_name,
        flow_id=args.flow_id,
    )

    # Resolve JSON file
    json_file = args.json_file
    if not json_file and entry.get("json_file"):
        json_file = str(REPO_ROOT / entry["json_file"])
    if not json_file:
        print("[ERROR] No --json-file specified and flow has no registered JSON path.")
        sys.exit(1)

    _deploy_single_flow(
        flow_id=flow_id,
        flow_name=flow_name,
        json_file=json_file,
        dry_run=args.dry_run,
    )


def cmd_deploy_all(args: argparse.Namespace) -> None:
    """Deploy all 7 Shraga flows in sequence."""
    print("=" * 60)
    print("Power Automate Flow - Batch Deploy (all 7 flows)")
    print("=" * 60)
    print(f"  Environment: {ENV_ID}")
    print(f"  Dry run:     {args.dry_run}")
    print("=" * 60)
    print()

    results: dict[str, str] = {}

    for name, entry in FLOW_REGISTRY.items():
        json_path = str(REPO_ROOT / entry["json_file"])
        print(f"\n{'~' * 60}")
        print(f"  Deploying: {name}")
        print(f"{'~' * 60}")

        try:
            _deploy_single_flow(
                flow_id=entry["id"],
                flow_name=name,
                json_file=json_path,
                dry_run=args.dry_run,
            )
            results[name] = "OK" if not args.dry_run else "DRY RUN"
        except SystemExit:
            results[name] = "FAILED"
            # Continue deploying remaining flows
            continue

    # Summary
    print()
    print("=" * 60)
    print("Batch Deploy Summary")
    print("=" * 60)
    for name, status in results.items():
        print(f"  {name:<25s} {status}")
    print("=" * 60)

    failed_count = sum(1 for s in results.values() if s == "FAILED")
    if failed_count:
        print(f"\n[WARNING] {failed_count} flow(s) failed to deploy.")
        sys.exit(1)


def cmd_export(args: argparse.Namespace) -> None:
    """Export a flow definition from Power Automate to a JSON file."""
    flow_id, flow_name, _entry = resolve_flow(
        flow_name=args.flow_name,
        flow_id=args.flow_id,
    )

    output_file = args.output
    if not output_file:
        output_file = str(FLOWS_DIR / f"{flow_name}.json")

    print("=" * 60)
    print("Power Automate Flow - Export")
    print("=" * 60)
    print(f"  Environment: {ENV_ID}")
    print(f"  Flow:        {flow_name} ({flow_id})")
    print(f"  Output:      {output_file}")
    print("=" * 60)
    print()

    # Authenticate
    print("[AUTH] Acquiring token...")
    try:
        token = get_token()
    except Exception as e:
        print(f"[ERROR] Failed to get token: {e}")
        sys.exit(1)
    print("[AUTH] Token acquired.")

    # Fetch flow
    print(f"[GET] Fetching flow {flow_id}...")
    flow_data = get_flow(flow_id, token)
    if not flow_data:
        print(f"[ERROR] Flow not found: {flow_id}")
        sys.exit(1)

    display_name = flow_data.get("properties", {}).get("displayName", "?")
    print(f"[GET] Found: {display_name}")

    # Write to file
    print(f"[WRITE] Saving to {output_file}...")
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(flow_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"[OK] Exported {display_name} to {output_file}")


def _deploy_single_flow(
    flow_id: str,
    flow_name: str,
    json_file: str,
    dry_run: bool = False,
) -> None:
    """Deploy a single flow -- shared logic for deploy and deploy-all.

    Raises ``SystemExit`` on error.
    """
    print("=" * 60)
    print("Power Automate Flow - Definition Update")
    print("=" * 60)
    print(f"  Environment: {ENV_ID}")
    print(f"  Flow:        {flow_name} ({flow_id})")
    print(f"  JSON file:   {json_file}")
    print(f"  Dry run:     {dry_run}")
    print("=" * 60)
    print()

    # --- Load flow JSON ---
    print(f"[FILE] Loading flow JSON from {json_file}...")
    try:
        flow_json = load_flow_json(json_file)
    except FileNotFoundError:
        print(f"[ERROR] File not found: {json_file}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Invalid JSON in {json_file}: {e}")
        sys.exit(1)

    # --- Validate ---
    issues = validate_flow_json(flow_json)
    if issues:
        print(f"[WARNING] Validation found {len(issues)} issue(s):")
        for issue in issues:
            print(f"  - {issue}")
        print()

    definition = extract_definition(flow_json)
    conn_refs = extract_connection_references(flow_json)

    if not definition:
        print("[ERROR] Could not find 'properties.definition' in the JSON file.")
        sys.exit(1)

    print(f"[FILE] Definition actions: {len(definition.get('actions', {}))}")
    print(f"[FILE] Connection refs:    {len(conn_refs)}")
    if conn_refs:
        for ref_name in conn_refs:
            print(f"         - {ref_name}")
    print()

    # --- Authenticate ---
    print("[AUTH] Acquiring token for Power Automate API...")
    try:
        token = get_token()
    except Exception as e:
        print(f"[ERROR] Failed to get token: {e}")
        print("Make sure you are logged in (az login).")
        sys.exit(1)
    print("[AUTH] Token acquired.")
    print()

    # --- Verify flow exists ---
    print(f"[GET] Verifying flow {flow_id}...")
    current = get_flow(flow_id, token)
    if not current:
        print(f"[ERROR] Flow not found or inaccessible: {flow_id}")
        sys.exit(1)

    current_name = current.get("properties", {}).get("displayName", "?")
    flow_state = current.get("properties", {}).get("state", "?")
    print(f"[GET] Found: {current_name} (state: {flow_state})")
    print()

    # --- Dry run ---
    if dry_run:
        print("[DRY RUN] Would PATCH the flow with updated definition and connectionReferences.")
        print(f"  Flow: {current_name} ({flow_id})")
        print(f"  Actions: {len(definition.get('actions', {}))}")
        print(f"  Connections: {len(conn_refs)}")
        print()
        print("[DRY RUN] No changes made.")
        return

    # --- PATCH flow definition ---
    print(f"[PATCH] Updating {current_name}...")
    resp = patch_flow(flow_id, definition, conn_refs, token)

    if resp.status_code == 200:
        print("[OK] Flow definition updated successfully!")
    else:
        print(f"[ERROR] PATCH failed: {resp.status_code}")
        print(f"  {resp.text[:500]}")
        sys.exit(1)

    print()
    print("=" * 60)
    print(f"  Flow:   {current_name}")
    print(f"  Status: Definition updated")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with sub-commands."""
    parser = argparse.ArgumentParser(
        description="Deploy and manage Power Automate flow definitions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/update_flow.py list\n"
            "  python scripts/update_flow.py validate --json-file flows/TaskCompleted.json\n"
            "  python scripts/update_flow.py deploy --flow-name TaskCompleted --dry-run\n"
            "  python scripts/update_flow.py deploy-all --dry-run\n"
            "  python scripts/update_flow.py export --flow-name TaskRunner -o flows/TaskRunner.json\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- list ---
    subparsers.add_parser("list", help="List all known Shraga flows.")

    # --- validate ---
    validate_p = subparsers.add_parser("validate", help="Validate a flow JSON file.")
    validate_p.add_argument("--json-file", required=True, help="Path to the flow JSON file.")

    # --- deploy ---
    deploy_p = subparsers.add_parser("deploy", help="Deploy a single flow definition.")
    deploy_p.add_argument("--flow-name", help="Flow name from the registry (e.g. 'TaskCompleted').")
    deploy_p.add_argument("--flow-id", help="Flow GUID (alternative to --flow-name).")
    deploy_p.add_argument("--json-file", help="Path to the flow JSON file (auto-resolved if --flow-name used).")
    deploy_p.add_argument("--dry-run", action="store_true", help="Show what would happen without changes.")

    # --- deploy-all ---
    deploy_all_p = subparsers.add_parser("deploy-all", help="Deploy all 7 Shraga flows.")
    deploy_all_p.add_argument("--dry-run", action="store_true", help="Show what would happen without changes.")

    # --- export ---
    export_p = subparsers.add_parser("export", help="Export a flow definition from Power Automate.")
    export_p.add_argument("--flow-name", help="Flow name from the registry.")
    export_p.add_argument("--flow-id", help="Flow GUID (alternative to --flow-name).")
    export_p.add_argument("-o", "--output", help="Output file path (defaults to flows/<name>.json).")

    return parser


def main(argv: list[str] | None = None) -> None:
    """Entry point for the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "list": cmd_list,
        "validate": cmd_validate,
        "deploy": cmd_deploy,
        "deploy-all": cmd_deploy_all,
        "export": cmd_export,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
