import hashlib
import json
import uuid

import notify
import pytest
import requests
from notify import (
    build_inline_keyboard,
    format_telegram_message,
    generate_quickchart_url,
)

from bf_price_monitor.domain import DeliveryAttempt
from bf_price_monitor.storage.sqlite import init_db, record_delivery_attempt

BASE_ALERT = {
    "title": "Laptop Lenovo V15 G4 AMN AMD Ryzen 5 7520U 16GB 512GB SSD",
    "site": "emag",
    "url": "https://www.emag.ro/some-product/pd/ABC123/",
    "seller": "eMAG",
    "stock_status": "in_stock",
    "old_price": 100.0,
    "new_price": 80.0,
    "thirty_day_low": 90.0,
    "all_time_low": 75.0,
    "discount_vs_old_pct": 20.0,
    "discount_vs_30d_pct": 11.11,
    "verdict": "GENUINE_DEAL",
    "verdict_score": 9,
    "summary": "Merită cumpărat <acum>.",
    "is_recommended": True,
}


def test_format_telegram_message_renders_html_without_escaping_syntax_errors():
    message = format_telegram_message(BASE_ALERT)
    assert "<b>OFERTĂ REALĂ</b>" in message
    assert "<b>Laptop Lenovo" in message
    # The summary's raw "<acum>" must be escaped, not left as a stray tag.
    assert "&lt;acum&gt;" in message
    assert "<acum>" not in message


def test_format_telegram_message_handles_missing_seller_gracefully():
    alert = {**BASE_ALERT, "seller": None}
    message = format_telegram_message(alert)
    assert "Vânzător: Neverificat" in message


def test_format_telegram_message_all_verdict_badges():
    expected = {
        "GENUINE_DEAL": "OFERTĂ REALĂ",
        "FALSE_DISCOUNT": "REDUCERE FALSĂ",
        "INFLATED_REFERENCE": "PREȚ DE REFERINȚĂ UMFLAT",
        "NORMAL_DROP": "SCĂDERE DE PREȚ",
        "INSUFFICIENT_HISTORY": "ISTORIC NOU / INSUFICIENT",
    }
    for verdict, label in expected.items():
        alert = {**BASE_ALERT, "verdict": verdict}
        message = format_telegram_message(alert)
        assert f"<b>{label}</b>" in message


def test_build_inline_keyboard_produces_valid_urls():
    keyboard = build_inline_keyboard(BASE_ALERT)
    buttons = keyboard["inline_keyboard"][0]
    assert buttons[0]["url"] == BASE_ALERT["url"]
    assert buttons[0]["text"].startswith("\U0001f6d2")
    assert "EMAG" in buttons[0]["text"]

    compari_url = buttons[1]["url"]
    assert compari_url.startswith("https://www.compari.ro/CategorySearch.php?st=")
    assert "Laptop+Lenovo+V15+G4" in compari_url
    assert "AMN" not in compari_url


def test_generate_quickchart_url_returns_none_below_three_points():
    history = [
        {"date": "2026-09-01", "price": 100.0},
        {"date": "2026-09-02", "price": 95.0},
    ]
    assert generate_quickchart_url(history, "Some Product") is None


def test_generate_quickchart_url_returns_valid_url_for_three_or_more_points():
    history = [
        {"date": "2026-09-01", "price": 100.0},
        {"date": "2026-09-02", "price": 95.0},
        {"date": "2026-09-03", "price": 90.0},
    ]
    url = generate_quickchart_url(history, "Some Product", verdict="GENUINE_DEAL")
    assert url.startswith("https://quickchart.io/chart?")
    assert "c=" in url


# --- T-11: bounded retry + dead-letter queue --------------------------------


class _FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json_data = {} if json_data is None else json_data
        self.headers = {} if headers is None else headers
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        # Only the sendPhoto path calls this; _send_with_retry branches on
        # status_code directly.
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _sequenced_post(items):
    """Returns a fake requests.post that replays items (Response or
    Exception instances) in order, one per call."""
    calls = {"n": 0}

    def _post(url, json=None, timeout=None):
        item = items[calls["n"]]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    _post.call_count = lambda: calls["n"]
    return _post


@pytest.fixture(autouse=True)
def _isolate_dlq_and_sleep(monkeypatch, tmp_path):
    # T-11/T-12 tests must never write to the real data/dlq.jsonl or
    # data/alert_outbox.jsonl, and the backoff schedule (up to ~30s/attempt)
    # would make the suite crawl if time.sleep actually ran.
    monkeypatch.setattr(notify, "DLQ_FILE", tmp_path / "dlq.jsonl")
    monkeypatch.setattr(notify, "OUTBOX_FILE", tmp_path / "alert_outbox.jsonl")
    monkeypatch.setattr(notify.time, "sleep", lambda *_: None)


def _read_dlq_records():
    if not notify.DLQ_FILE.exists():
        return []
    lines = notify.DLQ_FILE.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


def _read_outbox_records():
    if not notify.OUTBOX_FILE.exists():
        return []
    lines = notify.OUTBOX_FILE.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


_OPTIONAL_CHANNEL_ENV_VARS = (
    "TEAMS_WEBHOOK_URL",
    "NTFY_TOPIC",
    "NTFY_SERVER",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "EMAIL_FROM",
    "EMAIL_TO",
)


