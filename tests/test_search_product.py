"""Offline tests for scripts/search_product.py (read-only on-demand search).

Every test replaces the whole scrape.SCRAPERS dict, so no shop is ever
contacted, and nothing here touches data/.
"""

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import scrape
import search_product as sp

NOW = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
QUERY = "lenovo v15"


def row(title, price, url=None, **extra):
    slug = title.lower().replace(" ", "-")
    return {
        "title": title,
        "price": price,
        "url": url or f"https://www.example.ro/{slug}",
        "stock_status": "in_stock",
        "seller": None,
        "is_marketplace": None,
        **extra,
    }


def answered(shop, rows):
    """A fake scraper that got a normal 200 response."""

    def scraper(query):
        scrape._record_fetch_attempt(shop, 200)
        return list(rows)

    return scraper


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    scrape._run_state.reset("test-run")
    monkeypatch.setattr(scrape._browser_state, "close", lambda: None)
    yield
    scrape._run_state.reset("test-run")


@pytest.fixture
def install(monkeypatch):
    def _install(**scrapers):
        monkeypatch.setattr(scrape, "SCRAPERS", dict(scrapers))

    return _install


def run(query=QUERY, **kwargs):
    kwargs.setdefault("delay", False)
    kwargs.setdefault("now_fn", lambda: NOW)
    return sp.run_search(query, **kwargs)


def status_of(report, shop):
    return next(s for s in report["shops"] if s["shop"] == shop)


# -- statuses ---------------------------------------------------------------


def test_status_ok_with_offer_fields(install):
    install(emag=answered("emag", [row("Lenovo V15 G4", 2999.99)]))
    report = run()
    assert status_of(report, "emag")["status"] == "ok"
    offer = report["offers"][0]
    assert offer["shop"] == "emag"
    assert offer["price"] == 2999.99
    assert offer["currency"] == "RON"
    assert offer["stock_status"] == "in_stock"
    assert offer["fetched_at"] == NOW.isoformat()
    assert offer["group_id"] is None


def test_status_no_match_when_page_answered_without_rows(install):
    install(emag=answered("emag", []))
    report = run()
    assert status_of(report, "emag")["status"] == "no_match"


def test_status_blocked_on_challenge_and_not_retried(install):
    calls = []

    def scraper(query):
        calls.append(query)
        scrape._record_fetch_attempt("emag", 200)
        scrape._record_challenge("emag")
        return []

    install(emag=scraper)
    report = run()
    assert status_of(report, "emag")["status"] == "blocked"
    assert len(calls) == 1


@pytest.mark.parametrize("code", [403, 429])
def test_status_blocked_on_rate_limit_codes(install, code):
    def scraper(query):
        scrape._record_fetch_attempt("emag", code)
        return []

    install(emag=scraper)
    report = run()
    assert status_of(report, "emag")["status"] == "blocked"
    assert status_of(report, "emag")["signals"]["rate_limit"] == 1


def test_status_error_when_scraper_raises(install):
    def scraper(query):
        raise RuntimeError("boom")

    install(emag=scraper)
    report = run()
    shop = status_of(report, "emag")
    assert shop["status"] == "error"
    assert "RuntimeError" in shop["detail"]


def test_status_error_when_no_response_was_received(install):
    install(emag=lambda query: [])
    report = run()
    shop = status_of(report, "emag")
    assert shop["status"] == "error"
    assert shop["detail"] == "no response received"


def test_status_error_when_matched_cards_had_no_price(install):
    def scraper(query):
        scrape._record_fetch_attempt("emag", 200)
        scrape._run_state.failure_counts["emag"] = 1
        return []

    install(emag=scraper)
    assert status_of(run(), "emag")["status"] == "error"


def test_status_unsupported_for_unknown_placeholder_and_excluded(install):
    called = []

    def never(query):
        called.append(query)
        return []

    install(
        emag=answered("emag", []),
        altex=scrape.placeholder_scraper(never),
        pcgarage=scrape.placeholder_scraper(never),
    )
    report = run(sites=["evomag", "altex", "nosuchshop", "pcgarage", "emag"])
    by_shop = {s["shop"]: s for s in report["shops"]}
    for name in ("evomag", "altex", "nosuchshop", "pcgarage"):
        assert by_shop[name]["status"] == "unsupported"
    assert by_shop["emag"]["status"] == "no_match"
    assert called == []


def test_excluded_shop_is_never_searched_even_if_registered(install):
    called = []
    install(evomag=lambda q: called.append(q) or [], emag=answered("emag", []))
    report = run()
    assert [s["shop"] for s in report["shops"]] == ["emag"]
    assert called == []


