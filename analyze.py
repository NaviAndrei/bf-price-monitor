# scripts/analyze.py
from huggingface_hub import InferenceClient
import json, os

client = InferenceClient(api_key=os.environ["HF_TOKEN"])
alerts = json.load(open("data/alerts.json"))

messages = []
for a in alerts:
    prompt = f"Product: {a['title']}. Old price: {a['old']}. New price: {a['new']}. Is this a genuine Black Friday discount or a fake price hike? One sentence."
    completion = client.chat.completions.create(
        model="meta-llama/Llama-3.3-70B-Instruct",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=60,
    )
    messages.append(
        f"🔥 {a['title']}\n{a['old']} → {a['new']} RON\n{completion.choices[0].message.content}\n{a['url']}"
    )

json.dump(messages, open("data/formatted_alerts.json", "w"), ensure_ascii=False)
