# scripts/notify.py
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests
from pydantic import ValidationError

from bf_price_monitor.domain import AlertDecision
from bf_price_monitor.storage.sqlite import init_db, record_delivery_attempt_new_cycle

FORMATTED_FILE = Path("data/formatted_alerts.json")
PRICE_HISTORY_FILE = Path("data/price_history.json")
SCRAPE_HEALTH_ALERTS_FILE = Path("data/scrape_health_alerts.json")
WATCHLIST_FILE = Path("data/watchlist.json")
DLQ_FILE = Path("data/dlq.jsonl")
OUTBOX_FILE = Path("data/alert_outbox.jsonl")
# T-37b (#55): same file scrape.py's dual-write already persists to -- this
# is a second process/step in monitor.yml, so it opens its own connection.
DB_FILE = Path("data/price_history.db")

# T-13: cooldown window applied when a watch entry has no cooldown_hours of
# its own.
DEFAULT_COOLDOWN_HOURS = 24

# T-12: a PENDING outbox record older than this is assumed to belong to a
# writer process that has already died (crash, kill, restart) rather than
# the run currently in progress, and is safe to replay.
REPLAY_GRACE_SECONDS = 300

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

# T-25 (#33): deal_stats fake_discount_reasons codes -> display text.
FAKE_DISCOUNT_REASON_TEXT = {
    "ADVERTISED_ORIGINAL_INFLATED": "preț tăiat umflat față de mediana pe 30 zile",
    "OBSERVED_PRE_SALE_HIKE": "preț majorat chiar înainte de reducere",
}


def _omnibus_detail_lines(deal_stats: dict) -> list[str]:
    lines = []
    savings = deal_stats.get("genuine_savings_percent")
    if savings is not None:
        suffix = "" if savings > 0 else " (legal, nu este o reducere)"
        lines.append(
            f"\U0001f9ee <b>Economie reală vs. minim 30 zile:</b> {savings:.2f}%{suffix}"
        )
    if deal_stats.get("fake_discount_suspect"):
        reasons = "; ".join(
            FAKE_DISCOUNT_REASON_TEXT.get(r, r)
            for r in deal_stats.get("fake_discount_reasons", [])
        )
        lines.append(
            f"\U0001f6a9 <b>SUSPICIUNE REDUCERE FALSĂ:</b> {html.escape(reasons)}"
        )
    return lines


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
        *_omnibus_detail_lines(alert.get("deal_stats") or {}),
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


