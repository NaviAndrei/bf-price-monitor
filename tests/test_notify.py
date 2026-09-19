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
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "testtoken")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
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

    expected_event_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"alert-decision:{DEAL_ALERT['url']}")
    )
    records = _read_outbox_records()
    assert {r["event_id"] for r in records} == {expected_event_id}

    # A second scheduled run for the same alert must derive the identical
    # event_id and then skip it as an exact-match duplicate (already SENT).
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

    event_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"alert-decision:{DEAL_ALERT['url']}")
    )
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
