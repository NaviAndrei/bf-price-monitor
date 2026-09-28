"""Pilot configuration. Every numeric default is a hypothesis to test.

contamination=0.03, score_threshold=-0.65 and the PELT settings come from
the #36 blueprint, not from any fit to this project's data. They are
recorded in each report's config fingerprint so a result can always be
traced to the exact values that produced it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

PILOT_VERSION = "anomaly-pilot-v1"

FEATURES: tuple[str, ...] = (
    "drop_pct",
    "relative_to_30d_median",
    "volatility_score_7d",
    "hour_of_day",
    "days_since_last_change",
)

ThresholdMode = Literal["fixed", "validation_quantile", "validation_f1"]


@dataclass(frozen=True)
class AnomalyConfig:
    features: tuple[str, ...] = FEATURES
    # Isolation Forest. score_samples() is the negated paper score, in
    # [-1, 0); lower means more anomalous.
    contamination: float = 0.03
    score_threshold: float = -0.65
    n_estimators: int = 200
    random_state: int = 29
    # fixed: use score_threshold as given.
    # validation_quantile: the validation-split score quantile that flags
    #   `contamination` of validation rows (needs no labels).
    # validation_f1: best F1 on labelled validation rows; refuses to run
    #   without enough real labels.
    threshold_mode: ThresholdMode = "fixed"
    # PELT on each offer's price prefix, normalised by its median so one
    # penalty works across price levels.
    pelt_model: str = "l2"
    pelt_min_size: int = 3
    pelt_jump: int = 1
    pelt_penalty: float = 0.01
    # A change point is "active" when it lies within this many
    # observations of the one being scored.
    pelt_active_window: int = 7
    # Chronological split fractions by row count; test is the remainder.
    train_fraction: float = 0.6
    validation_fraction: float = 0.2
    # Below this many labelled rows (with both classes present) in a split,
    # precision/recall are reported as blocked, never estimated.
    min_labels: int = 30
    bootstrap_samples: int = 1000

    def __post_init__(self) -> None:
        unknown = set(self.features) - set(FEATURES)
        if unknown or not self.features:
            raise ValueError(f"unknown or empty features: {sorted(unknown)}")
        if not 0 < self.contamination <= 0.5:
            raise ValueError("contamination must be in (0, 0.5]")
        if not -1 <= self.score_threshold <= 0:
            raise ValueError("score_threshold must be in [-1, 0]")
        if self.threshold_mode not in ("fixed", "validation_quantile", "validation_f1"):
            raise ValueError(f"unknown threshold_mode: {self.threshold_mode}")
        if self.pelt_min_size < 1 or self.pelt_jump < 1 or self.pelt_penalty <= 0:
            raise ValueError("PELT min_size/jump must be >= 1 and penalty > 0")
        if self.pelt_active_window < 1:
            raise ValueError("pelt_active_window must be >= 1")
        if not (
            0 < self.train_fraction
            and 0 < self.validation_fraction
            and self.train_fraction + self.validation_fraction < 1
        ):
            raise ValueError("split fractions must leave a non-empty test split")
        if self.min_labels < 1 or self.bootstrap_samples < 1:
            raise ValueError("min_labels and bootstrap_samples must be >= 1")

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> AnomalyConfig:
        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        values = dict(raw)
        if "features" in values:
            values["features"] = tuple(values["features"])
        return cls(**values)

    def fingerprint(self) -> str:
        payload = json.dumps(
            {"pilot_version": PILOT_VERSION, **asdict(self)}, sort_keys=True
        )
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
