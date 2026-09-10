import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scrape import should_alert  # noqa: E402


def test_price_decrease_no_thresholds_alerts():
    assert should_alert(100.0, 90.0, [110.0, 100.0], "in_stock") is True


def test_price_increase_never_alerts():
    assert should_alert(100.0, 110.0, [90.0, 100.0], "in_stock") is False


def test_unchanged_price_does_not_alert():
    assert should_alert(100.0, 100.0, [100.0], "in_stock") is False


def test_out_of_stock_suppresses_alert_even_on_drop():
    assert should_alert(100.0, 80.0, [100.0], "out_of_stock") is False


def test_target_price_met_alerts():
    assert (
        should_alert(100.0, 90.0, [110.0, 100.0], "in_stock", target_price=95.0)
        is True
    )


def test_target_price_unmet_suppresses_alert():
    # 96.0 is above the recorded low of 80.0, so this isn't a new all-time
    # low and the target_price gate applies normally.
    assert (
        should_alert(100.0, 96.0, [80.0, 100.0], "in_stock", target_price=95.0)
        is False
    )


def test_min_drop_percent_met_alerts():
    # 100 -> 90 is a 10% drop, threshold is 5%
    assert (
        should_alert(
            100.0, 90.0, [110.0, 100.0], "in_stock", min_drop_percent=5.0
        )
        is True
    )


def test_min_drop_percent_unmet_suppresses_alert():
    # 100 -> 98 is a 2% drop, threshold is 5%. 98.0 is above the recorded
    # low of 80.0, so this isn't a new all-time low and the threshold applies.
    assert (
        should_alert(
            100.0, 98.0, [80.0, 100.0], "in_stock", min_drop_percent=5.0
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
            [120.0, 110.0, 100.0],
            "in_stock",
            min_drop_percent=50.0,
        )
        is True
    )


def test_all_time_low_overrides_target_price():
    # New low, but well above an unmet target_price — override should still fire.
    assert (
        should_alert(
            100.0,
            99.0,
            [120.0, 110.0, 100.0],
            "in_stock",
            target_price=50.0,
        )
        is True
    )


def test_not_all_time_low_respects_target_price():
    # A drop that isn't a new low still has to clear target_price normally.
    assert (
        should_alert(
            100.0,
            95.0,
            [80.0, 90.0, 100.0],
            "in_stock",
            target_price=50.0,
        )
        is False
    )


def test_no_prior_history_never_alerts():
    assert should_alert(None, 90.0, [], "in_stock") is False
