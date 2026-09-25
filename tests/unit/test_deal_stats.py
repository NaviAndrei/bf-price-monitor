from datetime import date, timedelta

import pytest

from bf_price_monitor.analytics.deal_stats import (
    FAKE_DISCOUNT_MULTIPLIER,
    compute_deal_stats,
)

AS_OF = date(2026, 9, 25)


def _window(prices: list[float], start: date | None = None) -> list[dict]:
    # One observation per day, oldest first, ending the day before AS_OF --
    # the same {"date", "price"} shape scrape.py puts in history_30d.
    first = start or AS_OF - timedelta(days=len(prices))
    return [
        {"date": (first + timedelta(days=i)).isoformat(), "price": p}
        for i, p in enumerate(prices)
    ]


def test_empty_window_yields_no_reference_price_and_no_flag():
    stats = compute_deal_stats([], 80.0, None, None, AS_OF)
    assert stats["observation_count"] == 0
    assert stats["reference_price_30d"] is None
    assert stats["genuine_savings_percent"] is None
    assert stats["is_legal_discount"] is None
    assert stats["median_30d"] is None
    assert stats["z_score"] is None
    assert stats["fake_discount_suspect"] is False
    assert stats["fake_discount_reasons"] == []


def test_reference_price_is_lowest_prior_price_in_window():
    # Omnibus Art. 6a: the reduction is anchored to the lowest price of the
    # preceding 30 days, not the previous price or the average.
    stats = compute_deal_stats(_window([100.0, 90.0, 110.0]), 85.0, 110.0, None, AS_OF)
    assert stats["reference_price_30d"] == 90.0


def test_genuine_savings_percent_formula():
    # (1 - P_now / P_ref) * 100 = (1 - 81/90) * 100 = 10.0
    stats = compute_deal_stats(_window([100.0, 90.0]), 81.0, 90.0, None, AS_OF)
    assert stats["genuine_savings_percent"] == 10.0
    assert stats["is_legal_discount"] is True


@pytest.mark.parametrize("new_price", [90.0, 95.0])
def test_price_at_or_above_reference_is_legally_not_a_discount(new_price):
    stats = compute_deal_stats(_window([100.0, 90.0]), new_price, 100.0, None, AS_OF)
    assert stats["genuine_savings_percent"] <= 0
    assert stats["is_legal_discount"] is False


def test_robust_estimators_on_known_series():
    prices = [100.0, 102.0, 98.0, 100.0, 500.0]  # one wild outlier
    stats = compute_deal_stats(_window(prices), 99.0, 500.0, None, AS_OF)
    assert stats["median_30d"] == 100.0
    # MAD: |x - 100| = [0, 2, 2, 0, 400] -> median 2
    assert stats["mad_30d"] == 2.0
    # Inclusive quartiles of [98, 100, 100, 102, 500]
    assert stats["p25_30d"] == 100.0
    assert stats["p75_30d"] == 102.0
    assert stats["iqr_30d"] == 2.0
    assert stats["stdev_30d"] is not None and stats["stdev_30d"] > 150
    # Fewer than 20 points: 5% of n rounds down to 0, so nothing is trimmed.
    assert stats["trimmed_mean_30d"] == 180.0


def test_trimmed_mean_discards_top_and_bottom_five_percent():
    # 20 points -> 1 trimmed from each tail: the 1.0 and the 1000.0 go.
    prices = [1.0] + [100.0] * 18 + [1000.0]
    stats = compute_deal_stats(_window(prices), 100.0, 100.0, None, AS_OF)
    assert stats["trimmed_mean_30d"] == 100.0


def test_z_score_quantifies_drop_significance():
    prices = [100.0, 110.0, 90.0, 100.0]  # mean 100, sample stdev ~8.16
    stats = compute_deal_stats(_window(prices), 80.0, 100.0, None, AS_OF)
    assert stats["z_score"] == pytest.approx(-2.45, abs=0.01)


