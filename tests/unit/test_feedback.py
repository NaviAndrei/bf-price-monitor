"""T-28 (#37): feedback label vocabulary, callback codec, update classification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from bf_price_monitor.feedback import (
    CALLBACK_DATA_MAX_BYTES,
    FeedbackEvent,
    FeedbackLabel,
    FeedbackPayloadError,
    Rejection,
    build_feedback_keyboard_rows,
    classify_update,
    decode_callback,
    encode_callback,
    parse_allowed_user_ids,
    pseudonymize,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
CHAT_ID = -1001234567890
USER_ID = 987654321
SALT = b"s" * 32
DECISION = UUID("12345678-1234-5678-1234-567812345678")


def _update(
    *,
    update_id=100,
    query_id="q-1",
    user_id=USER_ID,
    chat_id=CHAT_ID,
    data=None,
    message_date=None,
):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": query_id,
            "from": {"id": user_id, "is_bot": False, "username": "someone"},
            "chat_instance": "ci",
            "message": {
                "message_id": 7,
                "date": int((NOW - timedelta(hours=1)).timestamp())
                if message_date is None
                else message_date,
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": encode_callback(DECISION, FeedbackLabel.USEFUL)
            if data is None
            else data,
        },
    }


def _classify(update):
    return classify_update(
        update,
        allowed_chat_id=str(CHAT_ID),
        allowed_user_ids=frozenset({str(USER_ID)}),
        salt=SALT,
        now=NOW,
    )


# -- codec -------------------------------------------------------------------


@pytest.mark.parametrize("label", list(FeedbackLabel))
def test_callback_round_trips_every_label_within_64_bytes(label):
    decision = uuid4()
    data = encode_callback(decision, label)
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_MAX_BYTES
    assert data.startswith("fb1:")
    assert decode_callback(data) == (decision, label)


def test_label_codes_are_unique_and_cover_every_label():
    datas = {encode_callback(DECISION, label) for label in FeedbackLabel}
    assert len(datas) == len(FeedbackLabel) == 6


@pytest.mark.parametrize(
    "data",
    [
        None,
        42,
        "",
        "x" * 65,
        "fb1:12345678123456781234567812345678",  # missing label
        "fb2:12345678123456781234567812345678:u",  # unknown version
        "fb1:not-a-uuid-not-a-uuid-not-a-uuid-12:u",
        "fb1:12345678123456781234567812345678:z",  # unknown code
        "fb1:12345678-1234-5678-1234-567812345678:u",  # dashed form rejected
        "fb1:1234567812345678123456781234567G:u",
        "fb1:ABCDEF78123456781234567812345678:u",  # upper-case hex rejected
        "fb1:12345678123456781234567812345678:u:extra",
    ],
)
def test_decode_rejects_malformed_payloads(data):
    with pytest.raises(FeedbackPayloadError):
        decode_callback(data)


def test_keyboard_rows_carry_all_six_labels_for_the_decision():
    rows = build_feedback_keyboard_rows(DECISION)
    buttons = [b for row in rows for b in row]
    assert len(rows) == 2
    assert {decode_callback(b["callback_data"])[1] for b in buttons} == set(
        FeedbackLabel
    )
    assert all(decode_callback(b["callback_data"])[0] == DECISION for b in buttons)
    assert all("url" not in b for b in buttons)


# -- classification -----------------------------------------------------------


def test_authorized_press_becomes_a_pseudonymous_event():
    event = _classify(_update())
    assert isinstance(event, FeedbackEvent)
    assert event.alert_decision_id == DECISION
    assert event.label is FeedbackLabel.USEFUL
    assert event.callback_query_id == "q-1"
    assert event.update_id == 100
    assert event.received_at_utc == NOW
    assert str(USER_ID) not in event.model_dump_json()
    assert str(CHAT_ID) not in event.model_dump_json()
    assert "someone" not in event.model_dump_json()
    assert event.user_ref == pseudonymize(SALT, "telegram-user", USER_ID)


def test_unauthorized_user_is_rejected_but_keeps_query_id_for_ack():
    result = _classify(_update(user_id=111))
    assert result == Rejection(
        update_id=100, reason="unauthorized_user", callback_query_id="q-1"
    )


def test_wrong_chat_is_rejected_even_for_an_allowlisted_user():
    result = _classify(_update(chat_id=555))
    assert isinstance(result, Rejection)
    assert result.reason == "unauthorized_chat"


def test_authorization_is_checked_before_payload_parsing():
    result = _classify(_update(user_id=111, data="garbage"))
    assert isinstance(result, Rejection)
    assert result.reason == "unauthorized_user"


def test_malformed_payload_from_authorized_user_is_rejected():
    result = _classify(_update(data="fb1:bogus"))
    assert isinstance(result, Rejection)
    assert result.reason == "malformed_payload"


def test_expired_alert_is_rejected():
    old = int((NOW - timedelta(days=31)).timestamp())
    result = _classify(_update(message_date=old))
    assert isinstance(result, Rejection)
    assert result.reason == "expired_alert"


def test_inaccessible_message_date_zero_is_not_treated_as_expired():
    # Telegram sends date=0 for messages it no longer exposes to the bot.
    assert isinstance(_classify(_update(message_date=0)), FeedbackEvent)


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"update_id": "7"}, "malformed_update"),
        ({"update_id": True, "callback_query": {}}, "malformed_update"),
        ({"update_id": 5, "message": {"text": "hi"}}, "not_a_callback"),
        ({"update_id": 5, "callback_query": {"id": ""}}, "malformed_callback"),
        (
            {"update_id": 5, "callback_query": {"id": "q", "from": {}}},
            "malformed_callback",
        ),
        (
            {"update_id": 5, "callback_query": {"id": "q", "from": {"id": 1}}},
            "unknown_chat",
        ),
    ],
)
def test_structurally_malformed_updates_never_raise(update, reason):
    result = _classify(update)
    assert isinstance(result, Rejection)
    assert result.reason == reason


def test_event_requires_utc_timestamp():
    with pytest.raises(ValidationError):
        FeedbackEvent(
            callback_query_id="q",
            update_id=1,
            alert_decision_id=DECISION,
            label=FeedbackLabel.USEFUL,
            user_ref="a" * 64,
            chat_ref="b" * 64,
            received_at_utc=datetime(2026, 11, 27, 12, 0),
        )


def test_event_rejects_unknown_label():
    with pytest.raises(ValidationError):
        FeedbackEvent(
            callback_query_id="q",
            update_id=1,
            alert_decision_id=DECISION,
            label="great",  # type: ignore[arg-type]
            user_ref="a" * 64,
            chat_ref="b" * 64,
            received_at_utc=NOW,
        )


def test_pseudonym_is_keyed_by_salt():
    a = pseudonymize(b"1" * 32, "telegram-user", USER_ID)
    b = pseudonymize(b"2" * 32, "telegram-user", USER_ID)
    assert a != b
    assert len(a) == 64
    assert str(USER_ID) not in a


def test_allowlist_parsing_ignores_non_numeric_tokens():
    assert parse_allowed_user_ids(" 1, 22 ,abc 333,,") == frozenset({"1", "22", "333"})
    assert parse_allowed_user_ids("") == frozenset()
    assert parse_allowed_user_ids(None) == frozenset()
