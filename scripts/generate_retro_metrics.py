"""Post-Black-Friday retro metrics (T-36, #47).

    uv run python scripts/generate_retro_metrics.py
    uv run python scripts/generate_retro_metrics.py --start 2026-11-23 --end 2026-12-01
    gh run list --repo NaviAndrei/bf-price-monitor --workflow monitor.yml --limit 1000 \
        --json databaseId,conclusion,createdAt,event,status > data/exports/monitor_runs.json
    uv run python scripts/generate_retro_metrics.py --runs data/exports/monitor_runs.json

Reads, for one UTC window [start, end):
- data/scrape_health.jsonl: runs, per-retailer extraction and bot-challenge
  counts, and schedule-slot coverage (the uptime proxy).
- data/alert_outbox.jsonl: alerts by source and effective status.
- data/price_history.db (SQLite mode=ro): observations, delivery attempts,
  feedback labels, and purchased-alert savings estimates.
- data/extraction_failures.jsonl (optional): failure reasons only. On the
  runner this file is not carried across runs, so it is partial by design.
- --runs (optional): a `gh run list` JSON export, for workflow success rate.

It never writes an input. Output goes to data/exports/ (git-ignored) as
JSON plus a Markdown summary for docs/retros/. Outputs hold counts only: no
URLs, titles, rater identities or chat ids. Every metric whose inputs are
missing or empty is reported as "no_data", never as zero or an estimate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bf_price_monitor.anomaly.dataset import (
    LABEL_RELEVANCE,
    alert_decision_ids,
    load_observations,
    open_readonly,
)
from bf_price_monitor.storage.feedback import list_feedback

DB_FILE = Path("data/price_history.db")
HEALTH_FILE = Path("data/scrape_health.jsonl")
OUTBOX_FILE = Path("data/alert_outbox.jsonl")
EXTRACTION_FAILURES_FILE = Path("data/extraction_failures.jsonl")
OUT_FILE = Path("data/exports/retro_metrics.json")

# Black Friday 2026 is Friday 27 November; the default window is that
# Monday through Cyber Monday (30 November), end exclusive.
DEFAULT_START = datetime(2026, 11, 23, tzinfo=UTC)
DEFAULT_END = datetime(2026, 12, 1, tzinfo=UTC)
# monitor.yml runs on cron "0 */2 * * *".
DEFAULT_SLOT_HOURS = 2
REFERENCE_DAYS = 30

SAVINGS_CAVEAT = (
    "Estimate only. 'purchased' is self-reported through the Telegram "
    "buttons, and the saving is the offer's median observed price over the "
    "prior 30 days minus the alerted price, floored at zero. It is not a "
    "receipt, and it ignores shipping, vouchers and whether the buyer would "
    "have bought anyway."
)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def contains(self, moment: datetime | None) -> bool:
        return moment is not None and self.start <= moment < self.end


def _no_data(reason: str) -> dict[str, str]:
    return {"status": "no_data", "reason": reason}


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _parse_bound(value: str) -> datetime:
    parsed = _parse_utc(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not an ISO date or datetime: {value!r}")
    return parsed


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Parsed records and the count of lines that could not be parsed."""
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            malformed += 1
    return records, malformed


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _input_manifest(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"present": False, "path": None}
    if not path.exists():
        return {"present": False, "path": path.as_posix()}
    return {"present": True, "path": path.as_posix(), "sha256": _sha256(path)}


# --- scrape health --------------------------------------------------------------