def test_site_names_are_normalised(install):
    install(emag=answered("emag", [row("Lenovo V15", 10.0)]))
    report = run(sites=["www.emag.ro", "EMAG"])
    assert [s["shop"] for s in report["shops"]] == ["emag"]


# -- listing never appeared (zero-result browser page) ----------------------


def test_raised_listing_timeout_without_block_is_no_match(install):
    def scraper(query):
        scrape._record_fetch_attempt("pcgarage", 200)
        raise scrape.ListingReadinessTimeout("pcgarage: .product_box")

    install(pcgarage=scraper)
    shop = status_of(run(), "pcgarage")
    assert shop["status"] == "no_match"
    assert shop["detail"] == "listing never appeared"


def test_zero_result_page_fixture_through_the_real_pcgarage_scraper(
    install, monkeypatch
):
    # Mirrors what fetch_with_browser does on a zero-result page: it swallows
    # ListingReadinessTimeout, leaves only a screenshot in DEBUG_DIR, and
    # returns the page HTML.
    def fake_browser_fetch(url, site):
        scrape._record_fetch_attempt(site, 200)
        scrape.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (scrape.DEBUG_DIR / f"{site}-20261029T100000000.png").write_bytes(b"png")
        return "<html><body><p>Nu am gasit produse</p></body></html>"

    monkeypatch.setattr(scrape, "fetch_with_browser", fake_browser_fetch)
    install(pcgarage=scrape.with_retry(scrape.scrape_pcgarage_listing, "pcgarage"))
    shop = status_of(run(), "pcgarage")
    assert shop["status"] == "no_match"
    assert shop["detail"] == "listing never appeared"


def test_listing_timeout_with_403_is_blocked_not_no_match(install):
    def scraper(query):
        scrape._record_fetch_attempt("pcgarage", 403)
        scrape.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (scrape.DEBUG_DIR / "pcgarage-1.png").write_bytes(b"png")
        return []

    install(pcgarage=scraper)
    assert status_of(run(), "pcgarage")["status"] == "blocked"


# -- request cap, URLs and side effects -------------------------------------


def test_urls_each_scraper_requests_for_a_query_with_space_and_dot(
    install, monkeypatch
):
    seen = []

    def fake_fetch(url, site):
        seen.append((site, url))
        scrape._record_fetch_attempt(site, 200)
        return "<html><body></body></html>"

    monkeypatch.setattr(scrape, "fetch", fake_fetch)
    monkeypatch.setattr(scrape, "fetch_with_browser", fake_fetch)
    install(
        emag=scrape.scrape_emag_listing,
        pcgarage=scrape.scrape_pcgarage_listing,
        flanco=scrape.scrape_flanco_listing,
        itgalaxy=scrape.scrape_itgalaxy_listing,
    )
    run("ssd 1.5 tb")
    assert seen == [
        ("emag", "https://www.emag.ro/search/ssd+1.5+tb"),
        ("pcgarage", "https://www.pcgarage.ro/cauta/?q=ssd+1.5+tb"),
        ("flanco", "https://www.flanco.ro/catalogsearch/result/?q=ssd+1.5+tb"),
        ("itgalaxy", "https://www.itgalaxy.ro/cauta/?search=ssd+1.5+tb"),
    ]


def test_one_request_per_shop_on_a_403_with_the_real_retry_stack(install, monkeypatch):
    hits = []
    sleeps = []

    class Blocked:
        status_code = 403
        text = "<html><body>forbidden</body></html>"

    monkeypatch.setattr(
        scrape.requests, "get", lambda *a, **k: hits.append(a) or Blocked()
    )
    monkeypatch.setattr(scrape.time, "sleep", lambda s: sleeps.append(s))
    install(emag=scrape.with_retry(scrape.scrape_emag_listing))
    report = run()
    assert len(hits) == 1
    assert sleeps == []
    assert status_of(report, "emag")["status"] == "blocked"


def test_overrides_are_active_during_the_call_and_restored_after(install):
    original = {name: getattr(scrape, name) for name in sp.OVERRIDDEN_ATTRIBUTES}
    during = {}

    def scraper(query):
        scrape._record_fetch_attempt("emag", 200)
        during.update(
            {name: getattr(scrape, name) for name in sp.OVERRIDDEN_ATTRIBUTES}
        )
        return []

    install(emag=scraper)
    run()
    assert during["MAX_FETCH_ATTEMPTS"] == 1
    assert during["SCRAPER_RETRY_ATTEMPTS"] == 1
    assert during["EXTRACTION_FAILURES_FILE"] != original["EXTRACTION_FAILURES_FILE"]
    assert during["DEBUG_DIR"] != original["DEBUG_DIR"]
    assert during["title_matches_query"] is not original["title_matches_query"]
    for name, value in original.items():
        assert getattr(scrape, name) == value


