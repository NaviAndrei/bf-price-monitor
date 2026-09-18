import json
from datetime import date, timedelta

import pytest
import scrape
from scrape import (
    canonicalize_url,
    prune_history,
    record_observation,
    should_alert,
    title_matches_query,
    update_lifetime_stats,
)


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
        should_alert(100.0, 96.0, "in_stock", target_price=95.0, all_time_low=80.0)
        is False
    )


def test_min_drop_percent_met_alerts():
    # 100 -> 90 is a 10% drop, threshold is 5%
    assert (
        should_alert(100.0, 90.0, "in_stock", min_drop_percent=5.0, all_time_low=100.0)
        is True
    )


def test_min_drop_percent_unmet_suppresses_alert():
    # 100 -> 98 is a 2% drop, threshold is 5%. 98.0 is above the recorded
    # low of 80.0, so this isn't a new all-time low and the threshold applies.
    assert (
        should_alert(100.0, 98.0, "in_stock", min_drop_percent=5.0, all_time_low=80.0)
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


def test_micro_drop_below_ron_floor_suppressed():
    # 100 -> 97 is a 3 RON drop (3%): clears the percent floor but not the
    # absolute RON floor, so it must still be suppressed.
    assert should_alert(100.0, 97.0, "in_stock", all_time_low=80.0) is False


def test_micro_drop_below_percent_floor_suppressed():
    # 1000 -> 995 is a 5 RON drop (0.5%): clears the RON floor but not the
    # percent floor, so it must still be suppressed.
    assert should_alert(1000.0, 995.0, "in_stock", all_time_low=800.0) is False


def test_drop_meeting_both_floors_alerts():
    # 200 -> 190 is a 10 RON drop (5%): clears both floors.
    assert should_alert(200.0, 190.0, "in_stock", all_time_low=150.0) is True


def test_all_time_low_override_bypasses_micro_drop_floor():
    # Only a 1 RON, 1% drop, but still a genuine new all-time low — under
    # the default "aggressive" policy the override must fire even though
    # it wouldn't clear either floor alone.
    assert should_alert(100.0, 99.0, "in_stock", all_time_low=100.0) is True


# --- atl_policy matrix (T-10) ---
#
# "aggressive" is the default and reproduces every pre-T-10 assertion above
# (all_time_low bypasses the micro-drop floor, target_price, and
# min_drop_percent unconditionally) since no existing watchlist entry sets
# atl_policy explicitly. "conservative" and "off" are opt-in policies that
# narrow the override; they must never change what "aggressive" does.


def test_atl_policy_default_is_aggressive():
    # Omitting atl_policy entirely must behave identically to passing
    # atl_policy="aggressive" explicitly — this is what keeps every
    # existing watchlist entry's behavior stable.
    without_policy = should_alert(100.0, 99.0, "in_stock", all_time_low=100.0)
    with_explicit_aggressive = should_alert(
        100.0, 99.0, "in_stock", all_time_low=100.0, atl_policy="aggressive"
    )
    assert without_policy is with_explicit_aggressive is True


def test_atl_aggressive_fires_despite_unmet_target_price():
    assert (
        should_alert(
            100.0,
            99.0,
            "in_stock",
            target_price=50.0,
            all_time_low=100.0,
            atl_policy="aggressive",
        )
        is True
    )


def test_atl_conservative_fires_when_it_clears_micro_drop_floor():
    # 200 -> 190 is a 10 RON / 5% drop: clears the micro-drop floor, so the
    # new-low override fires under "conservative" even with an unmet
    # target_price.
    assert (
        should_alert(
            200.0,
            190.0,
            "in_stock",
            target_price=50.0,
            all_time_low=200.0,
            atl_policy="conservative",
        )
        is True
    )


def test_atl_conservative_suppressed_when_it_fails_micro_drop_floor():
    # Only a 1 RON, 1% new low: under "conservative" the override does not
    # apply, and the drop is too small to pass the normal gates either.
    assert (
        should_alert(
            100.0, 99.0, "in_stock", all_time_low=100.0, atl_policy="conservative"
        )
        is False
    )


def test_atl_conservative_still_respects_target_price_when_floor_unmet():
    # Same 1 RON new low as above, this time with a target_price that also
    # isn't met — confirms the fallthrough to normal gates, not just the
    # floor check in isolation.
    assert (
        should_alert(
            100.0,
            99.0,
            "in_stock",
            target_price=50.0,
            all_time_low=100.0,
            atl_policy="conservative",
        )
        is False
    )


def test_atl_off_ignores_new_low_and_respects_min_drop_percent():
    # A genuine new all-time low, but atl_policy="off" means it's judged
    # purely on min_drop_percent like any other drop. 100 -> 90 is a 10%
    # drop, threshold is 50%, so it must be suppressed.
    assert (
        should_alert(
            100.0,
            90.0,
            "in_stock",
            min_drop_percent=50.0,
            all_time_low=100.0,
            atl_policy="off",
        )
        is False
    )


def test_atl_off_still_alerts_when_normal_gates_pass():
    # Same new all-time low, but this time min_drop_percent is met on its
    # own merits — "off" doesn't suppress alerts, it just removes the
    # override shortcut.
    assert (
        should_alert(
            100.0,
            90.0,
            "in_stock",
            min_drop_percent=5.0,
            all_time_low=100.0,
            atl_policy="off",
        )
        is True
    )


def test_atl_off_suppressed_by_unmet_target_price():
    assert (
        should_alert(
            100.0,
            90.0,
            "in_stock",
            target_price=50.0,
            all_time_low=100.0,
            atl_policy="off",
        )
        is False
    )


def test_atl_policy_no_atl_condition_regular_rules_unchanged():
    # new_price isn't below all_time_low at all — atl_policy must be
    # irrelevant in every branch since the override never applies.
    for policy in ("aggressive", "conservative", "off"):
        assert (
            should_alert(
                100.0,
                90.0,
                "in_stock",
                min_drop_percent=5.0,
                all_time_low=80.0,
                atl_policy=policy,
            )
            is True
        )


def test_atl_policy_invalid_value_raises():
    with pytest.raises(ValueError):
        should_alert(100.0, 90.0, "in_stock", all_time_low=100.0, atl_policy="bogus")


def test_title_matches_query_rejects_unrelated_product():
    assert title_matches_query("Acer Nitro V15", "laptop lenovo v15") is False


def test_title_matches_query_rejects_partial_number_match():
    assert title_matches_query("Xiaomi 15T", "iphone 15") is False


def test_title_matches_query_accepts_case_and_order_insensitive_match():
    assert title_matches_query("LAPTOP Lenovo V15 G4 AMN", "lenovo laptop v15") is True


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


def test_record_observation_stores_utc_timestamp_alongside_date():
    entry = {"title": "Product", "site": "emag", "history": []}
    record_observation(
        entry, "2026-01-01", "2026-01-01T10:00:00+00:00", 100.0, "in_stock"
    )
    assert entry["history"] == [
        {
            "date": "2026-01-01",
            "observed_at": "2026-01-01T10:00:00+00:00",
            "price": 100.0,
            "stock_status": "in_stock",
        }
    ]


def _watchlist_entry(**overrides):
    result = {
        "title": "Laptop Lenovo V15",
        "price": 2999.99,
        "url": "https://www.emag.ro/laptop-lenovo-v15/pd/ABC123/",
        "stock_status": "in_stock",
        "seller": None,
        "is_marketplace": None,
    }
    result.update(overrides)
    return result


def test_two_runs_same_date_produce_two_observations_with_utc_timestamps(
    tmp_path, monkeypatch
):
    # Regression test for T-05: the old "already recorded today" skip meant a
    # second run on the same calendar date (e.g. the 2-hour cron cadence, or
    # a manual re-run) silently dropped its observation. Two full main() runs
    # in the same run_id-scoped moment must now both persist.
    watchlist_file = tmp_path / "watchlist.json"
    watchlist_file.write_text(
        json.dumps([{"site": "emag", "query": "laptop lenovo v15"}]),
        encoding="utf-8",
    )
    history_file = tmp_path / "price_history.json"
    alerts_file = tmp_path / "alerts.json"

    monkeypatch.setattr(scrape, "WATCHLIST_FILE", watchlist_file)
    monkeypatch.setattr(scrape, "HISTORY_FILE", history_file)
    monkeypatch.setattr(scrape, "ALERTS_FILE", alerts_file)
    monkeypatch.setattr(
        scrape, "SCRAPERS", {"emag": lambda query: [_watchlist_entry()]}
    )
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)

    scrape.main()
    scrape.main()

    products = json.loads(history_file.read_text(encoding="utf-8"))["products"]
    history = next(iter(products.values()))["history"]

    assert len(history) == 2
    dates = {h["date"] for h in history}
    assert len(dates) == 1  # both runs landed on the same calendar date

    timestamps = [h["observed_at"] for h in history]
    assert len(set(timestamps)) == 2  # each run recorded its own instant
    for ts in timestamps:
        assert ts.endswith("+00:00")  # timezone-aware UTC, not naive local time


