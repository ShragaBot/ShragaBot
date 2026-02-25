#!/usr/bin/env python3
"""
Send an outbound message via the cr_shraga_conversations Dataverse table.

Extracts the conversation response-writing logic from GlobalManager.send_response
into a standalone CLI tool. The script looks up the original inbound message
(--reply-to) to resolve the user_email and mcs_conversation_id, then writes
an Outbound row to the conversations table.

Usage:
    python scripts/send_message.py --reply-to {id} --message 'Hello'
    python scripts/send_message.py --reply-to {id} --message 'Working on it' --followup
"""
import argparse
import os
import sys
import requests

DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
CONVERSATIONS_TABLE = os.environ.get("CONVERSATIONS_TABLE", "cr_shraga_conversations")
REQUEST_TIMEOUT = 30

# Conversation direction / status string values (match global_manager.py)
DIRECTION_OUTBOUND = "Outbound"
STATUS_UNCLAIMED = "Unclaimed"


def get_token() -> str:
    """Authenticate via DefaultAzureCredential (AZURE_TOKEN_CREDENTIALS controls provider)."""
    from azure.identity import DefaultAzureCredential
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


def fetch_parent_message(token: str, row_id: str) -> dict:
    """Look up an existing conversation row by its primary key.

    Returns the row dict or raises if not found / request fails.
    This is needed to resolve cr_useremail and cr_mcs_conversation_id
    from the original inbound message that we are replying to.
    """
    url = (
        f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}({row_id})"
        f"?$select=cr_shraga_conversationid,cr_useremail,cr_mcs_conversation_id,"
        f"cr_message,cr_direction,cr_status"
    )
    resp = requests.get(url, headers=build_headers(token), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def send_message(
    token: str,
    in_reply_to: str,
    user_email: str,
    mcs_conversation_id: str,
    message: str,
    followup: bool = False,
) -> dict | bool:
    """Write an outbound row to the cr_shraga_conversations table.

    This is a standalone extraction of GlobalManager.send_response (lines 495-520
    of global-manager/global_manager.py). The body fields, direction, and status
    values are identical to the original implementation.

    Args:
        token: Dataverse OAuth bearer token.
        in_reply_to: The cr_shraga_conversationid of the inbound message we
            are replying to.
        user_email: The user's email address (cr_useremail).
        mcs_conversation_id: The MCS conversation ID (cr_mcs_conversation_id).
        message: The outbound message text.
        followup: If True, sets cr_followup_expected to "true" so the relay
            flow knows to wait for more messages before responding to the user.

    Returns:
        The response JSON from Dataverse on success, True for 204 No Content,
        or None on failure.
    """
    body = {
        "cr_name": message[:100],
        "cr_useremail": user_email,
        "cr_mcs_conversation_id": mcs_conversation_id,
        "cr_message": message,
        "cr_direction": DIRECTION_OUTBOUND,
        "cr_status": STATUS_UNCLAIMED,
        "cr_in_reply_to": in_reply_to,
        "cr_followup_expected": "true" if followup else "",
    }
    url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
    resp = requests.post(
        url,
        headers=build_headers(token, content_type="application/json"),
        json=body,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    if resp.status_code == 204 or not resp.content:
        return True
    return resp.json()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on success, 1 on failure."""
    parser = argparse.ArgumentParser(
        description=(
            "Send an outbound message via the cr_shraga_conversations "
            "Dataverse table."
        ),
    )
    parser.add_argument(
        "--reply-to",
        required=True,
        help=(
            "The cr_shraga_conversationid of the inbound message to reply to. "
            "The script looks up this row to resolve user_email and "
            "mcs_conversation_id automatically."
        ),
    )
    parser.add_argument(
        "--message",
        required=True,
        help="The outbound message text to send.",
    )
    parser.add_argument(
        "--followup",
        action="store_true",
        default=False,
        help=(
            "Mark the message as having a follow-up expected. The relay flow "
            "will wait for more messages before responding to the user."
        ),
    )
    args = parser.parse_args(argv)

    reply_to_id = args.reply_to
    message_text = args.message
    followup = args.followup

    # --- Authenticate ---
    print(f"[AUTH] Acquiring token via DefaultAzureCredential...")
    try:
        token = get_token()
    except Exception as e:
        print(f"[ERROR] Failed to get token: {e}")
        print("Make sure you are logged in (az login) or have valid Azure credentials.")
        return 1
    print("[AUTH] Token acquired.")

    # --- Look up parent message ---
    print(f"[LOOKUP] Fetching parent message {reply_to_id}...")
    try:
        parent = fetch_parent_message(token, reply_to_id)
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] Failed to fetch parent message: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"        Response: {e.response.text[:500]}")
        return 1
    except Exception as e:
        print(f"[ERROR] Failed to fetch parent message: {e}")
        return 1

    user_email = parent.get("cr_useremail", "")
    mcs_conversation_id = parent.get("cr_mcs_conversation_id", "")

    if not user_email:
        print(f"[ERROR] Parent message {reply_to_id} has no cr_useremail. Cannot send reply.")
        return 1

    print(f"[LOOKUP] Found: user_email={user_email}, mcs_conversation_id={mcs_conversation_id}")

    # --- Send message ---
    print(f"[SEND] Writing outbound message (followup={followup})...")
    try:
        result = send_message(
            token=token,
            in_reply_to=reply_to_id,
            user_email=user_email,
            mcs_conversation_id=mcs_conversation_id,
            message=message_text,
            followup=followup,
        )
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] Failed to send message: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"        Response: {e.response.text[:500]}")
        return 1
    except Exception as e:
        print(f"[ERROR] Failed to send message: {e}")
        return 1

    print(f"[SEND] Outbound message written successfully.")
    print(f"  reply_to:  {reply_to_id}")
    print(f"  to:        {user_email}")
    print(f"  message:   {message_text[:80]}{'...' if len(message_text) > 80 else ''}")
    print(f"  followup:  {followup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
