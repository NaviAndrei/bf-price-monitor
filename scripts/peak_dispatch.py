"""Catch-up dispatcher for the BF-week hourly cadence.

GitHub delivers only part of monitor.yml's scheduled runs (measured
2026-10-21..30: 41% of the 2-hour cron, 11.5% of the old */30 cron; see
docs/runbooks/BF_WEEK_CADENCE.md). This script runs every 10 minutes from
Windows Task Scheduler on the runner PC and dispatches monitor.yml with
`workflow_dispatch` only when a scrape is due, so a late or dropped cron is
made up without doubling scrapes that did arrive.

"Last scrape" is the newest monitor.yml run, any event, whose
`scrape-analyze-notify` job was not skipped. A run that has not completed
counts as "scrape under way" and blocks a dispatch. Inside the window set by
the BF_PEAK_START_UTC / BF_PEAK_END_UTC repository variables a scrape is due
after WINDOW_INTERVAL_MINUTES (minus SLACK_MINUTES); outside it nothing is
dispatched unless --baseline-interval is given. Missing variables mean no
peak window (only --baseline-interval applies); invalid ones are an error.

Exit codes: 0 dispatched or nothing to do, 1 any gh or parse error (nothing
dispatched), 2 bad usage. One log line per tick goes to
%LOCALAPPDATA%\\bf-price-monitor\\peak_dispatch.log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cadence_gate import is_in_peak_window

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO = "NaviAndrei/bf-price-monitor"
REPO_ARGS = ["--repo", REPO]
WORKFLOW_FILE = "monitor.yml"
SCRAPE_JOB = "scrape-analyze-notify"
START_VAR = "BF_PEAK_START_UTC"
END_VAR = "BF_PEAK_END_UTC"
WINDOW_INTERVAL_MINUTES = 60
SLACK_MINUTES = 5
LOOKBACK = timedelta(hours=24)
# A run still queued or in progress after this long is treated as stuck and
# no longer blocks a dispatch.
STALE_RUN = timedelta(hours=2)
RUN_LIST_LIMIT = 30
GH_TIMEOUT_SECONDS = 60
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_LINES = 500
_TOKEN_RE = re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+")

GhRunner = Callable[[Sequence[str]], str]
Clock = Callable[[], datetime]


class GhError(Exception):
    """A gh call failed or returned something unparsable."""


@dataclass(frozen=True)
class Decision:
    action: str  # dispatch | not-due | skip | error
    reason: str


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("[redacted]", text)


def run_gh(args: Sequence[str]) -> str:
    exe = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
    try:
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GH_TIMEOUT_SECONDS,
            cwd=REPO_ROOT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh {args[0]} could not run: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        detail = _redact(proc.stderr.strip())[:200]
        raise GhError(
            f"gh {' '.join(args[:2])} failed (exit {proc.returncode}): {detail}"
        )
    return proc.stdout


def read_variable(gh: GhRunner, name: str) -> str | None:
    try:
        return gh(["variable", "get", name, *REPO_ARGS]).strip() or None
    except GhError as exc:
        if "was not found" in str(exc).lower():
            return None
        raise


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("no UTC offset")
    return parsed


def _is_scrape(jobs: list[dict[str, object]]) -> bool:
    return any(
        job.get("name") == SCRAPE_JOB and job.get("conclusion") != "skipped"
        for job in jobs
    )


def find_last_scrape(gh: GhRunner, now: datetime) -> tuple[datetime | None, str | None]:
    """Return (created time of the newest scrape, reason a run is under way)."""
    raw = gh(
        [
            "run",
            "list",
            "--workflow",
            WORKFLOW_FILE,
            "--limit",
            str(RUN_LIST_LIMIT),
            "--json",
            "databaseId,createdAt,status",
            *REPO_ARGS,
        ]
    )
    try:
        runs = sorted(
            (
                (_parse_utc(r["createdAt"]), int(r["databaseId"]), str(r["status"]))
                for r in json.loads(raw)
            ),
            reverse=True,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise GhError(f"unparsable run list: {type(exc).__name__}") from exc
    for created, run_id, status in runs:
        if status != "completed" and now - created <= STALE_RUN:
            return None, f"run {run_id} is {status}"
    for created, run_id, status in runs:
        if status != "completed" or now - created > LOOKBACK:
            continue
        try:
            jobs = json.loads(
                gh(["run", "view", str(run_id), "--json", "jobs", *REPO_ARGS])
            )["jobs"]
        except (ValueError, KeyError, TypeError) as exc:
            raise GhError(f"unparsable jobs for run {run_id}") from exc
        if _is_scrape(jobs):
            return created, None
    return None, None


def evaluate(gh: GhRunner, now: datetime, baseline_interval: int) -> Decision:
    start = read_variable(gh, START_VAR)
    end = read_variable(gh, END_VAR)
    in_window = False
    if start is not None and end is not None:
        try:
            _parse_utc(start)
            _parse_utc(end)
        except ValueError:
            return Decision(
                "error", "window variables are not valid ISO 8601 with offset"
            )
        in_window = is_in_peak_window(now, start, end)
    if in_window:
        interval = WINDOW_INTERVAL_MINUTES
        where = "inside window"
    elif baseline_interval > 0:
        interval = baseline_interval
        where = (
            "outside window, baseline on"
            if start is not None and end is not None
            else "no window set, baseline on"
        )
    else:
        if start is None or end is None:
            return Decision("skip", "no peak window set, baseline off")
        return Decision("skip", "outside peak window, baseline off")

    last, under_way = find_last_scrape(gh, now)
    if under_way:
        return Decision("skip", f"scrape under way: {under_way}")
    if last is None:
        return Decision("dispatch", f"{where}: no scrape in the last 24 h")
    age = now - last
    needed = timedelta(minutes=max(interval - SLACK_MINUTES, 0))
    minutes = int(age.total_seconds() // 60)
    if age >= needed:
        return Decision(
            "dispatch",
            f"{where}: last scrape {minutes} min ago, due at {needed.seconds // 60}",
        )
    return Decision(
        "not-due",
        f"{where}: last scrape {minutes} min ago, due at {needed.seconds // 60}",
    )


def default_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "bf-price-monitor" / "peak_dispatch.log"


def write_log(path: Path, now: datetime, decision: str, reason: str) -> None:
    line = f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}\t{decision}\t{_redact(reason)}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            kept = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
            path.write_text("".join(kept[-LOG_KEEP_LINES:]), encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        print(
            f"[peak_dispatch] could not write log: {type(exc).__name__}",
            file=sys.stderr,
        )


# gh puts the HTTP status in its error text. These are the three cases the
# runbook troubleshooting table covers, so the log line says what to fix.
_STATUS_HINTS = (
    ("HTTP 401", "token rejected: recreate gh_token.dpapi (runbook step 2)"),
    (
        "HTTP 403",
        "token lacks a permission: give it Actions: Read and write and Variables: Read",
    ),
    (
        "HTTP 404",
        "token cannot see this repository: grant it NaviAndrei/bf-price-monitor",
    ),
)


def _explain(message: str) -> str:
    for status, hint in _STATUS_HINTS:
        if status in message:
            return f"{message} - {hint}"
    return message


def main(
    argv: Sequence[str] | None = None,
    *,
    gh: GhRunner = run_gh,
    clock: Clock = lambda: datetime.now(UTC),
    log_path: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="decide, do not dispatch"
    )
    parser.add_argument(
        "--baseline-interval",
        type=int,
        default=0,
        metavar="MINUTES",
        help="also keep this cadence outside the window (default 0 = off)",
    )
    parser.add_argument("--log-file", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.baseline_interval < 0:
        parser.error("--baseline-interval must be 0 or more")

    now = clock()
    path = log_path or args.log_file or default_log_path()
    try:
        decision = evaluate(gh, now, args.baseline_interval)
        if decision.action == "dispatch" and not args.dry_run:
            gh(["workflow", "run", WORKFLOW_FILE, "--ref", "master", *REPO_ARGS])
    except GhError as exc:
        decision = Decision("error", _explain(str(exc)))
    label = decision.action
    if decision.action == "dispatch":
        label = "would-dispatch (dry-run)" if args.dry_run else "dispatched"
    reason = _redact(decision.reason)
    write_log(path, now, label, reason)
    safe = f"[peak_dispatch] {label}: {reason}".encode("ascii", "replace")
    print(safe.decode("ascii"))
    return 1 if decision.action == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
