"""Evaluation metrics for the anomaly pilot. Pure Python, no extra needed.

The label gate is deliberate: below `min_labels` real labels (or with only
one class present) precision, recall and PR-AUC are reported as blocked,
never estimated from unlabelled rows or from synthetic stand-ins.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

from bf_price_monitor.anomaly.dataset import FeatureRow

# Labels only exist for alerts the deterministic policy actually sent, so
# every labelled metric is conditional on that selection.
SELECTION_CAVEAT = (
    "Labels exist only for alerts the deterministic policy sent. Model "
    "recall is recall among labelled alerts, and deterministic recall is "
    "not estimable; model-flagged rows the policy never sent are unlabelled "
    "and counted separately, never treated as false positives."
)


def precision_recall(
    predicted: Sequence[bool], labels: Sequence[int]
) -> tuple[float | None, float | None]:
    tp = sum(1 for p, y in zip(predicted, labels, strict=True) if p and y == 1)
    fp = sum(1 for p, y in zip(predicted, labels, strict=True) if p and y == 0)
    fn = sum(1 for p, y in zip(predicted, labels, strict=True) if not p and y == 1)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return precision, recall


def average_precision(scores: Sequence[float], labels: Sequence[int]) -> float | None:
    """PR-AUC as average precision; higher score = more anomalous."""
    positives = sum(labels)
    if positives == 0:
        return None
    ranked = sorted(zip(scores, labels, strict=True), key=lambda pair: -pair[0])
    hits = 0
    total = 0.0
    for rank, (_, label) in enumerate(ranked, start=1):
        if label == 1:
            hits += 1
            total += hits / rank
    return total / positives


def bootstrap_ci(
    statistic: Callable[[list[int]], float | None],
    size: int,
    samples: int,
    seed: int,
) -> tuple[float, float] | None:
    """95% percentile interval of `statistic` over resampled row indices."""
    rng = random.Random(seed)
    values = []
    for _ in range(samples):
        value = statistic([rng.randrange(size) for _ in range(size)])
        if value is not None:
            values.append(value)
    if not values:
        return None
    values.sort()
    lo = values[int(0.025 * (len(values) - 1))]
    hi = values[int(0.975 * (len(values) - 1))]
    return round(lo, 4), round(hi, 4)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def label_gate(labels: Sequence[int], min_labels: int) -> str | None:
    """Why labelled metrics can't be reported, or None when they can."""
    if len(labels) < min_labels:
        return f"only {len(labels)} labelled rows; at least {min_labels} required"
    if len(set(labels)) < 2:
        return "labelled rows contain a single class"
    return None


def labelled_metrics(
    model_flags: Sequence[bool],
    anomaly_scores: Sequence[float],
    labels: Sequence[int],
    *,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    model_p, model_r = precision_recall(model_flags, labels)
    n = len(labels)

    def _model_precision(idx: list[int]) -> float | None:
        return precision_recall(
            [model_flags[i] for i in idx], [labels[i] for i in idx]
        )[0]

    def _model_recall(idx: list[int]) -> float | None:
        return precision_recall(
            [model_flags[i] for i in idx], [labels[i] for i in idx]
        )[1]

    def _deterministic_precision(idx: list[int]) -> float | None:
        return sum(labels[i] for i in idx) / len(idx)

    return {
        "model_precision": _round(model_p),
        "model_precision_ci95": bootstrap_ci(_model_precision, n, samples, seed),
        "model_recall_among_labelled": _round(model_r),
        "model_recall_ci95": bootstrap_ci(_model_recall, n, samples, seed),
        "model_pr_auc": _round(average_precision(anomaly_scores, labels)),
        # Every labelled row was a sent alert, so this is the share the
        # raters judged relevant.
        "deterministic_precision": _round(sum(labels) / n),
        "deterministic_precision_ci95": bootstrap_ci(
            _deterministic_precision, n, samples, seed
        ),
    }


def _volume(rows: Sequence[FeatureRow], flags: Sequence[bool]) -> dict[str, Any]:
    n = len(rows)
    model = sum(flags)
    return {
        "rows": n,
        "model_flagged": model,
        "model_flag_rate": _round(model / n) if n else None,
        "deterministic_alerts": sum(1 for r in rows if r.deterministic_alert),
    }


def evaluate_split(
    rows: Sequence[FeatureRow],
    if_flags: Sequence[bool],
    pelt_flags: Sequence[bool],
    anomaly_scores: Sequence[float],
    *,
    min_labels: int,
    bootstrap_samples: int,
    seed: int,
) -> dict[str, Any]:
    """Volumes, disagreement and (label-gated) metrics for one split.

    anomaly_scores: higher = more anomalous (negated score_samples).
    """
    ensemble = [a and b for a, b in zip(if_flags, pelt_flags, strict=True)]
    det = [r.deterministic_alert for r in rows]
    result: dict[str, Any] = _volume(rows, ensemble)
    result["isolation_forest_flagged"] = sum(if_flags)
    result["pelt_flagged"] = sum(pelt_flags)
    result["disagreement"] = {
        # The ensemble's own uncertainty flag: exactly one detector fired.
        "detectors_disagree": sum(
            1 for a, b in zip(if_flags, pelt_flags, strict=True) if a != b
        ),
        "model_and_deterministic": sum(
            1 for m, d in zip(ensemble, det, strict=True) if m and d
        ),
        "model_only": sum(1 for m, d in zip(ensemble, det, strict=True) if m and not d),
        "deterministic_only": sum(
            1 for m, d in zip(ensemble, det, strict=True) if d and not m
        ),
    }

    labelled = [i for i, r in enumerate(rows) if r.label is not None]
    labels = [label for r in rows if (label := r.label) is not None]
    result["labels"] = {
        "labelled_rows": len(labels),
        "positives": sum(labels),
        "negatives": len(labels) - sum(labels),
        "model_flagged_unlabelled": sum(
            1 for i, flag in enumerate(ensemble) if flag and rows[i].label is None
        ),
    }
    blocked = label_gate(labels, min_labels)
    if blocked:
        result["metrics"] = {"status": "blocked_insufficient_labels", "reason": blocked}
    else:
        result["metrics"] = {
            "status": "evaluated",
            **labelled_metrics(
                [ensemble[i] for i in labelled],
                [anomaly_scores[i] for i in labelled],
                labels,
                samples=bootstrap_samples,
                seed=seed,
            ),
        }

    by_retailer: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        by_retailer[row.retailer].append(i)
    result["slices"] = {
        retailer: {
            **_volume([rows[i] for i in idx], [ensemble[i] for i in idx]),
            "labelled_rows": sum(1 for i in idx if rows[i].label is not None),
        }
        for retailer, idx in sorted(by_retailer.items())
    }
    return result
