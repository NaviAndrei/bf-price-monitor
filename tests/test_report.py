import json
import sqlite3
from datetime import UTC, datetime
from html.parser import HTMLParser

import analyze
import notify
import pytest
import report

GENERATED_AT = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

EMAG_URL = "https://emag.ro/laptop-lenovo-v15/pd/AAA111"
PCG_URL = "https://pcgarage.ro/laptop-asus-vivobook-x/"
STALE_URL = "https://emag.ro/laptop-lenovo-v15-old/pd/BBB222"
SINGLE_URL = "https://flanco.ro/laptop-lenovo-v15-single/"


def _obs(day: str, price: float, stock: str = "in_stock") -> dict:
    return {
        "date": day,
        "observed_at": f"{day}T10:00:00+00:00",
        "price": price,
        "stock_status": stock,
    }


def _formatted_alert(**overrides) -> dict:
    alert = {
        "title": "Laptop Lenovo V15 G4",
        "site": "emag",
        "query": "laptop lenovo v15",
        "url": EMAG_URL,
        "old_price": 2999.99,
        "new_price": 2599.99,
        "thirty_day_low": 2899.99,
        "history_days": 21,
        "reference_price": None,
        "stock_status": "in_stock",
        "seller": None,
        "is_marketplace": None,
        "all_time_low": 2599.99,
        "all_time_high": 3099.99,
        "observed_at": "2026-09-27T10:00:00+00:00",
        "discount_vs_old_pct": 13.33,
        "discount_vs_30d_pct": 10.34,
        "rule_verdict": "GENUINE_DEAL",
        "verdict": "GENUINE_DEAL",
        "verdict_score": 8,
        "summary": "Prețul este sub minimul ultimelor 30 de zile (2899.99 RON).",
        "is_recommended": True,
        "deal_stats": {
            "observation_count": 5,
            "reference_price_30d": 2899.99,
            "genuine_savings_percent": 10.34,
            "is_legal_discount": True,
            "pre_sale_hike_detected": False,
            "fake_discount_suspect": False,
            "fake_discount_reasons": [],
            "history_30d_recorded": True,
        },
    }
    alert.update(overrides)
    return alert


def _write_data_dir(tmp_path, *, alerts=None, health=None, health_alerts=None):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    watchlist = {
        "watches": [
            {
                "id": "babebbc9-2a56-5d0c-b57c-9ca8ad44d7b4",
                "owner": "tester",
                "site": "emag",
                "query": "laptop lenovo v15",
                "target_price": 2500.0,
                "enabled": True,
            },
            {
                "id": "4d24a18d-89d8-59f0-8798-68babdceb715",
                "owner": "tester",
                "site": "pcgarage",
                "query": "laptop asus vivobook",
                "min_drop_percent": 5.0,
                "enabled": True,
            },
        ]
    }
    (data_dir / "watchlist.json").write_text(json.dumps(watchlist), encoding="utf-8")
    products = {
        EMAG_URL: {
            "title": "Laptop Lenovo V15 G4",
            "site": "emag",
            "history": [
                _obs("2026-09-06", 3099.99),
                _obs("2026-09-10", 2999.99),
                _obs("2026-09-15", 2899.99),
                _obs("2026-09-20", 2999.99),
                _obs("2026-09-27", 2599.99, "limited_stock"),
            ],
            "all_time_low": 2599.99,
            "all_time_high": 3099.99,
        },
        PCG_URL: {
            "title": "Laptop ASUS Vivobook X",
            "site": "pcgarage",
            "history": [_obs("2026-09-20", 3499.0), _obs("2026-09-27", 3399.0)],
            "all_time_low": 3399.0,
            "all_time_high": 3499.0,
        },
        # Prior observation is older than the 30-day window: the reference
        # falls back to the previous observed price (confirmed empty window).
        STALE_URL: {
            "title": "Laptop Lenovo V15 old stock",
            "site": "emag",
            "history": [_obs("2026-08-01", 2799.0), _obs("2026-09-27", 2699.0)],
            "all_time_low": 2699.0,
            "all_time_high": 2799.0,
        },
        SINGLE_URL: {
            "title": "Laptop Lenovo V15 single",
            "site": "flanco",
            "history": [_obs("2026-09-27", 2499.0)],
        },
    }
    (data_dir / "price_history.json").write_text(
        json.dumps({"schema_version": 1, "products": products}), encoding="utf-8"
    )
    if health is None:
        health = [
            {
                "store": "emag",
                "run_id": "run-1",
                "run_started_utc": "2026-09-27T10:00:00+00:00",
                "watches_requested": 1,
                "products_parsed": 12,
                "matched_count": 1,
                "policy_blocked_count": 0,
                "parse_failures": 0,
                "challenge_detected": False,
                "challenge_wait_entered": False,
                "latency_seconds": 4.2,
                "last_known_good_utc": "2026-09-27T10:00:00+00:00",
            },
            {
                "store": "pcgarage",
                "run_id": "run-1",
                "run_started_utc": "2026-09-27T10:00:00+00:00",
                "watches_requested": 1,
                "products_parsed": 0,
                "matched_count": 0,
                "policy_blocked_count": 0,
                "parse_failures": 1,
                "challenge_detected": True,
                "challenge_wait_entered": True,
                "latency_seconds": 30.0,
                "last_known_good_utc": "2026-09-25T10:00:00+00:00",
            },
        ]
    (data_dir / "scrape_health.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in health), encoding="utf-8"
    )
    (data_dir / "scrape_health_alerts.json").write_text(
        json.dumps(health_alerts or []), encoding="utf-8"
    )
    (data_dir / "formatted_alerts.json").write_text(
        json.dumps(alerts if alerts is not None else [_formatted_alert()]),
        encoding="utf-8",
    )
    return data_dir


