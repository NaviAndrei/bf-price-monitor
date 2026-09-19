# scripts/notify.py
import html
import json
import os
import random
import sys
import time
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path

import requests
from pydantic import ValidationError

from bf_price_monitor.domain import AlertDecision

FORMATTED_FILE = Path("data/formatted_alerts.json")
PRICE_HISTORY_FILE = Path("data/price_history.json")
SCRAPE_HEALTH_ALERTS_FILE = Path("data/scrape_health_alerts.json")
DLQ_FILE = Path("data/dlq.jsonl")

# T-11: bounded retry schedule. 1 initial attempt + 3 retries, capped
# exponential backoff with jitter so concurrent failures don't retry in
# lockstep. Telegram's 429 retry_after is honored but capped so a large
# server-supplied value can't stall a run past the batch.
MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2
MAX_ATTEMPT_SLEEP_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 60.0

VERDICT_BADGES = {
    "GENUINE_DEAL": ("\U0001f7e2", "OFERTĂ REALĂ"),
    "FALSE_DISCOUNT": ("\U0001f534", "REDUCERE FALSĂ"),
    "INFLATED_REFERENCE": ("\U0001f7e1", "PREȚ DE REFERINȚĂ UMFLAT"),
    "NORMAL_DROP": ("\U0001f535", "SCĂDERE DE PREȚ"),
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
        f"\U0001f3ec Magazin: {site.upper()} | Vânzător: {seller}",
        f"\U0001f4e6 Stoc: {stock_status}",
        "",
        f"\U0001f4b0 <b>Preț Nou:</b> {alert['new_price']:,.2f} RON",
        f"\U0001f4c9 <b>Preț Anterior:</b> {alert['old_price']:,.2f} RON (-{alert['discount_vs_old_pct']}%)",
        f"⚖️ <b>Minim 30 zile (Omnibus):</b> {alert['thirty_day_low']:,.2f} RON",
        f"\U0001f3c6 <b>Record Minim Istoric:</b> {alert['all_time_low']:,.2f} RON",
        "",
        f"\U0001f4a1 <i>{summary}</i>",
    ]
    if alert.get("is_recommended"):
        lines.append("✨ <b>Recomandat pentru cumpărare!</b>")
    return "\n".join(lines)


def build_inline_keyboard(alert: dict) -> dict:
    # First 4 words keeps Compari search queries clean — a full title
    # (with RAM/storage specs) over-narrows the search and returns nothing.
    cleaned_title = " ".join(alert["title"].split()[:4])
    compari_url = (
        "https://www.compari.ro/CategorySearch.php?st="
        f"{urllib.parse.quote_plus(cleaned_title)}"
    )
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"\U0001f6d2 Deschide Oferta ({alert['site'].upper()})",
                    "url": alert["url"],
                },
                {"text": "\U0001f4ca Compară pe Compari.ro", "url": compari_url},
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


def _extract_retry_after(response: requests.Response) -> float | None:
    header_value = response.headers.get("Retry-After")
    if header_value is not None:
        try:
            return float(header_value)
        except ValueError:
            pass
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("parameters", {}).get("retry_after")


def _sleep_before_retry(attempt: int, retry_after: float | None) -> None:
    if retry_after is not None:
        delay = min(retry_after, MAX_RETRY_AFTER_SECONDS)
    else:
        delay = min(
            MAX_ATTEMPT_SLEEP_SECONDS,
            BASE_DELAY_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1))
            + random.uniform(0, 1),
        )
    time.sleep(delay)


def _response_failure_reason(response: requests.Response) -> str:
    try:
        return response.json().get("description", response.text[:200])
    except ValueError:
        return response.text[:200]


def _write_dlq_record(
    alert: dict | None,
    store: str,
    url: str,
    attempt_count: int,
    final_status_code: int | str,
    failure_reason: str,
) -> None:
    record = {
        "alert_id": str((alert or {}).get("alert_id") or uuid.uuid4()),
        "store": store,
        "url": url,
        "attempted_at_utc": datetime.now(UTC).isoformat(),
        "attempt_count": attempt_count,
        "final_status_code": final_status_code,
        "failure_reason": failure_reason,
        "alert_payload": alert,
    }
    DLQ_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DLQ_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(
        f"DEAD-LETTER: alert for {store} {url} after {attempt_count} attempts: "
        f"{failure_reason}",
        file=sys.stderr,
    )