@pytest.fixture
def _full_pipeline_env(monkeypatch, tmp_path):
    """Isolates the file paths and env vars notify.main() reads, so T-12
    tests can drive main() end-to-end without touching real data/ files."""
    monkeypatch.setattr(notify, "FORMATTED_FILE", tmp_path / "formatted_alerts.json")
    monkeypatch.setattr(notify, "PRICE_HISTORY_FILE", tmp_path / "price_history.json")
    monkeypatch.setattr(
        notify, "SCRAPE_HEALTH_ALERTS_FILE", tmp_path / "scrape_health_alerts.json"
    )
    monkeypatch.setattr(notify, "WATCHLIST_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(notify, "DB_FILE", tmp_path / "price_history.db")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "testtoken")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    # T-26 (#35): optional channels stay unconfigured unless a test opts in.
    for name in _OPTIONAL_CHANNEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    notify.PRICE_HISTORY_FILE.write_text(json.dumps({"products": {}}), encoding="utf-8")
    return tmp_path


DEAL_ALERT = {**BASE_ALERT, "query": "laptop lenovo v15"}


def test_send_with_retry_429_with_retry_after_header_retries_then_succeeds(
    monkeypatch,
):
    post = _sequenced_post(
        [
            _FakeResponse(429, headers={"Retry-After": "2"}),
            _FakeResponse(200),
        ]
    )
    monkeypatch.setattr(notify.requests, "post", post)

    ok = notify._send_with_retry(
        "https://api.telegram.org/botX/sendMessage",
        {"chat_id": "1", "text": "hi"},
        alert=None,
        store="emag",
        alert_url="https://emag.ro/x",
    )

    assert ok is True
    assert post.call_count() == 2
    assert _read_dlq_records() == []


def test_send_with_retry_429_without_retry_after_uses_backoff_then_succeeds(
    monkeypatch,
):
    post = _sequenced_post(
        [
            _FakeResponse(429),
            _FakeResponse(429),
            _FakeResponse(200),
        ]
    )
    monkeypatch.setattr(notify.requests, "post", post)

    ok = notify._send_with_retry(
        "https://api.telegram.org/botX/sendMessage",
        {"chat_id": "1", "text": "hi"},
        alert=None,
        store="emag",
        alert_url="https://emag.ro/x",
    )

    assert ok is True
    assert post.call_count() == 3


def test_send_with_retry_5xx_retries_up_to_max_attempts_then_dead_letters(
    monkeypatch,
):
    post = _sequenced_post([_FakeResponse(500, text="boom")] * notify.MAX_ATTEMPTS)
    monkeypatch.setattr(notify.requests, "post", post)

    ok = notify._send_with_retry(
        "https://api.telegram.org/botX/sendMessage",
        {"chat_id": "1", "text": "hi"},
        alert={"site": "pcgarage", "url": "https://pcgarage.ro/y"},
        store="pcgarage",
        alert_url="https://pcgarage.ro/y",
    )

    assert ok is False
    assert post.call_count() == notify.MAX_ATTEMPTS
    records = _read_dlq_records()
    assert len(records) == 1
    assert records[0]["attempt_count"] == notify.MAX_ATTEMPTS
    assert records[0]["final_status_code"] == 500


def test_send_with_retry_network_error_retries_then_dead_letters(monkeypatch):
    post = _sequenced_post([requests.ConnectionError("refused")] * notify.MAX_ATTEMPTS)
    monkeypatch.setattr(notify.requests, "post", post)

    ok = notify._send_with_retry(
        "https://api.telegram.org/botX/sendMessage",
        {"chat_id": "1", "text": "hi"},
        alert={"site": "flanco", "url": "https://flanco.ro/z"},
        store="flanco",
        alert_url="https://flanco.ro/z",
    )

    assert ok is False
    assert post.call_count() == notify.MAX_ATTEMPTS
    records = _read_dlq_records()
    assert len(records) == 1
    assert records[0]["final_status_code"] == "network_error"
    assert "ConnectionError" in records[0]["failure_reason"]


def test_send_with_retry_permanent_4xx_dead_letters_immediately(monkeypatch):
    post = _sequenced_post([_FakeResponse(403, text="Forbidden")])
    monkeypatch.setattr(notify.requests, "post", post)

    ok = notify._send_with_retry(
        "https://api.telegram.org/botX/sendMessage",
        {"chat_id": "1", "text": "hi"},
        alert={"site": "emag", "url": "https://emag.ro/x"},
        store="emag",
        alert_url="https://emag.ro/x",
    )

    assert ok is False
    assert post.call_count() == 1  # no retry
    records = _read_dlq_records()
    assert len(records) == 1
    assert records[0]["attempt_count"] == 1
    assert records[0]["final_status_code"] == 403


def test_send_with_retry_success_writes_no_dlq_record(monkeypatch):
    post = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    ok = notify._send_with_retry(
        "https://api.telegram.org/botX/sendMessage",
        {"chat_id": "1", "text": "hi"},
        alert={"site": "emag", "url": "https://emag.ro/x"},
        store="emag",
        alert_url="https://emag.ro/x",
    )

    assert ok is True
    assert _read_dlq_records() == []


def test_one_dead_lettered_alert_does_not_abort_remaining_sends(monkeypatch):
    post = _sequenced_post([_FakeResponse(403), _FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    first_ok = notify._send_with_retry(
        "https://api.telegram.org/botX/sendMessage",
        {"chat_id": "1", "text": "one"},
        alert={"site": "emag", "url": "https://emag.ro/a"},
        store="emag",
        alert_url="https://emag.ro/a",
    )
    second_ok = notify._send_with_retry(
        "https://api.telegram.org/botX/sendMessage",
        {"chat_id": "1", "text": "two"},
        alert={"site": "flanco", "url": "https://flanco.ro/b"},
        store="flanco",
        alert_url="https://flanco.ro/b",
    )

    assert first_ok is False
    assert second_ok is True
    assert post.call_count() == 2
    assert len(_read_dlq_records()) == 1


def test_dlq_record_has_required_fields_and_types(monkeypatch):
    post = _sequenced_post([_FakeResponse(404, text="Not Found")])
    monkeypatch.setattr(notify.requests, "post", post)

    notify._send_with_retry(
        "https://api.telegram.org/botX/sendMessage",
        {"chat_id": "1", "text": "hi"},
        alert={"site": "emag", "url": "https://emag.ro/x", "title": "T"},
        store="emag",
        alert_url="https://emag.ro/x",
    )

    records = _read_dlq_records()
    assert len(records) == 1
    record = records[0]
    assert isinstance(record["alert_id"], str)
    assert record["store"] == "emag"
    assert record["url"] == "https://emag.ro/x"
    assert isinstance(record["attempted_at_utc"], str)
    assert isinstance(record["attempt_count"], int)
    assert record["final_status_code"] == 404
    assert isinstance(record["failure_reason"], str)
    assert record["alert_payload"]["title"] == "T"


# --- T-12: transactional alert outbox ---------------------------------------


def test_outbox_pending_written_before_send_attempt(
    monkeypatch, tmp_path, _full_pipeline_env
):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )

    def _post(url, json=None, timeout=None):
        records = _read_outbox_records()
        assert records, "no PENDING record on disk when send was attempted"
        assert records[-1]["status"] == "PENDING"
        return _FakeResponse(200)

    monkeypatch.setattr(notify.requests, "post", _post)

    notify.main()

    records = _read_outbox_records()
    assert records[-1]["status"] == "SENT"


def test_outbox_sent_written_after_successful_send(
    monkeypatch, tmp_path, _full_pipeline_env
):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )
    post = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    records = _read_outbox_records()
    assert [r["status"] for r in records] == ["PENDING", "SENT"]
    assert records[0]["event_id"] == records[1]["event_id"]
    assert records[0]["source"] == "deal"


def test_outbox_dead_letter_written_after_dlq(
    monkeypatch, tmp_path, _full_pipeline_env
):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )
    post = _sequenced_post([_FakeResponse(403, text="Forbidden")])
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    records = _read_outbox_records()
    assert [r["status"] for r in records] == ["PENDING", "DEAD_LETTER"]
    assert records[-1]["error_log"] is not None
    assert len(_read_dlq_records()) == 1


def test_replay_fires_for_pending_older_than_grace_window(monkeypatch, tmp_path):
    event_id = "evt-old"
    old_created = (
        notify.datetime.now(notify.UTC) - notify.timedelta(minutes=10)
    ).isoformat()
    notify._write_outbox_record(
        event_id,
        "health",
        {"text": "old alert"},
        {"text": "old alert"},
        "PENDING",
        old_created,
        0,
        "scrape_health",
    )
    post = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    effective = notify._replay_pending_outbox(
        "https://api.telegram.org/botX/sendMessage", "12345"
    )

    assert effective[event_id]["status"] == "SENT"
    assert post.call_count() == 1
    records = _read_outbox_records()
    assert records[-1]["status"] == "SENT"
    assert records[-1]["event_id"] == event_id


def test_replay_does_not_fire_for_pending_within_grace_window(monkeypatch, tmp_path):
    event_id = "evt-fresh"
    recent_created = (
        notify.datetime.now(notify.UTC) - notify.timedelta(minutes=1)
    ).isoformat()
    notify._write_outbox_record(
        event_id,
        "health",
        {"text": "fresh"},
        {"text": "fresh"},
        "PENDING",
        recent_created,
        0,
        "scrape_health",
    )
    post = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    effective = notify._replay_pending_outbox(
        "https://api.telegram.org/botX/sendMessage", "12345"
    )

    assert effective[event_id]["status"] == "PENDING"
    assert post.call_count() == 0
    records = _read_outbox_records()
    assert len(records) == 1


def test_duplicate_skip_for_already_sent_health_alert(
    monkeypatch, tmp_path, _full_pipeline_env
):
    message = "⚠️ test health alert"
    (tmp_path / "scrape_health_alerts.json").write_text(
        json.dumps([message]), encoding="utf-8"
    )
    (tmp_path / "formatted_alerts.json").write_text(json.dumps([]), encoding="utf-8")

    event_id = notify._health_event_id(message)
    now = notify.datetime.now(notify.UTC).isoformat()
    notify._write_outbox_record(
        event_id,
        "health",
        {"text": message},
        {"text": message},
        "SENT",
        now,
        1,
        "scrape_health",
    )

    post = _sequenced_post([])  # any call raises IndexError -> test failure
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert post.call_count() == 0
    records = _read_outbox_records()
    assert len(records) == 1  # untouched: no new PENDING/SENT record appended


def test_event_id_stable_across_pending_and_terminal_records(
    monkeypatch, tmp_path, _full_pipeline_env
):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )
    post = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    # T-41: price-aware derivation (previously uuid5 of the URL alone),
    # recomputed here rather than via notify's helpers so a drift in either
    # the key or the id derivation fails this test.
    dedup_key = hashlib.sha256(
        f"{DEAL_ALERT['url']}:{DEAL_ALERT['new_price']}:{DEAL_ALERT['site']}".encode()
    ).hexdigest()
    expected_event_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"alert-decision:{dedup_key}")
    )
    records = _read_outbox_records()
    assert {r["event_id"] for r in records} == {expected_event_id}

    # A second scheduled run for the same alert must derive the identical
    # event_id and then skip it (SENT within the cooldown window).
    post2 = _sequenced_post([])
    monkeypatch.setattr(notify.requests, "post", post2)
    notify.main()

    assert post2.call_count() == 0
    records_after = _read_outbox_records()
    assert all(r["event_id"] == expected_event_id for r in records_after)
    assert len(records_after) == 2  # no new record appended by the second run


def test_outbox_missing_or_empty_file_is_noop(monkeypatch, tmp_path):
    assert not notify.OUTBOX_FILE.exists()
    effective = notify._replay_pending_outbox(
        "https://api.telegram.org/botX/sendMessage", "12345"
    )
    assert effective == {}

    notify.OUTBOX_FILE.write_text("", encoding="utf-8")
    effective_empty = notify._replay_pending_outbox(
        "https://api.telegram.org/botX/sendMessage", "12345"
    )
    assert effective_empty == {}


