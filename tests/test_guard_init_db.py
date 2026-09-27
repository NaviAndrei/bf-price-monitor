"""Regression tests for the T-46 (#64) preventive init_db guard.

The factory-level tests exercise conftest._make_guarded_init_db directly
against a tmp_path stand-in and a fake real_init_db -- never the real repo
path, and never a real sqlite3 connection -- so they cannot themselves
reproduce the bug they guard against. The binding tests below confirm the
autouse fixture actually installed the guard on all three module-level
names, using introspection and a stand-in path rather than ever invoking a
call that could reach the real DB.
"""

import notify
import pytest
import scrape
from conftest import _ORIGINAL_INIT_DB, _is_installed_guard, _make_guarded_init_db

from bf_price_monitor.storage import sqlite as sqlite_storage


def test_guarded_init_db_blocks_calls_matching_the_protected_path(tmp_path):
    protected = tmp_path / "stand-in-price_history.db"
    calls = []

    def fake_init_db(path):
        calls.append(path)
        return "unreachable-connection-stand-in"

    guarded = _make_guarded_init_db(fake_init_db, protected)

    with pytest.raises(pytest.fail.Exception, match="Refusing to open"):
        guarded(protected)
    assert calls == []  # the stand-in "real" open was never reached


def test_guarded_init_db_passes_through_for_other_paths(tmp_path):
    protected = tmp_path / "stand-in-price_history.db"
    other = tmp_path / "test.db"
    calls = []

    def fake_init_db(path):
        calls.append(path)
        return "fake-connection"

    guarded = _make_guarded_init_db(fake_init_db, protected)

    assert guarded(other) == "fake-connection"
    assert calls == [other]


def test_autouse_fixture_patches_all_three_init_db_bindings():
    # The _block_writes_to_real_price_history_db autouse fixture is already
    # active for this test, same as every other test in the suite. This is
    # purely introspective -- an identity comparison and an attribute check,
    # no call into any of these functions -- so it cannot reach the real DB
    # even if the guard were somehow wired incorrectly.
    assert sqlite_storage.init_db is not _ORIGINAL_INIT_DB["sqlite_storage"]
    assert scrape.init_db is not _ORIGINAL_INIT_DB["scrape"]
    assert notify.init_db is not _ORIGINAL_INIT_DB["notify"]

    assert _is_installed_guard(sqlite_storage.init_db)
    assert _is_installed_guard(scrape.init_db)
    assert _is_installed_guard(notify.init_db)


def test_guard_factory_blocks_stand_in_path_when_applied_to_real_init_db_references(
    tmp_path,
):
    # Confirms the factory correctly guards the *actual* production init_db
    # callables (captured before patching, in _ORIGINAL_INIT_DB) -- but wired
    # to a tmp_path decoy instead of the real repo path. The guard raises
    # before ever delegating to the wrapped callable, so even though these
    # are the real unwrapped functions, none of them is ever invoked.
    protected = tmp_path / "stand-in-price_history.db"
    for original in _ORIGINAL_INIT_DB.values():
        guarded = _make_guarded_init_db(original, protected)
        with pytest.raises(pytest.fail.Exception, match="Refusing to open"):
            guarded(protected)