def test_daily_summary_and_lifetime_extrema_still_work_with_multiple_same_day_entries():
    # Regression test: consumers that group/derive stats by calendar date
    # (update_lifetime_stats, prune_history) must keep working even when a
    # single day now holds more than one observation.
    entry = {
        "title": "Product",
        "site": "emag",
        "history": [
            {
                "date": "2026-01-01",
                "observed_at": "2026-01-01T08:00:00+00:00",
                "price": 100.0,
                "stock_status": "in_stock",
            }
        ],
    }
    update_lifetime_stats(entry, 100.0, "2026-01-01")
    record_observation(
        entry, "2026-01-01", "2026-01-01T10:00:00+00:00", 90.0, "in_stock"
    )
    update_lifetime_stats(entry, 90.0, "2026-01-01")

    assert entry["all_time_low"] == 90.0
    assert entry["all_time_high"] == 100.0
    assert [h["price"] for h in entry["history"]] == [100.0, 90.0]

    pruned = prune_history(entry["history"], date(2026, 1, 1))
    assert [h["price"] for h in pruned] == [100.0, 90.0]


def test_canonicalize_url_strips_utm_and_known_tracking_params():
    url = (
        "https://www.emag.ro/laptop-lenovo-v15/pd/ABC123/"
        "?utm_source=fb&utm_campaign=bf&fbclid=xyz&gclid=abc"
    )
    assert canonicalize_url(url) == "https://emag.ro/laptop-lenovo-v15/pd/ABC123"


