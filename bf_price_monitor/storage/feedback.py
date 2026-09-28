"""Feedback label repository (T-28, #37).

Tables are created by migration 1 in ``bf_price_monitor.storage.sqlite``.
Every write here is a single transaction; ``ingest_batch`` commits the
labels and the consumer's ``last_update_id`` together, so the stored offset
can never run ahead of the labels it covers.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from bf_price_monitor.feedback import FeedbackEvent, Rejection


@dataclass
class IngestResult:
    stored: int = 0
    duplicate: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    last_update_id: int | None = None
    # (callback_query_id, outcome) for every callback in the batch, so the
    # caller can stop each button's spinner after the commit.
    callback_outcomes: list[tuple[str, str]] = field(default_factory=list)


def get_pseudonym_salt(db: sqlite3.Connection) -> bytes:
    row = db.execute(
        "SELECT value FROM feedback_settings WHERE key = 'pseudonym_salt'"
    ).fetchone()
    if row is None:  # pragma: no cover - migration 1 always inserts it
        raise RuntimeError("feedback pseudonym salt missing; database not migrated")
    return bytes(row["value"])


def alert_was_delivered(db: sqlite3.Connection, alert_decision_id: str) -> bool:
    """A label must reference an AlertDecision this bot actually delivered to
    Telegram. There is no alert_decisions table (see DECISIONS.md, T-37b), so
    the delivery_attempts audit row is the referential check."""
    row = db.execute(
        """
        SELECT 1 FROM delivery_attempts
        WHERE alert_decision_id = ? AND channel = 'telegram'
          AND final_state = 'delivered'
        LIMIT 1
        """,
        (alert_decision_id,),
    ).fetchone()
    return row is not None


def ingest_batch(
    db: sqlite3.Connection,
    consumer: str,
    items: Sequence[FeedbackEvent | Rejection],
    *,
    now: datetime,
) -> IngestResult:
    """Persists one getUpdates batch atomically. Idempotent: a callback whose
    callback_query_id is already stored counts as a duplicate and changes
    nothing, so a batch replayed after a crash is harmless. Items are
    processed in update_id order, which is Telegram's delivery order."""
    result = IngestResult()
    ordered = sorted(items, key=lambda item: item.update_id)
    with db:
        for item in ordered:
            if isinstance(item, Rejection):
                result.rejected[item.reason] += 1
                if item.callback_query_id is not None:
                    result.callback_outcomes.append(
                        (item.callback_query_id, "rejected")
                    )
                continue
            decision_id = str(item.alert_decision_id)
            if not alert_was_delivered(db, decision_id):
                result.rejected["unknown_alert"] += 1
                result.callback_outcomes.append((item.callback_query_id, "rejected"))
                continue
            cursor = db.execute(
                """
                INSERT INTO alert_feedback
                    (callback_query_id, update_id, alert_decision_id, label,
                     user_ref, chat_ref, callback_version, received_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(callback_query_id) DO NOTHING
                """,
                (
                    item.callback_query_id,
                    item.update_id,
                    decision_id,
                    item.label.value,
                    item.user_ref,
                    item.chat_ref,
                    item.callback_version,
                    item.received_at_utc.astimezone(UTC).isoformat(),
                ),
            )
            if cursor.rowcount == 1:
                result.stored += 1
                result.callback_outcomes.append((item.callback_query_id, "stored"))
            else:
                result.duplicate += 1
                result.callback_outcomes.append((item.callback_query_id, "duplicate"))
        valid_ids = [item.update_id for item in ordered if item.update_id >= 0]
        if valid_ids:
            result.last_update_id = max(valid_ids)
            db.execute(
                """
                INSERT INTO feedback_consumer_state
                    (consumer, last_update_id, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(consumer) DO UPDATE SET
                    last_update_id = excluded.last_update_id,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (consumer, result.last_update_id, now.astimezone(UTC).isoformat()),
            )
    return result


def get_consumer_state(
    db: sqlite3.Connection, consumer: str
) -> tuple[int, datetime] | None:
    row = db.execute(
        "SELECT last_update_id, updated_at_utc FROM feedback_consumer_state "
        "WHERE consumer = ?",
        (consumer,),
    ).fetchone()
    if row is None:
        return None
    return cast(int, row["last_update_id"]), datetime.fromisoformat(
        row["updated_at_utc"]
    )


def acquire_lease(
    db: sqlite3.Connection,
    consumer: str,
    holder: str,
    *,
    now: datetime,
    ttl: timedelta,
) -> bool:
    """Single-writer guard: at most one feedback consumer (scheduled poller or
    always-on worker) may pull updates for a database at a time. A lease
    whose holder died simply expires after ttl."""
    expires = (now + ttl).astimezone(UTC).isoformat()
    with db:
        cursor = db.execute(
            """
            INSERT INTO feedback_consumer_lease (consumer, holder, expires_at_utc)
            VALUES (?, ?, ?)
            ON CONFLICT(consumer) DO UPDATE SET
                holder = excluded.holder,
                expires_at_utc = excluded.expires_at_utc
            WHERE feedback_consumer_lease.holder = excluded.holder
               OR feedback_consumer_lease.expires_at_utc < ?
            """,
            (consumer, holder, expires, now.astimezone(UTC).isoformat()),
        )
    return cursor.rowcount == 1


def release_lease(db: sqlite3.Connection, consumer: str, holder: str) -> None:
    with db:
        db.execute(
            "DELETE FROM feedback_consumer_lease WHERE consumer = ? AND holder = ?",
            (consumer, holder),
        )


def list_feedback(
    db: sqlite3.Connection,
    *,
    current_only: bool = True,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Labels for offline evaluation, oldest first. chat_ref and the raw
    callback_query_id are deliberately not returned."""
    source = "alert_feedback_current" if current_only else "alert_feedback"
    sql = (
        "SELECT seq, alert_decision_id, label, user_ref, callback_version, "
        f"received_at_utc FROM {source}"
    )
    params: tuple[str, ...] = ()
    if since is not None:
        sql += " WHERE received_at_utc >= ?"
        params = (since.astimezone(UTC).isoformat(),)
    sql += " ORDER BY seq ASC"
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def feedback_summary(db: sqlite3.Connection) -> dict[str, int]:
    rows = db.execute(
        "SELECT label, COUNT(*) AS n FROM alert_feedback_current "
        "GROUP BY label ORDER BY label"
    ).fetchall()
    return {row["label"]: row["n"] for row in rows}


def purge_feedback_before(db: sqlite3.Connection, cutoff: datetime) -> int:
    with db:
        cursor = db.execute(
            "DELETE FROM alert_feedback WHERE received_at_utc < ?",
            (cutoff.astimezone(UTC).isoformat(),),
        )
    return cursor.rowcount


def delete_feedback_for_user(db: sqlite3.Connection, user_ref: str) -> int:
    with db:
        cursor = db.execute(
            "DELETE FROM alert_feedback WHERE user_ref = ?", (user_ref,)
        )
    return cursor.rowcount
