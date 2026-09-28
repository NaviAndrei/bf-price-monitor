"""T-29 (#36): Isolation Forest + PELT ensemble and the pilot CLI.

Needs the optional `anomaly` extra. The base Quality Gate installs only
`dev`, so this module is skipped there. The dedicated `anomaly-pilot` CI
job sets REQUIRE_ANOMALY_EXTRA=1, which turns a missing extra into an
import error (a failed job) instead of a silent skip.
"""

from __future__ import annotations

import json
import os
import random
from datetime import UTC, datetime, timedelta

import pytest

if os.environ.get("REQUIRE_ANOMALY_EXTRA") != "1":
    pytest.importorskip("sklearn")
    pytest.importorskip("ruptures")

import numpy as np

from bf_price_monitor.anomaly.config import AnomalyConfig
from bf_price_monitor.anomaly.dataset import (
    Observation,
    build_feature_rows,
    chronological_split,
)
from bf_price_monitor.anomaly.model import (
    InsufficientLabelsError,
    feature_matrix,
    fit_isolation_forest,
    pelt_active_negative,
    run_pilot,
    score_rows,
    select_threshold,
)
from bf_price_monitor.storage import sqlite as sqlite_storage

T0 = datetime(2026, 9, 1, tzinfo=UTC)
CFG = AnomalyConfig(n_estimators=50, bootstrap_samples=20)


def _synthetic_rows(offers=12, days=30, seed=3):
    rng = random.Random(seed)
    observations = []
    for o in range(offers):
        base = rng.uniform(200, 5000)
        for d in range(days):
            price = base * (1 + rng.uniform(-0.01, 0.01))
            if o % 4 == 0 and d >= days - 5:
                price = base * 0.6  # a sharp, sustained drop late in the window
            observations.append(
                Observation(
                    f"offer-{o}",
                    "emag" if o % 2 else "pcgarage",
                    f"https://example.test/p/{o}",
                    round(price, 2),
                    T0 + timedelta(days=d, hours=o % 5),
                )
            )
    return build_feature_rows(observations)


# --- PELT -----------------------------------------------------------------------


def test_pelt_flags_a_recent_sustained_drop():
    assert pelt_active_negative([100.0] * 12 + [70.0] * 3, CFG)


@pytest.mark.parametrize(
    "history",
    [
        [100.0] * 15,  # flat
        [100.0] * 12 + [130.0] * 3,  # rise, not a drop
        [100.0] * 5 + [70.0] * 20,  # drop outside the active window
        [100.0, 70.0, 70.0],  # shorter than 2 * min_size
        [0.0] * 10,  # degenerate median
    ],
)
def test_pelt_ignores_non_drops(history):
    assert not pelt_active_negative(history, CFG)


def test_pelt_penalty_is_a_live_parameter():
    small_drop = [100.0] * 12 + [97.0] * 3
    assert not pelt_active_negative(small_drop, CFG)
    assert pelt_active_negative(
        small_drop, AnomalyConfig.from_mapping({"pelt_penalty": 0.0001})
    )


# --- fit / threshold ------------------------------------------------------------


def test_preprocessing_is_fit_on_train_only():
    split = chronological_split(_synthetic_rows(), 0.6, 0.2)
    pipeline = fit_isolation_forest(split.train, CFG)
    center = pipeline.named_steps["scale"].center_
    assert np.allclose(center, np.median(feature_matrix(split.train, CFG), axis=0))
    everything = split.train + split.validation + split.test
    assert not np.allclose(center, np.median(feature_matrix(everything, CFG), axis=0))


def test_scores_are_deterministic_for_a_fixed_seed():
    split = chronological_split(_synthetic_rows(), 0.6, 0.2)
    a = score_rows(fit_isolation_forest(split.train, CFG), split.validation, CFG)
    b = score_rows(fit_isolation_forest(split.train, CFG), split.validation, CFG)
    assert a == b
    assert all(-1 <= s <= 0 for s in a)