def test_replayed_event_not_resent_in_normal_loop_same_run(
    monkeypatch, tmp_path, _full_pipeline_env
):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )

    # T-41: price-aware id (previously uuid5 of the URL alone). The seeded
    # PENDING also carries its dedup_key, as every deal record now does —
    # which is what exercises the replay step's in-memory SENT record
    # against the live loop's cooldown check.
    dedup_key = hashlib.sha256(
        f"{DEAL_ALERT['url']}:{DEAL_ALERT['new_price']}:{DEAL_ALERT['site']}".encode()
    ).hexdigest()
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"alert-decision:{dedup_key}"))
    old_created = (
        notify.datetime.now(notify.UTC) - notify.timedelta(minutes=10)
    ).isoformat()
    send_payload = {
        "text": notify.format_telegram_message(DEAL_ALERT),
        "parse_mode": "HTML",
        "reply_markup": notify.build_inline_keyboard(DEAL_ALERT),
    }
    alert_payload = {
        "id": event_id,
        "evidence_urls": [DEAL_ALERT["url"]],
    }
    notify._write_outbox_record(
        event_id,
        "deal",
        alert_payload,
        send_payload,
        "PENDING",
        old_created,
        0,
        DEAL_ALERT["site"],
        dedup_key=dedup_key,
    )

    # Only one response queued: if the normal per-alert loop tried to resend
    # the just-replayed event, the second post() call runs out of items and
    # raises IndexError, failing the test.
    post = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert post.call_count() == 1
    records = [r for r in _read_outbox_records() if r["event_id"] == event_id]
    assert [r["status"] for r in records] == ["PENDING", "SENT"]


def test_replay_uses_site_field_for_store_label(monkeypatch, tmp_path):
    # alert_payload deliberately carries no "site" key (an AlertDecision
    # dump never has one) — the store label used for the replay send, and
    # therefore for the DLQ record if it fails, must come from the
    # top-level "site" field written at PENDING time, not a fallback.
    event_id = "evt-store-label"
    old_created = (
        notify.datetime.now(notify.UTC) - notify.timedelta(minutes=10)
    ).isoformat()
    notify._write_outbox_record(
        event_id,
        "deal",
        {"id": event_id, "evidence_urls": ["https://www.emag.ro/some-product/"]},
        {"text": "deal alert"},
        "PENDING",
        old_created,
        0,
        "emag",
    )
    post = _sequenced_post([_FakeResponse(403, text="Forbidden")])
    monkeypatch.setattr(notify.requests, "post", post)

    notify._replay_pending_outbox("https://api.telegram.org/botX/sendMessage", "12345")

    dlq_records = _read_dlq_records()
    assert len(dlq_records) == 1
    assert dlq_records[0]["store"] == "emag"

    outbox_records = _read_outbox_records()
    assert outbox_records[-1]["status"] == "DEAD_LETTER"
    assert outbox_records[-1]["site"] == "emag"


# --- T-13: alert dedup + per-watch cooldown windows -------------------------


def _seed_outbox_record(**overrides):
    """Writes a raw outbox record directly via _append_outbox_record,
    bypassing _write_outbox_record's auto-stamped last_attempt_at_utc so
    tests can seed an exact historical SENT timestamp."""
    now = notify.datetime.now(notify.UTC).isoformat()
    record = {
        "event_id": "evt-seed",
        "source": "deal",
        "alert_payload": {},
        "send_payload": {"text": "seed"},
        "channel": "telegram",
        "status": "SENT",
        "created_at_utc": now,
        "last_attempt_at_utc": now,
        "attempt_count": 1,
        "site": "emag",
        "error_log": None,
        "dedup_key": None,
    }
    record.update(overrides)
    notify._append_outbox_record(record)
    return record


def test_cooldown_skip_suppresses_same_deal_within_window(
    monkeypatch, tmp_path, _full_pipeline_env, capsys
):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )
    dedup_key = notify._deal_dedup_key(DEAL_ALERT)
    last_sent = (
        notify.datetime.now(notify.UTC) - notify.timedelta(hours=1)
    ).isoformat()
    _seed_outbox_record(
        event_id="evt-prior-deal",
        dedup_key=dedup_key,
        last_attempt_at_utc=last_sent,
        site=DEAL_ALERT["site"],
    )

    post = _sequenced_post([])  # any call -> IndexError -> test failure
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert post.call_count() == 0
    records = _read_outbox_records()
    assert len(records) == 1  # only the seeded record; nothing new appended
    assert "COOLDOWN SKIP" in capsys.readouterr().err


def test_cooldown_expired_allows_resend(monkeypatch, tmp_path, _full_pipeline_env):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )
    dedup_key = notify._deal_dedup_key(DEAL_ALERT)
    last_sent = (
        notify.datetime.now(notify.UTC) - notify.timedelta(hours=25)
    ).isoformat()
    _seed_outbox_record(
        event_id="evt-prior-deal",
        dedup_key=dedup_key,
        last_attempt_at_utc=last_sent,
        site=DEAL_ALERT["site"],
    )

    post = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert post.call_count() == 1
    records = _read_outbox_records()
    assert records[-1]["status"] == "SENT"
    assert records[-1]["dedup_key"] == dedup_key


def test_different_deal_not_suppressed_by_unrelated_dedup_key(
    monkeypatch, tmp_path, _full_pipeline_env
):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )
    other_alert = {**DEAL_ALERT, "url": "https://www.emag.ro/other-product/pd/XYZ/"}
    other_dedup_key = notify._deal_dedup_key(other_alert)
    last_sent = (
        notify.datetime.now(notify.UTC) - notify.timedelta(hours=1)
    ).isoformat()
    _seed_outbox_record(
        event_id="evt-other-deal",
        dedup_key=other_dedup_key,
        last_attempt_at_utc=last_sent,
        site=DEAL_ALERT["site"],
    )

    post = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert post.call_count() == 1
    records = _read_outbox_records()
    assert records[-1]["status"] == "SENT"


def test_custom_cooldown_hours_boundary(monkeypatch, tmp_path, _full_pipeline_env):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )
    notify.WATCHLIST_FILE.write_text(
        json.dumps(
            [{"site": "emag", "query": "laptop lenovo v15", "cooldown_hours": 2}]
        ),
        encoding="utf-8",
    )
    dedup_key = notify._deal_dedup_key(DEAL_ALERT)

    last_sent_within = (
        notify.datetime.now(notify.UTC) - notify.timedelta(hours=1, minutes=55)
    ).isoformat()
    _seed_outbox_record(
        event_id="evt-within",
        dedup_key=dedup_key,
        last_attempt_at_utc=last_sent_within,
        site=DEAL_ALERT["site"],
    )
    post_within = _sequenced_post([])
    monkeypatch.setattr(notify.requests, "post", post_within)
    notify.main()
    assert post_within.call_count() == 0

    notify.OUTBOX_FILE.write_text("", encoding="utf-8")
    last_sent_expired = (
        notify.datetime.now(notify.UTC) - notify.timedelta(hours=2, minutes=5)
    ).isoformat()
    _seed_outbox_record(
        event_id="evt-expired",
        dedup_key=dedup_key,
        last_attempt_at_utc=last_sent_expired,
        site=DEAL_ALERT["site"],
    )
    post_expired = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post_expired)
    notify.main()
    assert post_expired.call_count() == 1


def test_no_prior_outbox_state_always_sends(monkeypatch, tmp_path, _full_pipeline_env):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )

    post = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert post.call_count() == 1
    records = _read_outbox_records()
    assert records[-1]["status"] == "SENT"
    assert records[-1]["dedup_key"] == notify._deal_dedup_key(DEAL_ALERT)


def test_outbox_record_missing_dedup_key_never_suppresses(
    monkeypatch, tmp_path, _full_pipeline_env
):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )
    last_sent = (
        notify.datetime.now(notify.UTC) - notify.timedelta(hours=1)
    ).isoformat()
    _seed_outbox_record(
        event_id="evt-legacy",
        last_attempt_at_utc=last_sent,
        site=DEAL_ALERT["site"],
    )
    # Simulate a genuinely pre-T-13 record where the field never existed,
    # rather than merely being present with value None.
    raw = json.loads(notify.OUTBOX_FILE.read_text(encoding="utf-8").strip())
    del raw["dedup_key"]
    notify.OUTBOX_FILE.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    post = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert post.call_count() == 1
    records = _read_outbox_records()
    assert records[-1]["status"] == "SENT"


def test_cooldown_uses_last_attempt_at_utc_not_created_at_utc(
    monkeypatch, tmp_path, _full_pipeline_env
):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps([DEAL_ALERT]), encoding="utf-8"
    )
    dedup_key = notify._deal_dedup_key(DEAL_ALERT)
    # created_at_utc is far outside any cooldown window, but
    # last_attempt_at_utc (the actual send time) is recent — suppression
    # must key off last_attempt_at_utc, or this alert would wrongly send.
    old_created = (
        notify.datetime.now(notify.UTC) - notify.timedelta(days=10)
    ).isoformat()
    recent_sent = (
        notify.datetime.now(notify.UTC) - notify.timedelta(hours=1)
    ).isoformat()
    _seed_outbox_record(
        event_id="evt-retry-after-days",
        dedup_key=dedup_key,
        created_at_utc=old_created,
        last_attempt_at_utc=recent_sent,
        site=DEAL_ALERT["site"],
    )

    post = _sequenced_post([])  # any call -> IndexError -> test failure
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert post.call_count() == 0


# --- T-41 (#58): unified outbox + price-aware dedup -------------------------
# These drive notify.main() end-to-end with no seeded outbox records, so
# every event_id/dedup_key comes from the production derivation itself.


def _write_formatted(tmp_path, alerts):
    (tmp_path / "formatted_alerts.json").write_text(
        json.dumps(alerts), encoding="utf-8"
    )


