from pathlib import Path

import notify
import pytest
import scrape

from bf_price_monitor.storage import sqlite as sqlite_storage

# T-46 (#64): the real DB, git-ignored, so a stray write here shows no diff
# and can silently corrupt production price history.
_REAL_PRICE_HISTORY_DB = (
    Path(__file__).resolve().parent.parent / "data" / "price_history.db"
)

# T-46 (#64): a prior version of this file also ran a post-test alarm that
# fingerprinted data/price_history.db plus its -wal/-shm sidecars and failed
# if anything about them changed. Dropped after review, for two reasons:
#
# 1. Ordinary read-only SQLite access (e.g. a plain `mode=ro` connection for
#    an integrity check) creates or resizes the -wal/-shm sidecars as a side
#    effect of opening the WAL-mode database at all -- that's not proof any
#    row was written, so a byte-level fingerprint of those files produces
#    false alarms unrelated to any test.
# 2. This repo runs scrape.py/notify.py on a schedule (monitor.yml, a
#    self-hosted runner) that read AND write data/price_history.db outside
#    of any test process. A before/after snapshot taken by a pytest fixture
#    has no way to distinguish "this test wrote it" from "the scheduled job
#    wrote it while this test happened to be running" -- there is no cheap,
#    reliable way in this repo to attribute a change to the test process
#    specifically, short of a full content hash taken via a read-only
#    connection before and after every single test, which was rejected as
#    too expensive to run on every test in the suite.
#
# Isolation now relies entirely on the *preventive* guard below
# (_block_writes_to_real_price_history_db), which blocks a write attempt
# before it reaches disk rather than detecting one after the fact. Its scope
# and known bypasses:
#
# - Covers: any test path that reaches the real DB via
#   bf_price_monitor.storage.sqlite.init_db, scrape.init_db, or
#   notify.init_db -- which is every current call site exercised by this
#   test suite (scrape.py:1183, notify.py:984).
# - Does NOT cover: a call that opens the real path via a raw
#   sqlite3.connect(...) instead of going through init_db (e.g.
#   scripts/verify_migration.py:57 does exactly this). Not currently
#   exercised by any test, so not currently a live risk, but a future test
#   of that script would not be intercepted.
# - Does NOT cover: scripts/migrate_history_to_sqlite.py, which does its own
#   `from bf_price_monitor.storage.sqlite import init_db` -- a fourth
#   separately-bound name this fixture does not patch. Also not currently
#   exercised by any test. If either script gains test coverage, its
#   init_db/sqlite3.connect entry point needs the same guard treatment
#   applied here.


def _make_guarded_init_db(real_init_db, protected_path):
    """Wrap an init_db-shaped callable so it refuses to open protected_path.

    Kept as a standalone, parameterized factory (rather than inlined in the
    fixture) so a regression test can exercise the blocking behavior against
    a tmp_path stand-in and a fake real_init_db, without ever touching the
    real repo path or opening a real sqlite3 connection. The wrapper is
    tagged with _is_t46_write_guard so a test can also confirm, purely by
    introspection, that a given attribute is one of these wrappers rather
    than the original unwrapped function.
    """
    protected_resolved = Path(protected_path).resolve()

    def _guarded(db_path, *args, **kwargs):
        if Path(db_path).resolve() == protected_resolved:
            pytest.fail(
                "Refusing to open the real data/price_history.db for writing "
                "during a test (T-46, #64). Redirect the module's DB_FILE (or "
                "equivalent) to tmp_path instead."
            )
        return real_init_db(db_path, *args, **kwargs)

    _guarded._is_t46_write_guard = True
    return _guarded


def _is_installed_guard(candidate) -> bool:
    return getattr(candidate, "_is_t46_write_guard", False)


# Captured once at import time, before any fixture has patched these names,
# so a test can later assert the live attribute is no longer this original
# reference -- proof the autouse fixture below actually ran, without calling
# anything.
_ORIGINAL_INIT_DB = {
    "sqlite_storage": sqlite_storage.init_db,
    "scrape": scrape.init_db,
    "notify": notify.init_db,
}


@pytest.fixture(autouse=True)
def _block_writes_to_real_price_history_db(monkeypatch):
    # scrape.py and notify.py each did `from bf_price_monitor.storage.sqlite
    # import init_db`, which binds a separate name in their own module
    # namespace at import time -- patching sqlite_storage.init_db alone would
    # not reach either of those already-bound aliases, so all three names
    # are wrapped independently.
    monkeypatch.setattr(
        sqlite_storage,
        "init_db",
        _make_guarded_init_db(sqlite_storage.init_db, _REAL_PRICE_HISTORY_DB),
    )
    monkeypatch.setattr(
        scrape, "init_db", _make_guarded_init_db(scrape.init_db, _REAL_PRICE_HISTORY_DB)
    )
    monkeypatch.setattr(
        notify, "init_db", _make_guarded_init_db(notify.init_db, _REAL_PRICE_HISTORY_DB)
    )
