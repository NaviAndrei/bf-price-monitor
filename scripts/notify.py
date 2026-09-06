# scripts/notify.py
import json
import os
import time

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

messages = json.load(open("data/formatted_alerts.json", encoding="utf-8"))

for msg in messages:
    while True:
        r = requests.post(SEND_URL, json={"chat_id": CHAT_ID, "text": msg})
        if r.status_code == 429:
            retry_after = r.json().get("parameters", {}).get("retry_after", 5)
            print(f"Rate limited by Telegram, waiting {retry_after}s before retrying")
            time.sleep(retry_after)
            continue
        break
    time.sleep(1.1)  # Telegram allows ~1 message/second per chat
