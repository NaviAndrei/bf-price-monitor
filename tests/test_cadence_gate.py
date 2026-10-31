"""T-32 (#43): BF-week adaptive cadence gate.

Pure-function tests for scripts/cadence_gate.py -- no network, no workflow
execution. See docs/runbooks/BF_WEEK_CADENCE.md for the operational decision
this backs.
"""

from datetime import UTC, datetime

import cadence_gate


class _FrozenDatetime:
    # Stand-in for the `datetime` name inside cadence_gate, exposing just the
    # two calls that module actually makes (datetime.now(UTC) and
    # datetime.fromisoformat(...)) -- simpler than subclassing datetime.
    def __init__(self, frozen: datetime) -> None:
        self._frozen = frozen

    def now(self, tz=None):
        return self._frozen

    def fromisoformat(self, value):
        return datetime.fromisoformat(value)


def _freeze_now(monkeypatch, frozen: datetime) -> None:
    monkeypatch.setattr(cadence_gate, "datetime", _FrozenDatetime(frozen))


def test_baseline_cron_always_runs_regardless_of_window():
    assert cadence_gate.should_run("schedule", "0 */2 * * *") is True


def test_manual_dispatch_always_runs():
    assert cadence_gate.should_run("workflow_dispatch", None) is True


def test_tight_cadence_runs_inside_the_configured_window(monkeypatch):
    monkeypatch.setenv("BF_PEAK_START_UTC", "2026-11-26T18:00:00+00:00")
    monkeypatch.setenv("BF_PEAK_END_UTC", "2026-11-27T23:59:00+00:00")
    _freeze_now(monkeypatch, datetime(2026, 11, 27, 10, 0, tzinfo=UTC))

    assert cadence_gate.should_run("schedule", cadence_gate.TIGHT_CADENCE_CRON) is True


def test_tight_cadence_does_not_run_outside_the_configured_window(monkeypatch):
    monkeypatch.setenv("BF_PEAK_START_UTC", "2026-11-26T18:00:00+00:00")
    monkeypatch.setenv("BF_PEAK_END_UTC", "2026-11-27T23:59:00+00:00")
    _freeze_now(monkeypatch, datetime(2026, 11, 28, 0, 1, tzinfo=UTC))

    assert cadence_gate.should_run("schedule", cadence_gate.TIGHT_CADENCE_CRON) is False


def test_tight_cadence_fails_closed_when_window_is_unconfigured(monkeypatch):
    monkeypatch.delenv("BF_PEAK_START_UTC", raising=False)
    monkeypatch.delenv("BF_PEAK_END_UTC", raising=False)

    assert cadence_gate.should_run("schedule", cadence_gate.TIGHT_CADENCE_CRON) is False


def test_is_in_peak_window_rejects_naive_boundaries():
    # Naive datetimes can't be compared against an aware now_utc without an
    # implicit (and easy to get wrong) assumption about the missing offset --
    # treated as misconfiguration rather than guessed at.
    now = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
    assert (
        cadence_gate.is_in_peak_window(
            now, "2026-11-26T18:00:00", "2026-11-27T23:59:00"
        )
        is False
    )


def test_is_in_peak_window_rejects_unparsable_boundaries():
    now = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
    assert cadence_gate.is_in_peak_window(now, "not-a-date", "also-not-a-date") is False


_START = "2026-11-05T18:00:00+00:00"
_END = "2026-11-30T21:59:00+00:00"


def test_tight_cron_is_the_odd_hour_cron():
    # Odd UTC hours never share a minute with the even-hour baseline cron.
    assert cadence_gate.TIGHT_CADENCE_CRON == "0 1-23/2 * * *"


def test_odd_hour_run_inside_the_window_scrapes(monkeypatch):
    monkeypatch.setenv("BF_PEAK_START_UTC", _START)
    monkeypatch.setenv("BF_PEAK_END_UTC", _END)
    _freeze_now(monkeypatch, datetime(2026, 11, 20, 13, 0, tzinfo=UTC))

    assert cadence_gate.should_run("schedule", cadence_gate.TIGHT_CADENCE_CRON) is True


def test_odd_hour_run_before_and_after_the_window_is_skipped(monkeypatch):
    monkeypatch.setenv("BF_PEAK_START_UTC", _START)
    monkeypatch.setenv("BF_PEAK_END_UTC", _END)
    for moment in (
        datetime(2026, 11, 5, 17, 0, tzinfo=UTC),
        datetime(2026, 11, 30, 23, 0, tzinfo=UTC),
    ):
        _freeze_now(monkeypatch, moment)
        assert (
            cadence_gate.should_run("schedule", cadence_gate.TIGHT_CADENCE_CRON)
            is False
        )


def test_window_boundaries_are_inclusive():
    start = datetime(2026, 11, 5, 18, 0, tzinfo=UTC)
    end = datetime(2026, 11, 30, 21, 59, tzinfo=UTC)
    assert cadence_gate.is_in_peak_window(start, _START, _END) is True
    assert cadence_gate.is_in_peak_window(end, _START, _END) is True


def test_old_half_hour_string_is_ungated_so_the_constant_must_match_the_workflow():
    assert cadence_gate.should_run("schedule", "*/30 * * * *") is True


def test_tight_cadence_fails_closed_on_invalid_variables(monkeypatch):
    monkeypatch.setenv("BF_PEAK_START_UTC", "not-a-date")
    monkeypatch.setenv("BF_PEAK_END_UTC", _END)
    _freeze_now(monkeypatch, datetime(2026, 11, 20, 13, 0, tzinfo=UTC))

    assert cadence_gate.should_run("schedule", cadence_gate.TIGHT_CADENCE_CRON) is False
