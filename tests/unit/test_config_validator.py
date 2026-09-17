from __future__ import annotations

import json

import pytest

from bf_price_monitor.config.validator import load_watchlist, validate_watchlist


def write_watchlist(tmp_path, data):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_valid_legacy_watchlist_passes(tmp_path):
    path = write_watchlist(
        tmp_path,
        [
            {"site": "emag", "query": "laptop lenovo v15", "target_price": 2500.0},
            {
                "site": "pcgarage",
                "query": "laptop asus vivobook",
                "min_drop_percent": 5.0,
            },
        ],
    )
    validate_watchlist(path)
    assert load_watchlist(path) == json.loads(path.read_text())


def test_valid_modern_watchlist_passes(tmp_path):
    path = write_watchlist(
        tmp_path,
        {
            "watches": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "query": "laptop lenovo v15",
                    "target_price": 2500.0,
                    "drop_rule": "percentage",
                    "drop_threshold": 5.0,
                    "cadence_minutes": 120,
                }
            ]
        },
    )
    validate_watchlist(path)
    assert load_watchlist(path) == json.loads(path.read_text())["watches"]


def test_missing_watches_key_raises(tmp_path):
    path = write_watchlist(tmp_path, {"not_watches": []})
    with pytest.raises(ValueError):
        validate_watchlist(path)


def test_negative_target_price_raises_with_path_in_message(tmp_path):
    path = write_watchlist(
        tmp_path,
        [{"site": "emag", "query": "laptop lenovo v15", "target_price": -100.0}],
    )
    with pytest.raises(ValueError) as exc_info:
        validate_watchlist(path)
    message = str(exc_info.value)
    assert "target_price" in message
