"""
Configure the MCS bot's SendMessage topic to relay messages via ShragaRelay.

Every user message goes through the ShragaRelay flow:
  1. Pass System.User.Email, System.Conversation.Id, System.Activity.Text to flow
  2. Flow writes to conversations table, waits for task manager response
  3. Flow returns responseText + isFollowupExpected
  4. Topic sends responseText back to user, loops if follow-up expected

The topic definition is loaded from bot/fallback_topic.yaml (the source of truth).
The target is the SendMessage custom topic (OnActivity trigger, catches all messages),
NOT the Fallback system topic (OnUnknownIntent, only catches unmatched intents).

Requires: az login (DefaultAzureCredential)
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path

import requests
import json
from azure.identity import DefaultAzureCredential

DATAVERSE_URL = "https://org3e79cdb1.crm3.dynamics.com"
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"

# WARNING: This flow ID MUST match the flowId in bot/fallback_topic.yaml.
# If you change the flow in Power Automate or Copilot Studio, update BOTH files
# to keep them in sync. A mismatch will cause the bot topic to invoke a
# nonexistent or wrong flow, silently breaking the relay pipeline.
#
# ShragaRelay workflow entity ID (used by Copilot Studio topic):
#   dec9329f-8112-f111-8341-002248d570fd
# ShragaRelay Power Automate flow ID (used by update_flow.py):
#   0e3f6ece-54a1-606e-e34b-5b1d5d4c536d
# NOTE: Copilot Studio uses the Dataverse WORKFLOW ENTITY ID, not the PA flow ID.
RELAY_FLOW_ID = "dec9329f-8112-f111-8341-002248d570fd"

# SendMessage custom topic component ID (OnActivity trigger -- catches all messages)
SENDMESSAGE_TOPIC_ID = "f1c8960a-b669-41d5-a92b-0db9c1248f2c"

# Fallback system topic component ID (kept for reference, not used by this script)
# FALLBACK_COMPONENT_ID = "928c6921-eb36-450f-b2bc-9ad966b3f02e"

# Bot ID
BOT_ID = "888e4800-5a06-f111-8406-7c1e5287291b"


def get_headers():
    cred = DefaultAzureCredential()
    token = cred.get_token(f"{DATAVERSE_URL}/.default").token
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }


# Load the topic YAML from the repo (bot/fallback_topic.yaml is the source of truth).
# Despite the filename, this YAML is deployed to the SendMessage custom topic.
_TOPIC_YAML_PATH = Path(__file__).resolve().parent.parent / "bot" / "fallback_topic.yaml"
RELAY_TOPIC_YAML = _TOPIC_YAML_PATH.read_text(encoding="utf-8")


def update_sendmessage_topic():
    """Update the SendMessage topic to relay messages through ShragaRelay flow."""
    headers = get_headers()

    # First, read current state
    resp = requests.get(
        f"{DATAVERSE_API}/botcomponents({SENDMESSAGE_TOPIC_ID})?$select=name,data,schemaname,statecode",
        headers=headers, timeout=30,
    )
    if resp.status_code != 200:
        print(f"ERROR: Could not read SendMessage topic: {resp.status_code} {resp.text[:200]}")
        return False

    current = resp.json()
    print(f"Current SendMessage topic:")
    print(f"  Name: {current.get('name')}")
    print(f"  Schema: {current.get('schemaname')}")
    print(f"  State: {current.get('statecode')} (0=active, 1=inactive)")
    print(f"  Current data:\n{current.get('data', '')}\n")

    # Update the topic data
    print("Updating SendMessage topic with ShragaRelay flow definition...")
    payload = {"data": RELAY_TOPIC_YAML}

    resp = requests.patch(
        f"{DATAVERSE_API}/botcomponents({SENDMESSAGE_TOPIC_ID})",
        headers=headers,
        json=payload,
        timeout=30,
    )

    if resp.status_code in (200, 204):
        print("OK: SendMessage topic updated successfully!")

        # Verify the update
        resp2 = requests.get(
            f"{DATAVERSE_API}/botcomponents({SENDMESSAGE_TOPIC_ID})?$select=data",
            headers=headers, timeout=30,
        )
        if resp2.status_code == 200:
            new_data = resp2.json().get("data", "")
            print(f"\nNew topic data:\n{new_data}")
        return True
    else:
        print(f"ERROR: Update failed: {resp.status_code}")
        print(resp.text[:500])
        return False


def disable_conversational_boosting():
    """Disable the 'Conversational boosting' (Search) topic so it doesn't
    intercept messages before Fallback."""
    headers = get_headers()

    # The conversational boosting topic
    search_id = "239dd253-1057-4f58-b876-6e5629cc70b6"

    resp = requests.get(
        f"{DATAVERSE_API}/botcomponents({search_id})?$select=name,statecode",
        headers=headers, timeout=30,
    )
    if resp.status_code == 200:
        current = resp.json()
        print(f"\nConversational boosting topic state: {current.get('statecode')}")
        if current.get("statecode") == 0:
            # State 0 = active, 1 = inactive
            print("Disabling Conversational boosting topic...")
            resp2 = requests.patch(
                f"{DATAVERSE_API}/botcomponents({search_id})",
                headers=headers,
                json={"statecode": 1},
                timeout=30,
            )
            if resp2.status_code in (200, 204):
                print("✓ Conversational boosting disabled!")
                return True
            else:
                print(f"WARN: Could not disable: {resp2.status_code} {resp2.text[:200]}")
                return False
        else:
            print("Already disabled.")
            return True
    else:
        print(f"WARN: Could not read Conversational boosting topic: {resp.status_code}")
        return False


if __name__ == "__main__":
    print("=== Configuring MCS Bot SendMessage Topic ===\n")
    update_sendmessage_topic()
    disable_conversational_boosting()
    print("\n=== Done ===")
    print("\nNOTE: You need to publish the bot in Copilot Studio for changes to take effect.")
