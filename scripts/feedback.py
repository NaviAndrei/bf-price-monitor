"""Alert feedback labels CLI (T-28, #37).

    uv run python scripts/feedback.py collect            # one poll (monitor.yml)
    uv run python scripts/feedback.py collect --loop     # always-on worker (Docker)
    uv run python scripts/feedback.py summary
    uv run python scripts/feedback.py export --out data/exports/feedback.jsonl
    uv run python scripts/feedback.py purge --before 2027-01-01
    uv run python scripts/feedback.py forget-user --telegram-user-id <id>

`collect` pulls pending inline-button presses with Telegram getUpdates,
stores authorized ones in data/price_history.db, confirms the offset to
Telegram only after that commit, then answers each callback best-effort.
It is disabled (exit 0, no network) unless TELEGRAM_FEEDBACK_ALLOWED_USER_IDS
is set. Output is counts only: never payloads, user ids, chat ids or tokens.
See docs/feedback-labels.md.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bf_price_monitor.feedback import (
    FeedbackEvent,
    Rejection,
    classify_update,
    parse_allowed_user_ids,
    pseudonymize,
)
from bf_price_monitor.storage import feedback as feedback_store
from bf_price_monitor.storage import sqlite as sqlite_storage

DB_FILE = Path("data/price_history.db")
EXPORT_DIR = Path("data/exports")
CONSUMER = "telegram-getupdates"
# Telegram may restart update ids at a random value after a week with no
# updates; past this age the stored offset is not trusted as a lower bound.
OFFSET_TRUST_WINDOW = timedelta(days=6)
LOOP_POLL_TIMEOUT_SECONDS = 20
LOOP_ERROR_BACKOFF_SECONDS = 30.0
HTTP_TIMEOUT_SLACK_SECONDS = 10

ACK_TEXT = {
    "stored": "Mulțumesc! Feedback salvat.",
    "duplicate": "Feedback deja înregistrat.",
    "rejected": "",
}


class TelegramError(RuntimeError):
    """A getUpdates call failed; the message is a safe, token-free reason."""


class TelegramConflict(TelegramError):
    """HTTP 409: a webhook is set or another getUpdates consumer is active."""


class TelegramFeedbackClient:
    def __init__(self, bot_token: str, http: Any = requests) -> None:
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._http = http

    def _post(self, method: str, payload: dict[str, Any], timeout: float) -> Any:
        try:
            response = self._http.post(
                f"{self._base}/{method}", json=payload, timeout=timeout
            )
        except requests.RequestException as exc:
            # Never str(exc): request exceptions embed the URL, i.e. the token.
            raise TelegramError(f"{method}: {exc.__class__.__name__}") from None
        if response.status_code == 409:
            raise TelegramConflict(f"{method}: HTTP 409")
        if response.status_code >= 400:
            raise TelegramError(f"{method}: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(f"{method}: non-JSON response") from None
        if not isinstance(body, Mapping) or body.get("ok") is not True:
            raise TelegramError(f"{method}: ok=false")
        return body.get("result")

    def get_updates(
        self, offset: int | None, *, timeout: int, limit: int = 100
    ) -> list[Any]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            # Only button presses: the bot never downloads chat messages.
            "allowed_updates": ["callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._post("getUpdates", payload, timeout + HTTP_TIMEOUT_SLACK_SECONDS)
        if not isinstance(result, list):
            raise TelegramError("getUpdates: result is not a list")
        return result

    def answer_callback(self, callback_query_id: str, text: str) -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            self._post("answerCallbackQuery", payload, HTTP_TIMEOUT_SLACK_SECONDS)
        except TelegramError:
            return False
        return True


def _log(message: str) -> None:
    print(f"[feedback] {message}", flush=True)


def _format_result(result: feedback_store.IngestResult) -> str:
    rejected = ",".join(f"{k}={v}" for k, v in sorted(result.rejected.items()))
    return (
        f"stored={result.stored} duplicate={result.duplicate} rejected={{{rejected}}}"
    )


def collect_once(
    db: Any,
    client: TelegramFeedbackClient,
    *,
    chat_id: str,
    allowed_user_ids: frozenset[str],
    holder: str,
    poll_timeout: int = 0,
    lease_ttl: timedelta = timedelta(minutes=5),
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> feedback_store.IngestResult | None:
    """One poll cycle. Returns None when another consumer holds the lease.
    Order matters: fetch -> commit labels + offset (one transaction) ->
    confirm offset to Telegram -> answer callbacks. A crash after the commit
    but before the confirm just means Telegram redelivers the batch, which
    ingest_batch treats as duplicates."""
    if not feedback_store.acquire_lease(
        db, CONSUMER, holder, now=now_fn(), ttl=lease_ttl
    ):
        _log("skipped: another feedback consumer holds the lease")
        return None

    now = now_fn()
    state = feedback_store.get_consumer_state(db, CONSUMER)
    offset = None
    if state is not None and now - state[1] < OFFSET_TRUST_WINDOW:
        offset = state[0] + 1
    updates = client.get_updates(offset, timeout=poll_timeout)
    if not updates:
        return feedback_store.IngestResult()

    salt = feedback_store.get_pseudonym_salt(db)
    received_at = now_fn()
    items: list[FeedbackEvent | Rejection] = [
        classify_update(
            update,
            allowed_chat_id=chat_id,
            allowed_user_ids=allowed_user_ids,
            salt=salt,
            now=received_at,
        )
        if isinstance(update, Mapping)
        else Rejection(update_id=-1, reason="malformed_update")
        for update in updates
    ]
    result = feedback_store.ingest_batch(db, CONSUMER, items, now=received_at)

    if result.last_update_id is not None:
        try:
            client.get_updates(result.last_update_id + 1, timeout=0, limit=1)
        except TelegramError as exc:
            _log(f"offset confirm deferred to next poll ({exc})")

    unanswered = sum(
        not client.answer_callback(query_id, ACK_TEXT[outcome])
        for query_id, outcome in result.callback_outcomes
    )
    suffix = f" unanswered_callbacks={unanswered}" if unanswered else ""
    _log(_format_result(result) + suffix)
    return result


def _feedback_config(env: Mapping[str, str]) -> tuple[str, str, frozenset[str]] | None:
    allowed = parse_allowed_user_ids(env.get("TELEGRAM_FEEDBACK_ALLOWED_USER_IDS"))
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not allowed:
        _log("disabled: TELEGRAM_FEEDBACK_ALLOWED_USER_IDS is not set")
        return None
    if not token or not chat_id:
        _log("disabled: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
        return None
    return token, chat_id, allowed


def _cmd_collect(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    config = _feedback_config(env)
    if config is None:
        return 0
    token, chat_id, allowed = config
    client = TelegramFeedbackClient(token)
    holder = f"{'worker' if args.loop else 'scheduled'}-{uuid4().hex[:12]}"
    db = sqlite_storage.init_db(args.db)
    try:
        if not args.loop:
            try:
                collect_once(
                    db,
                    client,
                    chat_id=chat_id,
                    allowed_user_ids=allowed,
                    holder=holder,
                )
            except TelegramConflict:
                _log("skipped: HTTP 409 (webhook set or another poller active)")
            except TelegramError as exc:
                _log(f"poll failed: {exc}")
                return 1
            finally:
                feedback_store.release_lease(db, CONSUMER, holder)
            return 0
        return _run_worker(db, client, chat_id, allowed, holder)
    finally:
        db.close()


def _run_worker(
    db: Any,
    client: TelegramFeedbackClient,
    chat_id: str,
    allowed: frozenset[str],
    holder: str,
) -> int:
    """Always-on long-polling worker for Docker mode: near-immediate button
    acknowledgement with no inbound port or external host. Stops cleanly on
    SIGTERM/SIGINT between polls (a poll lasts at most
    LOOP_POLL_TIMEOUT_SECONDS)."""
    stop = threading.Event()

    def _request_stop(signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    _log("worker started")
    try:
        while not stop.is_set():
            try:
                collect_once(
                    db,
                    client,
                    chat_id=chat_id,
                    allowed_user_ids=allowed,
                    holder=holder,
                    poll_timeout=LOOP_POLL_TIMEOUT_SECONDS,
                    lease_ttl=timedelta(seconds=LOOP_POLL_TIMEOUT_SECONDS * 3),
                )
            except TelegramError as exc:
                _log(
                    f"poll failed: {exc}; retrying in {LOOP_ERROR_BACKOFF_SECONDS:.0f}s"
                )
                stop.wait(LOOP_ERROR_BACKOFF_SECONDS)
    finally:
        feedback_store.release_lease(db, CONSUMER, holder)
        _log("worker stopped")
    return 0


def _pseudonymize_raters(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replaces the stable user_ref with a per-export rater_N alias, so an
    exported file can't be joined back to the database's pseudonyms."""
    aliases: dict[str, str] = {}
    out = []
    for row in rows:
        alias = aliases.setdefault(row["user_ref"], f"rater_{len(aliases) + 1}")
        out.append(
            {**{k: v for k, v in row.items() if k != "user_ref"}, "rater": alias}
        )
    return out