def test_threshold_modes():
    split = chronological_split(_synthetic_rows(), 0.6, 0.2)
    scores = score_rows(fit_isolation_forest(split.train, CFG), split.validation, CFG)
    assert select_threshold(CFG, split.validation, scores)["value"] == -0.65

    quantile_cfg = AnomalyConfig.from_mapping({"threshold_mode": "validation_quantile"})
    chosen = select_threshold(quantile_cfg, split.validation, scores)
    assert chosen["fit_on"] == "validation"
    assert chosen["value"] == pytest.approx(np.quantile(scores, 0.03))

    f1_cfg = AnomalyConfig.from_mapping({"threshold_mode": "validation_f1"})
    with pytest.raises(InsufficientLabelsError, match="refused"):
        select_threshold(f1_cfg, split.validation, scores)

    # With enough (test-constructed) labels the F1 search runs on
    # validation rows only. These labels exist only inside this test.
    for i, row in enumerate(split.validation):
        row.label = 1 if scores[i] < np.median(scores) else 0
    chosen = select_threshold(f1_cfg, split.validation, scores)
    assert chosen["mode"] == "validation_f1" and chosen["validation_f1"] > 0.9


# --- orchestration --------------------------------------------------------------


def test_run_pilot_keeps_test_sealed_by_default():
    split = chronological_split(_synthetic_rows(), 0.6, 0.2)
    report = run_pilot(split, CFG)
    assert report["test"] == {"status": "sealed", "rows": len(split.test)}
    assert report["validation"]["metrics"]["status"] == "blocked_insufficient_labels"
    assert report["environment"]["pilot_version"] == "anomaly-pilot-v1"
    assert "https://" not in json.dumps(report, default=str)

    unsealed = run_pilot(split, CFG, unseal_test=True)
    assert unsealed["test"]["rows"] == len(split.test)
    assert "metrics" in unsealed["test"]


def test_ensemble_flags_the_injected_drops():
    split = chronological_split(_synthetic_rows(), 0.6, 0.2)
    cfg = AnomalyConfig.from_mapping(
        {
            "n_estimators": 50,
            "bootstrap_samples": 20,
            "threshold_mode": "validation_quantile",
            "contamination": 0.1,
        }
    )
    report = run_pilot(split, cfg, unseal_test=True)
    flagged = {item["offer_id"] for item in report["test"]["top_flagged"]}
    assert flagged
    assert flagged <= {"offer-0", "offer-4", "offer-8"}


# --- CLI ------------------------------------------------------------------------


def test_cli_writes_blocked_report_without_labels(tmp_path, capsys):
    import anomaly_pilot

    db_path = tmp_path / "price_history.db"
    conn = sqlite_storage.init_db(db_path)
    rng = random.Random(5)
    for o in range(8):
        for d in range(20):
            sqlite_storage.record_observation(
                conn,
                {
                    "sku": f"SKU{o}",
                    "title": f"Product {o}",
                    "price": round(100 * (o + 1) * (1 + rng.uniform(-0.02, 0.02)), 2),
                    "in_stock": True,
                    "retailer": "emag",
                    "url": f"https://www.emag.ro/p/pd/SKU{o}/",
                    "scraped_at": (T0 + timedelta(days=d)).isoformat(),
                },
            )
    conn.close()
    before = db_path.read_bytes()

    out = tmp_path / "exports" / "report.json"
    config = tmp_path / "pilot.json"
    config.write_text(json.dumps({"n_estimators": 30, "bootstrap_samples": 10}))
    assert (
        anomaly_pilot.main(
            ["--db", str(db_path), "--out", str(out), "--config", str(config)]
        )
        == 0
    )

    report = json.loads(out.read_text("utf-8"))
    assert report["status"] == "blocked_insufficient_labels"
    assert "at least 30" in report["reason"]
    assert report["test"]["status"] == "sealed"
    assert report["test_unsealed"] is False
    assert report["config"]["n_estimators"] == 30
    assert report["config_fingerprint"].startswith("sha256:")
    assert report["data_fingerprint"].startswith("sha256:")
    assert "emag.ro" not in out.read_text("utf-8")
    assert db_path.read_bytes() == before
    assert "status=blocked_insufficient_labels" in capsys.readouterr().out


def test_cli_reports_missing_database(tmp_path):
    import anomaly_pilot

    assert anomaly_pilot.main(["--db", str(tmp_path / "none.db")]) == 2
