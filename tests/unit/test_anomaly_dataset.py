"""T-29 (#36): pilot config, causal features, splits and label joins.

Pure Python: runs in the base Quality Gate without the `anomaly` extra.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from bf_price_monitor.anomaly.config import FEATURES, AnomalyConfig
from bf_price_monitor.anomaly.dataset import (
    LABEL_RELEVANCE,
    InsufficientDataError,
    Observation,
    alert_decision_ids,
    attach_outcomes,
    build_feature_rows,
    chronological_split,
    data_fingerprint,
    data_profile,
    load_decided_alert_ids,
    load_labels,
    load_observations,
    open_readonly,
)
from bf_price_monitor.storage import sqlite as sqlite_storage

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
URL = "https://www.emag.ro/laptop-x/pd/ABC123XYZ/"


def _series(prices, offer="o1", retailer="emag", url=URL, step=timedelta(days=1)):
    return [
        Observation(offer, retailer, url, float(p), T0 + i * step)
        for i, p in enumerate(prices)
    ]


# --- config -----------------------------------------------------------------


def test_config_defaults_are_the_blueprint_hypotheses():
    cfg = AnomalyConfig()
    assert cfg.contamination == 0.03
    assert cfg.score_threshold == -0.65
    assert (cfg.pelt_model, cfg.pelt_min_size) == ("l2", 3)
    assert cfg.threshold_mode == "fixed"
    assert cfg.features == FEATURES


@pytest.mark.parametrize(
    "override",
    [
        {"contamination": 0},
        {"contamination": 0.6},
        {"score_threshold": 0.2},
        {"threshold_mode": "best_guess"},
        {"pelt_penalty": 0},
        {"pelt_min_size": 0},
        {"pelt_active_window": 0},
        {"train_fraction": 0.8, "validation_fraction": 0.2},
        {"features": ["price_x"]},
        {"features": []},
        {"min_labels": 0},
    ],
)
def test_config_rejects_invalid_values(override):
    with pytest.raises(ValueError):
        AnomalyConfig.from_mapping(override)


def test_config_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown config keys"):
        AnomalyConfig.from_mapping({"contamnation": 0.05})


def test_config_fingerprint_is_stable_and_parameter_sensitive():
    assert AnomalyConfig().fingerprint() == AnomalyConfig().fingerprint()
    assert AnomalyConfig().fingerprint().startswith("sha256:")
    changed = AnomalyConfig.from_mapping({"score_threshold": -0.6})
    assert changed.fingerprint() != AnomalyConfig().fingerprint()


# --- alert id join --------------------------------------------------------------


def test_alert_decision_ids_match_notify_formula():
    import notify

    for price in (1299.99, 1299.0):
        alert = {"url": URL, "new_price": price, "site": "emag"}
        expected = notify._deal_event_id(notify._deal_dedup_key(alert))
        assert alert_decision_ids(URL, price, "emag")[0] == expected


def test_alert_decision_ids_also_cover_integer_spelling():
    import notify

    ids = alert_decision_ids(URL, 1299.0, "emag")
    as_int = {"url": URL, "new_price": 1299, "site": "emag"}
    assert notify._deal_event_id(notify._deal_dedup_key(as_int)) in ids
    assert len(alert_decision_ids(URL, 1299.5, "emag")) == 1


# --- features -------------------------------------------------------------------


def test_first_observation_of_each_offer_yields_no_row():
    rows = build_feature_rows(_series([100]) + _series([50, 40], offer="o2"))
    assert [(r.offer_id, r.price) for r in rows] == [("o2", 40.0)]


def test_feature_values_for_a_known_series():
    rows = build_feature_rows(_series([100, 100, 80]))
    last = rows[-1]
    assert set(last.features) == set(FEATURES)
    assert last.features["drop_pct"] == pytest.approx(20.0)
    # prior 30-day median of [100, 100] is 100
    assert last.features["relative_to_30d_median"] == pytest.approx(-0.2)
    assert last.features["hour_of_day"] == 12.0
    # no price change before the last row: counted from the first observation
    assert last.features["days_since_last_change"] == pytest.approx(2.0)
    assert last.features["volatility_score_7d"] > 0
    assert last.history == (100.0, 100.0, 80.0)


def test_days_since_last_change_tracks_the_latest_prior_change():
    rows = build_feature_rows(_series([100, 90, 90, 90, 70]))
    # the 100 -> 90 change happened at day 1; the last row is day 4
    assert rows[-1].features["days_since_last_change"] == pytest.approx(3.0)


def test_features_are_causal():
    base = build_feature_rows(_series([100, 95, 90, 85]))
    extended = build_feature_rows(_series([100, 95, 90, 85, 10, 500]))
    for before, after in zip(base, extended, strict=False):
        assert before.features == after.features
        assert before.history == after.history


def test_thirty_day_window_excludes_older_observations():
    obs = [
        Observation("o1", "emag", URL, 1000.0, T0),
        Observation("o1", "emag", URL, 100.0, T0 + timedelta(days=40)),
        Observation("o1", "emag", URL, 100.0, T0 + timedelta(days=41)),
    ]
    rows = build_feature_rows(obs)
    # at day 41 only the day-40 price (100) is in the prior 30 days
    assert rows[-1].features["relative_to_30d_median"] == pytest.approx(0.0)


def test_rows_are_ordered_chronologically_across_offers():
    rows = build_feature_rows(
        _series([10, 9, 8], offer="b") + _series([10, 9, 8], offer="a")
    )
    keys = [(r.scraped_at, r.offer_id) for r in rows]
    assert keys == sorted(keys)


# --- split ----------------------------------------------------------------------


def test_chronological_split_orders_and_never_straddles_a_timestamp():
    obs = []
    for offer in ("a", "b", "c"):
        obs += _series([100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90], offer=offer)
    rows = build_feature_rows(obs)
    split = chronological_split(rows, 0.6, 0.2)
    assert max(r.scraped_at for r in split.train) < split.validation_start
    assert all(
        split.validation_start <= r.scraped_at < split.test_start
        for r in split.validation
    )
    assert min(r.scraped_at for r in split.test) >= split.test_start
    assert len(split.train) + len(split.validation) + len(split.test) == len(rows)
    # three offers share every timestamp, so each split holds whole days
    for part in (split.train, split.validation, split.test):
        assert len(part) % 3 == 0


def test_chronological_split_refuses_empty_partitions():
    rows = build_feature_rows(_series([100, 90]))
    with pytest.raises(InsufficientDataError):
        chronological_split(rows, 0.6, 0.2)
    same_time = build_feature_rows(
        _series([100, 90], offer="a", step=timedelta(0))
        + _series([100, 90, 80, 70], offer="b", step=timedelta(0))
    )
    with pytest.raises(InsufficientDataError, match="empty partition"):
        chronological_split(same_time, 0.6, 0.2)


# --- SQLite loading and label policy ------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite_storage.init_db(path)
    for i, price in enumerate([100.0, 100.0, 80.0]):
        sqlite_storage.record_observation(
            conn,
            {
                "sku": "ABC123XYZ",
                "title": "Laptop X",
                "price": price,
                "in_stock": True,
                "retailer": "emag",
                "url": URL,
                "scraped_at": (T0 + timedelta(days=i)).isoformat(),
            },
        )
    conn.close()
    return path


def _add_feedback(conn, alert_id, label, user, seq_hint):
    conn.execute(
        "INSERT INTO alert_feedback (callback_query_id, update_id, "
        "alert_decision_id, label, user_ref, chat_ref, callback_version, "
        "received_at_utc) VALUES (?, ?, ?, ?, ?, ?, 'fb1', ?)",
        (
            f"cb{seq_hint}",
            seq_hint,
            alert_id,
            label,
            user * 64,
            "c" * 64,
            (T0 + timedelta(minutes=seq_hint)).isoformat(),
        ),
    )


def test_label_policy_is_explicit_for_every_feedback_label():
    from bf_price_monitor.feedback import FeedbackLabel

    assert set(LABEL_RELEVANCE) == {label.value for label in FeedbackLabel}


def test_load_join_and_label_policy(db_path):
    alert_80 = alert_decision_ids(URL, 80.0, "emag")[0]
    alert_other = "11111111-2222-3333-4444-555555555555"
    alert_tied = "66666666-2222-3333-4444-555555555555"
    writer = sqlite_storage.init_db(db_path)
    sqlite_storage.record_delivery_attempt_new_cycle(
        writer,
        {
            "alert_decision_id": alert_80,
            "channel": "telegram",
            "destination": "d" * 64,
            "response_class": "2xx",
            "final_state": "delivered",
        },
    )
    writer.close()
    conn = sqlite3.connect(db_path)
    with conn:
        _add_feedback(conn, alert_80, "useful", "a", 1)
        _add_feedback(conn, alert_80, "purchased", "b", 2)
        _add_feedback(conn, alert_80, "fake_discount", "c", 3)
        _add_feedback(conn, alert_other, "wrong_product", "a", 4)
        _add_feedback(conn, alert_tied, "useful", "a", 5)
        _add_feedback(conn, alert_tied, "wrong_price", "b", 6)
        # relabel: user d's latest vote on alert_other wins
        _add_feedback(conn, alert_other, "useful", "d", 7)
        _add_feedback(conn, alert_other, "fake_discount", "d", 8)
    conn.close()

    ro = open_readonly(db_path)
    observations = load_observations(ro)
    decided = load_decided_alert_ids(ro)
    labels = load_labels(ro)
    ro.close()

    assert decided == {alert_80}
    assert labels.by_alert == {alert_80: 1, alert_other: 0}
    assert labels.excluded_by_policy == 1
    assert labels.tied_alerts == 1
    assert labels.raw_counts["fake_discount"] == 2

    rows = build_feature_rows(observations)
    attach_outcomes(rows, decided, labels)
    last = rows[-1]
    assert last.price == 80.0 and last.deterministic_alert and last.label == 1
    assert not rows[0].deterministic_alert and rows[0].label is None
    assert labels.matched_alerts == 1 and labels.unmatched_alerts == 1


def test_open_readonly_cannot_write(db_path):
    ro = open_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM price_observations")
    ro.close()


def test_loaders_tolerate_a_pre_migration_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(sqlite_storage._SCHEMA_SQL.split("-- T-37b")[0])
    conn.close()
    ro = open_readonly(path)
    assert load_decided_alert_ids(ro) == set()
    assert load_labels(ro).by_alert == {}
    assert load_observations(ro) == []
    ro.close()


def test_data_fingerprint_and_profile(db_path):
    ro = open_readonly(db_path)
    observations = load_observations(ro)
    labels = load_labels(ro)
    ro.close()
    fp = data_fingerprint(observations, set(), labels)
    assert fp == data_fingerprint(list(reversed(observations)), set(), labels)
    assert fp != data_fingerprint(observations, {"x"}, labels)
    profile = data_profile(observations, build_feature_rows(observations))
    assert profile["observations"] == 3 and profile["feature_rows"] == 2
    assert profile["midnight_timestamp_fraction"] == 0.0
    assert data_profile([], [])["observations"] == 0


# --- offline-only isolation -----------------------------------------------------


def test_production_paths_never_touch_the_pilot():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    forbidden = (
        "bf_price_monitor.anomaly",
        "anomaly_pilot",
        "--extra anomaly",
        "--all-extras",
        "sklearn",
        "ruptures",
    )
    for rel in (
        "scripts/scrape.py",
        "scripts/analyze.py",
        "scripts/notify.py",
        "scripts/run_loop.py",
        "scripts/feedback.py",
        ".github/workflows/monitor.yml",
        "Dockerfile",
        "docker-compose.yml",
    ):
        text = (root / rel).read_text("utf-8")
        for needle in forbidden:
            assert needle not in text, f"{rel} references {needle}"
