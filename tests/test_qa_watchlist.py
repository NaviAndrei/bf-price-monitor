from datetime import UTC, datetime

import pytest
import qa_watchlist as qa
import scrape


def _watch(**overrides):
    base = {
        "id": "w-1",
        "owner": "NaviAndrei",
        "site": "emag",
        "query": "laptop lenovo v15",
        "enabled": True,
    }
    base.update(overrides)
    return base


def _result(title="Laptop Lenovo V15 great", url="https://www.emag.ro/x/pd/ABC", **kw):
    r = {
        "title": title,
        "price": 100.0,
        "url": url,
        "stock_status": "in_stock",
        "seller": None,
        "is_marketplace": None,
    }
    r.update(kw)
    return r


@pytest.fixture(autouse=True)
def _reset_run_state():
    scrape._run_state.reset("test-run")
    yield
    scrape._run_state.reset("test-run")


@pytest.fixture
def frozen_now():
    return datetime(2026, 11, 22, 10, 0, tzinfo=UTC)  # inside the 7-day BF window


def test_disabled_watches_excluded_from_active_count(monkeypatch):
    watchlist = [_watch(id="a", enabled=True), _watch(id="b", enabled=False)]
    monkeypatch.setitem(scrape.SCRAPERS, "emag", lambda q: [_result()])
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    assert report["active_watch_count"] == 1
    assert report["retailer_check_count"] == 1


def test_single_site_watch_produces_one_retailer_check(monkeypatch):
    watchlist = [_watch(id="a", site="pcgarage")]
    monkeypatch.setitem(scrape.SCRAPERS, "pcgarage", lambda q: [_result()])
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    assert report["retailer_check_count"] == 1
    assert report["records"][0]["retailer_checked"] == "pcgarage"


def test_fanout_expands_only_non_placeholder_retailers(monkeypatch):
    watchlist = [_watch(id="a", site=scrape.FANOUT_SITE)]
    monkeypatch.setitem(scrape.SCRAPERS, "emag", lambda q: [_result()])
    monkeypatch.setitem(scrape.SCRAPERS, "pcgarage", lambda q: [_result()])
    monkeypatch.setitem(scrape.SCRAPERS, "flanco", lambda q: [_result()])
    monkeypatch.setitem(
        scrape.SCRAPERS, "altex", scrape.placeholder_scraper(lambda q: [])
    )
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    checked = {r["retailer_checked"] for r in report["records"]}
    # Placeholder retailers are excluded from fan-out expansion entirely
    # (scrape.py's own _watch_retailers behavior) -- altex never appears.
    assert checked == {"emag", "pcgarage", "flanco"}


def test_matching_result_produces_pass(monkeypatch):
    watchlist = [_watch(id="a")]
    monkeypatch.setitem(
        scrape.SCRAPERS,
        "emag",
        lambda q: [
            _result(title="Laptop Lenovo V15 pro", url="https://www.emag.ro/p/pd/ABC")
        ],
    )
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    assert report["records"][0]["status"] == qa.STATUS_PASS
    assert report["overall_acceptance_status"] in (
        "PASS",
        "BLOCKED",
    )  # timing may block


def test_empty_successful_results_produce_no_results_not_pass(monkeypatch):
    watchlist = [_watch(id="a")]
    monkeypatch.setitem(scrape.SCRAPERS, "emag", lambda q: [])
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    assert report["records"][0]["status"] == qa.STATUS_NO_RESULTS


def test_scraper_exception_reported_and_later_watches_still_audited(monkeypatch):
    def boom(q):
        raise RuntimeError("selector broke")

    watchlist = [_watch(id="a", site="emag"), _watch(id="b", site="pcgarage")]
    monkeypatch.setitem(scrape.SCRAPERS, "emag", boom)
    monkeypatch.setitem(
        scrape.SCRAPERS,
        "pcgarage",
        lambda q: [
            _result(title="Laptop Lenovo V15 great", url="https://www.pcgarage.ro/p/1")
        ],
    )
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    assert len(report["records"]) == 2
    a_rec = next(r for r in report["records"] if r["watch_id"] == "a")
    b_rec = next(r for r in report["records"] if r["watch_id"] == "b")
    assert a_rec["status"] == qa.STATUS_UNVERIFIED
    assert a_rec["failure_category"] == "exception"
    assert b_rec["status"] == qa.STATUS_PASS


def test_challenge_failure_not_classified_as_mismatch(monkeypatch):
    def blocked(q):
        scrape._record_challenge("emag")
        return []

    watchlist = [_watch(id="a")]
    monkeypatch.setitem(scrape.SCRAPERS, "emag", blocked)
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    rec = report["records"][0]
    assert rec["status"] == qa.STATUS_UNVERIFIED
    assert rec["failure_category"] == "bot_challenge"
    assert rec["status"] != qa.STATUS_MISMATCH
    assert rec["status"] != qa.STATUS_NO_RESULTS