def scrape_metrics(health: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not health:
        return _no_data("no scrape_health records in the window")
    return {
        "status": "ok",
        "runs": len({r.get("run_id") for r in health}),
        "store_runs": len(health),
        "products_parsed": sum(int(r.get("products_parsed") or 0) for r in health),
        "parse_failures": sum(int(r.get("parse_failures") or 0) for r in health),
    }


def slot_coverage(
    health: Sequence[dict[str, Any]], window: Window, slot_hours: int, now: datetime
) -> dict[str, Any]:
    """Share of elapsed schedule slots with at least one recorded run.

    A proxy for uptime that needs no GitHub API: a slot with no
    scrape_health record means the runner, the workflow or the scraper did
    not complete a run in that interval. `health` is the whole file, not
    just the window: if nothing was recorded before the window ends, a
    missing or fresh file can't be told apart from an outage, so the
    result is no_data rather than 0%.
    """
    starts = [
        moment
        for r in health
        if (moment := _parse_utc(r.get("run_started_utc"))) is not None
    ]
    if not any(s < window.end for s in starts):
        return _no_data("scrape_health.jsonl has no runs before the window end")
    horizon = min(window.end, now)
    slot = timedelta(hours=slot_hours)
    slots: list[datetime] = []
    cursor = window.start
    while cursor + slot <= horizon:
        slots.append(cursor)
        cursor += slot
    if not slots:
        return _no_data("no complete schedule slot has elapsed in the window")
    covered = {int((s - window.start) / slot) for s in starts if window.contains(s)}
    covered &= set(range(len(slots)))
    missed = [slots[i] for i in range(len(slots)) if i not in covered]
    return {
        "status": "ok",
        "slot_hours": slot_hours,
        "expected_slots": len(slots),
        "covered_slots": len(covered),
        "uptime_pct": round(100 * len(covered) / len(slots), 2),
        "longest_gap_hours": _longest_gap_hours(missed, slot_hours),
        "missed_slot_starts_utc": [m.isoformat() for m in missed],
    }


def _longest_gap_hours(missed: Sequence[datetime], slot_hours: int) -> int:
    longest = run = 0
    previous: datetime | None = None
    for moment in missed:
        contiguous = previous is not None and moment - previous == timedelta(
            hours=slot_hours
        )
        run = run + 1 if contiguous else 1
        longest = max(longest, run)
        previous = moment
    return longest * slot_hours


def retailer_metrics(
    health: Sequence[dict[str, Any]], failures: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    if not health:
        return _no_data("no scrape_health records in the window")
    by_store: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in health:
        by_store[str(record.get("store"))].append(record)
    reasons: dict[str, Counter[str]] = defaultdict(Counter)
    for failure in failures:
        reasons[str(failure.get("store"))][str(failure.get("failure_reason"))] += 1

    stores: dict[str, dict[str, Any]] = {}
    for store, records in sorted(by_store.items()):
        parsed = sum(int(r.get("products_parsed") or 0) for r in records)
        failed = sum(int(r.get("parse_failures") or 0) for r in records)
        challenged = sum(1 for r in records if r.get("challenge_detected"))
        waited = sum(1 for r in records if r.get("challenge_wait_entered"))
        latencies = [
            float(r["latency_seconds"])
            for r in records
            if isinstance(r.get("latency_seconds"), int | float)
        ]
        stores[store] = {
            "store_runs": len(records),
            "zero_product_runs": sum(
                1 for r in records if not r.get("products_parsed")
            ),
            "products_parsed": parsed,
            "parse_failures": failed,
            # Share of product cards that yielded a price.
            "extraction_success_rate": _rate(parsed, parsed + failed),
            "challenge_detected_runs": challenged,
            "challenge_detected_rate": _rate(challenged, len(records)),
            "challenge_wait_entered_runs": waited,
            "challenge_wait_entered_rate": _rate(waited, len(records)),
            "median_latency_seconds": (
                round(statistics.median(latencies), 3) if latencies else None
            ),
            "failure_reasons": dict(reasons[store].most_common()),
        }

    ranked = sorted(
        (s for s in stores if stores[s]["extraction_success_rate"] is not None),
        key=lambda s: (
            -stores[s]["extraction_success_rate"],
            stores[s]["challenge_detected_rate"],
            s,
        ),
    )
    return {
        "status": "ok",
        "ranking_rule": (
            "extraction_success_rate descending, then challenge_detected_rate ascending"
        ),
        "ranking": ranked,
        "best": ranked[0] if ranked else None,
        "worst": ranked[-1] if ranked else None,
        "stores": stores,
    }


# --- alerts ---------------------------------------------------------------------


def outbox_metrics(outbox: Sequence[dict[str, Any]], window: Window) -> dict[str, Any]:
    """Alerts by source and effective status (later records win, as in notify)."""
    effective: dict[str, dict[str, Any]] = {}
    for record in outbox:
        if "event_id" in record:
            effective[str(record["event_id"])] = record
    in_window = [
        r
        for r in effective.values()
        if window.contains(_parse_utc(r.get("created_at_utc")))
    ]
    if not in_window:
        return _no_data("no alert_outbox events created in the window")
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    for record in in_window:
        by_source[str(record.get("source"))][str(record.get("status"))] += 1
    deal = by_source.get("deal", Counter())
    return {
        "status": "ok",
        "events": len(in_window),
        "by_source": {k: dict(sorted(v.items())) for k, v in sorted(by_source.items())},
        "deal_alerts_triggered": sum(deal.values()),
        "deal_alerts_sent": deal.get("SENT", 0),
        "deal_alerts_dead_lettered": deal.get("DEAD_LETTER", 0),
        "deal_alerts_pending": deal.get("PENDING", 0),
    }


def _table_exists(conn: Any, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _db_rows(conn: Any, table: str, sql: str) -> list[Any]:
    return conn.execute(sql).fetchall() if _table_exists(conn, table) else []


def delivery_metrics(conn: Any, window: Window) -> dict[str, Any]:
    rows = [
        row
        for row in _db_rows(
            conn,
            "delivery_attempts",
            "SELECT alert_decision_id, channel, final_state, completed_at_utc "
            "FROM delivery_attempts",
        )
        if window.contains(_parse_utc(row["completed_at_utc"]))
    ]
    if not rows:
        return _no_data("no delivery_attempts completed in the window")
    return {
        "status": "ok",
        "attempts": len(rows),
        "alert_decisions": len({row["alert_decision_id"] for row in rows}),
        "by_final_state": dict(sorted(Counter(r["final_state"] for r in rows).items())),
        "by_channel": dict(sorted(Counter(r["channel"] for r in rows).items())),
    }


# --- feedback and savings -------------------------------------------------------


def feedback_rows(conn: Any, window: Window) -> list[dict[str, Any]]:
    if not _table_exists(conn, "alert_feedback_current"):
        return []
    return [
        item
        for item in list_feedback(conn, current_only=True, since=window.start)
        if window.contains(_parse_utc(item["received_at_utc"]))
    ]


def feedback_metrics(labels: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Acceptance = relevant / (relevant + not relevant) over current labels.

    Uses the #36 relevance policy: wrong_product and duplicate judge
    identity rather than the price, so they are counted but excluded from
    the rate.
    """
    if not labels:
        return _no_data("no feedback labels received in the window")
    counts = Counter(item["label"] for item in labels)
    relevant = sum(n for k, n in counts.items() if LABEL_RELEVANCE.get(k) == 1)
    rejected = sum(n for k, n in counts.items() if LABEL_RELEVANCE.get(k) == 0)
    return {
        "status": "ok",
        "labels": len(labels),
        "labelled_alerts": len({item["alert_decision_id"] for item in labels}),
        "raters": len({item["user_ref"] for item in labels}),
        "by_label": dict(sorted(counts.items())),
        "excluded_from_rate": len(labels) - relevant - rejected,
        "acceptance_rate_pct": (
            round(100 * relevant / (relevant + rejected), 2)
            if relevant + rejected
            else None
        ),
    }


def savings_metrics(
    labels: Sequence[dict[str, Any]], observations: Iterable[Any]
) -> dict[str, Any]:
    purchased = sorted(
        {item["alert_decision_id"] for item in labels if item["label"] == "purchased"}
    )
    if not purchased:
        return _no_data("no 'purchased' labels received in the window")

    by_offer: dict[str, list[Any]] = defaultdict(list)
    for observation in observations:
        by_offer[observation.offer_id].append(observation)
    located: dict[str, tuple[Any, list[float]]] = {}
    for history in by_offer.values():
        history.sort(key=lambda o: o.scraped_at)
        for i, obs in enumerate(history):
            prior = [
                p.price
                for p in history[:i]
                if obs.scraped_at - p.scraped_at <= timedelta(days=REFERENCE_DAYS)
            ]
            for decision_id in alert_decision_ids(obs.url, obs.price, obs.retailer):
                located.setdefault(decision_id, (obs, prior))

    savings: list[float] = []
    no_reference = 0
    for decision_id in purchased:
        if decision_id not in located:
            continue
        obs, prior = located[decision_id]
        if not prior:
            no_reference += 1
            continue
        savings.append(max(statistics.median(prior) - obs.price, 0.0))
    matched = len(savings) + no_reference
    return {
        "status": "ok",
        "purchased_alerts": len(purchased),
        "matched_to_observation": matched,
        "unmatched": len(purchased) - matched,
        "without_prior_30d_price": no_reference,
        "estimated_savings_total": round(sum(savings), 2),
        "estimated_savings_median": (
            round(statistics.median(savings), 2) if savings else None
        ),
        "currency": "as stored by the scrapers (RON for the current retailers)",
        "caveat": SAVINGS_CAVEAT,
    }


# --- GitHub runs ----------------------------------------------------------------


def workflow_metrics(runs: Sequence[dict[str, Any]], window: Window) -> dict[str, Any]:
    in_window = [r for r in runs if window.contains(_parse_utc(r.get("createdAt")))]
    completed = [r for r in in_window if r.get("status") == "completed"]
    if not completed:
        return _no_data("no completed monitor.yml runs in the window")
    conclusions = Counter(str(r.get("conclusion")) for r in completed)
    scheduled = [r for r in completed if r.get("event") == "schedule"]
    return {
        "status": "ok",
        "runs": len(completed),
        "by_conclusion": dict(sorted(conclusions.items())),
        "success_rate_pct": round(100 * conclusions["success"] / len(completed), 2),
        "scheduled_runs": len(scheduled),
        "scheduled_success_rate_pct": (
            round(
                100
                * sum(1 for r in scheduled if r.get("conclusion") == "success")
                / len(scheduled),
                2,
            )
            if scheduled
            else None
        ),
    }


# --- report ---------------------------------------------------------------------


def build_report(
    *,
    window: Window,
    db_path: Path,
    health_path: Path,
    outbox_path: Path,
    failures_path: Path,
    runs_path: Path | None,
    slot_hours: int = DEFAULT_SLOT_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    malformed: dict[str, int] = {}

    def _jsonl(name: str, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records, malformed[name] = _read_jsonl(path)
        return records

    def _in_window(records: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
        return [r for r in records if window.contains(_parse_utc(r.get(field)))]

    all_health = _jsonl("scrape_health", health_path)
    health = _in_window(all_health, "run_started_utc")
    failures = _in_window(_jsonl("extraction_failures", failures_path), "timestamp_utc")
    outbox = _jsonl("alert_outbox", outbox_path)

    report: dict[str, Any] = {
        "generator": "generate_retro_metrics/1",
        "window": {
            "start_utc": window.start.isoformat(),
            "end_utc": window.end.isoformat(),
        },
        "generated_at_utc": now.isoformat(),
        "inputs": {
            "db": _input_manifest(db_path),
            "scrape_health": _input_manifest(health_path),
            "alert_outbox": _input_manifest(outbox_path),
            "extraction_failures": _input_manifest(failures_path),
            "monitor_runs": _input_manifest(runs_path),
        },
        "malformed_lines": malformed,
        "scrapes": scrape_metrics(health),
        "uptime": {
            "schedule_slots": slot_coverage(all_health, window, slot_hours, now),
            "workflow_runs": (
                workflow_metrics(json.loads(runs_path.read_text("utf-8")), window)
                if runs_path is not None and runs_path.exists()
                else _no_data("no --runs export supplied")
            ),
        },
        "alerts": {"outbox": outbox_metrics(outbox, window)},
        "retailers": retailer_metrics(health, failures),
    }

    if db_path.exists():
        conn = open_readonly(db_path)
        try:
            observations = load_observations(conn)
            labels = feedback_rows(conn, window)
            report["alerts"]["delivery_attempts"] = delivery_metrics(conn, window)
        finally:
            conn.close()
        report["scrapes"]["observations_in_db"] = sum(
            1 for o in observations if window.contains(o.scraped_at)
        )
        report["feedback"] = feedback_metrics(labels)
        report["savings"] = savings_metrics(labels, observations)
    else:
        missing = _no_data(f"database not found: {db_path.as_posix()}")
        report["alerts"]["delivery_attempts"] = missing
        report["feedback"] = missing
        report["savings"] = missing

    sections = [
        report["scrapes"],
        report["alerts"]["outbox"],
        report["alerts"]["delivery_attempts"],
        report["feedback"],
    ]
    has_data = any(s.get("status") == "ok" for s in sections)
    has_data = has_data or bool(report["scrapes"].get("observations_in_db"))
    report["evidence_status"] = "has_event_data" if has_data else "no_event_data"
    return report


def _cell(section: dict[str, Any], key: str) -> str:
    if section.get("status") != "ok":
        return "no data"
    value = section.get(key)
    return "n/a" if value is None else str(value)


def render_markdown(report: dict[str, Any]) -> str:
    window = report["window"]
    scrapes = report["scrapes"]
    slots = report["uptime"]["schedule_slots"]
    runs = report["uptime"]["workflow_runs"]
    outbox = report["alerts"]["outbox"]
    feedback = report["feedback"]
    savings = report["savings"]
    lines = [
        f"<!-- generated by scripts/generate_retro_metrics.py at "
        f"{report['generated_at_utc']} -->",
        f"Window: {window['start_utc']} to {window['end_utc']} (end exclusive). "
        f"Evidence status: **{report['evidence_status']}**.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Scrape runs | {_cell(scrapes, 'runs')} |",
        f"| Store runs | {_cell(scrapes, 'store_runs')} |",
        f"| Products parsed | {_cell(scrapes, 'products_parsed')} |",
        f"| Uptime, schedule slots covered (%) | {_cell(slots, 'uptime_pct')} |",
        f"| Longest gap (hours) | {_cell(slots, 'longest_gap_hours')} |",
        f"| Workflow success rate (%) | {_cell(runs, 'success_rate_pct')} |",
        f"| Deal alerts triggered | {_cell(outbox, 'deal_alerts_triggered')} |",
        f"| Deal alerts sent | {_cell(outbox, 'deal_alerts_sent')} |",
        f"| Deal alerts dead-lettered | {_cell(outbox, 'deal_alerts_dead_lettered')} |",
        f"| Feedback labels | {_cell(feedback, 'labels')} |",
        f"| Feedback acceptance rate (%) | {_cell(feedback, 'acceptance_rate_pct')} |",
        f"| Purchased alerts (self-reported) | {_cell(savings, 'purchased_alerts')} |",
        f"| Estimated savings, total | {_cell(savings, 'estimated_savings_total')} |",
        "",
    ]
    retailers = report["retailers"]
    if retailers.get("status") == "ok":
        lines += [
            "| Retailer | Store runs | Extraction success | Challenge rate "
            "| Zero-product runs |",
            "|---|---|---|---|---|",
        ]
        for store in retailers["ranking"] + [
            s for s in retailers["stores"] if s not in retailers["ranking"]
        ]:
            s = retailers["stores"][store]
            lines.append(
                f"| {store} | {s['store_runs']} | {s['extraction_success_rate']} "
                f"| {s['challenge_detected_rate']} | {s['zero_product_runs']} |"
            )
    else:
        lines.append("Retailers: no data.")
    if savings.get("status") == "ok":
        lines += ["", f"Savings caveat: {savings['caveat']}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post-Black-Friday retro metrics (#47)"
    )
    parser.add_argument("--start", type=_parse_bound, default=DEFAULT_START)
    parser.add_argument("--end", type=_parse_bound, default=DEFAULT_END)
    parser.add_argument("--db", type=Path, default=DB_FILE)
    parser.add_argument("--health", type=Path, default=HEALTH_FILE)
    parser.add_argument("--outbox", type=Path, default=OUTBOX_FILE)
    parser.add_argument(
        "--extraction-failures", type=Path, default=EXTRACTION_FAILURES_FILE
    )
    parser.add_argument("--runs", type=Path, help="`gh run list --json` export")
    parser.add_argument("--slot-hours", type=int, default=DEFAULT_SLOT_HOURS)
    parser.add_argument("--out", type=Path, default=OUT_FILE)
    args = parser.parse_args(argv)
    if args.start >= args.end:
        parser.error("--start must be before --end")
    if args.slot_hours < 1:
        parser.error("--slot-hours must be at least 1")

    report = build_report(
        window=Window(args.start, args.end),
        db_path=args.db,
        health_path=args.health,
        outbox_path=args.outbox,
        failures_path=args.extraction_failures,
        runs_path=args.runs,
        slot_hours=args.slot_hours,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    markdown = args.out.with_suffix(".md")
    markdown.write_text(render_markdown(report), "utf-8")
    print(
        f"evidence_status={report['evidence_status']} "
        f"window={args.start.date()}..{args.end.date()} "
        f"report={args.out} summary={markdown}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
