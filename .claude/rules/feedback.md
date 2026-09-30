---
paths:
  - "scripts/feedback.py"
  - "scripts/anomaly_pilot.py"
---

# Feedback labels and anomaly pilot rules

Sources: `docs/feedback-labels.md` (#37), `docs/anomaly-pilot.md` (#36). Read them before structural changes.

- **Counts only in output:** `feedback.py` output must never contain callback payloads, Telegram user ids, usernames, chat ids or the bot token.
- **Disabled unless configured:** `feedback.py collect` must exit 0 without any network call while `TELEGRAM_FEEDBACK_ALLOWED_USER_IDS` is unset.
- **Commit before confirm:** store the label batch (and the consumer offset) in one transaction, and confirm the offset to Telegram only after that commit. `callback_query.id` stays `UNIQUE` so redelivery is a no-op.
- **Anomaly pilot is offline and read-only:** `anomaly_pilot.py` opens `data/price_history.db` read-only, is never run by a workflow or the Docker image, and keeps the test split sealed unless `--unseal-test` is passed.
- **30-label gate:** precision/recall are reported only when a split has at least `min_labels` (30) real labels. Never fabricate or back-fill labels to pass the gate.
- **Detector output never overrides `rule_verdict`.** It is advisory only.