def test_matching_uses_established_title_query_contract(monkeypatch):
    watchlist = [_watch(id="a", query="iphone 15")]
    monkeypatch.setitem(
        scrape.SCRAPERS,
        "emag",
        lambda q: [
            _result(title="Xiaomi 15T unrelated", url="https://www.emag.ro/x/pd/A"),
            _result(title="Apple iPhone 15 128GB", url="https://www.emag.ro/y/pd/B"),
        ],
    )
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    rec = report["records"][0]
    assert rec["matching_count"] == 1
    assert rec["sample_titles"] == ["Apple iPhone 15 128GB"]


def test_same_query_different_retailers_not_a_duplicate(monkeypatch):
    watchlist = [
        _watch(id="a", site="pcgarage", query="laptop asus vivobook"),
        _watch(id="b", site="flanco", query="laptop asus vivobook"),
    ]
    monkeypatch.setitem(
        scrape.SCRAPERS,
        "pcgarage",
        lambda q: [
            _result(title="laptop asus vivobook x", url="https://www.pcgarage.ro/p/1")
        ],
    )
    monkeypatch.setitem(
        scrape.SCRAPERS,
        "flanco",
        lambda q: [
            _result(title="laptop asus vivobook x", url="https://www.flanco.ro/p/1")
        ],
    )
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    assert all(r["duplicate_of"] == [] for r in report["records"])


def test_same_retailer_normalized_duplicate_is_reported(monkeypatch):
    watchlist = [
        _watch(id="a", site="emag", query="laptop lenovo v15"),
        _watch(id="b", site="emag", query="  Laptop   LENOVO v15 "),
    ]
    monkeypatch.setitem(
        scrape.SCRAPERS,
        "emag",
        lambda q: [_result(title="laptop lenovo v15 x", url="https://www.emag.ro/p/1")],
    )
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    by_id = {r["watch_id"]: r for r in report["records"]}
    assert by_id["a"]["duplicate_of"] == ["b"]
    assert by_id["b"]["duplicate_of"] == ["a"]


def test_target_price_feasibility_uses_matching_history_and_never_mutates(monkeypatch):
    watch = _watch(id="a", query="laptop lenovo v15", target_price=100.0)
    watchlist = [watch]
    history = {
        "https://www.emag.ro/p/1": {
            "title": "Laptop Lenovo V15 pro",
            "history": [{"date": "2026-09-01", "price": 500.0}],
        }
    }
    monkeypatch.setitem(scrape.SCRAPERS, "emag", lambda q: [_result()])
    report = qa.run_audit(watchlist, history=history, sleep_fn=lambda _: None)
    rec = report["records"][0]
    assert rec["target_price_feasibility"]["status"] == "below_observed_historical_low"
    assert rec["target_price_feasibility"]["observed_low"] == 500.0
    assert watch["target_price"] == 100.0  # never mutated


def test_malformed_result_url_fails_safely(monkeypatch):
    watchlist = [_watch(id="a")]
    monkeypatch.setitem(
        scrape.SCRAPERS,
        "emag",
        lambda q: [_result(title="laptop lenovo v15 x", url="not a url at all")],
    )
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    rec = report["records"][0]
    assert rec["status"] == qa.STATUS_MANUAL_REVIEW
    assert rec["requires_human_review"] is True


def test_report_contains_timestamps_counts_evidence_and_summary(
    monkeypatch, frozen_now
):
    watchlist = [_watch(id="a")]
    monkeypatch.setitem(scrape.SCRAPERS, "emag", lambda q: [_result()])
    report = qa.run_audit(
        watchlist, history={}, sleep_fn=lambda _: None, now=frozen_now
    )
    assert report["generated_at_utc"] == frozen_now.isoformat()
    assert report["schema_version"] == qa.SCHEMA_VERSION
    assert report["active_watch_count"] == 1
    assert report["retailer_check_count"] == 1
    assert "pass_count" in report
    assert "unverified_or_failed_count" in report
    assert isinstance(report["records"], list) and report["records"]


def test_cli_exits_zero_only_when_every_active_check_passes(monkeypatch, frozen_now):
    watchlist = [_watch(id="a")]
    monkeypatch.setitem(scrape.SCRAPERS, "emag", lambda q: [_result()])
    report_pass = qa.run_audit(
        watchlist, history={}, sleep_fn=lambda _: None, now=frozen_now
    )
    assert report_pass["overall_acceptance_status"] == "PASS"

    monkeypatch.setitem(scrape.SCRAPERS, "emag", lambda q: [])
    report_fail = qa.run_audit(
        watchlist, history={}, sleep_fn=lambda _: None, now=frozen_now
    )
    assert report_fail["overall_acceptance_status"] == "BLOCKED"


