# scripts/notify.py
import html
import json
import os
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

FORMATTED_FILE = Path("data/formatted_alerts.json")
PRICE_HISTORY_FILE = Path("data/price_history.json")

VERDICT_BADGES = {
    "GENUINE_DEAL": ("\U0001F7E2", "OFERTĂ REALĂ"),
    "FALSE_DISCOUNT": ("\U0001F534", "REDUCERE FALSĂ"),
    "INFLATED_REFERENCE": ("\U0001F7E1", "PREȚ DE REFERINȚĂ UMFLAT"),
    "NORMAL_DROP": ("\U0001F535", "SCĂDERE DE PREȚ"),
    "INSUFFICIENT_HISTORY": ("⚪", "ISTORIC NOU / INSUFICIENT"),
}

# Telegram caption limit for sendPhoto is 1024 chars; sendMessage allows far
# more, so a message that would be truncated as a caption falls back to a
# plain text message instead of the chart.
CAPTION_LIMIT = 1024


def format_telegram_message(alert: dict) -> str:
    emoji, label = VERDICT_BADGES.get(
        alert.get("verdict"), ("⚪", "VERDICT NECUNOSCUT")
    )
    title = html.escape(alert["title"])
    site = html.escape(alert["site"])
    seller = html.escape(alert.get("seller") or "Neverificat")
    stock_status = html.escape(alert.get("stock_status", "unknown"))
    summary = html.escape(alert.get("summary", ""))
    verdict_score = alert.get("verdict_score", "?")

    lines = [
        f"{emoji} <b>{label}</b> (Scor: {verdict_score}/10)",
        f"<b>{title}</b>",
        f"\U0001F3EC Magazin: {site.upper()} | Vânzător: {seller}",
        f"\U0001F4E6 Stoc: {stock_status}",
        "",
        f"\U0001F4B0 <b>Preț Nou:</b> {alert['new_price']:,.2f} RON",
        f"\U0001F4C9 <b>Preț Anterior:</b> {alert['old_price']:,.2f} RON (-{alert['discount_vs_old_pct']}%)",
        f"⚖️ <b>Minim 30 zile (Omnibus):</b> {alert['thirty_day_low']:,.2f} RON",
        f"\U0001F3C6 <b>Record Minim Istoric:</b> {alert['all_time_low']:,.2f} RON",
        "",
        f"\U0001F4A1 <i>{summary}</i>",
    ]
    if alert.get("is_recommended"):
        lines.append("✨ <b>Recomandat pentru cumpărare!</b>")
    return "\n".join(lines)


def build_inline_keyboard(alert: dict) -> dict:
    cleaned_title = " ".join(alert["title"].split()[:5])
    compari_url = (
        "https://www.compari.ro/CategorySearch.php?st="
        f"{urllib.parse.quote_plus(cleaned_title)}"
    )
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"\U0001F6D2 Deschide Oferta ({alert['site'].upper()})",
                    "url": alert["url"],
                },
                {"text": "\U0001F4CA Compară pe Compari.ro", "url": compari_url},
            ]
        ]
    }


def generate_quickchart_url(
    history: list[dict], title: str, verdict: str | None = None
) -> str | None:
    if len(history) < 3:
        return None

    labels = [datetime.fromisoformat(h["date"]).strftime("%d.%m") for h in history]
    prices = [h["price"] for h in history]
    color = "#28a745" if verdict == "GENUINE_DEAL" else "#007bff"

    chart_config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": title[:40],
                    "data": prices,
                    "borderColor": color,
                    "backgroundColor": color,
                    "fill": False,
                    "tension": 0.2,
                    "pointRadius": 2,
                }
            ],
        },
        "options": {
            "plugins": {"legend": {"display": False}},
            "scales": {
                "y": {
                    "title": {"display": True, "text": "Preț (RON)"},
                    "grid": {"color": "#eeeeee"},
                },
                "x": {"grid": {"display": False}},
            },
        },
    }
    return (
        "https://quickchart.io/chart?w=500&h=260&devicePixelRatio=2.0&c="
        f"{urllib.parse.quote(json.dumps(chart_config))}"
    )


def _post_with_retry(url: str, payload: dict) -> requests.Response:
    while True:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 429:
            retry_after = r.json().get("parameters", {}).get("retry_after", 5)
            print(f"Rate limited by Telegram, waiting {retry_after}s before retrying")
            time.sleep(retry_after)
            continue
        return r


def main():
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    send_message_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    send_photo_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"

    alerts = json.load(open(FORMATTED_FILE, encoding="utf-8"))
    if not alerts:
        print("No alerts to send")
        return

    price_history = json.load(open(PRICE_HISTORY_FILE, encoding="utf-8"))
    products = price_history.get("products", {})

    for alert in alerts:
        message = format_telegram_message(alert)
        keyboard = build_inline_keyboard(alert)
        product_history = products.get(alert["url"], {}).get("history", [])
        chart_url = generate_quickchart_url(
            product_history, alert["title"], alert.get("verdict")
        )

        sent = False
        if chart_url and len(message) <= CAPTION_LIMIT:
            try:
                r = _post_with_retry(
                    send_photo_url,
                    {
                        "chat_id": chat_id,
                        "photo": chart_url,
                        "caption": message,
                        "parse_mode": "HTML",
                        "reply_markup": keyboard,
                    },
                )
                r.raise_for_status()
                sent = True
            except requests.RequestException as e:
                print(f"sendPhoto failed ({e.__class__.__name__}), falling back to sendMessage")

        if not sent:
            _post_with_retry(
                send_message_url,
                {
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
            )

        time.sleep(1.1)  # Telegram allows ~1 message/second per chat


if __name__ == "__main__":
    main()