def _alert_cards(html: str) -> list[str]:
    return [c.split("</article>")[0] for c in html.split('class="alert-card')[1:]]


def _render(data_dir) -> str:
    return report.build_report_html(report.load_report_data(data_dir), GENERATED_AT)


class _BalanceChecker(HTMLParser):
    VOID = {"meta", "br", "hr", "img", "input", "link", "circle", "line", "polyline"}

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.tags.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}>, open: {self.stack[-3:]}")
            return
        self.stack.pop()


def _assert_well_formed(html: str) -> _BalanceChecker:
    checker = _BalanceChecker()
    checker.feed(html)
    checker.close()
    assert checker.errors == []
    assert checker.stack == []
    return checker


def test_report_renders_all_sections_from_fixture_data(tmp_path):
    html = _render(_write_data_dir(tmp_path))

    assert html.startswith("<!DOCTYPE html>")
    checker = _assert_well_formed(html)
    for section in ('id="alerts"', 'id="watches"', 'id="health"'):
        assert section in html
    # Watches, with their matched products under them.
    assert "laptop lenovo v15" in html
    assert "laptop asus vivobook" in html
    assert "Laptop Lenovo V15 G4" in html
    assert "Laptop ASUS Vivobook X" in html
    # History: one inline SVG sparkline per product with >= 2 observations.
    assert checker.tags.count("svg") >= 3
    assert "<polyline" in html
    # The 30-day reference line is drawn and labelled.
    assert 'class="ref-line"' in html
    assert "Minim 30 zile (Omnibus)" in html
    # Store health.
    assert "eMAG" in html and "PC Garage" in html
    assert "challenge" in html.lower()
    # Recent alert with its reason.
    assert "Prețul este sub minimul ultimelor 30 de zile" in html
    assert notify.VERDICT_BADGES["GENUINE_DEAL"][1] in html


def test_report_is_self_contained_with_no_scripts_or_remote_resources(tmp_path):
    html = _render(_write_data_dir(tmp_path)).lower()

    assert "<script" not in html
    assert "<link" not in html
    assert "<iframe" not in html
    assert "@import" not in html
    assert "url(" not in html
    assert 'src="http' not in html
    assert "src='http" not in html
    assert "cdn" not in html
    assert "<style>" in html  # CSS is inline