# A urllib3/requests connection error stringifies the failing URL verbatim,
# and our Telegram URLs carry the bot token in the path (Telegram's own API
# shape, not ours to change) -- redact known secret shapes before any such
# string reaches a log line or the on-disk DLQ.
_SECRET_PATTERNS = [
    re.compile(r"bot[0-9]{5,16}:[A-Za-z0-9_-]{34,36}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    ),
]


def _redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


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
            reason = _redact_secrets(f"{e.__class__.__name__}: {e}")
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


def _health_event_id(message: str) -> str:
    # Health alerts have no persisted decision to key off of, so the event
    # id is a content hash: the same alert text replayed after a crash
    # reuses the same id instead of minting a new PENDING record each time.
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _append_outbox_record(record: dict) -> None:
    OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTBOX_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_outbox_record(
    event_id: str,
    source: str,
    alert_payload: dict,
    send_payload: dict,
    status: str,
    created_at_utc: str,
    attempt_count: int,
    site: str,
    error_log: str | None = None,
    dedup_key: str | None = None,
) -> dict:
    """Appends one status record for event_id and returns it. The outbox is append-only:
    the effective status of an event is whichever record for its event_id
    was written last (see _load_outbox_effective). site is the store label
    (e.g. "emag", "scrape_health") used for the DLQ/log label if a replay
    of this event later fails — stored top-level since alert_payload for
    deal alerts is an AlertDecision dump with no site field of its own.
    dedup_key (T-13) is the "same offer" identity used for cooldown-window
    suppression; None for health alerts and for any record written before
    T-13, which can never match a later dedup_key lookup."""
    record = {
        "event_id": event_id,
        "source": source,
        "alert_payload": alert_payload,
        "send_payload": send_payload,
        "channel": "telegram",
        "status": status,
        "created_at_utc": created_at_utc,
        "last_attempt_at_utc": None
        if status == "PENDING"
        else datetime.now(UTC).isoformat(),
        "attempt_count": attempt_count,
        "site": site,
        "error_log": error_log,
        "dedup_key": dedup_key,
    }
    _append_outbox_record(record)
    return record


def _load_outbox_effective() -> dict[str, dict]:
    if not OUTBOX_FILE.exists():
        return {}
    effective: dict[str, dict] = {}
    with open(OUTBOX_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            effective[record["event_id"]] = record  # later lines win
    return effective


def _replay_pending_outbox(
    send_message_url: str, chat_id: str, db: sqlite3.Connection | None = None
) -> dict[str, dict]:
    """Resends any PENDING event older than REPLAY_GRACE_SECONDS using the
    payload captured when it was first queued, then records the terminal
    status. Returns the (now up-to-date) effective-status map so the caller
    can dedup the rest of this run against it without re-reading the file.
    db is optional (T-37b, #55): when given, the terminal outcome is also
    audited into SQLite; existing callers that only need the JSONL replay
    keep working unchanged."""
    effective = _load_outbox_effective()
    now = datetime.now(UTC)

    for event_id, record in effective.items():
        if record["status"] != "PENDING":
            continue
        created_at = datetime.fromisoformat(record["created_at_utc"])
        if now - created_at < timedelta(seconds=REPLAY_GRACE_SECONDS):
            continue  # likely still in flight from the current run

        alert_payload = record["alert_payload"]
        store = record.get("site") or "deal"
        if record["source"] == "health":
            alert_url = ""
        else:
            evidence_urls = alert_payload.get("evidence_urls") or []
            alert_url = evidence_urls[0] if evidence_urls else ""

        ok = _send_with_retry(
            send_message_url,
            {"chat_id": chat_id, **record["send_payload"]},
            alert=alert_payload,
            store=store,
            alert_url=alert_url,
        )
        status = "SENT" if ok else "DEAD_LETTER"
        # T-41: keep the record actually written (with its stamped
        # last_attempt_at_utc) rather than the PENDING copy, whose None
        # timestamp would crash the cooldown check now that replayed deal
        # records share their dedup_key with the live alert loop.
        effective[event_id] = _write_outbox_record(
            event_id,
            record["source"],
            record["alert_payload"],
            record["send_payload"],
            status,
            record["created_at_utc"],
            record.get("attempt_count", 0) + 1,
            store,
            None if ok else "replay send failed after retries",
            dedup_key=record.get("dedup_key"),
        )
        if db is not None:
            _record_delivery_attempt(
                db,
                event_id=event_id,
                dedup_key=record.get("dedup_key"),
                chat_id=chat_id,
                sent=ok,
            )
        print(f"REPLAYED: event {event_id} -> {status}", file=sys.stderr)

    return effective


def _send_health_alerts(
    send_message_url: str,
    chat_id: str,
    outbox_effective: dict[str, dict],
    db: sqlite3.Connection,
) -> None:
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
        event_id = _health_event_id(message)
        effective_record = outbox_effective.get(event_id)
        if effective_record and effective_record["status"] == "SENT":
            print(f"DUPLICATE SKIP: event {event_id}", file=sys.stderr)
            continue

        alert_payload = {"text": message}
        send_payload = {"text": message}
        created_at = datetime.now(UTC).isoformat()
        _write_outbox_record(
            event_id,
            "health",
            alert_payload,
            send_payload,
            "PENDING",
            created_at,
            0,
            "scrape_health",
        )
        ok = _send_with_retry(
            send_message_url,
            {"chat_id": chat_id, **send_payload},
            alert=alert_payload,
            store="scrape_health",
            alert_url="",
        )
        status = "SENT" if ok else "DEAD_LETTER"
        _write_outbox_record(
            event_id,
            "health",
            alert_payload,
            send_payload,
            status,
            created_at,
            1,
            "scrape_health",
            None if ok else "delivery failed after retries",
        )
        _record_delivery_attempt(
            db,
            event_id=event_id,
            dedup_key=None,
            chat_id=chat_id,
            sent=ok,
        )
        time.sleep(1.1)  # Telegram allows ~1 message/second per chat


def _deal_dedup_key(alert: dict) -> str:
    # T-13: "same offer" identity (url+price+site), used for cooldown-window
    # suppression. Since T-41 it is also the basis of the deal's outbox
    # event_id (see _deal_event_id), so a genuine price change yields a new
    # event instead of colliding with the URL's first-ever alert.
    raw = f"{alert['url']}:{alert['new_price']}:{alert['site']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _deal_event_id(dedup_key: str) -> str:
    # T-41: derived from the price-aware dedup_key, not the URL alone — a
    # URL-only id plus a permanent already-SENT skip meant each URL could
    # alert exactly once, ever.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"alert-decision:{dedup_key}"))


def _load_cooldown_by_site() -> dict[str, float]:
    """Maps site -> the most conservative (minimum) cooldown_hours among
    that site's watchlist entries. Reads the raw watchlist dict directly
    rather than a validated Watch model, since the modern Watch-model
    schema isn't the format in production use. Missing file or missing
    cooldown_hours on an entry simply leaves that site unconstrained here;
    the caller falls back to DEFAULT_COOLDOWN_HOURS."""
    if not WATCHLIST_FILE.exists():
        return {}
    watchlist = json.load(open(WATCHLIST_FILE, encoding="utf-8"))
    if isinstance(watchlist, dict):
        watchlist = watchlist.get("watches", [])
    by_site: dict[str, float] = {}
    for entry in watchlist:
        cooldown_hours = entry.get("cooldown_hours")
        site = entry.get("site")
        if cooldown_hours is None or site is None:
            continue
        if site not in by_site or cooldown_hours < by_site[site]:
            by_site[site] = cooldown_hours
    return by_site


def _find_cooldown_block(
    dedup_key: str,
    cooldown_hours: float,
    outbox_effective: dict[str, dict],
    now: datetime,
) -> dict | None:
    """Returns the blocking SENT record if dedup_key was sent within
    cooldown_hours of now, else None. Records without a dedup_key (None)
    never match, since dedup_key is always a non-empty hash — this is how
    pre-T-13 outbox records are guaranteed to never suppress anything."""
    for record in outbox_effective.values():
        if record.get("dedup_key") != dedup_key:
            continue
        if record["status"] != "SENT":
            continue
        sent_at = datetime.fromisoformat(record["last_attempt_at_utc"])
        if now - sent_at < timedelta(hours=cooldown_hours):
            return record
    return None


def _destination_ref(chat_id: str) -> str:
    # T-37b (#55): delivery_attempts is an audit table; never store the raw
    # Telegram chat id in it, only a stable non-reversible reference.
    return hashlib.sha256(f"telegram:{chat_id}".encode()).hexdigest()


def _decision_id_from_event(event_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(event_id)
    except ValueError:
        # Health event ids are content-hash hex strings (_health_event_id),
        # not UUIDs, but DeliveryAttempt.alert_decision_id is typed UUID --
        # derive one deterministically so replays stay idempotent.
        return uuid.uuid5(uuid.NAMESPACE_URL, event_id)


def _record_delivery_attempt(
    db: sqlite3.Connection,
    *,
    event_id: str,
    dedup_key: str | None,
    chat_id: str,
    sent: bool,
) -> None:
    """Audits one terminal outbox finalization into SQLite as a new delivery
    cycle. alert_decision_id may already carry prior audit rows -- this deal
    re-alerting once its cooldown expires, or a health alert retried after
    an earlier failure -- so attempt_number is allocated atomically at the
    storage-write boundary (record_delivery_attempt_new_cycle), never
    computed here: a separate SELECT MAX before this call would leave a
    window where a concurrent run could allocate the same number. This is
    additive: alert_outbox.jsonl remains the operational source of truth for
    PENDING/replay/cooldown behavior, which this never touches. response_class
    is coarse ("2xx"/"unknown") since per-HTTP-retry classification inside
    _send_with_retry isn't surfaced to these call sites (deferred)."""
    record_delivery_attempt_new_cycle(
        db,
        {
            "alert_decision_id": _decision_id_from_event(event_id),
            "channel": "telegram",
            "destination": _destination_ref(chat_id),
            "response_class": "2xx" if sent else "unknown",
            "dedup_key": dedup_key,
            "final_state": "delivered" if sent else "failed",
        },
    )


def main():
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    send_message_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    send_photo_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"

    # T-37b (#55): notify.py runs as its own step/process (see DB_FILE), so
    # it opens its own connection rather than reusing scrape.py's.
    db = init_db(DB_FILE)
    try:
        _run(chat_id, send_message_url, send_photo_url, db)
    finally:
        db.close()


def _run(
    chat_id: str,
    send_message_url: str,
    send_photo_url: str,
    db: sqlite3.Connection,
) -> None:
    # T-12: replay anything left PENDING by a crashed prior run before this
    # run queues anything new, so the dedup check below sees an up-to-date
    # effective status for every event_id.
    outbox_effective = _replay_pending_outbox(send_message_url, chat_id, db)

    _send_health_alerts(send_message_url, chat_id, outbox_effective, db)

    alerts = json.load(open(FORMATTED_FILE, encoding="utf-8"))
    if not alerts:
        print("No alerts to send")
        return

    price_history = json.load(open(PRICE_HISTORY_FILE, encoding="utf-8"))
    products = price_history.get("products", {})
    cooldown_by_site = _load_cooldown_by_site()
    seen_this_run: set[str] = set()

    for alert in alerts:
        dedup_key = _deal_dedup_key(alert)
        # T-41: the same offer can reach this loop more than once per run
        # (e.g. matched by two watches); one delivery attempt per run is
        # enough regardless of how that attempt ended.
        if dedup_key in seen_this_run:
            print(f"IN-RUN DUPLICATE SKIP: dedup_key={dedup_key}", file=sys.stderr)
            continue
        seen_this_run.add(dedup_key)

        # Structure-only validation (T-01): every alert reaching this point
        # already passed should_alert()'s filter in scrape.py, so verdict is
        # always "alert" here — this confirms the payload matches the
        # AlertDecision domain model before it goes out, without changing
        # what gets sent. watch_id/observation_id are deterministic
        # derivations, not stored fields, since formatted_alerts.json has
        # neither.
        try:
            decision = AlertDecision.model_validate(
                {
                    # T-12: deterministic id (not the model's random uuid4
                    # default) so the same alert produces the same outbox
                    # event_id across separate runs — required for replay
                    # and dedup to actually match up after a restart.
                    "id": _deal_event_id(dedup_key),
                    "policy_version": "omnibus-v1",
                    "watch_id": uuid.uuid5(
                        uuid.NAMESPACE_URL, f"{alert['site']}:{alert['query']}"
                    ),
                    "observation_id": uuid.uuid5(uuid.NAMESPACE_URL, alert["url"]),
                    "verdict": "alert",
                    # T-25 (#33): fake discount is its own reason, kept
                    # next to (never replacing) the rule engine's verdict.
                    "reasons": [alert.get("rule_verdict", "unknown")]
                    + (
                        ["FAKE_DISCOUNT_SUSPECT"]
                        if (alert.get("deal_stats") or {}).get("fake_discount_suspect")
                        else []
                    ),
                    "evidence_urls": [alert["url"]],
                }
            )
        except ValidationError as e:
            print(
                f"AlertDecision validation failed for {alert.get('url')}: {e}",
                file=sys.stderr,
            )
            raise

        event_id = str(decision.id)
        effective_record = outbox_effective.get(event_id)
        if effective_record and effective_record["status"] == "PENDING":
            # Replay already resolved every PENDING past the grace window,
            # so one still here is in flight from a concurrent run.
            print(f"IN-FLIGHT SKIP: event {event_id}", file=sys.stderr)
            continue

        # T-13: same-offer cooldown. Since T-41 this is the only cross-run
        # suppression for deals: SENT blocks only within the window, and
        # DEAD_LETTER is retried.
        cooldown_hours = cooldown_by_site.get(alert["site"], DEFAULT_COOLDOWN_HOURS)
        blocking_record = _find_cooldown_block(
            dedup_key, cooldown_hours, outbox_effective, datetime.now(UTC)
        )
        if blocking_record:
            print(
                f"COOLDOWN SKIP: dedup_key={dedup_key} "
                f"last_sent={blocking_record['last_attempt_at_utc']} "
                f"cooldown={cooldown_hours}h",
                file=sys.stderr,
            )
            continue

        message = format_telegram_message(alert)
        keyboard = build_inline_keyboard(alert)
        product_history = products.get(alert["url"], {}).get("history", [])
        chart_url = generate_quickchart_url(
            product_history, alert["title"], alert.get("verdict")
        )

        # T-12/T-41: PENDING is written before either send attempt (photo or
        # text) so a crash mid-send still leaves evidence this event was in
        # flight; send_payload is the text message a later replay resends
        # verbatim via sendMessage, since alert_payload (the AlertDecision
        # dump) alone has no title/price/summary to reconstruct it from.
        send_payload = {
            "text": message,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        }
        alert_payload = decision.model_dump(mode="json")
        created_at = datetime.now(UTC).isoformat()
        outbox_effective[event_id] = _write_outbox_record(
            event_id,
            "deal",
            alert_payload,
            send_payload,
            "PENDING",
            created_at,
            0,
            alert["site"],
            dedup_key=dedup_key,
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
            sent = _send_with_retry(
                send_message_url,
                {"chat_id": chat_id, **send_payload},
                alert=alert,
                store=alert["site"],
                alert_url=alert["url"],
            )

        status = "SENT" if sent else "DEAD_LETTER"
        outbox_effective[event_id] = _write_outbox_record(
            event_id,
            "deal",
            alert_payload,
            send_payload,
            status,
            created_at,
            1,
            alert["site"],
            None if sent else "delivery failed after retries",
            dedup_key=dedup_key,
        )
        _record_delivery_attempt(
            db,
            event_id=event_id,
            dedup_key=dedup_key,
            chat_id=chat_id,
            sent=sent,
        )

        time.sleep(1.1)  # Telegram allows ~1 message/second per chat


if __name__ == "__main__":
    main()