def _enable_photo_path(alert):
    # generate_quickchart_url needs >= 3 history points to return a chart,
    # which is what routes main() onto the sendPhoto branch.
    history = [
        {"date": "2026-09-01", "price": 100.0},
        {"date": "2026-09-02", "price": 95.0},
        {"date": "2026-09-03", "price": 90.0},
    ]
    notify.PRICE_HISTORY_FILE.write_text(
        json.dumps({"products": {alert["url"]: {"history": history}}}),
        encoding="utf-8",
    )


def _age_outbox_records(hours):
    """Shifts every record's timestamps back by `hours`, simulating the
    passage of time between two scheduled runs."""
    records = _read_outbox_records()
    delta = notify.timedelta(hours=hours)
    for record in records:
        for field in ("created_at_utc", "last_attempt_at_utc"):
            if record.get(field):
                shifted = notify.datetime.fromisoformat(record[field]) - delta
                record[field] = shifted.isoformat()
    notify.OUTBOX_FILE.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


def test_realert_on_new_lower_price_same_url(monkeypatch, tmp_path, _full_pipeline_env):
    # Bug A: event_id was uuid5(url) and SENT was a permanent skip, so a
    # further price drop on an already-alerted URL could never alert again.
    _write_formatted(tmp_path, [DEAL_ALERT])
    monkeypatch.setattr(notify.requests, "post", _sequenced_post([_FakeResponse(200)]))
    notify.main()

    _write_formatted(tmp_path, [{**DEAL_ALERT, "new_price": 70.0}])
    post2 = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post2)
    notify.main()

    assert post2.call_count() == 1


def test_cooldown_expiry_rearms_same_price(monkeypatch, tmp_path, _full_pipeline_env):
    # Bug A: once the cooldown window has passed, the same deal must be
    # allowed to alert again — the cooldown is the only suppression.
    _write_formatted(tmp_path, [DEAL_ALERT])
    monkeypatch.setattr(notify.requests, "post", _sequenced_post([_FakeResponse(200)]))
    notify.main()

    _age_outbox_records(hours=notify.DEFAULT_COOLDOWN_HOURS + 1)
    post2 = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post2)
    notify.main()

    assert post2.call_count() == 1


def test_photo_path_writes_pending_before_send(
    monkeypatch, tmp_path, _full_pipeline_env
):
    # Bug B: a successful sendPhoto previously wrote no outbox record at
    # all, so a crash mid-send left no trace and nothing could dedup it.
    _write_formatted(tmp_path, [DEAL_ALERT])
    _enable_photo_path(DEAL_ALERT)
    calls = []

    def _post(url, json=None, timeout=None):
        calls.append(
            (url.rsplit("/", 1)[-1], [r["status"] for r in _read_outbox_records()])
        )
        return _FakeResponse(200)

    monkeypatch.setattr(notify.requests, "post", _post)
    notify.main()

    assert calls == [("sendPhoto", ["PENDING"])]
    records = _read_outbox_records()
    assert [r["status"] for r in records] == ["PENDING", "SENT"]
    assert records[-1]["dedup_key"] == notify._deal_dedup_key(DEAL_ALERT)


def test_photo_path_second_run_within_cooldown_skipped(
    monkeypatch, tmp_path, _full_pipeline_env
):
    # Bug B: with no outbox record from the photo path, the next scheduled
    # run re-sent the identical deal.
    _write_formatted(tmp_path, [DEAL_ALERT])
    _enable_photo_path(DEAL_ALERT)
    monkeypatch.setattr(notify.requests, "post", _sequenced_post([_FakeResponse(200)]))
    notify.main()

    post2 = _sequenced_post([])  # any call -> IndexError -> test failure
    monkeypatch.setattr(notify.requests, "post", post2)
    notify.main()

    assert post2.call_count() == 0


def test_same_deal_twice_in_one_run_sent_once(
    monkeypatch, tmp_path, _full_pipeline_env
):
    # Bug C: the in-memory effective map was never updated inside the main
    # loop, so the same offer matched by two watches went out twice.
    _write_formatted(tmp_path, [DEAL_ALERT, {**DEAL_ALERT, "query": "lenovo v15 16gb"}])
    post = _sequenced_post([_FakeResponse(200)])
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert post.call_count() == 1
    assert [r["status"] for r in _read_outbox_records()] == ["PENDING", "SENT"]


# --- T-37b (#55): delivery_attempts SQLite audit trail -----------------------


def _delivery_attempt_rows(db_path):
    conn = init_db(db_path)
    try:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM delivery_attempts").fetchall()
        ]
    finally:
        conn.close()