def test_canonicalize_url_normalizes_www_and_trailing_slash():
    assert (
        canonicalize_url("https://WWW.emag.ro/laptop-lenovo-v15/pd/ABC123/")
        == "https://emag.ro/laptop-lenovo-v15/pd/ABC123"
    )


def test_canonicalize_url_keeps_non_tracking_query_params():
    url = "https://www.pcgarage.ro/produs-123/?color=black&utm_source=fb"
    assert canonicalize_url(url) == "https://pcgarage.ro/produs-123?color=black"


def test_canonicalize_url_tracking_variants_produce_same_key():
    a = "https://www.emag.ro/laptop-lenovo-v15/pd/ABC123/?utm_source=google"
    b = "https://emag.ro/laptop-lenovo-v15/pd/ABC123?ref=homepage&fbclid=999"
    assert canonicalize_url(a) == canonicalize_url(b)


def test_tracking_param_variant_does_not_create_duplicate_offer(tmp_path, monkeypatch):
    # Regression test for T-06: a retailer swapping tracking params between
    # runs (e.g. a different ad campaign each time) must not fork one
    # product's history into two separate offers.
    watchlist_file = tmp_path / "watchlist.json"
    watchlist_file.write_text(
        json.dumps([{"site": "emag", "query": "laptop lenovo v15"}]),
        encoding="utf-8",
    )
    history_file = tmp_path / "price_history.json"
    alerts_file = tmp_path / "alerts.json"

    monkeypatch.setattr(scrape, "WATCHLIST_FILE", watchlist_file)
    monkeypatch.setattr(scrape, "HISTORY_FILE", history_file)
    monkeypatch.setattr(scrape, "ALERTS_FILE", alerts_file)
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)

    url_variant_1 = "https://www.emag.ro/laptop-lenovo-v15/pd/ABC123/?utm_source=fb"
    url_variant_2 = "https://emag.ro/laptop-lenovo-v15/pd/ABC123?ref=email&fbclid=999"

    monkeypatch.setattr(
        scrape,
        "SCRAPERS",
        {"emag": lambda query: [_watchlist_entry(url=url_variant_1)]},
    )
    scrape.main()

    monkeypatch.setattr(
        scrape,
        "SCRAPERS",
        {"emag": lambda query: [_watchlist_entry(url=url_variant_2)]},
    )
    scrape.main()

    products = json.loads(history_file.read_text(encoding="utf-8"))["products"]
    assert len(products) == 1
    history = next(iter(products.values()))["history"]
    assert len(history) == 2


