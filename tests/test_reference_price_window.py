"""T-51: characterization of the 30-day reference price as scrape.py computes it.

These tests pin current behaviour; they do not argue it is the legal rule.
The window is read per product URL (so per retailer), covers the 30 days
before today, excludes the observation being recorded, and falls back to the
previous price when the window is empty. Files are redirected to tmp_path and
the scrapers are fake, so nothing touches data/ or the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import scrape

TODAY = datetime.now(UTC).date()
URLS = {
    "emag": "https://www.emag.ro/tableta-x/pd/EMAGX/",
    "pcgarage": "https://www.pcgarage.ro/tableta-x/",
}


def _entry(site: str, days_ago_to_price: dict[int, float]) -> dict:
    rows = sorted(days_ago_to_price.items(), reverse=True)  # oldest first
    history = [
        {
            "date": (TODAY - timedelta(days=d)).isoformat(),
            "observed_at": f"{(TODAY - timedelta(days=d)).isoformat()}T09:00:00+00:00",
            "price": p,
            "stock_status": "in_stock",
        }
        for d, p in rows
    ]
    prices = [p for _, p in rows]
    return {
        "title": "Tableta X",
        "site": site,
        "all_time_low": min(prices),
        "all_time_high": max(prices),
        "first_seen": history[0]["date"],
        "history": history,
    }


def _run_once(tmp_path, monkeypatch, history_by_site, new_prices):
    history_file = tmp_path / "price_history.json"
    alerts_file = tmp_path / "alerts.json"
    products = {
        scrape.canonicalize_url(URLS[site]): _entry(site, rows)
        for site, rows in history_by_site.items()
    }
    history_file.write_text(
        json.dumps({"schema_version": 1, "products": products}), encoding="utf-8"
    )

    def scraper_for(site):
        return lambda query: [
            {
                "title": "Tableta X",
                "price": new_prices[site],
                "url": URLS[site],
                "stock_status": "in_stock",
                "seller": site,
                "is_marketplace": False,
            }
        ]

    monkeypatch.setattr(scrape, "HISTORY_FILE", history_file)
    monkeypatch.setattr(scrape, "ALERTS_FILE", alerts_file)
    monkeypatch.setattr(scrape, "DB_FILE", tmp_path / "price_history.db")
    monkeypatch.setattr(scrape, "SCRAPE_HEALTH_FILE", tmp_path / "health.jsonl")
    monkeypatch.setattr(scrape, "SCRAPE_HEALTH_ALERTS_FILE", tmp_path / "h_alerts.json")
    monkeypatch.setattr(
        scrape, "EXTRACTION_FAILURES_FILE", tmp_path / "extraction_failures.jsonl"
    )
    monkeypatch.setattr(
        scrape, "SCRAPERS", {site: scraper_for(site) for site in history_by_site}
    )
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)
    watchlist = [
        {"id": f"w-{site}", "site": site, "query": "tableta x"}
        for site in history_by_site
    ]
    scrape._run(watchlist)
    alerts = json.loads(alerts_file.read_text(encoding="utf-8"))
    return {a["site"]: a for a in alerts}


def test_twenty_days_of_history_gives_the_lowest_price_of_those_days(
    tmp_path, monkeypatch
):
    rows = {20: 120.0, 15: 110.0, 10: 80.0, 5: 100.0, 1: 130.0}
    alerts = _run_once(tmp_path, monkeypatch, {"emag": rows}, {"emag": 90.0})
    alert = alerts["emag"]
    assert alert["thirty_day_low"] == 80.0
    assert alert["history_days"] == 20
    assert alert["old_price"] == 130.0  # previous observation, not the low


def test_observations_older_than_thirty_days_are_outside_the_window(
    tmp_path, monkeypatch
):
    rows = {45: 10.0, 29: 120.0, 3: 140.0}
    alerts = _run_once(tmp_path, monkeypatch, {"emag": rows}, {"emag": 100.0})
    assert alerts["emag"]["thirty_day_low"] == 120.0  # the 45-day-old 10.0 is ignored


def test_empty_window_falls_back_to_the_previous_price(tmp_path, monkeypatch):
    rows = {60: 90.0, 40: 150.0}
    alerts = _run_once(tmp_path, monkeypatch, {"emag": rows}, {"emag": 100.0})
    assert alerts["emag"]["thirty_day_low"] == 150.0


def test_same_product_at_two_retailers_never_mixes_windows(tmp_path, monkeypatch):
    emag = {20: 200.0, 10: 80.0, 2: 150.0}
    pcgarage = {20: 200.0, 10: 170.0, 2: 190.0}
    alerts = _run_once(
        tmp_path,
        monkeypatch,
        {"emag": emag, "pcgarage": pcgarage},
        {"emag": 100.0, "pcgarage": 120.0},
    )
    assert alerts["emag"]["thirty_day_low"] == 80.0
    assert alerts["pcgarage"]["thirty_day_low"] == 170.0
    assert {a["site"] for a in alerts.values()} == {"emag", "pcgarage"}


@pytest.mark.parametrize("site", ["emag", "pcgarage"])
def test_todays_own_observation_is_excluded_from_the_window(
    tmp_path, monkeypatch, site
):
    rows = {20: 200.0, 2: 150.0}
    alerts = _run_once(tmp_path, monkeypatch, {site: rows}, {site: 60.0})
    assert alerts[site]["thirty_day_low"] == 150.0  # not the new 60.0
