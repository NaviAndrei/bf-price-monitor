---
name: feedback-label-report
description: Report the current alert-feedback label counts per label, the gap to the 30-label minimum for the #36 anomaly pilot, and the callback audit since a date. Counts only, never user ids. Use when checking whether the anomaly pilot can be unblocked.
disable-model-invocation: true
allowed-tools: Bash(python scripts/feedback.py summary), Bash(python scripts/feedback.py audit *), Read
---

# Feedback label report

Counts only. Follow `.claude/rules/feedback.md`: never print callback payloads, Telegram
user ids, usernames, chat ids or the bot token, and never fabricate or back-fill labels.

Note: `/check-bf-readiness` already covers two of this skill's checks (stored label count
and the 30-label gap). Use this skill when you also want the per-label breakdown and the
callback audit.

## Commands (verified against `scripts/feedback.py`)

`--db` is a top-level option and goes before the subcommand if you need it; `--since` is
required for `audit`.

```bash
python scripts/feedback.py summary
python scripts/feedback.py audit --since <ISO-8601 date or datetime>
```

- `summary` prints `[feedback] current labels: N`, then one `label: count` line per label.
- `audit --since` prints the callback metrics line (`callbacks_received`, `_stored`,
  `_duplicate`, `_rejected`, `_answered`, `_answer_failed`) and, when callbacks exist, one
  explained line per callback. Those lines show a 12-character hash of the callback id and
  an 8-character alert id, not user ids.

Both commands read the local database `data/price_history.db`. That file is gitignored and
is whatever this machine has; the production collector runs in GitHub Actions. If the local
count is 0 while production has labels, say so rather than treating 0 as the real count.
Never run `collect`, `export`, `purge` or `forget-user`.

## Procedure

1. Run `summary`. Record the total and each label's count.
2. Run `audit --since` with the date the user gives, or a default of 7 days ago.
3. Compute the gap: `max(0, 30 - total)`. The 30 is `min_labels` in
   `bf_price_monitor/anomaly/config.py`, applied **per data split** by the anomaly pilot
   (`docs/anomaly-pilot.md`). A total of 30 or more does not by itself pass the gate for
   every split; say so when total is 30 or more.
4. Report in this shape, nothing more:

```
Labels: <total>   (<label>: <n>, ... one per label)
Gap to 30: <n>
Audit since <date>: received=<n> stored=<n> duplicate=<n> rejected=<n> answered=<n> answer_failed=<n>
Source: local data/price_history.db (may differ from the production collector)
```

If a command fails, print its error and stop; do not infer counts.
