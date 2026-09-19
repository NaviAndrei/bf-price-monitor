"""T-08: HTML fixture contract tests.

Each test feeds a static HTML fixture straight into the store's real
extraction path (scrape_*_listing) by monkeypatching only the network call
(fetch / fetch_with_browser) — the CSS/attribute selectors themselves run
unmodified. A selector change in scrape.py that breaks a store's markup
contract will fail these tests without needing network access.

T-07 additions: the "jsonld_priority" fixtures carry a JSON-LD Product/Offer
block whose price deliberately disagrees with the visible CSS price, proving
the JSON-LD layer is tried (and wins) before the CSS fallback. The
"no_price" fixtures (title/href present, no price anywhere) double as the
failure-capture case, since every extraction layer is expected to fail
against them.
"""

import json
from pathlib import Path

import pytest
import scrape

FIXTURES_DIR = Path(__file__).parent / "fixtures"
QUERY = "laptop lenovo v15"


def _load_fixture(store: str, name: str) -> str:
    return (FIXTURES_DIR / store / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_extraction_failures_log(monkeypatch, tmp_path):
    # Several fixtures here (no_price, changed_markup) exercise the "every
    # layer failed" path by design, which would otherwise append real
    # records to the project's own data/extraction_failures.jsonl on every
    # test run. Redirected here rather than per-test, so no test can leak a
    # write regardless of which fixture triggers it.
    monkeypatch.setattr(
        scrape, "EXTRACTION_FAILURES_FILE", tmp_path / "extraction_failures.jsonl"
    )


# --- eMAG (plain requests via fetch()) -------------------------------------


def test_emag_success_extracts_price_title_and_stock(monkeypatch):
    monkeypatch.setattr(
        scrape, "fetch", lambda url, site: _load_fixture("emag", "success")
    )
    results = scrape.scrape_emag_listing(QUERY)
    assert len(results) == 1
    assert results[0]["price"] == 2999.99
    assert results[0]["stock_status"] == "in_stock"
    assert "Lenovo V15" in results[0]["title"]


def test_emag_no_price_yields_no_results(monkeypatch):
    monkeypatch.setattr(
        scrape, "fetch", lambda url, site: _load_fixture("emag", "no_price")
    )
    assert scrape.scrape_emag_listing(QUERY) == []


def test_emag_out_of_stock_flags_stock_status(monkeypatch):
    monkeypatch.setattr(
        scrape, "fetch", lambda url, site: _load_fixture("emag", "out_of_stock")
    )
    results = scrape.scrape_emag_listing(QUERY)
    assert len(results) == 1
    assert results[0]["stock_status"] == "out_of_stock"
    assert results[0]["price"] == 2799.0


def test_emag_changed_markup_breaks_current_selectors(monkeypatch):
    monkeypatch.setattr(
        scrape, "fetch", lambda url, site: _load_fixture("emag", "changed_markup")
    )
    assert scrape.scrape_emag_listing(QUERY) == []


def test_emag_jsonld_price_wins_over_stale_css_price(monkeypatch):
    monkeypatch.setattr(
        scrape, "fetch", lambda url, site: _load_fixture("emag", "jsonld_priority")
    )
    results = scrape.scrape_emag_listing(QUERY)
    assert len(results) == 1
    assert results[0]["price"] == 2599.00


# --- PC Garage (Playwright via fetch_with_browser()) ------------------------


def test_pcgarage_success_extracts_price_title_and_stock(monkeypatch):
    monkeypatch.setattr(
        scrape,
        "fetch_with_browser",
        lambda url, site: _load_fixture("pcgarage", "success"),
    )
    results = scrape.scrape_pcgarage_listing(QUERY)
    assert len(results) == 1
    assert results[0]["price"] == 1798.99
    assert results[0]["stock_status"] == "in_stock"
    assert "Lenovo V15" in results[0]["title"]


def test_pcgarage_no_price_yields_no_results(monkeypatch):
    monkeypatch.setattr(
        scrape,
        "fetch_with_browser",
        lambda url, site: _load_fixture("pcgarage", "no_price"),
    )
    assert scrape.scrape_pcgarage_listing(QUERY) == []


def test_pcgarage_out_of_stock_flags_stock_status(monkeypatch):
    monkeypatch.setattr(
        scrape,
        "fetch_with_browser",
        lambda url, site: _load_fixture("pcgarage", "out_of_stock"),
    )
    results = scrape.scrape_pcgarage_listing(QUERY)
    assert len(results) == 1
    assert results[0]["stock_status"] == "out_of_stock"
    assert results[0]["price"] == 1699.0


def test_pcgarage_changed_markup_breaks_current_selectors(monkeypatch):
    monkeypatch.setattr(
        scrape,
        "fetch_with_browser",
        lambda url, site: _load_fixture("pcgarage", "changed_markup"),
    )
    assert scrape.scrape_pcgarage_listing(QUERY) == []


def test_pcgarage_jsonld_price_wins_over_stale_css_price(monkeypatch):
    monkeypatch.setattr(
        scrape,
        "fetch_with_browser",
        lambda url, site: _load_fixture("pcgarage", "jsonld_priority"),
    )
    results = scrape.scrape_pcgarage_listing(QUERY)
    assert len(results) == 1
    assert results[0]["price"] == 1699.00


# --- Flanco (Playwright via fetch_with_browser()) ---------------------------


def test_flanco_success_extracts_price_title_and_stock(monkeypatch):
    monkeypatch.setattr(
        scrape,
        "fetch_with_browser",
        lambda url, site: _load_fixture("flanco", "success"),
    )
    results = scrape.scrape_flanco_listing(QUERY)
    assert len(results) == 1
    assert results[0]["price"] == 2398.99
    assert results[0]["stock_status"] == "in_stock"
    assert "Lenovo V15" in results[0]["title"]


def test_flanco_no_price_yields_no_results(monkeypatch):
    monkeypatch.setattr(
        scrape,
        "fetch_with_browser",
        lambda url, site: _load_fixture("flanco", "no_price"),
    )
    assert scrape.scrape_flanco_listing(QUERY) == []


def test_flanco_out_of_stock_flags_stock_status(monkeypatch):
    monkeypatch.setattr(
        scrape,
        "fetch_with_browser",
        lambda url, site: _load_fixture("flanco", "out_of_stock"),
    )
    results = scrape.scrape_flanco_listing(QUERY)
    assert len(results) == 1
    assert results[0]["stock_status"] == "out_of_stock"
    assert results[0]["price"] == 2199.0


def test_flanco_changed_markup_breaks_current_selectors(monkeypatch):
    monkeypatch.setattr(
        scrape,
        "fetch_with_browser",
        lambda url, site: _load_fixture("flanco", "changed_markup"),
    )
    assert scrape.scrape_flanco_listing(QUERY) == []


def test_flanco_jsonld_price_wins_over_stale_css_price(monkeypatch):
    monkeypatch.setattr(
        scrape,
        "fetch_with_browser",
        lambda url, site: _load_fixture("flanco", "jsonld_priority"),
    )
    results = scrape.scrape_flanco_listing(QUERY)
    assert len(results) == 1
    assert results[0]["price"] == 2199.00


# --- T-07: failure capture ---------------------------------------------------


def test_failed_extraction_writes_failure_log_record(monkeypatch):
    monkeypatch.setattr(
        scrape, "fetch", lambda url, site: _load_fixture("emag", "no_price")
    )

    results = scrape.scrape_emag_listing(QUERY)

    failures_file = scrape.EXTRACTION_FAILURES_FILE
    assert results == []
    assert failures_file.exists()
    lines = failures_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["store"] == "emag"
    assert record["query"] == QUERY
    assert "url" in record
    assert "timestamp_utc" in record
    assert "failure_reason" in record
    assert "html" not in record