def test_cli_main_exit_code_matches_acceptance_status(
    monkeypatch, tmp_path, frozen_now
):
    watchlist_file = tmp_path / "watchlist.json"
    watchlist_file.write_text(
        '{"watches": [{"id": "11111111-1111-1111-1111-111111111111", '
        '"site": "emag", "query": "laptop lenovo v15", "enabled": true}]}',
        encoding="utf-8",
    )
    history_file = tmp_path / "price_history.json"
    history_file.write_text("{}", encoding="utf-8")
    json_out = tmp_path / "out" / "report.json"
    md_out = tmp_path / "out" / "report.md"

    monkeypatch.setitem(scrape.SCRAPERS, "emag", lambda q: [_result()])
    original_run_audit = qa.run_audit
    monkeypatch.setattr(
        qa,
        "run_audit",
        lambda *a, **kw: original_run_audit(*a, **{**kw, "now": frozen_now}),
    )

    exit_code = qa.main(
        [
            "--watchlist",
            str(watchlist_file),
            "--history",
            str(history_file),
            "--json-output",
            str(json_out),
            "--markdown-output",
            str(md_out),
            "--no-delay",
        ]
    )
    assert exit_code == 0
    assert json_out.exists()
    assert md_out.exists()


def test_browser_state_closed_in_finally_including_on_failure(monkeypatch, tmp_path):
    watchlist_file = tmp_path / "watchlist.json"
    watchlist_file.write_text(
        '{"watches": [{"id": "11111111-1111-1111-1111-111111111111", '
        '"site": "emag", "query": "laptop lenovo v15", "enabled": true}]}',
        encoding="utf-8",
    )
    history_file = tmp_path / "price_history.json"
    history_file.write_text("{}", encoding="utf-8")

    closed = {"called": False}
    monkeypatch.setattr(
        scrape._browser_state, "close", lambda: closed.__setitem__("called", True)
    )

    def boom(*a, **kw):
        raise RuntimeError("audit blew up")

    monkeypatch.setattr(qa, "run_audit", boom)

    with pytest.raises(RuntimeError):
        qa.main(
            [
                "--watchlist",
                str(watchlist_file),
                "--history",
                str(history_file),
                "--json-output",
                str(tmp_path / "r.json"),
                "--markdown-output",
                str(tmp_path / "r.md"),
                "--no-delay",
            ]
        )
    assert closed["called"] is True


def test_no_test_performs_real_sleep_or_network(monkeypatch):
    # sleep_fn defaults to time.sleep; every test above passes an explicit
    # no-op sleep_fn, and SCRAPERS is always monkeypatched to fakes -- this
    # test locks in that run_audit() itself never calls time.sleep directly
    # when a custom sleep_fn is supplied, and never reaches for the network
    # when SCRAPERS entries are fakes.
    watchlist = [_watch(id="a"), _watch(id="b", site="pcgarage")]
    calls = []
    monkeypatch.setitem(scrape.SCRAPERS, "emag", lambda q: [_result()])
    monkeypatch.setitem(scrape.SCRAPERS, "pcgarage", lambda q: [_result()])
    qa.run_audit(watchlist, history={}, sleep_fn=lambda s: calls.append(s))
    assert len(calls) == 1  # delay only between the two calls, not before the first


def test_bf_timing_window_blocked_outside_seven_days():
    early = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    timing = qa._bf_timing_window(early)
    assert timing["within_seven_day_window"] is False
    assert timing["bf_event_date"] == "2026-11-27"


def test_bf_timing_window_pass_inside_seven_days():
    inside = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    timing = qa._bf_timing_window(inside)
    assert timing["within_seven_day_window"] is True


def test_structural_url_check_rejects_wrong_host():
    result = qa._structural_url_check("https://evil.example.com/p/1", "emag")
    assert result["valid"] is False
    assert result["host_ok"] is False


def test_unknown_site_is_unsupported(monkeypatch):
    watchlist = [_watch(id="a", site="unknownretailer")]
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    rec = report["records"][0]
    assert rec["status"] == qa.STATUS_UNSUPPORTED


def test_no_query_watch_is_manual_review():
    watchlist = [_watch(id="a", query=None)]
    report = qa.run_audit(watchlist, history={}, sleep_fn=lambda _: None)
    rec = report["records"][0]
    assert rec["status"] == qa.STATUS_MANUAL_REVIEW