def test_alert_reference_labels_reuse_notify_wording_for_all_three_states(
    tmp_path,
):
    genuine = _formatted_alert(title="Genuine window product")
    confirmed_empty = _formatted_alert(
        title="Confirmed empty product",
        deal_stats={
            "observation_count": 0,
            "reference_price_30d": None,
            "genuine_savings_percent": None,
            "is_legal_discount": None,
            "pre_sale_hike_detected": False,
            "fake_discount_suspect": False,
            "fake_discount_reasons": [],
            "history_30d_recorded": True,
        },
    )
    unknown = _formatted_alert(title="Legacy unknown product")
    del unknown["deal_stats"]
    html = _render(
        _write_data_dir(tmp_path, alerts=[genuine, confirmed_empty, unknown])
    )

    # The report embeds exactly what the alert message showed.
    for alert in (genuine, confirmed_empty, unknown):
        assert notify._thirty_day_low_line(alert) in html
    assert "Minim 30 zile (Omnibus)" in html
    assert "Ultimul preț observat (fără observații în ultimele 30 zile)" in html
    assert "Preț de referință (istoric pe 30 de zile neconfirmat)" in html
    # Each label sits in its own alert's card, not a neighbour's.
    cards = _alert_cards(html)
    assert len(cards) == 3
    assert "Minim 30 zile (Omnibus)" in cards[0]
    assert "fără observații" in cards[1]
    assert "neconfirmat" in cards[2]
    assert "Minim 30 zile (Omnibus)" not in cards[1] + cards[2]


def test_product_reference_uses_analyze_provenance_logic(tmp_path):
    data = report.load_report_data(_write_data_dir(tmp_path))

    rows = {row["url"]: row for row in report.build_product_rows(data["products"])}
    # Prior 30-day window nonempty: genuine 30-day minimum of the *prior*
    # observations (the latest 2599.99 is excluded, as scrape.py does).
    assert rows[EMAG_URL]["provenance"] == analyze.WINDOW_GENUINE
    assert rows[EMAG_URL]["thirty_day_low"] == 2899.99
    # Prior observation older than 30 days: previous-price fallback.
    assert rows[STALE_URL]["provenance"] == analyze.WINDOW_CONFIRMED_EMPTY
    assert rows[STALE_URL]["thirty_day_low"] == 2799.0
    # No prior observation at all: no reference price to show.
    assert rows[SINGLE_URL]["thirty_day_low"] is None

    html = report.build_report_html(data, GENERATED_AT)
    assert "Ultimul preț observat (fără observații în ultimele 30 zile)" in html


def test_badge_taxonomy_for_deal_and_verdict_states(tmp_path):
    alert = _formatted_alert(
        stock_status="limited_stock",
        deal_stats={
            "observation_count": 5,
            "reference_price_30d": 2899.99,
            "genuine_savings_percent": 10.34,
            "is_legal_discount": True,
            "pre_sale_hike_detected": True,
            "fake_discount_suspect": False,
            "fake_discount_reasons": [],
            "history_30d_recorded": True,
        },
    )
    false_discount = _formatted_alert(
        title="Fake sale",
        new_price=2999.99,
        all_time_low=2599.99,
        verdict="FALSE_DISCOUNT",
        rule_verdict="FALSE_DISCOUNT",
        deal_stats={
            "observation_count": 5,
            "reference_price_30d": 2899.99,
            "genuine_savings_percent": -3.45,
            "is_legal_discount": False,
            "pre_sale_hike_detected": False,
            "fake_discount_suspect": True,
            "fake_discount_reasons": ["OBSERVED_PRE_SALE_HIKE"],
            "history_30d_recorded": True,
        },
    )
    html = _render(_write_data_dir(tmp_path, alerts=[alert, false_discount]))
    cards = _alert_cards(html)

    assert "GENUINE ALL-TIME LOW" in cards[0]
    assert "OMNIBUS VERIFIED" in cards[0]
    assert "PRE-HIKE DETECTED" in cards[0]
    assert "LIMITED STOCK" in cards[0]
    assert notify.VERDICT_BADGES["FALSE_DISCOUNT"][1] in cards[1]
    assert "GENUINE ALL-TIME LOW" not in cards[1]
    assert "OMNIBUS VERIFIED" not in cards[1]
    # Fake-discount reason text comes from notify's own reason mapping.
    assert notify.FAKE_DISCOUNT_REASON_TEXT["OBSERVED_PRE_SALE_HIKE"] in cards[1]


