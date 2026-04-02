#!/bin/bash
# Register Telegram webhooks for all bots in config.json
# Usage: ./setup_webhooks.sh <public_url>
# Example: ./setup_webhooks.sh https://your-machine.tail12345.ts.net

set -e

PUBLIC_URL="${1:?Usage: ./setup_webhooks.sh <public_url>}"
CONFIG="config.json"

if [ ! -f "$CONFIG" ]; then
    echo "Error: config.json not found. Copy config.example.json to config.json and edit it."
    exit 1
fi

echo "Registering webhooks with: $PUBLIC_URL"
echo ""

python3 -c "
import json, urllib.request

with open('$CONFIG') as f:
    config = json.load(f)

for agent in config['agents']:
    name = agent['name']
    token = agent['token']
    prefix = token.split(':')[0]
    webhook_url = f'$PUBLIC_URL/webhook/{prefix}'

    url = f'https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}'
    try:
        resp = urllib.request.urlopen(url, timeout=30)
        data = json.loads(resp.read())
        status = '✓' if data.get('ok') else '✗'
        print(f'  {status} {name}: {webhook_url}')
    except Exception as e:
        print(f'  ✗ {name}: {e}')
"

echo ""
echo "Done! Send a message to any bot to test."
