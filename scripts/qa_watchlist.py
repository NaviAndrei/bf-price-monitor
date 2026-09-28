# scripts/qa_watchlist.py
"""Read-only watchlist QA auditor (T-34, #45).

Verifies every enabled entry in data/watchlist.json against a live retailer
search, reusing scrape.py's own scraper functions, matching contract
(title_matches_query) and URL canonicalization (canonicalize_url) rather
than re-implementing any retailer-specific parsing. Never runs analyze.py
or notify.py, and never writes price_history.json, price_history.db,
alerts, or health logs -- scrape.py's own SCRAPERS calls are its only
production side effect (each retailer's existing per-card extraction
failure log, data/extraction_failures.jsonl, may still be appended to by
that reused code on a real parse failure, same as a normal scrape run).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape  # noqa: E402  (path insert above must run first)

SCHEMA_VERSION = "1.0"

# Day after the fourth Thursday of November (fixed US-Thanksgiving-anchored
# definition used consistently by every retailer calendar this project
# tracks) -- computed, not guessed; see docs/watchlist-qa.md for the
# calendar arithmetic. No project decision recorded a different date.
BF_DATE_2026 = date(2026, 11, 27)
BF_WINDOW_DAYS = 7

DEFAULT_DELAY_RANGE = (3.0, 7.0)  # matches scrape.py's own polite delay

# Base host for each retailer's live listing page, used only for structural
# URL verification (scheme/host/path) -- not a second copy of any selector
# or request logic, which stays exclusively in scrape.py.
RETAILER_HOSTS = {
    "emag": "emag.ro",
    "pcgarage": "pcgarage.ro",
    "flanco": "flanco.ro",
    "altex": "altex.ro",
}

SAMPLE_TITLE_LIMIT = 5
SAMPLE_URL_LIMIT = 5

STATUS_PASS = "PASS"
STATUS_MISMATCH = "MISMATCH"
STATUS_NO_RESULTS = "NO_RESULTS"
STATUS_UNVERIFIED = "UNVERIFIED"
STATUS_UNSUPPORTED = "UNSUPPORTED"
STATUS_MANUAL_REVIEW = "MANUAL_REVIEW"

STATUS_MEANINGS = {
    STATUS_PASS: "Live listing succeeded and at least one result matches intent.",
    STATUS_MISMATCH: "Live results were obtained but none matched intended product.",
    STATUS_NO_RESULTS: "Listing loaded successfully but returned no suitable item.",
    STATUS_UNVERIFIED: (
        "Timeout, network failure, bot-challenge, or another operational "
        "condition prevented a trustworthy decision -- not evidence of staleness."
    ),
    STATUS_UNSUPPORTED: "Placeholder or unavailable retailer scraper.",
    STATUS_MANUAL_REVIEW: "Product intent cannot be inferred safely from the watch alone.",
}


def _normalized_query_key(retailer: str, query: str) -> str:
    return f"{retailer}:{' '.join(query.lower().split())}"


def _last_sunday(year: int, month: int) -> date:
    first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = first_of_next - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() - 6) % 7)


def _eu_dst_active(dt_utc: datetime) -> bool:
    # Windows ships no IANA tz database (zoneinfo needs the optional
    # `tzdata` package there), so Europe/Bucharest's UTC+2/+3 offset is
    # computed directly from the fixed EU DST rule (clocks change at
    # 01:00 UTC on the last Sunday of March/October) rather than adding a
    # new dependency for one date-only conversion.
    year = dt_utc.year
    dst_start = datetime.combine(
        _last_sunday(year, 3), datetime.min.time(), tzinfo=UTC
    ) + timedelta(hours=1)
    dst_end = datetime.combine(
        _last_sunday(year, 10), datetime.min.time(), tzinfo=UTC
    ) + timedelta(hours=1)
    return dst_start <= dt_utc < dst_end


def _bucharest_date(dt_utc: datetime) -> date:
    offset_hours = 3 if _eu_dst_active(dt_utc) else 2
    return (dt_utc + timedelta(hours=offset_hours)).date()


def _bf_timing_window(now_utc: datetime) -> dict[str, Any]:
    window_start = BF_DATE_2026 - timedelta(days=BF_WINDOW_DAYS)
    audit_date_bucharest = _bucharest_date(now_utc)
    within_window = window_start <= audit_date_bucharest <= BF_DATE_2026
    return {
        "bf_event_date": BF_DATE_2026.isoformat(),
        "bf_date_source": (
            "Computed: day after the fourth Thursday of November 2026 "
            "(fixed US-Thanksgiving-anchored Black Friday definition); "
            "no project-specific override found in docs/DECISIONS.md."
        ),
        "audit_timestamp_utc": now_utc.isoformat(),
        "audit_date_europe_bucharest": audit_date_bucharest.isoformat(),
        "window_start_date": window_start.isoformat(),
        "within_seven_day_window": within_window,
    }


def _structural_url_check(url: str, retailer: str) -> dict[str, Any]:
    canonical = scrape.canonicalize_url(url)
    parsed = urlsplit(canonical)
    expected_host = RETAILER_HOSTS.get(retailer)
    host_ok = bool(expected_host) and parsed.netloc.lower().endswith(expected_host)
    scheme_ok = parsed.scheme in ("http", "https")
    path_ok = bool(parsed.path) and parsed.path != "/"
    return {
        "canonical_url": canonical,
        "scheme_ok": scheme_ok,
        "host_ok": host_ok,
        "path_ok": path_ok,
        "valid": scheme_ok and host_ok and path_ok,
    }


def _target_price_feasibility(
    watch: dict[str, Any], history: dict[str, Any]
) -> dict[str, Any]:
    target_price = watch.get("target_price")
    if target_price is None:
        return {"status": "not_set", "target_price": None, "observed_low": None}

    query = watch.get("query")
    observed_prices: list[float] = []
    if query:
        for entry in history.values():
            if not scrape.title_matches_query(entry.get("title", ""), query):
                continue
            observed_prices.extend(h["price"] for h in entry.get("history", []))

    if not observed_prices:
        return {
            "status": "no_matching_history",
            "target_price": target_price,
            "observed_low": None,
        }

    observed_low = min(observed_prices)
    if target_price < observed_low:
        return {
            "status": "below_observed_historical_low",
            "target_price": target_price,
            "observed_low": observed_low,
        }
    return {
        "status": "within_observed_range",
        "target_price": target_price,
        "observed_low": observed_low,
    }


@dataclass
class AuditRecord:
    watch_id: str | None
    configured_site: str
    retailer: str
    query: str | None
    enabled: bool
    audit_timestamp_utc: str
    status: str
    matching_count: int = 0
    sample_titles: list[str] = field(default_factory=list)
    result_urls: list[dict[str, Any]] = field(default_factory=list)
    stock_statuses: list[str] = field(default_factory=list)
    failure_category: str | None = None
    reason: str | None = None
    recommended_action: str = "none"
    requires_human_review: bool = False
    duplicate_of: list[str] = field(default_factory=list)
    target_price_feasibility: dict[str, Any] = field(default_factory=dict)
    url_verification: str = "structural_only"

    def to_dict(self) -> dict[str, Any]:
        return {
            "watch_id": self.watch_id,
            "configured_site": self.configured_site,
            "retailer_checked": self.retailer,
            "query": self.query,
            "enabled": self.enabled,
            "audit_timestamp_utc": self.audit_timestamp_utc,
            "status": self.status,
            "status_meaning": STATUS_MEANINGS[self.status],
            "matching_count": self.matching_count,
            "sample_titles": self.sample_titles,
            "result_urls": self.result_urls,
            "stock_statuses": self.stock_statuses,
            "failure_category": self.failure_category,
            "reason": self.reason,
            "recommended_action": self.recommended_action,
            "requires_human_review": self.requires_human_review,
            "duplicate_of": self.duplicate_of,
            "target_price_feasibility": self.target_price_feasibility,
            "url_verification": self.url_verification,
        }


def _audit_one(
    watch: dict[str, Any],
    retailer: str,
    *,
    history: dict[str, Any],
    now_utc: datetime,
) -> AuditRecord:
    watch_id = watch.get("id")
    configured_site = watch["site"]
    query = watch.get("query")
    record = AuditRecord(
        watch_id=watch_id,
        configured_site=configured_site,
        retailer=retailer,
        query=query,
        enabled=bool(watch.get("enabled", True)),
        audit_timestamp_utc=now_utc.isoformat(),
        status=STATUS_UNVERIFIED,
        target_price_feasibility=_target_price_feasibility(watch, history),
    )

    scraper = scrape.SCRAPERS.get(retailer)
    if scraper is None:
        record.status = STATUS_UNSUPPORTED
        record.reason = f"No scraper registered for site {retailer!r}"
        record.recommended_action = "Fix or remove the watch's site value"
        record.requires_human_review = True
        return record
    if scrape.is_placeholder_scraper(scraper):
        record.status = STATUS_UNSUPPORTED
        record.reason = f"{retailer} is a placeholder scraper (no live data available)"
        record.recommended_action = "none (known accepted gap)"
        record.url_verification = "not_applicable"
        return record

    if not query:
        record.status = STATUS_MANUAL_REVIEW
        record.reason = (
            "Watch has no query (direct_url-only watches are not auditable here)"
        )
        record.recommended_action = "Review manually"
        record.requires_human_review = True
        record.url_verification = "not_applicable"
        return record

    pre_challenge = scrape._run_state.challenge_counts.get(retailer, 0)
    pre_failure = scrape._run_state.failure_counts.get(retailer, 0)
    try:
        results = scraper(query)
    except Exception as exc:  # noqa: BLE001 -- must keep auditing remaining watches
        record.status = STATUS_UNVERIFIED
        record.failure_category = "exception"
        record.reason = f"{exc.__class__.__name__}: {exc}"
        record.recommended_action = "Retry the audit; do not treat as stale"
        return record

    post_challenge = scrape._run_state.challenge_counts.get(retailer, 0)
    post_failure = scrape._run_state.failure_counts.get(retailer, 0)
    challenge_hit = post_challenge > pre_challenge
    extraction_failed = post_failure > pre_failure

    matches = [
        r for r in results if scrape.title_matches_query(r.get("title", ""), query)
    ]
    record.matching_count = len(matches)
    record.sample_titles = [m["title"] for m in matches[:SAMPLE_TITLE_LIMIT]]
    record.stock_statuses = [
        m.get("stock_status", "unknown") for m in matches[:SAMPLE_TITLE_LIMIT]
    ]
    record.result_urls = [
        _structural_url_check(m["url"], retailer) for m in matches[:SAMPLE_URL_LIMIT]
    ]

    if challenge_hit:
        record.status = STATUS_UNVERIFIED
        record.failure_category = "bot_challenge"
        record.reason = "Bot-challenge/blocked page detected during live fetch"
        record.recommended_action = "Retry the audit; do not treat as stale"
    elif results and not matches:
        record.status = STATUS_MISMATCH
        record.reason = "Live results were returned but none matched all query tokens"
        record.recommended_action = "Correct or narrow the query"
        record.requires_human_review = True
    elif matches:
        all_urls_valid = all(u["valid"] for u in record.result_urls)
        if not all_urls_valid:
            record.status = STATUS_MANUAL_REVIEW
            record.reason = "Matching result(s) had a structurally invalid URL"
            record.recommended_action = "Review manually"
            record.requires_human_review = True
        else:
            record.status = STATUS_PASS
            record.recommended_action = "none"
    elif extraction_failed:
        record.status = STATUS_UNVERIFIED
        record.failure_category = "extraction_failure"
        record.reason = "Listing loaded but price extraction failed for every candidate"
        record.recommended_action = "Retry the audit; do not treat as stale"
    else:
        record.status = STATUS_NO_RESULTS
        record.reason = "Listing loaded successfully; no candidate matched the query"
        record.recommended_action = "Review query specificity or product availability"
        record.requires_human_review = True

    return record


def run_audit(
    watchlist: list[dict[str, Any]],
    history: dict[str, Any],
    *,
    sleep_fn=time.sleep,
    delay_range: tuple[float, float] = DEFAULT_DELAY_RANGE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure audit core: no file I/O. Callers own reading/writing paths."""
    now_utc = now or datetime.now(UTC)
    active = [w for w in watchlist if w.get("enabled", True)]
    pairs = list(scrape._watch_retailers(active))

    records: list[AuditRecord] = []
    for index, (watch, retailer) in enumerate(pairs):
        if index > 0 and delay_range != (0, 0):
            sleep_fn(random.uniform(*delay_range))
        records.append(_audit_one(watch, retailer, history=history, now_utc=now_utc))

    # Duplicate detection: same normalized query on the same retailer,
    # coming from two different watch ids. A shared query across different
    # retailers is expected (e.g. the same laptop tracked on pcgarage and
    # flanco) and must not be flagged.
    groups: dict[str, list[AuditRecord]] = defaultdict(list)
    for rec in records:
        if rec.query:
            groups[_normalized_query_key(rec.retailer, rec.query)].append(rec)
    for group in groups.values():
        ids = {r.watch_id for r in group}
        if len(ids) > 1:
            for rec in group:
                rec.duplicate_of = sorted(i for i in ids if i != rec.watch_id)

    pass_count = sum(1 for r in records if r.status == STATUS_PASS)
    fail_statuses = {
        STATUS_MISMATCH,
        STATUS_NO_RESULTS,
        STATUS_UNVERIFIED,
        STATUS_UNSUPPORTED,
        STATUS_MANUAL_REVIEW,
    }
    unverified_count = sum(1 for r in records if r.status in fail_statuses)

    watch_ids_with_failure = {r.watch_id for r in records if r.status != STATUS_PASS}
    all_active_pass = len(active) > 0 and not watch_ids_with_failure

    timing = _bf_timing_window(now_utc)
    overall_acceptance = (
        "PASS" if (all_active_pass and timing["within_seven_day_window"]) else "BLOCKED"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": now_utc.isoformat(),
        "audit_mode": "live",
        **timing,
        "active_watch_count": len(active),
        "retailer_check_count": len(records),
        "pass_count": pass_count,
        "unverified_or_failed_count": unverified_count,
        "corrections": [],
        "removals": [],
        "overall_acceptance_status": overall_acceptance,
        "records": [r.to_dict() for r in records],
    }


