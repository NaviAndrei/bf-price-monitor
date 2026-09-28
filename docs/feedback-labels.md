# Alert feedback labels (T-28, #37)

Each Telegram deal alert can carry six feedback buttons. A press is stored as
a label against that alert's `AlertDecision` id in `data/price_history.db`,
and the stored labels are the training and evaluation data for the offline
anomaly pilot (#36).

| Button | Stored label |
|---|---|
| 👍 Util | `useful` |
| 👎 Reducere falsă | `fake_discount` |
| 🛒 Cumpărat | `purchased` |
| ❌ Alt produs | `wrong_product` |
| 💸 Preț greșit | `wrong_price` |
| 🔁 Duplicat | `duplicate` |

## Enabling it

Set the repository secret `TELEGRAM_FEEDBACK_ALLOWED_USER_IDS` to the
numeric Telegram user id(s) allowed to label alerts, comma-separated. Only
people in this allowlist, pressing buttons in the configured
`TELEGRAM_CHAT_ID`, are stored. While the secret is unset, both halves stay
off: `notify.py` sends no feedback buttons, and `scripts/feedback.py collect`
exits immediately without contacting Telegram.

## How a press is collected

The monitor runs as scheduled jobs with no server listening, so presses are
**pulled**. The `Collect alert feedback` step in `monitor.yml` runs
`scripts/feedback.py collect` right after the runtime-state restore:

1. It takes a single-consumer lease in the database. A second collector
   working on the same database skips the run.
2. It calls `getUpdates` with `allowed_updates=["callback_query"]`, so the
   bot never downloads chat messages. The stored offset (the last committed
   `update_id` plus one) is passed only if it was written in the last six
   days. Telegram restarts `update_id` at a random value after a week with no
   updates, and an older offset could silently confirm, and so lose, new
   presses.
3. It validates each press:
   - the callback format is `fb1:<32-hex decision id>:<label code>`, at most
     38 bytes, well under Telegram's 64-byte `callback_data` limit;
   - the chat must match `TELEGRAM_CHAT_ID`, and the user must be in the
     allowlist;
   - the alert can be at most 30 days old;
   - the decision id must belong to an alert this bot actually delivered to
     Telegram, which is checked against the `delivery_attempts` audit table.
4. It stores the whole batch **in one transaction**: new labels plus the
   consumer's `last_update_id`. `callback_query.id` is `UNIQUE`, so a
   redelivered press is counted as a duplicate and changes nothing.
5. Only after that commit does it confirm the offset to Telegram (with a
   `getUpdates` call using `offset = last + 1`). A crash between the commit
   and the confirmation just means Telegram redelivers the batch, which step
   4 absorbs.
6. It answers every callback best-effort, to stop the button's spinner.
   Stored presses get "Mulțumesc! Feedback salvat."; rejected presses get
   no text, which reveals nothing to someone outside the allowlist.

Output is counts only, for example
`[feedback] stored=1 duplicate=0 rejected={unauthorized_user=1}`. It never
contains payloads, user ids, usernames, chat ids or the bot token.

### Limits of scheduled polling

- **Delayed acknowledgement.** Telegram shows a spinner on a pressed button
  until the next scheduled run answers it. Answers to presses older than
  roughly 15 minutes fail with "query is too old". The label is still stored,
  and the run logs `unanswered_callbacks=N`.
- **24-hour retention.** Telegram keeps unconsumed updates for at most 24
  hours. Scheduled runs fire irregularly (about 4 to 5 of the 12 daily cron
  slots), so a gap longer than 24 hours between runs loses the presses made
  early in that gap.
- **One consumer per bot token.** While a webhook is set, `getUpdates`
  returns HTTP 409, and two concurrent pollers also get 409. The collector
  treats 409 as "skip this run" and advances nothing. Never configure a
  webhook for this bot, and never run the scheduled collector and the Docker
  worker against the same bot token at the same time.

### Always-on mode (Docker, #38)

`scripts/feedback.py collect --loop` is a long-polling worker (20-second
`getUpdates` timeout) that acknowledges presses within seconds. It needs no
inbound port, public URL or paid host. It renews its lease on every poll and
stops cleanly on SIGTERM or SIGINT. When it's in use, set
`TELEGRAM_FEEDBACK_ALLOWED_USER_IDS` only on the worker, which disables the
scheduled collector. A webhook receiver was considered and rejected because
it needs a publicly reachable HTTPS endpoint.

## Relabeling policy

Labels are append-only. Each (alert, rater) pair has one **current** label:
the press ingested most recently wins, and earlier presses remain in
`alert_feedback` as history. The `alert_feedback_current` view applies this
rule. Ingestion order follows `update_id` within a batch. `update_id` is kept
for audit only, because Telegram may restart it at a random value.

## Storage and privacy

- **Tables** (created by migration 1, `PRAGMA user_version = 1`):
  - `alert_feedback` holds the history.
  - `alert_feedback_current` is the view of current labels.
  - `feedback_consumer_state` holds the offset.
  - `feedback_consumer_lease` holds the single-consumer lease.
  - `feedback_settings` holds the pseudonym salt.
- **Pseudonyms.** Users and chats are stored as `HMAC-SHA256(salt, id)`,
  using a random 32-byte salt created inside the database. A plain hash of a
  Telegram id could be brute-forced; without the database file, these
  pseudonyms can't be.
- **What stays where.** Raw ids, usernames, message text and
  `callback_query.id` values never leave the runner-local database.
  `data/price_history.db` is git-ignored and is never uploaded as a workflow
  artifact.

## Query, export, retention, deletion

```bash
uv run python scripts/feedback.py summary                       # current label counts
uv run python scripts/feedback.py export                        # -> data/exports/feedback-<UTC>.jsonl
uv run python scripts/feedback.py export --history --since 2026-11-01 --out data/exports/nov.jsonl
uv run python scripts/feedback.py purge --before 2027-06-01     # retention
uv run python scripts/feedback.py forget-user --telegram-user-id <id>
```

- **Exports** contain `seq`, `alert_decision_id`, `label`,
  `callback_version`, `received_at_utc` and a `rater_N` alias that is
  generated fresh for each export. They contain no user, chat or callback
  identifiers. `data/exports/` is git-ignored; never commit an export or
  attach one to an issue.
- **Retention.** Labels are kept until the anomaly pilot (#36) evaluation is
  done. After that, purge anything older than 12 months with `purge`.
- **Deletion.** `forget-user` removes every label from one rater.
  `purge --before` removes everything received before a date.
- In code, the same queries are available as `list_feedback`,
  `feedback_summary`, `purge_feedback_before` and `delete_feedback_for_user`
  in `bf_price_monitor/storage/feedback.py`.

## Backup and recovery

Labels exist only in `data/price_history.db`, so they follow the #42
runtime-state path:

- **Backup.** `runtime_state.py save` copies the database with the SQLite
  backup API. The snapshot is promoted only if it passes `integrity_check`
  and its row count matches the live database for **every table**, including
  `alert_feedback`.
- **Restore.** `runtime_state.py restore` puts the last good snapshot back
  before the collector runs. The labels, the offset and the pseudonym salt
  all survive, which `test_feedback_labels_offset_and_salt_survive_save_and_restore`
  covers.
- **Snapshot loss.** If a snapshot is lost, labels collected since the
  previous good snapshot are lost too. Presses still unconsumed on Telegram,
  up to 24 hours old, are collected again by the next run.
- **Manual backup.** Before risky runner maintenance, take an export as an
  extra copy. An export can't be imported back automatically; it is only for
  evaluation data.
- **Migration rollback.** Migration 1 is additive, and older code ignores
  the new tables. If a restored database carries an older `user_version`, it
  is upgraded in place on first open. The upgrade runs in a single
  transaction, so a failed upgrade leaves the previous version intact.