def test_z_score_is_none_for_flat_or_single_point_history():
    flat = compute_deal_stats(_window([100.0, 100.0]), 90.0, 100.0, None, AS_OF)
    single = compute_deal_stats(_window([100.0]), 90.0, 100.0, None, AS_OF)
    assert flat["z_score"] is None
    assert single["z_score"] is None
    assert single["stdev_30d"] is None
    # A single point still has a well-defined spread of zero.
    assert single["iqr_30d"] == 0.0


def test_days_at_current_price_counts_trailing_run():
    # Price has sat at 90 since 3 days before AS_OF.
    stats = compute_deal_stats(
        _window([100.0, 90.0, 90.0, 90.0]), 90.0, 90.0, None, AS_OF
    )
    assert stats["days_at_current_price"] == 3


def test_days_at_current_price_is_zero_for_a_fresh_change():
    stats = compute_deal_stats(_window([100.0, 100.0]), 90.0, 100.0, None, AS_OF)
    assert stats["days_at_current_price"] == 0


def test_hike_then_drop_is_flagged_fake_discount():
    # Classic pattern: stable at 100, hiked to 150 just before the "sale",
    # then "reduced" to 105 -- still above the 30-day low of 100.
    prices = [100.0] * 10 + [150.0, 150.0]
    stats = compute_deal_stats(_window(prices), 105.0, 150.0, None, AS_OF)
    assert stats["pre_sale_hike_detected"] is True
    assert stats["fake_discount_suspect"] is True
    assert stats["fake_discount_reasons"] == ["OBSERVED_PRE_SALE_HIKE"]
    assert stats["is_legal_discount"] is False


def test_hike_without_a_drop_is_not_a_fake_discount():
    # Found against real history: a price that jumped and simply stayed
    # there is a hike, but with no "discount" at all it isn't hike-then-drop.
    prices = [100.0] * 10 + [150.0]
    stats = compute_deal_stats(_window(prices), 150.0, 150.0, None, AS_OF)
    assert stats["pre_sale_hike_detected"] is True
    assert stats["fake_discount_suspect"] is False


def test_inflated_advertised_original_with_no_real_reduction_is_flagged():
    # Blueprint rule: P_orig > 1.25 * median(P_30d) and P_discount >= P_ref.
    prices = [100.0] * 10
    stats = compute_deal_stats(_window(prices), 100.0, 100.0, 140.0, AS_OF)
    assert stats["fake_discount_suspect"] is True
    assert stats["fake_discount_reasons"] == ["ADVERTISED_ORIGINAL_INFLATED"]


def test_both_reasons_reported_together():
    prices = [100.0] * 10 + [150.0]
    stats = compute_deal_stats(_window(prices), 110.0, 150.0, 160.0, AS_OF)
    assert stats["fake_discount_reasons"] == [
        "ADVERTISED_ORIGINAL_INFLATED",
        "OBSERVED_PRE_SALE_HIKE",
    ]


def test_hike_followed_by_genuine_drop_below_reference_is_not_fake():
    # Hiked, but the new price beats the real 30-day low: a real reduction
    # under Omnibus, so no fake-discount label even though a hike happened.
    prices = [100.0] * 10 + [150.0]
    stats = compute_deal_stats(_window(prices), 90.0, 150.0, 160.0, AS_OF)
    assert stats["pre_sale_hike_detected"] is True
    assert stats["fake_discount_suspect"] is False
    assert stats["fake_discount_reasons"] == []


def test_threshold_is_strictly_greater_than_multiplier():
    # Exactly 1.25x the median is not an inflated original.
    prices = [100.0] * 10
    at_threshold = 100.0 * FAKE_DISCOUNT_MULTIPLIER
    stats = compute_deal_stats(_window(prices), 100.0, 100.0, at_threshold, AS_OF)
    assert stats["fake_discount_suspect"] is False


def test_single_prior_observation_cannot_self_flag_a_hike():
    # With one point the median *is* old_price, so old_price can never
    # exceed 1.25x of itself -- no hike claim from one data point.
    stats = compute_deal_stats(_window([150.0]), 150.0, 150.0, None, AS_OF)
    assert stats["pre_sale_hike_detected"] is False
    assert stats["fake_discount_suspect"] is False