def test_load_history_migrates_legacy_raw_url_keys_to_canonical(tmp_path, monkeypatch):
    # Regression test for T-06: price_history.json predating canonicalization
    # is keyed by raw URLs (with "www." and a trailing slash). On load, those
    # keys must be remapped to their canonical form so an already-tracked
    # product keeps its history instead of forking into a fresh duplicate on
    # the next run.
    history_file = tmp_path / "price_history.json"
    raw_key = "https://www.emag.ro/laptop-lenovo-v15/pd/ABC123/"
    history_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "products": {
                    raw_key: {
                        "title": "Laptop Lenovo V15",
                        "site": "emag",
                        "all_time_low": 2499.99,
                        "all_time_high": 2999.0,
                        "first_seen": "2026-01-01",
                        "history": [
                            {
                                "date": "2026-01-01",
                                "observed_at": "2026-01-01T10:00:00+00:00",
                                "price": 2999.0,
                                "stock_status": "in_stock",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(scrape, "HISTORY_FILE", history_file)

    products = scrape.load_history()

    assert list(products.keys()) == [canonicalize_url(raw_key)]
    assert products[canonicalize_url(raw_key)]["all_time_low"] == 2499.99


def test_load_history_merges_pre_existing_tracking_param_duplicates(
    tmp_path, monkeypatch
):
    # Regression test for T-06: price_history.json can already hold two raw
    # keys that only differ by tracking params (the exact bug T-06 fixes
    # going forward). On load these must merge into one entry rather than
    # silently keeping just one and dropping the other's history/extrema.
    history_file = tmp_path / "price_history.json"
    raw_key_a = "https://www.emag.ro/laptop-lenovo-v15/pd/ABC123/?utm_source=fb"
    raw_key_b = "https://emag.ro/laptop-lenovo-v15/pd/ABC123?ref=email"
    history_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "products": {
                    raw_key_a: {
                        "title": "Laptop Lenovo V15",
                        "site": "emag",
                        "all_time_low": 2999.0,
                        "all_time_high": 2999.0,
                        "first_seen": "2026-01-05",
                        "history": [
                            {
                                "date": "2026-01-05",
                                "observed_at": "2026-01-05T10:00:00+00:00",
                                "price": 2999.0,
                                "stock_status": "in_stock",
                            }
                        ],
                    },
                    raw_key_b: {
                        "title": "Laptop Lenovo V15",
                        "site": "emag",
                        "all_time_low": 2499.99,
                        "all_time_high": 3099.0,
                        "first_seen": "2026-01-01",
                        "history": [
                            {
                                "date": "2026-01-01",
                                "observed_at": "2026-01-01T09:00:00+00:00",
                                "price": 3099.0,
                                "stock_status": "in_stock",
                            }
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(scrape, "HISTORY_FILE", history_file)

    products = scrape.load_history()

    assert len(products) == 1
    entry = next(iter(products.values()))
    assert entry["all_time_low"] == 2499.99
    assert entry["all_time_high"] == 3099.0
    assert entry["first_seen"] == "2026-01-01"
    assert [h["price"] for h in entry["history"]] == [3099.0, 2999.0]
