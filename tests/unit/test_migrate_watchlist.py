from __future__ import annotations

import json

import migrate_watchlist_to_modern as migration

from bf_price_monitor.config.validator import load_watchlist, validate_watchlist


def test_convert_carries_cooldown_hours_over():
    watch = migration.convert({"site": "emag", "query": "q", "cooldown_hours": 6})
    assert watch.cooldown_hours == 6


def test_convert_defaults_cooldown_hours_when_absent():
    watch = migration.convert({"site": "emag", "query": "q"})
    assert watch.cooldown_hours == 24


def test_migrated_cooldown_hours_passes_real_validator(tmp_path):
    source = tmp_path / "legacy.json"
    source.write_text(
        json.dumps([{"site": "emag", "query": "q", "cooldown_hours": 12}]),
        encoding="utf-8",
    )
    modern = migration.migrate(source, dry_run=True)["modern"]
    target = tmp_path / "modern.json"
    target.write_text(json.dumps(modern), encoding="utf-8")

    validate_watchlist(target)
    assert load_watchlist(target)[0]["cooldown_hours"] == 12
