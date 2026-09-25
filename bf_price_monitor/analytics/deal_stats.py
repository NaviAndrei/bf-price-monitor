"""Deal statistics and Omnibus reference-price checks (T-25 / #33).

Pure functions over an offer's prior 30-day observation window. Everything
here is deterministic and explanatory: the rule engine's rule_verdict in
scripts/analyze.py stays authoritative, and nothing in this module can
change it.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

# An advertised original price, or the observed price right before the
# "sale", more than 25% above the 30-day median counts as inflated. Same
# 1.25 factor evaluate_omnibus_rule() already uses for INFLATED_REFERENCE.
FAKE_DISCOUNT_MULTIPLIER = 1.25

# Fraction discarded from each tail for the trimmed mean.
TRIM_FRACTION = 0.05


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _trimmed_mean(sorted_prices: list[float]) -> float:
    k = int(len(sorted_prices) * TRIM_FRACTION)
    kept = sorted_prices[k : len(sorted_prices) - k] if k else sorted_prices
    return statistics.fmean(kept)


def _days_at_current_price(
    window: Sequence[Mapping[str, Any]], new_price: float, as_of: date
) -> int:
    # Walk back from the newest prior observation while it still matches
    # new_price; the earliest match is when the current price level began.
    # Bounded by the window, so this never reports more than ~30 days.
    run_start: date | None = None
    for obs in reversed(window):
        if float(obs["price"]) != new_price:
            break
        run_start = date.fromisoformat(obs["date"])
    return (as_of - run_start).days if run_start is not None else 0


def compute_deal_stats(
    window: Sequence[Mapping[str, Any]],
    new_price: float,
    old_price: float | None,
    advertised_original: float | None,
    as_of: date,
) -> dict[str, Any]:
    """Summarize the prior 30-day window and classify fake discounts.

    `window` holds the offer's observations from the 30 days *before*
    new_price was observed, oldest first, each with "date" (ISO) and
    "price". new_price itself must not be in it: the Omnibus reference
    price is the lowest price of the preceding 30 days, and including the
    current price would make every genuine reduction compare against
    itself.
    """
    prices = [float(obs["price"]) for obs in window]
    n = len(prices)

    reference_price = min(prices) if prices else None
    median = statistics.median(prices) if prices else None
    mean = statistics.fmean(prices) if prices else None
    stdev = statistics.stdev(prices) if n >= 2 else None

    p25: float | None
    p75: float | None
    if n >= 2:
        p25, _, p75 = statistics.quantiles(prices, n=4, method="inclusive")
    elif n == 1:
        p25 = p75 = prices[0]
    else:
        p25 = p75 = None

    mad = (
        statistics.median(abs(p - median) for p in prices)
        if median is not None
        else None
    )
    z_score = (
        (new_price - mean) / stdev
        if stdev is not None and mean is not None and stdev > 0
        else None
    )

    genuine_savings = (
        (1 - new_price / reference_price) * 100
        if reference_price is not None and reference_price > 0
        else None
    )

    hike_threshold = median * FAKE_DISCOUNT_MULTIPLIER if median is not None else None
    pre_sale_hike = (
        hike_threshold is not None
        and old_price is not None
        and old_price > hike_threshold
    )
    original_inflated = (
        hike_threshold is not None
        and advertised_original is not None
        and advertised_original > hike_threshold
    )
    # Hike-then-drop: the price was pushed well above its recent norm (by
    # the retailer's own struck-through figure, or as we observed it), and
    # the "discounted" price still doesn't beat the real 30-day low.
    no_real_reduction = reference_price is not None and new_price >= reference_price
    reasons: list[str] = []
    if no_real_reduction:
        if original_inflated:
            reasons.append("ADVERTISED_ORIGINAL_INFLATED")
        # old_price is not None whenever pre_sale_hike is True; the drop
        # check keeps a hike that simply persisted from counting as a sale.
        if pre_sale_hike and old_price is not None and new_price < old_price:
            reasons.append("OBSERVED_PRE_SALE_HIKE")

    return {
        "observation_count": n,
        "reference_price_30d": reference_price,
        "genuine_savings_percent": _round(genuine_savings),
        "is_legal_discount": (
            genuine_savings > 0 if genuine_savings is not None else None
        ),
        "median_30d": _round(median),
        "trimmed_mean_30d": _round(_trimmed_mean(sorted(prices))) if prices else None,
        "p25_30d": _round(p25),
        "p75_30d": _round(p75),
        "iqr_30d": _round(p75 - p25) if p25 is not None and p75 is not None else None,
        "mad_30d": _round(mad),
        "stdev_30d": _round(stdev),
        "z_score": _round(z_score),
        "days_at_current_price": _days_at_current_price(window, new_price, as_of),
        "pre_sale_hike_detected": pre_sale_hike,
        "fake_discount_suspect": bool(reasons),
        "fake_discount_reasons": reasons,
    }
