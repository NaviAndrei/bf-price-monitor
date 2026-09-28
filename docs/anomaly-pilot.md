# Offline anomaly pilot (T-29, #36)

**Status: blocked on real labels.** The tooling is built and tested, but the
precision/recall comparison against the deterministic policy that #36 asks
for **cannot be written yet**. It needs real Telegram feedback labels (#37),
and none exist yet. This document is the evaluation plan and a data
profile. It is not an evaluation result.

## Offline only

- `bf_price_monitor/anomaly/` and `scripts/anomaly_pilot.py` are never
  imported by `scrape.py`, `analyze.py`, `notify.py`, `feedback.py` or
  `run_loop.py`. They are never installed by `monitor.yml` or the Docker
  image. A unit test asserts both.
- scikit-learn and ruptures live in an optional `anomaly` extra, locked in
  `uv.lock`. The monitor installs with `uv sync --frozen`, which installs no
  extras. The Docker build verifies that neither package is in the image.
- The pilot opens `data/price_history.db` with SQLite `mode=ro`. It can't
  migrate or write the monitor's database, and a test checks that the file
  bytes are unchanged afterwards.
- Reports go to `data/exports/` (git-ignored). They contain offer ids,
  retailers, prices and timestamps. They contain no URLs, titles or rater
  identities.

## Running it

```bash
uv sync --frozen --extra dev --extra anomaly
uv run --no-sync python scripts/anomaly_pilot.py
uv run --no-sync python scripts/anomaly_pilot.py --config pilot.json   # override hypotheses
uv run --no-sync python scripts/anomaly_pilot.py --unseal-test         # final run only
uv sync --frozen --extra dev          # return to the normal dev environment
```

CI: the `anomaly-pilot` job in `quality.yml` installs the extra and runs
the pilot tests. `REQUIRE_ANOMALY_EXTRA=1` makes a missing extra fail that
job instead of skipping the tests.

## Design

**Features** (`dataset.py`). One row per observation that has at least one
prior observation of the same offer. Every feature uses only data at or
before the row's own timestamp:

| Feature | Definition |
|---|---|
| `drop_pct` | % change from the previous observation (positive = cheaper) |
| `relative_to_30d_median` | price ÷ median of the prior 30 days − 1 |
| `volatility_score_7d` | stdev ÷ mean of the last 7 days, including this row |
| `hour_of_day` | UTC hour |
| `days_since_last_change` | days since the latest price change before this row |

**Chronological split.** Rows are split 60/20/20 by time. The boundaries
are timestamps, so rows that share a timestamp never straddle two splits.
- **Train:** the RobustScaler and the Isolation Forest are fit here only.
- **Validation:** the threshold is chosen here only.
- **Test:** sealed. The report records only its row count until
  `--unseal-test` is passed, and the report says whether it was.

**Ensemble** (`model.py`):
- **Isolation Forest.** `score_samples()` is the negated paper score, and
  lower means more anomalous (checked against the scikit-learn 1.9.1
  source). A row is flagged when its score is below the threshold.
- **PELT** (ruptures) runs on the offer's price history up to the row,
  divided by that history's median. The row is flagged when the latest
  change point is within `pelt_active_window` observations and the new
  segment's mean is lower than the previous segment's.
- **Confirmed anomaly:** both detectors flag the row. Rows where exactly
  one detector fires are reported as disagreement, the ensemble's
  uncertainty signal.

**Every parameter is a hypothesis** (`config.py`, `AnomalyConfig`), and
each report records it under `config` and `config_fingerprint`:

| Parameter | Default | Source |
|---|---|---|
| `contamination` | 0.03 | #36 blueprint |
| `score_threshold` | −0.65 | #36 blueprint |
| `pelt_model`, `pelt_min_size` | `l2`, 3 | #36 blueprint |
| `pelt_penalty` | 0.01 | not given in the issue; chosen for median-normalised prices |
| `pelt_active_window` | 7 | not given in the issue |
| `threshold_mode` | `fixed` | the other modes are `validation_quantile` (needs no labels) and `validation_f1` (refuses to run without labels) |

With `min_size=3`, PELT can't place a change point in the last 3
observations. A drop is therefore confirmed only once it has lasted 3
scrapes. That is inherent to the blueprint's parameters.

## Labels and the deterministic baseline

- **Deterministic decisions** are the AlertDecision ids in
  `delivery_attempts`, the #55 audit trail. The rule engine is not
  re-implemented here.
- **Labels** come from `alert_feedback_current` (#37), which holds each
  rater's latest label. The explicit relevance policy is:
  - `useful` and `purchased` count as relevant (1).
  - `fake_discount` and `wrong_price` count as not relevant (0).
  - `wrong_product` and `duplicate` are excluded, because they judge
    identity, not price.

  A majority vote across raters decides each alert, and ties are dropped.
- **The join.** Each observation is mapped to the id `notify.py` would give
  it: `uuid5("alert-decision:" + sha256(url:price:site))`. A test pins the
  two formulas together. The report counts labelled alerts that matched no
  observation, so a join failure is visible.
- **Selection bias, stated up front.** Labels exist only for alerts the
  deterministic policy sent. That has three consequences:
  - Model recall is recall among labelled alerts.
  - Deterministic recall can't be estimated at all.
  - Rows the model flags but the policy never sent have no label. They are
    counted, never treated as false positives.
- **The label gate.** Precision, recall, PR-AUC and the bootstrap 95%
  intervals are computed only when a split has at least `min_labels` (30)
  labelled rows and both classes are present. Otherwise the report says
  `blocked_insufficient_labels`. Nothing is imputed, and synthetic labels
  exist only inside unit tests.

## Data profile (local development copy, not the production runner)

A run on 2026-09-28 against the development machine's copy of
`data/price_history.db`, with data through 2026-09-27:

| | |
|---|---|
| Observations / offers | 4,099 / 295 (eMAG 2,702, PC Garage 1,041, Flanco 356 observations) |
| Feature rows | 3,804 |
| Split | train 2,276 · validation 767 (from 2026-09-20) · test 761 (from 2026-09-25, sealed) |
| Timestamps at exactly 00:00 UTC | 41.8%: migrated JSON history is dated, not timed, so `hour_of_day` is an artifact on those rows |
| Sent alerts (`delivery_attempts`) | 0 in this copy |
| Feedback labels | 0 |
| Validation, fixed −0.65 threshold | Isolation Forest flagged 7, PELT 25, both 2; detectors disagree on 28 |
| Status | `blocked_insufficient_labels` |

These counts describe detector volume only. They say nothing about
precision, and they are not a comparison with the deterministic policy.

## To finish #36

1. Enable feedback collection: add the `TELEGRAM_FEEDBACK_ALLOWED_USER_IDS`
   secret (see docs/feedback-labels.md) and rate real alerts.
2. Once `feedback.py summary` shows roughly 30 or more relevant/not-relevant
   labels in the validation period, run the pilot on the runner's
   database.
3. Optionally rerun with `threshold_mode=validation_f1`. Then run once
   with `--unseal-test` and write the precision/recall comparison against
   the deterministic policy into this document.
4. Only then consider any production use. That would need its own issue.
   Detector output must never override `rule_verdict`.
