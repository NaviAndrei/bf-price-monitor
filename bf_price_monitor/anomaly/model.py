"""Isolation Forest + PELT ensemble. Requires the optional `anomaly` extra.

Fit, threshold selection and inference are separate steps so the
chronological discipline is visible in code:
- preprocessing (RobustScaler) and the forest are fit on train only;
- the threshold is chosen on validation only (or fixed by config);
- the test split stays sealed unless explicitly unsealed.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any

import numpy as np
import ruptures as rpt
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import RobustScaler

from bf_price_monitor.anomaly.config import PILOT_VERSION, AnomalyConfig
from bf_price_monitor.anomaly.dataset import FeatureRow, Split
from bf_price_monitor.anomaly.metrics import evaluate_split, label_gate


class InsufficientLabelsError(ValueError):
    pass


def feature_matrix(rows: Sequence[FeatureRow], cfg: AnomalyConfig) -> Any:
    return np.array(
        [[row.features[name] for name in cfg.features] for row in rows], dtype=float
    )


def fit_isolation_forest(train: Sequence[FeatureRow], cfg: AnomalyConfig) -> Pipeline:
    pipeline = Pipeline(
        [
            ("scale", RobustScaler()),
            (
                "forest",
                IsolationForest(
                    n_estimators=cfg.n_estimators,
                    contamination=cfg.contamination,
                    random_state=cfg.random_state,
                ),
            ),
        ]
    )
    pipeline.fit(feature_matrix(train, cfg))
    return pipeline


def score_rows(
    pipeline: Pipeline, rows: Sequence[FeatureRow], cfg: AnomalyConfig
) -> list[float]:
    """score_samples(): in [-1, 0), lower = more anomalous."""
    if not rows:
        return []
    return [float(s) for s in pipeline.score_samples(feature_matrix(rows, cfg))]


def select_threshold(
    cfg: AnomalyConfig, validation: Sequence[FeatureRow], scores: Sequence[float]
) -> dict[str, Any]:
    if cfg.threshold_mode == "fixed":
        return {"value": cfg.score_threshold, "mode": "fixed", "fit_on": "config"}
    if cfg.threshold_mode == "validation_quantile":
        value = float(np.quantile(np.asarray(scores), cfg.contamination))
        return {"value": value, "mode": "validation_quantile", "fit_on": "validation"}

    labelled = [
        (score, row.label)
        for row, score in zip(validation, scores, strict=True)
        if row.label is not None
    ]
    blocked = label_gate([label for _, label in labelled], cfg.min_labels)
    if blocked:
        raise InsufficientLabelsError(f"validation_f1 threshold refused: {blocked}")
    best = (-1.0, cfg.score_threshold)
    for candidate in sorted({score for score, _ in labelled}):
        tp = sum(1 for s, y in labelled if s <= candidate and y == 1)
        fp = sum(1 for s, y in labelled if s <= candidate and y == 0)
        fn = sum(1 for s, y in labelled if s > candidate and y == 1)
        f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
        if f1 > best[0]:
            best = (f1, candidate)
    return {
        "value": best[1],
        "mode": "validation_f1",
        "fit_on": "validation",
        "validation_f1": round(best[0], 4),
    }


def pelt_active_negative(history: Sequence[float], cfg: AnomalyConfig) -> bool:
    """True when PELT finds a recent downward regime shift in `history`.

    Runs on the offer's prefix only (causal). Prices are divided by the
    prefix median so one penalty works for a 100 RON and a 10,000 RON item.
    """
    n = len(history)
    if n < 2 * cfg.pelt_min_size:
        return False
    median = statistics.median(history)
    if median <= 0:
        return False
    signal = (np.asarray(history, dtype=float) / median).reshape(-1, 1)
    algo = rpt.Pelt(
        model=cfg.pelt_model, min_size=cfg.pelt_min_size, jump=cfg.pelt_jump
    )
    breakpoints = algo.fit(signal).predict(pen=cfg.pelt_penalty)
    if len(breakpoints) < 2:
        return False
    change = breakpoints[-2]
    previous_start = breakpoints[-3] if len(breakpoints) >= 3 else 0
    if n - change > cfg.pelt_active_window:
        return False
    return float(signal[change:].mean()) < float(signal[previous_start:change].mean())


def environment_fingerprint() -> dict[str, str]:
    return {
        "pilot_version": PILOT_VERSION,
        "scikit-learn": version("scikit-learn"),
        "ruptures": version("ruptures"),
        "numpy": version("numpy"),
    }


def _top_flagged(
    rows: Sequence[FeatureRow],
    scores: Sequence[float],
    flags: Sequence[bool],
    limit: int = 20,
) -> list[dict[str, Any]]:
    # Offer ids and prices only: no URLs or titles in the report.
    picked = sorted(
        (i for i, flag in enumerate(flags) if flag), key=lambda i: scores[i]
    )[:limit]
    return [
        {
            "offer_id": rows[i].offer_id,
            "retailer": rows[i].retailer,
            "scraped_at_utc": rows[i].scraped_at.isoformat(),
            "price": rows[i].price,
            "if_score": round(scores[i], 4),
            "deterministic_alert": rows[i].deterministic_alert,
            "label": rows[i].label,
        }
        for i in picked
    ]


def run_pilot(
    split: Split, cfg: AnomalyConfig, *, unseal_test: bool = False
) -> dict[str, Any]:
    pipeline = fit_isolation_forest(split.train, cfg)
    val_scores = score_rows(pipeline, split.validation, cfg)
    threshold = select_threshold(cfg, split.validation, val_scores)

    def _evaluate(rows: Sequence[FeatureRow], scores: list[float]) -> dict[str, Any]:
        if_flags = [s < threshold["value"] for s in scores]
        pelt_flags = [pelt_active_negative(r.history, cfg) for r in rows]
        evaluation = evaluate_split(
            rows,
            if_flags,
            pelt_flags,
            [-s for s in scores],
            min_labels=cfg.min_labels,
            bootstrap_samples=cfg.bootstrap_samples,
            seed=cfg.random_state,
        )
        ensemble = [a and b for a, b in zip(if_flags, pelt_flags, strict=True)]
        evaluation["top_flagged"] = _top_flagged(rows, scores, ensemble)
        return evaluation

    train_scores = score_rows(pipeline, split.train, cfg)
    train_if = [s < threshold["value"] for s in train_scores]
    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "environment": environment_fingerprint(),
        "threshold": threshold,
        "splits": {
            "train_rows": len(split.train),
            "validation_rows": len(split.validation),
            "test_rows": len(split.test),
            "validation_start_utc": split.validation_start.isoformat(),
            "test_start_utc": split.test_start.isoformat(),
        },
        # Train is the fit set: volume only, never quality metrics.
        "train": {
            "rows": len(split.train),
            "isolation_forest_flagged": sum(train_if),
        },
        "validation": _evaluate(split.validation, val_scores),
    }
    if unseal_test:
        report["test"] = _evaluate(split.test, score_rows(pipeline, split.test, cfg))
    else:
        report["test"] = {"status": "sealed", "rows": len(split.test)}
    return report
