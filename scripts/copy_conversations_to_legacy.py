#!/usr/bin/env python3
"""Copy all cr_shraga_conversation rows to cr_shraga_conversation_legacy using $batch API."""
import json
import uuid
import requests
from azure.identity import DefaultAzureCredential

DV = "https://org3e79cdb1.crm3.dynamics.com"
API = f"{DV}/api/data/v9.2"
BATCH_SIZE = 500  # Dataverse limit is 1000 per batch, use 500 for safety

def get_token():
    return DefaultAzureCredential().get_token(f"{DV}/.default").token

def get_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }

def fetch_all_source_rows(token):
    """Fetch all rows from the original table with paging."""
    fields = "cr_name,cr_useremail,cr_mcs_conversation_id,cr_message,cr_direction,cr_status,cr_claimed_by,cr_in_reply_to,cr_followup_expected,cr_processed_by,cr_shraga_conversationid"
    url = f"{API}/cr_shraga_conversations?$select={fields}&$orderby=createdon asc&$count=true"
    rows = []
    while url:
        r = requests.get(url, headers=get_headers(token), timeout=60)
        r.raise_for_status()
        d = r.json()
        if "@odata.count" in d:
            print(f"Total source rows: {d['@odata.count']}")
        rows.extend(d["value"])
        url = d.get("@odata.nextLink")
        if url:
            print(f"  Paging... fetched {len(rows)} so far")
    return rows

def delete_existing_legacy_rows(token):
    """Delete all existing rows in legacy table (from partial copy) using $batch."""
    url = f"{API}/cr_shraga_conversation_legacies?$select=cr_shraga_conversation_legacyid&$count=true"
    rows = []
    while url:
        r = requests.get(url, headers=get_headers(token), timeout=60)
        r.raise_for_status()
        d = r.json()
        if "@odata.count" in d:
            print(f"Legacy rows to delete: {d['@odata.count']}")
        rows.extend(d["value"])
        url = d.get("@odata.nextLink")

    if not rows:
        print("No legacy rows to delete.")
        return

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        _send_batch_delete(token, batch)
        print(f"  Deleted batch {i // BATCH_SIZE + 1} ({len(batch)} rows)")

def _send_batch_delete(token, rows):
    batch_id = str(uuid.uuid4())
    boundary = f"batch_{batch_id}"
    changeset_id = str(uuid.uuid4())
    cs_boundary = f"changeset_{changeset_id}"

    body_parts = [f"--{boundary}", f"Content-Type: multipart/mixed; boundary={cs_boundary}", ""]
    for idx, row in enumerate(rows):
        rid = row["cr_shraga_conversation_legacyid"]
        body_parts.extend([
            f"--{cs_boundary}",
            "Content-Type: application/http",
            "Content-Transfer-Encoding: binary",
            f"Content-ID: {idx + 1}",
            "",
            f"DELETE {API}/cr_shraga_conversation_legacies({rid}) HTTP/1.1",
            "Content-Type: application/json",
            "",
            "",
        ])
    body_parts.append(f"--{cs_boundary}--")
    body_parts.append(f"--{boundary}--")

    payload = "\r\n".join(body_parts)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/mixed; boundary={boundary}",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }
    r = requests.post(f"{API}/$batch", headers=headers, data=payload.encode("utf-8"), timeout=120)
    if r.status_code not in (200, 204):
        print(f"  Batch delete error: {r.status_code} - {r.text[:500]}")

def copy_rows_batch(token, rows):
    """Copy rows to legacy table using $batch API."""
    total = len(rows)
    for i in range(0, total, BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        _send_batch_create(token, batch)
        print(f"  Copied batch {i // BATCH_SIZE + 1} ({len(batch)} rows, {min(i + BATCH_SIZE, total)}/{total})")

def _send_batch_create(token, rows):
    batch_id = str(uuid.uuid4())
    boundary = f"batch_{batch_id}"
    changeset_id = str(uuid.uuid4())
    cs_boundary = f"changeset_{changeset_id}"

    body_parts = [f"--{boundary}", f"Content-Type: multipart/mixed; boundary={cs_boundary}", ""]
    for idx, row in enumerate(rows):
        record = {
            "cr_name": (row.get("cr_name") or "")[:200],
            "cr_useremail": row.get("cr_useremail") or "",
            "cr_mcs_conversation_id": row.get("cr_mcs_conversation_id") or "",
            "cr_message": row.get("cr_message") or "",
            "cr_direction": row.get("cr_direction") or "",
            "cr_status": row.get("cr_status") or "",
            "cr_claimed_by": row.get("cr_claimed_by") or "",
            "cr_in_reply_to": row.get("cr_in_reply_to") or "",
            "cr_followup_expected": row.get("cr_followup_expected") or "",
            "cr_processed_by": row.get("cr_processed_by") or "",
            "cr_original_id": row.get("cr_shraga_conversationid") or "",
        }
        json_body = json.dumps(record)
        body_parts.extend([
            f"--{cs_boundary}",
            "Content-Type: application/http",
            "Content-Transfer-Encoding: binary",
            f"Content-ID: {idx + 1}",
            "",
            f"POST {API}/cr_shraga_conversation_legacies HTTP/1.1",
            "Content-Type: application/json",
            "",
            json_body,
        ])
    body_parts.append(f"--{cs_boundary}--")
    body_parts.append(f"--{boundary}--")

    payload = "\r\n".join(body_parts)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/mixed; boundary={boundary}",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }
    r = requests.post(f"{API}/$batch", headers=headers, data=payload.encode("utf-8"), timeout=120)
    if r.status_code not in (200, 204):
        print(f"  Batch create error: {r.status_code} - {r.text[:500]}")
    else:
        # Check for individual failures in the multipart response
        if "HTTP/1.1 4" in r.text or "HTTP/1.1 5" in r.text:
            # Count failures
            failures = r.text.count("HTTP/1.1 4") + r.text.count("HTTP/1.1 5")
            print(f"  Warning: {failures} individual failures in batch")

def main():
    print("[AUTH] Getting token...")
    token = get_token()
    print("[AUTH] OK")

    print("\n[STEP 1] Fetch all source rows...")
    source_rows = fetch_all_source_rows(token)
    print(f"Fetched {len(source_rows)} rows.")

    print("\n[STEP 2] Delete existing legacy rows (from partial copy)...")
    delete_existing_legacy_rows(token)

    print("\n[STEP 3] Batch copy to legacy table...")
    copy_rows_batch(token, source_rows)

    print("\n[VERIFY] Checking legacy row count...")
    r = requests.get(f"{API}/cr_shraga_conversation_legacies?$select=cr_name&$count=true",
                     headers=get_headers(token), timeout=30)
    d = r.json()
    count = d.get("@odata.count", len(d.get("value", [])))
    print(f"Legacy table: {count} rows (expected {len(source_rows)})")

    if count == len(source_rows):
        print("\n[DONE] All rows backed up successfully!")
    else:
        print(f"\n[WARNING] Row count mismatch: {count} vs {len(source_rows)}")

if __name__ == "__main__":
    main()
