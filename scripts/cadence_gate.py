"""BF-week adaptive cadence gate (T-32, #43).

monitor.yml runs on two schedules: the normal `0 */2 * * *` cadence, and a
tighter `*/30 * * * *` cadence meant only for the configured BF peak window.
This script is the single source of truth for whether a given triggered run
should actually scrape: the 2-hour cron and any `workflow_dispatch` always
proceed; the 30-minute cron proceeds only when `now` (UTC) falls inside the
window defined by the BF_PEAK_START_UTC / BF_PEAK_END_UTC repository
variables (ISO 8601, e.g. "2026-11-26T18:00:00+00:00").

Deliberately fails closed: if either boundary is unset or unparsable, the
tightened cadence does not run. There is no hardcoded BF date in this
project -- eMAG/PC Garage/Flanco each announce their own Black Friday
campaign dates independently and not far enough in advance to bake into
code, so the window is only ever what the owner has explicitly configured
via repository variables. See docs/runbooks/BF_WEEK_CADENCE.md.

Always writes a `should_run=<true|false>` line to $GITHUB_OUTPUT (or stdout
when run locally).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

TIGHT_CADENCE_CRON = "*/30 * * * *"


def is_in_peak_window(
    now_utc: datetime, start_iso: str | None, end_iso: str | None
) -> bool:
    if not start_iso or not end_iso:
        return False
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except ValueError:
        return False
    if start.tzinfo is None or end.tzinfo is None:
        return False
    return start <= now_utc <= end


def should_run(event_name: str, schedule: str | None) -> bool:
    # workflow_dispatch (manual trigger) and the baseline 2-hour cron always
    # proceed; only the tightened 30-minute cron is gated by the peak window.
    if event_name != "schedule" or schedule != TIGHT_CADENCE_CRON:
        return True
    return is_in_peak_window(
        datetime.now(UTC),
        os.environ.get("BF_PEAK_START_UTC"),
        os.environ.get("BF_PEAK_END_UTC"),
    )


def _write_github_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--schedule", default=os.environ.get("CADENCE_EVENT_SCHEDULE"))
    args = parser.parse_args(argv)

    result = should_run(args.event_name, args.schedule)
    _write_github_output("should_run", "true" if result else "false")
    print(
        f"[cadence_gate] event_name={args.event_name!r} schedule={args.schedule!r} "
        f"-> should_run={result}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
