"""T-36 (#47): post-Black-Friday retro metric generator.

Every fixture below is constructed test data. The generator's contract is
that missing inputs are reported as no_data, never as zero or an estimate.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import generate_retro_metrics as retro
import pytest

from bf_price_monitor.anomaly.dataset import alert_decision_ids
from bf_price_monitor.storage import sqlite as sqlite_storage

BF = datetime(2026, 11, 27, tzinfo=UTC)
WINDOW = retro.Window(BF, BF + timedelta(hours=8))
AFTER = BF + timedelta(days=5)
URL = "https://www.emag.ro/laptop-x/pd/ABC123XYZ/"


def _jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), "utf-8")
    return path


def _health(store, hour, *, parsed=10, failures=0, challenge=False, waited=False):
    return {
        "store": store,
        "run_id": f"run-{hour}",
        "run_started_utc": (BF + timedelta(hours=hour, minutes=5)).isoformat(),
        "watches_requested": 2,
        "products_parsed": parsed,
        "matched_count": 1,
        "policy_blocked_count": 0,
        "parse_failures": failures,
        "challenge_detected": challenge,
        "challenge_wait_entered": waited,
        "latency_seconds": 12.5,
        "last_known_good_utc": None,
    }


def _report(tmp_path, **paths):
    defaults = {
        "db_path": tmp_path / "missing.db",
        "health_path": tmp_path / "missing_health.jsonl",
        "outbox_path": tmp_path / "missing_outbox.jsonl",
        "failures_path": tmp_path / "missing_failures.jsonl",
        "runs_path": None,
    }
    defaults.update(paths)
    return retro.build_report(window=WINDOW, now=AFTER, **defaults)


def test_missing_inputs_are_no_data_never_zero(tmp_path):
    report = _report(tmp_path)
    assert report["evidence_status"] == "no_event_data"
    for section in (
        report["scrapes"],
        report["uptime"]["schedule_slots"],
        report["uptime"]["workflow_runs"],
        report["alerts"]["outbox"],
        report["alerts"]["delivery_attempts"],
        report["retailers"],
        report["feedback"],
        report["savings"],
    ):
        assert section["status"] == "no_data"
        assert section["reason"]
    assert not any(i["present"] for i in report["inputs"].values())
    markdown = retro.render_markdown(report)
    assert "| Uptime, schedule slots covered (%) | no data |" in markdown
    assert "Retailers: no data." in markdown


def test_uptime_is_slot_coverage_and_needs_history(tmp_path):
    health = _jsonl(
        tmp_path / "h.jsonl",
        [_health("emag", 0), _health("emag", 2), _health("emag", 6)],
    )
    slots = _report(tmp_path, health_path=health)["uptime"]["schedule_slots"]
    assert slots["expected_slots"] == 4
    assert slots["covered_slots"] == 3
    assert slots["uptime_pct"] == 75.0
    assert slots["longest_gap_hours"] == 2
    assert slots["missed_slot_starts_utc"] == [(BF + timedelta(hours=4)).isoformat()]

    # A file whose history starts only after the window can't show an outage.
    late = _jsonl(tmp_path / "late.jsonl", [_health("emag", 48)])
    assert _report(tmp_path, health_path=late)["uptime"]["schedule_slots"] == {
        "status": "no_data",
        "reason": "scrape_health.jsonl has no runs before the window end",
    }


def test_slot_coverage_counts_only_elapsed_slots():
    health = [_health("emag", 0)]
    mid_window = BF + timedelta(hours=3)
    slots = retro.slot_coverage(health, WINDOW, 2, mid_window)
    assert slots["expected_slots"] == 1 and slots["uptime_pct"] == 100.0
    assert retro.slot_coverage(health, WINDOW, 2, BF)["status"] == "no_data"


def test_retailer_ranking_and_failure_reasons(tmp_path):
    health = _jsonl(
        tmp_path / "h.jsonl",
        [
            _health("emag", 0, parsed=18, failures=2),
            _health("emag", 2, parsed=20),
            _health("flanco", 0, parsed=5, failures=5, challenge=True, waited=True),
            _health("flanco", 2, parsed=0, challenge=True),
            _health("pcgarage", 0, parsed=0),
            _health("emag", 30),  # outside the window
        ],
    )
    failures = _jsonl(
        tmp_path / "f.jsonl",
        [
            {
                "store": "flanco",
                "url": URL,
                "query": "laptop",
                "timestamp_utc": (BF + timedelta(minutes=10)).isoformat(),
                "failure_reason": "no price found in any extraction layer",
                "run_id": "",
            }
        ],
    )
    report = _report(tmp_path, health_path=health, failures_path=failures)
    assert report["scrapes"] == {
        "status": "ok",
        "runs": 2,
        "store_runs": 5,
        "products_parsed": 43,
        "parse_failures": 7,
    }
    retailers = report["retailers"]
    assert retailers["ranking"] == ["emag", "flanco"]
    assert retailers["best"] == "emag" and retailers["worst"] == "flanco"
    emag, flanco = retailers["stores"]["emag"], retailers["stores"]["flanco"]
    assert emag["extraction_success_rate"] == 0.95
    assert flanco["extraction_success_rate"] == 0.5
    assert flanco["challenge_detected_rate"] == 1.0
    assert flanco["challenge_wait_entered_rate"] == 0.5
    assert flanco["zero_product_runs"] == 1
    assert flanco["failure_reasons"] == {"no price found in any extraction layer": 1}
    # No parsed or failed cards: unrankable, but still reported.
    assert retailers["stores"]["pcgarage"]["extraction_success_rate"] is None
    assert "emag.ro" not in json.dumps(report)
    assert "| pcgarage | 1 | None |" in retro.render_markdown(report)


def test_outbox_uses_effective_status_and_window(tmp_path):
    def record(event_id, status, source="deal", hours=1):
        return {
            "event_id": event_id,
            "source": source,
            "status": status,
            "created_at_utc": (BF + timedelta(hours=hours)).isoformat(),
        }

    outbox = _jsonl(
        tmp_path / "o.jsonl",
        [
            record("a", "PENDING"),
            record("a", "SENT"),  # later line wins
            record("b", "PENDING"),
            record("b", "DEAD_LETTER"),
            record("c", "PENDING"),
            record("h", "SENT", source="health"),
            record("old", "SENT", hours=-30),
        ],
    )
    with open(outbox, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    report = _report(tmp_path, outbox_path=outbox)
    alerts = report["alerts"]["outbox"]
    assert alerts["events"] == 4
    assert alerts["deal_alerts_triggered"] == 3
    assert alerts["deal_alerts_sent"] == 1
    assert alerts["deal_alerts_dead_lettered"] == 1
    assert alerts["deal_alerts_pending"] == 1
    assert alerts["by_source"]["health"] == {"SENT": 1}
    assert report["malformed_lines"]["alert_outbox"] == 1
    assert report["evidence_status"] == "has_event_data"


def _seed_db(path):
    conn = sqlite_storage.init_db(path)
    prices = [(BF - timedelta(days=d), 1000.0) for d in (20, 10, 5)]
    prices += [(BF - timedelta(days=40), 5000.0), (BF + timedelta(hours=1), 800.0)]
    for when, price in prices:
        sqlite_storage.record_observation(
            conn,
            {
                "sku": "ABC123XYZ",
                "title": "Laptop X",
                "price": price,
                "in_stock": True,
                "retailer": "emag",
                "url": URL,
                "scraped_at": when.isoformat(),
            },
        )
    alert_id = alert_decision_ids(URL, 800.0, "emag")[0]
    sqlite_storage.record_delivery_attempt_new_cycle(
        conn,
        {
            "alert_decision_id": alert_id,
            "channel": "telegram",
            "destination": "d" * 64,
            "response_class": "2xx",
            "final_state": "delivered",
            "completed_at_utc": (BF + timedelta(hours=1)).isoformat(),
        },
    )
    other = "11111111-2222-3333-4444-555555555555"
    with conn:
        for seq, (decision, label, user) in enumerate(
            [
                (alert_id, "purchased", "a"),
                (alert_id, "purchased", "b"),
                (other, "fake_discount", "a"),
                (other, "wrong_product", "b"),
                (other, "useful", "c"),
            ]
        ):
            conn.execute(
                "INSERT INTO alert_feedback (callback_query_id, update_id, "
                "alert_decision_id, label, user_ref, chat_ref, callback_version, "
                "received_at_utc) VALUES (?, ?, ?, ?, ?, ?, 'fb1', ?)",
                (
                    f"cb{seq}",
                    seq,
                    decision,
                    label,
                    user * 64,
                    "c" * 64,
                    (BF + timedelta(hours=2, minutes=seq)).isoformat(),
                ),
            )
    conn.close()


def test_feedback_savings_and_deliveries_from_sqlite(tmp_path):
    db = tmp_path / "price_history.db"
    _seed_db(db)
    before = db.read_bytes()
    report = _report(tmp_path, db_path=db)

    feedback = report["feedback"]
    assert feedback["labels"] == 5
    assert feedback["raters"] == 3
    assert feedback["excluded_from_rate"] == 1
    # purchased x2 + useful relevant, fake_discount not: 3 / 4.
    assert feedback["acceptance_rate_pct"] == 75.0

    savings = report["savings"]
    assert savings["purchased_alerts"] == 1  # two raters, one alert
    assert savings["matched_to_observation"] == 1
    # The 40-day-old 5000 is outside the 30-day reference; median(1000 x3).
    assert savings["estimated_savings_total"] == 200.0
    assert "Estimate only" in savings["caveat"]

    deliveries = report["alerts"]["delivery_attempts"]
    assert deliveries["attempts"] == 1
    assert deliveries["by_final_state"] == {"delivered": 1}
    assert report["scrapes"]["observations_in_db"] == 1
    assert report["evidence_status"] == "has_event_data"

    text = json.dumps(report)
    assert "a" * 64 not in text and "emag.ro" not in text
    assert db.read_bytes() == before


def test_savings_needs_a_located_alert_with_prior_prices(tmp_path):
    labels = [
        {"alert_decision_id": "unknown", "label": "purchased", "user_ref": "u"},
    ]
    savings = retro.savings_metrics(labels, [])
    assert savings["unmatched"] == 1
    assert savings["estimated_savings_total"] == 0
    assert savings["estimated_savings_median"] is None


def test_workflow_runs_export(tmp_path):
    def run(conclusion, event="schedule", hours=1, status="completed"):
        return {
            "databaseId": hours,
            "conclusion": conclusion,
            "createdAt": (BF + timedelta(hours=hours)).isoformat(),
            "event": event,
            "status": status,
        }

    runs = tmp_path / "runs.json"
    runs.write_text(
        json.dumps(
            [
                run("success"),
                run("failure", hours=3),
                run("success", event="workflow_dispatch", hours=4),
                run("", status="in_progress", hours=5),
                run("success", hours=-10),
            ]
        ),
        "utf-8",
    )
    workflow = _report(tmp_path, runs_path=runs)["uptime"]["workflow_runs"]
    assert workflow["runs"] == 3
    assert workflow["by_conclusion"] == {"failure": 1, "success": 2}
    assert workflow["success_rate_pct"] == pytest.approx(66.67)
    assert workflow["scheduled_success_rate_pct"] == 50.0


def test_cli_writes_json_and_markdown(tmp_path, capsys):
    out = tmp_path / "exports" / "retro.json"
    code = retro.main(
        [
            "--start",
            "2026-11-27",
            "--end",
            "2026-11-28",
            "--db",
            str(tmp_path / "none.db"),
            "--health",
            str(tmp_path / "none.jsonl"),
            "--outbox",
            str(tmp_path / "none.jsonl"),
            "--extraction-failures",
            str(tmp_path / "none.jsonl"),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    report = json.loads(out.read_text("utf-8"))
    assert report["window"]["start_utc"] == "2026-11-27T00:00:00+00:00"
    assert "no_event_data" in out.with_suffix(".md").read_text("utf-8")
    assert "evidence_status=no_event_data" in capsys.readouterr().out


def test_cli_rejects_an_empty_window():
    with pytest.raises(SystemExit):
        retro.main(["--start", "2026-11-28", "--end", "2026-11-27"])


def test_default_window_is_black_friday_week():
    assert retro.DEFAULT_START.strftime("%A %Y-%m-%d") == "Monday 2026-11-23"
    assert (retro.DEFAULT_END - timedelta(days=1)).strftime("%A") == "Monday"
    assert datetime(2026, 11, 27).strftime("%A") == "Friday"


def test_generator_never_opens_the_db_for_writing(tmp_path):
    db = tmp_path / "price_history.db"
    _seed_db(db)
    conn = retro.open_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM price_observations")
    conn.close()
