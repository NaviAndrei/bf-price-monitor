# Migration checklist

Every line below must be answered with evidence (a command you ran, a
`file:line` you read) before a migration is called done. "Looks right" is
not evidence.

## The two traps (verbatim from `CLAUDE.md`)

- **Schema-valid is not production-safe:** Validate migration output programmatically against the actual validator (e.g. `validate_watchlist()`) before approving promotion — never by eyeballing printed JSON.
- **Schema validation misses undeclared downstream dependencies:** A field a downstream consumer reads at runtime may never be declared in the schema. Grep actual runtime usage before claiming any migration is "1:1, no behavior loss."

## Checklist

| # | Check | Evidence required | Trap |
|---|---|---|---|
| 1 | Read `docs/DECISIONS.md` for entries touching the tables or fields involved (T-28 #37, T-47 #66, #69 are the schema precedents). | Quote the entry or state "none found" with the grep used. | both |
| 2 | Classify the migration: schema change (`_MIGRATIONS`), history import (`migrate_history_to_sqlite.py`), or watchlist shape (`migrate_watchlist_to_modern.py`). | One line naming the kind. | 1 |
| 3 | Schema change: append `(N+1, (statements...))` to `_MIGRATIONS` in `bf_price_monitor/storage/sqlite.py`. Never edit an applied version. Do not set `SCHEMA_VERSION` by hand; it is derived from the last entry. | `git diff` shows only an appended tuple. | 1 |
| 4 | Additive only: new tables, indexes or nullable columns. Anything destructive or type-changing needs an explicit approval from the user before you write it. | State what the statements do to existing rows. | 1 |
| 5 | Never run against `data/price_history.db` (the real, gitignored database). Use `tmp_path` or a copy. | Show the path used. | 1 |
| 6 | Validate with the real validator, not by printing rows: schema change = the `tests/unit/test_sqlite_storage.py` and `tests/unit/test_feedback_storage.py` suites; history import = `python scripts/verify_migration.py` (recomputes counts from the source JSON independently); watchlist = `validate_watchlist()` from `bf_price_monitor/config/validator.py`. | Command and exit code. | 1 |
| 7 | Grep for runtime readers of every table or column you changed or that the new one touches: `grep -rn "<name>" scripts bf_price_monitor tests`. Include `scripts/runtime_state.py` (it snapshots and restores the database) and `scripts/report.py`, `scripts/generate_retro_metrics.py`. Do not conclude "no behavior loss" without this output. | Paste the grep hits and say which readers are affected. | 2 |
| 8 | Add a test that applies the migration twice. Pattern: `test_reopening_is_idempotent_and_keeps_the_salt` in `tests/unit/test_feedback_storage.py` (open, close, `init_db` again, assert state unchanged). Also assert `_user_version(conn) == SCHEMA_VERSION`. | Test name and its pass output. | 1 |
| 9 | Add or keep a rollback test: `test_failed_migration_rolls_back_completely` shows the pattern (a broken statement leaves `user_version` unchanged). | Pass output. | 1 |
| 10 | Existing tests compare against `SCHEMA_VERSION` symbolically; confirm none hardcodes the old number (`grep -rn "user_version" tests`). | Grep output. | 2 |
| 11 | `ruff check .`, `ruff format --check .`, `python -m pytest -x --tb=short -q` all exit 0. | Three exit codes. | 1 |
| 12 | Record the why in `docs/DECISIONS.md` when the change is structural. Do not add to `docs/progress.md` unless the user asks. | Diff of the entry. | both |