def test_strict_mode_keeps_the_original_matcher(install):
    original = scrape.title_matches_query
    seen = []

    def scraper(query):
        scrape._record_fetch_attempt("emag", 200)
        seen.append(scrape.title_matches_query)
        return []

    install(emag=scraper)
    run(strict=True)
    assert seen == [original]


def test_overrides_restored_when_a_scraper_is_interrupted(install):
    original = {name: getattr(scrape, name) for name in sp.OVERRIDDEN_ATTRIBUTES}

    def scraper(query):
        raise KeyboardInterrupt

    install(emag=scraper)
    with pytest.raises(KeyboardInterrupt):
        run()
    for name, value in original.items():
        assert getattr(scrape, name) == value


def test_browser_closed_once_before_the_temp_dir_is_removed(install, monkeypatch):
    events = []

    def spy_close():
        events.append(("close", scrape.EXTRACTION_FAILURES_FILE.parent.exists()))

    monkeypatch.setattr(scrape._browser_state, "close", spy_close)
    install(emag=answered("emag", []))
    run()
    assert events == [("close", True)]


def test_browser_closed_even_when_a_scraper_is_interrupted(install, monkeypatch):
    closed = []
    monkeypatch.setattr(scrape._browser_state, "close", lambda: closed.append(1))

    def scraper(query):
        raise KeyboardInterrupt

    install(emag=scraper)
    with pytest.raises(KeyboardInterrupt):
        run()
    assert closed == [1]


def test_failure_rows_are_counted_then_the_temp_dir_is_removed(install):
    temp_paths = []

    def scraper(query):
        scrape._record_fetch_attempt("emag", 200)
        path = scrape.EXTRACTION_FAILURES_FILE
        temp_paths.append(path)
        path.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
        return []

    install(emag=scraper)
    report = run()
    assert report["diagnostics"]["extraction_failure_rows"] == 2
    assert not temp_paths[0].exists()
    assert not temp_paths[0].parent.exists()


def test_scraper_prints_never_reach_stdout(install, capsys):
    def scraper(query):
        print("[emag] noisy progress line")
        scrape._record_fetch_attempt("emag", 200)
        return []

    install(emag=scraper)
    run()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "noisy progress line" in captured.err


# -- query validation -------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["", " ", "a", "x" * 81, "a/b", "a+b", "o'neil", "a&b", "..", "ssd#1", "a?b"]
)
def test_invalid_queries_are_rejected(bad):
    with pytest.raises(ValueError):
        sp.validate_query(bad)


def test_valid_query_is_collapsed_and_keeps_allowed_characters():
    assert sp.validate_query("  căști,   sony-x  1.5 ") == "căști, sony-x 1.5"


def test_non_ascii_digits_and_superscripts_are_rejected():
    # Regression: str.isalnum() accepted "²" and Arabic-Indic digits, which
    # then went into shop URLs unchanged.
    for bad in ("ssd² 1tb", "ssd ١ tb"):
        with pytest.raises(ValueError):
            sp.validate_query(bad)


# -- loose matching ---------------------------------------------------------

STRICT = scrape.title_matches_query
LOOSE = sp.make_loose_matcher(STRICT)


@pytest.mark.parametrize(
    ("title", "query"),
    [
        ("Căști Sony WH-1000XM5", "casti sony"),
        ("Ștanță metalică", "stanta"),  # comma-below s and t
        ("Şanţ adânc", "sant"),  # cedilla s and t
        ("SSD Kingston 1 TB NVMe", "ssd 1tb"),
        ("SSD Kingston 1TB NVMe", "ssd 1 tb"),
        ("Laptop 15,6 inch", "laptop 15.6"),
        ("Router Wi-Fi 6", "router wifi"),
        ("Router WiFi 6", "router wi-fi"),
        ("iPhone 15, 128 GB", "iphone 15"),
        ("Intel Core i5-13420H", "i5 13420h"),
    ],
)
def test_loose_matcher_accepts_normalised_forms(title, query):
    assert LOOSE(title, query) is True


@pytest.mark.parametrize(
    ("title", "query"),
    [
        ("Căști Sony", "casti sony"),
        ("SSD Kingston 1 TB", "ssd 1tb"),
        ("iPhone 15, 128 GB", "iphone 15"),
    ],
)
def test_strict_matcher_rejects_those_same_forms(title, query):
    # Proves the cases above exercise normalisation, not the strict path.
    assert STRICT(title, query) is False