def _print_console_table(report: dict[str, Any]) -> None:
    header = (
        f"{'watch_id':<10} {'retailer':<10} {'query':<28} {'status':<14} {'matches':>7}"
    )
    print(header)
    print("-" * len(header))
    for rec in report["records"]:
        wid = (rec["watch_id"] or "")[:8]
        query = (rec["query"] or "")[:28]
        print(
            f"{wid:<10} {rec['retailer_checked']:<10} {query:<28} "
            f"{rec['status']:<14} {rec['matching_count']:>7}"
        )
    print()
    print(
        f"Active watches: {report['active_watch_count']}  "
        f"Retailer checks: {report['retailer_check_count']}  "
        f"PASS: {report['pass_count']}  "
        f"Not verified: {report['unverified_or_failed_count']}"
    )
    print(
        f"BF event date: {report['bf_event_date']} "
        f"(within 7-day window: {report['within_seven_day_window']})"
    )
    print(f"Overall acceptance status: {report['overall_acceptance_status']}")


def _write_markdown_report(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Watchlist QA Report (T-34, #45)",
        "",
        f"Generated: {report['generated_at_utc']} UTC "
        f"({report['audit_date_europe_bucharest']} Europe/Bucharest)",
        "",
        f"Black Friday 2026 date: **{report['bf_event_date']}** "
        f"({report['bf_date_source']})",
        f"Seven-day verification window starts {report['window_start_date']}; "
        f"this audit falls inside it: **{report['within_seven_day_window']}**.",
        "",
        f"Active watches checked: **{report['active_watch_count']}**.",
        f"Retailer checks performed: **{report['retailer_check_count']}**.",
        f"PASS: **{report['pass_count']}**. Not verified/failed: "
        f"**{report['unverified_or_failed_count']}**.",
        "",
        f"Overall acceptance status: **{report['overall_acceptance_status']}**",
        "",
        "## Per-watch evidence",
        "",
        "| watch_id | configured_site | retailer_checked | query | status | matches | reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for rec in report["records"]:
        lines.append(
            f"| {rec['watch_id']} | {rec['configured_site']} | {rec['retailer_checked']} "
            f"| {rec['query']} | {rec['status']} | {rec['matching_count']} "
            f"| {rec['reason'] or ''} |"
        )
    lines.append("")
    lines.append("## Corrections / removals")
    lines.append("")
    if not report["corrections"] and not report["removals"]:
        lines.append("None applied this run.")
    else:
        for c in report["corrections"]:
            lines.append(f"- Corrected: {c}")
        for r in report["removals"]:
            lines.append(f"- Removed: {r}")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only watchlist QA auditor: verifies every enabled "
            "data/watchlist.json entry against a live retailer search, "
            "using scrape.py's own scraper functions. Never runs "
            "analyze.py/notify.py and never writes price history, alerts, "
            "or health logs."
        )
    )
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=scrape.WATCHLIST_FILE,
        help="Path to the watchlist JSON file (default: data/watchlist.json)",
    )
    parser.add_argument(
        "--history",
        type=Path,
        default=scrape.HISTORY_FILE,
        help="Path to price_history.json, read-only (default: data/price_history.json)",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("docs/watchlist-qa-report.json"),
        help="Where to write the machine-readable JSON report",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=Path("docs/watchlist-qa.md"),
        help="Where to write the durable Markdown QA report",
    )
    parser.add_argument(
        "--no-delay",
        action="store_true",
        help="Disable the polite delay between live retailer calls (for local iteration only)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        from bf_price_monitor.config import load_watchlist

        watchlist = load_watchlist(args.watchlist)
    except ValueError as e:
        print(f"Watchlist validation failed: {e}", file=sys.stderr)
        return 1

    prior_history_file = scrape.HISTORY_FILE
    scrape.HISTORY_FILE = args.history
    try:
        history = scrape.load_history()
    finally:
        scrape.HISTORY_FILE = prior_history_file

    delay_range = (0, 0) if args.no_delay else DEFAULT_DELAY_RANGE
    try:
        report = run_audit(watchlist, history, delay_range=delay_range)
    finally:
        scrape._browser_state.close()

    _print_console_table(report)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _write_markdown_report(report, args.markdown_output)

    return 0 if report["overall_acceptance_status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