def _send_with_retry(
    url: str, payload: dict, *, alert: dict | None, store: str, alert_url: str
) -> bool:
    """Bounded, classified send: retries 429/5xx/network errors with capped
    backoff; permanent 4xx is not retried. Any exhausted or permanent
    failure is appended to the dead-letter queue rather than dropped."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = requests.post(url, json=payload, timeout=15)
        except requests.RequestException as e:
            reason = f"{e.__class__.__name__}: {e}"
            if attempt < MAX_ATTEMPTS:
                _sleep_before_retry(attempt, None)
                continue
            _write_dlq_record(alert, store, alert_url, attempt, "network_error", reason)
            return False

        if r.status_code == 200:
            return True

        reason = _response_failure_reason(r)

        if r.status_code == 429:
            if attempt < MAX_ATTEMPTS:
                _sleep_before_retry(attempt, _extract_retry_after(r))
                continue
            _write_dlq_record(alert, store, alert_url, attempt, r.status_code, reason)
            return False

        if r.status_code >= 500:
            if attempt < MAX_ATTEMPTS:
                _sleep_before_retry(attempt, None)
                continue
            _write_dlq_record(alert, store, alert_url, attempt, r.status_code, reason)
            return False

        # Permanent 4xx (400, 401, 403, 404, ...): not retryable.
        _write_dlq_record(alert, store, alert_url, attempt, r.status_code, reason)
        return False

    return False  # pragma: no cover - loop always returns internally


def _send_health_alerts(send_message_url: str, chat_id: str) -> None:
    # T-09: per-store health alerts (Critical Selector Drift, dead-man
    # checks) are written by scrape.py to a handoff file rather than sent
    # directly, since TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are only injected
    # into this step of monitor.yml, not the scrape step. Sent here, ahead of
    # the deal-alert early-return below, so a run with zero price-drop deals
    # still delivers a pending health alert.
    if not SCRAPE_HEALTH_ALERTS_FILE.exists():
        return
    health_alerts = json.load(open(SCRAPE_HEALTH_ALERTS_FILE, encoding="utf-8"))
    for message in health_alerts:
        _send_with_retry(
            send_message_url,
            {"chat_id": chat_id, "text": message},
            alert={"text": message},
            store="scrape_health",
            alert_url="",
        )
        time.sleep(1.1)  # Telegram allows ~1 message/second per chat


def main():
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    send_message_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    send_photo_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"

    _send_health_alerts(send_message_url, chat_id)

    alerts = json.load(open(FORMATTED_FILE, encoding="utf-8"))
    if not alerts:
        print("No alerts to send")
        return

    price_history = json.load(open(PRICE_HISTORY_FILE, encoding="utf-8"))
    products = price_history.get("products", {})

    for alert in alerts:
        # Structure-only validation (T-01): every alert reaching this point
        # already passed should_alert()'s filter in scrape.py, so verdict is
        # always "alert" here — this confirms the payload matches the
        # AlertDecision domain model before it goes out, without changing
        # what gets sent. watch_id/observation_id are deterministic
        # derivations, not stored fields, since formatted_alerts.json has
        # neither.
        try:
            AlertDecision.model_validate(
                {
                    "policy_version": "omnibus-v1",
                    "watch_id": uuid.uuid5(
                        uuid.NAMESPACE_URL, f"{alert['site']}:{alert['query']}"
                    ),
                    "observation_id": uuid.uuid5(uuid.NAMESPACE_URL, alert["url"]),
                    "verdict": "alert",
                    "reasons": [alert.get("rule_verdict", "unknown")],
                    "evidence_urls": [alert["url"]],
                }
            )
        except ValidationError as e:
            print(
                f"AlertDecision validation failed for {alert.get('url')}: {e}",
                file=sys.stderr,
            )
            raise

        message = format_telegram_message(alert)
        keyboard = build_inline_keyboard(alert)
        product_history = products.get(alert["url"], {}).get("history", [])
        chart_url = generate_quickchart_url(
            product_history, alert["title"], alert.get("verdict")
        )

        # The chart is a best-effort enhancement, not a distinct delivery: a
        # single failed attempt falls straight back to the text message
        # rather than consuming retry/DLQ accounting of its own.
        sent = False
        if chart_url and len(message) <= CAPTION_LIMIT:
            try:
                r = requests.post(
                    send_photo_url,
                    json={
                        "chat_id": chat_id,
                        "photo": chart_url,
                        "caption": message,
                        "parse_mode": "HTML",
                        "reply_markup": keyboard,
                    },
                    timeout=15,
                )
                r.raise_for_status()
                sent = True
            except requests.RequestException as e:
                print(
                    f"sendPhoto failed ({e.__class__.__name__}), falling back to sendMessage"
                )

        if not sent:
            _send_with_retry(
                send_message_url,
                {
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
                alert=alert,
                store=alert["site"],
                alert_url=alert["url"],
            )

        time.sleep(1.1)  # Telegram allows ~1 message/second per chat


if __name__ == "__main__":
    main()