def test_quarantined_store_and_health_alerts_are_shown(tmp_path):
    def rec(store, run_id, parsed):
        return {
            "store": store,
            "run_id": run_id,
            "run_started_utc": "2026-09-27T10:00:00+00:00",
            "watches_requested": 1,
            "products_parsed": parsed,
            "parse_failures": 0,
            "challenge_detected": False,
            "latency_seconds": 1.0,
            "last_known_good_utc": None,
        }

    health = [
        rec("flanco", "r1", 0),
        rec("emag", "r1", 5),
        rec("flanco", "r2", 0),
        rec("emag", "r2", 6),
    ]
    html = _render(
        _write_data_dir(
            tmp_path,
            health=health,
            health_alerts=["🚨 SCRAPER BREAKDOWN: Flanco returned 0 items"],
        )
    )

    assert "QUARANTINED" in html
    assert "SCRAPER BREAKDOWN: Flanco returned 0 items" in html


def test_empty_data_dir_does_not_crash(tmp_path):
    html = _render(tmp_path)

    _assert_well_formed(html)
    for section in ('id="alerts"', 'id="watches"', 'id="health"'):
        assert section in html
    assert "No alerts" in html
    assert "No store health records" in html


def test_malformed_inputs_degrade_instead_of_crashing(tmp_path):
    data_dir = _write_data_dir(tmp_path)
    (data_dir / "watchlist.json").write_text("{not json", encoding="utf-8")
    with open(data_dir / "scrape_health.jsonl", "a", encoding="utf-8") as f:
        f.write("{truncated line\n")
    (data_dir / "formatted_alerts.json").write_text("", encoding="utf-8")

    html = _render(data_dir)

    _assert_well_formed(html)
    assert "Watchlist could not be loaded" in html
    # Products are still listed even without watches to group them under.
    assert "Laptop ASUS Vivobook X" in html
    assert "eMAG" in html


def test_untrusted_text_is_escaped_and_unsafe_links_dropped(tmp_path):
    evil = _formatted_alert(
        title="<script>alert(1)</script>",
        summary="<img src=x onerror=alert(1)>",
        url="javascript:alert(1)",
    )
    html = _render(_write_data_dir(tmp_path, alerts=[evil]))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img" not in html
    assert "javascript:" not in html


def test_main_writes_report_without_touching_sqlite(tmp_path, monkeypatch):
    data_dir = _write_data_dir(tmp_path)
    output = tmp_path / "out" / "index.html"

    def _no_sqlite(*args, **kwargs):
        pytest.fail("report.py must not open any SQLite database")

    monkeypatch.setattr(sqlite3, "connect", _no_sqlite)

    assert report.main(["--data-dir", str(data_dir), "--output", str(output)]) == 0
    written = output.read_text(encoding="utf-8")
    assert written.startswith("<!DOCTYPE html>")
    assert "Laptop Lenovo V15 G4" in written


def _watch_blocks(html: str) -> dict[str, str]:
    blocks = [b.split("</div>")[0] for b in html.split('<div class="watch">')[1:]]
    return {b.split("</h3>")[0]: b for b in blocks}


def test_fanout_watch_lists_products_from_every_retailer(tmp_path):
    # #59 follow-up: an "all" watch fans out across retailers, so its
    # products are matched by query alone, whichever retailer they came from.
    data_dir = _write_data_dir(tmp_path)
    (data_dir / "watchlist.json").write_text(
        json.dumps(
            {
                "watches": [
                    {
                        "id": "babebbc9-2a56-5d0c-b57c-9ca8ad44d7b4",
                        "site": "all",
                        "query": "laptop lenovo v15",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    html = _render(data_dir)

    _assert_well_formed(html)
    [fanout] = [b for h, b in _watch_blocks(html).items() if "laptop lenovo v15" in h]
    assert "3 matching product(s)" in fanout
    for title in (
        "Laptop Lenovo V15 G4",
        "Laptop Lenovo V15 old stock",
        "Laptop Lenovo V15 single",
    ):
        assert title in fanout
    # Only the non-matching ASUS product is left over.
    [others] = [b for h, b in _watch_blocks(html).items() if "Other tracked" in h]
    assert "1 product(s)" in others
    assert "Laptop ASUS Vivobook X" in others
    assert "Lenovo" not in others


def test_single_site_watch_still_matches_only_its_own_retailer(tmp_path):
    html = _render(_write_data_dir(tmp_path))

    blocks = _watch_blocks(html)
    [emag] = [b for h, b in blocks.items() if "laptop lenovo v15" in h]
    assert "2 matching product(s)" in emag
    assert "Laptop Lenovo V15 single" not in emag
    [others] = [b for h, b in blocks.items() if "Other tracked" in h]
    assert "Laptop Lenovo V15 single" in others