def test_successful_text_send_writes_exactly_one_delivered_attempt(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    monkeypatch.setattr(notify.requests, "post", _sequenced_post([_FakeResponse(200)]))

    notify.main()

    rows = _delivery_attempt_rows(notify.DB_FILE)
    assert len(rows) == 1
    assert rows[0]["final_state"] == "delivered"


def test_successful_photo_send_writes_exactly_one_delivered_attempt(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _enable_photo_path(DEAL_ALERT)
    monkeypatch.setattr(notify.requests, "post", _sequenced_post([_FakeResponse(200)]))

    notify.main()

    rows = _delivery_attempt_rows(notify.DB_FILE)
    assert len(rows) == 1
    assert rows[0]["final_state"] == "delivered"


def test_exhausted_send_failure_writes_exactly_one_failed_attempt(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    monkeypatch.setattr(
        notify.requests,
        "post",
        _sequenced_post([_FakeResponse(403, text="Forbidden")]),
    )

    notify.main()

    rows = _delivery_attempt_rows(notify.DB_FILE)
    assert len(rows) == 1
    assert rows[0]["final_state"] == "failed"


def test_destination_ref_is_not_the_raw_chat_id(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    monkeypatch.setattr(notify.requests, "post", _sequenced_post([_FakeResponse(200)]))

    notify.main()

    rows = _delivery_attempt_rows(notify.DB_FILE)
    assert len(rows) == 1
    assert rows[0]["destination_ref"] != "12345"


def test_health_alert_delivery_attempt_has_no_dedup_key(
    monkeypatch, tmp_path, _full_pipeline_env
):
    message = "⚠️ test health alert"
    (tmp_path / "scrape_health_alerts.json").write_text(
        json.dumps([message]), encoding="utf-8"
    )
    _write_formatted(tmp_path, [])
    monkeypatch.setattr(notify.requests, "post", _sequenced_post([_FakeResponse(200)]))

    notify.main()

    rows = _delivery_attempt_rows(notify.DB_FILE)
    assert len(rows) == 1
    assert rows[0]["dedup_key"] is None
    assert rows[0]["final_state"] == "delivered"


def test_delivery_attempt_gets_new_number_per_real_delivery_cycle(
    monkeypatch, tmp_path, _full_pipeline_env
):
    # Same event_id/dedup_key can legitimately go through more than one live
    # PENDING->terminal cycle over its lifetime (e.g. this deal re-alerting
    # once its cooldown expires) -- each real cycle must get its own
    # attempt_number instead of colliding with the first row via the SQLite
    # UPSERT's (alert_decision_id, attempt_number) key.
    _write_formatted(tmp_path, [DEAL_ALERT])
    monkeypatch.setattr(notify.requests, "post", _sequenced_post([_FakeResponse(200)]))
    notify.main()

    rows = _delivery_attempt_rows(notify.DB_FILE)
    assert len(rows) == 1
    assert rows[0]["attempt_number"] == 1
    decision_id = rows[0]["alert_decision_id"]

    # Cooldown expires -> the real replay/cooldown machinery (not a mock)
    # drives a second genuine delivery cycle for the identical event.
    _age_outbox_records(hours=notify.DEFAULT_COOLDOWN_HOURS + 1)
    monkeypatch.setattr(notify.requests, "post", _sequenced_post([_FakeResponse(200)]))
    notify.main()

    rows = _delivery_attempt_rows(notify.DB_FILE)
    assert {r["alert_decision_id"] for r in rows} == {decision_id}
    assert sorted(r["attempt_number"] for r in rows) == [1, 2]

    # An exact, already-known-attempt-number rewrite of the second cycle
    # (e.g. a reconciliation job re-auditing a row it already knows) must
    # upsert onto attempt 2 via the storage layer's known-number path, not
    # append a third row. notify.py itself never does this in the live
    # pipeline -- every live call site allocates a new cycle -- so this
    # exercises record_delivery_attempt() (mode B) directly.
    second_attempt = next(r for r in rows if r["attempt_number"] == 2)
    db = init_db(notify.DB_FILE)
    try:
        record_delivery_attempt(
            db,
            DeliveryAttempt(
                alert_decision_id=uuid.UUID(second_attempt["alert_decision_id"]),
                channel=second_attempt["channel"],
                destination=second_attempt["destination_ref"],
                attempt_number=2,
                response_class=second_attempt["response_class"],
                dedup_key=second_attempt["dedup_key"],
                final_state=second_attempt["final_state"],
            ),
        )
    finally:
        db.close()

    rows = _delivery_attempt_rows(notify.DB_FILE)
    assert len(rows) == 2
    assert sorted(r["attempt_number"] for r in rows) == [1, 2]


# --- T-25 (#33): Omnibus genuine savings + fake-discount label ---------------


def test_format_telegram_message_shows_genuine_savings_percent():
    alert = {
        **BASE_ALERT,
        "deal_stats": {
            "genuine_savings_percent": 11.11,
            "fake_discount_suspect": False,
            "fake_discount_reasons": [],
        },
    }
    message = format_telegram_message(alert)
    assert "Economie reală vs. minim 30 zile:</b> 11.11%" in message
    assert "SUSPICIUNE REDUCERE FALSĂ" not in message


def test_format_telegram_message_flags_non_discount_and_fake_discount():
    alert = {
        **BASE_ALERT,
        "verdict": "FALSE_DISCOUNT",
        "deal_stats": {
            "genuine_savings_percent": -5.0,
            "fake_discount_suspect": True,
            "fake_discount_reasons": [
                "ADVERTISED_ORIGINAL_INFLATED",
                "OBSERVED_PRE_SALE_HIKE",
            ],
        },
    }
    message = format_telegram_message(alert)
    assert "-5.00% (legal, nu este o reducere)" in message
    assert "<b>SUSPICIUNE REDUCERE FALSĂ:</b>" in message
    assert "preț tăiat umflat" in message
    assert "preț majorat chiar înainte de reducere" in message


def test_format_telegram_message_without_deal_stats_is_unchanged():
    # formatted_alerts.json written before T-25 has no deal_stats key.
    message = format_telegram_message(BASE_ALERT)
    assert "Economie reală" not in message
    assert "SUSPICIUNE" not in message


def test_fake_discount_suspect_recorded_as_distinct_alert_reason(
    monkeypatch, tmp_path, _full_pipeline_env
):
    alert = {
        **DEAL_ALERT,
        "rule_verdict": "FALSE_DISCOUNT",
        "deal_stats": {
            "genuine_savings_percent": 0.0,
            "fake_discount_suspect": True,
            "fake_discount_reasons": ["OBSERVED_PRE_SALE_HIKE"],
        },
    }
    _write_formatted(tmp_path, [alert])
    monkeypatch.setattr(notify.requests, "post", _sequenced_post([_FakeResponse(200)]))

    notify.main()

    reasons = _read_outbox_records()[-1]["alert_payload"]["reasons"]
    assert reasons == ["FALSE_DISCOUNT", "FAKE_DISCOUNT_SUSPECT"]


def test_no_fake_discount_reason_when_not_suspect(
    monkeypatch, tmp_path, _full_pipeline_env
):
    alert = {**DEAL_ALERT, "rule_verdict": "GENUINE_DEAL"}
    _write_formatted(tmp_path, [alert])
    monkeypatch.setattr(notify.requests, "post", _sequenced_post([_FakeResponse(200)]))

    notify.main()

    reasons = _read_outbox_records()[-1]["alert_payload"]["reasons"]
    assert reasons == ["GENUINE_DEAL"]


# --- T-26 (#35): additional channels + per-watch routing --------------------
# Strict fakes: every HTTP post is matched against the URLs a test declares,
# and an undeclared URL fails the test instead of silently passing.

TELEGRAM_SEND_URL = "https://api.telegram.org/bottesttoken/sendMessage"
TEAMS_URL = (
    "https://prod-00.westeurope.logic.azure.com/workflows/abc/triggers/manual/"
    "paths/invoke?api-version=2016-06-01&sp=%2Ftriggers&sv=1.0&sig=SECRETSIG123"
)
NTFY_URL = "https://ntfy.sh/"


def _routed_post(routes):
    """routes maps URL -> list of responses/exceptions, consumed in order.
    Every call is recorded as (url, json) on _post.calls."""
    queues = {url: list(items) for url, items in routes.items()}
    calls = []

    def _post(url, json=None, timeout=None):
        assert url in queues, f"unexpected POST to {url}"
        calls.append((url, json))
        item = queues[url].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    _post.calls = calls
    return _post


def _write_modern_watchlist(watches):
    notify.WATCHLIST_FILE.write_text(json.dumps({"watches": watches}), encoding="utf-8")


def _deal_watch(**overrides):
    return {"site": DEAL_ALERT["site"], "query": DEAL_ALERT["query"], **overrides}


def _base_event_id(alert=DEAL_ALERT):
    return notify._deal_event_id(notify._deal_dedup_key(alert))


class _FakeSMTPFactory:
    """Stands in for smtplib.SMTP / SMTP_SSL. script holds one item per
    connection: None delivers, a plain OSError is raised on connect, and an
    SMTP reply error is raised from send_message. (SMTPException subclasses
    OSError, hence the explicit exclusion.)"""

    def __init__(self, script):
        self.script = list(script)
        self.connections = []
        self.sent = []

    def __call__(self, host, port, timeout=None):
        item = self.script.pop(0)
        if isinstance(item, OSError) and not isinstance(
            item, notify.smtplib.SMTPException
        ):
            raise item
        conn = _FakeSMTPConnection(self, host, port, item)
        self.connections.append(conn)
        return conn


class _FakeSMTPConnection:
    def __init__(self, factory, host, port, outcome):
        self.factory = factory
        self.host = host
        self.port = port
        self.outcome = outcome
        self.tls = False
        self.login_user = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.login_user = user

    def send_message(self, msg):
        if self.outcome is not None:
            raise self.outcome
        self.factory.sent.append(msg)
        return {}


SMTP_CONFIG = notify.SmtpConfig(
    host="smtp.example.com",
    port=587,
    username="bot@example.com",
    password="app-password",
    sender="bot@example.com",
    recipients="me@example.com",
)
EMAIL_PAYLOAD = {"subject": "Deal", "body": "Preț Nou: 80.00 RON"}


def _set_smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", SMTP_CONFIG.host)
    monkeypatch.setenv("SMTP_PORT", str(SMTP_CONFIG.port))
    monkeypatch.setenv("SMTP_USERNAME", SMTP_CONFIG.username)
    monkeypatch.setenv("SMTP_PASSWORD", SMTP_CONFIG.password)
    monkeypatch.setenv("EMAIL_FROM", SMTP_CONFIG.sender)
    monkeypatch.setenv("EMAIL_TO", SMTP_CONFIG.recipients)


# Formatting ------------------------------------------------------------------


def test_format_plain_text_message_strips_markup_and_unescapes():
    text = notify.format_plain_text_message(BASE_ALERT)
    assert "<b>" not in text
    assert "OFERTĂ REALĂ" in text
    assert "Merită cumpărat <acum>." in text
    assert "&lt;" not in text


def test_build_teams_card_is_adaptive_card_with_offer_actions():
    alert = {
        **BASE_ALERT,
        "deal_stats": {
            "genuine_savings_percent": 11.11,
            "fake_discount_suspect": False,
            "fake_discount_reasons": [],
        },
    }
    payload = notify.build_teams_card(alert)

    # Workflows webhook envelope (Teams connector TeamsIncomingWebhookTrigger):
    # type "message", and each attachment carries contentType, a contentUrl
    # that must be null, and the card object as content.
    assert set(payload) == {"type", "attachments"}
    assert payload["type"] == "message"
    assert len(payload["attachments"]) == 1
    attachment = payload["attachments"][0]
    assert set(attachment) == {"contentType", "contentUrl", "content"}
    assert attachment["contentUrl"] is None
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard"
    # Only 1.0-era elements are used; 1.2 matches Microsoft's Workflows sample.
    assert card["version"] == "1.2"
    texts = [block.get("text", "") for block in card["body"]]
    assert any("OFERTĂ REALĂ" in t for t in texts)
    assert BASE_ALERT["title"] in texts
    # T-25's deal_stats lines are carried over as plain text, not HTML.
    assert any("Economie reală vs. minim 30 zile: 11.11%" in t for t in texts)
    assert not any("<b>" in t for t in texts)
    facts = next(b for b in card["body"] if b["type"] == "FactSet")["facts"]
    assert {"title": "Preț Nou", "value": "80.00 RON"} in facts
    urls = [a["url"] for a in card["actions"]]
    assert urls[0] == BASE_ALERT["url"]
    assert urls[1].startswith("https://www.compari.ro/CategorySearch.php?st=")
    assert all(a["type"] == "Action.OpenUrl" for a in card["actions"])


def test_build_ntfy_payload_has_click_url_and_no_topic():
    payload = notify.build_ntfy_payload(BASE_ALERT)
    assert payload["click"] == BASE_ALERT["url"]
    assert "OFERTĂ REALĂ" in payload["title"]
    assert "Merită cumpărat <acum>." in payload["message"]
    assert payload["actions"][0]["url"] == BASE_ALERT["url"]
    # The topic is effectively a subscription secret on ntfy.sh; it is added
    # at send time and never persisted into the outbox's send_payload.
    assert "topic" not in payload


def test_build_email_payload_has_subject_and_links():
    payload = notify.build_email_payload(BASE_ALERT)
    assert "OFERTĂ REALĂ" in payload["subject"]
    assert "80.00 RON" in payload["subject"]
    assert "\n" not in payload["subject"]
    assert BASE_ALERT["url"] in payload["body"]
    assert "<b>" not in payload["body"]


# HTTP retry path shared by Telegram, Teams and ntfy -------------------------


def test_send_with_retry_accepts_202_accepted_as_success(monkeypatch):
    # A Teams Workflows webhook answers 202 Accepted, not 200.
    post = _sequenced_post([_FakeResponse(202)])
    monkeypatch.setattr(notify.requests, "post", post)

    ok = notify._send_with_retry(
        TEAMS_URL, {"type": "message"}, alert=None, store="emag", alert_url=""
    )

    assert ok is True
    assert post.call_count() == 1
    assert _read_dlq_records() == []


def test_teams_webhook_signature_redacted_from_network_error_dlq(monkeypatch):
    post = _sequenced_post(
        [requests.ConnectionError(f"Max retries exceeded with url: {TEAMS_URL}")]
        * notify.MAX_ATTEMPTS
    )
    monkeypatch.setattr(notify.requests, "post", post)

    notify._send_with_retry(
        TEAMS_URL, {"type": "message"}, alert=None, store="emag", alert_url=""
    )

    reason = _read_dlq_records()[0]["failure_reason"]
    assert "SECRETSIG123" not in reason
    assert "sig=[REDACTED]" in reason


# SMTP retry path --------------------------------------------------------------


def test_send_email_success_uses_starttls_and_login(monkeypatch):
    smtp = _FakeSMTPFactory([None])
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)

    ok = notify._send_email_with_retry(
        SMTP_CONFIG, EMAIL_PAYLOAD, alert=None, store="emag", alert_url=""
    )

    assert ok is True
    assert smtp.connections[0].tls is True
    assert smtp.connections[0].login_user == "bot@example.com"
    msg = smtp.sent[0]
    assert msg["Subject"] == "Deal"
    assert msg["To"] == "me@example.com"
    assert "80.00 RON" in msg.get_content()


def test_send_email_4xx_reply_retries_then_succeeds(monkeypatch):
    smtp = _FakeSMTPFactory(
        [notify.smtplib.SMTPDataError(451, b"try again later"), None]
    )
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)

    ok = notify._send_email_with_retry(
        SMTP_CONFIG, EMAIL_PAYLOAD, alert=None, store="emag", alert_url=""
    )

    assert ok is True
    assert len(smtp.connections) == 2
    assert _read_dlq_records() == []


def test_send_email_5xx_reply_dead_letters_without_retry(monkeypatch):
    smtp = _FakeSMTPFactory([notify.smtplib.SMTPDataError(550, b"mailbox unavailable")])
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)

    ok = notify._send_email_with_retry(
        SMTP_CONFIG, EMAIL_PAYLOAD, alert={"x": 1}, store="emag", alert_url="u"
    )

    assert ok is False
    assert len(smtp.connections) == 1
    records = _read_dlq_records()
    assert len(records) == 1
    assert records[0]["attempt_count"] == 1
    assert records[0]["final_status_code"] == 550


def test_send_email_auth_failure_is_permanent(monkeypatch):
    smtp = _FakeSMTPFactory(
        [notify.smtplib.SMTPAuthenticationError(535, b"bad credentials")]
    )
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)

    ok = notify._send_email_with_retry(
        SMTP_CONFIG, EMAIL_PAYLOAD, alert=None, store="emag", alert_url=""
    )

    assert ok is False
    assert len(smtp.connections) == 1
    assert _read_dlq_records()[0]["final_status_code"] == 535


