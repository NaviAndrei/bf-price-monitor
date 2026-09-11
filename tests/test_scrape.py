import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scrape import prune_history, should_alert, update_lifetime_stats  # noqa: E402


def test_price_decrease_no_thresholds_alerts():
    assert should_alert(100.0, 90.0, "in_stock", all_time_low=100.0) is True


def test_price_increase_never_alerts():
    assert should_alert(100.0, 110.0, "in_stock", all_time_low=90.0) is False


def test_unchanged_price_does_not_alert():
    assert should_alert(100.0, 100.0, "in_stock", all_time_low=100.0) is False


def test_out_of_stock_suppresses_alert_even_on_drop():
    assert should_alert(100.0, 80.0, "out_of_stock", all_time_low=100.0) is False


def test_target_price_met_alerts():
    assert (
        should_alert(100.0, 90.0, "in_stock", target_price=95.0, all_time_low=100.0)
        is True
    )


def test_target_price_unmet_suppresses_alert():
    # 96.0 is above the recorded low of 80.0, so this isn't a new all-time
    # low and the target_price gate applies normally.
    assert (
        should_alert(
            100.0, 96.0, "in_stock", target_price=95.0, all_time_low=80.0
        )
        is False
    )


def test_min_drop_percent_met_alerts():
    # 100 -> 90 is a 10% drop, threshold is 5%
    assert (
        should_alert(
            100.0, 90.0, "in_stock", min_drop_percent=5.0, all_time_low=100.0
        )
        is True
    )


def test_min_drop_percent_unmet_suppresses_alert():
    # 100 -> 98 is a 2% drop, threshold is 5%. 98.0 is above the recorded
    # low of 80.0, so this isn't a new all-time low and the threshold applies.
    assert (
        should_alert(
            100.0, 98.0, "in_stock", min_drop_percent=5.0, all_time_low=80.0
        )
        is False
    )


def test_all_time_low_overrides_min_drop_percent():
    # Only a 1% drop below prev_price, but still the lowest ever recorded —
    # the override should fire even though min_drop_percent isn't met.
    assert (
        should_alert(
            100.0,
            99.0,
            "in_stock",
            min_drop_percent=50.0,
            all_time_low=100.0,
        )
        is True
    )


def test_all_time_low_overrides_target_price():
    # New low, but well above an unmet target_price — override should still fire.
    assert (
        should_alert(
            100.0,
            99.0,
            "in_stock",
            target_price=50.0,
            all_time_low=100.0,
        )
        is True
    )


def test_not_all_time_low_respects_target_price():
    # A drop that isn't a new low still has to clear target_price normally.
    assert (
        should_alert(
            100.0,
            95.0,
            "in_stock",
            target_price=50.0,
            all_time_low=80.0,
        )
        is False
    )


def test_no_prior_history_never_alerts():
    assert should_alert(None, 90.0, "in_stock") is False


REFERENCE_DATE = date(2026, 1, 1)


def _days_ago(n: int) -> str:
    return (REFERENCE_DATE - timedelta(days=n)).isoformat()


def test_prune_history_drops_entries_older_than_90_days():
    history = [
        {"date": _days_ago(100), "price": 50.0, "stock_status": "in_stock"},
        {"date": _days_ago(95), "price": 55.0, "stock_status": "in_stock"},
        {"date": _days_ago(10), "price": 60.0, "stock_status": "in_stock"},
        {"date": _days_ago(0), "price": 65.0, "stock_status": "in_stock"},
    ]
    pruned = prune_history(history, REFERENCE_DATE)
    assert [h["price"] for h in pruned] == [60.0, 65.0]


def test_prune_history_keeps_entries_within_90_days_intact():
    history = [
        {"date": _days_ago(90), "price": 50.0, "stock_status": "in_stock"},
        {"date": _days_ago(30), "price": 55.0, "stock_status": "in_stock"},
        {"date": _days_ago(0), "price": 60.0, "stock_status": "in_stock"},
    ]
    pruned = prune_history(history, REFERENCE_DATE)
    assert pruned == history


def test_prune_history_min_entries_fallback_prevents_over_pruning():
    # Both entries are far outside the 90-day window, but dropping both would
    # leave 0 entries — min_entries=2 keeps the most recent 2 anyway so
    # prev_price/past_prices comparisons in main() don't break.
    history = [
        {"date": _days_ago(200), "price": 50.0, "stock_status": "in_stock"},
        {"date": _days_ago(150), "price": 55.0, "stock_status": "in_stock"},
    ]
    pruned = prune_history(history, REFERENCE_DATE, min_entries=2)
    assert pruned == history


def test_update_lifetime_stats_updates_on_new_low_and_high():
    entry = {"all_time_low": 90.0, "all_time_high": 120.0, "first_seen": "2025-01-01"}
    update_lifetime_stats(entry, 80.0, "2026-01-01")
    assert entry["all_time_low"] == 80.0
    assert entry["all_time_high"] == 120.0

    update_lifetime_stats(entry, 130.0, "2026-01-02")
    assert entry["all_time_low"] == 80.0
    assert entry["all_time_high"] == 130.0


def test_update_lifetime_stats_backward_compatibility_derives_from_history():
    # A legacy entry recorded before all_time_low/high/first_seen existed —
    # the function must compute a correct baseline from its history instead
    # of treating new_price as the only price ever seen.
    entry = {
        "title": "Legacy Product",
        "site": "emag",
        "history": [
            {"date": "2025-06-01", "price": 100.0, "stock_status": "in_stock"},
            {"date": "2025-06-15", "price": 90.0, "stock_status": "in_stock"},
            {"date": "2025-07-01", "price": 110.0, "stock_status": "in_stock"},
        ],
    }
    update_lifetime_stats(entry, 95.0, "2025-07-15")
    assert entry["all_time_low"] == 90.0
    assert entry["all_time_high"] == 110.0
    assert entry["first_seen"] == "2025-06-01"


def test_update_lifetime_stats_new_entry_defaults_first_seen_to_today():
    entry = {"title": "Brand New Product", "site": "emag", "history": []}
    update_lifetime_stats(entry, 200.0, "2026-01-01")
    assert entry["all_time_low"] == 200.0
    assert entry["all_time_high"] == 200.0
    assert entry["first_seen"] == "2026-01-01"
