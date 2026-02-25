"""
Create cr_processed_by column on cr_shraga_conversations table in Dataverse.

Uses the EntityDefinitions metadata API to add a Single Line of Text column.
"""
import os
import sys
import json
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dv_client import DataverseClient

DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
API_BASE = f"{DATAVERSE_URL}/api/data/v9.2"

def main():
    from azure.identity import DefaultAzureCredential
    cred = DefaultAzureCredential()
    dv = DataverseClient(dataverse_url=DATAVERSE_URL, credential=cred)
    token = dv.get_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }

    # Check if column already exists
    check_url = (
        f"{API_BASE}/EntityDefinitions(LogicalName='cr_shraga_conversation')"
        f"/Attributes(LogicalName='cr_processed_by')"
    )
    resp = requests.get(check_url, headers=headers, timeout=30)
    if resp.status_code == 200:
        print("Column cr_processed_by already exists on cr_shraga_conversations.")
        return 0

    # Create the column
    create_url = (
        f"{API_BASE}/EntityDefinitions(LogicalName='cr_shraga_conversation')/Attributes"
    )
    body = {
        "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
        "SchemaName": "cr_processed_by",
        "DisplayName": {
            "@odata.type": "Microsoft.Dynamics.CRM.Label",
            "LocalizedLabels": [
                {"Label": "Processed By", "LanguageCode": 1033}
            ]
        },
        "Description": {
            "@odata.type": "Microsoft.Dynamics.CRM.Label",
            "LocalizedLabels": [
                {"Label": "Role:version:session_id of the Claude session that processed this message", "LanguageCode": 1033}
            ]
        },
        "RequiredLevel": {"Value": "None"},
        "MaxLength": 200,
        "FormatName": {"Value": "Text"},
        "ImeMode": "Auto"
    }

    print(f"Creating cr_processed_by column on cr_shraga_conversation...")
    resp = requests.post(create_url, headers=headers, json=body, timeout=60)
    if resp.status_code in (200, 201, 204):
        print(f"SUCCESS: Column created (HTTP {resp.status_code})")
        return 0
    else:
        print(f"FAILED: HTTP {resp.status_code}")
        print(resp.text[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())
