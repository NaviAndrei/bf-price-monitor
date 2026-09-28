"""T-29 (#36): label-gated evaluation metrics. Pure Python."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bf_price_monitor.anomaly.dataset import FeatureRow
from bf_price_monitor.anomaly.metrics import (
    average_precision,
    bootstrap_ci,
    evaluate_split,
    label_gate,
    precision_recall,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _row(retailer="emag", deterministic=False, label=None):
    return FeatureRow(
        offer_id="o",
        retailer=retailer,
        scraped_at=T0,
        price=1.0,
        features={},
        history=(1.0,),
        decision_ids=(),
        deterministic_alert=deterministic,
        label=label,
    )


def test_precision_recall_known_values():
    assert precision_recall([True, True, False, False], [1, 0, 1, 0]) == (0.5, 0.5)
    assert precision_recall([False, False], [0, 0]) == (None, None)


def test_average_precision_known_values():
    assert average_precision([0.9, 0.8, 0.1], [1, 1, 0]) == pytest.approx(1.0)
    # ranks: 0 (neg), 1 (pos) -> precision at the only hit is 1/2
    assert average_precision([0.9, 0.1], [0, 1]) == pytest.approx(0.5)
    assert average_precision([0.5], [0]) is None


def test_bootstrap_ci_is_seeded_and_bounded():
    labels = [1, 0, 1, 1, 0, 1, 0, 1]

    def mean(idx):
        return sum(labels[i] for i in idx) / len(idx)

    a = bootstrap_ci(mean, len(labels), 200, seed=1)
    assert a == bootstrap_ci(mean, len(labels), 200, seed=1)
    assert a is not None and 0 <= a[0] <= 0.625 <= a[1] <= 1
    assert bootstrap_ci(lambda idx: None, 3, 10, seed=1) is None


def test_label_gate():
    assert "at least 30" in label_gate([1] * 10, 30)
    assert label_gate([1] * 40, 30) == "labelled rows contain a single class"
    assert label_gate([1, 0] * 20, 30) is None


def test_evaluate_split_blocks_metrics_without_real_labels():
    rows = [_row(deterministic=True), _row(), _row(retailer="flanco")]
    result = evaluate_split(
        rows,
        [True, True, False],
        [True, False, False],
        [0.9, 0.8, 0.1],
        min_labels=30,
        bootstrap_samples=10,
        seed=1,
    )
    assert result["metrics"]["status"] == "blocked_insufficient_labels"
    assert "model_precision" not in result["metrics"]
    assert result["model_flagged"] == 1
    assert result["isolation_forest_flagged"] == 2
    assert result["disagreement"] == {
        "detectors_disagree": 1,
        "model_and_deterministic": 1,
        "model_only": 0,
        "deterministic_only": 0,
    }
    assert result["labels"]["model_flagged_unlabelled"] == 1
    assert set(result["slices"]) == {"emag", "flanco"}


def test_evaluate_split_reports_metrics_once_the_gate_passes():
    # 20 relevant + 20 not-relevant labelled alerts; the model flags the
    # first 15 of each class, and scores relevant ones higher.
    rows = [_row(deterministic=True, label=1) for _ in range(20)]
    rows += [_row(deterministic=True, label=0) for _ in range(20)]
    flags = [i < 15 or 20 <= i < 35 for i in range(40)]
    scores = [1.0 - i / 100 for i in range(40)]
    result = evaluate_split(
        rows, flags, flags, scores, min_labels=30, bootstrap_samples=50, seed=1
    )
    metrics = result["metrics"]
    assert metrics["status"] == "evaluated"
    assert metrics["model_precision"] == 0.5
    assert metrics["model_recall_among_labelled"] == 0.75
    assert metrics["deterministic_precision"] == 0.5
    assert metrics["model_pr_auc"] == pytest.approx(1.0)
    assert metrics["model_precision_ci95"] is not None
