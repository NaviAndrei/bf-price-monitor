"""T-28 (#37): versioned migration and feedback repository (tmp_path only)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from bf_price_monitor.feedback import FeedbackEvent, FeedbackLabel, Rejection
from bf_price_monitor.storage import feedback as store
from bf_price_monitor.storage import sqlite as sqlite_storage

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
DECISION = UUID("12345678-1234-5678-1234-567812345678")
OTHER_DECISION = UUID("87654321-4321-8765-4321-876543218765")
USER_A = "a" * 64
USER_B = "b" * 64
CHAT = "c" * 64


@pytest.fixture
def db(tmp_path):
    conn = sqlite_storage.init_db(tmp_path / "t.db")
    yield conn
    conn.close()


def _deliver(db, decision=DECISION, channel="telegram", final_state="delivered"):
    sqlite_storage.record_delivery_attempt_new_cycle(
        db,
        {
            "alert_decision_id": decision,
            "channel": channel,
            "destination": "d" * 64,
            "response_class": "2xx",
            "final_state": final_state,
        },
    )


def _event(
    query_id, update_id, label=FeedbackLabel.USEFUL, user=USER_A, decision=DECISION
):
    return FeedbackEvent(
        callback_query_id=query_id,
        update_id=update_id,
        alert_decision_id=decision,
        label=label,
        user_ref=user,
        chat_ref=CHAT,
        received_at_utc=NOW,
    )


# -- migration ---------------------------------------------------------------


def _tables(conn):
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }


def test_fresh_database_is_migrated_to_current_version(db):
    assert sqlite_storage._user_version(db) == sqlite_storage.SCHEMA_VERSION == 1
    assert {
        "alert_feedback",
        "alert_feedback_current",
        "feedback_consumer_state",
        "feedback_consumer_lease",
        "feedback_settings",
    } <= _tables(db)
    assert len(store.get_pseudonym_salt(db)) == 32


def test_legacy_v0_database_upgrades_in_place_keeping_its_data(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(path))
    legacy.executescript(sqlite_storage._SCHEMA_SQL)
    legacy.execute("INSERT INTO canonical_products (id, title) VALUES ('p1', 'Laptop')")
    legacy.commit()
    assert legacy.execute("PRAGMA user_version").fetchone()[0] == 0
    legacy.close()

    conn = sqlite_storage.init_db(path)
    try:
        assert sqlite_storage._user_version(conn) == 1
        assert (
            conn.execute("SELECT title FROM canonical_products").fetchone()[0]
            == "Laptop"
        )
        assert "alert_feedback" in _tables(conn)
    finally:
        conn.close()


def test_reopening_is_idempotent_and_keeps_the_salt(tmp_path):
    path = tmp_path / "t.db"
    first = sqlite_storage.init_db(path)
    salt = store.get_pseudonym_salt(first)
    first.close()
    second = sqlite_storage.init_db(path)
    try:
        assert store.get_pseudonym_salt(second) == salt
        assert (
            second.execute("SELECT COUNT(*) FROM feedback_settings").fetchone()[0] == 1
        )
    finally:
        second.close()


def test_failed_migration_rolls_back_completely(tmp_path, monkeypatch):
    broken = ((1, ("CREATE TABLE half_done (x INTEGER)", "THIS IS NOT SQL")),)
    monkeypatch.setattr(sqlite_storage, "_MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        sqlite_storage.init_db(tmp_path / "t.db")
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "half_done" not in _tables(conn)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("label", "great"),
        ("received_at_utc", "2026-11-27T12:00:00"),
        ("alert_decision_id", "not-a-uuid"),
        ("user_ref", "123456789"),
    ],
)
def test_schema_constraints_reject_invalid_rows(db, column, value):
    row = {
        "callback_query_id": "q",
        "update_id": 1,
        "alert_decision_id": str(DECISION),
        "label": "useful",
        "user_ref": USER_A,
        "chat_ref": CHAT,
        "callback_version": "fb1",
        "received_at_utc": NOW.isoformat(),
        column: value,
    }
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            f"INSERT INTO alert_feedback ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )


# -- ingestion ---------------------------------------------------------------


def test_ingest_stores_label_and_offset_in_one_commit(db):
    _deliver(db)
    result = store.ingest_batch(db, "c", [_event("q1", 10)], now=NOW)
    assert (result.stored, result.duplicate, result.last_update_id) == (1, 0, 10)
    assert result.callback_outcomes == [("q1", "stored")]
    assert store.get_consumer_state(db, "c") == (10, NOW)
    rows = store.list_feedback(db)
    assert [(r["alert_decision_id"], r["label"]) for r in rows] == [
        (str(DECISION), "useful")
    ]


def test_replayed_batch_is_a_duplicate_and_changes_nothing(db):
    _deliver(db)
    batch = [_event("q1", 10)]
    store.ingest_batch(db, "c", batch, now=NOW)
    replay = store.ingest_batch(db, "c", batch, now=NOW)
    assert (replay.stored, replay.duplicate) == (0, 1)
    assert replay.callback_outcomes == [("q1", "duplicate")]
    assert db.execute("SELECT COUNT(*) FROM alert_feedback").fetchone()[0] == 1


def test_label_for_undelivered_alert_is_rejected(db):
    _deliver(db, decision=OTHER_DECISION)  # a different alert was delivered
    _deliver(db, decision=DECISION, final_state="failed")  # this one failed
    result = store.ingest_batch(db, "c", [_event("q1", 10)], now=NOW)
    assert result.stored == 0
    assert result.rejected["unknown_alert"] == 1
    assert store.list_feedback(db) == []
    # The update is still consumed: the offset advances past it.
    assert result.last_update_id == 10


def test_non_telegram_delivery_does_not_validate_a_label(db):
    _deliver(db, channel="ntfy")
    result = store.ingest_batch(db, "c", [_event("q1", 10)], now=NOW)
    assert result.rejected["unknown_alert"] == 1


def test_relabel_policy_latest_press_wins_and_history_is_kept(db):
    _deliver(db)
    store.ingest_batch(db, "c", [_event("q1", 10, FeedbackLabel.USEFUL)], now=NOW)
    store.ingest_batch(
        db, "c", [_event("q2", 11, FeedbackLabel.FAKE_DISCOUNT)], now=NOW
    )
    current = store.list_feedback(db)
    history = store.list_feedback(db, current_only=False)
    assert [r["label"] for r in current] == ["fake_discount"]
    assert [r["label"] for r in history] == ["useful", "fake_discount"]


def test_relabel_is_per_rater(db):
    _deliver(db)
    store.ingest_batch(
        db,
        "c",
        [
            _event("q1", 10, FeedbackLabel.USEFUL, user=USER_A),
            _event("q2", 11, FeedbackLabel.PURCHASED, user=USER_B),
        ],
        now=NOW,
    )
    assert store.feedback_summary(db) == {"purchased": 1, "useful": 1}


def test_out_of_order_batch_is_applied_in_update_id_order(db):
    _deliver(db)
    batch = [
        _event("q2", 11, FeedbackLabel.WRONG_PRICE),
        _event("q1", 10, FeedbackLabel.USEFUL),
    ]
    result = store.ingest_batch(db, "c", batch, now=NOW)
    assert result.last_update_id == 11
    assert [r["label"] for r in store.list_feedback(db)] == ["wrong_price"]


def test_rejections_advance_offset_and_are_counted_by_reason(db):
    batch = [
        Rejection(update_id=20, reason="unauthorized_user", callback_query_id="qx"),
        Rejection(update_id=21, reason="not_a_callback"),
    ]
    result = store.ingest_batch(db, "c", batch, now=NOW)
    assert result.rejected == {"unauthorized_user": 1, "not_a_callback": 1}
    assert result.callback_outcomes == [("qx", "rejected")]
    assert store.get_consumer_state(db, "c") == (21, NOW)


def test_failure_mid_batch_rolls_back_labels_and_offset(db, monkeypatch):
    _deliver(db)
    store.ingest_batch(db, "c", [_event("q1", 10)], now=NOW)
    calls = {"n": 0}
    real = store.alert_was_delivered

    def flaky(conn, decision_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, decision_id)

    monkeypatch.setattr(store, "alert_was_delivered", flaky)
    with pytest.raises(sqlite3.OperationalError):
        store.ingest_batch(db, "c", [_event("q2", 11), _event("q3", 12)], now=NOW)
    assert db.execute("SELECT COUNT(*) FROM alert_feedback").fetchone()[0] == 1
    assert store.get_consumer_state(db, "c") == (10, NOW)


# -- lease, retention, deletion ------------------------------------------------


def test_lease_blocks_a_second_holder_until_it_expires(db):
    ttl = timedelta(minutes=5)
    assert store.acquire_lease(db, "c", "h1", now=NOW, ttl=ttl)
    assert store.acquire_lease(db, "c", "h1", now=NOW, ttl=ttl)  # renew
    assert not store.acquire_lease(db, "c", "h2", now=NOW, ttl=ttl)
    assert store.acquire_lease(db, "c", "h2", now=NOW + timedelta(minutes=6), ttl=ttl)


def test_released_lease_is_immediately_available(db):
    ttl = timedelta(minutes=5)
    store.acquire_lease(db, "c", "h1", now=NOW, ttl=ttl)
    store.release_lease(db, "c", "h1")
    assert store.acquire_lease(db, "c", "h2", now=NOW, ttl=ttl)


def test_purge_and_forget_user(db):
    _deliver(db)
    store.ingest_batch(
        db,
        "c",
        [_event("q1", 10, user=USER_A), _event("q2", 11, user=USER_B)],
        now=NOW,
    )
    assert store.delete_feedback_for_user(db, USER_A) == 1
    assert [r["user_ref"] for r in store.list_feedback(db)] == [USER_B]
    assert store.purge_feedback_before(db, NOW + timedelta(seconds=1)) == 1
    assert store.list_feedback(db) == []


def test_list_feedback_since_filter_and_no_chat_ref(db):
    _deliver(db)
    store.ingest_batch(db, "c", [_event("q1", 10)], now=NOW)
    assert store.list_feedback(db, since=NOW + timedelta(days=1)) == []
    (row,) = store.list_feedback(db, since=NOW - timedelta(days=1))
    assert "chat_ref" not in row
    assert "callback_query_id" not in row
