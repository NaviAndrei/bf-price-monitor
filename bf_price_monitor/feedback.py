"""Alert feedback labels (T-28, #37).

Pure, I/O-free pieces of the feedback pipeline: the label vocabulary, the
versioned Telegram ``callback_data`` codec, and the classification of one raw
Telegram ``Update`` into either a storable ``FeedbackEvent`` or a rejection
reason. HTTP (getUpdates / answerCallbackQuery) lives in
``scripts/feedback.py``; persistence lives in ``bf_price_monitor.storage``.

Privacy: nothing here returns or logs a raw Telegram user id, username or
chat id. Identities leave this module only as keyed HMAC pseudonyms
(``pseudonymize``), keyed by a random per-database salt that never leaves
the runner-local SQLite file.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Telegram InlineKeyboardButton.callback_data must be 1-64 bytes.
CALLBACK_DATA_MAX_BYTES = 64
CALLBACK_VERSION = "fb1"

# A feedback press on an alert older than this is rejected as expired: the
# deal context it would label has long since changed, and it bounds how far
# back a replayed/forged update could reach.
MAX_ALERT_AGE = timedelta(days=30)


class FeedbackLabel(StrEnum):
    USEFUL = "useful"
    FAKE_DISCOUNT = "fake_discount"
    WRONG_PRODUCT = "wrong_product"
    WRONG_PRICE = "wrong_price"
    DUPLICATE = "duplicate"
    PURCHASED = "purchased"


# One-character wire codes keep callback_data far under 64 bytes. Codes are
# part of the fb1 wire format: never reuse or reassign one; add new labels
# with new codes (or a new CALLBACK_VERSION).
_LABEL_CODES: dict[FeedbackLabel, str] = {
    FeedbackLabel.USEFUL: "u",
    FeedbackLabel.FAKE_DISCOUNT: "f",
    FeedbackLabel.WRONG_PRODUCT: "w",
    FeedbackLabel.WRONG_PRICE: "p",
    FeedbackLabel.DUPLICATE: "d",
    FeedbackLabel.PURCHASED: "b",
}
_CODE_LABELS = {code: label for label, code in _LABEL_CODES.items()}

# Button captions (Romanian, like the rest of the alert UI), in keyboard order.
BUTTON_ROWS: tuple[tuple[tuple[FeedbackLabel, str], ...], ...] = (
    (
        (FeedbackLabel.USEFUL, "\U0001f44d Util"),
        (FeedbackLabel.FAKE_DISCOUNT, "\U0001f44e Reducere falsă"),
        (FeedbackLabel.PURCHASED, "\U0001f6d2 Cumpărat"),
    ),
    (
        (FeedbackLabel.WRONG_PRODUCT, "❌ Alt produs"),
        (FeedbackLabel.WRONG_PRICE, "\U0001f4b8 Preț greșit"),
        (FeedbackLabel.DUPLICATE, "\U0001f501 Duplicat"),
    ),
)


class FeedbackPayloadError(ValueError):
    """callback_data is not a well-formed fb1 feedback payload."""


def encode_callback(alert_decision_id: UUID, label: FeedbackLabel) -> str:
    data = f"{CALLBACK_VERSION}:{alert_decision_id.hex}:{_LABEL_CODES[label]}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:  # pragma: no cover
        raise FeedbackPayloadError("callback_data exceeds Telegram's 64-byte limit")
    return data


def decode_callback(data: object) -> tuple[UUID, FeedbackLabel]:
    if not isinstance(data, str):
        raise FeedbackPayloadError("callback_data is not a string")
    if not data or len(data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
        raise FeedbackPayloadError("callback_data length out of range")
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_VERSION:
        raise FeedbackPayloadError("unknown callback format or version")
    _, id_hex, code = parts
    if len(id_hex) != 32 or id_hex != id_hex.lower():
        raise FeedbackPayloadError("malformed alert decision id")
    try:
        decision_id = UUID(hex=id_hex)
    except ValueError as exc:
        raise FeedbackPayloadError("malformed alert decision id") from exc
    label = _CODE_LABELS.get(code)
    if label is None:
        raise FeedbackPayloadError("unknown label code")
    return decision_id, label


def build_feedback_keyboard_rows(alert_decision_id: UUID) -> list[list[dict[str, str]]]:
    return [
        [
            {
                "text": caption,
                "callback_data": encode_callback(alert_decision_id, label),
            }
            for label, caption in row
        ]
        for row in BUTTON_ROWS
    ]


def pseudonymize(salt: bytes, kind: str, raw_id: int | str) -> str:
    """Keyed, non-reversible reference for a Telegram identity. A plain
    sha256 of a ~10-digit Telegram id is brute-forceable in seconds; the
    per-database random salt makes the reference useless without the DB."""
    return hmac.new(salt, f"{kind}:{raw_id}".encode(), hashlib.sha256).hexdigest()


class FeedbackEvent(BaseModel):
    """One validated, authorized feedback press, ready to persist."""

    model_config = ConfigDict(frozen=True)

    callback_query_id: str = Field(min_length=1, max_length=128)
    update_id: int = Field(ge=0)
    alert_decision_id: UUID
    label: FeedbackLabel
    user_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    chat_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    callback_version: str = CALLBACK_VERSION
    received_at_utc: datetime
    # Audit context (#69). Telegram gives no timestamp for the press itself;
    # message_date_utc is when Telegram says the alert message was sent.
    message_id: int | None = None
    message_date_utc: datetime | None = None

    @field_validator("received_at_utc")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("received_at_utc must be timezone-aware UTC")
        return value


class Rejection(BaseModel):
    """An update that is consumed (offset advances past it) but not stored.
    ``callback_query_id`` is set when the update was a callback, so the
    caller can still stop the button's loading spinner. The remaining
    optional fields carry whatever audit context was validated before the
    rejection (an unauthorized press never gets its payload decoded)."""

    model_config = ConfigDict(frozen=True)

    update_id: int
    reason: str
    callback_query_id: str | None = None
    message_id: int | None = None
    message_date_utc: datetime | None = None
    alert_decision_id: UUID | None = None
    label: FeedbackLabel | None = None


def classify_update(
    update: Mapping[str, Any],
    *,
    allowed_chat_id: str,
    allowed_user_ids: Collection[str],
    salt: bytes,
    now: datetime,
) -> FeedbackEvent | Rejection:
    """Validates and authorizes one Telegram Update. Never raises on bad
    input: every malformed or unauthorized update becomes a Rejection with a
    reason code that is safe to log (it never contains payload data)."""
    update_id = update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
        return Rejection(update_id=-1, reason="malformed_update")

    query = update.get("callback_query")
    if not isinstance(query, Mapping):
        return Rejection(update_id=update_id, reason="not_a_callback")
    query_id = query.get("id")
    if not isinstance(query_id, str) or not 0 < len(query_id) <= 128:
        return Rejection(update_id=update_id, reason="malformed_callback")

    context: dict[str, Any] = {}

    def reject(reason: str) -> Rejection:
        return Rejection(
            update_id=update_id,
            reason=reason,
            callback_query_id=query_id,
            **context,
        )

    sender = query.get("from")
    sender_id = sender.get("id") if isinstance(sender, Mapping) else None
    if not isinstance(sender_id, int) or isinstance(sender_id, bool):
        return reject("malformed_callback")
    message = query.get("message")
    chat = message.get("chat") if isinstance(message, Mapping) else None
    chat_id = chat.get("id") if isinstance(chat, Mapping) else None
    sent_at: datetime | None = None
    if isinstance(message, Mapping):
        message_id = message.get("message_id")
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            context["message_id"] = message_id
        message_date = message.get("date")
        if isinstance(message_date, int) and message_date > 0:
            try:
                sent_at = datetime.fromtimestamp(message_date, UTC)
            except (OverflowError, OSError, ValueError):
                sent_at = None
            else:
                context["message_date_utc"] = sent_at
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        # No message: an inline-mode button or a message Telegram no longer
        # exposes. Either way it can't be tied to our alert chat.
        return reject("unknown_chat")

    # Authorization before payload parsing: an outsider learns nothing about
    # which payloads are valid.
    if str(chat_id) != allowed_chat_id:
        return reject("unauthorized_chat")
    if str(sender_id) not in allowed_user_ids:
        return reject("unauthorized_user")

    try:
        decision_id, label = decode_callback(query.get("data"))
    except FeedbackPayloadError:
        return reject("malformed_payload")

    context["alert_decision_id"] = decision_id
    context["label"] = label
    if sent_at is not None and now - sent_at > MAX_ALERT_AGE:
        return reject("expired_alert")

    return FeedbackEvent(
        callback_query_id=query_id,
        update_id=update_id,
        alert_decision_id=decision_id,
        label=label,
        user_ref=pseudonymize(salt, "telegram-user", sender_id),
        chat_ref=pseudonymize(salt, "telegram-chat", chat_id),
        received_at_utc=now,
        message_id=context.get("message_id"),
        message_date_utc=sent_at,
    )


def parse_allowed_user_ids(raw: str | None) -> frozenset[str]:
    """Comma/whitespace separated numeric Telegram user ids; anything
    non-numeric is dropped so a typo can't widen the allowlist."""
    if not raw:
        return frozenset()
    tokens = raw.replace(",", " ").split()
    return frozenset(t for t in tokens if t.lstrip("-").isdigit())