def _cmd_export(args: argparse.Namespace) -> int:
    out: Path = args.out or EXPORT_DIR / (
        f"feedback-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    )
    since = datetime.fromisoformat(args.since) if args.since else None
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    db = sqlite_storage.init_db(args.db)
    try:
        rows = feedback_store.list_feedback(
            db, current_only=not args.history, since=since
        )
    finally:
        db.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for row in _pseudonymize_raters(rows):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _log(f"exported {len(rows)} label(s) to {out}")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    db = sqlite_storage.init_db(args.db)
    try:
        summary = feedback_store.feedback_summary(db)
    finally:
        db.close()
    total = sum(summary.values())
    _log(f"current labels: {total}")
    for label, count in summary.items():
        _log(f"  {label}: {count}")
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    cutoff = datetime.fromisoformat(args.before)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    db = sqlite_storage.init_db(args.db)
    try:
        deleted = feedback_store.purge_feedback_before(db, cutoff)
    finally:
        db.close()
    _log(f"purged {deleted} label(s) received before {cutoff.isoformat()}")
    return 0


def _cmd_forget_user(args: argparse.Namespace) -> int:
    db = sqlite_storage.init_db(args.db)
    try:
        salt = feedback_store.get_pseudonym_salt(db)
        user_ref = pseudonymize(salt, "telegram-user", args.telegram_user_id)
        deleted = feedback_store.delete_feedback_for_user(db, user_ref)
    finally:
        db.close()
    _log(f"deleted {deleted} label(s) for the given user")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alert feedback labels (#37)")
    parser.add_argument("--db", type=Path, default=DB_FILE)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="poll Telegram for button presses")
    collect.add_argument("--loop", action="store_true", help="always-on worker")
    export = sub.add_parser("export", help="privacy-safe JSONL export")
    export.add_argument("--out", type=Path)
    export.add_argument("--history", action="store_true", help="include relabels")
    export.add_argument("--since", help="ISO date/time (UTC if no offset)")
    sub.add_parser("summary", help="current label counts")
    purge = sub.add_parser("purge", help="delete labels received before a date")
    purge.add_argument("--before", required=True, help="ISO date/time")
    forget = sub.add_parser("forget-user", help="delete one rater's labels")
    forget.add_argument("--telegram-user-id", required=True, type=int)
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "collect":
        return _cmd_collect(args, os.environ if env is None else env)
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "summary":
        return _cmd_summary(args)
    if args.command == "purge":
        return _cmd_purge(args)
    return _cmd_forget_user(args)


if __name__ == "__main__":
    raise SystemExit(main())