@pytest.mark.parametrize(
    ("title", "query"),
    [
        ("Lenovo V15 G4", "lenovo v15 g5"),
        ("SSD Kingston 1 TB", "ssd 2tb"),
        ("Router Wi-Fi 6", "router bluetooth"),
        ("Laptop Lenovo", "laptop lenovo v15"),
    ],
)
def test_loose_matcher_never_drops_a_query_word(title, query):
    assert LOOSE(title, query) is False


def test_loose_matcher_falls_back_to_strict_for_a_word_with_no_letters_or_digits():
    assert LOOSE("SSD +", "ssd +") is True  # strict accepts the exact token
    assert LOOSE("SSD plus", "ssd +") is False


@pytest.mark.parametrize(
    ("title", "query"),
    [
        ("LAPTOP Lenovo V15 G4 AMN", "lenovo laptop v15"),
        ("Acer Nitro V15", "nitro v15"),
        ("Xiaomi 15T", "xiaomi 15t"),
        ("Apple iPhone 15 128GB", "iphone 128gb"),
    ],
)
def test_loose_is_a_superset_of_strict(title, query):
    assert STRICT(title, query) is True
    assert LOOSE(title, query) is True


def test_rows_are_tagged_strict_or_normalized_in_loose_mode(install):
    install(
        emag=answered(
            "emag",
            [
                row("Casti Sony WH", 100.0),
                row("Căști Sony WH-1000", 200.0),
            ],
        )
    )
    report = run("casti sony")
    tags = {o["title"]: o["match"] for o in report["offers"]}
    assert tags == {"Casti Sony WH": "strict", "Căști Sony WH-1000": "normalized"}


# -- sorting and cleaning ---------------------------------------------------


def test_offers_sorted_by_price_then_shop_then_title(install):
    install(
        flanco=answered("flanco", [row("B phone", 50.0), row("A phone", 50.0)]),
        emag=answered("emag", [row("C phone", 50.0), row("Z phone", 10.0)]),
    )
    report = run("phone")
    assert [(o["price"], o["shop"], o["title"]) for o in report["offers"]] == [
        (10.0, "emag", "Z phone"),
        (50.0, "emag", "C phone"),
        (50.0, "flanco", "A phone"),
        (50.0, "flanco", "B phone"),
    ]


def test_duplicate_urls_within_a_shop_keep_the_cheapest(install):
    install(
        emag=answered(
            "emag",
            [
                row("Phone", 90.0, "https://www.emag.ro/p/1?utm_source=x"),
                row("Phone", 80.0, "https://emag.ro/p/1/"),
            ],
        )
    )
    report = run("phone")
    assert [o["price"] for o in report["offers"]] == [80.0]
    assert report["offers"][0]["url"] == "https://emag.ro/p/1"


def test_rows_without_price_title_or_url_are_dropped(install):
    install(
        emag=answered(
            "emag",
            [
                {"title": "No price", "price": None, "url": "https://x.ro/1"},
                {"title": "", "price": 1.0, "url": "https://x.ro/2"},
                {"title": "No url", "price": 1.0, "url": ""},
                row("Good phone", 5.0),
            ],
        )
    )
    report = run("phone")
    assert [o["title"] for o in report["offers"]] == ["Good phone"]


# -- grouping ---------------------------------------------------------------

PHONE = "Apple iPhone 15 128GB Negru"
LAPTOP = "Laptop Lenovo IdeaPad E1504FA Ryzen 5 7520U 16GB 512GB SSD"


def test_identical_titles_in_two_shops_form_a_medium_group(install):
    install(
        emag=answered("emag", [row(PHONE, 3999.0)]),
        flanco=answered("flanco", [row(PHONE, 3899.0)]),
    )
    report = run("iphone 15")
    assert [o["group_id"] for o in report["offers"]] == ["g1", "g1"]
    assert report["groups"] == [
        {
            "group_id": "g1",
            "confidence": "medium",
            "shops": ["emag", "flanco"],
            "score": 0.8,
            "reasons": ["TITLE_MATCH", "SUGGESTED_EQUIVALENCE"],
        }
    ]


def test_a_laptop_with_a_readable_cpu_reaches_high_confidence(install):
    install(
        emag=answered("emag", [row(LAPTOP, 2500.0)]),
        itgalaxy=answered("itgalaxy", [row(LAPTOP, 2450.0)]),
    )
    report = run("lenovo ideapad")
    assert {o["group_confidence"] for o in report["offers"]} == {"high"}


def test_same_shop_offers_are_never_grouped(install):
    install(
        emag=answered(
            "emag",
            [
                row(PHONE, 3999.0, "https://x.ro/1"),
                row(PHONE, 4100.0, "https://x.ro/2"),
            ],
        )
    )
    report = run("iphone 15")
    assert report["groups"] == []
    assert {o["group_id"] for o in report["offers"]} == {None}


