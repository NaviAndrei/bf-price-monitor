# scripts/analyze.py
import json
import os
from pathlib import Path

import requests
from huggingface_hub import InferenceClient

ALERTS_FILE = Path("data/alerts.json")
FORMATTED_FILE = Path("data/formatted_alerts.json")

HF_MODEL = "meta-llama/Llama-3.3-70B-Instruct"
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3:8b"
TELEGRAM_MESSAGE_LIMIT = 4096


def resolve_reference(alert: dict) -> tuple[float | None, str]:
    # Flanco's OUG 27/2022-mandated reference_price is legally audited, so it
    # takes priority over our own scrape-derived thirty_day_low when present.
    if alert.get("reference_price") is not None:
        return alert["reference_price"], "legally verified"
    return alert.get("thirty_day_low"), "observed"


def build_prompt(alert: dict, reference_low: float | None, comparison_method: str) -> str:
    stock_status = alert.get("stock_status", "unknown")
    return (
        f"Product: {alert['title']}. Old price: {alert['old_price']} RON. "
        f"New price: {alert['new_price']} RON. {comparison_method.capitalize()} 30-day low: "
        f"{reference_low} RON. Current stock status: {stock_status}. "
        "Is this a genuine Black Friday discount or a fake price hike? Consider that a price "
        "drop on an out-of-stock item is more likely a stale or manipulated listing than a real "
        "offer. One sentence."
    )


def ask_hf(client: InferenceClient, prompt: str) -> str:
    completion = client.chat.completions.create(
        model=HF_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=60,
    )
    return completion.choices[0].message.content


def ask_ollama(prompt: str) -> str:
    r = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False},
        timeout=180,  # qwen3:8b is a "thinking" model; ~90s to reason before answering on this hardware
    )
    r.raise_for_status()
    return r.json()["message"]["content"]


def get_verdict(client: InferenceClient, prompt: str) -> str:
    try:
        return ask_hf(client, prompt)
    except Exception as e:
        print(f"HF inference failed ({e.__class__.__name__}), falling back to Ollama")
    try:
        return ask_ollama(prompt)
    except Exception as e:
        print(f"Ollama fallback also failed ({e.__class__.__name__}), skipping verdict")
        return "(no AI verdict available)"


def chunk_message(header: str, lines: list[str]) -> list[str]:
    # Telegram caps a single message at 4096 characters; a group covering a
    # broad query (60+ matched cards) can exceed that, so split on line
    # boundaries rather than truncating.
    chunks = []
    current = header
    for line in lines:
        if len(current) + len(line) > TELEGRAM_MESSAGE_LIMIT:
            chunks.append(current)
            current = header
        current += line
    chunks.append(current)
    return chunks


def main():
    alerts = json.load(open(ALERTS_FILE, encoding="utf-8"))
    if not alerts:
        json.dump([], open(FORMATTED_FILE, "w", encoding="utf-8"))
        print("No alerts to analyze")
        return

    client = InferenceClient(api_key=os.environ.get("HF_TOKEN", ""))

    # Group by watchlist entry (site + query) so one Telegram message covers
    # one search rather than one message per matched product.
    groups: dict[tuple[str, str], list[dict]] = {}
    for a in alerts:
        groups.setdefault((a["site"], a.get("query", "")), []).append(a)

    messages = []
    for (site, query), group_alerts in groups.items():
        header = f'🔥 {site.upper()} — "{query}"\n'
        lines = []
        for a in group_alerts:
            reference_low, comparison_method = resolve_reference(a)
            prompt = build_prompt(a, reference_low, comparison_method)
            verdict = get_verdict(client, prompt)
            lines.append(
                f"\n{a['title']}\n{a['old_price']} → {a['new_price']} RON "
                f"(vs. {comparison_method} 30-day low: {reference_low} RON)\n{verdict}\n{a['url']}\n"
            )
        messages.extend(chunk_message(header, lines))

    json.dump(messages, open(FORMATTED_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"Analyzed {len(alerts)} alert(s) into {len(messages)} message(s)")


if __name__ == "__main__":
    main()
