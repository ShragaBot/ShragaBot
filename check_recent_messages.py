#!/usr/bin/env python3
"""Check recent messages in Dataverse messages table"""
import requests
import sys
from azure.identity import DefaultAzureCredential

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DATAVERSE_URL = 'https://org3e79cdb1.crm3.dynamics.com'

def get_token():
    return DefaultAzureCredential().get_token(f"{DATAVERSE_URL}/.default").token

token = get_token()
headers = {
    'Authorization': f'Bearer {token}',
    'Accept': 'application/json',
    'OData-MaxVersion': '4.0',
    'OData-Version': '4.0'
}

url = f'{DATAVERSE_URL}/api/data/v9.2/cr_shragamessages'
params = {
    '$select': 'cr_name,cr_content,createdon',
    '$orderby': 'createdon desc',
    '$top': 20
}

response = requests.get(url, headers=headers, params=params, timeout=10)
data = response.json()
messages = data.get('value', [])

print('=' * 80)
print(f'RECENT MESSAGES (Last {len(messages)})')
print('=' * 80)

for msg in messages:
    name = msg.get('cr_name', 'N/A')[:60]
    created = msg.get('createdon', 'N/A')[:19]
    content = msg.get('cr_content', '')[:100]

    print(f'\n{created} | {name}')
    print(f'  {content}...')

print('\n' + '=' * 80)
