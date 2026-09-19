import json

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
    # T-11 tests must never write to the real data/dlq.jsonl, and the
    # backoff schedule (up to ~30s/attempt) would make the suite crawl if
    # time.sleep actually ran.
    monkeypatch.setattr(notify, "DLQ_FILE", tmp_path / "dlq.jsonl")
    monkeypatch.setattr(notify.time, "sleep", lambda *_: None)


def _read_dlq_records():
    if not notify.DLQ_FILE.exists():
        return []
    lines = notify.DLQ_FILE.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


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
