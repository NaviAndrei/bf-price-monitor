"""Offline anomaly-detection pilot (T-29, #36).

    uv sync --frozen --extra anomaly
    uv run --no-sync python scripts/anomaly_pilot.py
    uv run --no-sync python scripts/anomaly_pilot.py --config pilot.json
    uv run --no-sync python scripts/anomaly_pilot.py --unseal-test

Reads data/price_history.db read-only (never migrates or writes it), fits
Isolation Forest + PELT on the chronological train split, and writes a
JSON report to data/exports/ (git-ignored). Offline only: no workflow,
Docker stage or alert path runs this. The test split stays sealed unless
--unseal-test is passed, and that choice is recorded in the report.
Precision/recall are reported only when enough real feedback labels exist.
See docs/anomaly-pilot.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bf_price_monitor.anomaly.config import AnomalyConfig
from bf_price_monitor.anomaly.dataset import (
    InsufficientDataError,
    attach_outcomes,
    build_feature_rows,
    chronological_split,
    data_fingerprint,
    data_profile,
    load_decided_alert_ids,
    load_labels,
    load_observations,
    open_readonly,
)

DB_FILE = Path("data/price_history.db")
REPORT_FILE = Path("data/exports/anomaly_pilot_report.json")


def build_report(
    db_path: Path, cfg: AnomalyConfig, *, unseal_test: bool
) -> dict[str, Any]:
    # Imported here so --help and config errors work without the extra.
    from bf_price_monitor.anomaly.model import InsufficientLabelsError, run_pilot

    conn = open_readonly(db_path)
    try:
        observations = load_observations(conn)
        decided = load_decided_alert_ids(conn)
        labels = load_labels(conn)
    finally:
        conn.close()

    rows = build_feature_rows(observations)
    attach_outcomes(rows, decided, labels)
    report: dict[str, Any] = {
        "config": {k: v for k, v in vars(cfg).items()},
        "config_fingerprint": cfg.fingerprint(),
        "data_fingerprint": data_fingerprint(observations, decided, labels),
        "data_profile": data_profile(observations, rows),
        "deterministic_alert_ids": len(decided),
        "labels": {
            "raw_counts": labels.raw_counts,
            "excluded_by_policy": labels.excluded_by_policy,
            "tied_alerts": labels.tied_alerts,
            "labelled_alerts": len(labels.by_alert),
            "matched_alerts": labels.matched_alerts,
            "unmatched_alerts": labels.unmatched_alerts,
        },
        "test_unsealed": unseal_test,
    }
    try:
        split = chronological_split(rows, cfg.train_fraction, cfg.validation_fraction)
        report.update(run_pilot(split, cfg, unseal_test=unseal_test))
    except (InsufficientDataError, InsufficientLabelsError) as exc:
        report["status"] = "blocked"
        report["reason"] = str(exc)
        return report

    metrics = report["validation"]["metrics"]
    if metrics["status"] == "evaluated":
        report["status"] = "evaluated"
    else:
        report["status"] = "blocked_insufficient_labels"
        report["reason"] = metrics["reason"]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline anomaly pilot (#36)")
    parser.add_argument("--db", type=Path, default=DB_FILE)
    parser.add_argument("--out", type=Path, default=REPORT_FILE)
    parser.add_argument(
        "--config", type=Path, help="JSON object overriding AnomalyConfig defaults"
    )
    parser.add_argument(
        "--unseal-test",
        action="store_true",
        help="also score the held-out test split (do this once, at the end)",
    )
    args = parser.parse_args(argv)

    overrides = json.loads(args.config.read_text("utf-8")) if args.config else {}
    cfg = AnomalyConfig.from_mapping(overrides)
    if not args.db.exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2

    report = build_report(args.db, cfg, unseal_test=args.unseal_test)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n", "utf-8")

    validation = report.get("validation", {})
    print(
        f"status={report['status']} "
        f"rows={report['data_profile'].get('feature_rows', 0)} "
        f"validation_flagged={validation.get('model_flagged', 'n/a')} "
        f"labelled={report['labels']['matched_alerts']} "
        f"report={args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