def test_different_products_are_not_grouped(install):
    install(
        emag=answered("emag", [row("Apple iPhone 15 128GB Negru", 3999.0)]),
        flanco=answered("flanco", [row("Samsung Galaxy S24 256GB Gri", 3500.0)]),
    )
    assert run("phone")["groups"] == []


def test_capacity_difference_vetoes_a_pair_the_scorer_would_accept():
    a = LAPTOP
    b = LAPTOP.replace("512GB", "256GB")
    assert sp.match_titles(a, b).accepted is True
    assert sp._link_score(a, b) is None


def test_refurbished_marker_on_one_side_vetoes_a_pair():
    a = LAPTOP
    b = LAPTOP + " Resigilat"
    assert sp.match_titles(a, b).accepted is True
    assert sp._link_score(a, b) is None


def test_group_ids_follow_the_cheapest_member(install):
    cheap = "Apple iPhone 14 128GB Negru"
    install(
        emag=answered("emag", [row(PHONE, 3999.0), row(cheap, 2000.0)]),
        flanco=answered("flanco", [row(PHONE, 3899.0), row(cheap, 1900.0)]),
    )
    report = run("iphone")
    by_title = {o["title"]: o["group_id"] for o in report["offers"]}
    assert by_title[cheap] == "g1"
    assert by_title[PHONE] == "g2"


# -- cache and pacing -------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def counting(shop, rows=None, *, code=200, challenge=False):
    calls = []

    def scraper(query):
        calls.append(query)
        scrape._record_fetch_attempt(shop, code)
        if challenge:
            scrape._record_challenge(shop)
        return list(rows or [])

    scraper.calls = calls
    return scraper