def test_send_email_connection_error_retries_to_max_then_dead_letters(monkeypatch):
    smtp = _FakeSMTPFactory([ConnectionRefusedError("refused")] * notify.MAX_ATTEMPTS)
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)

    ok = notify._send_email_with_retry(
        SMTP_CONFIG, EMAIL_PAYLOAD, alert=None, store="emag", alert_url=""
    )

    assert ok is False
    assert smtp.script == []  # every attempt consumed
    records = _read_dlq_records()
    assert records[0]["attempt_count"] == notify.MAX_ATTEMPTS
    assert records[0]["final_status_code"] == "network_error"


def test_send_email_port_465_uses_implicit_tls(monkeypatch):
    smtp_ssl = _FakeSMTPFactory([None])
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", smtp_ssl)
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTPFactory([]))
    config = notify.SmtpConfig(**{**SMTP_CONFIG.__dict__, "port": 465})

    ok = notify._send_email_with_retry(
        config, EMAIL_PAYLOAD, alert=None, store="emag", alert_url=""
    )

    assert ok is True
    assert smtp_ssl.connections[0].tls is False  # no STARTTLS on implicit TLS


# Per-watch routing through the outbox, end to end ---------------------------


def test_watch_without_channels_routes_to_telegram_only(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch()])
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", TEAMS_URL)
    post = _routed_post({TELEGRAM_SEND_URL: [_FakeResponse(200)]})
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert [url for url, _ in post.calls] == [TELEGRAM_SEND_URL]


def test_watch_routed_to_telegram_and_teams_delivers_on_both(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch(channels=["telegram", "teams"])])
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", TEAMS_URL)
    post = _routed_post(
        {TELEGRAM_SEND_URL: [_FakeResponse(200)], TEAMS_URL: [_FakeResponse(202)]}
    )
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert [url for url, _ in post.calls] == [TELEGRAM_SEND_URL, TEAMS_URL]
    teams_body = post.calls[1][1]
    assert teams_body["attachments"][0]["content"]["type"] == "AdaptiveCard"

    records = _read_outbox_records()
    assert [(r["channel"], r["status"]) for r in records] == [
        ("telegram", "PENDING"),
        ("telegram", "SENT"),
        ("teams", "PENDING"),
        ("teams", "SENT"),
    ]
    base_id = _base_event_id()
    # Telegram keeps the pre-T-26 event id, so existing outbox history still
    # matches; every other channel gets its own derived id.
    assert records[0]["event_id"] == base_id
    assert records[2]["event_id"] == f"{base_id}:teams"
    # The Teams send_payload is what a replay resends verbatim.
    assert records[3]["send_payload"] == teams_body

    rows = _delivery_attempt_rows(notify.DB_FILE)
    assert sorted(r["channel"] for r in rows) == ["teams", "telegram"]
    assert {r["alert_decision_id"] for r in rows} == {base_id}
    assert all(r["final_state"] == "delivered" for r in rows)
    assert all(TEAMS_URL not in r["destination_ref"] for r in rows)


def test_watch_routed_to_teams_only_skips_telegram(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch(channels=["teams"])])
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", TEAMS_URL)
    post = _routed_post({TEAMS_URL: [_FakeResponse(202)]})
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert [url for url, _ in post.calls] == [TEAMS_URL]


def test_routed_channel_not_configured_is_reported_not_sent(
    monkeypatch, tmp_path, _full_pipeline_env, capsys
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch(channels=["telegram", "teams"])])
    post = _routed_post({TELEGRAM_SEND_URL: [_FakeResponse(200)]})
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert [url for url, _ in post.calls] == [TELEGRAM_SEND_URL]
    assert {r["channel"] for r in _read_outbox_records()} == {"telegram"}
    assert "CHANNEL NOT CONFIGURED: teams" in capsys.readouterr().err


def test_two_watches_matching_same_offer_union_their_channels(
    monkeypatch, tmp_path, _full_pipeline_env
):
    # T-41's in-run duplicate skip keeps only the first alert per offer, so
    # routing must merge channels across every watch that matched it.
    second = {**DEAL_ALERT, "query": "lenovo v15 16gb"}
    _write_formatted(tmp_path, [DEAL_ALERT, second])
    _write_modern_watchlist(
        [
            _deal_watch(channels=["telegram"]),
            _deal_watch(query=second["query"], channels=["teams"]),
        ]
    )
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", TEAMS_URL)
    post = _routed_post(
        {TELEGRAM_SEND_URL: [_FakeResponse(200)], TEAMS_URL: [_FakeResponse(202)]}
    )
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert [url for url, _ in post.calls] == [TELEGRAM_SEND_URL, TEAMS_URL]


def test_ntfy_channel_posts_json_with_topic_to_server_root(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch(channels=["ntfy"])])
    monkeypatch.setenv("NTFY_TOPIC", "bf-test-topic")
    post = _routed_post({NTFY_URL: [_FakeResponse(200)]})
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    url, body = post.calls[0]
    assert url == NTFY_URL
    assert body["topic"] == "bf-test-topic"
    assert body["click"] == DEAL_ALERT["url"]
    stored = _read_outbox_records()[-1]
    assert stored["channel"] == "ntfy"
    assert stored["status"] == "SENT"
    assert "topic" not in stored["send_payload"]


def test_email_channel_delivers_through_outbox(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch(channels=["email"])])
    _set_smtp_env(monkeypatch)
    smtp = _FakeSMTPFactory([None])
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)
    monkeypatch.setattr(notify.requests, "post", _routed_post({}))

    notify.main()

    assert len(smtp.sent) == 1
    assert DEAL_ALERT["url"] in smtp.sent[0].get_content()
    assert [(r["channel"], r["status"]) for r in _read_outbox_records()] == [
        ("email", "PENDING"),
        ("email", "SENT"),
    ]


