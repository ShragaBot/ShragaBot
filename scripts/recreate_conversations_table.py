#!/usr/bin/env python3
"""
Recreate cr_shraga_conversations table as User-owned.

The original table was Organization-owned which prevents row-level security.
This script deletes it and recreates with identical schema but UserOwned.

WARNING: Deletes all existing conversation data. Only run in dev.

Usage:
    python scripts/recreate_conversations_table.py
    python scripts/recreate_conversations_table.py --skip-delete  # create only (if table doesn't exist)
"""
import argparse
import json
import os
import sys
import time

import requests
from azure.identity import DefaultAzureCredential

DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
METADATA_API = f"{DATAVERSE_URL}/api/data/v9.2/EntityDefinitions"
TABLE_SCHEMA_NAME = "cr_shraga_conversation"
REQUEST_TIMEOUT = 60


def get_token():
    cred = DefaultAzureCredential()
    return cred.get_token(f"{DATAVERSE_URL}/.default").token


def hdrs(token, content_type="application/json"):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": content_type,
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }


def _label(text):
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.Label",
        "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                              "Label": text, "LanguageCode": 1033}],
    }


def _string_col(schema_name, display_name, max_length=200, is_primary=False):
    col = {
        "AttributeType": "String",
        "SchemaName": schema_name,
        "MaxLength": max_length,
        "DisplayName": _label(display_name),
        "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
    }
    if is_primary:
        col["IsPrimaryName"] = True
    return col


def _memo_col(schema_name, display_name, max_length=100000):
    return {
        "AttributeType": "Memo",
        "SchemaName": schema_name,
        "MaxLength": max_length,
        "DisplayName": _label(display_name),
        "@odata.type": "Microsoft.Dynamics.CRM.MemoAttributeMetadata",
    }


def find_table(token):
    """Find the table's EntityDefinition by schema name."""
    url = f"{METADATA_API}(LogicalName='{TABLE_SCHEMA_NAME}')?$select=MetadataId,LogicalName,OwnershipType"
    resp = requests.get(url, headers=hdrs(token), timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def delete_table(token):
    """Delete the existing table."""
    info = find_table(token)
    if not info:
        print("[SKIP] Table not found, nothing to delete.")
        return True

    metadata_id = info["MetadataId"]
    ownership = info.get("OwnershipType")
    print(f"[INFO] Found table: {TABLE_SCHEMA_NAME}, OwnershipType={ownership}, MetadataId={metadata_id}")

    url = f"{METADATA_API}({metadata_id})"
    print(f"[DELETE] Deleting table {TABLE_SCHEMA_NAME}...")
    resp = requests.delete(url, headers=hdrs(token), timeout=REQUEST_TIMEOUT)
    if resp.status_code in (200, 204):
        print("[DELETE] Table deleted successfully.")
        return True
    else:
        print(f"[ERROR] Delete failed: {resp.status_code}")
        print(resp.text[:500])
        return False


def create_table(token):
    """Create the conversations table as User-owned."""
    body = {
        "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
        "SchemaName": TABLE_SCHEMA_NAME,
        "DisplayName": _label("Shraga Conversation"),
        "DisplayCollectionName": _label("Shraga Conversations"),
        "Description": _label("Message bus between MCS bot and task managers"),
        "HasNotes": False,
        "HasActivities": False,
        "OwnershipType": "UserOwned",
        "IsActivity": False,
        "PrimaryNameAttribute": "cr_name",
        "Attributes": [
            _string_col("cr_name", "Name", 200, is_primary=True),
            _string_col("cr_useremail", "User Email", 200),
            _string_col("cr_mcs_conversation_id", "MCS Conversation ID", 500),
            _memo_col("cr_message", "Message", 100000),
            _string_col("cr_direction", "Direction", 20),
            _string_col("cr_status", "Status", 50),
            _string_col("cr_claimed_by", "Claimed By", 500),
            _string_col("cr_in_reply_to", "In Reply To", 100),
            _string_col("cr_followup_expected", "Follow-up Expected", 10),
            _string_col("cr_processed_by", "Processed By", 500),
        ],
    }

    print(f"[CREATE] Creating table {TABLE_SCHEMA_NAME} as UserOwned...")
    resp = requests.post(METADATA_API, headers=hdrs(token), json=body, timeout=REQUEST_TIMEOUT)
    if resp.status_code in (200, 201, 204):
        print("[CREATE] Table created successfully!")
        # Verify ownership
        info = find_table(token)
        if info:
            print(f"[VERIFY] OwnershipType = {info.get('OwnershipType')}")
        return True
    else:
        print(f"[ERROR] Create failed: {resp.status_code}")
        print(resp.text[:1000])
        return False


def main():
    parser = argparse.ArgumentParser(description="Recreate cr_shraga_conversations as User-owned")
    parser.add_argument("--skip-delete", action="store_true", help="Skip deletion (create only)")
    args = parser.parse_args()

    print("[AUTH] Getting token...")
    token = get_token()
    print("[AUTH] Token acquired.")

    if not args.skip_delete:
        if not delete_table(token):
            return 1
        # Wait for Dataverse to propagate the deletion
        print("[WAIT] Waiting 10s for deletion to propagate...")
        time.sleep(10)

    if not create_table(token):
        return 1

    print("\n[DONE] Table recreated as UserOwned.")
    print("[NEXT] Update the Shraga User security role to set User-level permissions on this table.")
    print("[NEXT] Reconnect Power Automate flows that reference this table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