def test_no_cache_dir_means_nothing_is_written(install, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    install(emag=answered("emag", [row("Lenovo V15", 1.0)]))
    run()
    assert list(tmp_path.iterdir()) == []


def test_ok_result_is_reused_until_the_ttl_expires(install, tmp_path):
    clock = Clock()
    scraper = counting("emag", [row("Lenovo V15", 1.0)])
    install(emag=scraper)
    cache = tmp_path / "cache"
    first = run(cache_dir=cache, cache_ttl=900, now_fn=clock)
    clock.advance(899)
    second = run(cache_dir=cache, cache_ttl=900, now_fn=clock)
    assert len(scraper.calls) == 1
    assert status_of(first, "emag")["from_cache"] is False
    assert status_of(second, "emag")["from_cache"] is True
    clock.advance(1)
    run(cache_dir=cache, cache_ttl=900, now_fn=clock)
    assert len(scraper.calls) == 2


def test_blocked_result_follows_the_monitors_cooldown_constant(
    install, tmp_path, monkeypatch
):
    monkeypatch.setattr(scrape, "RATE_LIMIT_COOLDOWN_MINUTES", 10)
    clock = Clock()
    scraper = counting("emag", code=429)
    install(emag=scraper)
    cache = tmp_path / "cache"
    run(cache_dir=cache, now_fn=clock)
    clock.advance(599)
    run(cache_dir=cache, now_fn=clock)
    assert len(scraper.calls) == 1
    clock.advance(1)
    run(cache_dir=cache, now_fn=clock)
    assert len(scraper.calls) == 2


def test_blocked_default_window_is_thirty_minutes(install, tmp_path):
    assert scrape.RATE_LIMIT_COOLDOWN_MINUTES * 60 == 1800
    clock = Clock()
    scraper = counting("emag", challenge=True)
    install(emag=scraper)
    cache = tmp_path / "cache"
    run(cache_dir=cache, now_fn=clock)
    clock.advance(1799)
    run(cache_dir=cache, now_fn=clock)
    assert len(scraper.calls) == 1


def test_error_result_is_cached_for_ninety_seconds_only(install, tmp_path):
    clock = Clock()
    calls = []

    def scraper(query):
        calls.append(query)
        raise RuntimeError("transient")

    install(emag=scraper)
    cache = tmp_path / "cache"
    run(cache_dir=cache, now_fn=clock)
    clock.advance(89)
    run(cache_dir=cache, now_fn=clock)
    assert len(calls) == 1
    clock.advance(1)
    run(cache_dir=cache, now_fn=clock)
    assert len(calls) == 2


def test_strict_and_loose_do_not_share_cache_entries(install, tmp_path):
    scraper = counting("emag", [row("Lenovo V15", 1.0)])
    install(emag=scraper)
    cache = tmp_path / "cache"
    run(cache_dir=cache)
    run(cache_dir=cache, strict=True)
    assert len(scraper.calls) == 2
    assert sp.cache_key("emag", "x", "strict") != sp.cache_key("emag", "x", "loose")


def test_cache_key_ignores_case_diacritics_and_spacing():
    assert sp.cache_key("emag", "Căști  SONY", "loose") == sp.cache_key(
        "emag", "casti sony", "loose"
    )


def test_ttl_zero_ignores_ok_entries_but_still_honours_blocked(install, tmp_path):
    clock = Clock()
    ok = counting("emag", [row("Lenovo V15", 1.0)])
    blocked = counting("flanco", code=403)
    install(emag=ok, flanco=blocked)
    cache = tmp_path / "cache"
    run(cache_dir=cache, now_fn=clock)
    run(cache_dir=cache, cache_ttl=0, now_fn=clock)
    assert len(ok.calls) == 2
    assert len(blocked.calls) == 1


def test_cache_files_are_written_atomically_and_corrupt_files_are_ignored(
    install, tmp_path
):
    scraper = counting("emag", [row("Lenovo V15", 1.0)])
    install(emag=scraper)
    cache = tmp_path / "cache"
    run(cache_dir=cache)
    files = list(cache.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".json"
    files[0].write_text("not json", encoding="utf-8")
    run(cache_dir=cache)
    assert len(scraper.calls) == 2


def test_pause_between_live_shops_only(install, tmp_path):
    sleeps = []
    install(
        emag=counting("emag", [row("Lenovo V15", 1.0)]),
        flanco=counting("flanco", [row("Lenovo V15", 2.0)]),
        itgalaxy=counting("itgalaxy", [row("Lenovo V15", 3.0)]),
    )
    cache = tmp_path / "cache"
    run(delay=True, sleep_fn=sleeps.append, cache_dir=cache)
    assert sleeps == [5.0, 5.0]
    sleeps.clear()
    run(delay=True, sleep_fn=sleeps.append, cache_dir=cache)
    assert sleeps == []  # everything came from the cache


def test_no_delay_skips_the_pause(install):
    sleeps = []
    install(
        emag=counting("emag"),
        flanco=counting("flanco"),
    )
    run(delay=False, sleep_fn=sleeps.append)
    assert sleeps == []


# -- command line -----------------------------------------------------------


def test_exit_code_0_when_a_shop_answers_even_if_another_is_blocked(install):
    install(
        emag=counting("emag", [row("Lenovo V15", 1.0)]),
        flanco=counting("flanco", code=429),
    )
    assert sp.main(["lenovo", "v15", "--no-delay"]) == 0


def test_exit_code_1_when_no_shop_answered(install):
    install(emag=counting("emag", code=429), flanco=counting("flanco", challenge=True))
    assert sp.main(["lenovo v15", "--no-delay"]) == 1


def test_exit_code_2_for_an_invalid_query(install, capsys):
    install(emag=counting("emag"))
    assert sp.main(["a&b"]) == 2
    assert "error:" in capsys.readouterr().err


def test_json_output_is_clean_and_matches_the_schema(install, capsys):
    def noisy(query):
        print("progress noise")
        scrape._record_fetch_attempt("emag", 200)
        return [row("Lenovo V15", 12.5)]

    install(emag=noisy)
    assert sp.main(["lenovo v15", "--json", "--no-delay"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "query",
        "mode",
        "generated_at",
        "shops",
        "offers",
        "groups",
        "diagnostics",
    }
    assert payload["mode"] == "loose"
    assert set(payload["offers"][0]) == {
        "shop",
        "title",
        "price",
        "currency",
        "stock_status",
        "seller",
        "is_marketplace",
        "url",
        "fetched_at",
        "match",
        "group_id",
        "group_confidence",
    }
    assert set(payload["shops"][0]) == {
        "shop",
        "status",
        "detail",
        "offer_count",
        "from_cache",
        "fetched_at",
        "signals",
    }


def test_table_output_lists_shops_and_offers(install, capsys):
    install(emag=counting("emag", [row("Lenovo V15", 12.5)]))
    assert sp.main(["lenovo v15", "--no-delay"]) == 0
    out = capsys.readouterr().out
    assert "emag" in out
    assert "12.50 RON" in out
    assert "loose matching" in out


def test_table_output_flags_normalised_rows_and_dropped_cards(install, capsys):
    def scraper(query):
        scrape._record_fetch_attempt("emag", 200)
        scrape.EXTRACTION_FAILURES_FILE.write_text("{}\n", encoding="utf-8")
        return [row("Căști Sony WH", 99.0)]

    install(emag=scraper)
    assert sp.main(["casti sony", "--no-delay"]) == 0
    out = capsys.readouterr().out
    assert "(~ = matched only after normalisation)" in out
    assert "1 matched card(s) had no extractable price" in out


def test_table_output_without_offers(install, capsys):
    install(emag=counting("emag"))
    assert sp.main(["lenovo v15", "--no-delay"]) == 0
    assert "No offers found." in capsys.readouterr().out


def test_cli_passes_sites_and_cache_dir(install, tmp_path):
    install(
        emag=counting("emag", [row("Lenovo V15", 1.0)]),
        flanco=counting("flanco", [row("Lenovo V15", 2.0)]),
    )
    cache = tmp_path / "c"
    assert sp.main(["lenovo v15", "--sites", "flanco", "--cache-dir", str(cache)]) == 0
    assert len(list(Path(cache).iterdir())) == 1


def test_empty_sites_option_searches_nothing_instead_of_every_shop(install, capsys):
    # Regression: "--sites ''" fell back to all live shops because an empty
    # string is falsy. An empty name is now an unknown shop: no request made.
    scraper = counting("emag", [row("Lenovo V15", 1.0)])
    install(emag=scraper)
    assert sp.main(["lenovo v15", "--sites", "", "--no-delay", "--json"]) == 2
    assert scraper.calls == []
    assert "--sites needs at least one shop" in capsys.readouterr().err


def test_naive_cache_timestamp_is_ignored_instead_of_crashing(tmp_path):
    # Regression: subtracting a naive timestamp from an aware one raised
    # TypeError, outside the try block that guards the cache read.
    key = sp.cache_key("emag", "lenovo v15", "strict")
    (tmp_path / f"{key}.json").write_text(
        json.dumps(
            {
                "stored_at": "2026-10-29T09:00:00",
                "result": {"status": "ok", "offers": []},
            }
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 10, 29, 9, 1, tzinfo=UTC)
    assert sp.load_cache(tmp_path, key, now, 900) is None


def test_zero_result_page_without_screenshot_has_a_non_empty_detail(install):
    # Without a debug screenshot the scraper's silent timeout cannot be told
    # apart from "no matches", so the detail must still explain the status.
    install(emag=counting("emag"))
    report = run()
    shop = report["shops"][0]
    assert shop["status"] == "no_match"
    assert shop["detail"] == "no matching offers"


def test_no_delay_without_cache_dir_warns_on_stderr(install, capsys):
    install(emag=counting("emag", [row("Lenovo V15", 1.0)]))
    assert sp.main(["lenovo v15", "--no-delay"]) == 0
    assert (
        "no pause and no cache: repeated runs will hit the shops again"
        in capsys.readouterr().err
    )


def test_no_delay_with_cache_dir_does_not_warn(install, tmp_path, capsys):
    install(emag=counting("emag", [row("Lenovo V15", 1.0)]))
    cache = tmp_path / "cache"
    assert sp.main(["lenovo v15", "--no-delay", "--cache-dir", str(cache)]) == 0
    assert "no pause and no cache" not in capsys.readouterr().err


def test_table_output_survives_a_non_encodable_title(install, monkeypatch):
    # A Windows console on cp1252 cannot encode U+FE0F, which an emag title
    # carried in the live smoke run. The table must still print and exit 0.
    install(emag=counting("emag", [row("Casti ️Bluetooth", 1.0)]))
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    assert sp.main(["casti bluetooth", "--no-delay", "--sites", "emag"]) == 0
    stream.flush()
    assert b"Casti" in raw.getvalue()


def test_loose_matching_is_the_default_and_strict_opts_out(install, capsys):
    # Real scrapers drop non-matching titles themselves, by calling the
    # module-level matcher, so the fake must do the same to show the flag.
    rows = [row("Căști Sony WH", 1.0)]

    def filtering(query):
        scrape._record_fetch_attempt("emag", 200)
        return [r for r in rows if scrape.title_matches_query(r["title"], query)]

    install(emag=filtering)
    loose = run("casti sony")
    assert loose["mode"] == "loose"
    assert [o["title"] for o in loose["offers"]] == ["Căști Sony WH"]
    strict = run("casti sony", strict=True)
    assert strict["mode"] == "strict"
    assert strict["offers"] == []
    assert sp.main(["casti sony", "--strict", "--no-delay", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "strict"


def test_comma_attached_query_word_is_recovered_only_by_loose_matching():
    # Live example reported 2026-10-29. Strict splits on whitespace only, so
    # "Bluetooth," never equals "bluetooth"; loose matching recovers it.
    title = "Casti Anker Soundcore, Wireless, Bluetooth, Dark Grey"
    assert STRICT(title, "casti bluetooth") is False
    assert LOOSE(title, "casti bluetooth") is True


@pytest.mark.parametrize("extra", [[], ["--json"]], ids=["table", "json"])
def test_stdout_survives_a_cp1252_pipe_and_keeps_romanian_letters(
    install, monkeypatch, extra
):
    # Regression: a cp1252 pipe cannot encode U+FE0F (an emag title in the live
    # smoke run), so --json raised UnicodeEncodeError, and the table replaced
    # "ă" and "ș" with "?". Both must now print the title as UTF-8, unchanged.
    title = "Căști Sony WH-1000XM5 ️Bluetooth"
    install(emag=counting("emag", [row(title, 1.0)]))
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    assert sp.main(["casti sony", "--no-delay", "--sites", "emag", *extra]) == 0
    stream.flush()
    assert "Căști Sony WH-1000XM5" in raw.getvalue().decode("utf-8")


def test_table_keeps_a_space_between_a_long_status_and_the_title(install, capsys):
    # Regression: stock_status was padded to 14 characters with no separator, so
    # "supplier_stock" (14 characters) ran straight into the title.
    install(
        emag=counting(
            "emag",
            [row("Lenovo V15 supplier", 12.5, stock_status="supplier_stock")],
        )
    )
    assert sp.main(["lenovo v15", "--no-delay", "--sites", "emag"]) == 0
    assert "supplier_stock Lenovo V15 supplier" in capsys.readouterr().out


def test_real_fetch_with_browser_403_is_one_attempt_and_one_rate_limit_hit(
    monkeypatch, tmp_path
):
    # Runs the real fetch_with_browser under the retry override. Only the
    # browser page is faked; a 403 must be one attempt and one 403/429 hit.
    class FakeResponse:
        status = 403

    class FakePage:
        def set_default_navigation_timeout(self, timeout_ms):
            pass

        def route(self, pattern, handler):
            pass

        def goto(self, url, **kwargs):
            return FakeResponse()

        def content(self):
            return "<html><body>Forbidden</body></html>"

        def close(self):
            pass

    class FakeContext:
        def new_page(self):
            return FakePage()

    monkeypatch.setattr(
        scrape._browser_state, "get_context", lambda site: FakeContext()
    )
    scrape._run_state.reset("test-browser-403")
    saved = sp.apply_overrides(tmp_path, loose_matcher=None)
    try:
        html = scrape.fetch_with_browser(
            "https://www.pcgarage.ro/cauta/?q=x", "pcgarage"
        )
    finally:
        sp.restore_overrides(saved)
    assert html is not None
    assert scrape._run_state.fetch_attempt_counts.get("pcgarage") == 1
    assert scrape._run_state.rate_limit_hit_counts.get("pcgarage") == 1


def test_offers_carry_seller_and_marketplace_sort_out_of_stock_last_and_get_marks(
    install, capsys
):
    install(
        emag=answered(
            "emag",
            [
                row("Lenovo V15 cheap", 1000.0, stock_status="out_of_stock"),
                row(
                    "Lenovo V15 market",
                    1500.0,
                    seller="Some Seller",
                    is_marketplace=True,
                ),
                row("Lenovo V15 stock", 2000.0),
            ],
        )
    )
    report = run()
    offers = report["offers"]
    assert [o["title"] for o in offers] == [
        "Lenovo V15 market",
        "Lenovo V15 stock",
        "Lenovo V15 cheap",
    ]
    assert offers[0]["seller"] == "Some Seller"
    assert offers[0]["is_marketplace"] is True
    assert offers[1]["seller"] == "" and offers[1]["is_marketplace"] is False
    assert sp.main(["lenovo v15", "--no-delay"]) == 0
    table = capsys.readouterr().out
    assert "Lenovo V15 cheap [out]" in table
    assert "Lenovo V15 market [mkt]" in table


def test_exit_1_when_the_only_answer_is_listing_never_appeared(install):
    def scraper(query):
        scrape._record_fetch_attempt("pcgarage", 200)
        raise scrape.ListingReadinessTimeout("pcgarage: .product_box")

    install(pcgarage=scraper)
    assert sp.exit_code(run()) == 1


def test_failed_cache_write_warns_and_keeps_the_result(
    install, tmp_path, monkeypatch, capsys
):
    install(emag=answered("emag", [row("Lenovo V15", 1.0)]))

    def broken_store(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(sp, "store_cache", broken_store)
    report = run(cache_dir=tmp_path / "cache")
    assert [o["title"] for o in report["offers"]] == ["Lenovo V15"]
    assert "warning: could not write cache: disk full" in capsys.readouterr().err


def test_module_does_not_import_the_monitor_write_paths():
    source = Path(sp.__file__).read_text(encoding="utf-8")
    for forbidden in ("save_history", "init_db", "sqlite", "alerts.json", "notify"):
        assert forbidden not in source