def test_email_missing_sender_or_recipient_is_not_configured(
    monkeypatch, tmp_path, _full_pipeline_env, capsys
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch(channels=["telegram", "email"])])
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    post = _routed_post({TELEGRAM_SEND_URL: [_FakeResponse(200)]})
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert [url for url, _ in post.calls] == [TELEGRAM_SEND_URL]
    err = capsys.readouterr().err
    assert "EMAIL_FROM" in err
    assert "CHANNEL NOT CONFIGURED: email" in err


# Retry/DLQ and cooldown behave identically, independently per channel -------


def test_teams_5xx_exhausts_retries_and_dead_letters_independently(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch(channels=["telegram", "teams"])])
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", TEAMS_URL)
    post = _routed_post(
        {
            TELEGRAM_SEND_URL: [_FakeResponse(200)],
            TEAMS_URL: [_FakeResponse(502, text="bad gateway")] * notify.MAX_ATTEMPTS,
        }
    )
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert sum(url == TEAMS_URL for url, _ in post.calls) == notify.MAX_ATTEMPTS
    effective = notify._load_outbox_effective()
    base_id = _base_event_id()
    assert effective[base_id]["status"] == "SENT"
    assert effective[f"{base_id}:teams"]["status"] == "DEAD_LETTER"
    dlq = _read_dlq_records()
    assert len(dlq) == 1
    assert dlq[0]["final_status_code"] == 502
    assert dlq[0]["store"] == DEAL_ALERT["site"]
    rows = _delivery_attempt_rows(notify.DB_FILE)
    assert {(r["channel"], r["final_state"]) for r in rows} == {
        ("telegram", "delivered"),
        ("teams", "failed"),
    }


def test_teams_permanent_4xx_dead_letters_after_one_attempt(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch(channels=["teams"])])
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", TEAMS_URL)
    post = _routed_post({TEAMS_URL: [_FakeResponse(400, text="bad card")]})
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert len(post.calls) == 1
    assert _read_outbox_records()[-1]["status"] == "DEAD_LETTER"
    assert _read_dlq_records()[0]["final_status_code"] == 400


def test_dead_lettered_teams_delivery_retried_next_run_without_resending_telegram(
    monkeypatch, tmp_path, _full_pipeline_env
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch(channels=["telegram", "teams"])])
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", TEAMS_URL)
    monkeypatch.setattr(
        notify.requests,
        "post",
        _routed_post(
            {
                TELEGRAM_SEND_URL: [_FakeResponse(200)],
                TEAMS_URL: [_FakeResponse(403)],
            }
        ),
    )
    notify.main()

    # Next run: Telegram's SENT is inside its cooldown, while Teams'
    # DEAD_LETTER is retried and must not be suppressed by Telegram's SENT.
    post2 = _routed_post({TEAMS_URL: [_FakeResponse(202)]})
    monkeypatch.setattr(notify.requests, "post", post2)
    notify.main()

    assert [url for url, _ in post2.calls] == [TEAMS_URL]


def test_cooldown_is_per_channel(monkeypatch, tmp_path, _full_pipeline_env):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch(channels=["telegram", "teams"])])
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", TEAMS_URL)
    dedup_key = notify._deal_dedup_key(DEAL_ALERT)
    last_sent = (
        notify.datetime.now(notify.UTC) - notify.timedelta(hours=1)
    ).isoformat()
    _seed_outbox_record(
        event_id=f"{_base_event_id()}:teams",
        channel="teams",
        dedup_key=dedup_key,
        last_attempt_at_utc=last_sent,
        site=DEAL_ALERT["site"],
    )
    post = _routed_post({TELEGRAM_SEND_URL: [_FakeResponse(200)]})
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    # Teams is in cooldown; Telegram, never sent, still goes out.
    assert [url for url, _ in post.calls] == [TELEGRAM_SEND_URL]


def test_replay_resends_pending_teams_record_via_teams_provider(
    monkeypatch, tmp_path, _full_pipeline_env
):
    base_id = _base_event_id()
    old_created = (
        notify.datetime.now(notify.UTC) - notify.timedelta(minutes=10)
    ).isoformat()
    card = notify.build_teams_card(DEAL_ALERT)
    notify._write_outbox_record(
        f"{base_id}:teams",
        "deal",
        {"id": base_id, "evidence_urls": [DEAL_ALERT["url"]]},
        card,
        "PENDING",
        old_created,
        0,
        DEAL_ALERT["site"],
        dedup_key=notify._deal_dedup_key(DEAL_ALERT),
        channel="teams",
    )
    _write_formatted(tmp_path, [])
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", TEAMS_URL)
    post = _routed_post({TEAMS_URL: [_FakeResponse(202)]})
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    assert post.calls == [(TEAMS_URL, card)]
    last = _read_outbox_records()[-1]
    assert (last["channel"], last["status"]) == ("teams", "SENT")
    rows = _delivery_attempt_rows(notify.DB_FILE)
    assert [(r["channel"], r["alert_decision_id"]) for r in rows] == [
        ("teams", base_id)
    ]


def test_replay_of_unconfigured_channel_dead_letters_instead_of_hanging(
    monkeypatch, tmp_path, _full_pipeline_env
):
    old_created = (
        notify.datetime.now(notify.UTC) - notify.timedelta(minutes=10)
    ).isoformat()
    notify._write_outbox_record(
        "evt:teams",
        "deal",
        {"id": "evt", "evidence_urls": [DEAL_ALERT["url"]]},
        {"type": "message"},
        "PENDING",
        old_created,
        0,
        DEAL_ALERT["site"],
        channel="teams",
    )
    _write_formatted(tmp_path, [])
    post = _routed_post({})  # nothing may be posted anywhere
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    last = _read_outbox_records()[-1]
    assert (last["event_id"], last["status"]) == ("evt:teams", "DEAD_LETTER")
    assert _read_dlq_records()[0]["final_status_code"] == "channel_not_configured"


# Cross-channel outcome matrix --------------------------------------------------
# One table drives every channel's real Provider.send (as built by
# _load_providers) through the same four outcomes. HTTP and SMTP codes differ,
# so each channel supplies its own equivalent: 2xx vs. None (delivered), 503
# vs. SMTP 451 (transient), 4xx vs. SMTP 550 (permanent).

MATRIX_NTFY_TOPIC = "bf-matrix-secret-topic"
MATRIX_ENV = {
    "TEAMS_WEBHOOK_URL": TEAMS_URL,
    "NTFY_TOPIC": MATRIX_NTFY_TOPIC,
    "SMTP_HOST": SMTP_CONFIG.host,
    "SMTP_PORT": str(SMTP_CONFIG.port),
    "SMTP_USERNAME": SMTP_CONFIG.username,
    "SMTP_PASSWORD": SMTP_CONFIG.password,
    "EMAIL_FROM": SMTP_CONFIG.sender,
    "EMAIL_TO": SMTP_CONFIG.recipients,
}


def _smtp_reply(code):
    return notify.smtplib.SMTPDataError(code, b"server reply")


# channel -> (endpoint URL or None for SMTP, delivered, transient, permanent)
CHANNEL_OUTCOMES = {
    "telegram": (
        TELEGRAM_SEND_URL,
        lambda: _FakeResponse(200),
        lambda: _FakeResponse(503, text="unavailable"),
        lambda: _FakeResponse(403, text="Forbidden"),
    ),
    "teams": (
        TEAMS_URL,
        lambda: _FakeResponse(202),
        lambda: _FakeResponse(503, text="unavailable"),
        lambda: _FakeResponse(400, text="Bad Request"),
    ),
    "ntfy": (
        NTFY_URL,
        lambda: _FakeResponse(200),
        lambda: _FakeResponse(503, text="unavailable"),
        lambda: _FakeResponse(400, text="Bad Request"),
    ),
    "email": (
        None,
        lambda: None,
        lambda: _smtp_reply(451),
        lambda: _smtp_reply(550),
    ),
}

TRANSIENT_CODE = {"telegram": 503, "teams": 503, "ntfy": 503, "email": 451}
PERMANENT_CODE = {"telegram": 403, "teams": 400, "ntfy": 400, "email": 550}


def _send_through_provider(monkeypatch, channel, script):
    """Installs a strict fake for the channel's transport, sends one deal
    through that channel's Provider, and returns (ok, attempts_made)."""
    url = CHANNEL_OUTCOMES[channel][0]
    if url is None:
        smtp = _FakeSMTPFactory(script)
        monkeypatch.setattr(notify.smtplib, "SMTP", smtp)
        monkeypatch.setattr(notify.requests, "post", _routed_post({}))
    else:
        post = _routed_post({url: script})
        monkeypatch.setattr(notify.requests, "post", post)

    providers = notify._load_providers("12345", TELEGRAM_SEND_URL, env=MATRIX_ENV)
    ok = providers[channel].send(
        notify.DEAL_PAYLOAD_BUILDERS[channel](DEAL_ALERT),
        alert=DEAL_ALERT,
        store=DEAL_ALERT["site"],
        alert_url=DEAL_ALERT["url"],
    )
    attempts = len(smtp.connections) if url is None else len(post.calls)
    return ok, attempts


