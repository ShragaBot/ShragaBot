#!/usr/bin/env python3
"""Get full completion message"""
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
    '$filter': "contains(cr_name, 'Task completed')",
    '$select': 'cr_name,cr_content,createdon',
    '$orderby': 'createdon desc',
    '$top': 1
}

response = requests.get(url, headers=headers, params=params, timeout=10)
data = response.json()
messages = data.get('value', [])

if messages:
    msg = messages[0]
    print('=' * 80)
    print('LATEST COMPLETION MESSAGE')
    print('=' * 80)
    print(f'Time: {msg.get("createdon", "N/A")[:19]}')
    print(f'Title: {msg.get("cr_name", "N/A")}')
    print('\nFULL CONTENT:')
    print('-' * 80)
    print(msg.get('cr_content', ''))
    print('-' * 80)
else:
    print('No completion messages found')
