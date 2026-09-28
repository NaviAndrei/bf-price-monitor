"""T-28 (#37): feedback collector, Telegram client, CLI, and the end-to-end
path from a delivered alert's button to a stored label.

A fake Telegram server models getUpdates offset semantics as documented
("an update is considered confirmed as soon as getUpdates is called with an
offset higher than its update_id"). No network, no real data/ files.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import feedback
import notify
import pytest
import requests
from test_notify import BASE_ALERT

from bf_price_monitor.feedback import FeedbackLabel, decode_callback, encode_callback
from bf_price_monitor.storage import feedback as store
from bf_price_monitor.storage import sqlite as sqlite_storage

CHAT_ID = "12345"
USER_ID = 987654321
TOKEN = "123456:SECRET-bot-token"
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


class FakeTelegram:
    def __init__(self, updates=(), fail_on_calls=()):
        self.updates = list(updates)
        self.confirmed = 0
        self.calls = []
        self.answered = []
        self.fail_on_calls = set(fail_on_calls)
        self.answer_ok = True

    def get_updates(self, offset, *, timeout, limit=100):
        self.calls.append(offset)
        if len(self.calls) in self.fail_on_calls:
            raise feedback.TelegramError("getUpdates: ConnectionError")
        if offset is not None:
            self.confirmed = max(self.confirmed, offset - 1)
        return [u for u in self.updates if u["update_id"] > self.confirmed][:limit]

    def answer_callback(self, callback_query_id, text):
        self.answered.append((callback_query_id, text))
        return self.answer_ok


def _press(
    update_id,
    decision_id,
    label=FeedbackLabel.USEFUL,
    *,
    user_id=USER_ID,
    query_id=None,
    chat_id=int(CHAT_ID),
    data=None,
):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": query_id or f"q{update_id}",
            "from": {"id": user_id, "is_bot": False, "username": "rater_name"},
            "chat_instance": "ci",
            "message": {
                "message_id": 1,
                "date": int((NOW - timedelta(minutes=5)).timestamp()),
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": data or encode_callback(decision_id, label),
        },
    }


DECISION = "12345678-1234-5678-1234-567812345678"


@pytest.fixture
def db(tmp_path):
    conn = sqlite_storage.init_db(tmp_path / "price_history.db")
    sqlite_storage.record_delivery_attempt_new_cycle(
        conn,
        {
            "alert_decision_id": DECISION,
            "channel": "telegram",
            "destination": "d" * 64,
            "response_class": "2xx",
            "final_state": "delivered",
        },
    )
    yield conn
    conn.close()


def _collect(db, tg, *, now=NOW, holder="h", allowed=frozenset({str(USER_ID)})):
    return feedback.collect_once(
        db,
        tg,
        chat_id=CHAT_ID,
        allowed_user_ids=allowed,
        holder=holder,
        now_fn=lambda: now,
    )


def _decision():
    from uuid import UUID

    return UUID(DECISION)


# -- collect_once --------------------------------------------------------------


def test_collect_stores_confirms_offset_after_commit_and_answers(db):
    tg = FakeTelegram([_press(10, _decision())])
    result = _collect(db, tg)
    assert result.stored == 1
    # First call: no stored offset. Second: confirm offset 11 after commit.
    assert tg.calls == [None, 11]
    assert tg.confirmed == 10
    assert tg.answered == [("q10", feedback.ACK_TEXT["stored"])]
    assert store.get_consumer_state(db, feedback.CONSUMER) == (10, NOW)


def test_duplicate_press_delivery_is_idempotent(db):
    press = _press(10, _decision())
    tg = FakeTelegram([press, {**press, "update_id": 11}])  # same callback id twice
    result = _collect(db, tg)
    assert (result.stored, result.duplicate) == (1, 1)
    assert db.execute("SELECT COUNT(*) FROM alert_feedback").fetchone()[0] == 1


def test_crash_between_commit_and_offset_confirm_does_not_double_store(db):
    tg = FakeTelegram([_press(10, _decision())], fail_on_calls={2})
    first = _collect(db, tg)
    assert first.stored == 1
    assert tg.confirmed == 0  # the confirm call "crashed"

    second = _collect(db, tg)
    assert tg.calls[-1] == 11  # stored offset used; Telegram now confirms 10
    assert (second.stored, second.duplicate) == (0, 0)
    assert db.execute("SELECT COUNT(*) FROM alert_feedback").fetchone()[0] == 1


def test_replay_of_unconfirmed_batch_after_stale_offset_is_harmless(db):
    tg = FakeTelegram([_press(10, _decision())], fail_on_calls={2})
    _collect(db, tg)
    # > OFFSET_TRUST_WINDOW later the stored offset is not trusted (Telegram
    # may have restarted update ids), so the unconfirmed update comes back.
    later = NOW + feedback.OFFSET_TRUST_WINDOW + timedelta(hours=1)
    replay = _collect(db, tg, now=later)
    assert tg.calls[-2] is None
    assert (replay.stored, replay.duplicate) == (0, 1)
    assert db.execute("SELECT COUNT(*) FROM alert_feedback").fetchone()[0] == 1


def test_randomly_restarted_update_ids_are_not_lost(db):
    tg = FakeTelegram([_press(500, _decision())])
    _collect(db, tg)
    # A week without updates: Telegram picks a random, lower next update_id.
    fresh = FakeTelegram([_press(7, _decision(), FeedbackLabel.PURCHASED)])
    later = NOW + timedelta(days=8)
    result = _collect(db, fresh, now=later)
    assert fresh.calls[0] is None
    assert result.stored == 1
    assert [r["label"] for r in store.list_feedback(db)] == ["purchased"]


def test_unauthorized_malformed_and_unknown_alert_presses_are_consumed_not_stored(db):
    tg = FakeTelegram(
        [
            _press(10, _decision(), user_id=111),
            _press(11, _decision(), data="fb1:garbage"),
            _press(12, _decision(), chat_id=999),
            _press(13, _decision().__class__("87654321-4321-8765-4321-876543218765")),
        ]
    )
    result = _collect(db, tg)
    assert result.stored == 0
    assert result.rejected == {
        "unauthorized_user": 1,
        "malformed_payload": 1,
        "unauthorized_chat": 1,
        "unknown_alert": 1,
    }
    assert tg.confirmed == 13
    # Every spinner is stopped, with no text for rejected presses.
    assert sorted(tg.answered) == [(f"q{i}", "") for i in (10, 11, 12, 13)]


def test_non_mapping_update_is_rejected_without_crashing(db):
    tg = FakeTelegram()
    tg.get_updates = lambda offset, *, timeout, limit=100: (
        ["junk"] if offset is None else []
    )
    result = _collect(db, tg)
    assert result.rejected == {"malformed_update": 1}


def test_expired_callback_answer_failure_still_stores_label(db, capsys):
    tg = FakeTelegram([_press(10, _decision())])
    tg.answer_ok = False  # Telegram: "query is too old" (HTTP 400)
    result = _collect(db, tg)
    assert result.stored == 1
    assert "unanswered_callbacks=1" in capsys.readouterr().out


def test_second_consumer_is_blocked_by_lease(db):
    store.acquire_lease(
        db, feedback.CONSUMER, "worker-1", now=NOW, ttl=timedelta(minutes=5)
    )
    tg = FakeTelegram([_press(10, _decision())])
    assert _collect(db, tg, holder="scheduled-2") is None
    assert tg.calls == []


def test_logs_never_contain_identifiers_or_payloads(db, capsys):
    tg = FakeTelegram([_press(10, _decision()), _press(11, _decision(), user_id=111)])
    _collect(db, tg)
    out = capsys.readouterr().out
    for secret in (str(USER_ID), "111", CHAT_ID, "rater_name", DECISION, "fb1:", "q10"):
        assert secret not in out
    assert "stored=1" in out


# -- Telegram client --------------------------------------------------------


class _Resp:
    def __init__(self, status_code=200, body=None, bad_json=False):
        self.status_code = status_code
        self._body = {"ok": True, "result": []} if body is None else body
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("no json")
        return self._body


class _Http:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.requests = response, exc, []

    def post(self, url, json=None, timeout=None):
        self.requests.append((url, json, timeout))
        if self.exc:
            raise self.exc
        return self.response


def test_client_requests_only_callback_queries_with_offset():
    http = _Http(_Resp(body={"ok": True, "result": [{"update_id": 1}]}))
    client = feedback.TelegramFeedbackClient(TOKEN, http)
    assert client.get_updates(5, timeout=0) == [{"update_id": 1}]
    url, payload, _ = http.requests[0]
    assert url.endswith("/getUpdates")
    assert payload["allowed_updates"] == ["callback_query"]
    assert payload["offset"] == 5


def test_client_maps_409_to_conflict():
    client = feedback.TelegramFeedbackClient(TOKEN, _Http(_Resp(409)))
    with pytest.raises(feedback.TelegramConflict):
        client.get_updates(None, timeout=0)


@pytest.mark.parametrize(
    "http",
    [
        _Http(_Resp(500)),
        _Http(_Resp(bad_json=True)),
        _Http(_Resp(body={"ok": False})),
        _Http(_Resp(body={"ok": True, "result": {}})),
        _Http(exc=requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/x")),
    ],
)
def test_client_errors_never_leak_the_token(http):
    client = feedback.TelegramFeedbackClient(TOKEN, http)
    with pytest.raises(feedback.TelegramError) as info:
        client.get_updates(None, timeout=0)
    assert TOKEN not in str(info.value)
    assert info.value.__cause__ is None


def test_answer_callback_is_best_effort():
    ok = feedback.TelegramFeedbackClient(
        TOKEN, _Http(_Resp(body={"ok": True, "result": True}))
    )
    assert ok.answer_callback("q", "thanks") is True
    too_old = feedback.TelegramFeedbackClient(TOKEN, _Http(_Resp(400)))
    assert too_old.answer_callback("q", "") is False


# -- CLI ------------------------------------------------------------------------


def test_collect_is_disabled_without_allowlist_and_makes_no_request(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("network used"))
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID}
    assert feedback.main(["--db", str(tmp_path / "x.db"), "collect"], env=env) == 0
    assert "disabled" in capsys.readouterr().out
    assert not (tmp_path / "x.db").exists()


def test_collect_requires_bot_credentials(tmp_path, capsys):
    env = {"TELEGRAM_FEEDBACK_ALLOWED_USER_IDS": str(USER_ID)}
    assert feedback.main(["--db", str(tmp_path / "x.db"), "collect"], env=env) == 0
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().out


def _cli_env():
    return {
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "TELEGRAM_CHAT_ID": CHAT_ID,
        "TELEGRAM_FEEDBACK_ALLOWED_USER_IDS": str(USER_ID),
    }


def test_collect_cli_skips_on_conflict_and_releases_lease(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(409))
    path = tmp_path / "x.db"
    assert feedback.main(["--db", str(path), "collect"], env=_cli_env()) == 0
    assert "HTTP 409" in capsys.readouterr().out
    conn = sqlite_storage.init_db(path)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM feedback_consumer_lease").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_collect_cli_reports_failure_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(502))
    assert (
        feedback.main(["--db", str(tmp_path / "x.db"), "collect"], env=_cli_env()) == 1
    )


def test_worker_loop_stops_on_sigterm_and_releases_lease(db, monkeypatch):
    handlers = {}
    monkeypatch.setattr(
        feedback.signal, "signal", lambda sig, h: handlers.__setitem__(sig, h)
    )
    monkeypatch.setattr(feedback, "LOOP_ERROR_BACKOFF_SECONDS", 0)
    polls = {"n": 0}

    class LoopTelegram(FakeTelegram):
        def get_updates(self, offset, *, timeout, limit=100):
            polls["n"] += 1
            assert timeout in (feedback.LOOP_POLL_TIMEOUT_SECONDS, 0)
            if polls["n"] == 1:
                raise feedback.TelegramError("getUpdates: Timeout")  # backoff path
            if polls["n"] >= 3:
                handlers[feedback.signal.SIGTERM](feedback.signal.SIGTERM, None)
            return super().get_updates(offset, timeout=timeout, limit=limit)

    tg = LoopTelegram([_press(10, _decision())])
    assert (
        feedback._run_worker(db, tg, CHAT_ID, frozenset({str(USER_ID)}), "worker-x")
        == 0
    )
    assert db.execute("SELECT COUNT(*) FROM alert_feedback").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM feedback_consumer_lease").fetchone()[0] == 0


def _seed_labels(path):
    conn = sqlite_storage.init_db(path)
    try:
        sqlite_storage.record_delivery_attempt_new_cycle(
            conn,
            {
                "alert_decision_id": DECISION,
                "channel": "telegram",
                "destination": "d" * 64,
                "response_class": "2xx",
                "final_state": "delivered",
            },
        )
        tg = FakeTelegram(
            [
                _press(10, _decision(), FeedbackLabel.USEFUL),
                _press(11, _decision(), FeedbackLabel.FAKE_DISCOUNT),
                _press(12, _decision(), FeedbackLabel.PURCHASED, user_id=222),
            ]
        )
        _collect(conn, tg, allowed=frozenset({str(USER_ID), "222"}))
    finally:
        conn.close()


def test_export_is_privacy_safe_and_current_only_by_default(tmp_path):
    path = tmp_path / "x.db"
    _seed_labels(path)
    out = tmp_path / "exports" / "f.jsonl"
    assert feedback.main(["--db", str(path), "export", "--out", str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [(r["label"], r["rater"]) for r in rows] == [
        ("fake_discount", "rater_1"),
        ("purchased", "rater_2"),
    ]
    text = out.read_text(encoding="utf-8")
    for forbidden in ("user_ref", "chat_ref", "callback_query_id", str(USER_ID), "222"):
        assert forbidden not in text

    history = tmp_path / "h.jsonl"
    feedback.main(["--db", str(path), "export", "--out", str(history), "--history"])
    assert len(history.read_text(encoding="utf-8").splitlines()) == 3
    since = tmp_path / "s.jsonl"
    feedback.main(
        ["--db", str(path), "export", "--out", str(since), "--since", "2099-01-01"]
    )
    assert since.read_text(encoding="utf-8") == ""


def test_summary_purge_and_forget_user_cli(tmp_path, capsys):
    path = tmp_path / "x.db"
    _seed_labels(path)
    feedback.main(["--db", str(path), "summary"])
    out = capsys.readouterr().out
    assert "current labels: 2" in out and "fake_discount: 1" in out

    feedback.main(["--db", str(path), "forget-user", "--telegram-user-id", "222"])
    assert "deleted 1 label(s)" in capsys.readouterr().out
    feedback.main(["--db", str(path), "purge", "--before", "2099-01-01"])
    assert "purged 2 label(s)" in capsys.readouterr().out


# -- end to end: notify.py button -> collector -> stored label ----------------


def test_end_to_end_alert_button_press_is_stored_against_its_decision(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(notify, "FORMATTED_FILE", tmp_path / "formatted_alerts.json")
    monkeypatch.setattr(notify, "PRICE_HISTORY_FILE", tmp_path / "price_history.json")
    monkeypatch.setattr(notify, "SCRAPE_HEALTH_ALERTS_FILE", tmp_path / "health.json")
    monkeypatch.setattr(notify, "WATCHLIST_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(notify, "DB_FILE", tmp_path / "price_history.db")
    monkeypatch.setattr(notify, "DLQ_FILE", tmp_path / "dlq.jsonl")
    monkeypatch.setattr(notify, "OUTBOX_FILE", tmp_path / "alert_outbox.jsonl")
    monkeypatch.setattr(notify.time, "sleep", lambda *_: None)
    for name, value in _cli_env().items():
        monkeypatch.setenv(name, value)
    for name in ("NTFY_TOPIC", "SMTP_HOST"):
        monkeypatch.delenv(name, raising=False)
    notify.PRICE_HISTORY_FILE.write_text(json.dumps({"products": {}}), encoding="utf-8")
    alert = {**BASE_ALERT, "query": "laptop lenovo v15"}
    notify.FORMATTED_FILE.write_text(json.dumps([alert]), encoding="utf-8")
    sent = []

    class _Ok:
        status_code = 200
        headers: dict = {}
        text = ""

        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return _Ok()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    notify.main()

    (payload,) = sent
    rows = payload["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["url"] == alert["url"]  # offer links untouched
    fb_button = rows[1][1]
    decision_id, label = decode_callback(fb_button["callback_data"])
    assert label is FeedbackLabel.FAKE_DISCOUNT
    outbox = [
        json.loads(line)
        for line in notify.OUTBOX_FILE.read_text(encoding="utf-8").splitlines()
    ]
    assert outbox[-1]["event_id"] == str(decision_id)

    conn = sqlite_storage.init_db(notify.DB_FILE)
    try:
        press = _press(40, decision_id, data=fb_button["callback_data"])
        result = _collect(conn, FakeTelegram([press]))
        assert result.stored == 1
        (row,) = store.list_feedback(conn)
        assert row["alert_decision_id"] == str(decision_id)
        assert row["label"] == "fake_discount"
    finally:
        conn.close()


def test_monitor_workflow_collects_between_restore_and_save_without_blocking():
    from pathlib import Path

    workflow = Path(__file__).resolve().parent.parent / ".github/workflows/monitor.yml"
    text = workflow.read_text(encoding="utf-8")
    restore = text.index("- name: Restore runtime state")
    collect = text.index("- name: Collect alert feedback")
    save = text.index("- name: Save runtime state")
    assert restore < collect < save
    step = text[collect : text.index("- name:", collect + 1)]
    assert "continue-on-error: true" in step
    assert "TELEGRAM_FEEDBACK_ALLOWED_USER_IDS" in step
    assert "scripts/feedback.py collect" in step
    send = text[text.index("- name: Send alerts") : save]
    assert "TELEGRAM_FEEDBACK_ALLOWED_USER_IDS" in send