@pytest.mark.parametrize("channel", list(CHANNEL_OUTCOMES))
def test_matrix_success_delivers_once_without_dlq(monkeypatch, channel):
    delivered = CHANNEL_OUTCOMES[channel][1]

    ok, attempts = _send_through_provider(monkeypatch, channel, [delivered()])

    assert ok is True
    assert attempts == 1
    assert _read_dlq_records() == []


@pytest.mark.parametrize("channel", list(CHANNEL_OUTCOMES))
def test_matrix_transient_failure_retries_then_succeeds(monkeypatch, channel):
    _, delivered, transient, _ = CHANNEL_OUTCOMES[channel]

    ok, attempts = _send_through_provider(
        monkeypatch, channel, [transient(), delivered()]
    )

    assert ok is True
    assert attempts == 2
    assert _read_dlq_records() == []


@pytest.mark.parametrize("channel", list(CHANNEL_OUTCOMES))
def test_matrix_permanent_failure_dead_letters_after_one_attempt(monkeypatch, channel):
    permanent = CHANNEL_OUTCOMES[channel][3]

    ok, attempts = _send_through_provider(monkeypatch, channel, [permanent()])

    assert ok is False
    assert attempts == 1
    records = _read_dlq_records()
    assert len(records) == 1
    assert records[0]["attempt_count"] == 1
    assert records[0]["final_status_code"] == PERMANENT_CODE[channel]
    assert records[0]["url"] == DEAL_ALERT["url"]


@pytest.mark.parametrize("channel", list(CHANNEL_OUTCOMES))
def test_matrix_transient_exhausts_retries_then_dead_letters(
    monkeypatch, channel, capsys
):
    transient = CHANNEL_OUTCOMES[channel][2]

    ok, attempts = _send_through_provider(
        monkeypatch, channel, [transient() for _ in range(notify.MAX_ATTEMPTS)]
    )

    assert ok is False
    assert attempts == notify.MAX_ATTEMPTS
    records = _read_dlq_records()
    assert len(records) == 1
    assert records[0]["attempt_count"] == notify.MAX_ATTEMPTS
    assert records[0]["final_status_code"] == TRANSIENT_CODE[channel]
    # Endpoint secrets never reach the dead-letter file or the log.
    leaked = notify.DLQ_FILE.read_text(encoding="utf-8") + capsys.readouterr().err
    for secret in ("SECRETSIG123", MATRIX_NTFY_TOPIC, SMTP_CONFIG.password):
        assert secret not in leaked


@pytest.mark.parametrize("channel", ["telegram", "teams", "ntfy"])
def test_matrix_http_network_error_exhausts_then_dead_letters(monkeypatch, channel):
    url = CHANNEL_OUTCOMES[channel][0]
    script = [
        requests.ConnectionError(f"Max retries exceeded with url: {url}")
        for _ in range(notify.MAX_ATTEMPTS)
    ]

    ok, attempts = _send_through_provider(monkeypatch, channel, script)

    assert ok is False
    assert attempts == notify.MAX_ATTEMPTS
    record = _read_dlq_records()[0]
    assert record["final_status_code"] == "network_error"
    assert "SECRETSIG123" not in record["failure_reason"]
    assert MATRIX_NTFY_TOPIC not in record["failure_reason"]


# SMTP-specific classification not covered by the matrix ------------------------


def test_send_email_recipients_refused_all_4xx_retries_then_succeeds(monkeypatch):
    refused = notify.smtplib.SMTPRecipientsRefused(
        {"me@example.com": (450, b"mailbox busy")}
    )
    smtp = _FakeSMTPFactory([refused, None])
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)

    ok = notify._send_email_with_retry(
        SMTP_CONFIG, EMAIL_PAYLOAD, alert=None, store="emag", alert_url=""
    )

    assert ok is True
    assert len(smtp.connections) == 2
    assert _read_dlq_records() == []


def test_send_email_recipients_refused_with_5xx_is_permanent(monkeypatch):
    refused = notify.smtplib.SMTPRecipientsRefused(
        {
            "a@example.com": (450, b"mailbox busy"),
            "b@example.com": (550, b"no such user"),
        }
    )
    smtp = _FakeSMTPFactory([refused])
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)

    ok = notify._send_email_with_retry(
        SMTP_CONFIG, EMAIL_PAYLOAD, alert=None, store="emag", alert_url=""
    )

    assert ok is False
    assert len(smtp.connections) == 1
    record = _read_dlq_records()[0]
    assert record["attempt_count"] == 1
    # Only reply codes are recorded, never the refused addresses.
    assert "example.com" not in record["failure_reason"]


def test_smtp_reply_echoing_one_of_several_recipients_is_redacted(monkeypatch, capsys):
    # GitHub masks only a secret's exact value: with EMAIL_TO holding two
    # addresses, a reply echoing just one of them would not be masked.
    config = notify.SmtpConfig(
        **{
            **SMTP_CONFIG.__dict__,
            "recipients": "alice@example.org, Bob.Smith@example.net",
        }
    )
    reply = notify.smtplib.SMTPDataError(
        550, b"5.1.1 <bob.smith@EXAMPLE.net>: Recipient address rejected"
    )
    smtp = _FakeSMTPFactory([reply])
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)

    ok = notify._send_email_with_retry(
        config, EMAIL_PAYLOAD, alert=None, store="emag", alert_url=""
    )

    assert ok is False
    reason = _read_dlq_records()[0]["failure_reason"]
    assert reason == "SMTPDataError: 550 5.1.1 <[REDACTED]>: Recipient address rejected"
    leaked = notify.DLQ_FILE.read_text(encoding="utf-8") + capsys.readouterr().err
    assert "bob.smith" not in leaked.lower()


def test_smtp_sender_echoed_in_transient_error_is_redacted(monkeypatch):
    reply = notify.smtplib.SMTPSenderRefused(
        451, f"4.7.1 <{SMTP_CONFIG.sender}>: try later".encode(), SMTP_CONFIG.sender
    )
    smtp = _FakeSMTPFactory([reply] * notify.MAX_ATTEMPTS)
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)

    notify._send_email_with_retry(
        SMTP_CONFIG, EMAIL_PAYLOAD, alert=None, store="emag", alert_url=""
    )

    record = _read_dlq_records()[0]
    assert record["final_status_code"] == 451
    assert SMTP_CONFIG.sender not in record["failure_reason"]


def test_secret_bearing_dataclass_fields_are_hidden_from_repr():
    config_repr = repr(SMTP_CONFIG)
    for value in (
        SMTP_CONFIG.password,
        SMTP_CONFIG.username,
        SMTP_CONFIG.sender,
        SMTP_CONFIG.recipients,
    ):
        assert value not in config_repr
    assert SMTP_CONFIG.host in config_repr

    providers = notify._load_providers("12345", TELEGRAM_SEND_URL, env=MATRIX_ENV)
    for provider in providers.values():
        # Field name, not value: the send closure's repr carries a memory
        # address that could contain a short value like the chat id.
        assert "destination=" not in repr(provider)
    assert "SECRETSIG123" not in repr(providers["teams"])
    assert MATRIX_NTFY_TOPIC not in repr(providers["ntfy"])


def test_send_email_server_disconnect_is_transient(monkeypatch):
    smtp = _FakeSMTPFactory(
        [notify.smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), None]
    )
    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)

    ok = notify._send_email_with_retry(
        SMTP_CONFIG, EMAIL_PAYLOAD, alert=None, store="emag", alert_url=""
    )

    assert ok is True
    assert len(smtp.connections) == 2


# ntfy topic stays out of persisted state on the failure path ------------------


def test_ntfy_topic_absent_from_outbox_dlq_and_logs_when_dead_lettered(
    monkeypatch, tmp_path, _full_pipeline_env, capsys
):
    _write_formatted(tmp_path, [DEAL_ALERT])
    _write_modern_watchlist([_deal_watch(channels=["ntfy"])])
    monkeypatch.setenv("NTFY_TOPIC", MATRIX_NTFY_TOPIC)
    post = _routed_post(
        {
            NTFY_URL: [
                requests.ConnectionError(f"Max retries exceeded with url: {NTFY_URL}")
                for _ in range(notify.MAX_ATTEMPTS)
            ]
        }
    )
    monkeypatch.setattr(notify.requests, "post", post)

    notify.main()

    # The topic does travel in the request body, as ntfy's JSON API requires,
    # but nothing written to disk or to the log carries it.
    assert all(body["topic"] == MATRIX_NTFY_TOPIC for _, body in post.calls)
    assert [r["status"] for r in _read_outbox_records()] == ["PENDING", "DEAD_LETTER"]
    assert MATRIX_NTFY_TOPIC not in notify.OUTBOX_FILE.read_text(encoding="utf-8")
    assert MATRIX_NTFY_TOPIC not in notify.DLQ_FILE.read_text(encoding="utf-8")
    assert MATRIX_NTFY_TOPIC not in capsys.readouterr().err
