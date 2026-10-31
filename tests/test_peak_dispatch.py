"""Catch-up dispatcher (scripts/peak_dispatch.py): offline, injected gh and clock."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import peak_dispatch
import pytest

START = "2026-11-05T18:00:00+00:00"
END = "2026-11-30T21:59:00+00:00"
IN_WINDOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
OUTSIDE = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGh:
    """Stands in for run_gh. `runs` = [(id, created, status, scraped)]."""

    def __init__(self, runs=(), variables=None, fail=None):
        self.runs = list(runs)
        self.variables = {"BF_PEAK_START_UTC": START, "BF_PEAK_END_UTC": END}
        if variables is not None:
            self.variables = variables
        self.fail = fail  # substring of a command that raises GhError
        self.calls: list[list[str]] = []

    @property
    def dispatched(self) -> bool:
        return any(c[:2] == ["workflow", "run"] for c in self.calls)

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if self.fail and self.fail in " ".join(args):
            raise peak_dispatch.GhError(f"gh {args[0]} failed (exit 1): boom")
        if args[:2] == ["variable", "get"]:
            if args[2] not in self.variables:
                raise peak_dispatch.GhError(
                    f"gh variable get failed: {args[2]} was not found"
                )
            return self.variables[args[2]] + "\n"
        if args[:2] == ["run", "list"]:
            return json.dumps(
                [
                    {"databaseId": i, "createdAt": _iso(c), "status": s}
                    for i, c, s, _ in self.runs
                ]
            )
        if args[:2] == ["run", "view"]:
            run = next(r for r in self.runs if str(r[0]) == args[2])
            conclusion = "success" if run[3] else "skipped"
            return json.dumps(
                {
                    "jobs": [
                        {"name": "gate", "conclusion": "success"},
                        {"name": "scrape-analyze-notify", "conclusion": conclusion},
                    ]
                }
            )
        if args[:2] == ["workflow", "run"]:
            return ""
        raise AssertionError(args)


def _run(argv, gh, now, tmp_path):
    log = tmp_path / "peak.log"
    code = peak_dispatch.main(argv, gh=gh, clock=lambda: now, log_path=log)
    return code, log


def _ago(now, minutes):
    return now - timedelta(minutes=minutes)


def test_outside_window_baseline_off_never_dispatches(tmp_path):
    gh = FakeGh(runs=[(1, _ago(OUTSIDE, 600), "completed", True)])
    code, _ = _run([], gh, OUTSIDE, tmp_path)
    assert code == 0 and not gh.dispatched


def test_inside_window_due_dispatches(tmp_path):
    gh = FakeGh(runs=[(1, _ago(IN_WINDOW, 70), "completed", True)])
    code, log = _run([], gh, IN_WINDOW, tmp_path)
    assert code == 0 and gh.dispatched
    assert gh.calls[-1] == [
        "workflow",
        "run",
        "monitor.yml",
        "--ref",
        "master",
        "--repo",
        "NaviAndrei/bf-price-monitor",
    ]
    assert "\tdispatched\t" in log.read_text()


def test_inside_window_recent_scrape_is_not_due(tmp_path):
    gh = FakeGh(runs=[(1, _ago(IN_WINDOW, 20), "completed", True)])
    code, _ = _run([], gh, IN_WINDOW, tmp_path)
    assert code == 0 and not gh.dispatched


@pytest.mark.parametrize(("minutes", "expected"), [(54, False), (55, True), (56, True)])
def test_slack_edge(tmp_path, minutes, expected):
    gh = FakeGh(runs=[(1, _ago(IN_WINDOW, minutes), "completed", True)])
    _run([], gh, IN_WINDOW, tmp_path)
    assert gh.dispatched is expected


def test_slack_edge_to_the_second(tmp_path):
    just_short = IN_WINDOW - timedelta(minutes=55) + timedelta(seconds=1)
    gh = FakeGh(runs=[(1, just_short, "completed", True)])
    _run([], gh, IN_WINDOW, tmp_path)
    assert not gh.dispatched


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 5, 17, 59, 59, tzinfo=UTC), False),
        (datetime(2026, 11, 5, 18, 0, 0, tzinfo=UTC), True),
        (datetime(2026, 11, 30, 21, 59, 0, tzinfo=UTC), True),
        (datetime(2026, 11, 30, 21, 59, 1, tzinfo=UTC), False),
    ],
)
def test_window_boundaries_are_inclusive(tmp_path, now, expected):
    gh = FakeGh(runs=[(1, _ago(now, 300), "completed", True)])
    _run([], gh, now, tmp_path)
    assert gh.dispatched is expected


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
def test_unfinished_run_blocks_dispatch(tmp_path, status):
    gh = FakeGh(
        runs=[
            (2, _ago(IN_WINDOW, 3), status, False),
            (1, _ago(IN_WINDOW, 300), "completed", True),
        ]
    )
    code, log = _run([], gh, IN_WINDOW, tmp_path)
    assert code == 0 and not gh.dispatched
    assert "scrape under way" in log.read_text()


def test_stuck_run_older_than_cap_does_not_block(tmp_path):
    gh = FakeGh(
        runs=[
            (2, _ago(IN_WINDOW, 7 * 60), "queued", False),
            (1, _ago(IN_WINDOW, 300), "completed", True),
        ]
    )
    _run([], gh, IN_WINDOW, tmp_path)
    assert gh.dispatched


def test_gate_skipped_run_is_not_a_scrape(tmp_path):
    # A gated run 5 minutes ago must not reset the clock; the scrape 2 h ago does.
    gh = FakeGh(
        runs=[
            (2, _ago(IN_WINDOW, 5), "completed", False),
            (1, _ago(IN_WINDOW, 120), "completed", True),
        ]
    )
    _run([], gh, IN_WINDOW, tmp_path)
    assert gh.dispatched


def test_any_event_scrape_counts(tmp_path):
    # The list is event-agnostic: a dispatched or cron run both reset the clock.
    gh = FakeGh(runs=[(1, _ago(IN_WINDOW, 10), "completed", True)])
    _run([], gh, IN_WINDOW, tmp_path)
    assert not gh.dispatched
    assert not any("--event" in c for c in gh.calls)


def test_no_scrape_in_24_hours_is_due(tmp_path):
    gh = FakeGh(
        runs=[
            (2, _ago(IN_WINDOW, 30), "completed", False),
            (1, _ago(IN_WINDOW, 25 * 60), "completed", True),
        ]
    )
    code, log = _run([], gh, IN_WINDOW, tmp_path)
    assert code == 0 and gh.dispatched
    assert "no scrape in the last 24 h" in log.read_text()


def test_empty_run_list_is_due(tmp_path):
    gh = FakeGh(runs=[])
    _run([], gh, IN_WINDOW, tmp_path)
    assert gh.dispatched


@pytest.mark.parametrize(
    "variables",
    [
        {},
        {"BF_PEAK_START_UTC": START},
        {"BF_PEAK_END_UTC": END},
    ],
)
def test_missing_variables_mean_no_window_and_baseline_off_skips(tmp_path, variables):
    gh = FakeGh(runs=[], variables=variables)
    code, log = _run([], gh, IN_WINDOW, tmp_path)
    assert code == 0 and not gh.dispatched
    assert "no peak window set" in log.read_text()


@pytest.mark.parametrize(
    "variables",
    [{}, {"BF_PEAK_START_UTC": START}, {"BF_PEAK_END_UTC": END}],
)
def test_missing_variables_still_honour_the_baseline_interval(tmp_path, variables):
    # No window is not "stop": the baseline cadence keeps running.
    due = FakeGh(runs=[(1, _ago(OUTSIDE, 130), "completed", True)], variables=variables)
    _run(["--baseline-interval", "120"], due, OUTSIDE, tmp_path)
    assert due.dispatched
    fresh = FakeGh(
        runs=[(1, _ago(OUTSIDE, 30), "completed", True)], variables=variables
    )
    _run(["--baseline-interval", "120"], fresh, OUTSIDE, tmp_path)
    assert not fresh.dispatched


def test_every_gh_call_names_the_repository(tmp_path):
    gh = FakeGh(runs=[(1, _ago(IN_WINDOW, 70), "completed", True)])
    _run([], gh, IN_WINDOW, tmp_path)
    assert gh.dispatched and len(gh.calls) >= 4
    for call in gh.calls:
        assert call[-2:] == ["--repo", "NaviAndrei/bf-price-monitor"], call


@pytest.mark.parametrize(
    "bad", ["not a date", "2026-11-05T18:00:00", "2026-13-45T00:00:00+00:00"]
)
def test_invalid_variables_fail_closed_with_exit_1(tmp_path, bad):
    gh = FakeGh(runs=[], variables={"BF_PEAK_START_UTC": bad, "BF_PEAK_END_UTC": END})
    code, _ = _run(["--baseline-interval", "120"], gh, IN_WINDOW, tmp_path)
    assert code == 1 and not gh.dispatched


@pytest.mark.parametrize(
    "fail", ["run list", "run view", "variable get", "workflow run"]
)
def test_gh_error_exits_1_without_dispatching(tmp_path, fail):
    gh = FakeGh(runs=[(1, _ago(IN_WINDOW, 300), "completed", True)], fail=fail)
    code, log = _run([], gh, IN_WINDOW, tmp_path)
    assert code == 1
    assert "\terror\t" in log.read_text()
    if fail != "workflow run":
        assert not gh.dispatched


@pytest.mark.parametrize(
    ("status", "hint"),
    [
        ("401", "recreate gh_token.dpapi"),
        ("403", "Actions: Read and write"),
        ("404", "grant it NaviAndrei/bf-price-monitor"),
    ],
)
def test_http_status_from_gh_is_explained_with_the_fix(tmp_path, status, hint):
    fake = FakeGh(runs=[(1, _ago(IN_WINDOW, 300), "completed", True)])

    def gh(args):
        if args[:2] == ["workflow", "run"]:
            raise peak_dispatch.GhError(
                f"gh workflow run failed (exit 1): HTTP {status}: denied"
            )
        return fake(args)

    code, log = _run([], gh, IN_WINDOW, tmp_path)
    line = log.read_text()
    assert code == 1
    assert "\terror\t" in line
    assert f"HTTP {status}" in line
    assert hint in line


def test_unparsable_run_list_exits_1(tmp_path):
    def gh(args):
        if args[:2] == ["variable", "get"]:
            return START if args[2].endswith("START_UTC") else END
        return "not json"

    code, _ = _run([], gh, IN_WINDOW, tmp_path)
    assert code == 1


def test_dry_run_never_dispatches(tmp_path, capsys):
    gh = FakeGh(runs=[(1, _ago(IN_WINDOW, 300), "completed", True)])
    code, log = _run(["--dry-run"], gh, IN_WINDOW, tmp_path)
    assert code == 0 and not gh.dispatched
    assert "would-dispatch (dry-run)" in capsys.readouterr().out
    assert "would-dispatch (dry-run)" in log.read_text()


def test_baseline_interval_applies_outside_the_window(tmp_path):
    due = FakeGh(runs=[(1, _ago(OUTSIDE, 130), "completed", True)])
    _run(["--baseline-interval", "120"], due, OUTSIDE, tmp_path)
    assert due.dispatched
    fresh = FakeGh(runs=[(1, _ago(OUTSIDE, 90), "completed", True)])
    _run(["--baseline-interval", "120"], fresh, OUTSIDE, tmp_path)
    assert not fresh.dispatched


def test_baseline_does_not_change_the_window_interval(tmp_path):
    gh = FakeGh(runs=[(1, _ago(IN_WINDOW, 30), "completed", True)])
    _run(["--baseline-interval", "10"], gh, IN_WINDOW, tmp_path)
    assert not gh.dispatched


def test_negative_baseline_is_bad_usage(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _run(["--baseline-interval", "-1"], FakeGh(), IN_WINDOW, tmp_path)
    assert exc.value.code == 2


def test_unknown_flag_is_bad_usage(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _run(["--nope"], FakeGh(), IN_WINDOW, tmp_path)
    assert exc.value.code == 2


def test_one_log_line_per_tick_and_no_token_leaks(tmp_path, capsys):
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"

    def gh(args):
        raise peak_dispatch.GhError(f"gh variable get failed (exit 1): token {secret}")

    log = tmp_path / "peak.log"
    code = peak_dispatch.main([], gh=gh, clock=lambda: IN_WINDOW, log_path=log)
    assert code == 1
    assert secret not in log.read_text() and secret not in capsys.readouterr().out
    assert len(log.read_text().splitlines()) == 1
    peak_dispatch.main([], gh=gh, clock=lambda: IN_WINDOW, log_path=log)
    assert len(log.read_text().splitlines()) == 2


def test_run_gh_redacts_tokens_from_stderr(monkeypatch):
    secret = "github_pat_" + "X9y8Z7w6V5u4"

    class Proc:
        returncode = 1
        stdout = ""
        stderr = f"bad credentials {secret}"

    monkeypatch.setattr(peak_dispatch.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(peak_dispatch.GhError) as exc:
        peak_dispatch.run_gh(["run", "list"])
    assert secret not in str(exc.value)


def test_log_is_truncated_when_over_one_megabyte(tmp_path):
    log = tmp_path / "peak.log"
    log.write_text("old line\n" * 150_000, encoding="utf-8")
    assert log.stat().st_size > peak_dispatch.LOG_MAX_BYTES
    peak_dispatch.write_log(log, IN_WINDOW, "not-due", "x")
    lines = log.read_text().splitlines()
    assert len(lines) == peak_dispatch.LOG_KEEP_LINES + 1
    assert lines[-1].endswith("not-due\tx")


def test_log_write_failure_does_not_crash(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    peak_dispatch.write_log(blocker / "sub" / "peak.log", IN_WINDOW, "skip", "r")
    assert "could not write log" in capsys.readouterr().err


def test_other_not_found_error_is_an_error_not_an_unset_variable(tmp_path):
    # A 404 from a token that cannot see the repo's variables must not look
    # like "window not configured".
    def denied(args):
        raise peak_dispatch.GhError(
            "gh variable get failed (exit 1): HTTP 404: Not Found"
        )

    code, _ = _run([], denied, IN_WINDOW, tmp_path)
    assert code == 1


def test_non_ascii_gh_error_does_not_crash_a_cp1252_console(tmp_path, capsys):
    def gh(args):
        raise peak_dispatch.GhError("gh failed: cafő ✓")

    log = tmp_path / "peak.log"
    code = peak_dispatch.main([], gh=gh, clock=lambda: IN_WINDOW, log_path=log)
    assert code == 1
    capsys.readouterr().out.encode("cp1252")
    assert log.read_text(encoding="utf-8").count("\n") == 1


def test_stuck_run_cap_is_two_hours(tmp_path):
    gh = FakeGh(
        runs=[
            (2, _ago(IN_WINDOW, 130), "queued", False),
            (1, _ago(IN_WINDOW, 300), "completed", True),
        ]
    )
    _run([], gh, IN_WINDOW, tmp_path)
    assert gh.dispatched
